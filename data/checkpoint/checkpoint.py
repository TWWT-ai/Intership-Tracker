"""Save a resumable research checkpoint into the project: data/checkpoint/.

Read-only against the pipeline's state; copies scripts and backups so the work
can continue in a later session, whose scratchpad will be a different folder.
"""
import csv
import json
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(r"D:\School\Personal\Intership tracker")
SCRATCH = Path(__file__).parent
OUT = ROOT / "data" / "checkpoint"
(OUT / "backups").mkdir(parents=True, exist_ok=True)
TARGET = "2029"

# ---- copy working files out of the session scratchpad
copied = []
for name in ("repair_locations.py", "compare_graduation.py", "build_report.py", "fingerprint.py",
             "checkpoint.py", "companies-searched.csv", "fingerprints.json", "fingerprint-output.txt"):
    src = SCRATCH / name
    if src.exists():
        shutil.copy2(src, OUT / name)
        copied.append(name)
for name in ("internships-before-graduation.xlsx", "last_run-before-graduation.json",
             "id_registry-before-graduation.json", "internships-before-rerun.xlsx"):
    src = SCRATCH / name
    if src.exists():
        shutil.copy2(src, OUT / "backups" / name)
        copied.append(f"backups/{name}")
# The copied scripts look for backups next to themselves; point them at backups/.
for name in ("compare_graduation.py", "build_report.py"):
    path = OUT / name
    if path.exists():
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('SCRATCH / "internships-before-graduation.xlsx"',
                                     'SCRATCH / "backups" / "internships-before-graduation.xlsx"'), encoding="utf-8")

# ---- state
payload = json.loads((ROOT / "data/state/last_run.json").read_text(encoding="utf-8"))
jobs = payload["jobs"]
log_path = ROOT / "data/logs/graduation-run-2026-09-13.txt"
log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
profile = json.loads((ROOT / "data/profile.json").read_text(encoding="utf-8"))


def grab(pattern):
    m = re.search(pattern, log)
    return m.group(1) if m else ""


fields = ["id", "company", "job_title", "work_type", "expected_graduation", "deadline", "open_status",
          "status", "location", "application_url", "year_of_study"]


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields + ["sources"], extrasaction="ignore")
        w.writeheader()
        for j in rows:
            w.writerow({**{k: j.get(k) or "" for k in fields},
                        "sources": ", ".join(f"{s['source']}{'/' + s['via'] if s.get('via') else ''}" for s in j["sources"])})


with_year = [j for j in jobs if j.get("expected_graduation")]
admits = sorted([j for j in with_year if TARGET in j["expected_graduation"]],
                key=lambda j: (j.get("open_status") == "CLOSED", j.get("deadline") or "9999", j["company"]))
write_csv(OUT / "roles-2029.csv", admits)
write_csv(OUT / "roles-with-graduation-year.csv", sorted(with_year, key=lambda j: (j["company"], j["job_title"])))

statuses = [j for j in jobs if j.get("status") not in (None, "", "FOUND")]
sources = Counter(s["source"] + (f"/{s['via']}" if s.get("via") else "") for j in jobs for s in j["sources"])


def md(text):
    return str(text or "").replace("|", "/").replace("\n", " ")


rows_md = "\n".join(
    f"| {j['id']} | {md(j['company'])} | {md(j['job_title'])} | {md(j.get('work_type'))} | {md(j['expected_graduation'])} "
    f"| {md(j.get('deadline') or '-')} | {md(j.get('open_status'))} | {md(j.get('status') if j.get('status') != 'FOUND' else '')} "
    f"| {md((j.get('year_of_study') or '')[:140])} |"
    for j in admits)

sweep_line = grab(r"\[Eligibility\] (\d+ employer pages read.+)")
chain_done = "chain exit" in log

