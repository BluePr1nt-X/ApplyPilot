"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import logging
from pathlib import Path

from applypilot.config import TAILORED_DIR

log = logging.getLogger(__name__)


# ── Resume Parser ────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """Parse a structured text resume into sections.

    Expects a format with header lines (name, title, location, contact)
    followed by ALL-CAPS section headers (SUMMARY, TECHNICAL SKILLS, etc.).

    Args:
        text: Full resume text.

    Returns:
        {"name": str, "title": str, "location": str, "contact": str, "sections": dict}
    """
    lines = [line.rstrip() for line in text.strip().split("\n")]

    # Header: first few lines before SUMMARY
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip().upper() == "SUMMARY":
            body_start = i
            break
        if line.strip():
            header_lines.append(line.strip())

    name = header_lines[0] if len(header_lines) > 0 else ""
    title = header_lines[1] if len(header_lines) > 1 else ""
    # The header may have 3 or 4 lines depending on whether location is included
    location = ""
    contact = ""
    if len(header_lines) > 3:
        location = header_lines[2]
        contact = header_lines[3]
    elif len(header_lines) > 2:
        # Could be location or contact -- check for email/phone indicators
        if "@" in header_lines[2] or "|" in header_lines[2]:
            contact = header_lines[2]
        else:
            location = header_lines[2]

    # Split body into sections by ALL-CAPS headers
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[body_start:]:
        stripped = line.strip()
        # Detect section headers (all caps, no leading dash/bullet, longer than 3 chars)
        if (
            stripped
            and stripped == stripped.upper()
            and not stripped.startswith("-")
            and len(stripped) > 3
            and not stripped.startswith("\u2022")
        ):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "name": name,
        "title": title,
        "location": location,
        "contact": contact,
        "sections": sections,
    }


def parse_skills(text: str) -> list[tuple[str, str]]:
    """Parse skills section into (category, value) pairs.

    Args:
        text: The TECHNICAL SKILLS section text.

    Returns:
        List of (category_name, skills_string) tuples.
    """
    skills: list[tuple[str, str]] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list[dict]:
    """Parse experience/project entries from section text.

    Args:
        text: The EXPERIENCE or PROJECTS section text.

    Returns:
        List of {"title": str, "subtitle": str, "bullets": list[str]} dicts.
    """
    entries: list[dict] = []
    lines = text.strip().split("\n")
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("\u2022 "):
            if current:
                current["bullets"].append(stripped[2:].strip())
        elif current is None or (
            not stripped.startswith("-")
            and not stripped.startswith("\u2022")
            and len(current.get("bullets", [])) > 0
        ):
            # New entry
            if current:
                entries.append(current)
            current = {"title": stripped, "subtitle": "", "bullets": []}
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


# ── HTML Template ────────────────────────────────────────────────────────

def build_html(resume: dict) -> str:
    """Build professional resume HTML from parsed data.

    Args:
        resume: Parsed resume dict from parse_resume().

    Returns:
        Complete HTML string ready for PDF rendering.
    """
    sections = resume["sections"]

    # Skills
    skills_html = ""
    if "TECHNICAL SKILLS" in sections:
        skills = parse_skills(sections["TECHNICAL SKILLS"])
        rows = ""
        for cat, val in skills:
            rows += f'<div class="skill-row"><span class="skill-cat">{cat}:</span> {val}</div>\n'
        skills_html = f'<div class="section"><div class="section-title">Technical Skills</div>{rows}</div>'

    # Experience
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'

    # Projects
    proj_html = ""
    if "PROJECTS" in sections:
        entries = parse_entries(sections["PROJECTS"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        proj_html = f'<div class="section"><div class="section-title">Projects</div>{items}</div>'

    # Education
    edu_html = ""
    if "EDUCATION" in sections:
        edu_text = sections["EDUCATION"].strip()
        edu_html = f'<div class="section"><div class="section-title">Education</div><div class="edu">{edu_text}</div></div>'

    # Summary
    summary_html = ""
    if "SUMMARY" in sections:
        summary_html = f'<div class="section"><div class="section-title">Summary</div><div class="summary">{sections["SUMMARY"].strip()}</div></div>'

    # Contact line parsing
    contact = resume["contact"]
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    contact_html = " &nbsp;|&nbsp; ".join(contact_parts)

    # Location line (may be empty)
    location_html = f'<div class="location">{resume["location"]}</div>' if resume["location"] else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0.35in 0.5in;
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 10pt;
    line-height: 1.35;
    color: #1a1a1a;
}}
.header {{
    text-align: center;
    margin-bottom: 4px;
    padding-bottom: 4px;
    border-bottom: 1.5px solid #2a7ab5;
}}
.name {{
    font-size: 18pt;
    font-weight: 700;
    color: #1a3a5c;
    letter-spacing: 0.5px;
}}
.title {{
    font-size: 10.5pt;
    color: #3a6b8c;
    margin: 1px 0;
}}
.location {{
    font-size: 9pt;
    color: #555;
}}
.contact {{
    font-size: 9pt;
    color: #444;
    margin-top: 1px;
}}
.contact a {{
    color: #2c3e50;
    text-decoration: none;
}}
.section {{
    margin-top: 5px;
}}
.section-title {{
    font-size: 10pt;
    font-weight: 700;
    color: #1a3a5c;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1.5px solid #2a7ab5;
    padding-bottom: 1px;
    margin-bottom: 3px;
}}
.summary {{
    font-size: 9.5pt;
    color: #333;
    line-height: 1.4;
}}
.skill-row {{
    font-size: 9.5pt;
    margin: 0;
    line-height: 1.35;
}}
.skill-cat {{
    font-weight: 600;
    color: #1a3a5c;
}}
.entry {{
    margin-bottom: 4px;
    break-inside: avoid;
}}
.entry-title {{
    font-weight: 600;
    font-size: 10pt;
    color: #1a3a5c;
}}
.entry-subtitle {{
    font-size: 9pt;
    color: #4a7a9b;
    font-style: italic;
    margin-bottom: 1px;
}}
ul {{
    margin-left: 14px;
    padding: 0;
}}
li {{
    font-size: 9.5pt;
    margin-bottom: 1px;
    line-height: 1.35;
}}
.edu {{
    font-size: 10pt;
}}
</style>
</head>
<body>
<div class="header">
    <div class="name">{resume['name']}</div>
    <div class="title">{resume['title']}</div>
    {location_html}
    <div class="contact">{contact_html}</div>
</div>
{summary_html}
{skills_html}
{exp_html}
{proj_html}
{edu_html}
</body>
</html>"""


