"""CAPTCHA / spend budget tracking.

CapSolver charges per task. Without a cap, an unattended `applypilot watch`
run could burn arbitrary $$ if the agent gets stuck on a site that endlessly
challenges (Turnstile loops are notorious). This module:

  - Records every detected CapSolver task to `captcha_events`.
  - Rolls up daily spend in `budget_state` (UTC date).
  - Exposes `is_over_daily_cap()` and `per_job_count()` so the daemon and
    apply launcher can short-circuit before more spend.

Detection hook: `record_from_tool_use(...)` is called from launcher.py's
stream parser whenever the agent invokes browser_evaluate against the
CapSolver API. The estimated cost is looked up by TASK_TYPE since CapSolver
doesn't return per-task cost in createTask responses (the user can verify
via `applypilot doctor` against the CapSolver getBalance endpoint).

Caps come from environment variables, configurable in ~/.applypilot/.env:

  CAPSOLVER_DAILY_USD_CAP   default: 2.00
  CAPSOLVER_PER_JOB_MAX     default: 3
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from applypilot.database import get_connection

log = logging.getLogger(__name__)

# Per-task cost estimates (USD). CapSolver pricing varies a bit, these are
# midpoint estimates that err slightly high so a $2 cap won't be a surprise.
COST_BY_TYPE: dict[str, float] = {
    "hcaptcha": 0.0012,
    "recaptchav2": 0.0010,
    "recaptchav3": 0.0010,
    "turnstile": 0.0012,
    "funcaptcha": 0.0020,
    "image": 0.0005,
    "unknown": 0.0015,
}

# Map full CapSolver TASK_TYPE strings to our short keys.
TYPE_ALIASES: dict[str, str] = {
    "HCaptchaTaskProxyLess":          "hcaptcha",
    "HCaptchaEnterpriseTaskProxyLess":"hcaptcha",
    "ReCaptchaV2TaskProxyLess":       "recaptchav2",
    "ReCaptchaV3TaskProxyLess":       "recaptchav3",
    "ReCaptchaV2EnterpriseTaskProxyLess":"recaptchav2",
    "ReCaptchaV3EnterpriseTaskProxyLess":"recaptchav3",
    "AntiTurnstileTaskProxyLess":     "turnstile",
    "FunCaptchaTaskProxyLess":        "funcaptcha",
    "FunCaptchaTask":                 "funcaptcha",
    "ImageToTextTask":                "image",
}

DEFAULT_DAILY_CAP_USD = 2.00
DEFAULT_PER_JOB_MAX = 3

# In-memory per-job counter (job_url -> count). Reset between job runs by
# clear_job_counter(). Used for the per-job kill switch.
_job_counters: dict[str, int] = defaultdict(int)


# ---------------------------------------------------------------------------
# Caps from env
# ---------------------------------------------------------------------------

def daily_cap_usd() -> float:
    raw = os.environ.get("CAPSOLVER_DAILY_USD_CAP")
    try:
        return float(raw) if raw else DEFAULT_DAILY_CAP_USD
    except ValueError:
        return DEFAULT_DAILY_CAP_USD


def per_job_max() -> int:
    raw = os.environ.get("CAPSOLVER_PER_JOB_MAX")
    try:
        return int(raw) if raw else DEFAULT_PER_JOB_MAX
    except ValueError:
        return DEFAULT_PER_JOB_MAX


# ---------------------------------------------------------------------------
# Type detection from a browser_evaluate body
# ---------------------------------------------------------------------------

# Match the literal `type: 'XYZ'` or `type: "XYZ"` field in a CapSolver task.
_TYPE_RE = re.compile(r"""['\"]?type['\"]?\s*:\s*['\"]([A-Za-z0-9_]+)['\"]""")

# Match `clientKey: '...'` to confirm this is a CapSolver call (defensive).
_CAPSOLVER_HOST_RE = re.compile(r"api\.capsolver\.com/createTask", re.IGNORECASE)

# Cheap detector for getTaskResult polling — we don't bill those, just track.
_POLL_RE = re.compile(r"api\.capsolver\.com/getTaskResult", re.IGNORECASE)


def looks_like_capsolver_create(body: str) -> bool:
    return bool(body) and bool(_CAPSOLVER_HOST_RE.search(body))


def looks_like_capsolver_poll(body: str) -> bool:
    return bool(body) and bool(_POLL_RE.search(body))


