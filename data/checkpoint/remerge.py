"""Re-merge the saved run after company aliases change. Offline.

Recomputes each row's company name, deduplicates again, and stops without
writing if a row about to be absorbed holds any status or note the user set.
Merged rows have their eligibility re-settled from their combined facts.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(r"D:\School\Personal\Intership tracker")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from tracker import config, dedupe, eligibility, excel, normalize, pipeline  # noqa: E402
from tracker.models import ApplicationStatus, Job  # noqa: E402

jobs = [Job.from_dict(d) for d in json.loads((config.STATE_DIR / "last_run.json").read_text(encoding="utf-8"))["jobs"]]
state = excel.read_user_state()
before_ids = [j.id for j in jobs]

for job in jobs:
    job.company, job.normalized_company = normalize.company(job.company)

merged, count = dedupe.deduplicate(jobs)
kept = {j.id for j in merged}
absorbed = [j for j in jobs if j.id not in kept]


def touched(job: Job) -> bool:
    saved = {k: v for k, v in state.get(job.id, {}).items() if str(v or "").strip()}
    return bool(saved) or job.status is not ApplicationStatus.FOUND or bool(job.notes)


blocked = [j for j in absorbed if touched(j)]
if blocked:
    print("STOPPED - these rows carry your status or notes and would be absorbed:")
    for j in blocked:
        print("  ", j.id, j.company, "|", j.job_title)
    sys.exit(2)

survivor_of = {}
for job in merged:
    for other in absorbed:
        if dedupe.merge_key(other) == dedupe.merge_key(job) and any(
                ref.external_id and ref.external_id in {r.external_id for r in job.sources} for ref in other.sources):
            survivor_of[other.id] = job.id
print(f"{count} rows merged: {len(before_ids)} -> {len(merged)}")
for other in absorbed:
    print(f"  {other.id} ({other.company} | {other.job_title[:50]}) -> {survivor_of.get(other.id, '?')}")

profile = config.load_profile()
dedupe.assign_ids(merged)
for job in merged:
    eligibility.settle(job, profile)
pipeline._score_all(merged, profile, config.load("search_filters"), config.load("matching_weights"))
written = excel.write(merged)
pipeline._save_previous(merged)
print("workbook written:", written)
