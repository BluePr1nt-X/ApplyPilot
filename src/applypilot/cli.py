"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
    ats_safe: bool = typer.Option(False, "--ats-safe",
        help="Force ATS-safe PDF style (single column, plain Arial, no styling) "
             "for ALL resumes. Without this flag, the per-domain registry decides."),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
        pdf_engine="ats_safe" if ats_safe else None,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier
    from applypilot.profiles import router as _profiles
    _profile_path = _profiles.profile_file()
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command(name="alert-test")
def alert_test() -> None:
    """Fire a test alert to every configured sink (Slack/email/webhook)."""
    _bootstrap()
    from applypilot import alerts
    if not alerts.is_configured():
        console.print("[yellow]No alert sinks configured.[/yellow] "
                      "Set ALERT_SLACK_WEBHOOK, ALERT_WEBHOOK_URL, or "
                      "ALERT_EMAIL_SMTP_HOST in ~/.applypilot/.env.")
        raise typer.Exit(code=1)
    results = alerts.notify("test", note="manual test alert from `applypilot alert-test`")
    for sink, ok in results.items():
        mark = "[green]ok[/green]" if ok else "[red]fail[/red]"
        console.print(f"  {sink}: {mark}")


@app.command()
def profile(
    action: str = typer.Argument(
        "list",
        help="One of: list, add, use, show, delete.",
    ),
    name: Optional[str] = typer.Argument(None, help="Profile name (for add/use/show/delete)."),
    copy_from: Optional[str] = typer.Option(
        None, "--copy-from",
        help="When adding, seed the new profile by copying files from another."),
) -> None:
    """Manage profile families (different resumes per role family)."""
    _bootstrap()

    from applypilot.profiles import router as profiles_mod

    action = action.lower()

    if action == "list":
        profiles_list = profiles_mod.list_profiles()
        if not profiles_list:
            console.print("[yellow]No profiles found.[/yellow] Run `applypilot init` to create one.")
            return
        active_name = profiles_mod.get_active()
        table = Table(title="Profiles", show_header=True, header_style="bold cyan")
        table.add_column("Name")
        table.add_column("Active")
        table.add_column("profile.json")
        table.add_column("resume.txt")
        table.add_column("resume.pdf")
        table.add_column("Path")
        for p in profiles_list:
            yes = "[green]yes[/green]" if p.is_active else ""
            tick = lambda b: "[green]ok[/green]" if b else "[red]-[/red]"  # noqa: E731
            table.add_row(p.name, yes,
                          tick(p.has_profile_json), tick(p.has_resume_txt),
                          tick(p.has_resume_pdf), str(p.dir))
        console.print(table)
        console.print(f"\n[dim]Active profile: {active_name}[/dim]")
        return

    if action == "add":
        if not name:
            console.print("[red]Profile name required for `add`.[/red]")
            raise typer.Exit(code=1)
        try:
            new_dir = profiles_mod.add_profile(name, copy_from=copy_from)
        except (FileExistsError, FileNotFoundError, ValueError) as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=1)
        if copy_from:
            console.print(f"[green]Created profile '{name}'[/green] at {new_dir} "
                          f"(seeded from '{copy_from}').")
        else:
            console.print(f"[green]Created profile '{name}'[/green] at {new_dir}. "
                          f"Switch to it (`applypilot profile use {name}`) and run "
                          f"`applypilot init` to populate it.")
        return

    if action == "use":
        if not name:
            console.print("[red]Profile name required for `use`.[/red]")
            raise typer.Exit(code=1)
        try:
            profiles_mod.set_active(name)
        except (FileNotFoundError, ValueError) as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]Active profile set to[/green] [bold]{name}[/bold]")
        return

    if action == "show":
        target_name = name or profiles_mod.get_active()
        try:
            data = profiles_mod.load_profile(target_name)
        except FileNotFoundError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=1)
        import json as _json
        console.print(f"[bold]Profile: {target_name}[/bold]")
        console.print(f"[dim]Path: {profiles_mod.profile_file(target_name)}[/dim]\n")
        console.print(_json.dumps(data, indent=2, ensure_ascii=False))
        return

    if action == "delete":
        if not name:
            console.print("[red]Profile name required for `delete`.[/red]")
            raise typer.Exit(code=1)
        try:
            ok = profiles_mod.delete_profile(name)
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=1)
        if ok:
            console.print(f"[green]Deleted profile[/green] {name}")
        else:
            console.print(f"[yellow]No profile found for[/yellow] {name}")
        return

    console.print(f"[red]Unknown action:[/red] {action}. Use one of: list, add, use, show, delete.")
    raise typer.Exit(code=1)