# ── ATS-Safe HTML template ───────────────────────────────────────────────

def build_html_ats_safe(resume: dict) -> str:
    """Build a strictly ATS-parser-friendly HTML for the tailored resume.

    Differences from build_html:
      - Pure black text on white. No colors, no borders, no SVG.
      - Single column, single font family (Arial / Helvetica fallback).
      - No <ul>; bullets are rendered as `- ` text inside <p>.
      - No multi-column or table layouts that confuse Workday/Taleo parsers.
      - Plain section headings as bold <h2>, not styled bars.
    """
    sections = resume["sections"]

    def _bullets(items: list[str]) -> str:
        return "".join(f"<p>- {b}</p>\n" for b in items)

    def _entries_block(text: str) -> str:
        out = []
        for e in parse_entries(text):
            subtitle = f"<p><em>{e['subtitle']}</em></p>" if e["subtitle"] else ""
            out.append(
                f"<p><strong>{e['title']}</strong></p>{subtitle}{_bullets(e['bullets'])}"
            )
        return "".join(out)

    summary_html = ""
    if "SUMMARY" in sections:
        summary_html = (
            f"<h2>Summary</h2><p>{sections['SUMMARY'].strip()}</p>"
        )

    skills_html = ""
    if "TECHNICAL SKILLS" in sections:
        rows = [
            f"<p><strong>{cat}:</strong> {val}</p>"
            for cat, val in parse_skills(sections["TECHNICAL SKILLS"])
        ]
        skills_html = "<h2>Technical Skills</h2>" + "".join(rows)

    exp_html = ""
    if "EXPERIENCE" in sections:
        exp_html = "<h2>Experience</h2>" + _entries_block(sections["EXPERIENCE"])

    proj_html = ""
    if "PROJECTS" in sections:
        proj_html = "<h2>Projects</h2>" + _entries_block(sections["PROJECTS"])

    edu_html = ""
    if "EDUCATION" in sections:
        edu_html = (
            f"<h2>Education</h2><p>{sections['EDUCATION'].strip()}</p>"
        )

    contact = resume.get("contact", "")
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    contact_html = " | ".join(contact_parts)
    location_html = (
        f"<p>{resume['location']}</p>" if resume.get("location") else ""
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0.6in 0.7in;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11pt;
    line-height: 1.4;
    color: #000;
    background: #fff;
}}
h1 {{
    font-size: 14pt;
    font-weight: bold;
    margin-bottom: 2px;
}}
h2 {{
    font-size: 12pt;
    font-weight: bold;
    text-transform: uppercase;
    margin-top: 12px;
    margin-bottom: 4px;
}}
p {{
    font-size: 11pt;
    margin-bottom: 2px;
}}
strong {{ font-weight: bold; }}
em {{ font-style: italic; }}
.header {{ margin-bottom: 6px; }}
.contact {{ font-size: 10pt; }}
</style>
</head>
<body>
<div class="header">
<h1>{resume.get('name', '')}</h1>
<p>{resume.get('title', '')}</p>
{location_html}
<p class="contact">{contact_html}</p>
</div>
{summary_html}
{skills_html}
{exp_html}
{proj_html}
{edu_html}
</body>
</html>"""


# ── PDF Renderer ─────────────────────────────────────────────────────────

def render_pdf(html: str, output_path: str) -> None:
    """Render HTML to PDF using Playwright's headless Chromium.

    Args:
        html: Complete HTML string.
        output_path: Path to write the PDF file.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()


