"""Persistent login vault.

Each registered domain maps to a Playwright `storage_state` JSON file at
`~/.applypilot/auth/<domain>.json`. The interactive seeding flow opens a
non-headless browser, lets the user complete login (including SSO), then
serializes the resulting cookie jar + localStorage to disk.

The vault is consumed by `apply.chrome.inject_cookies()` (PR4) — it loads the
saved storage_state into the per-worker Chrome profile before subprocess
launch, so the apply agent never has to traverse a login wall again.

Optional encryption (PR12): if the user sets `APPLYPILOT_VAULT_PASSWORD`
in the environment (or stores it in the keyring under service
`applypilot-vault`), all storage_state files are Fernet-encrypted with a
PBKDF2-derived key. Without a password configured, files are written as
plaintext JSON (legacy/default behavior).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import yaml

from applypilot.config import APP_DIR, CONFIG_DIR
from applypilot.database import get_connection

log = logging.getLogger(__name__)

# Where storage_state JSON files live
AUTH_DIR = APP_DIR / "auth"

# Registry shipped with the package
REGISTRY_PATH = CONFIG_DIR / "auth_sites.yaml"

# Magic header prepended to encrypted vault files. Used to detect plaintext
# vs. encrypted on read so legacy unencrypted files keep working.
ENCRYPTED_HEADER = b"AP-VAULT-ENC-V1\n"

# Static salt for PBKDF2 derivation. We're not protecting from a determined
# offline attacker (cookies are domain-scoped, not particularly sensitive)
# — the goal is to keep plaintext cookies off disk if the user has set a
# password. A static salt is fine here.
_PBKDF2_SALT = b"applypilot-vault-v1"
_PBKDF2_ITERATIONS = 200_000

VAULT_PASSWORD_ENV = "APPLYPILOT_VAULT_PASSWORD"
KEYRING_VAULT_SERVICE = "applypilot-vault"
KEYRING_VAULT_USER = "master"


def _get_vault_password() -> str | None:
    pw = os.environ.get(VAULT_PASSWORD_ENV)
    if pw:
        return pw
    try:
        import keyring
        return keyring.get_password(KEYRING_VAULT_SERVICE, KEYRING_VAULT_USER)
    except Exception:
        return None


def _derive_key(password: str) -> bytes:
    """PBKDF2 → urlsafe-base64 32 byte key suitable for Fernet."""
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32,
        salt=_PBKDF2_SALT, iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _fernet():
    """Return a Fernet instance, or None if vault password not configured."""
    pw = _get_vault_password()
    if not pw:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(_derive_key(pw))
    except ImportError:
        log.warning("cryptography not installed — vault encryption disabled")
        return None


def encryption_enabled() -> bool:
    """True iff a vault password is configured AND cryptography is importable."""
    return _fernet() is not None


def _write_state(path: Path, state: dict) -> None:
    """Write a storage_state dict to disk, encrypting if a password is set."""
    raw = json.dumps(state).encode("utf-8")
    f = _fernet()
    if f is None:
        path.write_bytes(raw)
        return
    encrypted = f.encrypt(raw)
    path.write_bytes(ENCRYPTED_HEADER + encrypted)


def _read_state(path: Path) -> dict:
    """Read a storage_state file, decrypting if encrypted. Returns dict.

    Raises ValueError if the file is encrypted but no password is configured,
    or RuntimeError on decryption failure.
    """
    data = path.read_bytes()
    if data.startswith(ENCRYPTED_HEADER):
        f = _fernet()
        if f is None:
            raise ValueError(
                f"Vault file {path} is encrypted but no APPLYPILOT_VAULT_PASSWORD "
                f"is set. Export the password (and ensure `cryptography` is installed)."
            )
        try:
            payload = data[len(ENCRYPTED_HEADER):]
            return json.loads(f.decrypt(payload).decode("utf-8"))
        except Exception as e:
            raise RuntimeError(
                f"Failed to decrypt {path} — wrong password? ({e})"
            ) from e
    # Plaintext path
    return json.loads(data.decode("utf-8"))


@dataclass
class AuthSite:
    """One registered domain in the vault."""
    domain: str
    login_url: str
    expiry_days: int = 7
    aliases: list[str] | None = None
    notes: str | None = None


@dataclass
class VaultEntry:
    """Database row from `auth_sessions`."""
    domain: str
    storage_state_path: str | None
    seeded_at: str | None
    last_used_at: str | None
    last_refresh_attempted_at: str | None
    status: str | None
    expiry_days: int
    notes: str | None


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------

def load_registry() -> list[AuthSite]:
    """Load the auth_sites.yaml registry."""
    if not REGISTRY_PATH.exists():
        return []
    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    sites = []
    for entry in raw.get("sites", []) or []:
        domain = (entry.get("domain") or "").strip().lower()
        if not domain:
            continue
        sites.append(AuthSite(
            domain=domain,
            login_url=entry.get("login_url") or f"https://{domain}",
            expiry_days=int(entry.get("expiry_days", 7)),
            aliases=entry.get("aliases") or [],
            notes=entry.get("notes"),
        ))
    return sites


def find_site(domain_or_url: str) -> AuthSite | None:
    """Find a registered site by exact domain match or URL host."""
    needle = (domain_or_url or "").strip().lower()
    if needle.startswith(("http://", "https://")):
        needle = urlparse(needle).hostname or ""

    for site in load_registry():
        if site.domain == needle:
            return site
        # Allow user to seed a more specific subdomain match (e.g.
        # company.wd1.myworkdayjobs.com matches myworkdayjobs.com).
        if needle.endswith("." + site.domain):
            return site
        for alias in site.aliases or []:
            if alias.lower() == needle:
                return site
    return None


# ---------------------------------------------------------------------------
# DB helpers (auth_sessions table created in database.init_aux_tables)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_session(domain: str, storage_state_path: Path, expiry_days: int,
                    notes: str | None = None) -> None:
    conn = get_connection()
    now = _now_iso()
    conn.execute("""
        INSERT INTO auth_sessions (domain, storage_state_path, seeded_at, last_used_at,
                                   status, expiry_days, notes)
        VALUES (?, ?, ?, ?, 'fresh', ?, ?)
        ON CONFLICT(domain) DO UPDATE SET
            storage_state_path=excluded.storage_state_path,
            seeded_at=excluded.seeded_at,
            last_used_at=excluded.last_used_at,
            status='fresh',
            expiry_days=excluded.expiry_days,
            notes=COALESCE(excluded.notes, auth_sessions.notes)
    """, (domain, str(storage_state_path), now, now, expiry_days, notes))
    conn.commit()


def list_sessions() -> list[VaultEntry]:
    """All vault entries from the DB."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT domain, storage_state_path, seeded_at, last_used_at, "
        "last_refresh_attempted_at, status, COALESCE(expiry_days, 7), notes "
        "FROM auth_sessions ORDER BY domain"
    ).fetchall()
    return [VaultEntry(*tuple(r)) for r in rows]


