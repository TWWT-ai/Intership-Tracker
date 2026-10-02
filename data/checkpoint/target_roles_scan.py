"""Sweep company career boards for the three target areas, keeping full descriptions.

Areas: Algorithm Engineer, Forward-Deployed Engineer, AI/ML (and the related
technical roles). Every posting kept is written with its full description, so the
sheet builder can classify on responsibilities rather than on the title.

Boards: every board the workbook already points at, plus AI/robotics/autonomy/
defence employers listed below. A candidate board URL that does not resolve is
reported, never guessed at. Robots rules and rate limits are the pipeline's.

Writes data/checkpoint/target-roles.json.
"""
import json
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

OUT = ROOT / "data" / "checkpoint" / "target-roles.json"

TARGET_TITLE = re.compile(
    r"forward[\s-]?deployed|\bfde\b|deployment strategist"
    r"|algorithm|algorithmic"
    r"|machine learning|\bml\b|\bai\b|artificial intelligence|deep learning|\bllm\b|generative"
    r"|computer vision|perception|autonomy|autonomous|robotic|robotics"
    r"|data scien|research engineer|quantitative develop|quant develop", re.IGNORECASE)
STUDENT_TITLE = re.compile(
    r"intern|internship|placement|year in industry|summer analyst|undergraduate|student"
    r"|spring week|insight|new ?grad|graduate", re.IGNORECASE)

#: Employers in the target areas. Slugs are probed; a 404 is reported.
CANDIDATES = [
    # AI labs and enterprise AI
    ("Palantir", "https://jobs.lever.co/palantir"),
    ("OpenAI", "https://jobs.ashbyhq.com/openai"),
    ("Anthropic", "https://job-boards.greenhouse.io/anthropic"),
    ("Google DeepMind", "https://job-boards.greenhouse.io/deepmind"),
    ("Scale AI", "https://job-boards.greenhouse.io/scaleai"),
    ("Cohere", "https://jobs.ashbyhq.com/cohere"),
    ("Sierra", "https://jobs.ashbyhq.com/sierra"),
    ("Harvey", "https://jobs.ashbyhq.com/harvey"),
    ("Glean", "https://job-boards.greenhouse.io/gleanwork"),
    ("Databricks", "https://job-boards.greenhouse.io/databricks"),
    ("LangChain", "https://jobs.ashbyhq.com/langchain"),
    ("Baseten", "https://jobs.ashbyhq.com/baseten"),
    ("Faculty", "https://job-boards.greenhouse.io/faculty"),
    ("Quantexa", "https://job-boards.greenhouse.io/quantexa"),
    ("Tractable", "https://job-boards.greenhouse.io/tractable"),
    ("Synthesia", "https://job-boards.greenhouse.io/synthesia"),
    ("ElevenLabs", "https://jobs.ashbyhq.com/elevenlabs"),
    ("PolyAI", "https://job-boards.greenhouse.io/polyai"),
    ("Isomorphic Labs", "https://job-boards.greenhouse.io/isomorphiclabs"),
    ("Speechmatics", "https://job-boards.greenhouse.io/speechmatics"),
    # Robotics, autonomy, automotive
    ("Wayve", "https://job-boards.greenhouse.io/wayve"),
    ("Oxa", "https://job-boards.greenhouse.io/oxa"),
    ("Dyson", "https://careers.dyson.com"),
    ("Ocado Technology", "https://job-boards.greenhouse.io/ocadotechnology"),
    ("Improbable", "https://job-boards.greenhouse.io/improbable"),
    ("Cambridge Consultants", "https://job-boards.greenhouse.io/cambridgeconsultants"),
    # Defence technology
    ("Anduril", "https://job-boards.greenhouse.io/andurilindustries"),
    ("Helsing", "https://jobs.ashbyhq.com/helsing"),
    ("Arondite", "https://jobs.ashbyhq.com/arondite"),
    # Semiconductors and infrastructure
    ("Arm", "https://job-boards.greenhouse.io/arm"),
    ("Graphcore", "https://job-boards.greenhouse.io/graphcore"),
    ("Cloudflare", "https://job-boards.greenhouse.io/cloudflare"),
    ("Snowflake", "https://job-boards.greenhouse.io/snowflakecomputing"),
    # Quantitative technology
    ("Quadrature", "https://job-boards.greenhouse.io/quadraturecapital"),
    ("XTX Markets", "https://job-boards.greenhouse.io/xtxmarkets"),
    ("G-Research", "https://job-boards.greenhouse.io/gresearch"),
]


class TargetRoles(careers._Filter):
    """The board's own reader, with the role screen swapped for the target areas.

    A student title with no function named is kept too: what the posting asks for
    decides the category, and that is only readable once it is opened.
    """

    def screen(self, title, company="", employment=""):
        if self.excluded(title):
            return False
        return bool(TARGET_TITLE.search(title) or STUDENT_TITLE.search(title))

    def keep(self, p, company=""):
        return self.screen(p.get("title") or "")

    def for_graduate(self, p, target_year):
        return False


settings = dict(
    config.load("sources")["sources"]["careers"],
    workday_queries=["intern", "internship", "placement", "graduate", "algorithm",
                     "machine learning", "artificial intelligence", "computer vision", "forward deployed"],
    search_queries=["intern", "internship", "placement", "algorithm", "machine learning",
                    "artificial intelligence", "computer vision", "forward deployed"],
    workday_max_pages=3, search_max_pages=3)
wanted = TargetRoles(settings)
platforms = {"greenhouse", "lever", "ashby", "workable", "workday", "oracle", "amazon"}

boards = {b.key: b for b in careers.discover_boards(settings) if b.platform in platforms}
extra, unresolved = [], []
for company, url in CANDIDATES:
    board = careers.board_from_url(url, company)
    if board is None:
        unresolved.append({"company": company, "url": url, "why": "not a board this tool can read"})
    elif board.key not in boards:
        extra.append(board)
print(f"boards from the workbook: {len(boards)} | target-area boards added: {len(extra)}", flush=True)

rows, failed = [], []
with Session() as session:
    for host in ("boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co",
                 "api.ashbyhq.com", "apply.workable.com"):
        session.limiter.configure(host, settings.get("rate_limit_seconds") or 1.0)
    todo = list(boards.values()) + extra
    for n, board in enumerate(todo, start=1):
        try:
            postings = careers._READERS[board.platform](session, board, settings, wanted)
        except (SourceError, SourceBlocked) as exc:
            failed.append({"company": board.company, "board": board.key, "why": str(exc)[:160]})
            print(f"[{n}/{len(todo)}] {board.company}: FAILED {str(exc)[:80]}", flush=True)
            continue
        kept = 0
        for p in postings:
            title = p.get("title") or ""
            if not wanted.screen(title):
                continue
            rows.append({"company": board.company, "platform": careers.PLATFORM_NAMES[board.platform],
                         "board": board.key, "title": title, "location": p.get("location") or "",
                         "team": p.get("team") or "", "url": p.get("apply_url") or p.get("url") or "",
                         "description": p.get("description") or "", "deadline": str(p.get("deadline") or ""),
                   "posted": str(p.get("posted") or "")})
            kept += 1
        print(f"[{n}/{len(todo)}] {board.company}: {len(postings)} postings, {kept} in target areas", flush=True)

OUT.write_text(json.dumps({"rows": rows, "failed": failed, "unresolved": unresolved}, indent=1), encoding="utf-8")
print(f"\nkept {len(rows)} postings at {len({r['company'] for r in rows})} companies -> {OUT}")
print(f"boards that could not be read: {len(failed)} | candidate URLs that did not resolve: {len(unresolved)}")
