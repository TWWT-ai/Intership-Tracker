"""Internship tracker CLI.

    python main.py daily                    the scheduled workflow
    python main.py search --source trackr   one source, no Excel write
    python main.py verify                   re-check deadlines / open status
    python main.py eligibility              read graduation years off employer postings
    python main.py deadlines                what closes soon
    python main.py job INT-000123           one job in detail
    python main.py prep INT-000123          review packet for one job (never submits)
    python main.py ingest --file page.html --source gradcracker
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
import sys
from datetime import date
from pathlib import Path

import click

from tracker import (config, deadlines as deadline_mod, dedupe, eligibility, excel, graduation, match,
                     pipeline, prep, programme, verify)
from tracker.models import ApplicationStatus, Job, OpenStatus, Priority

echo = pipeline.echo


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-v", "--verbose", is_flag=True, help="Show debug output on the console.")
def cli(verbose: bool) -> None:
    """Search UK internships, rank them against your profile, and keep
    data/internships.xlsx up to date. Never submits an application."""
    pipeline.setup_logging(verbose)


# ---------------------------------------------------------------------------
# Main workflows
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--source", type=click.Choice(sorted(pipeline.ADAPTERS)), help="Limit to one source.")
@click.option("--skip-verify", is_flag=True, help="Do not open job pages to check deadlines.")
@click.option("--dry-run", is_flag=True, help="Run everything but do not write Excel.")
def daily(source: str | None, skip_verify: bool, dry_run: bool) -> None:
    """The scheduled daily workflow. Safe to run repeatedly."""
    pipeline.run_daily(only=source, skip_verify=skip_verify, dry_run=dry_run)


@cli.command("run-all")
@click.pass_context
def run_all(ctx: click.Context) -> None:
    """The full pipeline, verification included. Same as `daily`."""
    ctx.invoke(daily, source=None, skip_verify=False, dry_run=False)


@cli.command()
@click.option("--source", type=click.Choice(sorted(pipeline.ADAPTERS)), help="Limit to one source.")
@click.option("--limit", type=int, default=0, help="Show at most this many results.")
def search(source: str | None, limit: int) -> None:
    """Search sources and print what was found, without touching Excel."""
    from sources.fetch import Session

    with Session() as session:
        jobs, ok, failed = pipeline.collect(session, only=source)

    merged, duplicates = dedupe.deduplicate(jobs)
    echo("")
    echo(f"{len(jobs)} listings, {duplicates} merged -> {len(merged)} unique")
    echo("")
    _print_table(merged[:limit] if limit else merged)

    if failed:
        echo("")
        echo(f"Sources that failed: {', '.join(failed)}")


@cli.command()
@click.option("--limit", type=int, default=0, help="Verify at most this many jobs.")
def verify_cmd(limit: int) -> None:
    """Re-check open/closed status and deadlines on the real job pages."""
    from sources.fetch import Session

    jobs = _load_last_run()
    if not jobs:
        echo("No previous run found. Run `python main.py daily` first.")
        return

    settings = config.load("verification")
    if limit:
        settings = {**settings, "budget": {**settings.get("budget", {}), "max_verifications": limit}}

    with Session() as session:
        stats = verify.verify_all(session, jobs, settings)

    echo(f"[Verify] {stats.summary()}")
    for job in jobs:
        if job.open_status is OpenStatus.CLOSED:
            echo(f"  CLOSED  {job.id} {job.company} - {job.job_title}")

    excel.write(jobs)
    pipeline._save_previous(jobs)
    echo(f"Workbook updated: {config.WORKBOOK}")


cli.add_command(verify_cmd, name="verify")


@cli.command("eligibility")
@click.option("--limit", type=int, default=0, help="Read at most this many employer postings.")
def eligibility_cmd(limit: int) -> None:
    """Read who may apply off each open row's own employer posting, for rows
    no feed described, then rewrite the workbook. No new search."""
    from sources.fetch import Session

    jobs = _load_last_run()
    if not jobs:
        echo("No previous run found. Run `python main.py daily` first.")
        return

    profile = config.load_profile()
    with Session() as session:
        summary = verify.sweep_eligibility(session, jobs, config.load("verification"), profile, limit=limit)
    echo(f"[Eligibility] {summary}")

    filters, weights = config.load("search_filters"), config.load("matching_weights")
    pipeline._score_all(jobs, profile, filters, weights)
    written = excel.write(jobs)
    pipeline._save_previous(jobs)

    target = profile.get("graduation_year")
    if target:
        admitted = [j for j in jobs if str(target) in (j.expected_graduation or "")]
        echo(f"{len(admitted)} rows admit graduation year {target}")
    verdicts = Counter(j.eligible_for_you or "not assessed" for j in jobs)
    echo("Eligible for you: " + ", ".join(f"{k} {v}" for k, v in verdicts.most_common()))
    echo(f"Workbook updated: {written}")


# ---------------------------------------------------------------------------
# Pipeline stages, runnable on their own
# ---------------------------------------------------------------------------


@cli.command()
def normalize() -> None:
    """Show how the last run's titles and companies were normalized."""
    jobs = _load_last_run()
    if not jobs:
        echo("No previous run found.")
        return
    for job in jobs[:60]:
        echo(f"{job.company:26} | {job.job_title[:44]:44}")
        echo(f"  -> {job.normalized_company:22} | {job.normalized_title}")


