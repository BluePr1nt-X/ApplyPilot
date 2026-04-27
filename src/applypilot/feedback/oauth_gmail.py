"""Gmail OAuth helper — replaces App Passwords for inbox polling.

Flow:
  1. User runs `applypilot inbox add-gmail`.
  2. We open the system browser to Google's consent screen using the
     `google-auth-oauthlib` InstalledAppFlow on a localhost loopback port.
  3. The user logs in + grants `https://mail.google.com/` scope.
  4. We persist the refresh token + client credentials in the OS keyring
     under service `applypilot-gmail-oauth` so future pollers can mint
     access tokens without re-prompting.
  5. At poll time we mint an access token and authenticate to IMAP via
     SASL XOAUTH2.

The user must supply their own Google Cloud OAuth client (Desktop App
credentials.json) — we don't ship Anthropic client IDs. Setup docs in
the CLI command's help text.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from applypilot.config import APP_DIR

log = logging.getLogger(__name__)

KEYRING_SERVICE = "applypilot-gmail-oauth"
SCOPES = ["https://mail.google.com/"]
GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993

# Where the user drops their OAuth client_secret.json. Optional — they can
# instead set env vars GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET.
DEFAULT_CLIENT_SECRET_PATH = APP_DIR / "google_oauth_client.json"


def _load_client_config() -> dict | None:
    """Resolve installed-app client config from disk or env."""
    if DEFAULT_CLIENT_SECRET_PATH.exists():
        try:
            data = json.loads(DEFAULT_CLIENT_SECRET_PATH.read_text(encoding="utf-8"))
            return data
        except Exception as e:
            log.warning("Failed to parse %s: %s", DEFAULT_CLIENT_SECRET_PATH, e)

    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    csec = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if cid and csec:
        return {
            "installed": {
                "client_id": cid,
                "client_secret": csec,
                "auth_uri":  "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
    return None


def is_available() -> bool:
    """True if google-auth-oauthlib is installed."""
    try:
        import google_auth_oauthlib  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Interactive seeding
# ---------------------------------------------------------------------------

def authorize(email: str) -> dict:
    """Run the OAuth installed-app flow, return refresh-token bundle.

    Raises RuntimeError if google-auth-oauthlib isn't installed or no
    client config can be resolved. The result dict is also persisted to
    keyring; callers don't need to handle storage themselves.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise RuntimeError(
            "google-auth-oauthlib is required. "
            "Install with: pip install google-auth-oauthlib"
        ) from e

    client_config = _load_client_config()
    if not client_config:
        raise RuntimeError(
            f"No Google OAuth client config found.\n"
            f"Drop a Desktop App credentials JSON at {DEFAULT_CLIENT_SECRET_PATH} "
            f"or set GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET in .env."
        )

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # `run_local_server` opens a browser, captures the redirect, and returns
    # credentials with a refresh_token. port=0 picks a free localhost port.
    creds = flow.run_local_server(
        port=0, prompt="consent",
        open_browser=True, access_type="offline",
    )

    bundle = {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "scopes": list(creds.scopes or SCOPES),
        "email": email,
    }
    save_credentials(email, bundle)
    return bundle


def save_credentials(email: str, bundle: dict) -> None:
    try:
        import keyring
    except ImportError as e:
        raise RuntimeError(
            "`keyring` is required to persist OAuth refresh tokens."
        ) from e
    keyring.set_password(KEYRING_SERVICE, email, json.dumps(bundle))


def load_credentials(email: str) -> dict | None:
    try:
        import keyring
        raw = keyring.get_password(KEYRING_SERVICE, email)
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def delete_credentials(email: str) -> bool:
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, email)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Access-token minting
# ---------------------------------------------------------------------------

def get_access_token(email: str) -> str | None:
    """Mint a fresh access token using the stored refresh token.

    Returns None if no credentials are stored or refresh fails.
    """
    bundle = load_credentials(email)
    if not bundle:
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError:
        log.warning("google-auth not installed — install with: pip install google-auth")
        return None

    try:
        creds = Credentials(
            token=None,
            refresh_token=bundle["refresh_token"],
            client_id=bundle["client_id"],
            client_secret=bundle["client_secret"],
            token_uri=bundle["token_uri"],
            scopes=bundle.get("scopes") or SCOPES,
        )
        creds.refresh(Request())
        return creds.token
    except Exception as e:
        log.warning("OAuth refresh failed for %s: %s", email, e)
        return None


# ---------------------------------------------------------------------------
# IMAP login via XOAUTH2
# ---------------------------------------------------------------------------

def imap_login(client, email: str) -> bool:
    """Authenticate an open `imapclient.IMAPClient` to Gmail via XOAUTH2.

    Returns True on success. Caller is responsible for opening the client
    against `imap.gmail.com:993` (use `GMAIL_IMAP_HOST`/`PORT` constants).
    """
    token = get_access_token(email)
    if not token:
        return False
    auth_string = f"user={email}\x01auth=Bearer {token}\x01\x01"
    try:
        client.oauth2_login(email, token)
        return True
    except Exception:
        # Some imapclient versions expose `authenticate("XOAUTH2", ...)` only
        try:
            client.authenticate("XOAUTH2", lambda _: auth_string.encode("ascii"))
            return True
        except Exception as e:
            log.warning("XOAUTH2 IMAP auth failed for %s: %s", email, e)
            return False
