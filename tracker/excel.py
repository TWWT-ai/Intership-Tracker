"""The Excel dashboard: read-modify-write, never rebuild.

Two rules this module exists to enforce:

1. **User-owned cells are never overwritten.** Application Status and Notes
   belong to the user. A daily sync that re-finds a job on another platform
   must leave "Application Submitted" as "Application Submitted".
2. **One row per real job**, with the platform it came from colour-coded so the
   sheet is scannable: Trackr blue, Gradcracker orange, Bright Network green.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from tracker import config, normalize, status_sync
from tracker.models import (
    USER_OWNED_STATUSES,
    ApplicationStatus,
    Job,
    OpenStatus,
    Priority,
)

log = logging.getLogger(__name__)

SHEET_JOBS = "Internships"
SHEET_ELIGIBILITY = "Eligibility"
SHEET_DEADLINES = "Deadlines"
SHEET_RUNS = "Runs"
SHEET_GUIDE = "Guide"
SHEET_LISTS = "Lists"
SHEET_STATUS_REVIEW = "Status Review"

#: Every column the sheet can show, as (label, attr). Which ones are actually
#: written, and in what order, is the user's choice in config/workbook.yaml.
#: An attr of the form "field=value" renders "Yes" when job.field == value.
COLUMNS: list[tuple[str, str]] = [
    ("ID", "id"),
    ("Priority", "priority"),
    ("Match Score", "match_score"),
    ("Match Category", "match_category"),
    ("Company", "company"),
    ("Job Title", "job_title"),
    ("Date Posted", "date_posted"),
    ("Deadline", "deadline"),
    ("Open Status", "open_status"),
    ("Primary Source", "primary_source"),
    ("Sources", "_sources"),
    ("Application URL", "application_url"),
    ("Location", "location"),
    ("Work Type", "work_type"),
    ("Summer Internship", "work_type=Summer Internship"),
    ("Industrial Placement", "work_type=Industrial Placement"),
    ("Spring Week", "work_type=Spring Week"),
    ("Expected Graduation", "expected_graduation"),
    ("Eligible for You", "eligible_for_you"),
    ("Visa Sponsorship", "visa_sponsorship"),
    ("Work Arrangement", "work_arrangement"),
    ("Duration", "duration"),
    ("Start Date", "start_date"),
    ("Salary", "salary"),
    ("Degree Requirements", "degree_requirements"),
    ("Year of Study", "year_of_study"),
    ("Experience Requirements", "experience_requirements"),
    ("Skills", "_skills"),
    ("Programming Languages", "_programming_languages"),
    ("Technologies", "_technologies"),
    ("Strengths", "_strengths"),
    ("Missing Skills", "_missing_skills"),
    ("Concerns", "_concerns"),
    ("Listed Deadline", "listed_deadline"),
    ("Deadline Source", "deadline_source"),
    ("Last Verified", "last_verified"),
    ("Source URL", "source_url"),
    ("Date Found", "date_found"),
    ("Last Updated", "last_updated"),
    ("CV", "cv"),
    ("WA", "written_answers"),
    ("CL", "cover_letter"),
    ("Application Status", "status"),
    ("Status Evidence", "status_evidence"),
    ("Notes", "notes"),
]

_CATALOGUE = dict(COLUMNS)

#: Where the user records how far an application got. Workbooks written before
#: it replaced the separate Applications sheet call it "Status".
STATUS_COLUMN = "Application Status"
_LEGACY_STATUS_COLUMN = "Status"

#: Cells the pipeline writes once on creation and never again.
USER_OWNED_COLUMNS = (STATUS_COLUMN, "Notes")

#: The dropdown offers every stage; leaving the cell blank means not applied.
DROPDOWN_STATUSES = [s for s in ApplicationStatus if s is not ApplicationStatus.FOUND]

_HEADER_FILL = PatternFill("solid", fgColor="1F3864")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_THIN = Side(style="thin", color="D9D9D9")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

_PRIORITY_FILLS = {
    Priority.HIGH: PatternFill("solid", fgColor="F8CBAD"),
    Priority.MEDIUM: PatternFill("solid", fgColor="FFE699"),
    Priority.LOW: PatternFill("solid", fgColor="E2EFDA"),
}

_OPEN_FILLS = {
    OpenStatus.OPEN: PatternFill("solid", fgColor="C6EFCE"),
    OpenStatus.CLOSED: PatternFill("solid", fgColor="D9D9D9"),
    OpenStatus.UNKNOWN: PatternFill("solid", fgColor="FFEB9C"),
}


def _layout() -> dict:
    """config/workbook.yaml, or {} so a missing file means the full catalogue."""
    try:
        return config.load("workbook")
    except FileNotFoundError:
        return {}


def columns() -> list[tuple[str, str]]:
    """The visible (label, attr) pairs, in the user's order.

    The layout lives in config rather than in the sheet because the sheet is
    rebuilt every run: a column deleted only in Excel would come straight back.
    """
    wanted = _layout().get("columns") or [label for label, _ in COLUMNS]
    unknown = [label for label in wanted if label not in _CATALOGUE]
    if unknown:
        log.warning("config/workbook.yaml names unknown columns, ignored: %s", ", ".join(unknown))

    labels = list(dict.fromkeys(label for label in wanted if label in _CATALOGUE))
    # User state is matched back to rows by ID and lives in Application Status,
    # so a layout without either would silently lose every status set.
    if "ID" not in labels:
        labels.insert(0, "ID")
    if STATUS_COLUMN not in labels:
        at = labels.index("Notes") if "Notes" in labels else len(labels)
        labels.insert(at, STATUS_COLUMN)
    return [(label, _CATALOGUE[label]) for label in labels]


def col_index() -> dict[str, int]:
    """{label: 1-based column number} for the current layout."""
    return {label: i + 1 for i, (label, _) in enumerate(columns())}


def _flag_styles() -> dict[str, dict[str, str]]:
    """Colours for the work-type and CV / WA / CL columns."""
    return _layout().get("flag_columns") or {}


def _source_styles() -> dict[str, dict[str, str]]:
    """{'trackr': {'colour': '2F6FBF', 'fill': 'DCE9F8', 'name': 'Trackr'}, ...}"""
    sources = config.load("sources").get("sources", {})
    return {
        key: {
            "colour": spec.get("colour", "444444"),
            "fill": spec.get("fill", "EEEEEE"),
            "name": spec.get("display_name", key.title()),
        }
        for key, spec in sources.items()
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def write(
    jobs: list[Job],
    path: Path | None = None,
    run_summary: dict | None = None,
    status_updates: dict[str, ApplicationStatus] | None = None,
    status_ledger: dict | None = None,
    status_review: list[dict] | None = None,
) -> Path:
    """Update the workbook in place, preserving everything the user owns.

    `status_updates` is the one sanctioned way a status reaches the sheet
    without the user typing it: the email status sync, which has already
    applied its forward-only, evidence-only rules. It is applied after the
    user's cells are restored, so it can only ever be those decisions. Without
    it, statuses are exactly as the user left them.

    Returns the path actually written, which differs from `path` if the
    workbook was open in Excel.
    """
    path = path or config.WORKBOOK
    config.ensure_dirs()

    if path.exists():
        wb = load_workbook(path)
        existing = read_user_state(path)
    else:
        wb = Workbook()
        wb.remove(wb.active)
        existing = {}

    _apply_user_state(jobs, existing)

    if status_updates:
        for job in jobs:
            if job.id in status_updates:
                job.status = status_updates[job.id]

    # Rendered from the ledger each write, so the 01:00 rebuild keeps it.
    ledger = status_ledger if status_ledger is not None else status_sync.load_ledger()
    labels = status_sync.evidence_labels(ledger, {j.id: j.status for j in jobs})
    for job in jobs:
        job.status_evidence = labels.get(job.id, "")

    # Priority depends on application status, and the status only becomes known
    # once the user's edits are read back. Recompute so a submitted application
    # cannot keep sitting at the top of the sheet as HIGH.
    from tracker import match as match_mod

    weights = config.load("matching_weights")
    for job in jobs:
        match_mod.assign_priority(job, weights)

    jobs = sort_jobs(jobs)

    # Applications are tracked in the Application Status column. A separate
    # Applications sheet in an older workbook is the user's and is left alone.
    _write_jobs_sheet(wb, jobs)
    _write_eligibility_sheet(wb, jobs)
    _write_deadlines_sheet(wb, jobs)
    _write_runs_sheet(wb, run_summary)
    if status_review is not None:
        _write_status_review_sheet(wb, status_review)
    _write_guide_sheet(wb)
    _write_lists_sheet(wb)
    wb.active = wb[SHEET_JOBS]

    return _save(wb, path)


def _save(wb: Workbook, path: Path) -> Path:
    """Save, or divert if the user has the workbook open in Excel.

    Excel holds an exclusive lock on the file. Losing a whole run's work to
    that is avoidable, so the data goes to a sibling file instead.
    """
    try:
        wb.save(path)
        return path
    except PermissionError:
        fallback = path.with_name(path.stem + ".PENDING.xlsx")
        wb.save(fallback)
        return fallback


def read_user_state(path: Path | None = None) -> dict[str, dict]:
    """Read back the cells the user owns, keyed by internship ID.

    This runs before any write so that a status the user typed survives the
    sync that re-finds the job on a different platform.
    """
    path = path or config.WORKBOOK
    if not path.exists():
        return {}

    wb = load_workbook(path, read_only=True, data_only=True)
    if SHEET_JOBS not in wb.sheetnames:
        wb.close()
        return {}

    ws = wb[SHEET_JOBS]
    rows = ws.iter_rows(values_only=True)
    try:
        header = next(rows)
    except StopIteration:
        wb.close()
        return {}

    index = {name: i for i, name in enumerate(header) if name}
    if STATUS_COLUMN not in index and _LEGACY_STATUS_COLUMN in index:
        index[STATUS_COLUMN] = index[_LEGACY_STATUS_COLUMN]
    state: dict[str, dict] = {}

    for row in rows:
        if not row or "ID" not in index:
            continue
        job_id = row[index["ID"]]
        if not job_id:
            continue
        state[str(job_id)] = {
            col: row[index[col]]
            for col in USER_OWNED_COLUMNS
            if col in index and index[col] < len(row)
        }
        for col in ("Date Found",):
            if col in index and index[col] < len(row):
                state[str(job_id)][col] = row[index[col]]

    wb.close()
    return state


def _apply_user_state(jobs: list[Job], existing: dict[str, dict]) -> None:
    """Restore user-owned values onto freshly-scraped jobs.

    The scraped `Job` always arrives with status FOUND. If the workbook says
    otherwise, the workbook wins — that value came from the user.
    """
    for job in jobs:
        saved = existing.get(job.id)
        if not saved:
            continue

        # A blank cell is a status too: "not applied" is written blank, so a
        # user clearing the cell has reset it. None means unrecognised text,
        # which is never guessed into a stage.
        if STATUS_COLUMN in saved:
            status = ApplicationStatus.parse(saved[STATUS_COLUMN])
            if status is not None:
                job.status = status

        if saved.get("Notes"):
            job.notes = str(saved["Notes"])

        found = saved.get("Date Found")
        if isinstance(found, dt.datetime):
            job.date_found = found.date()
        elif isinstance(found, dt.date):
            job.date_found = found


def sort_jobs(jobs: list[Job]) -> list[Job]:
    """HIGH first, then soonest deadline.

    Dead roles - closed, or past their deadline - sink to the bottom so they
    stay available as history without cluttering the top. Sorting only on
    priority and deadline would do the opposite: an expired job has the
    earliest deadline of all and would lead the sheet.
    """
    order = {Priority.HIGH: 0, Priority.MEDIUM: 1, Priority.LOW: 2}
    far_future = dt.date(2099, 12, 31)
    today = dt.date.today()

    def is_dead(job: Job) -> bool:
        return job.open_status is OpenStatus.CLOSED or bool(
            job.deadline and job.deadline < today
        )

    return sorted(
        jobs,
        key=lambda j: (
            is_dead(j),
            order.get(j.priority, 3),
            j.deadline or far_future,
            -(j.match_score or 0),
            j.company.casefold(),
        ),
    )


def _cell_value(job: Job, attr: str) -> object:
    """Render one field for the sheet, sanitised against formula injection."""
    if attr == "_sources":
        # Every place the job was seen, naming the careers platform too, so the
        # user can tell "Company site (Workday)" from "Company site (Lever)".
        styles = _source_styles()
        labels = []
        for ref in job.sources:
            label = styles.get(ref.source, {}).get("name", ref.source)
            labels.append(f"{label} ({ref.via})" if ref.via else label)
        return ", ".join(dict.fromkeys(labels))
    if attr.startswith("_"):
        value = getattr(job, attr[1:], [])
        return ", ".join(value) if isinstance(value, list) else value
    if "=" in attr:
        field_name, wanted = attr.split("=", 1)
        return "Yes" if getattr(job, field_name, "") == wanted else None
    if attr == "status":
        # Blank reads as "not applied" and keeps the column quiet until used.
        return None if job.status is ApplicationStatus.FOUND else str(job.status)

    value = getattr(job, attr, "")
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None, microsecond=0)
    if isinstance(value, dt.date) or value is None or isinstance(value, (int, float)):
        return value
    return normalize.excel_safe(str(value))


def _write_jobs_sheet(wb: Workbook, jobs: list[Job]) -> None:
    if SHEET_JOBS in wb.sheetnames:
        del wb[SHEET_JOBS]
    ws = wb.create_sheet(SHEET_JOBS, 0)

    cols = columns()
    idx = {label: i + 1 for i, (label, _) in enumerate(cols)}
    flags = _flag_styles()

    ws.append([label for label, _ in cols])
    for cell in ws[1]:
        spec = flags.get(cell.value)
        cell.fill = PatternFill("solid", fgColor=spec["header"]) if spec else _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True,
                                   horizontal="center" if spec else None)
    ws.row_dimensions[1].height = 30

    styles = _source_styles()

    for job in jobs:
        ws.append([_cell_value(job, attr) for _, attr in cols])
        _style_row(ws, ws.max_row, job, styles, idx, flags)

    _finish_jobs_sheet(ws, len(jobs), idx)


#: Whether the user can apply, at a glance: green yes, amber check, grey no.
_ELIGIBILITY_STYLE = {"Yes": ("E2EFDA", "375623"), "REQUIRES VERIFICATION": ("FFF2CC", "7F6000"),
                      "No": (None, "808080")}


def _style_row(ws, row: int, job: Job, styles: dict, idx: dict[str, int], flags: dict) -> None:
    """Style one row. Columns the layout leaves out are simply skipped."""
    closed = job.open_status is OpenStatus.CLOSED

    def at(label: str):
        return ws.cell(row=row, column=idx[label]) if label in idx else None

    for col in range(1, len(idx) + 1):
        ws.cell(row=row, column=col).border = _BORDER

    if priority_cell := at("Priority"):
        priority_cell.fill = _PRIORITY_FILLS.get(job.priority, _PRIORITY_FILLS[Priority.LOW])
        priority_cell.font = Font(bold=True)
        priority_cell.alignment = Alignment(horizontal="center")

    if open_cell := at("Open Status"):
        open_cell.fill = _OPEN_FILLS.get(job.open_status, _OPEN_FILLS[OpenStatus.UNKNOWN])
        open_cell.alignment = Alignment(horizontal="center")
        if closed:
            open_cell.font = Font(strike=True, color="808080")

    # Platform colour: this is the "which site did this come from" signal.
    if primary_cell := at("Primary Source"):
        style = styles.get(job.primary_source, {})
        primary_cell.value = style.get("name", job.primary_source)
        if style:
            primary_cell.fill = PatternFill("solid", fgColor=style["fill"])
            primary_cell.font = Font(bold=True, color=style["colour"])
        primary_cell.alignment = Alignment(horizontal="center")

    # A job seen on several platforms is worth spotting at a glance.
    if (sources_cell := at("Sources")) and len(job.source_names()) > 1:
        sources_cell.font = Font(bold=True)

    if score_cell := at("Match Score"):
        score_cell.number_format = "0"
        score_cell.alignment = Alignment(horizontal="center")

    for label in ("Date Posted", "Deadline", "Start Date", "Listed Deadline", "Date Found", "Last Updated"):
        if cell := at(label):
            cell.number_format = "yyyy-mm-dd"
    if cell := at("Last Verified"):
        cell.number_format = "yyyy-mm-dd hh:mm"

    # The click-through. This is the whole point of the dashboard.
    url = job.application_url or job.source_url
    if (cell := at("Application URL")) and url and url.startswith("http"):
        cell.hyperlink = url
        cell.value = "Apply" if not closed else "View (closed)"
        cell.font = Font(color="0563C1", underline="single", strike=closed)
        cell.alignment = Alignment(horizontal="center")

    if (cell := at("Source URL")) and job.source_url and job.source_url.startswith("http"):
        cell.hyperlink = job.source_url
        cell.value = "Listing"
        cell.font = Font(color="0563C1", underline="single")
        cell.alignment = Alignment(horizontal="center")

    # Work type and CV / WA / CL: each column keeps its own colour so the
    # requirements read at a glance. "No" stays uncoloured on purpose.
    for label, spec in flags.items():
        cell = at(label)
        if cell is None or not cell.value:
            continue
        cell.alignment = Alignment(horizontal="center")
        if cell.value == "No":
            cell.font = Font(color="808080")
            continue
        cell.fill = PatternFill("solid", fgColor=spec["fill"])
        if cell.value == "Yes":
            cell.font = Font(bold=True, color=spec["colour"])

    if (cell := at("Eligible for You")) is not None and cell.value:
        fill, colour = _ELIGIBILITY_STYLE.get(str(cell.value), (None, None))
        if fill:
            cell.fill = PatternFill("solid", fgColor=fill)
        if colour:
            cell.font = Font(bold=cell.value == "Yes", color=colour)
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    # An application takes its row's work-type colour, so a mixed list of
    # summer, placement and spring-week applications sorts itself by eye. A
    # row with no specific type stays plain rather than being given one.
    status_cell = at(STATUS_COLUMN)
    type_spec = flags.get(job.work_type)
    if status_cell is not None and status_cell.value and type_spec:
        status_cell.fill = PatternFill("solid", fgColor=type_spec["fill"])
        status_cell.font = Font(bold=True, color=type_spec["colour"])

    if closed:
        for label in ("Company", "Job Title"):
            if cell := at(label):
                cell.font = Font(strike=True, color="808080")


_WIDTHS = {
    "ID": 12, "Priority": 10, "Match Score": 8, "Match Category": 22,
    "Company": 24, "Job Title": 42, "Date Posted": 12, "Deadline": 12, "Open Status": 12,
    "Primary Source": 15, "Sources": 30, "Application URL": 15, STATUS_COLUMN: 22, "Status Evidence": 40,
    "Location": 18, "Work Type": 18, "Summer Internship": 11,
    "Industrial Placement": 11, "Spring Week": 9, "Expected Graduation": 12, "Eligible for You": 16, "Visa Sponsorship": 11, "Work Arrangement": 14,
    "Duration": 14, "Start Date": 12, "Salary": 18, "Degree Requirements": 26,
    "Year of Study": 16, "Experience Requirements": 26, "Skills": 34,
    "Programming Languages": 26, "Technologies": 30, "Strengths": 34,
    "Missing Skills": 30, "Concerns": 40, "Listed Deadline": 13,
    "Deadline Source": 16, "Last Verified": 17, "Source URL": 12,
    "Date Found": 12, "Last Updated": 12, "CV": 9, "WA": 9, "CL": 9, "Notes": 40,
}


def _finish_jobs_sheet(ws, row_count: int, idx: dict[str, int]) -> None:
    ws.freeze_panes = "C2"
    last_col = get_column_letter(len(idx))
    last_row = max(row_count + 1, 2)
    ws.auto_filter.ref = f"A1:{last_col}{last_row}"

    for label, col in idx.items():
        ws.column_dimensions[get_column_letter(col)].width = _WIDTHS.get(label, 14)

    # Application Status dropdown, sourced from the hidden Lists sheet.
    status_col = get_column_letter(idx[STATUS_COLUMN])
    dv = DataValidation(
        type="list",
        formula1=f"={SHEET_LISTS}!$A$2:$A${len(DROPDOWN_STATUSES) + 1}",
        allow_blank=True,
        showDropDown=False,
    )
    ws.add_data_validation(dv)
    dv.add(f"{status_col}2:{status_col}{last_row}")

    # Deadline urgency, applied to the Deadline column only.
    if "Deadline" not in idx:
        return
    deadline_col = get_column_letter(idx["Deadline"])
    rng = f"{deadline_col}2:{deadline_col}{last_row}"
    ws.conditional_formatting.add(
        rng,
        CellIsRule(operator="lessThan", formula=["TODAY()"],
                   fill=PatternFill("solid", fgColor="D9D9D9"), font=Font(color="808080")),
    )
    ws.conditional_formatting.add(
        rng,
        CellIsRule(operator="between", formula=["TODAY()", "TODAY()+7"],
                   fill=PatternFill("solid", fgColor="FFC7CE"), font=Font(color="9C0006", bold=True)),
    )
    ws.conditional_formatting.add(
        rng,
        CellIsRule(operator="between", formula=["TODAY()+8", "TODAY()+21"],
                   fill=PatternFill("solid", fgColor="FFEB9C"), font=Font(color="9C6500")),
    )



_ELIGIBILITY_HEADERS = [
    ("ID", 12), ("Company", 22), ("Programme (employer's name)", 40), ("Type", 18), ("Classified by", 42),
    ("University year (as stated)", 42), ("Graduation year", 12), ("Degree subject", 30), ("Duration", 12),
    ("Programme dates", 20), ("Location", 18), ("Deadline", 12), ("Right to work", 32), ("Visa sponsorship", 11),
    ("First-year eligible", 11), ("Second-year eligible", 11), ("Placement-year eligible", 12),
    ("Eligible for you", 16), ("Why", 46), ("Application URL", 12),
]


def _write_eligibility_sheet(wb: Workbook, jobs: list[Job]) -> None:
    """Every fact behind "Eligible for You", in the employer's own words, so a
    Yes or a REQUIRES VERIFICATION can be checked without opening the posting."""
    from openpyxl.utils import get_column_letter

    if SHEET_ELIGIBILITY in wb.sheetnames:
        del wb[SHEET_ELIGIBILITY]
    ws = wb.create_sheet(SHEET_ELIGIBILITY, 1)
    ws.append([label for label, _ in _ELIGIBILITY_HEADERS])

    for job in jobs:
        values = [job.id, job.company, job.job_title, job.work_type, job.classification_basis,
                  job.university_year, job.expected_graduation, job.degree_subject, job.duration,
                  job.programme_dates, job.location, job.deadline, job.right_to_work, job.visa_sponsorship,
                  job.first_year_eligible, job.second_year_eligible, job.placement_year_eligible,
                  job.eligible_for_you, job.eligibility_reason, job.application_url]
        ws.append([normalize.excel_safe(v) if isinstance(v, str) else v for v in values])
        row = ws.max_row
        link = ws.cell(row, len(values))
        if (job.application_url or "").startswith("http"):
            link.hyperlink = job.application_url
            link.value = "Apply"
            link.font = Font(color="0563C1", underline="single")
        verdict = ws.cell(row, len(values) - 2)
        fill, colour = _ELIGIBILITY_STYLE.get(str(verdict.value or ""), (None, None))
        if fill:
            verdict.fill = PatternFill("solid", fgColor=fill)
        if colour:
            verdict.font = Font(bold=verdict.value == "Yes", color=colour)

    for i, (_, width) in enumerate(_ELIGIBILITY_HEADERS, start=1):
        cell = ws.cell(1, i)
        cell.fill, cell.font = _HEADER_FILL, _HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = ws.dimensions


def _write_deadlines_sheet(wb: Workbook, jobs: list[Job]) -> None:
    from tracker import deadlines as deadline_mod

    if SHEET_DEADLINES in wb.sheetnames:
        del wb[SHEET_DEADLINES]
    ws = wb.create_sheet(SHEET_DEADLINES)

    ws.append(["Bucket", "Days Left", "Priority", "Score", "Company", "Job Title",
               "Deadline", "Open Status", "Primary Source", "Apply", "Status"])
    for cell in ws[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT

    styles = _source_styles()
    for bucket, bucket_jobs in deadline_mod.bucket(jobs).items():
        for job in bucket_jobs:
            days = (job.deadline - dt.date.today()).days if job.deadline else None
            ws.append([
                bucket, days, str(job.priority), job.match_score, job.company,
                job.job_title, job.deadline, str(job.open_status),
                styles.get(job.primary_source, {}).get("name", job.primary_source),
                "", str(job.status),
            ])
            row = ws.max_row
            ws.cell(row=row, column=7).number_format = "yyyy-mm-dd"
            url = job.application_url or job.source_url
            if url and url.startswith("http"):
                cell = ws.cell(row=row, column=10)
                cell.hyperlink = url
                cell.value = "Apply"
                cell.font = Font(color="0563C1", underline="single")

    # An empty sheet reads as "something broke". Say what it actually means.
    if ws.max_row == 1:
        ws.append(["Nothing closing within 14 days."])
        ws.cell(row=2, column=1).font = Font(italic=True, color="808080")
        total = sum(1 for j in jobs if j.deadline)
        ws.append([f"{total} of {len(jobs)} tracked jobs have a known deadline. "
                   "Jobs with no deadline are listed in Internships with a note in Concerns."])
        ws.cell(row=3, column=1).font = Font(italic=True, color="808080")

    ws.freeze_panes = "A2"
    for i, width in enumerate([18, 10, 10, 8, 24, 42, 12, 12, 15, 10, 14], start=1):
        ws.column_dimensions[get_column_letter(i)].width = width


def _write_runs_sheet(wb: Workbook, summary: dict | None) -> None:
    headers = ["Run At", "Sources OK", "Sources Failed", "Collected", "Merged",
               "Verified", "New", "Updated", "Expired", "HIGH", "MEDIUM", "LOW", "Notes"]

    if SHEET_RUNS not in wb.sheetnames:
        ws = wb.create_sheet(SHEET_RUNS)
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = _HEADER_FILL
            cell.font = _HEADER_FONT
        ws.freeze_panes = "A2"
        for i in range(1, len(headers) + 1):
            ws.column_dimensions[get_column_letter(i)].width = 16
        ws.column_dimensions[get_column_letter(len(headers))].width = 60
    else:
        ws = wb[SHEET_RUNS]

    if summary:
        ws.append([
            dt.datetime.now().replace(microsecond=0),
            ", ".join(summary.get("sources_ok", [])),
            ", ".join(summary.get("sources_failed", [])),
            summary.get("collected", 0), summary.get("merged", 0),
            summary.get("verified", 0), summary.get("new", 0),
            summary.get("updated", 0), summary.get("expired", 0),
            summary.get("high", 0), summary.get("medium", 0), summary.get("low", 0),
            normalize.excel_safe(summary.get("notes", "")),
        ])
        ws.cell(row=ws.max_row, column=1).number_format = "yyyy-mm-dd hh:mm"


_REVIEW_COLUMNS = [("Email Date", "email_date", 12), ("Sender", "sender_domain", 24),
                   ("Subject", "subject", 50), ("Detected Stage", "detected_stage", 18),
                   ("Company", "company", 22), ("Possible Role(s)", "possible_roles", 50),
                   ("Reason Not Processed", "reason", 50)]


def _write_status_review_sheet(wb: Workbook, items: list[dict]) -> None:
    """Recruitment emails the status sync could not settle on its own.

    Rewritten on every real sync. Nothing here changed a status: each is a
    question for the user, never a guess the tool made.
    """
    if SHEET_STATUS_REVIEW in wb.sheetnames:
        del wb[SHEET_STATUS_REVIEW]
    ws = wb.create_sheet(SHEET_STATUS_REVIEW)
    ws.append([label for label, _, _ in _REVIEW_COLUMNS])
    for cell in ws[1]:
        cell.fill, cell.font = _HEADER_FILL, _HEADER_FONT
    for item in items:
        ws.append([normalize.excel_safe(str(item.get(key) or "")) for _, key, _ in _REVIEW_COLUMNS])
    for i, (_, _, width) in enumerate(_REVIEW_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(_REVIEW_COLUMNS))}{max(len(items) + 1, 2)}"


def _write_guide_sheet(wb: Workbook) -> None:
    if SHEET_GUIDE in wb.sheetnames:
        del wb[SHEET_GUIDE]
    ws = wb.create_sheet(SHEET_GUIDE)
    styles = _source_styles()

    ws["A1"] = "How to use this workbook"
    ws["A1"].font = Font(bold=True, size=14)

    rows = [
        ("", ""),
        ("Where to look", "Sort is already done: HIGH priority and nearest deadline are at the top of Internships."),
        ("To apply", "Click 'Apply' in the Application URL column. It opens the real application page."),
        ("After applying", "Pick a stage in Application Status (Application Submitted, Online Assessment, "
                           "Video Interview, ...). Blank means not applied. The daily scan never overwrites it. "
                           "The 23:00 email sync may move it forward only on an explicit recruitment email, "
                           "shown in Status Evidence; edit a status yourself and that row is never auto-changed again."),
        ("", ""),
        ("Platform colours", "The Primary Source cell is coloured by where the job was found:"),
    ]
    for label, text in rows:
        ws.append([label, text])

    for key, style in styles.items():
        ws.append(["", style["name"]])
        cell = ws.cell(row=ws.max_row, column=2)
        cell.fill = PatternFill("solid", fgColor=style["fill"])
        cell.font = Font(bold=True, color=style["colour"])

    ws.append(["", "A job found on more than one platform appears ONCE, with all platforms listed in Sources (shown in bold)."])
    ws.append(["", ""])
    ws.append(["Open Status", "Whether the role is still accepting applications, checked on the real job page:"])
    ws.append(["", "OPEN - confirmed open"])
    ws.cell(row=ws.max_row, column=2).fill = _OPEN_FILLS[OpenStatus.OPEN]
    ws.append(["", "CLOSED - confirmed closed. Kept for history, sorted to the bottom."])
    ws.cell(row=ws.max_row, column=2).fill = _OPEN_FILLS[OpenStatus.CLOSED]
    ws.append(["", "UNKNOWN - could not be confirmed either way. Verify before relying on it."])
    ws.cell(row=ws.max_row, column=2).fill = _OPEN_FILLS[OpenStatus.UNKNOWN]
    ws.append(["", ""])
    ws.append(["Deadline", "'Deadline' is the verified date where one was found. 'Listed Deadline' is what the platform claimed. If they disagree, Concerns says so."])
    ws.append(["Match Score", "While data/profile.json is unconfigured, scores measure role relevance only and are capped at 60. Strengths stay empty rather than guessed."])
    ws.append(["Applications", "An Application Status cell takes its row's work-type colour: orange Summer "
               "Internship, purple Industrial Placement, teal Spring Week. Any status drops the row out of "
               "HIGH priority and out of the closing-soon alerts."])
    ws.append(["", ""])
    ws.append(["CV / WA / CL", "What the application asks for, as the platform states it: CV, Written Answers, "
               "Cover Letter. Yes / Optional / No; blank means the platform did not say."])
    ws.append(["Work type", "Summer Internship, Industrial Placement and Spring Week each have their own "
               "column; the row's type says Yes."])
    for label, spec in _flag_styles().items():
        ws.append(["", label])
        cell = ws.cell(row=ws.max_row, column=2)
        cell.fill = PatternFill("solid", fgColor=spec["fill"])
        cell.font = Font(bold=True, color=spec["colour"])
    ws.append(["Expected Graduation", "The graduation year(s) a posting admits: as the employer states it, "
               "or read from the year of study it names for a dated intake (Year of Study holds the "
               "sentence). A row open to the graduation year in data/profile.json is marked Spring Week "
               "unless it is a placement."])
    ws.append(["Eligible for You", "Yes, No or REQUIRES VERIFICATION for your graduation year and degree in "
               "data/profile.json, read from the posting itself. The Eligibility sheet shows every fact behind "
               "it - year of study, graduation year, subject, duration, dates, right to work, visa - in the "
               "employer's words, and what decided the programme type."])
    ws.append(["Notes", "Yours. The sync never writes to it and never overwrites what you type."])
    ws.append(["Columns", "To add or remove columns, edit config/workbook.yaml. Deleting a column in "
               "Excel alone is undone by the next run."])

    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 110
    for row in ws.iter_rows(min_col=2, max_col=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")


def _write_lists_sheet(wb: Workbook) -> None:
    """Hidden source for the Application Status dropdown."""
    if SHEET_LISTS in wb.sheetnames:
        del wb[SHEET_LISTS]
    ws = wb.create_sheet(SHEET_LISTS)
    ws["A1"] = STATUS_COLUMN
    for status in DROPDOWN_STATUSES:
        ws.append([str(status)])
    ws.sheet_state = "hidden"