@cli.command()
def deduplicate() -> None:
    """Show which jobs were found on more than one platform."""
    jobs = _load_last_run()
    if not jobs:
        echo("No previous run found.")
        return

    multi = [j for j in jobs if len(j.source_names()) > 1]
    echo(f"{len(multi)} of {len(jobs)} jobs were found on more than one platform")
    echo("")
    for job in multi:
        echo(f"{job.id}  {job.company} - {job.job_title}")
        for ref in job.sources:
            echo(f"    {ref.source:15} {ref.source_url or '(no listing url)'}")


@cli.command("match")
def match_cmd() -> None:
    """Re-score the last run against the current profile, weights and
    programme rules, without searching again."""
    jobs = _load_last_run()
    if not jobs:
        echo("No previous run found.")
        return

    profile = config.load_profile()
    filters, weights = config.load("search_filters"), config.load("matching_weights")
    # config/programmes.yaml may have changed too: re-read each row's stated
    # eligibility, which only ever acts on evidence.
    for job in jobs:
        programme.refine(job, f"{job.year_of_study}. {job.description}")
        graduation.apply(job, f"{job.year_of_study}. {job.description}", profile)
        eligibility.reclassify(job)
        eligibility.assess(job, job.description, profile)
    pipeline._score_all(jobs, profile, filters, weights)

    excel.write(jobs)
    pipeline._save_previous(jobs)
    _print_table(excel.sort_jobs(jobs)[:40])

    if not config.profile_is_configured(profile):
        echo("")
        echo("data/profile.json is not configured - scores are relevance-only and capped at 60.")


@cli.command()
@click.option("--days", type=int, default=7, help="Look this many days ahead.")
def deadlines(days: int) -> None:
    """Applications closing soon."""
    jobs = _load_last_run()
    if not jobs:
        echo("No previous run found.")
        return
    echo(deadline_mod.report(jobs, within_days=days))


