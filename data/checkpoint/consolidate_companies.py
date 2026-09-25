"""One row per company in data/internships.xlsx. Formatting + consolidation only.

Reads the Internships sheet as it is now (including the user's own edits),
writes a consolidated copy, re-reads that copy and proves nothing was lost, and
only then backs up and replaces the workbook. Every per-programme value stays
on its own line, aligned with its programme, so each title keeps its deadline,
link, status and eligibility. Nothing is searched and the tracker's saved state
(data/state) is not touched.

    pipenv run python data/checkpoint/consolidate_companies.py            # consolidate
    pipenv run python data/checkpoint/consolidate_companies.py --check    # verify only, write nothing
"""
import datetime as dt
import os
import re
import shutil
import sys
from collections import Counter, OrderedDict
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from rapidfuzz import fuzz

ROOT = Path(r"D:\School\Personal\Intership tracker")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from tracker import config, excel, normalize  # noqa: E402

WORKBOOK = ROOT / "data" / "internships.xlsx"
BACKUP = ROOT / "data" / "checkpoint" / "backups" / "internships-before-company-consolidation.xlsx"
DRAFT = ROOT / "data" / "checkpoint" / "internships-by-company-draft.xlsx"
SHEET = excel.SHEET_JOBS

AGGREGATORS = {"Gradcracker", "Bright Network", "Higherin", "TargetJobs", "gradcracker", "brightnetwork",
               "higherin", "targetjobs"}
PRIORITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
TYPE_COLUMNS = ("Summer Internship", "Industrial Placement", "Spring Week")
#: A line for a programme that has no value in this column. Not "-": Excel's
#: formula guard would prefix a leading "-" with an apostrophe.
DASH = "(not stated)"

#: Names confirmed on 2026-09-16 to be one employer from shared programmes or
#: careers links -> the name to keep. Applied only in this consolidation; the
#: tracker's own company matching is unchanged.
SAME_COMPANY = {
    "Maven": "Maven Securities",
    "Marshall Wace Internship Programmes": "Marshall Wace",
    "Bny": "BNY Mellon",
    "Zurich": "Zurich Insurance",
    "Crédit Agricole CIB": "Crédit Agricole",
    "Honeywell Aerospace": "Honeywell",
    "Symphony Communication Services": "Symphony",
}

#: Similar names checked and confirmed to be different employers.
DIFFERENT_COMPANIES = {("Citadel", "Citadel Securities"), ("Capital One", "Capital Group"),
                       ("Volkswagen Financial Services", "Volkswagen Group"), ("Red Bull", "Red Bull Powertrains")}
LINE_HEIGHT = 15
MAX_ROW_HEIGHT = 409           # Excel's limit


def text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dt.datetime, dt.date)):
        return f"{value:%d %b %Y}"
    return str(value).strip()


def company_key(name: str) -> str:
    """The tracker's own company normalisation: case, punctuation, legal
    suffixes and known aliases. Different companies are never merged by it."""
    return normalize.company(name)[1] or name.casefold().strip()


# ---------------------------------------------------------------- read
wb = load_workbook(WORKBOOK)
ws = wb[SHEET]
header = [c.value for c in ws[1]]
col = {name: i for i, name in enumerate(header) if name}
needed = ["ID", "Company", "Job Title", "Application URL"]
missing = [n for n in needed if n not in col]
assert not missing, f"columns missing from the sheet: {missing}"

rows = []
for r in range(2, ws.max_row + 1):
    cells = [ws.cell(r, i + 1) for i in range(len(header))]
    if not cells[col["ID"]].value:
        continue
    record = {name: cells[i].value for name, i in col.items()}
    url_cell = cells[col["Application URL"]]
    record["_url"] = (url_cell.hyperlink.target if url_cell.hyperlink else "") or (
        text(url_cell.value) if text(url_cell.value).startswith("http") else "")
    rows.append(record)

print(f"original rows: {len(rows)} | columns: {len(header)}")
assert len({r['ID'] for r in rows}) == len(rows), "duplicate IDs in the original sheet"

