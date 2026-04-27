"""Multi-profile support for different role families.

Each profile is a directory under `~/.applypilot/profiles/<name>/` containing
its own `profile.json`, `resume.txt`, `resume.pdf`. The "active" profile is
referenced by the single-line file `~/.applypilot/active_profile`.

Legacy layout (single profile at `~/.applypilot/profile.json` etc.) is
migrated automatically to `profiles/default/` on first call.
"""