@app.command(name="validate-pdf")
def validate_pdf_cmd(
    pdf_path: str = typer.Argument(..., help="Path to the PDF to round-trip check."),
    source_text: Optional[str] = typer.Option(
        None, "--source",
        help="Source resume .txt for token derivation. Defaults to <pdf>.txt sibling."),
    show_text: bool = typer.Option(False, "--show-text",
                                   help="Print the extracted text after the result."),
) -> None:
    """Round-trip extract a PDF with pdfplumber and verify critical tokens survive.

    Use this to debug 'my resume disappears in Workday' issues.
    """
    _bootstrap()
    from pathlib import Path
    from applypilot.scoring.pdf_validator import validate_pdf

    pdf = Path(pdf_path)
    if not pdf.exists():
        console.print(f"[red]PDF not found:[/red] {pdf}")
        raise typer.Exit(code=1)

    src_path = Path(source_text) if source_text else pdf.with_suffix(".txt")
    expected_text = src_path.read_text(encoding="utf-8") if src_path.exists() else None
    if expected_text is None:
        console.print(f"[yellow]No source .txt found at {src_path}[/yellow] — "
                      "validator will only confirm extraction succeeded.")

    result = validate_pdf(pdf, expected_text=expected_text or "")
    if result.passed:
        console.print(f"[green]PASS[/green] — round-trip extraction looks ATS-safe.")
        console.print(f"  notes: {result.notes}")
    else:
        console.print(f"[red]FAIL[/red] — extracted text is missing tokens.")
        if result.missing_tokens:
            console.print("  missing:")
            for t in result.missing_tokens:
                console.print(f"    - {t}")
        if result.notes:
            console.print(f"  notes: {result.notes}")

    if show_text:
        console.print("\n[bold]Extracted text:[/bold]")
        console.print(result.extracted_text)