# ---------------------------------------------------------------- group
same_key = {company_key(k): company_key(v) for k, v in SAME_COMPANY.items()}
groups: "OrderedDict[str, list[dict]]" = OrderedDict()
for record in rows:
    key = company_key(text(record["Company"]))
    groups.setdefault(same_key.get(key, key), []).append(record)
unused = [k for k in SAME_COMPANY if not any(company_key(text(r["Company"])) == company_key(k) for r in rows)]
assert not unused, f"confirmed-same names not found in the sheet: {unused}"


def display_name(records):
    """The spelling already used in the sheet: the most common one, preferring
    names that came from Trackr or the employer's own site over job boards,
    which often title-case a slug ("Bny", "Blackrock")."""
    names = {text(r["Company"]) for r in records}
    chosen = next((v for k, v in SAME_COMPANY.items() if k in names or v in names), None)
    if not chosen:
        trusted = [text(r["Company"]) for r in records if text(r.get("Primary Source")) not in AGGREGATORS]
        chosen = Counter(trusted or [text(r["Company"]) for r in records]).most_common(1)[0][0]
    # A job board title-cases legal suffixes taken from a web address ("Llp").
    return re.sub(r"\b(Llp|Plc|Llc|Uk)\b", lambda m: m.group(1).upper(), chosen)


def programme_order(record):
    status = text(record.get("Open Status"))
    deadline = record.get("Deadline")
    closed = status == "CLOSED"
    return (closed, deadline is None, deadline or dt.datetime.max, text(record["Job Title"]))


companies = []
for key, records in groups.items():
    records = sorted(records, key=programme_order)
    companies.append((key, display_name(records), records))

# Similar-looking names that the normalisation did not treat as one company.
review_names = []
keys = [(k, name) for k, name, _ in companies]
for i, (k1, n1) in enumerate(keys):
    for k2, n2 in keys[i + 1:]:
        short, long_ = sorted((k1, k2), key=len)
        if len(short) >= 3 and (long_.startswith(short + " ") or fuzz.ratio(k1, k2) >= 92):
            if (n1, n2) not in DIFFERENT_COMPANIES and (n2, n1) not in DIFFERENT_COMPANIES:
                review_names.append((n1, n2))

# ---------------------------------------------------------------- build
LINED = ["ID", "Job Title", "Deadline", "Open Status", "Primary Source", "Expected Graduation", "Eligible for You",
         "Duration", "Visa Sponsorship", "CV", "WA", "CL"]
flag_styles = excel._flag_styles()
priority_fills = {str(k): v for k, v in excel._PRIORITY_FILLS.items()}


def lines(records, name):
    return "\n".join(text(r.get(name)) or DASH for r in records)


def unique_lines(values):
    out = []
    for value in values:
        for part in re.split(r"\n|,\s*", text(value)):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def labelled(records, name):
    """User-owned columns: only programmes with a value, each tagged with its ID."""
    return "\n".join(f"{r['ID']}: {text(r.get(name))}" for r in records if text(r.get(name)))


consolidated = []
for key, name, records in companies:
    row = {h: None for h in header if h}
    row["Company"] = name
    for column in LINED:
        if column in col:
            # One programme: its cells stay exactly as they were, blanks included.
            row[column] = records[0].get(column) if len(records) == 1 else lines(records, column)
    if "Priority" in col:
        present = [text(r.get("Priority")) for r in records if text(r.get("Priority"))]
        row["Priority"] = min(present, key=lambda p: PRIORITY_RANK.get(p, 9)) if present else None
    if "Match Score" in col:
        scores = [r.get("Match Score") for r in records if isinstance(r.get("Match Score"), (int, float))]
        row["Match Score"] = max(scores) if scores else None
    if "Sources" in col:
        row["Sources"] = ", ".join(unique_lines(r.get("Sources") for r in records)) or None
    if "Location" in col:
        row["Location"] = "\n".join(unique_lines(r.get("Location") for r in records)) or None
    if "Skills" in col:
        row["Skills"] = ", ".join(unique_lines(r.get("Skills") for r in records)) or None
    for column in TYPE_COLUMNS:
        if column in col:
            row[column] = "Yes" if any(text(r.get(column)) == "Yes" for r in records) else None
    urls = []
    for r in records:
        if r["_url"] and r["_url"] not in urls:
            urls.append(r["_url"])
    row["_urls"] = urls
    row["Application URL"] = "\n".join(urls) or None
    for column in ("Application Status", "Notes"):
        if column in col:
            if len(records) == 1:
                row[column] = records[0].get(column)
            else:
                row[column] = labelled(records, column) or None
    row["_records"] = records
    consolidated.append(row)

