"""ApplyPilot daemon — `applypilot watch`.

Cron-driven orchestrator that runs the existing pipeline + apply on a schedule
so the user can leave it running unattended. Each stage is a thin wrapper around
existing code (`pipeline.run_pipeline`, `apply.launcher.main`); this module only
adds scheduling, locks, quotas, quiet hours, telemetry, and zombie cleanup.

Usage (via CLI):
    applypilot watch                    # daemon loop, defaults
    applypilot watch --once             # run every stage once and exit
    applypilot watch --daily-quota 50   # apply up to 50/day
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from pathlib import Path

from applypilot.config import APP_DIR, LOG_DIR, ensure_dirs, load_env
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

# Files written to APP_DIR
HEARTBEAT_PATH = APP_DIR / "daemon.heartbeat"
EVENTS_LOG_PATH = LOG_DIR / "daemon.jsonl"
STOP_FILE_PATH = APP_DIR / "daemon.stop"

# Stage execution lock — prevents the same stage from running twice concurrently
# if a previous tick is still going when the next fires.
_stage_locks: dict[str, threading.Lock] = {
    "discover": threading.Lock(),
    "enrich":   threading.Lock(),
    "score":    threading.Lock(),
    "tailor":   threading.Lock(),
    "cover":    threading.Lock(),
    "pdf":      threading.Lock(),
    "apply":    threading.Lock(),
    "inbox":    threading.Lock(),
}


@dataclass
class WatchConfig:
    """Daemon configuration. Cron strings use 5-field syntax: 'min hr dom mon dow'."""

    discover_cron: str = "0 */6 * * *"        # every 6 hours
    enrich_cron:   str = "*/30 * * * *"       # every 30 min
    score_cron:    str = "0 * * * *"          # hourly
    tailor_cron:   str = "*/20 * * * *"       # every 20 min
    cover_cron:    str = "*/25 * * * *"       # every 25 min
    pdf_cron:      str = "*/30 * * * *"       # every 30 min
    apply_cron:    str = "*/15 * * * *"       # every 15 min
    inbox_cron:    str = "*/30 * * * *"       # every 30 min — IMAP outcome poller

    daily_quota:    int = 30
    min_score:      int = 7
    workers:        int = 1
    validation:     str = "normal"
    headless:       bool = True
    apply_model:    str = "haiku"

    # Quiet hours during which apply runs are skipped (discovery still runs).
    # Format: "HH:MM-HH:MM" in local time. Set "" to disable.
    quiet_hours: str = "23:00-07:00"

    # Optional: limit jobs per individual apply tick (defends against draining
    # the whole queue in one go and burning the daily quota in 5 minutes).
    apply_batch_size: int = 5


# ---------------------------------------------------------------------------
# Telemetry helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_utc_iso_prefix() -> str:
    """Today's date in UTC as 'YYYY-MM-DD' — used to filter `applied_at`."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _write_heartbeat() -> None:
    HEARTBEAT_PATH.write_text(_now_iso(), encoding="utf-8")


