"""UK hackathon and build-event tracker: the data side.

Claude finds and checks events (skills/event-researcher/SKILL.md). This module
is the trust boundary for what it found, keeps the lead queue, and works out
everything the sheet shows that depends on today's date.
tracker/events_excel.py draws it; tracker/event_search.py plans the searches.
"""

from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import urlsplit

from rapidfuzz import fuzz

from tracker import config, normalize

# (column label, field key), in sheet order: what, when, can I, area, details.
COLUMNS = [
    ("ID", "id"), ("My Status", "my_status"), ("Event Name", "name"),
    ("Status", "status"), ("Action", "action"),
    ("Registration Deadline", "registration_deadline"),
    ("Days Until Deadline", "days_to_deadline"), ("Start Date", "start_date"),
    ("End Date", "end_date"), ("Days Until Event", "days_to_event"),
    ("Student Eligibility", "student_eligible"),
    ("Undergraduate Eligibility", "undergrad_eligible"),
    ("Technical Area", "technical_area"), ("Event Type", "event_type"),
    ("Format Evidence", "evidence"),
    ("Location", "location"), ("UK Region", "region"), ("Cost", "cost"),
    ("Application Required", "application_required"), ("Team Size", "team_size"),
    ("Individual Participation", "individual"), ("Eligibility", "eligibility"),
    ("Organisation", "organisation"), ("University/Company", "host"),
    ("Prize", "prize"), ("Sponsorship/Companies", "sponsors"),
    ("Recurring", "recurring"), ("Event Description", "description"),
    ("Official Event URL", "official_url"), ("Source URL", "source_url"),
    ("Discovery Method", "discovery_method"),
    ("Last Verified", "last_verified"), ("Notes", "notes"), ("My Notes", "my_notes"),
]
USER_OWNED = {"My Status": "my_status", "My Notes": "my_notes"}
COMPUTED = {"action", "days_to_deadline", "days_to_event"}
DATE_FIELDS = ("start_date", "end_date", "registration_deadline", "last_verified")
_NOT_RESEARCHED = {"id", *USER_OWNED.values(), *COMPUTED}
_YES_NO = ("student_eligible", "undergrad_eligible", "individual",
           "application_required", "recurring")
_URL = re.compile(r"https?://\S+$")
_BLANK = ("", None, "Unclear", "Unknown")
# Wording aimed at an AI reader: reported to the user, never obeyed.
SUSPICIOUS = re.compile(r"ignore (all |any )?(previous|prior|above) instructions"
                        r"|system prompt|you are (an ai|claude|chatgpt)", re.I)


def _cfg() -> dict:
    return config.load("events")


def _pick(value: str, allowed: list[str], fallback: str) -> str:
    return next((a for a in allowed if a.casefold() == value.casefold()), fallback)


def _methods(value: object) -> str:
    allowed = _cfg()["discovery_methods"]
    picked = [_pick(p.strip(), allowed, "Other")
              for p in normalize.clean_text(value).split(",") if p.strip()]
    return ", ".join(dict.fromkeys(picked))


def clean(raw: dict) -> dict | None:
    """Web research is untrusted input, as scraped pages are. Values that do not
    fit become blank or Unclear, because a plausible wrong value is worse than
    a missing one. No URL means it cannot be checked: None, and ingest keeps
    it as a lead instead of a row."""
    cfg = _cfg()
    ev = {k: normalize.clean_text(raw.get(k)) for _, k in COLUMNS
          if k not in _NOT_RESEARCHED and k not in DATE_FIELDS}
    ev["description"] = normalize.clean_text(raw.get("description"), limit=600)
    ev["evidence"] = normalize.clean_text(raw.get("evidence"), limit=300)
    for k in ("official_url", "source_url"):
        if not _URL.match(ev[k]):
            ev[k] = ""
    if not ev["name"] or not (ev["official_url"] or ev["source_url"]):
        return None
    for k in DATE_FIELDS:
        ev[k] = normalize.parse_date(raw.get(k))
    for k in _YES_NO:
        ev[k] = _pick(ev[k], cfg["yes_no_unclear"], "Unclear")
    ev["cost"] = _pick(ev["cost"], cfg["costs"], "Unknown")
    ev["region"] = _pick(ev["region"], cfg["regions"], "Unknown")
    ev["status"] = _pick(ev["status"], cfg["statuses"], "")
    ev["discovery_method"] = _methods(raw.get("discovery_method"))
    flag = normalize.clean_text(raw.get("review_reason"), limit=200)
    ev["review_flags"] = [flag] if flag else []
    ev["conflicts"] = []
    return ev


