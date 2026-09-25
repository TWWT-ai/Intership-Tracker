"""Add the FDE findings to data/internships.xlsx as their own sheet.

Only adds a sheet: every existing sheet, its merged cells, filters and links are
left exactly as they are. Backs the workbook up first, then reloads and verifies
what it wrote before replacing the file.
"""
import csv
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from openpyxl import load_workbook  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

WORKBOOK = ROOT / "data" / "internships.xlsx"
BACKUP = ROOT / "data" / "checkpoint" / "backups" / "internships-before-fde-sheet.xlsx"
DRAFT = ROOT / "data" / "checkpoint" / "internships-fde-draft.xlsx"
CSV = ROOT / "data" / "checkpoint" / "fde-roles.csv"
SHEET = "Forward-Deployed Engineer"

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
LEVEL_FILL = {"Internship": "C6EFCE", "Graduate": "E2EFDA", "Experienced": "FFF2CC", "Senior": "D9D9D9"}
WIDTHS = {"Company": 26, "Job Title": 46, "Location": 26, "Level": 14, "Exact FDE title": 18,
          "Team": 22, "Platform": 12, "Application URL": 42, "What the posting says": 70}

if (ROOT / "data" / "~$internships.xlsx").exists():
    sys.exit("Excel has the workbook open - close it first.")
rows = list(csv.DictReader(CSV.open(encoding="utf-8-sig")))
print(f"rows to write: {len(rows)}")

wb = load_workbook(WORKBOOK)
before = list(wb.sheetnames)
if SHEET in wb.sheetnames:
    del wb[SHEET]
ws = wb.create_sheet(SHEET, 1)
header = list(WIDTHS)

for col, title in enumerate(header, start=1):
    cell = ws.cell(1, col, title)
    cell.fill, cell.font = HEADER_FILL, HEADER_FONT
    cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.column_dimensions[get_column_letter(col)].width = WIDTHS[title]

for r, row in enumerate(rows, start=2):
    for col, title in enumerate(header, start=1):
        value = row.get(title, "")
        cell = ws.cell(r, col, value)
        cell.alignment = Alignment(vertical="top", wrap_text=title in ("Job Title", "What the posting says",
                                                                      "Location", "Team"))
        if title == "Application URL" and value.startswith("http"):
            cell.hyperlink = value
            cell.font = Font(color="0563C1", underline="single")
        if title == "Level":
            fill = LEVEL_FILL.get(value)
            if fill:
                cell.fill = PatternFill("solid", fgColor=fill)
    ws.row_dimensions[r].height = 30

ws.freeze_panes = "C2"
if rows:
    ws.auto_filter.ref = f"A1:{get_column_letter(len(header))}{len(rows) + 1}"
wb.save(DRAFT)

# ---- verify the draft before it replaces the workbook
v = load_workbook(DRAFT)
problems = []
if [s for s in v.sheetnames if s != SHEET] != [s for s in before if s != SHEET]:
    problems.append(("sheets changed", v.sheetnames, before))
check = v[SHEET]
if [c.value for c in check[1]] != header:
    problems.append(("header", [c.value for c in check[1]]))
if check.max_row - 1 != len(rows):
    problems.append(("row count", check.max_row - 1, len(rows)))
for r, row in enumerate(rows, start=2):
    for col, title in enumerate(header, start=1):
        if (check.cell(r, col).value or "") != (row.get(title) or ""):
            problems.append((r, title, check.cell(r, col).value, row.get(title)))
    url = row.get("Application URL") or ""
    if url.startswith("http") and check.cell(r, header.index("Application URL") + 1).hyperlink is None:
        problems.append((r, "missing link", url))
original = load_workbook(WORKBOOK)
for sheet in before:
    if sheet == SHEET:
        continue
    a, b = original[sheet], v[sheet]
    if (a.max_row, a.max_column) != (b.max_row, b.max_column):
        problems.append((sheet, "size changed", (a.max_row, a.max_column), (b.max_row, b.max_column)))
    if {str(m) for m in a.merged_cells.ranges} != {str(m) for m in b.merged_cells.ranges}:
        problems.append((sheet, "merged cells changed"))
    if a.auto_filter.ref != b.auto_filter.ref:
        problems.append((sheet, "filter changed", a.auto_filter.ref, b.auto_filter.ref))

print("verification problems:", len(problems))
for p in problems[:20]:
    print("   ", p)
if problems:
    sys.exit("VERIFICATION FAILED - the workbook was NOT changed. Draft: " + str(DRAFT))

shutil.copy2(WORKBOOK, BACKUP)
shutil.copy2(DRAFT, WORKBOOK)
print(f"sheet '{SHEET}' added with {len(rows)} rows. backup: {BACKUP}")
print("sheets now:", load_workbook(WORKBOOK, read_only=True).sheetnames)
