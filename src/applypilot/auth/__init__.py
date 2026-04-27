"""Persistent login vault for ATS / employer career sites.

Stores Playwright `storage_state` JSON per domain at ~/.applypilot/auth/,
so the apply agent can skip SSO login walls that would otherwise be blocked.
"""
