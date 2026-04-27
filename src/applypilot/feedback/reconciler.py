"""Match a recruiter email to a `jobs` row and persist the outcome.

Strategy:
  1. Try to match by sender domain ↔ jobs.site (e.g. `@workable-mail.com`
     → site contains "workable") OR ↔ application_url host.
  2. Restrict to jobs applied within the trailing 60 days.
  3. If multiple candidates remain, pick the highest fit_score (most likely
     match) and record `outcome_confidence` × 0.7 to flag the ambiguity.
  4. Update `jobs.outcome` + bump weekly `feedback_signals` rollup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from applypilot.database import get_connection

log = logging.getLogger(__name__)

# Window for matching emails to applied jobs.
_MATCH_WINDOW_DAYS = 60

# Map common forwarder / ATS-bot domains → company-name hint substring.
# This is intentionally short and conservative — falls back to host suffix
# matching if not found here.
_FORWARDER_HINTS: dict[str, str] = {
    "workable-mail.com":      "workable",
    "hire.successfactors.com":"successfactors",
    "greenhouse-mail.io":     "greenhouse",
    "lever.co":               "lever",
    "ashbyhq.com":            "ashby",
    "smartrecruiters.com":    "smartrecruiters",
    "myworkday.com":          "workday",
    "myworkdayjobs.com":      "workday",
}


@dataclass
class MatchResult:
    """Outcome of matching one email to the jobs table."""
    job_url: str | None
    confidence: float
    candidate_count: int
    notes: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _week_iso(dt: datetime | None = None) -> str:
    """ISO 8601 'YYYY-Www' for weekly rollups."""
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%G-W%V")


def _extract_sender_domain(sender: str) -> str:
    """Extract bare domain from 'Name <addr@host>' or 'addr@host'."""
    if not sender:
        return ""
    if "<" in sender and ">" in sender:
        sender = sender[sender.index("<") + 1:sender.index(">")]
    if "@" in sender:
        return sender.rsplit("@", 1)[-1].strip().lower()
    return sender.strip().lower()


def _domain_root(domain: str) -> str:
    """Reduce 'jobs.acme.com' → 'acme.com' (last two labels — naive but works)."""
    if not domain:
        return ""
    parts = domain.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return domain


def _candidate_query(sender_domain: str, hint: str | None,
                     window_days: int = _MATCH_WINDOW_DAYS) -> tuple[str, list]:
    """Build SQL + params to find candidate jobs for a sender."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    root = _domain_root(sender_domain)

    where = ["applied_at IS NOT NULL", "applied_at >= ?"]
    params: list = [cutoff]

    or_clauses: list[str] = []
    if sender_domain:
        or_clauses.append("LOWER(application_url) LIKE ?")
        params.append(f"%{sender_domain}%")
        or_clauses.append("LOWER(url) LIKE ?")
        params.append(f"%{sender_domain}%")
    if root and root != sender_domain:
        or_clauses.append("LOWER(application_url) LIKE ?")
        params.append(f"%{root}%")
        or_clauses.append("LOWER(url) LIKE ?")
        params.append(f"%{root}%")
        or_clauses.append("LOWER(site) LIKE ?")
        params.append(f"%{root.split('.')[0]}%")
    if hint:
        or_clauses.append("LOWER(site) LIKE ?")
        params.append(f"%{hint}%")

    if or_clauses:
        where.append("(" + " OR ".join(or_clauses) + ")")

    sql = (
        "SELECT url, title, site, application_url, fit_score, applied_at, outcome "
        "FROM jobs WHERE " + " AND ".join(where) + " "
        "ORDER BY fit_score DESC NULLS LAST, applied_at DESC"
    )
    return sql, params