@cli.command()
def applications() -> None:
    """Your application funnel, as recorded in the workbook."""
    jobs = _load_last_run()
    tracked = [j for j in jobs if str(j.status) != "FOUND"]
    if not tracked:
        echo("No applications tracked yet.")
        echo("Set the Application Status column in data/internships.xlsx as you apply.")
        return

    by_status: dict[str, list[Job]] = {}
    for job in tracked:
        by_status.setdefault(str(job.status), []).append(job)

    for status in ApplicationStatus:
        group = by_status.get(str(status), [])
        if not group:
            continue
        echo(f"{status} ({len(group)})")
        for job in group:
            echo(f"  {job.id}  {job.company} - {job.job_title}")
        echo("")


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("job_id")
def job(job_id: str) -> None:
    """Everything known about one internship, e.g. INT-000123."""
    jobs = _load_last_run()
    found = next((j for j in jobs if j.id.upper() == job_id.upper()), None)
    if found is None:
        echo(f"No job with id {job_id}. Run `python main.py daily` first.")
        return

    echo(f"{found.id}  {found.company} - {found.job_title}")
    echo("=" * 72)
    rows = [
        ("Priority", f"{found.priority}  (score {found.match_score}, {found.match_category})"),
        ("Open status", f"{found.open_status}  (verified {found.last_verified or 'never'})"),
        ("Deadline", f"{found.deadline}  from {found.deadline_source}"),
        ("Listed deadline", found.listed_deadline),
        ("Location", f"{found.location} {found.work_arrangement}".strip()),
        ("Work type", found.work_type),
        ("Duration", found.duration),
        ("Start date", found.start_date),
        ("Salary", found.salary),
        ("Degree", found.degree_requirements),
        ("Year of study", found.year_of_study),
        ("Experience", found.experience_requirements),
        ("Status", found.status),
        ("Found on", ", ".join(found.source_names())),
        ("Apply", found.application_url),
        ("Listing", found.source_url),
    ]
    for label, value in rows:
        if value not in (None, "", "None"):
            echo(f"{label:16} {value}")

    for label, values in (("Skills", found.skills), ("Languages", found.programming_languages),
                          ("Strengths", found.strengths), ("Missing", found.missing_skills),
                          ("Concerns", found.concerns)):
        if values:
            echo("")
            echo(f"{label}:")
            for value in values:
                echo(f"  - {value}")

    if found.description:
        echo("")
        echo("Description:")
        echo(f"  {found.description[:900]}")


@cli.command()
@click.argument("name")
def company(name: str) -> None:
    """Every tracked internship at one company."""
    jobs = _load_last_run()
    needle = name.casefold()
    found = [j for j in jobs if needle in j.company.casefold()]

    if not found:
        echo(f"No tracked internships at '{name}'.")
        return

    echo(f"{len(found)} internship(s) at companies matching '{name}'")
    echo("")
    _print_table(excel.sort_jobs(found))


# ---------------------------------------------------------------------------
# Manual ingest and export
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--folder", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="A folder of pages saved from your browser; every page is read.")
@click.option("--file", "html_file", type=click.Path(exists=True, path_type=Path),
              help="A search or job page saved from your browser.")
@click.option("--urls", type=click.Path(exists=True, path_type=Path),
              help="A text file of job URLs, one per line.")
@click.option("--csv", "csv_file", type=click.Path(exists=True, path_type=Path),
              help="A CSV export of listings.")
@click.option("--source", type=click.Choice(sorted(pipeline.ADAPTERS)),
              help="Which platform the file came from (required for --file).")
def ingest(folder: Path | None, html_file: Path | None, urls: Path | None, csv_file: Path | None,
           source: str | None) -> None:
    """Add listings from a saved page, URL list or CSV.

    Use this when a platform is blocked: save the page in your own browser and
    hand it over. Ingested jobs go through the same pipeline as scraped ones.
    """
    from sources import manual
    from sources.fetch import Session

    if not any((folder, html_file, urls, csv_file)):
        raise click.UsageError("Give one of --folder, --file, --urls or --csv.")

    jobs: list[Job] = []
    if folder:
        if not source:
            raise click.UsageError("--folder needs --source so the right parser is used.")
        jobs += manual.from_folder(folder, source)
    if html_file:
        if not source:
            raise click.UsageError("--file needs --source so the right parser is used.")
        jobs += manual.from_html(html_file, source)
    if csv_file:
        jobs += manual.from_csv(csv_file, default_source=source or "manual")
    if urls:
        with Session() as session:
            jobs += manual.from_urls(session, urls)

    if not jobs:
        echo("No listings could be read from that input.")
        return

    echo(f"Read {len(jobs)} listings")
    manual.prepare(jobs, config.load_profile())

    existing = _load_last_run()
    merged, duplicates = dedupe.deduplicate(existing + jobs)
    dedupe.assign_ids(merged)

    profile = config.load_profile()
    pipeline._score_all(merged, profile, config.load("search_filters"),
                        config.load("matching_weights"))

    excel.write(merged)
    pipeline._save_previous(merged)
    echo(f"{duplicates} merged with existing rows. Workbook now holds {len(merged)} jobs.")


