"""Outcome feedback loop.

Polls a configured IMAP inbox, classifies recruiter emails into
acknowledged / rejected / interview / offer, and writes the result back
to the corresponding `jobs` row so analytics + scoring can see what's
actually converting.
"""