# ---------------------------------------------------------------------------
# Dedupe and merge
# ---------------------------------------------------------------------------

_CONFLICT_FIELDS = ("end_date", "registration_deadline", "cost",
                    "student_eligible", "undergrad_eligible")


def _host(url: str) -> str:
    return urlsplit(url.lower()).netloc.removeprefix("www.")


def _canonical(url: str) -> str:
    return f"{_host(url)}{urlsplit(url.lower()).path.rstrip('/')}" if url else ""


def _is_aggregator(url: str) -> bool:
    host = _host(url)
    return bool(host) and any(host == h or host.endswith("." + h)
                              for h in _cfg()["aggregator_hosts"])


def same_event(a: dict, b: dict) -> bool:
    """Editions of an annual event stay separate rows, since that history is
    how a recurring event is recognised. Differing start dates therefore end
    the check before names count. One undated side still matches: a TBC row
    that gets its date is the same row.

    90 is the name threshold because "HackSheffield 11" and "HackSheffield 11 -
    24h hackathon" must meet, while two societies' "AI Hackathon" must not
    meet unless the organiser or date also agrees."""
    if a["start_date"] and b["start_date"] and a["start_date"] != b["start_date"]:
        return False
    names = fuzz.token_set_ratio(normalize._fold(a["name"]), normalize._fold(b["name"]))
    if a["official_url"] and _canonical(a["official_url"]) == _canonical(b["official_url"]):
        return names >= 60
    dated = a["start_date"] is not None and a["start_date"] == b["start_date"]
    org = fuzz.token_set_ratio(normalize._fold(a["organisation"]),
                               normalize._fold(b["organisation"])) >= 80
    return names >= 90 and (dated or org)


def merge(old: dict, new: dict) -> None:
    """The newer check wins field by field, but a blank never erases a known
    value. Disagreements go to Review Queue rather than being settled silently.
    The organiser's own page beats a listing platform as the official URL.
    Every discovery route that found the event is kept."""
    # ponytail: only the latest merge's disagreements are kept; a later source
    # that agrees clears them. Add per-source history if that hides real conflicts.
    old["conflicts"] = [f"{k} {old[k]} vs {new[k]}" for k in _CONFLICT_FIELDS
                        if old.get(k) not in _BLANK and new[k] not in _BLANK
                        and old[k] != new[k]]
    newer = (new["last_verified"] or date.min) >= (old["last_verified"] or date.min)
    for k, v in new.items():
        if k in ("official_url", "source_url", "conflicts", "discovery_method",
                 "review_flags") or v in _BLANK:
            continue
        if newer or old.get(k) in _BLANK:
            old[k] = v
    if newer:  # a newer check with no flags means the doubt was resolved
        old["review_flags"] = new["review_flags"]
    methods = [m for m in (*old["discovery_method"].split(", "),
                           *new["discovery_method"].split(", ")) if m]
    old["discovery_method"] = ", ".join(sorted(set(methods)))
    urls = [u for u in (old["official_url"], new["official_url"]) if u]
    official = next((u for u in urls if not _is_aggregator(u)), urls[0] if urls else "")
    others = [u for u in (old["official_url"], new["official_url"],
                          old["source_url"], new["source_url"]) if u and u != official]
    old["official_url"], old["source_url"] = official, (others[0] if others else "")


# ---------------------------------------------------------------------------
# Store and leads
# ---------------------------------------------------------------------------

