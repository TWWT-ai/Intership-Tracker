"""Advance Application Status from recruitment emails, and nothing else.

This is the one narrow exception to "the pipeline never writes a status". A
Claude run reads Gmail (read-only), classifies each email into a stage from its
own explicit wording, and writes data/state/status_evidence.json. This module is
the deterministic half: it matches each item to exactly one workbook row and
applies the rules below. When anything is uncertain it changes nothing and the
email goes to Status Review, because a wrong status is worse than a stale one.

Rules:
* Forward only. A stage is written only when it is later than the cell.
* Offer, Rejected and Withdrawn are terminal: never changed automatically.
* Evidence applies in email-date order; an email older than the evidence behind
  the row's current auto-set stage is ignored.
* A row whose cell no longer matches what the sync last wrote was edited by the
  user, and is locked from automatic changes until explicitly unlocked.
* Every processed message id is remembered, so a second run changes nothing.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from rapidfuzz import fuzz

from tracker import config, normalize
from tracker.models import ApplicationStatus, Job

#: Structural, like dedupe's TITLE_THRESHOLD: an email names a role more loosely
#: than a listing ("Technology Summer Analyst" for "2027 Technology Summer Analyst
#: Program"), and token_set_ratio already scores a subset at 100. Below this the
#: words shared are too few to call it the same role. It is a gate, not the whole
#: test - see _contained().
ROLE_THRESHOLD = 85

#: How long an unresolved review item stays on the Status Review sheet.
REVIEW_DAYS = 30

#: Backups kept of the workbook and ledger, one pair per real write.
BACKUPS_KEPT = 14

#: A lock older than this belongs to a run that died, not to one in progress.
STALE_LOCK = timedelta(hours=2)

_ORDER = [
    ApplicationStatus.FOUND,
    ApplicationStatus.APPLIED,
    ApplicationStatus.OA,
    ApplicationStatus.VIDEO_INTERVIEW,
    ApplicationStatus.SECOND_STAGE,
    ApplicationStatus.ASSESSMENT_CENTRE,
    ApplicationStatus.OFFER,
]
RANK = {status: i for i, status in enumerate(_ORDER)}
TERMINAL = frozenset({ApplicationStatus.OFFER, ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN})
#: Stages an email may establish. Withdrawn is only ever the user's own act.
EMAIL_STAGES = frozenset(set(_ORDER[1:]) | {ApplicationStatus.REJECTED})

_YEAR = re.compile(r"\b20\d\d\b")


def ledger_path() -> Path:
    return config.STATE_DIR / "status_sync.json"


def evidence_path() -> Path:
    return config.STATE_DIR / "status_evidence.json"


def lock_path() -> Path:
    return config.STATE_DIR / ".status_sync.lock"


def backup_dir() -> Path:
    return config.DATA_DIR / "checkpoint" / "backups"


# ---------------------------------------------------------------------------
# Evidence and ledger
# ---------------------------------------------------------------------------


@dataclass
class Evidence:
    company: str
    role_text: str
    stage: str                       # an ApplicationStatus value, or "unclear"
    email_date: date | None
    sender_domain: str
    subject: str
    thread_id: str
    message_id: str
    programme_year: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Evidence":
        raw_date = str(d.get("email_date") or "")[:10]
        try:
            when = date.fromisoformat(raw_date)
        except ValueError:
            when = None
        return cls(company=str(d.get("company") or "").strip(), role_text=str(d.get("role_text") or "").strip(),
                   stage=str(d.get("stage") or "unclear").strip(), email_date=when,
                   sender_domain=str(d.get("sender_domain") or "").strip(),
                   subject=str(d.get("subject") or "").strip(), thread_id=str(d.get("thread_id") or ""),
                   message_id=str(d.get("message_id") or ""),
                   programme_year=str(d.get("programme_year") or "").strip())

    def status(self) -> ApplicationStatus | None:
        """The stage this email establishes, or None when it establishes none."""
        found = ApplicationStatus.parse(self.stage) if self.stage.casefold() != "unclear" else None
        return found if found in EMAIL_STAGES else None

    def as_record(self) -> dict:
        return {"email_date": self.email_date.isoformat() if self.email_date else "",
                "sender_domain": self.sender_domain, "subject": self.subject,
                "thread_id": self.thread_id, "message_id": self.message_id}


def load_evidence(path: Path | None = None) -> dict:
    """The Claude step's output. A missing file is a run with nothing to read."""
    path = path or evidence_path()
    if not path.exists():
        return {"emails_checked": 0, "ignored": {}, "items": []}
    return json.loads(path.read_text(encoding="utf-8"))


