"""Alert dispatchers — Slack webhook, email (SMTP), generic webhook.

Triggered from the daemon and apply launcher when interesting events occur:
  - budget_cap_reached:    daily CAPSOLVER spend hit the cap
  - captcha_loop:          per-job kill switch fired
  - outcome_summary:       daily rollup of inbox results
  - vault_session_expired: a domain in the auth vault needs re-seeding
  - daemon_stopped:        the watch daemon exited

All sinks are best-effort and silent on failure (we don't want a busted
webhook to kill the daemon). Sink configuration via env vars:

  ALERT_SLACK_WEBHOOK   = https://hooks.slack.com/services/...
  ALERT_WEBHOOK_URL     = https://your.svc/applypilot   (generic JSON POST)
  ALERT_EMAIL_SMTP_HOST = smtp.gmail.com
  ALERT_EMAIL_SMTP_PORT = 587
  ALERT_EMAIL_FROM      = me@example.com
  ALERT_EMAIL_TO        = me@example.com
  ALERT_EMAIL_PASSWORD  = ...   (or store in keyring under 'applypilot-alerts')
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any
from urllib import request as urlreq, error as urlerr

log = logging.getLogger(__name__)

# Map event_type -> human-readable subject for emails / Slack.
_SUBJECTS: dict[str, str] = {
    "budget_cap_reached":   "ApplyPilot: CAPTCHA daily cap reached",
    "captcha_loop":         "ApplyPilot: CAPTCHA loop kill switch fired",
    "outcome_summary":      "ApplyPilot: daily outcome summary",
    "vault_session_expired":"ApplyPilot: auth vault needs refresh",
    "daemon_stopped":       "ApplyPilot: daemon stopped",
    "test":                 "ApplyPilot: test alert",
}

# Configurable via env: which events trigger sinks (default: all).
# Comma-separated list. e.g. ALERT_EVENTS=budget_cap_reached,captcha_loop
def _enabled_events() -> set[str] | None:
    raw = os.environ.get("ALERT_EVENTS")
    if not raw:
        return None  # all events enabled
    return {e.strip() for e in raw.split(",") if e.strip()}


# ---------------------------------------------------------------------------
# Sink: Slack
# ---------------------------------------------------------------------------

def send_slack(message: str, *, blocks: list | None = None,
               webhook_url: str | None = None) -> bool:
    url = webhook_url or os.environ.get("ALERT_SLACK_WEBHOOK")
    if not url:
        return False
    payload: dict[str, Any] = {"text": message}
    if blocks:
        payload["blocks"] = blocks
    try:
        req = urlreq.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlreq.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except (urlerr.URLError, OSError) as e:
        log.warning("Slack alert failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Sink: generic webhook (JSON POST)
# ---------------------------------------------------------------------------

def send_webhook(payload: dict, *, url: str | None = None) -> bool:
    url = url or os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return False
    try:
        req = urlreq.Request(
            url, data=json.dumps(payload, default=str).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlreq.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except (urlerr.URLError, OSError) as e:
        log.warning("Webhook alert failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Sink: email (SMTP)
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str, *,
               to_addr: str | None = None,
               from_addr: str | None = None) -> bool:
    host = os.environ.get("ALERT_EMAIL_SMTP_HOST")
    port = int(os.environ.get("ALERT_EMAIL_SMTP_PORT", "587"))
    to_addr = to_addr or os.environ.get("ALERT_EMAIL_TO")
    from_addr = from_addr or os.environ.get("ALERT_EMAIL_FROM")

    if not host or not to_addr or not from_addr:
        return False

    password = os.environ.get("ALERT_EMAIL_PASSWORD")
    if not password:
        try:
            import keyring
            password = keyring.get_password("applypilot-alerts", from_addr)
        except Exception:
            password = None

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
                if password:
                    s.login(from_addr, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port) as s:
                s.ehlo()
                try:
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                except smtplib.SMTPException:
                    pass  # server may not advertise STARTTLS
                if password:
                    s.login(from_addr, password)
                s.send_message(msg)
        return True
    except Exception as e:
        log.warning("Email alert failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def notify(event_type: str, **context: Any) -> dict[str, bool]:
    """Dispatch an alert across every configured sink. Best-effort.

    Returns a {sink: ok} map. Always returns successfully — never raises.
    """
    allow = _enabled_events()
    if allow is not None and event_type not in allow:
        return {}

    subject = _SUBJECTS.get(event_type, f"ApplyPilot: {event_type}")
    body_lines = [subject, ""]
    for k, v in sorted(context.items()):
        body_lines.append(f"{k}: {v}")
    body = "\n".join(body_lines)

    results: dict[str, bool] = {}
    if os.environ.get("ALERT_SLACK_WEBHOOK"):
        results["slack"] = send_slack(body)
    if os.environ.get("ALERT_WEBHOOK_URL"):
        results["webhook"] = send_webhook({
            "event": event_type, "subject": subject, "context": context,
        })
    if os.environ.get("ALERT_EMAIL_SMTP_HOST") and os.environ.get("ALERT_EMAIL_TO"):
        results["email"] = send_email(subject, body)
    return results


def is_configured() -> bool:
    """True if at least one alert sink has env config."""
    return any(os.environ.get(k) for k in (
        "ALERT_SLACK_WEBHOOK", "ALERT_WEBHOOK_URL", "ALERT_EMAIL_SMTP_HOST",
    ))