def _read(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _save(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")


def load_store() -> list[dict]:
    store = _read(config.EVENTS_STORE, [])
    for ev in store:
        for k in DATE_FIELDS:
            ev[k] = date.fromisoformat(ev[k]) if ev.get(k) else None
    return store


def save_store(store: list[dict]) -> None:
    _save(config.EVENTS_STORE, store)


def load_leads() -> list[dict]:
    """Stage 1 of research: events found but with no page to verify yet."""
    return _read(config.EVENTS_LEADS, [])


def _same_lead(a: str, b: str) -> bool:
    # token_sort, not token_set: a generic "AI Challenge" must not swallow
    # "Warwick AI Challenge". A miss here only lets a lead expire.
    return fuzz.token_sort_ratio(normalize._fold(a), normalize._fold(b)) >= 90


def _keep_lead(leads: list[dict], raw: dict, name: str, today: date) -> None:
    lead = next((l for l in leads if _same_lead(l["name"], name)), None)
    if lead is None:
        lead = {"name": name, "first_seen": today.isoformat()}
        leads.append(lead)
    for k, limit in (("organisation", 200), ("notes", 300)):
        lead[k] = normalize.clean_text(raw.get(k), limit=limit) or lead.get(k, "")
    lead["discovery_method"] = (_methods(raw.get("discovery_method"))
                                or lead.get("discovery_method", ""))
    lead["last_tried"] = today.isoformat()


def ingest(candidates: list[dict], today: date | None = None, save: bool = True) -> dict:
    """Merge one research sweep. Running it twice changes nothing. With
    save=False it only counts new against known rows, which is how a sweep
    measures whether a search round is still finding anything."""
    today = today or date.today()
    store, leads = load_store(), load_leads()
    out = {"added": 0, "updated": 0, "leads": 0, "gave_up": [], "flagged": []}
    for raw in candidates:
        if SUSPICIOUS.search(" ".join(str(v) for v in raw.values())):
            out["flagged"].append(normalize.clean_text(raw.get("name"), limit=80) or "?")
        ev = clean(raw)
        if ev is None:
            name = normalize.clean_text(raw.get("name"), limit=120)
            if name:
                _keep_lead(leads, raw, name, today)
                out["leads"] += 1
            continue
        leads = [l for l in leads if not _same_lead(l["name"], ev["name"])]
        # Most recent first, so an undated candidate joins the latest edition.
        ordered = sorted(store, key=lambda e: e["start_date"] or date.max, reverse=True)
        match = next((e for e in ordered if same_event(e, ev)), None)
        if match:
            merge(match, ev)
            out["updated"] += 1
        else:
            ev["id"] = f"EVT-{len(store) + 1:04d}"  # rows are never deleted
            store.append(ev)
            out["added"] += 1
    max_age = _cfg()["lead_max_age_days"]
    out["gave_up"] = [l["name"] for l in leads
                      if (today - date.fromisoformat(l["first_seen"])).days > max_age]
    leads = [l for l in leads if l["name"] not in out["gave_up"]]
    if save:
        save_store(store)
        _save(config.EVENTS_LEADS, leads)
    return out


# ---------------------------------------------------------------------------
# What a row shows today
# ---------------------------------------------------------------------------

_TBC = ("", "Upcoming", "Date TBC", "Recurring / annual - date TBC")
_GROUP = {"Date TBC": 1, "Recurring / annual - date TBC": 1,
          "Registration closed": 2, "Completed": 3}
_BY_EVENT_DATE = {"Registered", "Accepted", "Applied", "Attended", "Not going"}
# Years and edition numbers, but not a number that is part of the name ("24-Hour").
_EDITION = re.compile(r"\b(19|20)\d{2}\b|\b\d+(st|nd|rd|th)?\b(?!-)", re.I)


def effective_status(ev: dict, today: date) -> str:
    """Worked out at every render from the dates, so an event that has ended
    cannot stay listed as open just because nobody re-checked it."""
    end = ev["end_date"] or ev["start_date"]
    if end and end < today:
        return "Completed"
    if ev["registration_deadline"] and ev["registration_deadline"] < today:
        return "Registration closed"
    if not ev["start_date"] and ev["status"] in _TBC:
        return "Recurring / annual - date TBC" if ev["recurring"] == "Yes" else "Date TBC"
    return ev["status"] or "Upcoming"


def _days(d: date | None, today: date) -> int | None:
    return (d - today).days if d and d >= today else None


def next_action(row: dict, today: date) -> str:
    """The one thing to do next, so the sheet answers 'what now?' at a glance."""
    status, mine, dl = row["status"], row.get("my_status") or "", row["registration_deadline"]
    if status == "Completed" or mine in ("Attended", "Not going"):
        return ""
    if mine in ("Registered", "Accepted"):
        return f"Attend {row['start_date'].isoformat()}" if row["start_date"] else "Attend - date TBC"
    if mine == "Applied":
        return "Await decision"
    by = f" by {dl.isoformat()}" if dl and dl >= today else ""
    return {"Registration open": f"Register{by}", "Applications required": f"Apply{by}",
            "Waitlist": f"Join waitlist{by}", "Registration closed": "",
            "Registration not yet open": "Watch for registration",
            "Upcoming": "Check how to register"}.get(status, "Check back for dates")


def display(ev: dict, today: date, mine: dict | None) -> dict:
    """One row as the sheet shows it. Status, action and day counts come from
    today's date on every write and are never stored."""
    row = {**ev, **(mine or {})}
    row["status"] = effective_status(ev, today)
    row["days_to_deadline"] = _days(ev["registration_deadline"], today)
    row["days_to_event"] = _days(ev["start_date"], today)
    row["action"] = next_action(row, today)
    return row


def next_date(row: dict, today: date) -> date:
    """Someone already in cares about the event date; everyone else cares
    about the deadline first."""
    dl = None if row.get("my_status") in _BY_EVENT_DATE else row["registration_deadline"]
    return min((d for d in (dl, row["start_date"]) if d and d >= today), default=date.max)


def sort_key(row: dict, today: date):
    return (_GROUP.get(row["status"], 0), next_date(row, today), row["name"].casefold())


def review_reasons(row: dict) -> list[str]:
    """What still needs a human look. Uncertain events stay in the sheet and
    are listed here, rather than being dropped or given invented values."""
    reasons = []
    if row["student_eligible"] == "Unclear":
        reasons.append("Student eligibility unclear")
    if row["undergrad_eligible"] == "Unclear":
        reasons.append("Undergraduate eligibility unclear")
    if not row["start_date"] and row["recurring"] != "Yes":
        reasons.append("Date unclear")
    if row["region"] == "Unknown":
        reasons.append("UK eligibility unclear")
    if not row["official_url"] or _is_aggregator(row["official_url"]):
        reasons.append("Official source unavailable")
    if not row.get("evidence"):
        reasons.append("Unclear whether participants build/code/solve")
    reasons += [f"Sources disagree: {c}" for c in row.get("conflicts", [])]
    return reasons + [f"Researcher: {f}" for f in row.get("review_flags", [])]


def due_for_check(store: list[dict], today: date) -> list[dict]:
    """Rows the next sweep has to re-open: anything not finished that is
    stale, never checked, or still has something unclear."""
    days = _cfg()["stale_after_days"]
    return [e for e in store if effective_status(e, today) != "Completed"
            and (not e["last_verified"] or (today - e["last_verified"]).days > days
                 or review_reasons(e))]


def series_name(name: str) -> str:
    """The name without edition numbers or years, i.e. what the next edition
    will probably still be called."""
    return " ".join(_EDITION.sub(" ", name).split())


def edition_date(ev: dict) -> date | None:
    return ev["start_date"] or ev["end_date"]


def recurring_followups(store: list[dict], today: date) -> list[dict]:
    """Finished editions with no later one in the store. A past event is a
    discovery signal: the plan searches for its next edition."""
    # ponytail: exact series-name match; fuzzy if renamed successors get missed.
    key = lambda e: series_name(e["name"]).casefold()  # noqa: E731
    latest: dict[str, dict] = {}
    for e in store:
        if effective_status(e, today) == "Completed" and e["recurring"] != "No":
            if key(e) not in latest or edition_date(e) > edition_date(latest[key(e)]):
                latest[key(e)] = e
    return [e for k, e in latest.items()
            if not any(key(o) == k and (edition_date(o) is None or edition_date(o) > edition_date(e))
                       for o in store)]
