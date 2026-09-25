"""Build the phone-readable completion report from the finished run. Read-only."""
import html
import json
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(r"D:\School\Personal\Intership tracker")
SCRATCH = Path(__file__).parent
TARGET = 2029
TODAY = date.today()
TYPES = ("Summer Internship", "Industrial Placement", "Spring Week")


def esc(value):
    return html.escape(str(value if value is not None else ""))


def sheet(path):
    wb = load_workbook(path, read_only=True, data_only=True)
    it = wb["Internships"].iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(it)]
    rows = {str(v[0]): dict(zip(header, v)) for v in it if v and v[0]}
    wb.close()
    return header, rows


jobs = json.loads((ROOT / "data/state/last_run.json").read_text(encoding="utf-8"))
saved_at = datetime.fromisoformat(jobs["saved_at"])
jobs = {j["id"]: j for j in jobs["jobs"]}
_, before = sheet(SCRATCH / "backups" / "internships-before-graduation.xlsx")
header, after = sheet(ROOT / "data/internships.xlsx")
log = (ROOT / "data/logs/graduation-run-2026-09-13.txt").read_text(encoding="utf-8", errors="replace")

# --- safety: user-owned cells
checked = lost = changed = 0
for job_id, old in before.items():
    for col in ("Application Status", "Notes"):
        value = str(old.get(col) or "").strip()
        if not value:
            continue
        checked += 1
        new = after.get(job_id)
        if new is None:
            lost += 1
        elif str(new.get(col) or "").strip() != value:
            changed += 1
safe = lost == 0 and changed == 0

# --- run facts from the log
def grab(pattern, default=""):
    m = re.search(pattern, log)
    return m.group(1) if m else default

excel_line = grab(r"\[Excel\] (\d+ new, \d+ updated, \d+ expired)")
eligibility_line = grab(r"\[Eligibility\] (.+)")
completed = grab(r"Search completed in (\d+)s")
blocked = sorted(set(re.findall(r"\[([A-Za-z ]+)\] BLOCKED", log)))
careers_found = grab(r"\[Company site\] (\d+) listings found")


def kind(row):
    return next((t for t in TYPES if str(row.get(t) or "").strip() == "Yes"), "")


def admits(job):
    return str(TARGET) in (job.get("expected_graduation") or "")


open_rows = []
for job_id, job in jobs.items():
    if not admits(job):
        continue
    row = after.get(job_id, {})
    deadline = job.get("deadline")
    closed = job.get("open_status") == "CLOSED" or (deadline and deadline < TODAY.isoformat())
    open_rows.append({
        "id": job_id, "company": job["company"], "title": job["job_title"], "type": kind(row) or job.get("work_type") or "",
        "grad": job.get("expected_graduation"), "deadline": deadline, "closed": bool(closed),
        "status": row.get("Application Status") or "", "evidence": job.get("year_of_study") or "",
        "url": job.get("application_url") or "", "location": job.get("location") or "",
    })
live = sorted([r for r in open_rows if not r["closed"]], key=lambda r: (r["deadline"] is None, r["deadline"] or ""))
past = [r for r in open_rows if r["closed"]]
with_years = sum(1 for j in jobs.values() if j.get("expected_graduation"))
type_counts = Counter(r["type"] for r in live)


def days_left(deadline):
    if not deadline:
        return None
    return (date.fromisoformat(deadline) - TODAY).days


def role_card(r):
    left = days_left(r["deadline"])
    if left is None:
        when, urgency = "No deadline stated", "none"
    elif left <= 7:
        when, urgency = f"{date.fromisoformat(r['deadline']):%a %d %b} &middot; {left} days", "soon"
    else:
        when, urgency = f"{date.fromisoformat(r['deadline']):%d %b %Y} &middot; {left} days", "later"
    type_class = {"Spring Week": "spring", "Summer Internship": "summer", "Industrial Placement": "placement"}.get(r["type"], "other")
    status = f'<span class="status">{esc(r["status"])}</span>' if r["status"] else ""
    link = f'<a class="apply" href="{esc(r["url"])}">Open posting</a>' if r["url"].startswith("http") else ""
    return f"""
    <article class="role">
      <div class="role-top">
        <span class="type {type_class}">{esc(r['type'] or 'Type not stated')}</span>
        <span class="due {urgency}">{when}</span>
      </div>
      <h3>{esc(r['company'])}</h3>
      <p class="title">{esc(r['title'])}</p>
      <p class="meta"><span class="mono">{esc(r['id'])}</span> &middot; {esc(r['location'])} &middot; graduating {esc(r['grad'])} {status}</p>
      <blockquote>{esc(r['evidence'])}</blockquote>
      {link}
    </article>"""