@cli.command()
@click.argument("job_id")
@click.option("--cv", default="", help="Which CV version you sent.")
@click.option("--notes", default="", help="Anything worth remembering.")
def apply(job_id: str, cv: str, notes: str) -> None:
    """Record an application in your existing Job-Applications-Tracker.xlsx.

    This only logs that you applied. It never submits anything.
    """
    jobs = _load_last_run()
    found = next((j for j in jobs if j.id.upper() == job_id.upper()), None)
    if found is None:
        echo(f"No job with id {job_id}.")
        return

    script = config.env_override("APPLICATION_SCRIPT")
    workbook = config.env_override("APPLICATION_WORKBOOK")

    if not script or not workbook:
        echo("The export bridge is not configured.")
        echo("Set these in .env to enable it:")
        echo("  INTERNSHIP_TRACKER_APPLICATION_SCRIPT=<path to job-tracker tracker.py>")
        echo("  INTERNSHIP_TRACKER_APPLICATION_WORKBOOK=<path to Job-Applications-Tracker.xlsx>")
        echo("")
        echo("For now, here is the row to add by hand:")
        echo(f"  Company: {found.company}")
        echo(f"  Role:    {found.job_title}")
        echo(f"  Source:  {found.primary_source}")
        echo(f"  Link:    {found.application_url}")
        echo(f"  Deadline:{found.deadline}")
        return

    command = [
        sys.executable, str(script), "--file", str(workbook), "add",
        "--company", found.company, "--role", found.job_title,
        "--source", found.primary_source, "--link", found.application_url or found.source_url,
        "--date", date.today().isoformat(), "--status", "Applied",
    ]
    if found.deadline:
        command += ["--deadline", found.deadline.isoformat()]
    if cv:
        command += ["--cv", cv]
    if notes:
        command += ["--notes", notes]

    result = subprocess.run(command, capture_output=True, text=True)
    echo(result.stdout or result.stderr)
    if result.returncode == 0:
        echo(f"Logged. Now set {found.id} to Application Submitted in data/internships.xlsx.")


@cli.command("prep")
@click.argument("job_id")
def prep_cmd(job_id: str) -> None:
    """Build or refresh the review packet for one internship, e.g. INT-000123.

    Writes only data/applications/<ID>/: the tracker's facts as the one brief
    every writing skill reads, plus review.md. Offline. Never writes the
    workbook, never changes a status, never opens the application. The
    workflow stops at your review; applying is yours.

    Exit code: 0 written, nothing to confirm; 2 written, but nothing may be
    drafted until you confirm each CONFIRM line; 1 unknown ID, nothing written.
    """
    jobs = _load_last_run()
    found = next((j for j in jobs if j.id.upper() == job_id.upper()), None)
    if found is None:
        echo(f"No job with id {job_id}. Nothing written.")
        sys.exit(1)

    # The workbook, not last_run.json, holds what the user last set. Read only.
    saved = excel.read_user_state().get(found.id) or {}
    status = (ApplicationStatus.parse(saved[excel.STATUS_COLUMN]) if excel.STATUS_COLUMN in saved
              else found.status)

    packet = prep.write_packet(found, status, config.load_profile())
    echo(f"{found.id}  {found.company} - {found.job_title}")
    echo(f"Packet: {packet.folder}")
    for item in packet.needs_confirmation:
        echo(f"  CONFIRM  {item}")
    for item in packet.advisories:
        echo(f"  CHECK    {item}")
    echo("")
    if packet.needs_confirmation:
        echo("Nothing may be drafted until you confirm each CONFIRM line.")
    else:
        echo("Next: /application-prep drafts into it. Review review.md.")
    echo("Nothing has been submitted. Applying, and setting the status, is yours.")
    if packet.needs_confirmation:
        sys.exit(2)