def render_pdf_ats_safe(text_path: Path, output_path: Path | None = None) -> Path:
    """Render the tailored resume in ATS-safe style. Same parser, stripped HTML.

    Use this for employers whose ATS is known to mis-parse styled PDFs
    (Workday, Taleo, iCIMS, Oracle Recruiting Cloud).
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")
    resume = parse_resume(text)
    html = build_html_ats_safe(resume)
    out = Path(output_path) if output_path else text_path.with_suffix(".pdf")
    render_pdf(html, str(out))
    log.info("ATS-safe PDF generated: %s", out)
    return out


# ── Public API ───────────────────────────────────────────────────────────

def convert_to_pdf(
    text_path: Path, output_path: Path | None = None, html_only: bool = False,
    engine: str = "modern",
) -> Path:
    """Convert a text resume/cover letter to PDF.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .pdf extension.
        html_only: If True, output HTML instead of PDF.
        engine: 'modern' (styled) or 'ats_safe' (stripped, parser-friendly).

    Returns:
        Path to the generated PDF (or HTML) file.
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")
    resume = parse_resume(text)
    if engine == "ats_safe":
        html = build_html_ats_safe(resume)
    else:
        html = build_html(resume)

    if html_only:
        out = output_path or text_path.with_suffix(".html")
        out = Path(out)
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = output_path or text_path.with_suffix(".pdf")
    out = Path(out)
    render_pdf(html, str(out))
    log.info("PDF generated (engine=%s): %s", engine, out)
    return out