@app.command()
def inbox(
    action: str = typer.Argument(
        "poll",
        help="One of: add, add-gmail, list, poll, stats, review, remove, test.",
    ),
    email: Optional[str] = typer.Option(None, "--email", help="Email address (for add/remove/test)."),
    host: Optional[str] = typer.Option(None, "--host", help="IMAP host (e.g. imap.gmail.com)."),
    port: int = typer.Option(993, "--port"),
    no_ssl: bool = typer.Option(False, "--no-ssl"),
    password: Optional[str] = typer.Option(
        None, "--password",
        help="Password (App Password for Gmail). Prefer leaving blank — you'll be prompted.",
    ),
    no_llm: bool = typer.Option(False, "--no-llm",
                                help="Disable LLM fallback for ambiguous emails."),
    max_per_poll: int = typer.Option(200, "--max", help="Max emails per poll."),
    threshold: float = typer.Option(0.70, "--threshold",
                                    help="Confidence threshold for `review`."),
    weeks: int = typer.Option(4, "--weeks", help="Window for `stats`."),
) -> None:
    """Outcome feedback loop — IMAP poll, classify, reconcile to jobs."""
    _bootstrap()

    from applypilot.feedback import inbox as inbox_mod
    from applypilot.feedback import reconciler as recon_mod
    import getpass

    action = action.lower()

    if action == "list":
        accounts = inbox_mod.list_accounts()
        if not accounts:
            console.print("[yellow]No inbox accounts configured.[/yellow] "
                          "Run `applypilot inbox add` to set one up.")
            return
        table = Table(title="Inbox Accounts", show_header=True, header_style="bold cyan")
        table.add_column("Email")
        table.add_column("Host")
        table.add_column("Port")
        table.add_column("Last UID", justify="right")
        table.add_column("Last polled")
        table.add_column("Status")
        for a in accounts:
            table.add_row(a.email, a.host, str(a.port),
                          str(a.last_uid_seen), a.last_polled_at or "-",
                          a.status)
        console.print(table)
        return

    if action == "add":
        if not email or not host:
            console.print("[red]--email and --host are required for `add`.[/red]")
            raise typer.Exit(code=1)
        pw = password or getpass.getpass(f"Password for {email} (App Password for Gmail): ")
        ok, msg = inbox_mod.test_connection(email, host, port, not no_ssl, pw)
        if not ok:
            console.print(f"[red]Login failed:[/red] {msg}")
            raise typer.Exit(code=1)
        inbox_mod.store_password(email, pw)
        inbox_mod.add_account(email, host, port=port, ssl=not no_ssl, auth_method="password")
        console.print(f"[green]Added inbox account:[/green] {email} @ {host}:{port}")
        return

    if action == "add-gmail":
        if not email:
            console.print("[red]--email is required for `add-gmail`.[/red]")
            raise typer.Exit(code=1)
        from applypilot.feedback import oauth_gmail
        if not oauth_gmail.is_available():
            console.print(
                "[red]google-auth-oauthlib is not installed.[/red]\n"
                "Install: [bold]pip install google-auth google-auth-oauthlib[/bold]"
            )
            raise typer.Exit(code=1)
        console.print(
            "[bold]Gmail OAuth setup[/bold]\n"
            "1. Create a Google Cloud project + OAuth 2.0 Client ID (Desktop App).\n"
            f"2. Download credentials JSON to {oauth_gmail.DEFAULT_CLIENT_SECRET_PATH}\n"
            "   OR set GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET in .env.\n"
            "3. Continue when ready — your browser will open for consent.\n"
        )
        if not typer.confirm("Continue?", default=True):
            raise typer.Exit(code=0)
        try:
            oauth_gmail.authorize(email)
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=1)
        inbox_mod.add_account(
            email, oauth_gmail.GMAIL_IMAP_HOST,
            port=oauth_gmail.GMAIL_IMAP_PORT, ssl=True,
            auth_method="oauth_gmail",
        )
        console.print(f"[green]Added Gmail OAuth account:[/green] {email}")
        return

    if action == "remove":
        if not email:
            console.print("[red]--email is required for `remove`.[/red]")
            raise typer.Exit(code=1)
        ok = inbox_mod.remove_account(email)
        if ok:
            console.print(f"[green]Removed:[/green] {email}")
        else:
            console.print(f"[yellow]No account found for[/yellow] {email}")
        return

    if action == "test":
        if not email or not host:
            console.print("[red]--email and --host are required for `test`.[/red]")
            raise typer.Exit(code=1)
        pw = password or inbox_mod.load_password(email) or getpass.getpass(
            f"Password for {email}: ")
        ok, msg = inbox_mod.test_connection(email, host, port, not no_ssl, pw)
        if ok:
            console.print(f"[green]Connection OK:[/green] {email} @ {host}:{port}")
        else:
            console.print(f"[red]Connection failed:[/red] {msg}")
            raise typer.Exit(code=1)
        return

    if action == "poll":
        accounts = inbox_mod.list_accounts()
        if not accounts:
            console.print("[yellow]No inbox accounts configured.[/yellow]")
            return
        for a in accounts:
            if a.status != "active":
                continue
            console.print(f"\n[cyan]Polling[/cyan] {a.email} ...")
            res = inbox_mod.poll_account(
                a, use_llm_fallback=not no_llm, max_per_poll=max_per_poll,
            )
            console.print(
                f"  fetched={res.fetched} classified={res.classified} "
                f"matched={res.matched} skipped={res.skipped} "
                f"errors={res.errors} last_uid={res.new_uid}"
            )
        return

    if action == "stats":
        s = recon_mod.stats(weeks=weeks)
        console.print()
        if s["by_label"]:
            console.print("[bold]Outcomes (all-time)[/bold]")
            for label in ("acknowledged", "rejected", "interview", "offer", "ghosted"):
                if label in s["by_label"]:
                    console.print(f"  {label:14s} {s['by_label'][label]}")
        if s["by_site"]:
            console.print(f"\n[bold]By site (last {weeks} weeks)[/bold]")
            t = Table(show_header=True, header_style="bold magenta")
            t.add_column("Site")
            t.add_column("Apps", justify="right")
            t.add_column("Acks", justify="right")
            t.add_column("Rejects", justify="right")
            t.add_column("Interviews", justify="right")
            t.add_column("Offers", justify="right")
            for row in s["by_site"]:
                t.add_row(row["site"], str(row["apps"]), str(row["acks"]),
                          str(row["rejections"]), str(row["interviews"]),
                          str(row["offers"]))
            console.print(t)
        return

    if action == "review":
        rows = recon_mod.list_low_confidence(threshold=threshold)
        if not rows:
            console.print("[green]No low-confidence matches[/green] "
                          f"(below {threshold}). Nothing to review.")
            return
        t = Table(title=f"Low-confidence outcomes (< {threshold})",
                  show_header=True, header_style="bold yellow")
        t.add_column("Title")
        t.add_column("Site")
        t.add_column("Outcome")
        t.add_column("Conf", justify="right")
        t.add_column("Subject")
        for r in rows:
            t.add_row(
                (r["title"] or "")[:40], (r["site"] or "")[:20],
                r["outcome"], f"{r['outcome_confidence']:.2f}",
                (r["outcome_subject"] or "")[:50],
            )
        console.print(t)
        return

    console.print(f"[red]Unknown action:[/red] {action}. "
                  "Use one of: add, list, poll, stats, review, remove, test.")
    raise typer.Exit(code=1)


