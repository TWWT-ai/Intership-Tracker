"""After the scan: correct company-site rows that show a non-UK city.

The running scan loaded the old UK filter and city picker. The picker showed the
first city of a comma-separated office list ("Abu Dhabi, Amsterdam, ..., London");
the filter read "New South Wales" as Wales, letting Sydney roles in. This re-reads
the real location of only the rows showing a city not recognised as UK - each
Oleeo posting page at its crawl delay, each Greenhouse board once - then:

* UK location: shows the UK city, picked by the fixed picker;
* not in the UK: the row is removed, as a fixed scan would never have kept it,
  unless the user set a status or note on it, in which case it stays and is
  reported;
* posting gone or unreadable: left as it is, reported.

The workbook is rewritten with user state restored by ID.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(r"D:\School\Personal\Intership tracker")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from sources import careers  # noqa: E402
from sources.fetch import Session, SourceError  # noqa: E402
from tracker import config, excel, normalize, pipeline  # noqa: E402
from tracker.models import ApplicationStatus, Job  # noqa: E402

settings = config.load("sources")["sources"]["careers"]
wanted = careers._Filter(settings)
jobs = [Job.from_dict(d) for d in json.loads((config.STATE_DIR / "last_run.json").read_text(encoding="utf-8"))["jobs"]]
workbook_state = excel.read_user_state()


def user_touched(job: Job) -> bool:
    saved = {k: v for k, v in workbook_state.get(job.id, {}).items() if str(v or "").strip()}
    return bool(saved) or job.status is not ApplicationStatus.FOUND or bool(job.notes)


suspects = [j for j in jobs
            if j.primary_source == "careers" and j.location and not wanted.is_uk(j.location)
            and any(s.source == "careers" and s.via in ("Oleeo", "Greenhouse") for s in j.sources)]
print(f"company-site rows showing a city not recognised as UK: {len(suspects)}")

fixed, correct, removed, kept_abroad, left = [], [], [], [], []
with Session() as session:
    greenhouse_boards: dict[str, dict[str, str]] = {}
    for job in suspects:
        ref = next(s for s in job.sources if s.source == "careers")
        try:
            if ref.via == "Oleeo":
                host = ref.external_id.split(":")[1]
                session.limiter.configure(host, float(settings.get("oleeo_rate_limit_seconds", 10)))
                fields = careers._oleeo_fields(careers._oleeo_page(session, job.application_url))
                place = fields.get("Cities") or fields.get("City") or fields.get("Location") or ""
                country = fields.get("Countries") or fields.get("Region") or ""
                raw = "; ".join(filter(None, (place, country)))
            else:
                token, posting_id = ref.external_id.split(":")[1], ref.external_id.split(":")[-1]
                if token not in greenhouse_boards:
                    board = careers.Board("greenhouse", (token,), job.company)
                    greenhouse_boards[token] = {p["id"]: p["location"]
                                                for p in careers._greenhouse(session, board, settings, wanted)}
                raw = greenhouse_boards[token].get(posting_id)
        except SourceError as exc:
            left.append((job, f"could not re-read: {exc}"))
            continue

        if not raw:
            left.append((job, "posting no longer listed"))
        elif not wanted.is_uk(raw):
            (kept_abroad if user_touched(job) else removed).append((job, raw))
        else:
            shown, _ = normalize.location(careers._first_uk_location(raw, wanted))
            if shown and shown != job.location:
                fixed.append((job, job.location, shown))
                job.location = shown
            else:
                correct.append(job)

for job, old, new in fixed:
    print(f"  fixed    {job.id} | {job.company[:22]:22} | {job.job_title[:48]:48} | {old} -> {new}")
for job in correct:
    print(f"  correct  {job.id} | {job.company[:22]:22} | {job.job_title[:48]:48} | {job.location}")
for job, raw in removed:
    print(f"  removed  {job.id} | {job.company[:22]:22} | {job.job_title[:48]:48} | not UK: {raw[:60]}")
for job, raw in kept_abroad:
    print(f"  KEPT     {job.id} | {job.company[:22]:22} | {job.job_title[:48]:48} | not UK but you set a status/note: {raw[:50]}")
for job, why in left:
    print(f"  left     {job.id} | {job.company[:22]:22} | {job.job_title[:48]:48} | {job.location} | {why}")

if fixed or removed:
    gone = {id(job) for job, _ in removed}
    jobs = [j for j in jobs if id(j) not in gone]
    written = excel.write(jobs)
    pipeline._save_previous(jobs)
    print(f"workbook rewritten: {written} ({len(jobs)} rows)")
else:
    print("nothing to rewrite")
