"""Role assessment from the posting itself: separate findings, each with its
evidence and its own review outcome - never one merged score.

    role_relevance      TECHNICAL | NOT_TECHNICAL | UNCLEAR          - from the duties
    study_level         CURRENT_UNDERGRADUATE | GRADUATE_ONLY | POSTGRADUATE_ONLY |
                        EXPERIENCED | LEVEL_UNCLEAR | NOT_STATED     - who may apply
    degree_subject_fit  CS_REQUIRED | CS_ACCEPTED | ANY_DISCIPLINE | EQUIVALENCE_UNCLEAR |
                        BROAD_FIELD | OTHER_SUBJECT_ONLY | NOT_STATED - which degrees

Personal eligibility (Eligible for You, tracker/eligibility.py) and the
deadline (tracker/deadline_extract.py) are the other two findings.

The text is only trusted once it is tied to one of the row's own listings for
the same place (`text_source`): merged rows can carry another city's posting,
and a finding read from someone else's text is a guess. Missing, short or
unconfirmed text gives UNCLEAR / NOT_STATED - review, never a rejection.
Offline: this module opens nothing. Phrases are config/relevance.yaml.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from tracker import config, normalize
from tracker.models import Job

TECHNICAL, NOT_TECHNICAL, UNCLEAR = "TECHNICAL", "NOT_TECHNICAL", "UNCLEAR"
CURRENT_UNDERGRADUATE, GRADUATE_ONLY, POSTGRADUATE_ONLY = "CURRENT_UNDERGRADUATE", "GRADUATE_ONLY", "POSTGRADUATE_ONLY"
EXPERIENCED, LEVEL_UNCLEAR, NOT_STATED = "EXPERIENCED", "LEVEL_UNCLEAR", "NOT_STATED"
CS_REQUIRED, CS_ACCEPTED, ANY_DISCIPLINE = "CS_REQUIRED", "CS_ACCEPTED", "ANY_DISCIPLINE"
EQUIVALENCE_UNCLEAR, BROAD_FIELD, OTHER_SUBJECT_ONLY = "EQUIVALENCE_UNCLEAR", "BROAD_FIELD", "OTHER_SUBJECT_ONLY"
#: Findings that are firm evidence a role is not for a current CS undergraduate.
STRONG_AGAINST = {NOT_TECHNICAL, GRADUATE_ONLY, POSTGRADUATE_ONLY, EXPERIENCED, OTHER_SUBJECT_ONLY}

CONFIRMED, EQUIVALENT, UNCONFIRMED, NONE = "confirmed", "equivalent", "unconfirmed", "none"
#: description_source prefix for text read off the employer's page itself
#: (`main.py eligibility`), rather than taken from a feed's listing.
PAGE = "page:"

_QUOTE = 160


# ---------------------------------------------------------------------------
# Phrase matching
# ---------------------------------------------------------------------------

def _phrase(p: str) -> str:
    return re.escape(p.strip()).replace(r"\ ", r"[\s/&-]+")


def _any(phrases) -> re.Pattern | None:
    """Whole-word match on any phrase. "c++" and "c#" need a custom boundary."""
    phrases = [p for p in (phrases or []) if str(p).strip()]
    if not phrases:
        return None
    alts = "|".join(_phrase(p) for p in sorted(phrases, key=len, reverse=True))
    return re.compile(rf"(?<![\w]){'(?:' + alts + ')'}(?![\w+#])", re.IGNORECASE)


@dataclass
class _Rules:
    raw: dict
    heading: re.Pattern
    heading_kind: dict
    technical: list[re.Pattern]
    business: list[re.Pattern]
    technical_all: re.Pattern
    incidental: re.Pattern | None
    title_technical: re.Pattern | None
    title_other: re.Pattern | None
    ug: dict
    deg: dict


_cache: dict[int, _Rules] = {}


def rules() -> _Rules:
    raw = config.load("relevance")
    key = id(raw)
    if key not in _cache:
        kinds = {}
        for kind, heads in (raw.get("headings") or {}).items():
            for h in heads:
                kinds.setdefault(h.casefold(), kind)
        alts = "|".join(_phrase(h) for h in sorted(kinds, key=len, reverse=True))
        groups = lambda name: [_any(line.split("|")) for line in raw.get(name) or []]  # noqa: E731
        tech = groups("technical_duties")
        ug = {k: _any(v) for k, v in (raw.get("undergraduate") or {}).items()}
        deg = {k: _any(v) for k, v in (raw.get("degree") or {}).items()}
        _cache.clear()
        _cache[key] = _Rules(
            raw=raw,
            heading=re.compile(rf"(?<![\w])({alts})(\s*[:?]|\s+(?=[A-Z0-9\"'(]))", re.IGNORECASE),
            heading_kind=kinds,
            technical=tech,
            business=groups("business_duties"),
            technical_all=_any([p for line in raw.get("technical_duties") or [] for p in line.split("|")]),
            incidental=_any(raw.get("incidental")),
            title_technical=_any(raw.get("title_technical")),
            title_other=_any((config.load("sources")["sources"]["careers"].get("non_tech_function_terms") or [])
                             + (config.load("sources")["sources"]["careers"].get("other_field_terms") or [])),
            ug=ug,
            deg=deg,
        )
    return _cache[key]


def _clean(text: str) -> str:
    """One line, dashes and replacement characters flattened."""
    text = str(text or "").replace("�", " ").replace("–", "-").replace("—", "-")
    text = text.replace("’", "'").replace("‘", "'")
    return " ".join(text.split())


def _quote(text: str, start: int, end: int) -> str:
    """The sentence around a match, trimmed to a readable quote."""
    left = max(text.rfind(". ", 0, start), text.rfind("? ", 0, start), text.rfind("! ", 0, start))
    left = 0 if left < 0 else left + 2
    right_candidates = [i for i in (text.find(". ", end), text.find("? ", end), text.find("! ", end)) if i >= 0]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    if right - left > _QUOTE:
        left = max(left, start - _QUOTE // 2)
        right = min(right, left + _QUOTE)
    return text[left:right].strip(" .")


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

@dataclass
class Sections:
    duties: str
    requirements: str
    has_duties: bool
    has_requirements: bool


def sections(text: str) -> Sections:
    """Split a flattened posting by its headings. A heading counts where a
    sentence starts, or where ":"/"?" follows it, and it must start with a
    capital: "the role" inside a sentence is not a heading."""
    r = rules()
    found = []
    for m in r.heading.finditer(text):
        start = m.start(1)
        if not text[start].isupper():
            continue
        before = text[:start].rstrip()
        sentence_start = not before or before[-1] in ".!?;:|)]•·*-"
        if not (sentence_start or m.group(2).strip() in (":", "?")):
            continue
        found.append((start, m.end(), _kind_of(m.group(1))))
    parts: dict[str, list[str]] = {"duties": [], "requirements": [], "background": []}
    for i, (_, end, kind) in enumerate(found):
        stop = found[i + 1][0] if i + 1 < len(found) else len(text)
        parts.setdefault(kind, []).append(text[end:stop])
    return Sections(" ".join(parts["duties"]), " ".join(parts["requirements"]),
                    bool(parts["duties"]), bool(parts["requirements"]))


def _kind_of(heading: str) -> str:
    fold = lambda s: re.sub(r"[\s/&-]+", " ", s.casefold()).strip()  # noqa: E731
    wanted = fold(heading)
    return next((kind for known, kind in rules().heading_kind.items() if fold(known) == wanted), "background")


def _headings(text: str) -> list[tuple[int, int, str]]:
    """(start, end, heading words) of each section heading, as `sections` finds them."""
    out = []
    for m in rules().heading.finditer(text):
        start = m.start(1)
        before = text[:start].rstrip()
        if text[start].isupper() and (not before or before[-1] in ".!?;:|)]•·*-"
                                      or m.group(2).strip() in (":", "?")):
            out.append((start, m.end(), m.group(1)))
    return out


_SENTENCE_END = re.compile(r"(?<=[.!?;•|])\s+")
#: "(e.g. procurement, commercial ...)" is one clause, not two.
_ABBREVIATION = re.compile(r"(?i)\b(?:e\.g|i\.e|etc|incl|approx|vs|no)\.$")
#: A bullet the feed flattened into the paragraph: "...sample production
#: Conceptualising, developing...". A capitalised -ing word after a lowercase
#: word, unless that word governs it ("studying Engineering", "in Computing").
_BULLET = re.compile(r"(?<=[a-z0-9%)\]])\s+(?=[A-Z][a-z]{2,}ing\b)")
_GOVERNS = {"in", "of", "and", "or", "the", "a", "an", "for", "to", "with", "studying", "study", "studies",
            "degree", "within", "into", "including", "e.g", "such", "as", "your", "our", "on", "at", "by"}
_ING_NOUNS = {"engineering", "computing", "accounting", "marketing", "banking", "manufacturing", "surveying",
              "nursing", "building", "planning", "trading", "consulting", "publishing", "mining", "printing",
              "training", "programming", "modelling", "modeling", "processing", "learning", "housing",
              "funding", "lending", "pricing", "sourcing", "testing", "monitoring", "reporting", "networking"}


def clauses(text: str) -> list[tuple[int, int]]:
    """(start, end) of each clause of a flattened posting: sentences, plus the
    boundaries flattening erased - section headings and run-together bullets.
    Without them, Envisics' "Developing mechanical parts" ran into "Minimum
    Requirements Studying engineering" and read as a mechanical degree
    requirement (INT-001403)."""
    cuts = {0, len(text)}
    cuts |= {m.end() for m in _SENTENCE_END.finditer(text)
             if not _ABBREVIATION.search(text, max(0, m.start() - 8), m.start())}
    for start, end, _ in _headings(text):
        cuts |= {start, end}
    for m in _BULLET.finditer(text):
        word = re.match(r"[A-Za-z]*", text[m.end():]).group(0).casefold()
        prev = text[:m.start()].rsplit(" ", 1)[-1].casefold().strip(",")
        if word not in _ING_NOUNS and prev not in _GOVERNS:
            cuts.add(m.end())
    edges = sorted(cuts)
    return [(a, b) for a, b in zip(edges, edges[1:]) if text[a:b].strip()]


def _clause_at(spans: list[tuple[int, int]], pos: int) -> tuple[int, int]:
    return next(((a, b) for a, b in spans if a <= pos < b), (0, 0))


# ---------------------------------------------------------------------------
# The three verdicts
# ---------------------------------------------------------------------------

def role_relevance(text: str, title: str = "") -> tuple[str, str, list[str]]:
    """(verdict, reason, quotes) from the duties sections only.

    Technology named as a tool ("using data and technology", "AI tools") is
    removed first, and a programming requirement is never a duty: whether the
    work itself is computing is what decides.
    """
    r = rules()
    if len(text) < int(r.raw.get("min_text_chars", 400)):
        return UNCLEAR, f"only {len(text)} characters of posting text", []
    secs = sections(text)
    if not secs.has_duties or len(secs.duties) < 80:
        return UNCLEAR, "the posting text has no duties section", []
    duties = r.incidental.sub(" ", secs.duties) if r.incidental else secs.duties
    tech = [(m, p) for p in r.technical if (m := p.search(duties))]
    rest = r.technical_all.sub(" ", duties) if r.technical_all else duties
    spans = clauses(rest)
    biz, set_aside = [], {}
    for p in r.business:
        for m in p.finditer(rest):
            why = _not_a_duty(rest, spans, m)
            if not why:
                biz.append((m, p))
                break
            set_aside.setdefault(m.group(0).casefold(), why)
    counted = {m.group(0).casefold() for m, _ in biz}
    set_aside = {w: why for w, why in set_aside.items() if w not in counted}
    t, b = len(tech), len(biz)
    tq = list(dict.fromkeys(_quote(duties, m.start(), m.end()) for m, _ in tech))[:2]
    bq = list(dict.fromkeys(_quote(rest, m.start(), m.end()) for m, _ in biz))[:2]
    words = lambda hits: ", ".join(dict.fromkeys(m.group(0).casefold() for m, _ in hits))  # noqa: E731
    aside = "; not counted: " + ", ".join(f"{w} ({why})" for w, why in list(set_aside.items())[:4]) \
        if set_aside else ""
    if t >= int(r.raw.get("technical_min", 2)) and t >= b + int(r.raw.get("technical_margin", 2)):
        return TECHNICAL, f"duties are computing work ({words(tech)})", tq
    enough = int(r.raw.get("business_min", 3))
    tie = b == enough - 1 and r.title_other and r.title_other.search(title)
    if tie and t <= int(r.raw.get("incidental_tech_max", 1)):
        # Two kinds of other work and a title naming another function: the
        # posting itself does not show it clearly, so it is reviewed, never
        # excluded (Airbus INT-001757, BDO INT-002038 and 10 more on 3 October).
        return UNCLEAR, (f"duties name two kinds of non-computing work ({words(biz)}) and the title names "
                         f"{tie.group(0)}; not clear enough from the posting to exclude{aside}"), bq
    if b >= enough and t <= int(r.raw.get("incidental_tech_max", 1)):
        named = r.title_technical.search(title) if r.title_technical else None
        if named:
            return UNCLEAR, (f"the title names {named.group(0)} but the duties read as {words(biz)} work; "
                             f"not excluded on that alone{aside}"), bq
        return NOT_TECHNICAL, f"duties are {words(biz)} work, not computing{aside}", bq
    if aside and not (t or b):
        return UNCLEAR, f"the duties name no kind of work this filter recognises{aside}", []
    if t or b:
        return UNCLEAR, (f"mixed duties: computing ({words(tech) or 'none'}) and other ({words(biz) or 'none'})"
                         f"{aside}"), tq + bq
    return UNCLEAR, "the duties name no kind of work this filter recognises", []


def _not_a_duty(text: str, spans: list[tuple[int, int]], m: re.Match) -> str:
    """Why a non-computing keyword, read in its own clause, is not a duty of
    the role - or "" when it is. Each was a false exclusion on 3 October:
    "commercial modelling tools" and "commercial awareness" (MBDA, Carter
    Jonas), "your analytical, commercial, communication ... skills" (HP),
    "personal brand" (Savills), "a wide portfolio of projects" (MBDA),
    "works with ... teams e.g. Marketing, Procurement" (Mondelez)."""
    ctx = rules().raw.get("business_context") or {}
    word = m.group(0).casefold()
    if word in {str(w).casefold() for w in ctx.get("weak_words") or []}:
        return "too general to count on its own"
    a, b = _clause_at(spans, m.start())
    after, before = text[m.end():b], text[a:m.start()]
    phrases = _any(ctx.get("not_duty_phrases"))
    if phrases and any(p.start() <= m.start() and m.end() <= p.end()
                       for p in phrases.finditer(text, max(0, m.start() - 40), min(len(text), m.end() + 40))):
        return "boilerplate, not a duty"                            # "the recruitment process", "social events"
    plain = re.sub(r"(?i)\b(e\.g|i\.e)\.", "eg", before)
    if re.search(r"(?i)\b(?:degree|degrees|studying|discipline|disciplines|bachelor'?s?|bsc|beng|msc)\b"
                 r"(?:\s+requirements?\s*:)?[^.;:?]{0,160}$", plain):
        return "a degree subject, not a duty"                       # "a degree in ... (e.g., Chemistry, Biology)"

    def list_then(pattern, limit: int, need_comma: bool = False):
        """The keyword is one item of a short list that ends in `pattern`."""
        hit = pattern.search(after[:limit]) if pattern else None
        if not hit:
            return None
        between = after[:hit.start()]
        ok = re.fullmatch(r"[\w\s,&/'-]*", between) and len(between.split()) <= 6
        return hit if ok and (not need_comma or "," in between or "," in before[-40:]) else None

    if s := list_then(_any(ctx.get("skill_words")), 60):
        return f"a {s.group(0).casefold()} word, not a duty"       # "commercial awareness", "... presentation skills"
    if (tool := _any(ctx.get("tool_words"))) and (s := tool.match(after.lstrip(" -"))):
        return f"describes the {s.group(0).casefold()}"             # "commercial modelling tools"
    cues = list(_any(ctx.get("collaborator_cues")).finditer(before)) if ctx.get("collaborator_cues") else []
    if cues and not re.search(r"\bto\s+[a-z]+", before[cues[-1].end():]) and len(before) - cues[-1].end() <= 160:
        return "names who the role works with, not what it does"   # "works with ... e.g. Marketing"
    enumerated = re.search(r"(?i)(?:\(|\be\.g\.?,?|\bsuch as|\bincluding)\s*[\w\s,&/'-]{0,60}$", before)
    if enumerated and list_then(_any(ctx.get("team_nouns")), 100, need_comma=True):
        return "one of a list of colleagues or teams"               # "(e.g. procurement, commercial, ... engineers"
    return ""


_YEARS = re.compile(r"(?i)(?<!over )(?<!more than )\b(\d{1,2})\s*\+?\s*(?:or more )?years?'? (?:of )?"
                    r"(?:[a-z-]+ ){0,4}experience")
#: "If you have already graduated...", "...or have already completed a degree":
#: a graduate is also welcome, which says nothing against a current student.
_CONDITIONAL = re.compile(r"(?i)\b(?:if|whether|or|unless)\b[^.]{0,25}$")
_GRADUATING = re.compile(r"(?i)graduat\w* (?:in |by |between )?(?:(?:summer|spring|autumn|winter|[a-z]+) )?(20\d\d)")


def study_level(title: str, text: str, today: date | None = None) -> tuple[str, str, list[str]]:
    """(value, reason, quotes): who may apply, in the posting's words.

    Firm answers need the posting's own wording; the title alone never settles
    it (an "Industrial Placement" title with no wording is LEVEL_UNCLEAR). An
    integrated master's (MEng, MComp, MSci, MMath) is not assumed to be an
    undergraduate degree: named as the course, it is LEVEL_UNCLEAR.
    """
    r, today = rules(), today or date.today()
    ug = r.ug
    secs = sections(text)
    where = secs.requirements if secs.has_requirements else text
    title_says = ug["not_student_titles"].search(title) if ug["not_student_titles"] else None
    if title_says and ug["student_titles"] and ug["student_titles"].search(title):
        title_says = None
    many = [m for m in _YEARS.finditer(where) if 2 <= int(m.group(1)) <= 15]
    if many:
        m = many[0]
        return EXPERIENCED, f"requires {m.group(1)}+ years of experience", [_quote(where, m.start(), m.end())]
    both = f"{title} {text}"
    bachelor = ug["bachelor_too"].search(both) if ug["bachelor_too"] else None
    in_text = ug["postgraduate"].search(text) if ug["postgraduate"] else None
    in_title = ug["postgraduate_titles"].search(title) if ug["postgraduate_titles"] else None
    if (in_text or in_title) and not bachelor:
        quote = _quote(text, in_text.start(), in_text.end()) if in_text else title
        return POSTGRADUATE_ONLY, "for postgraduate (PhD/Master's) students only", [quote]
    current = ug["current_study"].search(text) if ug["current_study"] else None
    if not current:
        current = next((m for m in _GRADUATING.finditer(text) if int(m.group(1)) >= today.year), None)
    if current:
        quote = _quote(text, current.start(), current.end())
        integrated = ug["integrated_masters"].search(quote) if ug["integrated_masters"] else None
        if integrated and not (ug["bachelor_too"] and ug["bachelor_too"].search(quote)):
            return LEVEL_UNCLEAR, (f"the course named is an integrated master's ({integrated.group(0)}); "
                                   "not assumed to be undergraduate"), [quote]
        return CURRENT_UNDERGRADUATE, "the posting asks for current students", [quote]
    if ug["already_graduated"]:
        for m in ug["already_graduated"].finditer(text):
            if not _CONDITIONAL.search(text[max(0, m.start() - 30):m.start()]):
                return GRADUATE_ONLY, "requires a completed degree", [_quote(text, m.start(), m.end())]
    if title_says:
        # Only the title says graduate or senior; the posting does not. Not an
        # exclusion - CS is only ruled out on the employer's own words.
        return LEVEL_UNCLEAR, f"only the title suggests a non-student role ({title_says.group(0)})", [title]
    if ug["integrated_masters"] and (m := ug["integrated_masters"].search(text)):
        return LEVEL_UNCLEAR, f"names an integrated master's ({m.group(0)}) without saying who may apply", \
            [_quote(text, m.start(), m.end())]
    if ug["programme_titles"] and (m := ug["programme_titles"].search(title)):
        return LEVEL_UNCLEAR, f"only the title suggests an undergraduate programme ({m.group(0)})", [title]
    return NOT_STATED, "the posting does not say who may apply", []


_SENTENCES = re.compile(r"(?<=[.!?;\u2022|])\s+")


def _strength(sentence: str) -> str:
    d = rules().deg
    if d["preferred"] and d["preferred"].search(sentence):
        return "preferred"
    if d["required"] and d["required"].search(sentence):
        return "required"
    return "unclear"


def _studied(sentence: str, start: int, end: int) -> bool:
    """Is the subject at sentence[start:end] the object of a study cue - a few
    words after one ("degree in ... Physics"), or just before one ("Physics
    degree")?"""
    cues = rules().deg["cues"]
    if not cues:
        return False
    before, after = sentence[max(0, start - 90):start], sentence[end:end + 30]
    lead = list(cues.finditer(before))
    if lead and len(before[lead[-1].end():].split()) <= 10 and not re.search(r"[;:]|\.\s", before[lead[-1].end():]):
        return True
    follow = cues.search(after)
    return bool(follow and len(after[:follow.start()].split()) <= 2)


def degree_subject(text: str) -> tuple[str, str, list[str]]:
    """(value, reason, quotes): which degree subjects the employer takes, in
    its exact words. Computer Science only counts when named (or "any
    degree"); AI, Data Science, "or related" and the like are recorded as
    EQUIVALENCE_UNCLEAR, STEM/"engineering"/"technical" as BROAD_FIELD. A
    subject only preferred is never a rejection."""
    d = rules().deg
    best: tuple[int, str, str, list[str]] | None = None
    rank = {ANY_DISCIPLINE: 0, CS_REQUIRED: 1, CS_ACCEPTED: 1, EQUIVALENCE_UNCLEAR: 2, BROAD_FIELD: 3,
            OTHER_SUBJECT_ONLY: 4}

    def offer(value, reason, sentence, m):
        nonlocal best
        item = (rank[value], value, reason, [_quote(sentence, m.start(), m.end())])
        if best is None or item[0] < best[0]:
            best = item

    heads = _headings(text)
    for a, b in clauses(text):
        sentence = text[a:b].strip()
        if not (d["cues"] and d["cues"].search(sentence)):
            continue
        own = _strength(sentence)
        under = next((h for s, e, h in reversed(heads) if e <= a), "")
        # A heading that says so ("Requirements") is recorded as the strength,
        # but an EXCLUSION needs the clause's own words ("must", "only open
        # to"): "Successful candidates will be: Currently pursuing ...
        # Mechanical or Aerospace Engineering" under "Requirements" (Airbus
        # INT-001168/-001889/-001903/-001960) names who is wanted without
        # excluding Computer Science in so many words - REVIEW, never hidden.
        strength = _strength(under) if own == "unclear" and under and _kind_of(under) == "requirements" else own
        if d["any"] and (m := d["any"].search(sentence)):
            offer(ANY_DISCIPLINE, "open to any degree subject", sentence, m)
            continue
        if d["cs_names"] and (m := d["cs_names"].search(sentence)):
            alone = not (d["other_subjects"] and d["other_subjects"].search(d["cs_names"].sub(" ", sentence))) \
                and not (d["equivalence_unclear"] and d["equivalence_unclear"].search(sentence))
            if strength == "required" and alone:
                offer(CS_REQUIRED, f"requires {m.group(0)}", sentence, m)
            else:
                offer(CS_ACCEPTED, f"accepts {m.group(0)} ({strength})", sentence, m)
            continue
        if d["equivalence_unclear"] and (m := d["equivalence_unclear"].search(sentence)):
            offer(EQUIVALENCE_UNCLEAR, f"names {m.group(0)}, not Computer Science ({strength}); "
                                       "equivalence not assumed", sentence, m)
            continue
        broad = d["broad_field"].search(sentence) if d["broad_field"] else None
        if broad:
            # "a 'STEM' subject (science, technology, engineering or mathematics)":
            # subjects named beside a broad field spell it out - they never
            # exclude Computer Science (INT-000372).
            offer(BROAD_FIELD, f"asks only for a broad field ({broad.group(0)}, {strength})", sentence, broad)
            continue
        # The subject must be what the study phrase names ("studying Mechanical
        # Engineering", "a Physics degree"), not a word the clause shares with it.
        other = next((m for m in d["other_subjects"].finditer(sentence) if _studied(sentence, m.start(), m.end())),
                     None) \
            if d["other_subjects"] else None
        if other:
            if own == "required" and not (d["open_ended"] and d["open_ended"].search(sentence)):
                offer(OTHER_SUBJECT_ONLY, f"requires a degree in another subject ({other.group(0)})", sentence, other)
            else:
                how = (f"listed under a '{under}' heading, but the clause does not say only or must"
                       if own != "required" and strength == "required" else strength)
                offer(EQUIVALENCE_UNCLEAR, f"names {other.group(0)} ({how}); Computer Science not named, "
                                           "nor excluded in so many words",
                      sentence, other)
    if best is None:
        return NOT_STATED, "no degree subject stated", []
    return best[1], best[2], best[3]


# ---------------------------------------------------------------------------
# Whose text is it?
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    source: str
    url: str
    company: str
    title: str
    location: str
    description: str


class Listings:
    """Every listing as its own platform returned it, before merging - the
    per-source snapshots each scan writes. It is what shows which listing a
    merged row's description belongs to."""

    def __init__(self, listings: list[Listing]) -> None:
        self.by_url = {(l.source, l.url): l for l in listings if l.url}

    @classmethod
    def load(cls, folder: Path | None = None) -> "Listings":
        folder = folder or config.SNAPSHOT_DIR
        found = []
        for path in sorted(Path(folder).glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for job in payload.get("jobs", []):
                for ref in job.get("sources") or []:
                    found.append(Listing(ref.get("source", ""), ref.get("source_url", ""), job.get("company", ""),
                                         job.get("job_title", ""), job.get("location", ""),
                                         job.get("description") or ""))
        return cls(found)

    def get(self, source: str, url: str) -> Listing | None:
        return self.by_url.get((source, url))


def _uk_places() -> re.Pattern | None:
    places = config.load("sources")["sources"]["careers"].get("uk_places") or []
    return _any(list(places) + list(rules().raw.get("extra_places") or []))


def places(*texts: str) -> set[str]:
    pattern = _uk_places()
    if not pattern:
        return set()
    found = set()
    for text in texts:
        found |= {m.group(0).casefold() for m in pattern.finditer(re.sub(r"[-_/+]", " ", text or ""))}
    return found


def _core(title: str) -> set[str]:
    """A title's words without places or punctuation, for comparing two listings."""
    text = re.sub(r"[^a-z0-9 ]", " ", (title or "").casefold().replace("&", " and "))
    drop = places(title)
    return {w for w in text.split() if w not in drop and w not in {"scheme", "programme", "program", "the"}}


def text_source(job: Job, listings: Listings) -> tuple[str, str, list[str]]:
    """(status, label, flags): is `job.description` this row's own posting?

    Confirmed: it is the text of one of the row's own listings, for the same
    place (or a place neither side names). Equivalent: another place's listing
    of the same programme whose text names this row's place among its
    locations. A row with one source is confirmed by construction. Anything
    else is unconfirmed. Flags name the row's listings for another place,
    which are never used.
    """
    if not job.description:
        return NONE, "no posting text saved", []
    row_places = places(job.job_title) or places(job.location)
    if job.description_source.startswith(PAGE):
        url = job.description_source[len(PAGE):]
        if own_page(job, url):
            return CONFIRMED, f"employer page ({url.split('/')[2] if '//' in url else url})", []
        return UNCONFIRMED, "the page text read is not this row's own posting for this place", []
    own = " ".join(job.description.split())
    matches, flags = [], []
    for ref in job.sources:
        listing = listings.get(ref.source, ref.source_url)
        ref_places = places(listing.title, listing.location) if listing else set()
        ref_places = ref_places or places(ref.source_url)
        if row_places and ref_places and not (row_places & ref_places):
            flags.append(f"{ref.source} listing for {'/'.join(sorted(p.title() for p in ref_places))} "
                          f"- another location, not used")
        same_text = listing is not None and " ".join(listing.description.split()) == own
        # The recorded source only counts where there is no listing text to
        # compare: text that disagrees with its listing is not that listing's.
        recorded = listing is None and job.description_source and ref.source_url == job.description_source
        if same_text or recorded:
            matches.append((ref, listing, ref_places))
    for ref, listing, ref_places in matches:
        if not row_places or not ref_places or row_places & ref_places:
            where = f" ({'/'.join(sorted(p.title() for p in ref_places))})" if ref_places else ""
            return CONFIRMED, f"{ref.source} listing{where}", flags
    for ref, listing, ref_places in matches:
        if listing and normalize.company(listing.company)[1] == job.normalized_company and (
                _core(listing.title) <= _core(job.job_title) or _core(job.job_title) <= _core(listing.title)) \
                and places(job.description) & row_places:
            return (EQUIVALENT, f"{ref.source} listing for {'/'.join(sorted(ref_places)).title()}, "
                                f"same programme; its text lists {'/'.join(sorted(row_places)).title()}", flags)
    if len(job.sources) == 1 and not matches:
        return CONFIRMED, f"{job.sources[0].source} listing (only source)", flags
    return UNCONFIRMED, "the text does not match any of this row's own listings for this place", flags


def own_page(job: Job, url: str) -> bool:
    """Is `url` one of this row's own postings, for the row's own place?"""
    refs = [ref for ref in job.sources if url in (ref.application_url, ref.source_url)]
    if not refs:
        return False
    row_places = places(job.job_title) or places(job.location)
    url_places = places(url)
    return not row_places or not url_places or bool(row_places & url_places)


def needs_text(job: Job) -> bool:
    """Not assessable yet: the posting text is missing, thin or not its own."""
    return any(why in (job.role_relevance_reason or "")
               for why in ("not assessed", "no duties section", "characters of posting text"))


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

def assess(job: Job, listings: Listings, today: date | None = None) -> None:
    """Set the role, study-level and degree findings on `job`, each with its
    reason and quotes. Nothing else is changed."""
    status, label, flags = text_source(job, listings)
    job.assessment_text_source = "; ".join([label] + flags)
    if status in (NONE, UNCONFIRMED):
        why = f"not assessed: {label}"
        job.role_relevance, job.role_relevance_reason, job.role_relevance_evidence = UNCLEAR, why, ""
        job.study_level, job.study_level_reason, job.study_level_evidence = NOT_STATED, why, ""
        job.degree_subject_fit, job.degree_subject_reason, job.degree_subject_evidence = NOT_STATED, why, ""
        return
    text = _clean(job.description)
    for (value, reason, quotes), attr in ((role_relevance(text, job.job_title), "role_relevance"),
                                          (study_level(job.job_title, text, today), "study_level"),
                                          (degree_subject(text), "degree_subject")):
        name = attr if attr != "degree_subject" else "degree_subject_fit"
        setattr(job, name, value)
        setattr(job, f"{attr}_reason", reason)
        setattr(job, f"{attr}_evidence",
                normalize.clean_text(" | ".join(f'"{q}"' for q in quotes[:2]), limit=500))


def assess_all(jobs: list[Job], listings: Listings | None = None, today: date | None = None) -> dict[str, int]:
    """Assess every row; returns the role-relevance counts."""
    listings = listings or Listings.load()
    for job in jobs:
        assess(job, listings, today)
    return {value: sum(1 for j in jobs if j.role_relevance == value) for value in (TECHNICAL, NOT_TECHNICAL, UNCLEAR)}