@app.command()
def budget(
    days: int = typer.Option(7, "--days", help="History rollup window."),
    set_daily: Optional[float] = typer.Option(
        None, "--set-daily-cap",
        help="Persist a new CAPSOLVER_DAILY_USD_CAP to ~/.applypilot/.env"),
    set_per_job: Optional[int] = typer.Option(
        None, "--set-per-job-max",
        help="Persist a new CAPSOLVER_PER_JOB_MAX to ~/.applypilot/.env"),
) -> None:
    """Show CAPTCHA / spend budget status and configure caps."""
    _bootstrap()

    from applypilot import budget as budget_mod
    from applypilot.config import ENV_PATH

    if set_daily is not None or set_per_job is not None:
        _persist_env_var(ENV_PATH, {
            **({"CAPSOLVER_DAILY_USD_CAP": f"{set_daily:.2f}"} if set_daily is not None else {}),
            **({"CAPSOLVER_PER_JOB_MAX": str(set_per_job)} if set_per_job is not None else {}),
        })
        from applypilot.config import load_env
        load_env()
        console.print(f"[green]Saved to[/green] {ENV_PATH}")

    s = budget_mod.summary(days=days)
    console.print()
    over = "[red]OVER CAP[/red]" if s["today_over_cap"] else "[green]OK[/green]"
    console.print(f"[bold]Budget — today ({s['today_date']} UTC)[/bold]  {over}")
    console.print(f"  CAPTCHA spend:  ${s['today_spent_usd']:.4f} / ${s['today_cap_usd']:.2f} "
                  f"(remaining ${s['today_remaining_usd']:.4f})")
    console.print(f"  Per-job max:    {s['per_job_max']} CAPTCHAs before kill switch")

    if s["history"]:
        console.print(f"\n[bold]History (last {days} days)[/bold]")
        h_table = Table(show_header=True, header_style="bold cyan")
        h_table.add_column("Date (UTC)")
        h_table.add_column("CAPTCHA $", justify="right")
        h_table.add_column("LLM $", justify="right")
        h_table.add_column("Apps Sent", justify="right")
        for row in s["history"]:
            h_table.add_row(row["date"], f"${row['captcha_usd']:.4f}",
                            f"${row['llm_usd']:.4f}", str(row["applications_sent"]))
        console.print(h_table)

    if s["by_type"]:
        console.print(f"\n[bold]By CAPTCHA type (last {days} days)[/bold]")
        t_table = Table(show_header=True, header_style="bold magenta")
        t_table.add_column("Type")
        t_table.add_column("Count", justify="right")
        t_table.add_column("Spend $", justify="right")
        for row in s["by_type"]:
            t_table.add_row(row["type"], str(row["count"]), f"${row['spend_usd']:.4f}")
        console.print(t_table)


