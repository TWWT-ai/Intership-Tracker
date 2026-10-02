"""Application deadlines found anywhere in a posting, with the evidence.

The old reader (`verify._deadline_from_text`) took the first date within 80
characters AFTER a fixed label, in label order, so "Deadline: rolling. Start
date 1 July 2027" produced 1 July 2027. Here every date in the text is found in
reading order, and one counts as a deadline only when its own clause says so -
a posting, start or event date in the same text is never mistaken for one.

Supported forms (anything else is noted, never guessed):

    3 Oct 2026 | 3 October 2026 | 3rd October 2026 | 3rd of October 2026
    October 3, 2026 | October 3rd, 2026 | 2026-10-03 | 2026/10/03
    3/10/2026 | 03/10/2026 | 03.10.2026 | 03-10-2026   (numeric: see below)

A numeric date is read in the order its SOURCE states (the page's <html lang>,
a locale in the URL, a platform whose convention was checked) - never from the
job's location. A part above 12 settles it; otherwise it is AMBIGUOUS. Month
and year only, a date with no year and a two-digit year are recorded as notes
with no date. Offline: this module opens nothing.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date

from tracker import config
from tracker._dateparse import _parse

OK, AMBIGUOUS, MONTH_ONLY, NO_YEAR, NOT_SUPPORTED = "ok", "AMBIGUOUS", "MONTH_ONLY", "NO_YEAR", "NOT_SUPPORTED"
DAYFIRST, MONTHFIRST = "dayfirst", "monthfirst"

CONFIRMED, LISTED, CONFLICT, ROLLING, NONE = "CONFIRMED", "LISTED", "CONFLICT", "ROLLING", "NONE"
#: A row's deadline status when no usable date was found, most telling first.
_NOTE_ORDER = (AMBIGUOUS, NOT_SUPPORTED, NO_YEAR, MONTH_ONLY, ROLLING)

_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
           "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
_ORD = r"(?:\s?(?:st|nd|rd|th))?"
#: (pattern, kind), most specific first; a later pattern never takes text an
#: earlier one matched.
_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    (re.compile(r"\b(\d{4})[/.](\d{1,2})[/.](\d{1,2})\b"), "year_first"),
    (re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b"), "numeric"),
    (re.compile(r"\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2}\b(?![/.\-]?\d)"), "two_digit_year"),
    (re.compile(rf"\b(\d{{1,2}}){_ORD}(?:\s+of)?\s+({_MONTHS})\.?,?\s+(\d{{4}})\b", re.I), "day_month_year"),
    (re.compile(rf"\b({_MONTHS})\.?\s+(\d{{1,2}}){_ORD},?\s+(\d{{4}})\b", re.I), "month_day_year"),
    (re.compile(rf"\b(\d{{1,2}}){_ORD}(?:\s+of)?\s+({_MONTHS})\b\.?", re.I), "no_year"),
    (re.compile(rf"\b({_MONTHS})\.?\s+(\d{{1,2}}){_ORD}\b(?!,?\s*\d)", re.I), "no_year"),
    (re.compile(rf"\b({_MONTHS})\.?\s+(\d{{4}})\b", re.I), "month_only"),
]
_CLAUSE_END = re.compile(r"(?:[.!?;|•\n]\s)|(?:\s[-–—]\s)|(?:\s{2,})")


@dataclass
class Mention:
    """One date-like piece of text, in reading order."""
    start: int
    end: int
    text: str
    kind: str
    status: str                 # ok | AMBIGUOUS | MONTH_ONLY | NO_YEAR | NOT_SUPPORTED
    value: date | None = None
    order: str = ""             # how day and month were told apart


@dataclass
class Candidate:
    """A date some source gives as this row's application deadline."""
    date: str                   # ISO, or "" for a note with no usable date
    status: str                 # ok | AMBIGUOUS | MONTH_ONLY | NO_YEAR | NOT_SUPPORTED | ROLLING
    source: str                 # platform or "employer page"
    url: str
    method: str                 # feed field | description text | employer page
    employer_page: bool
    quote: str
    raw: str = ""
    order: str = ""
    places: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _settings() -> dict:
    return config.load("verification")


