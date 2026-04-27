"""Active-profile router for multi-profile support.

Layout:
    ~/.applypilot/
        active_profile             # single line: name of the active profile
        profiles/
            default/
                profile.json
                resume.txt
                resume.pdf
            backend/                # additional families
                profile.json
                resume.txt
                resume.pdf

Backwards-compat:
    Older installs put `profile.json`, `resume.txt`, `resume.pdf` directly
    in `~/.applypilot/`. On first call to any router function, those files
    are migrated to `profiles/default/`.

Public API:
    - get_active() / set_active(name)
    - list_profiles()
    - profile_dir(name=None) / profile_file(name=None) / resume_text(name=None) / resume_pdf(name=None)
    - load_profile(name=None)
    - add_profile(name, copy_from=None)
    - delete_profile(name)
    - migrate_legacy()
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from applypilot.config import APP_DIR

log = logging.getLogger(__name__)

PROFILES_DIR = APP_DIR / "profiles"
ACTIVE_PROFILE_FILE = APP_DIR / "active_profile"
DEFAULT_NAME = "default"

_FILES = ("profile.json", "resume.txt", "resume.pdf")
_LEGACY_FILES = _FILES  # same names, just at APP_DIR root


@dataclass
class ProfileInfo:
    name: str
    dir: Path
    has_profile_json: bool
    has_resume_txt: bool
    has_resume_pdf: bool
    is_active: bool


# ---------------------------------------------------------------------------
# Legacy migration
# ---------------------------------------------------------------------------

def migrate_legacy() -> str | None:
    """Move legacy single-profile files into profiles/default/.

    Idempotent. Returns the name of the migrated profile, or None if no
    migration was needed.
    """
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    default_dir = PROFILES_DIR / DEFAULT_NAME

    legacy_present = [APP_DIR / f for f in _LEGACY_FILES if (APP_DIR / f).exists()]
    if not legacy_present:
        if not ACTIVE_PROFILE_FILE.exists():
            ACTIVE_PROFILE_FILE.write_text(DEFAULT_NAME, encoding="utf-8")
        return None

    default_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for fname in _LEGACY_FILES:
        src = APP_DIR / fname
        dst = default_dir / fname
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))
            moved.append(fname)
        elif src.exists() and dst.exists():
            # Both exist — keep the destination, remove the legacy duplicate.
            src.unlink()

    if moved:
        log.info("Migrated legacy profile files → %s: %s",
                 default_dir, ", ".join(moved))

    if not ACTIVE_PROFILE_FILE.exists():
        ACTIVE_PROFILE_FILE.write_text(DEFAULT_NAME, encoding="utf-8")

    return DEFAULT_NAME


# ---------------------------------------------------------------------------
# Active profile
# ---------------------------------------------------------------------------

def get_active() -> str:
    """Return the active profile name (creates default if missing)."""
    migrate_legacy()
    if ACTIVE_PROFILE_FILE.exists():
        name = ACTIVE_PROFILE_FILE.read_text(encoding="utf-8").strip()
        if name:
            return name
    ACTIVE_PROFILE_FILE.write_text(DEFAULT_NAME, encoding="utf-8")
    return DEFAULT_NAME


def set_active(name: str) -> None:
    """Switch active profile. Profile must exist."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Profile name cannot be empty.")
    if not _profile_exists(name):
        raise FileNotFoundError(
            f"Profile '{name}' does not exist. Run `applypilot profile add {name}` first."
        )
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_PROFILE_FILE.write_text(name, encoding="utf-8")
    log.info("Active profile set to '%s'", name)


def _profile_exists(name: str) -> bool:
    """A profile exists if its directory does."""
    return (PROFILES_DIR / name).is_dir()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def profile_dir(name: str | None = None) -> Path:
    """Directory for a profile (defaults to active)."""
    return PROFILES_DIR / (name or get_active())


def profile_file(name: str | None = None) -> Path:
    return profile_dir(name) / "profile.json"


def resume_text(name: str | None = None) -> Path:
    return profile_dir(name) / "resume.txt"


def resume_pdf(name: str | None = None) -> Path:
    return profile_dir(name) / "resume.pdf"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_profiles() -> list[ProfileInfo]:
    """Enumerate every profile directory under PROFILES_DIR."""
    migrate_legacy()
    if not PROFILES_DIR.exists():
        return []
    active = get_active()
    out: list[ProfileInfo] = []
    for d in sorted(PROFILES_DIR.iterdir()):
        if not d.is_dir():
            continue
        out.append(ProfileInfo(
            name=d.name,
            dir=d,
            has_profile_json=(d / "profile.json").exists(),
            has_resume_txt=(d / "resume.txt").exists(),
            has_resume_pdf=(d / "resume.pdf").exists(),
            is_active=(d.name == active),
        ))
    return out


def add_profile(name: str, copy_from: str | None = None) -> Path:
    """Create a profile directory; optionally seed from another profile.

    Returns the directory path. If `copy_from` is given, the source
    profile's profile.json + resume.{txt,pdf} are copied as a starting
    point — user can then edit.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Profile name cannot be empty.")
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("Profile name must not contain path separators or leading dots.")

    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    new_dir = PROFILES_DIR / name
    if new_dir.exists():
        raise FileExistsError(f"Profile '{name}' already exists at {new_dir}")
    new_dir.mkdir(parents=True)

    if copy_from:
        src_dir = PROFILES_DIR / copy_from
        if not src_dir.is_dir():
            raise FileNotFoundError(f"Source profile '{copy_from}' not found.")
        for fname in _FILES:
            src = src_dir / fname
            if src.exists():
                shutil.copy2(str(src), str(new_dir / fname))

    log.info("Created profile '%s' at %s", name, new_dir)
    return new_dir


def delete_profile(name: str) -> bool:
    """Remove a profile directory. Refuses to delete the active profile."""
    name = (name or "").strip()
    if not name or name == DEFAULT_NAME:
        # Allow deleting 'default' only if it's not the active one.
        pass
    target = PROFILES_DIR / name
    if not target.is_dir():
        return False
    if name == get_active():
        raise RuntimeError(
            f"Cannot delete '{name}' — it's the active profile. "
            f"Switch with `applypilot profile use <other>` first."
        )
    shutil.rmtree(str(target))
    log.info("Deleted profile '%s'", name)
    return True


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_profile(name: str | None = None) -> dict:
    """Load profile.json for the given (or active) profile.

    Raises FileNotFoundError if the profile.json doesn't exist — caller is
    expected to run `applypilot init` to create it.
    """
    p = profile_file(name)
    if not p.exists():
        raise FileNotFoundError(
            f"Profile not found at {p}. Run `applypilot init` to create one."
        )
    return json.loads(p.read_text(encoding="utf-8"))


def resolve_job_profile(job: dict) -> str:
    """Pick the profile name to use for a single job row.

    Precedence: explicit target_profile -> discovered_with_profile -> active.
    Falls back to the active profile if the named profile doesn't exist
    on disk anymore.
    """
    candidates = [job.get("target_profile"), job.get("discovered_with_profile")]
    for cand in candidates:
        if cand and _profile_exists(cand):
            return cand
    return get_active()


def load_resume_text(name: str | None = None) -> str:
    """Read the resume.txt for the given (or active) profile.

    Raises FileNotFoundError with a stable message if missing.
    """
    p = resume_text(name)
    if not p.exists():
        raise FileNotFoundError(
            f"resume.txt not found at {p}. Run `applypilot init` to add one."
        )
    return p.read_text(encoding="utf-8")
