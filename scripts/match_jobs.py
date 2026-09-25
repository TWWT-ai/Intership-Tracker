"""Score the last run against the candidate profile.

Thin wrapper over tracker.match, so the scoring the pipeline uses and the
scoring you can inspect here are the same code.

    python -m scripts.match_jobs            # score and print
    python -m scripts.match_jobs --write    # also update the workbook
"""

from __future__ import annotations

import sys

from scripts.deduplicate_jobs import load
from tracker import config, excel, match
from tracker.models import Priority


def main(argv: list[str]) -> int:
    jobs = load()
    if not jobs:
        print("No jobs found. Run `python main.py daily` first.")
        return 1

    profile = config.load_profile()
    filters = config.load("search_filters")
    weights = config.load("matching_weights")
    configured = config.profile_is_configured(profile)

    for job in jobs:
        job.eligible, reasons = match.check_eligibility(job, profile, filters)
        match.score(job, profile, filters, weights)
        if reasons:
            job.concerns.extend(reasons)
        match.assign_priority(job, weights)

    ordered = excel.sort_jobs(jobs)

    print(f"Scored {len(jobs)} jobs against "
          f"{'your profile' if configured else 'role relevance only'}")
    print()

    for priority in (Priority.HIGH, Priority.MEDIUM, Priority.LOW):
        group = [j for j in ordered if j.priority is priority]
        if not group:
            continue
        print(f"{priority} ({len(group)})")
        for job in group[:20]:
            print(f"  {job.match_score:3}  {job.company[:24]:24} {job.job_title[:44]:44} "
                  f"{job.deadline or '-'}")
            if job.strengths:
                print(f"       + {', '.join(job.strengths[:6])}")
            if job.missing_skills:
                print(f"       - {', '.join(job.missing_skills[:5])}")
        if len(group) > 20:
            print(f"  ... and {len(group) - 20} more")
        print()

    if not configured:
        print("data/profile.json is not configured. Scores measure role relevance only,")
        print("are capped at 60, and strengths/missing skills are left empty rather than")
        print("guessed. Run `python main.py profile --from-cv <path>` to draft one.")

    if "--write" in argv:
        path = excel.write(jobs)
        print(f"\nWorkbook updated: {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
