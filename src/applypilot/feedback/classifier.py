"""Classify a recruiter email into an outcome label.

Two-tier strategy:
  1. Rule-based regex on subject + first chunk of body. Cheap, deterministic.
  2. Single LLM call as fallback when rules are inconclusive but the sender
     looks like a recruiting domain. Costs ~1 LLM call per ambiguous email.

Output labels (single source of truth — used by inbox + reconciler + DB):
  acknowledged | rejected | interview | offer | unknown
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

LABELS = ("acknowledged", "rejected", "interview", "offer", "unknown")


@dataclass
class Classification:
    """Output of classify(...)."""
    label: str            # one of LABELS
    confidence: float     # 0.0 - 1.0
    source: str           # "rules" | "llm" | "default"
    matched: str | None   # rule name or LLM raw output (for debugging)


# ---------------------------------------------------------------------------
# Rule-based patterns
# ---------------------------------------------------------------------------

# Each rule: label, regex, confidence. Higher-confidence rules are checked
# first — first match wins.
_RULES: list[tuple[str, re.Pattern, float, str]] = [
    # Offer — checked first because "offer" is a strong signal
    ("offer", re.compile(
        r"\b(pleased to (extend|offer)|formal offer|offer of employment|"
        r"job offer|we are excited to offer|offer letter)\b", re.IGNORECASE
    ), 0.95, "offer.strong"),

    # Interview — be careful: "no interview" / "didn't interview" should not match
    ("interview", re.compile(
        r"\b(schedule (a|an) (call|chat|interview|screen|conversation)|"
        r"next steps? in (the|our) (interview )?process|"
        r"book (a|an) (interview|screen|call)|"
        r"(technical|recruiter|hiring manager|phone|video|onsite) "
        r"(screen|interview|call|chat))\b", re.IGNORECASE
    ), 0.90, "interview.strong"),

    ("interview", re.compile(
        r"\b(would (you )?(like|love) to (chat|meet|talk)|"
        r"15(-| )?minute (call|chat)|"
        r"available (this|next) week (for|to))\b", re.IGNORECASE
    ), 0.75, "interview.soft"),

    # Rejection — "not moving forward", "decided to pursue other candidates"
    ("rejected", re.compile(
        r"\b(unfortunately|regret to inform|after careful consideration|"
        r"(decided|chosen) to (move forward|proceed) with (other|another)|"
        r"not (be )?(moving|moving forward|selected|advancing|proceeding)|"
        r"other candidates whose|"
        r"will not be (proceeding|moving forward)|"
        r"unable to (offer|move forward)|"
        r"position has been filled|role has been filled|"
        r"more closely (matched|aligned) with our needs)\b", re.IGNORECASE
    ), 0.92, "rejected.strong"),

    ("rejected", re.compile(
        r"\b(thank you for (your interest|applying), (?:but|however)|"
        r"have decided not to|"
        r"have selected (a|another) candidate)\b", re.IGNORECASE
    ), 0.80, "rejected.medium"),

    # Acknowledgment — "we received your application"
    ("acknowledged", re.compile(
        r"\b(received your application|application received|"
        r"thank you for applying|thanks for applying|"
        r"we have your application|application has been received|"
        r"successfully submitted|application (submission|complete))\b",
        re.IGNORECASE
    ), 0.85, "acknowledged.strong"),
]

# Subject-only quick wins (run first, very high confidence)
_SUBJECT_FAST: list[tuple[str, re.Pattern, float, str]] = [
    ("offer",        re.compile(r"^(re: )?offer( of employment| letter)?$", re.I), 0.97, "subject.offer"),
    ("rejected",     re.compile(r"\bapplication (status|update)\b.*\b(unfortunately|regret)\b", re.I), 0.85, "subject.reject_explicit"),
    ("acknowledged", re.compile(r"^(re: )?(thanks?|thank you) for (applying|your application)", re.I), 0.90, "subject.ack"),
    ("acknowledged", re.compile(r"^application received", re.I), 0.95, "subject.ack_explicit"),
]


def classify_with_rules(subject: str, body: str) -> Classification | None:
    """Run rule-based classification. Returns None if no rule matched."""
    subject = subject or ""
    body = body or ""

    # Subject-only fast path
    for label, pat, conf, name in _SUBJECT_FAST:
        if pat.search(subject):
            return Classification(label, conf, "rules", name)

    # Combined subject + first 1500 chars of body
    combined = f"{subject}\n{body[:1500]}"
    for label, pat, conf, name in _RULES:
        if pat.search(combined):
            return Classification(label, conf, "rules", name)
    return None


# ---------------------------------------------------------------------------
# LLM fallback
# ---------------------------------------------------------------------------

_LLM_PROMPT_TEMPLATE = (
    "Classify this recruiter email into ONE label, no explanation.\n"
    "Allowed labels: acknowledged, rejected, interview, offer, unknown.\n\n"
    "Definitions:\n"
    "- acknowledged: confirms application was received; no decision yet.\n"
    "- rejected: company is not moving forward / position filled.\n"
    "- interview: invites the candidate to a call, screen, or interview.\n"
    "- offer: extends a job offer.\n"
    "- unknown: doesn't fit any of the above (e.g. recruiter cold outreach).\n\n"
    "Subject: {subject}\n\n"
    "Body (first 1500 chars):\n{body}\n\n"
    "Output exactly one word from the allowed list."
)


def classify_with_llm(subject: str, body: str) -> Classification:
    """Single LLM call to classify an ambiguous email.

    On any error or unexpected output, returns Classification("unknown", 0.0, ...).
    """
    try:
        from applypilot.llm import get_client
    except ImportError:
        return Classification("unknown", 0.0, "default", "llm_unavailable")

    prompt = _LLM_PROMPT_TEMPLATE.format(
        subject=(subject or "")[:300],
        body=(body or "")[:1500],
    )

    try:
        client = get_client()
        raw = client.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=10, temperature=0.0,
        )
    except Exception as e:
        log.warning("LLM classify failed: %s — defaulting to unknown", e)
        return Classification("unknown", 0.0, "default", f"llm_error:{e}")

    cleaned = (raw or "").strip().lower().strip(".:,")
    # Take the first word in case the model wrapped output in markdown
    first_word = cleaned.split()[0] if cleaned else ""
    if first_word in LABELS:
        return Classification(first_word, 0.65, "llm", raw.strip())
    return Classification("unknown", 0.0, "llm", raw.strip())


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def classify(subject: str, body: str, *, use_llm_fallback: bool = True,
             min_rules_confidence: float = 0.70) -> Classification:
    """Classify an email. Tries rules first, then optional LLM fallback.

    Args:
        subject: Email subject line.
        body: Plain-text email body (HTML stripped).
        use_llm_fallback: Whether to call LLM on ambiguous matches.
        min_rules_confidence: If a rule matches but with confidence below
            this threshold, also consult the LLM.
    """
    rules_match = classify_with_rules(subject, body)
    if rules_match and rules_match.confidence >= min_rules_confidence:
        return rules_match

    if use_llm_fallback:
        llm_match = classify_with_llm(subject, body)
        # If LLM found something, prefer it. Otherwise return whatever
        # rules found, even if low confidence.
        if llm_match.label != "unknown":
            return llm_match

    return rules_match or Classification("unknown", 0.0, "default", None)
