"""Deduplicate a list of jobs. Thin wrapper over tracker.dedupe.

The real logic lives in tracker/dedupe.py so the pipeline and this script can
never drift apart. Run it to inspect merges without touching the workbook:

    python -m scripts.deduplicate_jobs                 # the last run
    python -m scripts.deduplicate_jobs jobs.json       # a saved snapshot
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tracker import config, dedupe
from tracker.models import Job


def load(path: Path | None = None) -> list[Job]:
    path = path or (config.STATE_DIR / "last_run.json")
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("jobs", payload) if isinstance(payload, dict) else payload
    return [Job.from_dict(r) for r in records]


def main(argv: list[str]) -> int:
    jobs = load(Path(argv[0]) if argv else None)
    if not jobs:
        print("No jobs found. Run `python main.py daily` first.")
        return 1

    merged, duplicates = dedupe.deduplicate(jobs)
    dedupe.assign_ids(merged)

    print(f"{len(jobs)} listings -> {len(merged)} unique ({duplicates} merged)")
    print()

    multi = [j for j in merged if len(j.source_names()) > 1]
    if not multi:
        print("No job was found on more than one platform.")
        return 0

    print(f"{len(multi)} job(s) found on more than one platform:")
    for job in multi:
        print(f"\n{job.id}  {job.company} - {job.job_title}")
        for ref in job.sources:
            marker = "*" if ref.source == job.primary_source else " "
            print(f"  {marker} {ref.source:15} {ref.source_url or '(no listing url)'}")
    print("\n* = primary source (the platform the row is attributed to)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