def _words(phrases) -> re.Pattern | None:
    phrases = [p for p in phrases or [] if str(p).strip()]
    if not phrases:
        return None
    alts = "|".join(re.escape(p).replace(r"\ ", r"[\s/&-]+") for p in sorted(phrases, key=len, reverse=True))
    return re.compile(rf"(?<![\w])(?:{alts})(?![\w])", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def order_from_locale(locale: str) -> str:
    """dayfirst | monthfirst | "" from a stated locale ("en-GB", "en_us", "fr")."""
    if not locale:
        return ""
    table = {str(k).casefold(): v for k, v in (_settings().get("locale_date_order") or {}).items()}
    key = locale.replace("_", "-").casefold()
    if key in table:
        return table[key]
    language = key.split("-")[0]
    # Plain "en" says nothing about day/month order.
    return "" if language == "en" else table.get(language, "")


def locale_of_url(url: str) -> str:
    """A locale segment in a URL, e.g. Workday's /en-GB/. Recorded only: it is
    the site's display language, not evidence of how a date was written."""
    m = re.search(r"/([a-z]{2}[-_][a-zA-Z]{2})(?:/|$)", url or "")
    return m.group(1) if m else ""


def platform_order(source: str) -> str:
    """Only for platforms whose convention has been checked (config)."""
    return str((_settings().get("platform_date_order") or {}).get(source, "") or "")


def mentions(text: str, order: str = "", order_basis: str = "") -> list[Mention]:
    """Every date-like piece of `text`, in reading order.

    `order` is dayfirst/monthfirst when the source states it, else "" - and
    then a numeric date with both parts <= 12 is AMBIGUOUS.
    """
    text = text or ""
    taken: list[tuple[int, int]] = []
    found: list[Mention] = []
    for pattern, kind in _PATTERNS:
        for m in pattern.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            found.append(_read(m, kind, order, order_basis))
    return sorted(found, key=lambda x: x.start)


def _read(m: re.Match, kind: str, order: str, basis: str) -> Mention:
    raw = m.group(0)
    base = dict(start=m.start(), end=m.end(), text=raw, kind=kind)
    if kind == "two_digit_year":
        return Mention(**base, status=NOT_SUPPORTED, order="two-digit year")
    if kind == "no_year":
        return Mention(**base, status=NO_YEAR, order="no year stated")
    if kind == "month_only":
        return Mention(**base, status=MONTH_ONLY, order="month and year only")
    if kind == "iso":
        value = _valid(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
        return Mention(**base, status=OK if value else NOT_SUPPORTED, value=value, order="ISO")
    if kind == "year_first":
        value = _valid(f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}")
        return Mention(**base, status=OK if value else NOT_SUPPORTED, value=value, order="year first")
    if kind in ("day_month_year", "month_day_year"):
        value = _parse(re.sub(r"(\d)\s?(st|nd|rd|th)\b|\bof\b", r"\1", raw, flags=re.I), dayfirst=False)
        return Mention(**base, status=OK if value else NOT_SUPPORTED, value=value, order="month named")
    # numeric d/m/y or m/d/y
    a, b, year = int(m.group(1)), int(m.group(2)), m.group(3)
    if a > 12 and b <= 12:
        use, why = DAYFIRST, "first part above 12"
    elif b > 12 and a <= 12:
        use, why = MONTHFIRST, "second part above 12"
    elif a > 12 and b > 12:
        return Mention(**base, status=NOT_SUPPORTED, order="neither part is a month")
    elif a == b:
        use, why = DAYFIRST, "day and month equal"
    elif order in (DAYFIRST, MONTHFIRST):
        use, why = order, f"{order} ({basis})" if basis else order
    else:
        return Mention(**base, status=AMBIGUOUS, order="day/month order not stated by the source")
    day, month = (a, b) if use == DAYFIRST else (b, a)
    value = _valid(f"{year}-{month:02d}-{day:02d}")
    return Mention(**base, status=OK if value else NOT_SUPPORTED, value=value, order=why)


def _valid(iso: str) -> date | None:
    try:
        value = date.fromisoformat(iso)
    except ValueError:
        return None
    return value if date(2000, 1, 1) <= value <= date(date.today().year + 5, 12, 31) else None


# ---------------------------------------------------------------------------
# Which dates are deadlines
# ---------------------------------------------------------------------------

def _clause_before(text: str, start: int, limit: int = 160) -> str:
    window = text[max(0, start - limit):start]
    cut = [m.end() for m in _CLAUSE_END.finditer(window)]
    return window[cut[-1]:] if cut else window


def _clause_after(text: str, end: int, limit: int = 60) -> str:
    window = text[end:end + limit]
    m = _CLAUSE_END.search(window)
    return window[:m.start()] if m else window


def _is_deadline(text: str, mention: Mention) -> tuple[bool, str]:
    """(is it a deadline?, why) from the clause the date sits in."""
    s = _settings()
    strong, weak, negative = (_words(s.get("deadline_phrases")), _words(s.get("deadline_weak_words")),
                              _words(s.get("not_deadline_words")))
    before = _clause_before(text, mention.start)
    after = _clause_after(text, mention.end)
    last = lambda pattern, t: max((m.end() for m in pattern.finditer(t)), default=-1) if pattern else -1  # noqa: E731
    cue = last(strong, before)
    near = before[-int(s.get("weak_cue_chars", 40)):]
    weak_at = last(weak, near)
    if weak_at >= 0:
        weak_at += len(before) - len(near)
    cue = max(cue, weak_at)
    against = last(negative, before)
    if cue >= 0 and cue >= against:
        return True, "deadline wording before the date"
    if strong and strong.match(after.lstrip(" :,-(")) or (strong and re.match(
            r"^\s*(?:is|was|will be|as)\s+(?:the\s+)?", after) and strong.search(after[:40])):
        return True, "deadline wording after the date"
    return False, "not described as a deadline" if against < 0 else "described as another kind of date"


def _quote(text: str, start: int, end: int, width: int = 80) -> str:
    return " ".join(text[max(0, start - width):min(len(text), end + width)].split())


def candidates(text: str, *, source: str, url: str, method: str, employer_page: bool,
               order: str = "", order_basis: str = "", places_pattern=None) -> list[Candidate]:
    """Every deadline this text states, plus notes for deadline-worded dates
    that cannot be used (ambiguous, month only, no year...) and for rolling
    recruitment. Dates not described as deadlines are left out."""
    out: list[Candidate] = []
    for mention in mentions(text, order, order_basis):
        is_deadline, _ = _is_deadline(text, mention)
        if not is_deadline:
            continue
        clause = _clause_before(text, mention.start) + text[mention.start:mention.end]
        where = sorted({m.group(0).casefold() for m in places_pattern.finditer(clause)}) if places_pattern else []
        out.append(Candidate(
            date=mention.value.isoformat() if mention.value else "", status=mention.status, source=source,
            url=url, method=method, employer_page=employer_page,
            quote=_quote(text, mention.start, mention.end), raw=mention.text, order=mention.order, places=where))
    rolling = _words(_settings().get("rolling_phrases"))
    if rolling and (m := rolling.search(text or "")):
        out.append(Candidate(date="", status=ROLLING, source=source, url=url, method=method,
                             employer_page=employer_page, quote=_quote(text, m.start(), m.end())))
    return out


def field_candidate(value: date | None, *, source: str, url: str, employer_page: bool = False) -> Candidate | None:
    """A platform's own deadline field (Trackr closingDate, validThrough...)."""
    if not value:
        return None
    return Candidate(date=value.isoformat(), status=OK, source=source, url=url, method="feed field",
                     employer_page=employer_page, quote=f"{source} lists a deadline of {value.isoformat()}",
                     order="structured field")


# ---------------------------------------------------------------------------
# One row's deadline
# ---------------------------------------------------------------------------

@dataclass
class Resolution:
    status: str
    deadline: date | None          # what the Deadline column shows
    confirmed: date | None         # employer page only
    provisional: date | None       # an earlier conflicting date, kept apart
    reason: str
    applicable: list[dict]


def resolve(evidence: list[dict], row_places: set[str], current: date | None) -> Resolution:
    """The row's deadline from all its candidates.

    The Deadline column shows the employer-confirmed date where there is one;
    otherwise the date the row already shows (the platform's). Differing dates
    are CONFLICT with every candidate kept, and an earlier one is kept apart as
    the provisional reminder - never written over the employer's date.
    """
    def applies(c: dict) -> bool:
        return not c.get("places") or not row_places or bool(set(c["places"]) & row_places)

    usable = [c for c in evidence if applies(c)]
    dated = [c for c in usable if c["status"] == OK and c["date"]]
    days = sorted({c["date"] for c in dated})
    employer = sorted({c["date"] for c in dated if c["employer_page"]})
    earliest = date.fromisoformat(days[0]) if days else None

    if employer:
        confirmed = date.fromisoformat(employer[0])
        if len(employer) > 1:
            return Resolution(CONFLICT, confirmed, None, earliest,
                              f"the employer page states {len(employer)} different dates: {', '.join(employer)}",
                              usable)
        if earliest and earliest < confirmed:
            return Resolution(CONFLICT, confirmed, confirmed, earliest,
                              f"employer confirms {confirmed}; an earlier {earliest} is listed elsewhere", usable)
        return Resolution(CONFIRMED, confirmed, confirmed, None, "stated on the employer's own posting page", usable)
    if dated:
        show = current if current and current.isoformat() in days else date.fromisoformat(days[0])
        if len(days) > 1:
            return Resolution(CONFLICT, show, None, earliest if earliest and earliest < show else None,
                              f"sources list different dates ({', '.join(days)}); none confirmed by the employer",
                              usable)
        return Resolution(LISTED, show, None, None, "listed by a platform or the posting text, "
                          "not confirmed on the employer's page", usable)
    notes = [s for s in _NOTE_ORDER if any(c["status"] == s for c in usable)]
    if notes:
        return Resolution(notes[0], current, None, None, _NOTE_WHY[notes[0]], usable)
    return Resolution(NONE, current, None, None, "no deadline found", usable)


_NOTE_WHY = {
    AMBIGUOUS: "a numeric deadline whose day/month order the source does not state",
    NOT_SUPPORTED: "the deadline is written in a form this reader does not accept",
    NO_YEAR: "the deadline gives no year",
    MONTH_ONLY: "the deadline gives only a month and year",
    ROLLING: "recruitment is on a rolling basis or may close early",
}