def load_ledger(path: Path | None = None) -> dict:
    path = path or ledger_path()
    if not path.exists():
        return {"rows": {}, "processed": {}, "review": {}}
    ledger = json.loads(path.read_text(encoding="utf-8"))
    for key in ("rows", "processed", "review"):
        ledger.setdefault(key, {})
    return ledger


def save_ledger(ledger: dict, path: Path | None = None) -> None:
    path = path or ledger_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def evidence_labels(ledger: dict, statuses: dict[str, ApplicationStatus]) -> dict[str, str]:
    """{job id: Status Evidence text}, rendered from the ledger on every write.

    Rendering rather than storing the text on the row is what lets it survive
    the 01:00 rebuild, and makes it change only when the sync advanced a status.
    """
    labels = {}
    for job_id, entry in (ledger.get("rows") or {}).items():
        if entry.get("locked"):
            labels[job_id] = "locked - status edited by you"
        elif (record := entry.get("evidence") or {}) and                 statuses.get(job_id) is ApplicationStatus.parse(entry.get("status")):
            # Thread and message ids stay in the JSON; the cell shows what a person reads.
            labels[job_id] = normalize.clean_text(
                f"{record.get('email_date') or 'undated'} {record.get('sender_domain', '')} - "
                f"{record.get('subject', '')}", limit=200)
    return labels


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    evidence: Evidence
    kind: str                   # changed | up_to_date | stale | terminal | locked | processed | review | no_match
    job_id: str = ""
    company: str = ""
    role: str = ""
    before: ApplicationStatus | None = None
    after: ApplicationStatus | None = None
    reason: str = ""
    candidates: list[str] = field(default_factory=list)


def _years(text: str) -> set[str]:
    return set(_YEAR.findall(text or ""))


def match(item: Evidence, jobs: list[Job], statuses: dict[str, ApplicationStatus]) -> tuple[Job | None, str, list[Job]]:
    """(the one row this email is about, why not, the rows it could have been).

    Company through the alias table. Role through the same token_set_ratio the
    dedupe uses, plus one containment check: every word of the listing's title
    is in the email, or every word of the email's role is in the listing.
    token_set alone scores shared filler ("2027 Summer Intern ... EMEA") high
    enough to put a Technology Analyst email on a Product Analyst row. Two rows
    left standing is a question for the user, not a pick.
    """
    if not item.company:
        return None, "email names no company", []
    wanted = normalize.company(item.company)[1]
    same_company = [j for j in jobs if j.normalized_company == wanted]
    if not same_company:
        return None, f"no application at {item.company} in the tracker", []
    if not item.role_text:
        return None, "email does not name the role", same_company

    # The email's own words, not the normalized title: normalizing strips
    # "Summer Internship" and "Placement", which can be the only difference
    # between two roles ("Software engineer, summer intern" is not "Software
    # engineer, industry placement"). token_set still lets the email carry
    # extra words ("- Glasgow", "(Summer 2027)") around the listing's title.
    words = _canon(item.role_text)
    said = _content(item.role_text)
    roles = [j for j in same_company
             if fuzz.token_set_ratio(words, _canon(j.job_title)) >= ROLE_THRESHOLD
             and _contained(said, _content(j.job_title))]
    if not roles:
        return None, f"no {item.company} role in the tracker matches '{item.role_text}'", same_company

    if item.programme_year:
        # A 2026 rejection must never land on the 2027 round of the programme.
        fitting = [j for j in roles if not _years(j.job_title) or item.programme_year in _years(j.job_title)]
        if not fitting:
            return None, f"email is about the {item.programme_year} round; tracker rows are other years", roles
        roles = fitting

    in_cycle = [j for j in roles if _same_cycle(j, item)]
    if not in_cycle:
        return None, "every matching row belongs to an earlier recruitment round", roles
    roles = in_cycle

    if len(roles) == 1:
        return roles[0], "", roles
    return _one_of(item, roles, statuses)


