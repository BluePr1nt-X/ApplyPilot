"""Round-trip validate a generated PDF against its source text.

Why: a styled HTML→PDF render can look fine visually but parse badly
through ATS systems (Workday, Taleo, iCIMS) that re-extract text in their
own way. This module re-extracts the PDF locally with `pdfplumber` and
checks that the candidate's name, email, and top skills survive.

Soft signal — we warn but never block tailoring on a fail.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@dataclass
class ValidationResult:
    """Outcome of validate_pdf."""
    passed: bool
    extracted_text: str
    missing_tokens: list[str] = field(default_factory=list)
    notes: str = ""


def _extract_pdf_text(pdf_path: Path) -> str:
    """Use pdfplumber to extract concatenated text from a PDF."""
    try:
        import pdfplumber
    except ImportError as e:
        raise ImportError(
            "pdfplumber is required for PDF validation. "
            "Install with: pip install pdfplumber"
        ) from e

    out: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            out.append(t)
    return "\n".join(out)


def _expected_tokens(source_text: str) -> list[str]:
    """Pick a small set of must-survive tokens from the source resume.

    Includes: name (first non-empty header line), email (first match),
    and top 3 skill categories.
    """
    tokens: list[str] = []

    lines = [ln.strip() for ln in source_text.splitlines() if ln.strip()]
    if lines:
        tokens.append(lines[0])  # likely the name

    m = _EMAIL_RE.search(source_text)
    if m:
        tokens.append(m.group(0))

    # Top 3 skills section labels (e.g. "Languages:", "Cloud:", "Frameworks:")
    skills_section = ""
    in_skills = False
    for line in source_text.splitlines():
        s = line.strip()
        if s.upper() == "TECHNICAL SKILLS":
            in_skills = True
            continue
        if in_skills:
            if s and s.upper() == s and len(s) > 3 and ":" not in s:
                break  # next section header
            if ":" in s:
                cat = s.split(":", 1)[0].strip()
                if cat and cat not in tokens:
                    tokens.append(cat)
            if len([t for t in tokens if ":" not in t]) >= 5:
                break

    return tokens


def validate_pdf(pdf_path: Path | str,
                 expected_text: str | None = None,
                 expected_tokens: list[str] | None = None,
                 ) -> ValidationResult:
    """Round-trip a generated PDF and check critical tokens survived.

    Args:
        pdf_path: PDF file to inspect.
        expected_text: Optional source resume text — will derive tokens.
        expected_tokens: Override token list directly.

    Returns:
        ValidationResult.passed = True only if every token is found
        somewhere in the extracted text (case-insensitive).
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return ValidationResult(False, "", [], notes=f"missing pdf: {pdf_path}")

    try:
        extracted = _extract_pdf_text(pdf_path)
    except ImportError as e:
        return ValidationResult(False, "", [], notes=str(e))
    except Exception as e:
        return ValidationResult(False, "", [], notes=f"extraction error: {e}")

    if expected_tokens is None:
        if not expected_text:
            return ValidationResult(False, extracted, [],
                                    notes="no expected text/tokens provided")
        expected_tokens = _expected_tokens(expected_text)

    missing: list[str] = []
    haystack = extracted.lower()
    for tok in expected_tokens:
        if not tok:
            continue
        if tok.lower() not in haystack:
            missing.append(tok)

    return ValidationResult(
        passed=len(missing) == 0,
        extracted_text=extracted,
        missing_tokens=missing,
        notes=f"checked {len(expected_tokens)} token(s)",
    )
