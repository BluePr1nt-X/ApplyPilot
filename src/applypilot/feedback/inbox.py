"""IMAP inbox poller.

Fetches messages newer than `last_uid_seen` for each registered account,
classifies each, matches to a job, and records the outcome. Designed to
be safe to run on a cron — purely additive, idempotent on UIDs.

Credentials: passwords are stored via `keyring` under service
"applypilot-imap" with the email address as the key. Env-var fallback
(`IMAP_PASSWORD`) is supported for headless / containerized installs.
"""

from __future__ import annotations

import email as email_lib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from html.parser import HTMLParser

from applypilot.database import get_connection
from applypilot.feedback import classifier, reconciler

log = logging.getLogger(__name__)

KEYRING_SERVICE = "applypilot-imap"
ENV_PASSWORD_VAR = "IMAP_PASSWORD"

# Hard cap on how many emails to process in a single poll (defends against
# huge backlogs on first run).
DEFAULT_MAX_PER_POLL = 200


# ---------------------------------------------------------------------------
# Account CRUD
# ---------------------------------------------------------------------------

@dataclass
class Account:
    id: int
    email: str
    host: str
    port: int
    ssl: bool
    mailbox: str
    last_uid_seen: int
    last_polled_at: str | None
    status: str
    auth_method: str = "password"


def list_accounts() -> list[Account]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, email, host, port, ssl, mailbox, last_uid_seen, "
        "last_polled_at, status, COALESCE(auth_method, 'password') "
        "FROM email_accounts ORDER BY id"
    ).fetchall()
    return [
        Account(
            id=int(r[0]), email=r[1], host=r[2],
            port=int(r[3] or 993), ssl=bool(r[4]),
            mailbox=r[5] or "INBOX",
            last_uid_seen=int(r[6] or 0),
            last_polled_at=r[7],
            status=r[8] or "active",
            auth_method=r[9] or "password",
        )
        for r in rows
    ]


def add_account(email: str, host: str, port: int = 993, ssl: bool = True,
                mailbox: str = "INBOX", auth_method: str = "password") -> Account:
    """Insert a new account row (without password — store separately)."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO email_accounts (email, host, port, ssl, mailbox, status, auth_method) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?) "
        "ON CONFLICT(email) DO UPDATE SET host=excluded.host, port=excluded.port, "
        "ssl=excluded.ssl, mailbox=excluded.mailbox, status='active', "
        "auth_method=excluded.auth_method",
        (email, host, port, 1 if ssl else 0, mailbox, auth_method),
    )
    conn.commit()
    accounts = [a for a in list_accounts() if a.email == email]
    return accounts[0]


def remove_account(email: str) -> bool:
    conn = get_connection()
    cur = conn.execute("DELETE FROM email_accounts WHERE email=?", (email,))
    conn.commit()
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, email)
    except Exception:
        pass
    # Also remove any OAuth refresh token for this account.
    try:
        from applypilot.feedback import oauth_gmail
        oauth_gmail.delete_credentials(email)
    except Exception:
        pass
    return cur.rowcount > 0


def store_password(email: str, password: str) -> None:
    """Save a password to the OS keyring under our service namespace."""
    try:
        import keyring
    except ImportError as e:
        raise SystemExit(
            "`keyring` is required to store IMAP passwords. "
            "Install with: pip install keyring"
        ) from e
    keyring.set_password(KEYRING_SERVICE, email, password)


def load_password(email: str) -> str | None:
    """Look up the password from keyring, falling back to IMAP_PASSWORD env."""
    try:
        import keyring
        pw = keyring.get_password(KEYRING_SERVICE, email)
        if pw:
            return pw
    except Exception:
        log.debug("keyring lookup failed", exc_info=True)
    return os.environ.get(ENV_PASSWORD_VAR)


# ---------------------------------------------------------------------------
# Email parsing helpers
# ---------------------------------------------------------------------------

def _decode_header(raw) -> str:
    """Decode an RFC 2047 encoded header (e.g. =?utf-8?b?...?=)."""
    if raw is None:
        return ""
    try:
        parts = decode_header(raw)
        out = []
        for text, charset in parts:
            if isinstance(text, bytes):
                out.append(text.decode(charset or "utf-8", errors="replace"))
            else:
                out.append(text)
        return "".join(out)
    except Exception:
        return str(raw)


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(self.parts)


def _strip_html(html: str) -> str:
    s = _HTMLStripper()
    try:
        s.feed(html)
    except Exception:
        return html
    return s.text()


def extract_body(msg: Message) -> str:
    """Return plain-text body. Prefers text/plain, falls back to stripped HTML."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    return payload.decode("utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    html = payload.decode(charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    html = payload.decode("utf-8", errors="replace")
                return _strip_html(html)
        return ""

    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        text = payload.decode("utf-8", errors="replace")
    if msg.get_content_type() == "text/html":
        return _strip_html(text)
    return text


# ---------------------------------------------------------------------------
# Poller core
# ---------------------------------------------------------------------------

@dataclass
class PollResult:
    account_email: str
    fetched: int
    classified: int
    matched: int
    skipped: int
    errors: int
    new_uid: int


def _update_progress(account_id: int, last_uid: int) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE email_accounts SET last_uid_seen=?, last_polled_at=? WHERE id=?",
        (last_uid, datetime.now(timezone.utc).isoformat(), account_id),
    )
    conn.commit()