def _append_event(event: dict) -> None:
    """Append a JSON line to the daemon event log."""
    EVENTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def _record_run_start(stage: str) -> int:
    """Insert a daemon_runs row, return its id."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO daemon_runs (stage, started_at, status) VALUES (?, ?, ?)",
        (stage, _now_iso(), "running"),
    )
    conn.commit()
    return cur.lastrowid


def _record_run_finish(run_id: int, status: str, items: int = 0,
                       error: str | None = None) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE daemon_runs SET finished_at=?, status=?, items_processed=?, error=? "
        "WHERE id=?",
        (_now_iso(), status, items, error, run_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Quota + quiet-hours
# ---------------------------------------------------------------------------

def _applied_today_count() -> int:
    """Count applications submitted today (UTC)."""
    conn = get_connection()
    prefix = _today_utc_iso_prefix()
    row = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL "
        "AND applied_at LIKE ?",
        (f"{prefix}%",),
    ).fetchone()
    return row[0] if row else 0


def _parse_quiet_hours(spec: str) -> tuple[dtime, dtime] | None:
    if not spec or "-" not in spec:
        return None
    try:
        start_s, end_s = spec.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
        return dtime(sh, sm), dtime(eh, em)
    except (ValueError, AttributeError):
        log.warning("Invalid quiet_hours '%s' — ignoring", spec)
        return None


def _in_quiet_hours(spec: str) -> bool:
    parsed = _parse_quiet_hours(spec)
    if parsed is None:
        return False
    start, end = parsed
    now = datetime.now().time()
    if start <= end:
        return start <= now < end
    # Wraps midnight (e.g. 23:00-07:00)
    return now >= start or now < end


# ---------------------------------------------------------------------------
# Stage runners (wrap existing pipeline functions)
# ---------------------------------------------------------------------------

def _run_pipeline_stage(stage: str, cfg: WatchConfig) -> dict:
    """Run a single pipeline stage via run_pipeline([stage])."""
    from applypilot.pipeline import run_pipeline
    return run_pipeline(
        stages=[stage],
        min_score=cfg.min_score,
        workers=cfg.workers,
        validation_mode=cfg.validation,
    )


def _tick_stage(stage: str, cfg: WatchConfig) -> None:
    """Generic non-apply stage tick: lock, telemetry, run, log."""
    lock = _stage_locks[stage]
    if not lock.acquire(blocking=False):
        log.info("Skipping %s — previous run still in flight", stage)
        _append_event({"ts": _now_iso(), "stage": stage, "event": "skipped_busy"})
        return

    run_id = _record_run_start(stage)
    started = time.time()
    try:
        _write_heartbeat()
        _append_event({"ts": _now_iso(), "stage": stage, "event": "start"})
        result = _run_pipeline_stage(stage, cfg)
        errors = result.get("errors") or {}
        status = "error" if errors else "ok"
        _record_run_finish(run_id, status, items=0, error=json.dumps(errors) if errors else None)
        _append_event({
            "ts": _now_iso(), "stage": stage, "event": "finish",
            "status": status, "elapsed_s": round(time.time() - started, 2),
            "errors": errors,
        })
    except Exception as e:
        log.exception("Stage %s crashed", stage)
        _record_run_finish(run_id, "crash", error=str(e))
        _append_event({
            "ts": _now_iso(), "stage": stage, "event": "crash", "error": str(e),
            "elapsed_s": round(time.time() - started, 2),
        })
    finally:
        lock.release()


def _tick_inbox(cfg: WatchConfig) -> None:
    """Poll all configured email inboxes once and update outcomes."""
    lock = _stage_locks["inbox"]
    if not lock.acquire(blocking=False):
        log.info("Skipping inbox — previous poll still in flight")
        _append_event({"ts": _now_iso(), "stage": "inbox", "event": "skipped_busy"})
        return

    run_id = _record_run_start("inbox")
    started = time.time()
    try:
        _write_heartbeat()
        from applypilot.feedback import inbox as inbox_mod
        accounts = inbox_mod.list_accounts()
        if not accounts:
            _record_run_finish(run_id, "no_accounts")
            _append_event({"ts": _now_iso(), "stage": "inbox", "event": "no_accounts"})
            return

        _append_event({"ts": _now_iso(), "stage": "inbox", "event": "start",
                       "accounts": [a.email for a in accounts]})

        total_matched = 0
        total_errors = 0
        for a in accounts:
            if a.status != "active":
                continue
            try:
                res = inbox_mod.poll_account(a, use_llm_fallback=True)
                total_matched += res.matched
                total_errors += res.errors
                _append_event({
                    "ts": _now_iso(), "stage": "inbox", "event": "account_done",
                    "email": res.account_email, "fetched": res.fetched,
                    "matched": res.matched, "skipped": res.skipped,
                    "errors": res.errors,
                })
            except Exception as e:
                total_errors += 1
                log.warning("inbox poll failed for %s: %s", a.email, e)
                _append_event({"ts": _now_iso(), "stage": "inbox",
                               "event": "account_error", "email": a.email,
                               "error": str(e)})

        status = "ok" if total_errors == 0 else "partial"
        _record_run_finish(run_id, status, items=total_matched)
        _append_event({"ts": _now_iso(), "stage": "inbox", "event": "finish",
                       "status": status, "matched": total_matched,
                       "elapsed_s": round(time.time() - started, 2)})
        # Outcome alert: send a short summary when the poll detected new
        # interview / offer hits.
        if total_matched > 0:
            try:
                from applypilot import alerts
                from applypilot.feedback import reconciler
                stats = reconciler.stats(weeks=1)
                interviews = stats["by_label"].get("interview", 0)
                offers = stats["by_label"].get("offer", 0)
                rejections = stats["by_label"].get("rejected", 0)
                if interviews > 0 or offers > 0:
                    alerts.notify("outcome_summary",
                                  matched_this_poll=total_matched,
                                  interviews_total=interviews,
                                  offers_total=offers,
                                  rejections_total=rejections)
            except Exception:
                log.debug("outcome alert failed", exc_info=True)
    except Exception as e:
        log.exception("Inbox tick crashed")
        _record_run_finish(run_id, "crash", error=str(e))
        _append_event({"ts": _now_iso(), "stage": "inbox", "event": "crash",
                       "error": str(e)})
    finally:
        lock.release()


def _tick_apply(cfg: WatchConfig) -> None:
    """Apply tick: respects quiet hours and daily quota; bounded batch size."""
    lock = _stage_locks["apply"]
    if not lock.acquire(blocking=False):
        log.info("Skipping apply — previous run still in flight")
        _append_event({"ts": _now_iso(), "stage": "apply", "event": "skipped_busy"})
        return

    run_id = _record_run_start("apply")
    started = time.time()
    try:
        _write_heartbeat()

        if _in_quiet_hours(cfg.quiet_hours):
            _record_run_finish(run_id, "skipped_quiet")
            _append_event({"ts": _now_iso(), "stage": "apply", "event": "skipped_quiet_hours"})
            return

        # CAPTCHA spend cap — hard skip if we've already hit the daily cap.
        try:
            from applypilot import budget
            if budget.is_over_daily_cap():
                spent = budget.today_captcha_spend_usd()
                cap = budget.daily_cap_usd()
                _record_run_finish(run_id, "skipped_budget", error=f"${spent:.2f}/${cap:.2f}")
                _append_event({
                    "ts": _now_iso(), "stage": "apply", "event": "skipped_budget",
                    "captcha_spent_usd": spent, "captcha_cap_usd": cap,
                })
                # One-shot alert when we first hit the cap (state-tracked via
                # last_polled_at on a heartbeat sentinel — simplest: send each
                # tick the cap blocks; sinks are expected to dedupe if needed).
                try:
                    from applypilot import alerts
                    alerts.notify("budget_cap_reached",
                                  captcha_spent_usd=round(spent, 4),
                                  captcha_cap_usd=cap,
                                  date_utc=_today_utc_iso_prefix())
                except Exception:
                    log.debug("budget alert failed", exc_info=True)
                return
        except Exception:
            log.debug("budget cap check failed", exc_info=True)

        applied_today = _applied_today_count()
        remaining = cfg.daily_quota - applied_today
        if remaining <= 0:
            _record_run_finish(run_id, "skipped_quota", items=applied_today)
            _append_event({
                "ts": _now_iso(), "stage": "apply", "event": "skipped_quota",
                "applied_today": applied_today, "quota": cfg.daily_quota,
            })
            return

        batch = min(cfg.apply_batch_size, remaining)
        _append_event({
            "ts": _now_iso(), "stage": "apply", "event": "start",
            "batch": batch, "applied_today": applied_today, "quota": cfg.daily_quota,
        })

        from applypilot.apply.launcher import main as apply_main
        apply_main(
            limit=batch,
            target_url=None,
            min_score=cfg.min_score,
            headless=cfg.headless,
            model=cfg.apply_model,
            dry_run=False,
            continuous=False,
            workers=cfg.workers,
        )

        new_total = _applied_today_count()
        items_processed = max(0, new_total - applied_today)
        _record_run_finish(run_id, "ok", items=items_processed)
        _append_event({
            "ts": _now_iso(), "stage": "apply", "event": "finish",
            "items_processed": items_processed, "applied_today": new_total,
            "elapsed_s": round(time.time() - started, 2),
        })
    except Exception as e:
        log.exception("Apply tick crashed")
        _record_run_finish(run_id, "crash", error=str(e))
        _append_event({
            "ts": _now_iso(), "stage": "apply", "event": "crash", "error": str(e),
            "elapsed_s": round(time.time() - started, 2),
        })
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Zombie cleanup
# ---------------------------------------------------------------------------

def _cleanup_zombies(stale_minutes: int = 30) -> int:
    """Reset apply_status='in_progress' rows whose last_attempted_at is stale.

    Catches jobs whose previous apply session crashed without releasing the lock.
    Returns the number of rows reset.
    """
    conn = get_connection()
    cur = conn.execute(
        "UPDATE jobs SET apply_status=NULL "
        "WHERE apply_status='in_progress' "
        "AND (last_attempted_at IS NULL "
        "     OR datetime(last_attempted_at) < datetime('now', ?))",
        (f"-{stale_minutes} minutes",),
    )
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Daemon entry points
# ---------------------------------------------------------------------------

# Stage names (in order) for --once mode and scheduler registration.
_PIPELINE_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


def run_once(cfg: WatchConfig) -> None:
    """Run every pipeline stage once, then run apply once, then exit.

    Useful for OS-level cron users who don't want apscheduler in-process.
    """
    load_env()
    ensure_dirs()
    init_db()

    reset = _cleanup_zombies()
    if reset:
        log.info("Reset %d zombie in_progress jobs", reset)
        _append_event({"ts": _now_iso(), "event": "zombie_cleanup", "reset": reset})

    for stage in _PIPELINE_STAGES:
        _tick_stage(stage, cfg)

    _tick_apply(cfg)
    _tick_inbox(cfg)


def run_forever(cfg: WatchConfig) -> None:
    """Start apscheduler with cron triggers for each stage and block forever."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as e:
        raise SystemExit(
            "apscheduler is required for `applypilot watch`. "
            "Install it with: pip install apscheduler"
        ) from e

    load_env()
    ensure_dirs()
    init_db()

    reset = _cleanup_zombies()
    if reset:
        log.info("Reset %d zombie in_progress jobs at startup", reset)
        _append_event({"ts": _now_iso(), "event": "zombie_cleanup", "reset": reset})

    scheduler = BackgroundScheduler(timezone="UTC")
    cron_specs: list[tuple[str, str, callable]] = [
        ("discover", cfg.discover_cron, lambda: _tick_stage("discover", cfg)),
        ("enrich",   cfg.enrich_cron,   lambda: _tick_stage("enrich",   cfg)),
        ("score",    cfg.score_cron,    lambda: _tick_stage("score",    cfg)),
        ("tailor",   cfg.tailor_cron,   lambda: _tick_stage("tailor",   cfg)),
        ("cover",    cfg.cover_cron,    lambda: _tick_stage("cover",    cfg)),
        ("pdf",      cfg.pdf_cron,      lambda: _tick_stage("pdf",      cfg)),
        ("apply",    cfg.apply_cron,    lambda: _tick_apply(cfg)),
        ("inbox",    cfg.inbox_cron,    lambda: _tick_inbox(cfg)),
    ]

    for name, cron, fn in cron_specs:
        try:
            trigger = CronTrigger.from_crontab(cron, timezone="UTC")
        except ValueError as e:
            raise SystemExit(f"Invalid cron for {name}: '{cron}' — {e}") from e
        scheduler.add_job(
            fn, trigger=trigger, id=name, name=f"watch-{name}",
            max_instances=1, coalesce=True, misfire_grace_time=300,
        )

    scheduler.start()
    _append_event({
        "ts": _now_iso(), "event": "daemon_start",
        "config": {
            "discover_cron": cfg.discover_cron, "enrich_cron": cfg.enrich_cron,
            "score_cron": cfg.score_cron, "tailor_cron": cfg.tailor_cron,
            "cover_cron": cfg.cover_cron, "pdf_cron": cfg.pdf_cron,
            "apply_cron": cfg.apply_cron, "daily_quota": cfg.daily_quota,
            "quiet_hours": cfg.quiet_hours, "min_score": cfg.min_score,
            "apply_batch_size": cfg.apply_batch_size, "headless": cfg.headless,
        },
    })
    _write_heartbeat()

    if STOP_FILE_PATH.exists():
        STOP_FILE_PATH.unlink()

    try:
        while True:
            if STOP_FILE_PATH.exists():
                _append_event({"ts": _now_iso(), "event": "stop_file_detected"})
                STOP_FILE_PATH.unlink()
                break
            _write_heartbeat()
            time.sleep(5)
    except (KeyboardInterrupt, SystemExit):
        _append_event({"ts": _now_iso(), "event": "daemon_interrupt"})
    finally:
        scheduler.shutdown(wait=False)
        _append_event({"ts": _now_iso(), "event": "daemon_stop"})
        try:
            from applypilot import alerts
            if alerts.is_configured():
                alerts.notify("daemon_stopped", at=_now_iso())
        except Exception:
            pass


def request_stop() -> bool:
    """Drop a stop-file the running daemon polls for. Returns True on write."""
    ensure_dirs()
    STOP_FILE_PATH.write_text(_now_iso(), encoding="utf-8")
    return True


def read_status(tail_events: int = 20) -> dict:
    """Return current daemon status: heartbeat, recent events, scheduled stages."""
    status: dict = {"heartbeat": None, "heartbeat_age_s": None, "events": []}
    if HEARTBEAT_PATH.exists():
        hb = HEARTBEAT_PATH.read_text(encoding="utf-8").strip()
        status["heartbeat"] = hb
        try:
            ts = datetime.fromisoformat(hb)
            status["heartbeat_age_s"] = (datetime.now(timezone.utc) - ts).total_seconds()
        except ValueError:
            pass

    if EVENTS_LOG_PATH.exists():
        try:
            lines = EVENTS_LOG_PATH.read_text(encoding="utf-8").splitlines()
            status["events"] = [json.loads(line) for line in lines[-tail_events:] if line.strip()]
        except (OSError, json.JSONDecodeError):
            pass

    return status