def get_entry(domain: str) -> VaultEntry | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT domain, storage_state_path, seeded_at, last_used_at, "
        "last_refresh_attempted_at, status, COALESCE(expiry_days, 7), notes "
        "FROM auth_sessions WHERE domain=?", (domain,),
    ).fetchone()
    return VaultEntry(*tuple(row)) if row else None


def is_fresh(entry: VaultEntry) -> bool:
    """True if the entry's storage_state file exists and is not expired."""
    if not entry.storage_state_path or not Path(entry.storage_state_path).exists():
        return False
    if not entry.seeded_at:
        return False
    try:
        seeded = datetime.fromisoformat(entry.seeded_at)
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - seeded
    return age < timedelta(days=entry.expiry_days)


def lookup_for_url(url: str) -> tuple[AuthSite, VaultEntry, Path] | None:
    """Find a fresh storage_state for the given URL.

    Matches by exact host or suffix (so company.wd1.myworkdayjobs.com matches
    a vault entry for `myworkdayjobs.com`).

    Returns (site, entry, storage_state_path) if a fresh session exists,
    or None if no match / expired.
    """
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None

    for entry in list_sessions():
        if entry.domain == host or host.endswith("." + entry.domain):
            site = find_site(entry.domain)
            if site is None:
                continue
            if not is_fresh(entry):
                continue
            path = Path(entry.storage_state_path)
            return site, entry, path
    return None


def get_authenticated_domains() -> list[str]:
    """Return domains with a fresh storage_state — for prompt injection."""
    return [e.domain for e in list_sessions() if is_fresh(e)]


def mark_used(domain: str) -> None:
    """Touch last_used_at so we know the session is still being consumed."""
    conn = get_connection()
    conn.execute("UPDATE auth_sessions SET last_used_at=? WHERE domain=?",
                 (_now_iso(), domain))
    conn.commit()


def mark_refresh_attempted(domain: str) -> None:
    conn = get_connection()
    conn.execute("UPDATE auth_sessions SET last_refresh_attempted_at=?, status='needs_refresh' "
                 "WHERE domain=?", (_now_iso(), domain))
    conn.commit()


# ---------------------------------------------------------------------------
# Interactive seeding
# ---------------------------------------------------------------------------