def detect_task_type(body: str) -> str:
    """Return the short key (e.g. 'hcaptcha') from a browser_evaluate body."""
    m = _TYPE_RE.search(body or "")
    if not m:
        return "unknown"
    raw = m.group(1)
    return TYPE_ALIASES.get(raw, "unknown")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _bump_budget_today(captcha_usd: float = 0.0,
                       llm_usd: float = 0.0,
                       applications: int = 0) -> None:
    conn = get_connection()
    today = _today_iso()
    conn.execute(
        "INSERT INTO budget_state (date_utc, captcha_usd, llm_usd, applications_sent) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(date_utc) DO UPDATE SET "
        "  captcha_usd = COALESCE(captcha_usd, 0) + excluded.captcha_usd, "
        "  llm_usd = COALESCE(llm_usd, 0) + excluded.llm_usd, "
        "  applications_sent = COALESCE(applications_sent, 0) + excluded.applications_sent",
        (today, captcha_usd, llm_usd, applications),
    )
    conn.commit()


def record_event(job_url: str | None, captcha_type: str,
                 task_id: str | None = None,
                 cost_usd: float | None = None,
                 status: str = "created",
                 error: str | None = None) -> None:
    """Persist a single CAPTCHA event and bump today's budget rollup."""
    if cost_usd is None:
        cost_usd = COST_BY_TYPE.get(captcha_type, COST_BY_TYPE["unknown"])
    conn = get_connection()
    conn.execute(
        "INSERT INTO captcha_events (job_url, occurred_at, captcha_type, task_id, "
        "cost_usd, status, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_url, datetime.now(timezone.utc).isoformat(),
         captcha_type, task_id, cost_usd, status, error),
    )
    conn.commit()
    _bump_budget_today(captcha_usd=cost_usd)
    if job_url:
        _job_counters[job_url] += 1


def record_from_tool_use(job_url: str | None, body: str) -> int | None:
    """Detect + record a CapSolver task from a browser_evaluate body.

    Returns the per-job count after this event, or None if the body
    wasn't a CapSolver createTask call.
    """
    if not looks_like_capsolver_create(body):
        return None
    captcha_type = detect_task_type(body)
    record_event(job_url, captcha_type)
    return _job_counters.get(job_url or "", 0)


# ---------------------------------------------------------------------------
# Counter helpers
# ---------------------------------------------------------------------------

def per_job_count(job_url: str) -> int:
    return _job_counters.get(job_url, 0)


def clear_job_counter(job_url: str) -> None:
    _job_counters.pop(job_url, None)


def reset_all_counters() -> None:
    _job_counters.clear()


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------

def today_captcha_spend_usd() -> float:
    conn = get_connection()
    row = conn.execute(
        "SELECT COALESCE(captcha_usd, 0) FROM budget_state WHERE date_utc = ?",
        (_today_iso(),),
    ).fetchone()
    return float(row[0]) if row else 0.0


def is_over_daily_cap() -> bool:
    return today_captcha_spend_usd() >= daily_cap_usd()


def is_over_per_job_max(job_url: str) -> bool:
    return per_job_count(job_url) >= per_job_max()


def summary(days: int = 7) -> dict:
    """Rollup for `applypilot budget show`."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT date_utc, captcha_usd, llm_usd, applications_sent "
        "FROM budget_state ORDER BY date_utc DESC LIMIT ?",
        (days,),
    ).fetchall()

    today_cap = daily_cap_usd()
    today_spent = today_captcha_spend_usd()

    by_type_rows = conn.execute(
        "SELECT captcha_type, COUNT(*), COALESCE(SUM(cost_usd),0) "
        "FROM captcha_events WHERE occurred_at >= datetime('now', ?) "
        "GROUP BY captcha_type",
        (f"-{days} days",),
    ).fetchall()

    return {
        "today_date": _today_iso(),
        "today_spent_usd": today_spent,
        "today_cap_usd": today_cap,
        "today_remaining_usd": max(0.0, today_cap - today_spent),
        "today_over_cap": today_spent >= today_cap,
        "per_job_max": per_job_max(),
        "history": [
            {"date": r[0], "captcha_usd": float(r[1] or 0),
             "llm_usd": float(r[2] or 0),
             "applications_sent": int(r[3] or 0)}
            for r in rows
        ],
        "by_type": [
            {"type": r[0], "count": int(r[1]), "spend_usd": float(r[2])}
            for r in by_type_rows
        ],
    }