def find_match(sender: str, subject: str, body: str = "") -> MatchResult:
    """Locate the most likely job row for an incoming email.

    Returns MatchResult with job_url=None if no candidate found.
    """
    sender_domain = _extract_sender_domain(sender)
    hint = _FORWARDER_HINTS.get(sender_domain) or _FORWARDER_HINTS.get(_domain_root(sender_domain))

    sql, params = _candidate_query(sender_domain, hint)
    conn = get_connection()
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    if not rows:
        return MatchResult(None, 0.0, 0, "no_candidates")

    if len(rows) == 1:
        return MatchResult(rows[0]["url"], 1.0, 1, None)

    # Multiple candidates — heuristic: company name appearing in subject is
    # a strong tiebreaker; otherwise default to highest fit_score.
    subj_lower = (subject or "").lower()
    body_head = (body or "")[:500].lower()

    for r in rows:
        site = (r.get("site") or "").lower().strip()
        if site and (site in subj_lower or site in body_head):
            return MatchResult(r["url"], 0.85, len(rows), "site_in_subject")

    top = rows[0]
    return MatchResult(top["url"], 0.55, len(rows), "ambiguous_picked_highest_fit")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def record_outcome(job_url: str, label: str, confidence: float,
                   email_uid: str | None, subject: str | None) -> None:
    """Update the jobs row + bump weekly feedback_signals."""
    conn = get_connection()

    # Read existing site for rollup
    site_row = conn.execute(
        "SELECT site, applied_at FROM jobs WHERE url=?", (job_url,)
    ).fetchone()
    if not site_row:
        log.warning("record_outcome: no job row for url=%s", job_url)
        return

    site = (site_row["site"] or "unknown").strip()
    applied_at = site_row["applied_at"]

    conn.execute(
        "UPDATE jobs SET outcome=?, outcome_at=?, outcome_email_uid=?, "
        "outcome_confidence=?, outcome_subject=? WHERE url=?",
        (label, _now_iso(), email_uid, confidence,
         (subject or "")[:200], job_url),
    )

    # Bump rollup. Use the email's arrival date for the week, falling back
    # to today.
    week = _week_iso()
    column = {
        "acknowledged": "acks",
        "rejected":     "rejections",
        "interview":    "interviews",
        "offer":        "offers",
    }.get(label)
    if column:
        conn.execute(f"""
            INSERT INTO feedback_signals (site, week_iso, {column})
            VALUES (?, ?, 1)
            ON CONFLICT(site, week_iso) DO UPDATE SET
                {column} = COALESCE({column}, 0) + 1
        """, (site, week))

    conn.commit()
    log.info("Recorded outcome=%s confidence=%.2f for %s",
             label, confidence, job_url)


def list_low_confidence(threshold: float = 0.70, limit: int = 50) -> list[dict]:
    """Rows with outcome set but low confidence — for manual triage."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT url, title, site, outcome, outcome_confidence, outcome_subject, outcome_at "
        "FROM jobs WHERE outcome IS NOT NULL AND outcome_confidence < ? "
        "ORDER BY outcome_at DESC LIMIT ?",
        (threshold, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def stats(weeks: int = 4) -> dict:
    """Aggregate outcomes across the last N weeks."""
    conn = get_connection()

    # Overall outcome distribution
    by_label = dict(conn.execute(
        "SELECT outcome, COUNT(*) FROM jobs WHERE outcome IS NOT NULL GROUP BY outcome"
    ).fetchall())

    # Per-site rollup, last N weeks
    by_site = conn.execute(f"""
        SELECT site,
               SUM(applications_sent), SUM(acks), SUM(rejections),
               SUM(interviews), SUM(offers), SUM(no_reply_after_30d)
        FROM feedback_signals
        WHERE week_iso >= ?
        GROUP BY site
        ORDER BY SUM(applications_sent) DESC
    """, (_week_iso(datetime.now(timezone.utc) - timedelta(weeks=weeks)),)).fetchall()

    return {
        "by_label": {k: int(v) for k, v in by_label.items() if k},
        "by_site": [
            {
                "site": r[0], "apps": int(r[1] or 0), "acks": int(r[2] or 0),
                "rejections": int(r[3] or 0), "interviews": int(r[4] or 0),
                "offers": int(r[5] or 0), "no_reply": int(r[6] or 0),
            }
            for r in by_site
        ],
    }