def _persist_env_var(env_path, kv: dict[str, str]) -> None:
    """Read .env, replace or append given keys, write back."""
    from pathlib import Path
    p = Path(env_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    existing_keys = {}
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            k, _ = line.split("=", 1)
            existing_keys[k.strip()] = i
    for k, v in kv.items():
        if k in existing_keys:
            lines[existing_keys[k]] = f"{k}={v}"
        else:
            lines.append(f"{k}={v}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.command()
def login(
    domain: Optional[str] = typer.Argument(
        None,
        help="Domain to seed (e.g. myworkdayjobs.com). Omit with --list to view vault.",
    ),
    login_url: Optional[str] = typer.Option(None, "--login-url",
                                            help="Override the registry login URL."),
    list_sessions: bool = typer.Option(False, "--list", help="List vault entries and exit."),
    refresh: bool = typer.Option(False, "--refresh", help="Re-seed an existing entry."),
    delete: bool = typer.Option(False, "--delete", help="Remove a vault entry and its file."),
    set_password: bool = typer.Option(False, "--set-password",
        help="Set or change the master password used to encrypt vault files. "
             "Re-encrypts all existing entries afterwards."),
    clear_password: bool = typer.Option(False, "--clear-password",
        help="Remove the master password (decrypts all vault files back to plaintext)."),
) -> None:
    """Seed and manage the persistent login vault for ATS / employer sites.

    Run `applypilot login myworkdayjobs.com` to log into Workday once via SSO;
    the cookies will be reused by future apply runs so the agent never sees a
    login wall on that domain.
    """
    _bootstrap()

    from applypilot.auth import vault

    # Vault encryption mgmt
    if set_password or clear_password:
        import getpass as _gp
        if clear_password:
            console.print("[yellow]Clearing master password and converting "
                          "vault files to plaintext...[/yellow]")
            migrated, errors = vault.change_master_password(None)
        else:
            new_pw = _gp.getpass("New vault master password: ")
            confirm = _gp.getpass("Confirm: ")
            if new_pw != confirm or not new_pw:
                console.print("[red]Passwords don't match (or empty).[/red]")
                raise typer.Exit(code=1)
            console.print("[green]Setting master password and encrypting "
                          "all vault files...[/green]")
            migrated, errors = vault.change_master_password(new_pw)
        console.print(f"  migrated {migrated} file(s), errors={errors}")
        return

    if list_sessions:
        entries = vault.list_sessions()
        enc_label = "[green]encrypted[/green]" if vault.encryption_enabled() else "[yellow]plaintext[/yellow]"
        console.print(f"[dim]Vault encryption: {enc_label}[/dim]")
        if not entries:
            console.print("[yellow]Vault is empty.[/yellow] Run `applypilot login <domain>` to seed.")
            return
        table = Table(title="Auth Vault", show_header=True, header_style="bold cyan")
        table.add_column("Domain")
        table.add_column("Status")
        table.add_column("Seeded")
        table.add_column("Expires (days)")
        table.add_column("Path")
        for e in entries:
            status = "[green]fresh[/green]" if vault.is_fresh(e) else "[red]stale[/red]"
            table.add_row(
                e.domain, status, e.seeded_at or "-",
                str(e.expiry_days), e.storage_state_path or "-",
            )
        console.print(table)
        return

    if not domain:
        console.print("[red]Provide a domain[/red] (e.g. `applypilot login myworkdayjobs.com`) "
                      "or use `--list`.")
        raise typer.Exit(code=1)

    if delete:
        ok = vault.delete_session(domain)
        if ok:
            console.print(f"[green]Deleted vault entry:[/green] {domain}")
        else:
            console.print(f"[yellow]No vault entry found for[/yellow] {domain}")
        return

    if refresh:
        existing = vault.get_entry(domain)
        if existing is None:
            console.print(f"[yellow]No existing entry for {domain} — seeding fresh.[/yellow]")

    try:
        path = vault.seed_session(domain, login_url=login_url)
        console.print(f"[bold green]Vault updated:[/bold green] {domain} → {path}")
    except SystemExit as e:
        console.print(f"[yellow]{e}[/yellow]")
        raise typer.Exit(code=1)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)


@app.command()
def watch(
    daily_quota: int = typer.Option(30, "--daily-quota", help="Max applications per UTC day."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover/apply."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel browser/scraper workers."),
    validation: str = typer.Option("normal", "--validation", help="Tailor/cover validation mode."),
    quiet_hours: str = typer.Option("23:00-07:00", "--quiet-hours",
                                    help="Apply paused during HH:MM-HH:MM (local time). Empty to disable."),
    apply_batch: int = typer.Option(5, "--apply-batch",
                                    help="Max applications per apply tick."),
    apply_cron: str = typer.Option("*/15 * * * *", "--apply-cron"),
    discover_cron: str = typer.Option("0 */6 * * *", "--discover-cron"),
    enrich_cron: str = typer.Option("*/30 * * * *", "--enrich-cron"),
    score_cron: str = typer.Option("0 * * * *", "--score-cron"),
    tailor_cron: str = typer.Option("*/20 * * * *", "--tailor-cron"),
    cover_cron: str = typer.Option("*/25 * * * *", "--cover-cron"),
    pdf_cron: str = typer.Option("*/30 * * * *", "--pdf-cron"),
    apply_model: str = typer.Option("haiku", "--apply-model",
                                    help="Claude model used for the apply agent."),
    headless: bool = typer.Option(True, "--headless/--no-headless",
                                  help="Run apply browsers headless (default true for daemons)."),
    once: bool = typer.Option(False, "--once",
                              help="Run every stage once, then apply once, and exit. "
                                   "Use this with OS-level cron if you don't want apscheduler in-process."),
    show_status: bool = typer.Option(False, "--status",
                                     help="Print recent daemon events + heartbeat and exit."),
    stop: bool = typer.Option(False, "--stop",
                              help="Signal a running daemon to stop and exit."),
) -> None:
    """Run ApplyPilot autonomously: scheduled discovery + apply with daily quota."""
    _bootstrap()

    from applypilot import daemon as daemon_mod

    if show_status:
        s = daemon_mod.read_status()
        if s.get("heartbeat"):
            age = s.get("heartbeat_age_s")
            age_str = f"{age:.0f}s ago" if age is not None else "unknown"
            console.print(f"[bold]Heartbeat:[/bold] {s['heartbeat']} ({age_str})")
        else:
            console.print("[yellow]No heartbeat — daemon not running yet.[/yellow]")
        for event in s.get("events", []):
            console.print(f"  {event}")
        return

    if stop:
        daemon_mod.request_stop()
        console.print("[green]Stop signal written[/green] — daemon will exit on next poll (within 5s).")
        return

    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(f"[red]Invalid --validation:[/red] {validation}. Choose from: {', '.join(valid_modes)}")
        raise typer.Exit(code=1)

    from applypilot.config import check_tier
    check_tier(3, "applypilot watch (full autonomy)")

    cfg = daemon_mod.WatchConfig(
        discover_cron=discover_cron, enrich_cron=enrich_cron, score_cron=score_cron,
        tailor_cron=tailor_cron, cover_cron=cover_cron, pdf_cron=pdf_cron,
        apply_cron=apply_cron, daily_quota=daily_quota, min_score=min_score,
        workers=workers, validation=validation, headless=headless,
        apply_model=apply_model, quiet_hours=quiet_hours, apply_batch_size=apply_batch,
    )

    if once:
        console.print("[bold cyan]applypilot watch --once[/bold cyan] — running every stage once")
        daemon_mod.run_once(cfg)
        console.print("[green]Done.[/green]")
        return

    console.print("[bold cyan]applypilot watch[/bold cyan] — daemon mode (Ctrl+C to stop)")
    console.print(f"  daily quota: {daily_quota}, batch: {apply_batch}, quiet: {quiet_hours or 'none'}")
    console.print(f"  apply: {apply_cron}, discover: {discover_cron}, score: {score_cron}")
    daemon_mod.run_forever(cfg)


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Active profile + its files
    from applypilot.profiles import router as _profiles
    _profiles.migrate_legacy()
    _active = _profiles.get_active()
    _profile_file = _profiles.profile_file()
    _resume_txt = _profiles.resume_text()
    _resume_pdf = _profiles.resume_pdf()
    results.append(("active profile", ok_mark, f"{_active} ({_profile_file.parent})"))

    # profile.json
    if _profile_file.exists():
        results.append(("profile.json", ok_mark, str(_profile_file)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if _resume_txt.exists():
        results.append(("resume.txt", ok_mark, str(_resume_txt)))
    elif _resume_pdf.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # Inbox feedback loop (optional)
    try:
        from applypilot.feedback import inbox as _inbox_mod
        accounts = _inbox_mod.list_accounts()
        if accounts:
            emails = ", ".join(a.email for a in accounts)
            results.append(("Inbox feedback loop", ok_mark,
                            f"{len(accounts)} account(s): {emails}"))
        else:
            results.append(("Inbox feedback loop", "[dim]optional[/dim]",
                            "Run `applypilot inbox add --email ... --host ...` to enable"))
        try:
            import imapclient  # noqa: F401
        except ImportError:
            results.append(("imapclient", "[dim]optional[/dim]",
                            "pip install imapclient — needed for inbox poll"))
        try:
            import keyring  # noqa: F401
        except ImportError:
            results.append(("keyring", "[dim]optional[/dim]",
                            "pip install keyring — needed to store IMAP passwords"))
    except Exception:
        pass

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
