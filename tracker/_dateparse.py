"""Date extraction from scraped text.

Kept apart from the rest of normalization because the rules are fiddly and
worth stating plainly:

* A recognised date SHAPE is located first, then parsed. Handing raw text to a
  fuzzy parser lets it fill missing components from today's date, which turns
  "April" into a real-looking deadline.
* Day-first only applies to all-numeric dates. "April 3, 2027" is unambiguous
  and must not be reordered.
* Anything that does not match a known shape returns None. A fabricated
  deadline is worse than a missing one, because it looks actionable.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from dateutil import parser as dateparser

_ORDINAL = re.compile(r"(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)

_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|"
    "november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)

#: Ordered most-specific first. Each entry is (pattern, is_numeric), where
#: is_numeric decides whether day-first ordering applies.
_PATTERNS: tuple[tuple[re.Pattern, bool], ...] = (
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), False),                                  # 2027-04-03
    (re.compile(rf"\b\d{{1,2}}\s+(?:{_MONTHS})\.?,?\s+\d{{4}}\b", re.I), False),    # 3 April 2027
    (re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}},?\s+\d{{4}}\b", re.I), False),    # April 3, 2027
    (re.compile(r"\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}\b"), True),                     # 03/04/2027
    (re.compile(r"\b\d{4}[/.]\d{1,2}[/.]\d{1,2}\b"), False),                        # 2027/04/03
)

#: Month-only, e.g. "June 2027", resolved to the first of that month.
#: Deliberately NOT in _PATTERNS: a deadline must never be month-precision,
#: because "closes June 2027" would silently become "closes 1 June" and mislead.
#: Start dates are different - month precision is how employers state them.
_MONTH_YEAR = re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{4}}\b", re.I)

#: Outside this range a "date" is a parse artefact, not a deadline.
_EARLIEST = date(2000, 1, 1)


def find(text: str, dayfirst: bool = True, allow_month_precision: bool = False) -> date | None:
    """First real date in `text`, or None.

    `dayfirst` applies only to all-numeric forms, where 03/04/2027 is genuinely
    ambiguous. These are UK sites, so it defaults to True.

    `allow_month_precision` additionally accepts "June 2027", resolving it to
    the first of that month. Use it for start dates only, never for deadlines.
    """
    if not text:
        return None

    text = _ORDINAL.sub(r"\1", str(text))

    for pattern, is_numeric in _PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        parsed = _parse(match.group(0), dayfirst and is_numeric)
        if parsed is not None:
            return parsed

    if allow_month_precision:
        match = _MONTH_YEAR.search(text)
        if match:
            return _parse("1 " + match.group(0), dayfirst=False)

    return None


def _parse(fragment: str, dayfirst: bool) -> date | None:
    try:
        parsed = dateparser.parse(fragment, dayfirst=dayfirst)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed is None:
        return None

    result = parsed.date() if isinstance(parsed, datetime) else parsed
    latest = date(date.today().year + 5, 12, 31)
    return result if _EARLIEST <= result <= latest else None