def poll_account(account: Account, *,
                 use_llm_fallback: bool = True,
                 max_per_poll: int = DEFAULT_MAX_PER_POLL,
                 only_unseen: bool = False) -> PollResult:
    """Fetch + classify + reconcile new messages for one account.

    Idempotent: only fetches UIDs > account.last_uid_seen.
    """
    try:
        from imapclient import IMAPClient
    except ImportError as e:
        raise SystemExit(
            "`imapclient` is required. Install with: pip install imapclient"
        ) from e

    fetched = classified = matched = skipped = errors = 0
    new_uid = account.last_uid_seen

    log.info("Polling %s @ %s via %s (last_uid=%d)",
             account.email, account.host, account.auth_method,
             account.last_uid_seen)

    try:
        with IMAPClient(account.host, port=account.port, ssl=account.ssl) as client:
            if account.auth_method == "oauth_gmail":
                from applypilot.feedback import oauth_gmail
                if not oauth_gmail.imap_login(client, account.email):
                    log.error("OAuth login failed for %s", account.email)
                    return PollResult(account.email, 0, 0, 0, 0, 1, new_uid)
            else:
                pw = load_password(account.email)
                if not pw:
                    return PollResult(account.email, 0, 0, 0, 0, 1, new_uid)
                client.login(account.email, pw)
            client.select_folder(account.mailbox, readonly=True)

            criteria: list = ["UID", f"{account.last_uid_seen + 1}:*"]
            if only_unseen:
                criteria += ["UNSEEN"]
            uids = client.search(criteria)

            # imapclient returns the most recent first only when sorted; cap
            # to most-recent N to avoid huge first-run cost.
            uids = sorted(uids)
            if len(uids) > max_per_poll:
                uids = uids[-max_per_poll:]

            if not uids:
                _update_progress(account.id, account.last_uid_seen)
                return PollResult(account.email, 0, 0, 0, 0, 0, account.last_uid_seen)

            response = client.fetch(uids, ["RFC822", "INTERNALDATE"])

            for uid in uids:
                fetched += 1
                try:
                    raw = response[uid][b"RFC822"]
                    msg = email_lib.message_from_bytes(raw)
                    sender = _decode_header(msg.get("From"))
                    subject = _decode_header(msg.get("Subject"))
                    body = extract_body(msg)

                    cls = classifier.classify(
                        subject, body, use_llm_fallback=use_llm_fallback,
                    )
                    classified += 1

                    if cls.label == "unknown":
                        skipped += 1
                        new_uid = max(new_uid, uid)
                        continue

                    match = reconciler.find_match(sender, subject, body)
                    if match.job_url is None:
                        skipped += 1
                        new_uid = max(new_uid, uid)
                        continue

                    final_conf = round(cls.confidence * match.confidence, 4)
                    reconciler.record_outcome(
                        match.job_url, cls.label, final_conf,
                        email_uid=str(uid), subject=subject,
                    )
                    matched += 1
                    new_uid = max(new_uid, uid)
                except Exception as e:
                    log.warning("Failed to process UID %s: %s", uid, e)
                    errors += 1
                    new_uid = max(new_uid, uid)

            _update_progress(account.id, new_uid)
    except Exception as e:
        log.error("Poll failed for %s: %s", account.email, e)
        errors += 1

    return PollResult(account.email, fetched, classified, matched,
                      skipped, errors, new_uid)


def poll_all(*, use_llm_fallback: bool = True,
             max_per_poll: int = DEFAULT_MAX_PER_POLL) -> list[PollResult]:
    """Poll every active account."""
    return [
        poll_account(a, use_llm_fallback=use_llm_fallback,
                     max_per_poll=max_per_poll)
        for a in list_accounts() if a.status == "active"
    ]


# ---------------------------------------------------------------------------
# Connection test (used by `applypilot inbox add`)
# ---------------------------------------------------------------------------

def test_connection(email: str, host: str, port: int, ssl: bool,
                    password: str, mailbox: str = "INBOX") -> tuple[bool, str]:
    """Quick login + select-folder check. Returns (ok, message)."""
    try:
        from imapclient import IMAPClient
    except ImportError:
        return False, "imapclient not installed"
    try:
        with IMAPClient(host, port=port, ssl=ssl) as client:
            client.login(email, password)
            client.select_folder(mailbox, readonly=True)
            return True, "ok"
    except Exception as e:
        return False, str(e)