#: Spellings that name the same thing in a title.
_SAME_WORD = {"programme": "program", "programmes": "program", "programs": "program",
              "internship": "intern", "internships": "intern", "interns": "intern",
              "placements": "placement"}

#: Words that carry no role: an email and a listing may differ in these freely.
_FILLER = frozenset({"and", "of", "the", "for", "in", "a", "an", "to", "with", "at", "on", "uk",
                     "united", "kingdom", "europe", "emea", "role", "position", "opportunity"})


def _content(text: str) -> set[str]:
    """The words that name the role: canonical, without filler or years (the
    programme year is checked on its own)."""
    return {w for w in _canon(text).split() if w not in _FILLER and not _YEAR.fullmatch(w)}


def _contained(email: set[str], listing: set[str]) -> bool:
    """One names the other in full. "Software engineer, summer intern" contains
    "Software Engineer, Intern" but not "Software engineer, industry placement"."""
    return bool(email) and bool(listing) and (listing <= email or email <= listing)


def _places() -> re.Pattern:
    """The UK places the careers filter knows, as one whole-word pattern."""
    names = config.load("sources")["sources"]["careers"].get("uk_places") or []
    if not names:
        return re.compile(r"(?!x)x")                     # matches nothing
    return re.compile(r"\b(?:" + "|".join(re.escape(n.casefold()) for n in names) + r")\b")


def _canon(text: str) -> str:
    """A title as comparable words: lowercase, one spelling per word, places
    removed (the place is compared on its own, against the row's location)."""
    plain = _places().sub(" ", (text or "").casefold())
    return " ".join(_SAME_WORD.get(w, w) for w in re.findall(r"[a-z0-9]+", plain))


def _same_cycle(job: Job, item: Evidence) -> bool:
    """Whether a row can be the round this email is about.

    Nobody applies after a listing's deadline or before it was posted, so a
    confirmation cannot be about a round that had closed. A later stage can
    arrive after the deadline, but not a whole cycle after it: a deadline more
    than six months before the email is last year's round of the programme.
    """
    when = item.email_date
    if when is None:
        return True
    if job.date_posted and job.date_posted > when:
        return False
    if job.deadline:
        if item.status() is ApplicationStatus.APPLIED:
            return job.deadline >= when
        return job.deadline >= when - timedelta(days=183)
    return True