def seed_session(domain: str, login_url: str | None = None,
                 prompt_input: callable = input) -> Path:
    """Open a non-headless browser, let the user log in, save storage_state.

    Args:
        domain: Domain to seed (matched against auth_sites.yaml registry).
        login_url: Override the registry's login URL.
        prompt_input: Injectable input callable (for testing).

    Returns:
        Path to the saved storage_state JSON.

    Raises:
        FileNotFoundError: If Playwright is not installed.
        SystemExit:        If the user declines to confirm login.
    """
    AUTH_DIR.mkdir(parents=True, exist_ok=True)

    site = find_site(domain)
    if site is None and login_url is None:
        raise ValueError(
            f"Domain '{domain}' not in auth_sites.yaml registry. "
            "Add an entry there or pass --login-url."
        )

    target_login = login_url or (site.login_url if site else f"https://{domain}")
    expiry = site.expiry_days if site else 7
    notes = site.notes if site else None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise FileNotFoundError(
            "Playwright is required for `applypilot login`. "
            "Install with: pip install playwright && playwright install chromium"
        ) from e

    out_path = AUTH_DIR / f"{domain}.json"
    log.info("Seeding session for %s → %s", domain, out_path)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto(target_login, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                log.warning("Initial navigation hiccup (%s) — continuing anyway", e)

            print()
            print(f"Browser opened to: {target_login}")
            print(f"Sign in to {domain} (use SSO if needed — Google/Microsoft/etc.).")
            print("When you see the post-login dashboard / homepage, return here.")
            print()
            answer = (prompt_input(
                f"Press Enter when logged in to {domain} (or type 'skip' to abort): "
            ) or "").strip().lower()
            if answer == "skip":
                browser.close()
                raise SystemExit("Login skipped — no session saved.")

            state = context.storage_state()
            cookie_count = len(state.get("cookies") or [])
            origin_count = len(state.get("origins") or [])
            log.info("Captured %d cookies / %d origins for %s",
                     cookie_count, origin_count, domain)

            if cookie_count == 0:
                print(
                    f"WARNING: 0 cookies captured for {domain}. "
                    "Login may not have completed — saving anyway."
                )

            _write_state(out_path, state)
            _record_session(domain, out_path, expiry, notes)
            enc_note = " (encrypted)" if encryption_enabled() else ""
            print(f"Saved {cookie_count} cookies → {out_path}{enc_note}")
            return out_path
        finally:
            try:
                browser.close()
            except Exception:
                pass


def delete_session(domain: str) -> bool:
    """Remove a vault entry and its on-disk storage_state.

    Returns True if anything was deleted.
    """
    entry = get_entry(domain)
    deleted = False
    if entry and entry.storage_state_path:
        p = Path(entry.storage_state_path)
        if p.exists():
            p.unlink()
            deleted = True
    conn = get_connection()
    cur = conn.execute("DELETE FROM auth_sessions WHERE domain=?", (domain,))
    conn.commit()
    return deleted or (cur.rowcount > 0)


def reencrypt_all() -> tuple[int, int]:
    """Re-write every vault file with the current encryption setting.

    Reads each file with the *current* setting first, then writes back.
    For password-change flows that swap between encrypted and plaintext,
    use `change_master_password()` instead — it captures plaintext in
    memory before flipping the setting.
    """
    rewritten = 0
    errors = 0
    for entry in list_sessions():
        if not entry.storage_state_path:
            continue
        path = Path(entry.storage_state_path)
        if not path.exists():
            continue
        try:
            state = _read_state(path)
            _write_state(path, state)
            rewritten += 1
        except Exception as e:
            log.warning("Failed to re-encrypt %s: %s", path, e)
            errors += 1
    return rewritten, errors


def change_master_password(new_password: str | None) -> tuple[int, int]:
    """Atomically migrate vault files to a new encryption setting.

    Reads every storage_state with the *current* password, then sets the
    new password (or clears it), then re-writes all files. Pass None or
    empty string to disable encryption.

    Returns (migrated, errors).
    """
    # Phase 1: read every file with current setting into memory.
    decrypted: dict[Path, dict] = {}
    errors = 0
    for entry in list_sessions():
        if not entry.storage_state_path:
            continue
        path = Path(entry.storage_state_path)
        if not path.exists():
            continue
        try:
            decrypted[path] = _read_state(path)
        except Exception as e:
            log.warning("Failed to read %s for migration: %s", path, e)
            errors += 1

    # Phase 2: switch the master password setting.
    set_master_password(new_password)

    # Phase 3: write everything back under the new setting.
    migrated = 0
    for path, state in decrypted.items():
        try:
            _write_state(path, state)
            migrated += 1
        except Exception as e:
            log.warning("Failed to re-write %s: %s", path, e)
            errors += 1
    return migrated, errors


def set_master_password(password: str | None) -> None:
    """Persist (or clear) the master password in the OS keyring.

    Pass None or empty string to clear. This does NOT migrate existing
    files — call `change_master_password()` instead for atomic migration.
    """
    try:
        import keyring
    except ImportError as e:
        raise RuntimeError(
            "`keyring` is required to store the vault master password."
        ) from e
    if not password:
        try:
            keyring.delete_password(KEYRING_VAULT_SERVICE, KEYRING_VAULT_USER)
        except Exception:
            pass
        return
    keyring.set_password(KEYRING_VAULT_SERVICE, KEYRING_VAULT_USER, password)