blackrock = [(k, r) for k, r in sorted(after.items()) if r.get("Company") == "BlackRock"]
blackrock_rows = "".join(
    f"<tr><td class='mono'>{esc(k)}</td><td>{esc(r.get('Job Title'))}</td><td>{esc(kind(r))}</td>"
    f"<td class='mono'>{esc(r.get('Expected Graduation') or '-')}</td><td class='mono'>{esc(str(r.get('Deadline') or '')[:10])}</td>"
    f"<td>{esc(r.get('Application Status') or '')}</td></tr>" for k, r in blackrock)

page = f"""<title>Graduating 2029: Scan Report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,600;8..60,700&family=Public+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
:root {{
  --ground: #F4F6F3; --surface: #FFFFFF; --ink: #1B2230; --muted: #5A6472; --rule: #D9DED8;
  --accent: #1F3864; --ok: #2E7D4F; --warn: #B4540C; --bad: #B3261E;
  --summer: #C65911; --summer-bg: #FCE4D6; --placement: #7030A0; --placement-bg: #E9E0F1;
  --spring: #31859C; --spring-bg: #DAEEF3; --quote: #EEF1EC;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground: #12161D; --surface: #1A202A; --ink: #E7EAEE; --muted: #9AA3AE; --rule: #2C3440;
    --accent: #9DB7E6; --ok: #6CC596; --warn: #F0A35E; --bad: #F08A83;
    --summer: #F2A36F; --summer-bg: #3A2518; --placement: #C9A2EA; --placement-bg: #2C2138;
    --spring: #7FC6D8; --spring-bg: #16303A; --quote: #202834;
  }}
}}
:root[data-theme="dark"] {{
  --ground: #12161D; --surface: #1A202A; --ink: #E7EAEE; --muted: #9AA3AE; --rule: #2C3440;
  --accent: #9DB7E6; --ok: #6CC596; --warn: #F0A35E; --bad: #F08A83;
  --summer: #F2A36F; --summer-bg: #3A2518; --placement: #C9A2EA; --placement-bg: #2C2138;
  --spring: #7FC6D8; --spring-bg: #16303A; --quote: #202834;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--ground); color: var(--ink); font: 15px/1.55 "Public Sans", system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 44rem; margin: 0 auto; padding: 1.5rem 1rem 3rem; display: grid; gap: 2rem; }}
h1, h2, h3 {{ font-family: "Source Serif 4", Georgia, "Times New Roman", serif; text-wrap: balance; margin: 0; }}
h1 {{ font-size: 2rem; line-height: 1.15; }}
h2 {{ font-size: 1.3rem; margin-bottom: .75rem; }}
h3 {{ font-size: 1.1rem; }}
.mono {{ font-family: "JetBrains Mono", ui-monospace, Consolas, monospace; font-size: .85em; font-variant-numeric: tabular-nums; }}
.eyebrow {{ font-size: .72rem; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 600; }}
header {{ display: grid; gap: .5rem; }}
.lede {{ color: var(--muted); margin: 0; max-width: 60ch; }}
.facts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: .5rem; margin-top: .5rem; }}
.fact {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; padding: .6rem .75rem; }}
.fact b {{ display: block; font-family: "Source Serif 4", Georgia, serif; font-size: 1.5rem; font-variant-numeric: tabular-nums; }}
.fact span {{ color: var(--muted); font-size: .8rem; }}
.safety {{ display: flex; gap: .6rem; align-items: baseline; padding: .75rem .9rem; border-radius: 8px; border: 1px solid var(--rule); background: var(--surface); }}
.safety .dot {{ width: .6rem; height: .6rem; border-radius: 50%; flex: none; background: var(--ok); transform: translateY(-.05rem); }}
.safety.bad .dot {{ background: var(--bad); }}
.roles {{ display: grid; gap: .75rem; }}
.role {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; padding: .9rem 1rem; display: grid; gap: .3rem; }}
.role-top {{ display: flex; justify-content: space-between; gap: .5rem; flex-wrap: wrap; align-items: center; }}
.type {{ font-size: .75rem; font-weight: 600; padding: .1rem .5rem; border-radius: 999px; }}
.type.spring {{ color: var(--spring); background: var(--spring-bg); }}
.type.summer {{ color: var(--summer); background: var(--summer-bg); }}
.type.placement {{ color: var(--placement); background: var(--placement-bg); }}
.type.other {{ color: var(--muted); background: var(--quote); }}
.due {{ font-size: .8rem; font-variant-numeric: tabular-nums; color: var(--muted); }}
.due.soon {{ color: var(--bad); font-weight: 600; }}
.title {{ margin: 0; }}
.meta {{ margin: 0; color: var(--muted); font-size: .85rem; }}
.status {{ margin-left: .3rem; color: var(--accent); font-weight: 600; }}
blockquote {{ margin: .3rem 0 0; padding: .45rem .7rem; background: var(--quote); border-radius: 6px; font-size: .85rem; color: var(--ink); overflow-wrap: anywhere; }}
.apply {{ justify-self: start; margin-top: .2rem; color: var(--accent); font-weight: 600; text-decoration: none; }}
.apply:hover, .apply:focus-visible {{ text-decoration: underline; }}
a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
.table-wrap {{ overflow-x: auto; background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; }}
table {{ border-collapse: collapse; width: 100%; font-size: .85rem; min-width: 34rem; }}
th, td {{ text-align: left; padding: .5rem .6rem; border-bottom: 1px solid var(--rule); vertical-align: top; }}
th {{ color: var(--muted); font-weight: 600; font-size: .72rem; letter-spacing: .06em; text-transform: uppercase; }}
tr:last-child td {{ border-bottom: 0; }}
ul.plain {{ margin: 0; padding-left: 1.1rem; display: grid; gap: .35rem; }}
.muted {{ color: var(--muted); }}
details summary {{ cursor: pointer; font-weight: 600; }}
</style>
<main>
  <header>
    <span class="eyebrow">Internship scan &middot; finished {saved_at:%a %d %b, %H:%M}</span>
    <h1>{len(live)} open roles take students graduating in {TARGET}</h1>
    <p class="lede">Read from each employer's own posting: the graduation year they state, or the year of study they name for a dated intake. Spring Week is marked for any of these that is not a placement.</p>
    <div class="facts">
      <div class="fact"><b>{len(jobs)}</b><span>roles in your sheet</span></div>
      <div class="fact"><b>{with_years}</b><span>with a graduation year</span></div>
      <div class="fact"><b>{type_counts.get('Spring Week', 0)}</b><span>spring weeks for {TARGET}</span></div>
      <div class="fact"><b>{type_counts.get('Industrial Placement', 0)}</b><span>placements for {TARGET}</span></div>
    </div>
  </header>

  <div class="safety{'' if safe else ' bad'}">
    <span class="dot" aria-hidden="true"></span>
    <p style="margin:0">{'All ' + str(checked) + ' application statuses and notes you set are unchanged.' if safe else f'Check your sheet: {lost} rows with your status are missing and {changed} statuses changed.'}</p>
  </div>

  <section>
    <h2>Open to {TARGET} graduates, soonest first</h2>
    <div class="roles">{''.join(role_card(r) for r in live) or '<p class="muted">None found yet.</p>'}</div>
  </section>

  <section>
    <h2>BlackRock</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>ID</th><th>Programme</th><th>Type</th><th>Grad</th><th>Deadline</th><th>Your status</th></tr></thead>
      <tbody>{blackrock_rows}</tbody>
    </table></div>
  </section>

  <section>
    <h2>How the scan went</h2>
    <ul class="plain">
      <li>Sheet: {esc(excel_line or 'written')}.</li>
      <li>Company careers sites: {esc(careers_found or '?')} postings read, including Oleeo sites (BlackRock, Morgan Stanley, Nomura, Jefferies) at their 10-second crawl delay.</li>
      <li>Employer pages: {esc(eligibility_line or 'not reported')}.</li>
      <li>Skipped at an anti-bot check, never bypassed: {esc(', '.join(blocked + ['Bank of America']) if blocked else 'Bank of America')}.</li>
      <li class="muted">Search took {int(completed) // 60 if completed else '?'} minutes. The daily schedule is still switched off.</li>
    </ul>
  </section>

  {'<section><details><summary>' + str(len(past)) + ' more for ' + str(TARGET) + ' that are closed or past their deadline</summary><div class="roles" style="margin-top:.75rem">' + ''.join(role_card(r) for r in past) + '</div></details></section>' if past else ''}
</main>
"""
out = SCRATCH / "scan-report.html"
out.write_text(page, encoding="utf-8")
print(f"report written: {out} | live roles {len(live)} | past {len(past)} | safe {safe} ({checked} cells)")