def _one_of(item: Evidence, roles: list[Job], statuses: dict) -> tuple[Job | None, str, list[Job]]:
    """Several rows fit; pick one only on unambiguous evidence, else review.

    In order: the one row in the place the email names (a Manchester email and
    one Manchester listing); the one row the email names exactly (places and
    spelling aside); the one row the user has already marked as an application
    - identical listings of a role are one programme seen twice, and the marked
    one is theirs. Anything else is two plausible rows: a question, not a pick.
    """
    named = set(_places().findall(item.role_text.casefold()))
    if named:
        there = [j for j in roles if named & set(_places().findall(f"{j.job_title} {j.location}".casefold()))]
        if len(there) == 1:
            return there[0], "", roles

    wanted = _canon(item.role_text)
    exact = [j for j in roles if _canon(j.job_title) == wanted]
    if len(exact) == 1:
        return exact[0], "", roles

    marked = [j for j in roles if statuses.get(j.id, ApplicationStatus.FOUND) is not ApplicationStatus.FOUND]
    if len(marked) == 1:
        return marked[0], "", roles
    return None, f"could match {len(roles)} applications", roles


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def decide(evidence: dict, jobs: list[Job], statuses: dict[str, ApplicationStatus],
           ledger: dict) -> tuple[list[Outcome], dict[str, ApplicationStatus]]:
    """Every item's outcome, and {job id: new status} for the writes.

    `statuses` is the workbook as the user left it. The ledger is updated in
    place; the caller saves it only once the workbook write has succeeded.
    """
    rows, processed, review = ledger["rows"], ledger["processed"], ledger["review"]
    now = datetime.now().isoformat(timespec="seconds")
    outcomes: list[Outcome] = []

    # A cell that no longer says what the sync last wrote was changed by hand.
    for job_id, entry in rows.items():
        if entry.get("locked"):
            continue
        current = statuses.get(job_id)
        if current is not None and current is not ApplicationStatus.parse(entry.get("status")):
            entry["locked"], entry["locked_at"] = True, now

    items = [Evidence.from_dict(d) for d in evidence.get("items") or []]
    items.sort(key=lambda e: (e.email_date or date.min, e.message_id))
    live = dict(statuses)
    updates: dict[str, ApplicationStatus] = {}

    for item in items:
        if item.message_id and item.message_id in processed:
            outcomes.append(Outcome(item, "processed"))
            continue
        stage = item.status()
        job, why, candidates = match(item, jobs, live)
        names = [f"{j.company} - {j.job_title}" for j in candidates]

        if stage is None:
            outcomes.append(Outcome(item, "review", job_id=job.id if job else "",
                                    reason="stage not explicit in the email", candidates=names))
        elif job is None:
            kind = "no_match" if not candidates else "review"
            outcomes.append(Outcome(item, kind, reason=why, candidates=names))
        else:
            current = live.get(job.id, ApplicationStatus.FOUND)
            entry = rows.get(job.id, {})
            base = Outcome(item, "", job_id=job.id, company=job.company, role=job.job_title, before=current)
            auto_date = str((entry.get("evidence") or {}).get("email_date") or "")
            if current is None:
                # The cell holds text ApplicationStatus.parse() does not know. It is
                # the user's, and nothing the sync writes may replace it.
                base.kind, base.reason = "locked", "status cell holds text the sync does not recognise"
            elif entry.get("locked"):
                base.kind, base.reason = "locked", "you changed this status by hand"
            elif current in TERMINAL:
                base.kind, base.reason = "terminal", f"{current} is final"
            elif (entry and item.email_date and auto_date
                  and item.email_date.isoformat() < auto_date):
                base.kind, base.reason = "stale", "older than the email behind the current stage"
            elif stage is ApplicationStatus.REJECTED or RANK[stage] > RANK.get(current, 0):
                base.kind, base.after = "changed", stage
                live[job.id] = updates[job.id] = stage
                rows[job.id] = {"status": str(stage), "evidence": item.as_record(), "set_at": now,
                                "locked": False}
            else:
                base.kind, base.reason = "up_to_date", f"already at {current}"
            outcomes.append(base)

        if outcomes[-1].kind in ("review", "no_match"):
            review[item.message_id or f"{item.subject}|{item.email_date}"] = {
                **item.as_record(), "detected_stage": item.stage, "company": item.company,
                "possible_roles": "; ".join(names[:5]) or item.role_text,
                "reason": outcomes[-1].reason, "first_seen": review.get(
                    item.message_id, {}).get("first_seen", now)}
        else:
            # Settled one way or another: never reconsidered, and off the review list.
            if item.message_id:
                processed[item.message_id] = {"at": now, "outcome": outcomes[-1].kind,
                                              "job_id": outcomes[-1].job_id}
                review.pop(item.message_id, None)

    cutoff = (datetime.now() - timedelta(days=REVIEW_DAYS)).isoformat()
    for key in [k for k, v in review.items() if str(v.get("first_seen", "")) < cutoff]:
        del review[key]
    return outcomes, updates


