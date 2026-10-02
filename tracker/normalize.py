"""Normalization and sanitisation of scraped listings.

Two jobs, both applied before anything else touches a listing:

1. Make inconsistent platform data comparable — "SWE Intern", "Software
   Engineer Internship" and "Software Engineering Intern" must reduce to the
   same string or deduplication cannot work.
2. Treat every scraped string as untrusted input. Scraped text ends up in a
   spreadsheet the user opens, so it is length-capped, stripped of control
   characters, and defused against Excel formula injection.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

from tracker import _dateparse, config

# A cell beginning with any of these is interpreted as a formula by Excel and
# by every CSV importer. Scraped text is not trusted to be inert.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

_MAX_FIELD = 2000
_MAX_DESCRIPTION = 20000

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s+#.-]")


# ---------------------------------------------------------------------------
# Sanitisation (trust boundary)
# ---------------------------------------------------------------------------


def clean_text(value: object, limit: int = _MAX_FIELD) -> str:
    """Collapse whitespace, drop control characters, and cap length.

    Applied to every string arriving from a website.
    """
    if value is None:
        return ""
    text = str(value)
    # Drop Unicode control/format characters (Cc, Cf) — these include the
    # bidi overrides and zero-width joiners used to disguise text.
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))
    text = _WS.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text


def clean_description(value: object) -> str:
    return clean_text(value, limit=_MAX_DESCRIPTION)


def excel_safe(value: str) -> str:
    """Defuse Excel formula injection.

    A scraped description starting with "=" or "@" would be evaluated as a
    formula when the workbook is opened. Prefixing with an apostrophe forces
    Excel to treat it as literal text; the apostrophe is not part of the value
    and is not displayed.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _aliases() -> dict:
    return config.load("aliases")