@cli.command()
@click.option("--evidence", "evidence_file", type=click.Path(path_type=Path), default=None,
              help="The Claude step's output (default data/state/status_evidence.json).")
@click.option("--dry-run", is_flag=True, help="Show every change and review item; write nothing.")
@click.option("--unlock", "unlock_id", default=None, help="Let the sync manage a row you edited by hand again.")
def statuses(evidence_file: Path | None, dry_run: bool, unlock_id: str | None) -> None:
    """Advance Application Status from recruitment emails, forward only.

    Reads the evidence the Gmail step wrote. Moves a row forward only on an
    explicit stage, only when the email matches exactly one row, never past
    Offer, Rejected or Withdrawn, and never on a row you edited by hand.
    Everything uncertain goes to the Status Review sheet instead.

    Exit code: 0 wrote the workbook, 2 nothing needed writing, 3 skipped (busy).
    """
    from tracker import status_sync

    if unlock_id:
        echo(status_sync.unlock_row(unlock_id.upper()))
        return
    result = status_sync.run(evidence_file, dry_run=dry_run)
    echo(result.text)
    if dry_run:
        return
    if result.skipped:
        sys.exit(3)
    sys.exit(0 if result.wrote else 2)


@cli.command()
@click.option("--from-cv", "cv_path", type=click.Path(exists=True, path_type=Path),
              help="A CV file, or a folder of CV variants.")
def profile(cv_path: Path | None) -> None:
    """Show the candidate profile, or draft one from your CV."""
    current = config.load_profile()

    if not cv_path:
        if config.profile_is_configured(current):
            echo(f"Profile configured: {current.get('degree')} at {current.get('university')}")
            echo(f"Graduating {current.get('graduation_year')}, year {current.get('current_year')}")
            skills = match.profile_skills(current)
            echo(f"{len(skills)} skills recorded")
            for name, evidence in sorted(skills.items()):
                echo(f"  {name:22} {evidence}")
        else:
            echo("data/profile.json is NOT configured.")
            echo("Matching runs in relevance-only mode: scores are capped at 60 and")
            echo("strengths/missing skills are left empty rather than guessed.")
            echo("")
            echo("To draft one from your CV:")
            echo('  python main.py profile --from-cv "path\\to\\cv-or-folder"')
        return

    from scripts.analyze_cv import analyze

    draft = analyze(cv_path)
    out = config.DATA_DIR / "profile.draft.json"
    out.write_text(json.dumps(draft, indent=2), encoding="utf-8")

    echo(f"Draft written to {out}")
    echo("")
    echo("NOTHING was inferred beyond what the CV literally contains. Review it,")
    echo("correct it, then copy it over data/profile.json and set")
    echo('"profile_configured": true to switch on full CV matching.')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_last_run() -> list[Job]:
    path = config.STATE_DIR / "last_run.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [Job.from_dict(d) for d in payload.get("jobs", [])]


def _print_table(jobs: list[Job]) -> None:
    if not jobs:
        echo("(nothing to show)")
        return

    echo(f"{'ID':11} {'PRI':7} {'SC':>3} {'COMPANY':22} {'TITLE':40} {'DEADLINE':11} {'OPEN':8} SOURCES")
    echo("-" * 130)
    for job in jobs:
        echo(
            f"{job.id:11} {str(job.priority):7} {str(job.match_score or 0):>3} "
            f"{job.company[:22]:22} {job.job_title[:40]:40} "
            f"{str(job.deadline or '-'):11} {str(job.open_status)[:8]:8} "
            f"{','.join(job.source_names())}"
        )


if __name__ == "__main__":
    cli()