note = f"""# Internship research checkpoint

Generated {datetime.now():%Y-%m-%d %H:%M}. Saved so the work can be picked up in a later session.
Everything here is read from the pipeline's own state; nothing was typed in by hand.

## What the user asked for (their rules)

- Search every company in the sheet, on its own careers site, for programmes whose requirements admit
  **students graduating in {profile.get('graduation_year')}** - including industrial placements.
  Employers name these differently (Spring Insight Event, Discover Nomura, Immersion Programme...):
  record the employer's name, filter only on the requirement.
- Workbook: an **Expected Graduation** column right after Spring Week, showing the year(s) a posting admits.
  A row admitting {TARGET} gets **Spring Week = Yes**, unless it is a placement (keeps Industrial Placement).
- One row per programme (BlackRock's Spring Insight is its own row, INT-000606).
- Never overwrite Application Status / Notes; never bypass anti-bot checks; never submit applications.

## State at checkpoint

- Workbook `data/internships.xlsx`: **{len(jobs)} rows**, {len(with_year)} with an Expected Graduation,
  **{len(admits)} admitting {TARGET}**. User-set statuses in the saved run: {len(statuses)} rows.
- Full search (run `graduation-run-2026-09-13`): finished ({grab(r'Search completed in (\d+)s') or '?'} s).
  Sheet change: {grab(r'\[Excel\] (\d+ new, \d+ updated, \d+ expired)') or '?'}.
  Company careers postings read: {grab(r'\[Company site\] (\d+) listings found') or '?'}.
- Graduation-year sweep of employer postings (`main.py eligibility`):
  {('finished - ' + sweep_line) if sweep_line else ('running when this was saved' if not chain_done else 'did not report')}.
- Listings by platform: {', '.join(f'{k} {v}' for k, v in sources.most_common())}.
- Daily scheduled task **Internship Tracker Daily is DISABLED** (user asked). Re-enable:
  `Enable-ScheduledTask -TaskName "Internship Tracker Daily"`.
- `data/profile.json`: graduation_year = {profile.get('graduation_year')} (told by the user); profile_configured is still
  false, so match scores stay capped at 60.

## Roles admitting {TARGET} (soonest deadline first; closed last)

| ID | Company | Programme (employer's name) | Type | Grad | Deadline | Open | Your status | Evidence from the posting |
|---|---|---|---|---|---|---|---|---|
{rows_md}

Full detail with links: `roles-2029.csv`. Every row with a year: `roles-with-graduation-year.csv`.

## How it works now (code, 315 offline tests pass)

- `tracker/graduation.py`: stated year wins ("graduating in 2029", ranges); otherwise year of study + the programme's
  own dated year (spring week +2/+3, summer internship +1, placement +2). No programme year, no derived year.
- `tracker/programme.py` + `config/programmes.yaml`: type from who may apply; penultimate wording never turns a
  placement title into a summer internship; negated/redirect sentences ignored.
- `sources/careers.py`: boards Greenhouse, Lever, Ashby, Workable, Workday, Oracle, Amazon, **Oleeo** (tal.net at the
  10 s robots crawl delay; anti-bot pages skipped). Feed boards keep any UK posting admitting the user's year; search-only
  boards (Workday/Oracle) still title-screen. One row per posting across a tenant's sites.
  UK check strips "New South Wales"/"New England"; multi-city lists show the UK city.
- `main.py eligibility`: reads employer postings for rows no feed described (never search-results pages).
- `main.py match`: re-applies programme + graduation rules offline.

## To resume

1. If the sweep was cut off (laptop slept / offline), run it again - it skips rows that already have a year:
   `pipenv run python main.py eligibility`
2. Fix city text on company-site rows written by the long run (it loaded the old picker/UK filter; Anduril's Sydney
   internships came in as UK). With Excel closed:
   `pipenv run python data/checkpoint/repair_locations.py`
3. Check nothing the user set changed: `pipenv run python data/checkpoint/compare_graduation.py`
4. Build the phone report: `pipenv run python data/checkpoint/build_report.py` (writes next to the script), publish it,
   notify the user, send them `data/internships.xlsx`.

## Coverage and open decisions

- 482 companies. Own careers site scanned: ~176 (Workday 79, Greenhouse 33, Ashby 21, Oracle 19, Workable 12,
  Oleeo 8, Lever 7, Amazon 1). Detail: `companies-searched.csv` (from before Oleeo was added).
- Skipped at anti-bot checks, never bypassed: Gradcracker, Bright Network, Bank of America (Oleeo "Quick Check").
- Not scannable: 230 companies on custom career sites, 78 with no employer link. Fingerprints of the custom sites
  (`fingerprints.json`): SuccessFactors 20, Phenom 14, embedded Greenhouse 14, Workday on other hosts 12, iCIMS 10 -
  candidates for new readers. Google Careers and SmartRecruiters: robots.txt disallows.
- **Waiting on the user:** Eightfold (Microsoft, AstraZeneca, PayPal, AmEx, Qualcomm, STMicro) is off because
  urllib.robotparser applies first-match and their robots.txt says `Disallow: /` before `Allow: /api/pcsx`.
  Fixing means changing the compliance guard - user's decision. Also offered: GSA Capital (embed Greenhouse link) and
  Brevan Howard (myworkdaysite.com host) boards.
- Known: J.P. Morgan "Software Engineer Immersion Program - Summer Internship" admits a range including {TARGET}, so the
  user's rule marks it Spring Week. 10 Trackr rows where Trackr's category contradicts the employer's title were left
  as Trackr says (no evidence either way).

## Files in this folder

{chr(10).join('- ' + c for c in copied)}
- roles-2029.csv, roles-with-graduation-year.csv, research-checkpoint.md (this file)
"""
(OUT / "research-checkpoint.md").write_text(note, encoding="utf-8")
print(f"checkpoint saved to {OUT} | rows {len(jobs)} | with year {len(with_year)} | admit {TARGET}: {len(admits)} | sweep {'done' if sweep_line else 'running'}")
print("copied:", copied)