def _pick_engine_for_url(url: str | None, default: str = "modern") -> str:
    """Choose 'modern' or 'ats_safe' based on the application URL host.

    Reads config/ats_profiles.yaml for per-domain overrides. ATS platforms
    known to mis-parse styled resumes (Workday, Taleo, iCIMS, etc.) get
    `ats_safe` automatically.
    """
    if not url:
        return default
    try:
        import yaml
        from urllib.parse import urlparse
        from applypilot.config import CONFIG_DIR
        cfg_path = CONFIG_DIR / "ats_profiles.yaml"
        if not cfg_path.exists():
            return default
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return default
        for entry in cfg.get("domains", []) or []:
            domain = (entry.get("domain") or "").strip().lower()
            if not domain:
                continue
            if host == domain or host.endswith("." + domain):
                return entry.get("engine", "ats_safe")
    except Exception:
        log.debug("ats_profiles lookup failed", exc_info=True)
    return default


def batch_convert(limit: int = 50, engine: str | None = None) -> int:
    """Convert .txt files in TAILORED_DIR that don't have corresponding PDFs.

    Scans for .txt files (excluding _JOB.txt and _REPORT.json), checks if a
    .pdf with the same stem already exists, and converts any that are missing.

    Args:
        limit: Maximum number of files to convert.
        engine: Force a specific engine ('modern' or 'ats_safe'). If None,
            picks per-job based on application_url + ats_profiles.yaml.

    Returns:
        Number of PDFs generated.
    """
    if not TAILORED_DIR.exists():
        log.warning("Tailored directory does not exist: %s", TAILORED_DIR)
        return 0

    txt_files = sorted(TAILORED_DIR.glob("*.txt"))
    candidates = [
        f for f in txt_files
        if not f.name.endswith("_JOB.txt")
    ]

    to_convert: list[Path] = []
    for f in candidates:
        pdf_path = f.with_suffix(".pdf")
        if not pdf_path.exists():
            to_convert.append(f)
        if len(to_convert) >= limit:
            break

    if not to_convert:
        log.info("All text files already have PDFs.")
        return 0

    # Per-file engine lookup: query the jobs table for the job whose
    # tailored_resume_path matches this txt file, and pick the engine
    # based on its application_url + ats_profiles.yaml.
    from applypilot.database import get_connection
    conn = None
    if engine is None:
        try:
            conn = get_connection()
        except Exception:
            conn = None

    log.info("Converting %d files to PDF...", len(to_convert))
    converted = 0
    for f in to_convert:
        try:
            chosen = engine
            if chosen is None and conn is not None:
                row = conn.execute(
                    "SELECT url, application_url FROM jobs "
                    "WHERE tailored_resume_path = ?",
                    (str(f),),
                ).fetchone()
                if row:
                    target_url = row["application_url"] or row["url"]
                    chosen = _pick_engine_for_url(target_url)
            chosen = chosen or "modern"

            convert_to_pdf(f, engine=chosen)
            converted += 1

            # Optional: validate PDF round-trip + persist engine choice
            if conn is not None:
                pdf_path = f.with_suffix(".pdf")
                try:
                    from applypilot.scoring.pdf_validator import validate_pdf
                    validation = validate_pdf(pdf_path, expected_text=f.read_text(encoding="utf-8"))
                    conn.execute(
                        "UPDATE jobs SET tailored_pdf_engine = ?, "
                        "tailored_pdf_validated = ? "
                        "WHERE tailored_resume_path = ?",
                        (chosen, 1 if validation.passed else 0, str(f)),
                    )
                    conn.commit()
                    if not validation.passed:
                        log.warning(
                            "PDF validation soft-fail for %s: missing %s",
                            f.name, validation.missing_tokens,
                        )
                except ImportError:
                    pass
                except Exception:
                    log.debug("validator failed", exc_info=True)
        except Exception as e:
            log.error("Failed to convert %s: %s", f.name, e)

    log.info("Done: %d/%d PDFs generated in %s", converted, len(to_convert), TAILORED_DIR)
    return converted
