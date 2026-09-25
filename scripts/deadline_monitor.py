"""Report internships closing soon.

Thin wrapper over tracker.deadlines. Safe to schedule on its own (it reads the
last run and makes no network requests), which is useful as a midday reminder
separate from the morning sync.

    python -m scripts.deadline_monitor            # next 7 days
    python -m scripts.deadline_monitor 14         # next 14 days
"""

from __future__ import annotations

import sys

from scripts.deduplicate_jobs import load
from tracker import deadlines
from tracker.models import OpenStatus


def main(argv: list[str]) -> int:
    days = int(argv[0]) if argv and argv[0].isdigit() else 7

    jobs = load()
    if not jobs:
        print("No jobs found. Run `python main.py daily` first.")
        return 1

    print(deadlines.report(jobs, within_days=days))
    print()

    buckets = deadlines.bucket(jobs)
    if buckets:
        print("All tracked deadlines:")
        for label, group in buckets.items():
            print(f"  {label:16} {len(group)}")

    unknown = [j for j in jobs if j.open_status is OpenStatus.UNKNOWN and j.deadline is None]
    if unknown:
        print()
        print(f"{len(unknown)} job(s) have no deadline and could not be confirmed open.")
        print("Run `python main.py verify` to re-check them against the real job pages.")

    # A non-zero exit when something is closing today makes this usable as a
    # scheduled task that only notifies when it matters.
    return 2 if buckets.get("Today") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