def unlock(ledger: dict, job_id: str, current: ApplicationStatus | None) -> bool:
    """Hand a locked row back to the sync, starting from the cell as it is now."""
    entry = ledger["rows"].get(job_id)
    if not entry:
        return False
    entry.update({"locked": False, "locked_at": None, "unlocked_at": datetime.now().isoformat(timespec="seconds"),
                  "status": str(current or ApplicationStatus.FOUND), "evidence": {}})
    return True


# ---------------------------------------------------------------------------
# Safety around a real write
# ---------------------------------------------------------------------------


class Busy(Exception):
    """The workbook or the sync is in use; nothing was written."""


def workbook_open() -> bool:
    return (config.WORKBOOK.parent / f"~${config.WORKBOOK.name}").exists()


def acquire_lock() -> None:
    lock = lock_path()
    if lock.exists():
        age = datetime.now() - datetime.fromtimestamp(lock.stat().st_mtime)
        if age < STALE_LOCK:
            raise Busy("another status sync is running")
        lock.unlink()
    try:
        with lock.open("x", encoding="utf-8") as fh:
            fh.write(datetime.now().isoformat())
    except FileExistsError as exc:
        raise Busy("another status sync is running") from exc


def release_lock() -> None:
    lock_path().unlink(missing_ok=True)


def backup() -> Path:
    """Copy the workbook and ledger aside before a real write. Raises on failure,
    and the caller writes nothing if it does."""
    folder = backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = folder / f"status-sync-{stamp}.xlsx"
    shutil.copy2(config.WORKBOOK, target)
    if ledger_path().exists():
        shutil.copy2(ledger_path(), folder / f"status-sync-{stamp}.ledger.json")
    for old in sorted(folder.glob("status-sync-*.xlsx"))[:-BACKUPS_KEPT]:
        old.unlink(missing_ok=True)
        old.with_suffix(".ledger.json").unlink(missing_ok=True)
    return target


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def summary(evidence: dict, outcomes: list[Outcome], when: datetime | None = None, dry_run: bool = False) -> str:
    """The nightly report, in the words the user reads on their phone."""
    when = when or datetime.now()
    count = lambda *kinds: sum(1 for o in outcomes if o.kind in kinds)  # noqa: E731
    title = f"Application Status Sync - {when:%H:%M}" + (" (DRY RUN - nothing written)" if dry_run else "")
    lines = [title, "",
             f"Emails checked: {evidence.get('emails_checked', len(outcomes))}",
             f"Applications updated: {count('changed')}",
             f"Already up to date: {count('up_to_date', 'stale', 'terminal', 'processed')}",
             f"No matching application: {count('no_match')}",
             f"Needs review: {count('review')}"]
    if count("locked"):
        lines.append(f"Locked (you edited the status): {count('locked')}")
    ignored = evidence.get("ignored") or {}
    if ignored:
        lines.append("Ignored (no status evidence): " + ", ".join(f"{k} {v}" for k, v in sorted(ignored.items())))

    changes = [o for o in outcomes if o.kind == "changed"]
    lines.append("")
    if changes:
        lines.append("Changes:" if not dry_run else "Proposed changes:")
        for o in changes:
            before = "Not applied" if o.before in (None, ApplicationStatus.FOUND) else str(o.before)
            lines += [f"  {o.company} - {o.role}:", f"    {before} -> {o.after}"]
    else:
        lines.append("No status changes.")

    reviews = [o for o in outcomes if o.kind in ("review", "no_match")]
    if reviews:
        lines += ["", "Review:"]
        for o in reviews:
            who = o.evidence.company or o.evidence.sender_domain or "unknown sender"
            lines.append(f"  {who} - {o.evidence.stage} email: {o.reason}")
    return "\n".join(lines)


def review_rows(ledger: dict) -> list[dict]:
    """The Status Review sheet: every recruitment email the sync could not settle."""
    return sorted(ledger.get("review", {}).values(), key=lambda r: str(r.get("email_date", "")), reverse=True)


