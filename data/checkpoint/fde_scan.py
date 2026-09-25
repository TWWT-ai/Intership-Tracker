"""Sweep every known careers board for Forward-Deployed Engineer roles.

The pipeline keeps student programmes only, so an FDE posting - a graduate or
experienced role - is filtered out even when a board lists it. This reads the
same boards through the same robots-guarded readers, with one difference: the
title filter is FDE, not "student + tech". Nothing is invented; a row is written
only for a posting a board actually returned.

Writes data/checkpoint/fde-roles.csv. The sheet is written by fde_sheet.py.
"""
import csv
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from sources import careers  # noqa: E402
from sources.fetch import Session, SourceBlocked, SourceError  # noqa: E402
from tracker import config  # noqa: E402

OUT = ROOT / "data" / "checkpoint" / "fde-roles.csv"

#: The role, as employers spell it. "Deployment strategist" is Palantir's own
#: name for the same job, and is recorded as a related title, not as an FDE.
FDE = re.compile(r"forward[\s-]?deployed|\bfde\b|forward deployment", re.IGNORECASE)
RELATED = re.compile(r"deployment strategist|deployed engineer", re.IGNORECASE)

LEVELS = [(re.compile(r"\bintern(ship)?\b|placement|summer analyst", re.I), "Internship"),
          (re.compile(r"\bnew ?grad|\bgraduate\b|university|campus|\bentry\b", re.I), "Graduate"),
          (re.compile(r"\bsenior\b|\bstaff\b|\bprincipal\b|\blead\b|\bmanager\b|\bdirector\b|\bhead\b", re.I),
           "Senior"),
          (re.compile(r".*"), "Experienced")]

#: Employers known to hire FDEs whose board no listing in the sheet points at.
#: Each URL is checked before it is read; one that does not resolve is reported,
#: never guessed at.
CANDIDATES = [
    ("Palantir", "https://jobs.lever.co/palantir"),
    ("OpenAI", "https://jobs.ashbyhq.com/openai"),
    ("Anthropic", "https://job-boards.greenhouse.io/anthropic"),
    ("Scale AI", "https://job-boards.greenhouse.io/scaleai"),
    ("Sierra", "https://jobs.ashbyhq.com/sierra"),
    ("Harvey", "https://jobs.ashbyhq.com/harvey"),
    ("Glean", "https://job-boards.greenhouse.io/gleanwork"),
    ("Databricks", "https://job-boards.greenhouse.io/databricks"),
    ("Anysphere (Cursor)", "https://jobs.ashbyhq.com/anysphere"),
    ("Decagon", "https://jobs.ashbyhq.com/decagon"),
    ("Together AI", "https://jobs.ashbyhq.com/togetherai"),
    ("Mistral AI", "https://jobs.lever.co/mistral"),
    ("LangChain", "https://jobs.ashbyhq.com/langchain"),
    ("Baseten", "https://jobs.ashbyhq.com/baseten"),
    ("Hebbia", "https://jobs.ashbyhq.com/hebbia"),
    ("Ramp", "https://jobs.ashbyhq.com/ramp"),
    ("Vercel", "https://jobs.ashbyhq.com/vercel"),
    ("Writer", "https://job-boards.greenhouse.io/writer"),
    ("Abridge", "https://jobs.ashbyhq.com/abridge"),
    ("Sardine", "https://jobs.ashbyhq.com/sardine"),
    ("Rogo", "https://jobs.ashbyhq.com/rogo"),
    ("Snowflake", "https://job-boards.greenhouse.io/snowflake"),
    ("Sword Health", "https://jobs.lever.co/swordhealth"),
    ("Applied Intuition", "https://job-boards.greenhouse.io/appliedintuition"),
    ("Sigma Computing", "https://job-boards.greenhouse.io/sigmacomputing"),
]


class FDEOnly(careers._Filter):
    """The board's own filter, with the role swapped for FDE."""

    def screen(self, title, company="", employment=""):
        return bool(FDE.search(title) or RELATED.search(title))

    def keep(self, p, company=""):
        return self.screen(p.get("title") or "")

    def for_graduate(self, p, target_year):
        return False


def level(title: str) -> str:
    return next(name for pattern, name in LEVELS if pattern.search(title))


settings = dict(config.load("sources")["sources"]["careers"],
                workday_queries=["forward deployed", "fde"],
                search_queries=["forward deployed", "fde"],
                workday_max_pages=3, search_max_pages=3)
wanted = FDEOnly(settings)
platforms = {"greenhouse", "lever", "ashby", "workable", "workday", "oracle", "amazon"}

boards = {b.key: b for b in careers.discover_boards(settings) if b.platform in platforms}
extra, unresolved = [], []
for company, url in CANDIDATES:
    board = careers.board_from_url(url, company)
    if board is None:
        unresolved.append((company, url, "URL is not a board this tool can read"))
    elif board.key not in boards:
        extra.append(board)
print(f"boards from your sheet: {len(boards)} | extra FDE employers to try: {len(extra)}")

rows, failed = [], []
with Session() as session:
    for seconds, hosts in [(settings.get("rate_limit_seconds") or 1.0,
                            ["boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co",
                             "api.ashbyhq.com", "apply.workable.com"])]:
        for host in hosts:
            session.limiter.configure(host, seconds)
    for board in list(boards.values()) + extra:
        try:
            postings = careers._READERS[board.platform](session, board, settings, wanted)
        except (SourceError, SourceBlocked) as exc:
            failed.append((board.company, board.key, str(exc)[:120]))
            continue
        for p in postings:
            title = p.get("title") or ""
            if not (FDE.search(title) or RELATED.search(title)):
                continue
            rows.append({
                "Company": board.company,
                "Job Title": title,
                "Location": p.get("location") or "",
                "Level": level(title),
                "Exact FDE title": "Yes" if FDE.search(title) else "No - related role",
                "Team": p.get("team") or "",
                "Platform": careers.PLATFORM_NAMES[board.platform],
                "Application URL": p.get("apply_url") or p.get("url") or "",
                "What the posting says": " ".join((p.get("description") or "").split())[:600],
            })
        print(f"  {board.company} ({board.key}): {len(postings)} postings, "
              f"{sum(1 for r in rows if r['Company'] == board.company)} FDE so far")

rows.sort(key=lambda r: (r["Company"].casefold(), r["Job Title"].casefold()))
with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else
                       ["Company", "Job Title", "Location", "Level", "Exact FDE title", "Team",
                        "Platform", "Application URL", "What the posting says"])
    w.writeheader()
    w.writerows(rows)

print(f"\nFDE roles found: {len(rows)} at {len({r['Company'] for r in rows})} companies -> {OUT}")
print(f"boards that could not be read: {len(failed)}")
for f in failed[:15]:
    print("   ", f)
for u in unresolved:
    print("   unresolved candidate:", u)