def _fold(text: str) -> str:
    """Casefold, strip accents and most punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def company(raw: str) -> tuple[str, str]:
    """Return (display_name, normalized_name).

    The display name is what the user sees; the normalized name is the dedupe
    key. "NVIDIA Corporation", "Nvidia" and "NVIDIA Ltd" all normalize to
    "nvidia" so the same role on three platforms collapses to one row.
    """
    display = clean_text(raw, limit=200)
    if not display:
        return "", ""

    aliases = _aliases()
    folded = _fold(display)

    if folded in aliases.get("company_aliases", {}):
        canonical = aliases["company_aliases"][folded]
        return canonical, _fold(canonical)

    # Strip legal suffixes repeatedly: "Foo Technologies Ltd" -> "foo".
    tokens = folded.split()
    suffixes = set(aliases.get("company_suffixes", []))
    while tokens and tokens[-1] in suffixes:
        tokens.pop()
    normalized = " ".join(tokens) or folded

    if normalized in aliases.get("company_aliases", {}):
        canonical = aliases["company_aliases"][normalized]
        return canonical, _fold(canonical)

    return display, normalized


def title(raw: str) -> tuple[str, str]:
    """Return (display_title, normalized_title).

    Normalization expands abbreviations then removes words that describe the
    *packaging* of a role rather than the role itself — "Intern", "Summer
    2027", "12 month", the location. What survives is the actual job.
    """
    display = clean_text(raw, limit=300)
    if not display:
        return "", ""

    aliases = _aliases()
    folded = _fold(display)

    # Expand abbreviations token-wise so "SWE" -> "software engineer".
    expansions = aliases.get("title_expansions", {})
    tokens = [expansions.get(t, t) for t in folded.split()]
    folded = " ".join(tokens)

    # Remove noise phrases longest-first, so "industrial placement" is removed
    # as a unit before "placement" can match part of it.
    for noise in sorted(aliases.get("title_noise", []), key=len, reverse=True):
        folded = re.sub(rf"\b{re.escape(noise)}\b", " ", folded)

    normalized = _WS.sub(" ", folded).strip(" -.,;:/&")
    # If stripping noise removed everything (a listing titled just "Internship"),
    # fall back to the folded original rather than an empty dedupe key, which
    # would merge every such listing into one row.
    if not normalized:
        normalized = _fold(display)

    return display, normalized


def location(raw: str) -> tuple[str, str]:
    """Return (canonical_location, work_arrangement).

    Remote/Hybrid describe how you work, not where, so they are split out. A
    "Remote (UK)" listing keeps United Kingdom as its location and would
    otherwise be dropped by the location filter.
    """
    text = clean_text(raw, limit=200)
    if not text:
        return "", ""

    aliases = _aliases()
    folded = _fold(text)

    arrangement = ""
    for name, markers in aliases.get("work_arrangements", {}).items():
        if any(m in folded for m in markers):
            arrangement = name.capitalize().replace("Onsite", "On-site")
            for m in markers:
                folded = folded.replace(m, " ")
            break

    folded = _WS.sub(" ", folded).strip(" ,-()")
    if not folded:
        return ("Remote" if arrangement == "Remote" else ""), arrangement

    loc_aliases = aliases.get("location_aliases", {})
    if folded in loc_aliases:
        return loc_aliases[folded], arrangement

    # Multi-part locations ("London, United Kingdom") — take the first part
    # that maps to something known, else the first part as written.
    parts = [p.strip() for p in re.split(r"[,/|]|\band\b", folded) if p.strip()]
    for part in parts:
        if part in loc_aliases:
            return loc_aliases[part], arrangement

    return text.split(",")[0].strip(), arrangement


def work_type(raw: str) -> str:
    text = _fold(clean_text(raw, limit=100))
    if not text:
        return ""
    aliases = _aliases().get("work_type_aliases", {})
    if text in aliases:
        return aliases[text]
    for key, canonical in sorted(aliases.items(), key=lambda kv: len(kv[0]), reverse=True):
        if key in text:
            return canonical
    return clean_text(raw, limit=100)


#: Careers-platform posting URLs, reduced to the id that names one posting.
#: The same posting is linked in many shapes - listing vs apply page, EU vs US
#: host, with or without a locale or tracking query - so the URL itself is no
#: key, but the platform's own posting id is.
_POSTING_PATTERNS = (
    ("greenhouse", re.compile(r"greenhouse\.io/.*?(?:jobs/|token=)(\d{5,})", re.I)),
    ("greenhouse", re.compile(r"[?&]gh_jid=(\d{5,})", re.I)),
    ("lever", re.compile(r"lever\.co/[^/?#]+/([0-9a-f-]{36})", re.I)),
    ("ashby", re.compile(r"ashbyhq\.com/[^/?#]+/([0-9a-f-]{36})", re.I)),
    ("workable", re.compile(r"workable\.com/([^/?#]+)/j/([A-Z0-9]{6,})", re.I)),
    ("workday", re.compile(r"//([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com/.*/job/[^?#]*_([A-Za-z0-9-]+?)(?:[/?#]|$)", re.I)),
    ("oracle", re.compile(r"//([a-z0-9-]+)\.fa\.(?:[a-z0-9-]+\.)*oraclecloud\.com/[^?#]*/job/(\d+)", re.I)),
    ("eightfold", re.compile(r"//([a-z0-9-]+)\.eightfold\.ai/careers/job/(\d{6,})", re.I)),
    ("eightfold", re.compile(r"//apply\.careers\.microsoft\.com/careers/job/(\d{6,})", re.I)),
    ("amazon", re.compile(r"amazon\.jobs/(?:[a-z]{2}(?:-[a-z]{2})?/)?jobs/(\d+)", re.I)),
    ("oleeo", re.compile(r"//([a-z0-9-]+)\.tal\.net/.*?/opp/(\d+)-", re.I)),
)


def posting_key(url: str) -> str:
    """"platform:id" for a careers-platform posting URL, or "" for anything else.

    Two listings linking to one posting are the same job whatever their titles
    say, which is the one piece of evidence stronger than fuzzy matching.
    Generic careers pages carry no posting id and deliberately yield "".
    """
    for platform, pattern in _POSTING_PATTERNS:
        match = pattern.search(url or "")
        if match:
            return f"{platform}:" + ":".join(g.casefold() for g in match.groups())
    return ""


def skill(raw: str) -> str:
    """Canonical display spelling for one skill or technology."""
    text = clean_text(raw, limit=80).strip(" .,;")
    if not text:
        return ""
    return _aliases().get("skill_aliases", {}).get(text.casefold(), text)


def skills(values: list[str]) -> list[str]:
    """Canonicalise and de-duplicate a skill list, preserving order."""
    seen: dict[str, None] = {}
    for value in values:
        canonical = skill(value)
        if canonical:
            seen.setdefault(canonical, None)
    return list(seen)


def parse_date(raw: object, dayfirst: bool = True,
               allow_month_precision: bool = False) -> date | None:
    """Parse a date from scraped text, or return None.

    Never guesses. An unparseable string yields None and the caller records
    "no deadline" rather than inventing one - a fabricated deadline is worse
    than a missing one because it looks actionable.

    dayfirst defaults True and applies only to all-numeric forms: these are UK
    sites, where 03/04/2027 is 3 April.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw

    return _dateparse.find(
        clean_text(raw, limit=200),
        dayfirst=dayfirst,
        allow_month_precision=allow_month_precision,
    )


def extract_skills(text: str) -> tuple[list[str], list[str], list[str]]:
    """Find known skills mentioned in free text.

    Returns (all_skills, programming_languages, technologies). Only recognises
    terms already in the alias table — it never invents a skill from an unknown
    word, which keeps the workbook's skill columns trustworthy.
    """
    if not text:
        return [], [], []

    aliases = _aliases().get("skill_aliases", {})
    folded = " " + _fold(text) + " "
    found: dict[str, None] = {}

    for term, canonical in aliases.items():
        # Match on word boundaries so "R" does not match every capital R and
        # "go" does not match "going".
        if re.search(rf"(?<![\w+#]){re.escape(term)}(?![\w+#])", folded):
            found.setdefault(canonical, None)

    all_skills = list(found)
    languages = [s for s in all_skills if s in _LANGUAGES]
    technologies = [s for s in all_skills if s not in _LANGUAGES]
    return all_skills, languages, technologies


#: Which canonical skills count as programming languages rather than tools.
_LANGUAGES = frozenset(
    {
        "Python", "C++", "C#", "C", "Java", "JavaScript", "TypeScript", "Go",
        "Rust", "Scala", "MATLAB", "R", "SQL", "Bash", "Verilog",
        "SystemVerilog", "VHDL", "Kotlin", "Swift", "Ruby", "PHP", "Perl",
    }
)