# ---------------------------------------------------------------------------
# One run, end to end
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    text: str
    outcomes: list[Outcome]
    wrote: bool = False
    skipped: str = ""           # why nothing could be written, when that happened


def load_jobs() -> list[Job]:
    """The rows the workbook was built from, with the ids the statuses sit on."""
    path = config.STATE_DIR / "last_run.json"
    if not path.exists():
        return []
    return [Job.from_dict(d) for d in json.loads(path.read_text(encoding="utf-8")).get("jobs", [])]


def workbook_statuses() -> dict[str, ApplicationStatus | None]:
    """{id: status} exactly as the user left each cell. None is text that
    ApplicationStatus.parse() does not recognise - the user's, and untouchable."""
    from tracker import excel

    return {job_id: ApplicationStatus.parse(cells.get(excel.STATUS_COLUMN))
            for job_id, cells in excel.read_user_state().items() if excel.STATUS_COLUMN in cells}


def _skipped(reason: str, when: datetime) -> RunResult:
    text = (f"Application Status Sync - {when:%H:%M}\n\nskipped - {reason}\n"
            "Nothing was written. The next run's 3-day window catches up.")
    return RunResult(text, [], skipped=reason)


def _log(text: str, when: datetime) -> None:
    config.ensure_dirs()
    with (config.LOG_DIR / f"status-sync-{when:%Y-%m-%d}.txt").open("a", encoding="utf-8") as fh:
        fh.write(text + "\n\n")


def run(evidence_file: Path | None = None, dry_run: bool = False, when: datetime | None = None) -> RunResult:
    """Apply the Claude step's evidence to the workbook, or show what would change.

    A real run writes only when every guard passes: Excel closed, no other sync
    running, a backup taken. The ledger is saved only after the workbook write
    succeeded - a ledger ahead of the sheet would read as a manual edit and lock
    the row.
    """
    from tracker import excel

    when = when or datetime.now()
    evidence = load_evidence(evidence_file)
    jobs, statuses = load_jobs(), workbook_statuses()

    if dry_run:
        outcomes, _ = decide(evidence, jobs, statuses, load_ledger())   # a copy; never saved
        return RunResult(summary(evidence, outcomes, when, dry_run=True), outcomes)

    if workbook_open():
        result = _skipped("workbook busy", when)
        _log(result.text, when)
        return result
    try:
        acquire_lock()
    except Busy:
        result = _skipped("workbook busy", when)
        _log(result.text, when)
        return result

    try:
        ledger = load_ledger()
        visible = lambda: json.dumps({"rows": ledger["rows"], "review": ledger["review"]}, sort_keys=True)  # noqa: E731
        before = visible()
        outcomes, updates = decide(evidence, jobs, statuses, ledger)

        wrote = False
        if updates or visible() != before:
            try:
                backup()
            except OSError as exc:
                result = _skipped(f"backup failed ({exc})", when)
                _log(result.text, when)
                return result
            written = excel.write(jobs, status_updates=updates, status_ledger=ledger,
                                  status_review=review_rows(ledger))
            if written != config.WORKBOOK:
                written.unlink(missing_ok=True)
                result = _skipped("workbook busy", when)
                _log(result.text, when)
                return result
            wrote = True
        save_ledger(ledger)
        result = RunResult(summary(evidence, outcomes, when), outcomes, wrote=wrote)
        _log(result.text, when)
        return result
    finally:
        release_lock()


def unlock_row(job_id: str) -> str:
    """Return a row you edited to the sync's care. The sync starts again from the
    status the cell holds now; nothing is rewritten."""
    if workbook_open():
        return "skipped - workbook busy"
    acquire_lock()
    try:
        ledger = load_ledger()
        if not unlock(ledger, job_id, workbook_statuses().get(job_id)):
            return f"{job_id} was never set by the status sync; nothing to unlock."
        save_ledger(ledger)
        return f"{job_id} unlocked. The sync will move it forward again on new email evidence."
    finally:
        release_lock()