# ---------------------------------------------------------------- write draft
last_old = ws.max_row
ws.delete_rows(2, last_old)
ws._hyperlinks = []
link_font = Font(color="0563C1", underline="single")

for n, row in enumerate(consolidated, start=2):
    for name, i in col.items():
        value = row.get(name)
        if isinstance(value, str) and value:
            value = normalize.excel_safe(value)
        cell = ws.cell(n, i + 1, value)
        cell.alignment = Alignment(wrap_text=True, vertical="top",
                                   horizontal="center" if name in TYPE_COLUMNS or name in ("Priority", "Match Score")
                                   else None)
        if name == "Deadline" and isinstance(value, (dt.datetime, dt.date)):
            cell.number_format = "dd mmm yyyy"
    if row["_urls"]:
        cell = ws.cell(n, col["Application URL"] + 1)
        cell.hyperlink = row["_urls"][0]
        cell.value = "\n".join(row["_urls"])
        cell.font = link_font
    for name in TYPE_COLUMNS:
        if name in col and row.get(name) == "Yes" and name in flag_styles:
            spec = flag_styles[name]
            cell = ws.cell(n, col[name] + 1)
            cell.fill = PatternFill("solid", fgColor=spec["fill"])
            cell.font = Font(bold=True, color=spec["colour"])
    if "Priority" in col and row.get("Priority") in priority_fills:
        ws.cell(n, col["Priority"] + 1).fill = priority_fills[row["Priority"]]
    if "Eligible for You" in col:
        verdicts = row["Eligible for You"].split("\n")
        best = "Yes" if "Yes" in verdicts else "REQUIRES VERIFICATION" if "REQUIRES VERIFICATION" in verdicts else "No"
        fill, colour = excel._ELIGIBILITY_STYLE.get(best, (None, None))
        cell = ws.cell(n, col["Eligible for You"] + 1)
        if fill:
            cell.fill = PatternFill("solid", fgColor=fill)
        if colour:
            cell.font = Font(bold=best == "Yes", color=colour)

    widths = {name: (ws.column_dimensions[ws.cell(1, i + 1).column_letter].width or 12) for name, i in col.items()}
    estimate = 1
    for name, i in col.items():
        value = text(ws.cell(n, i + 1).value)
        if not value:
            continue
        chars_per_line = max(int(widths[name] * 1.1), 6)
        estimate = max(estimate, sum(max(1, -(-len(part) // chars_per_line)) for part in value.split("\n")))
    ws.row_dimensions[n].height = min(MAX_ROW_HEIGHT, LINE_HEIGHT * estimate)
    row["_height_capped"] = LINE_HEIGHT * estimate > MAX_ROW_HEIGHT

last_new = len(consolidated) + 1
last_col = ws.cell(1, len(header)).column_letter
ws.auto_filter.ref = f"A1:{last_col}{last_new}"
for validation in ws.data_validations.dataValidation:
    ranges = str(validation.sqref).split()
    validation.sqref = " ".join(re.sub(r"(\$?[A-Z]+\$?)(\d+)$", lambda m: f"{m.group(1)}{last_new}", rng)
                                if ":" in rng else rng for rng in ranges)
for cf_range in list(ws.conditional_formatting._cf_rules):
    pass  # the deadline colour rule still covers the column; single-programme dates keep it

check_only = "--check" in sys.argv
wb.save(DRAFT)
print(f"draft written: {DRAFT}")

# ---------------------------------------------------------------- verify the draft from disk
wb2 = load_workbook(DRAFT)
ws2 = wb2[SHEET]
assert [c.value for c in ws2[1]] == header, "header changed"
problems = []
seen_ids = Counter()
keys_after = Counter()
for n in range(2, ws2.max_row + 1):
    get = {name: ws2.cell(n, i + 1).value for name, i in col.items()}
    if not get["ID"]:
        continue
    ids = text(get["ID"]).split("\n")
    seen_ids.update(ids)
    keys_after[company_key(text(get["Company"]))] += 1
    records = next(r["_records"] for r in consolidated if r["Company"] == get["Company"])
    by_id = {r["ID"]: r for r in records}
    for column in LINED:
        if column not in col:
            continue
        if len(ids) == 1:
            if text(get[column]) != text(by_id[ids[0]].get(column)):
                problems.append((ids[0], column, by_id[ids[0]].get(column), get[column]))
            continue
        parts = text(get[column]).split("\n")
        if len(parts) != len(ids):
            problems.append((get["Company"], column, f"{len(ids)} programmes", f"{len(parts)} lines"))
            continue
        for pid, part in zip(ids, parts):
            original = text(by_id[pid].get(column))
            if (original or DASH) != part:
                problems.append((pid, column, original, part))
    url_cell = ws2.cell(n, col["Application URL"] + 1)
    kept_urls = set(text(url_cell.value).split("\n"))
    for r in records:
        if r["_url"] and r["_url"] not in kept_urls:
            problems.append((r["ID"], "Application URL", r["_url"], "missing"))
    if records and any(r["_url"] for r in records) and not url_cell.hyperlink:
        problems.append((get["Company"], "Application URL", "clickable link", "not clickable"))
    for column in ("Sources", "Location", "Skills"):
        if column in col:
            kept = set(unique_lines([get[column]]))
            for r in records:
                for part in unique_lines([r.get(column)]):
                    if part not in kept:
                        problems.append((r["ID"], column, part, "missing"))
    for column in TYPE_COLUMNS:
        if column in col:
            expected = "Yes" if any(text(r.get(column)) == "Yes" for r in records) else ""
            if text(get[column]) != expected:
                problems.append((get["Company"], column, expected, get[column]))
    for column in ("Application Status", "Notes"):
        if column in col:
            kept = text(get[column])
            for r in records:
                value = text(r.get(column))
                if value and value not in kept:
                    problems.append((r["ID"], column, value, "missing"))

original_ids = Counter(r["ID"] for r in rows)
lost = sorted(set(original_ids) - set(seen_ids))
doubled = sorted(i for i, c in seen_ids.items() if c > 1)
repeated_companies = [k for k, c in keys_after.items() if c > 1]

print(f"final company rows: {len(consolidated)}")
print(f"companies consolidated (had 2+ rows): {sum(1 for _, _, r in companies if len(r) > 1)}")
print(f"opportunities preserved: {sum(seen_ids.values())} of {len(rows)} | lost IDs: {len(lost)} | IDs twice: {len(doubled)}")
print(f"companies appearing more than once: {len(repeated_companies)}")
print(f"value mismatches: {len(problems)}")
for p in problems[:20]:
    print("   ", p)
print(f"row heights capped at Excel's limit (text still all there): "
      f"{[r['Company'] for r in consolidated if r['_height_capped']]}")
print(f"companies with several links (first is clickable, all listed; each stays clickable on the Eligibility sheet): "
      f"{sum(1 for r in consolidated if len(r['_urls']) > 1)}")
print(f"similar names NOT merged (please review): {len(review_names)}")
for a, b in review_names[:40]:
    print(f"    {a}  |  {b}")

ok = not (lost or doubled or repeated_companies or problems)
if not ok:
    print("VERIFICATION FAILED - the workbook was NOT replaced.")
    sys.exit(2)
if check_only:
    print("verification passed (--check: workbook not replaced)")
    sys.exit(0)
if (ROOT / "data" / "~$internships.xlsx").exists():
    print("Excel has the workbook open - close it; the verified draft is waiting at", DRAFT)
    sys.exit(3)
shutil.copy2(WORKBOOK, BACKUP)
shutil.copy2(DRAFT, WORKBOOK)
print(f"verified. backup: {BACKUP} | workbook replaced: {WORKBOOK}")
