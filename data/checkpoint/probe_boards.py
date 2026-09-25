"""Find the real board slug for target-area employers whose guessed URL 404'd.

Tries a few spellings per employer against each platform's public feed and keeps
only the ones that answer with postings. Nothing is guessed into config: a slug
that does not answer is reported so it can be looked up by hand.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from sources import careers  # noqa: E402
from sources.fetch import Session, SourceBlocked, SourceError  # noqa: E402
from tracker import config  # noqa: E402

CANDIDATES = {
    "Google DeepMind": ["https://job-boards.greenhouse.io/googledeepmind", "https://boards.greenhouse.io/deepmind",
                        "https://job-boards.greenhouse.io/deepmindunlisted"],
    "Arm": ["https://job-boards.greenhouse.io/armltd", "https://job-boards.greenhouse.io/arm1",
            "https://apply.workable.com/arm"],
    "Faculty": ["https://job-boards.greenhouse.io/facultyai", "https://apply.workable.com/faculty",
                "https://jobs.lever.co/faculty"],
    "Quantexa": ["https://job-boards.greenhouse.io/quantexa1", "https://apply.workable.com/quantexa",
                 "https://jobs.lever.co/quantexa"],
    "Tractable": ["https://job-boards.greenhouse.io/tractableai", "https://jobs.lever.co/tractable",
                  "https://jobs.ashbyhq.com/tractable"],
    "Synthesia": ["https://job-boards.greenhouse.io/synthesia1", "https://jobs.ashbyhq.com/synthesia",
                  "https://jobs.lever.co/synthesia"],
    "Ocado Group": ["https://job-boards.greenhouse.io/ocadogroup", "https://apply.workable.com/ocado",
                    "https://jobs.lever.co/ocado"],
    "Improbable": ["https://job-boards.greenhouse.io/improbableworlds", "https://jobs.lever.co/improbable",
                   "https://jobs.ashbyhq.com/improbable"],
    "Cambridge Consultants": ["https://apply.workable.com/cambridgeconsultants",
                              "https://job-boards.greenhouse.io/cambridgeconsultantsltd"],
    "Helsing": ["https://job-boards.greenhouse.io/helsing", "https://jobs.lever.co/helsing",
                "https://apply.workable.com/helsing"],
    "Arondite": ["https://job-boards.greenhouse.io/arondite", "https://jobs.lever.co/arondite"],
    "XTX Markets": ["https://job-boards.greenhouse.io/xtxmarkets1", "https://jobs.lever.co/xtxmarkets",
                    "https://jobs.ashbyhq.com/xtxmarkets"],
    "G-Research": ["https://job-boards.greenhouse.io/gresearch1", "https://jobs.lever.co/gresearch",
                   "https://jobs.ashbyhq.com/gresearch"],
    "Snowflake": ["https://job-boards.greenhouse.io/snowflake", "https://jobs.lever.co/snowflake",
                  "https://jobs.ashbyhq.com/snowflake"],
    "Oxa": ["https://job-boards.greenhouse.io/oxaautonomy", "https://apply.workable.com/oxa",
            "https://jobs.lever.co/oxa"],
    "Perplexity": ["https://job-boards.greenhouse.io/perplexityai", "https://jobs.ashbyhq.com/perplexity"],
    "Wayve": ["https://job-boards.greenhouse.io/wayve"],
}

settings = config.load("sources")["sources"]["careers"]
wanted = careers._Filter(settings)
resolved, dead = {}, {}
with Session() as session:
    for company, urls in CANDIDATES.items():
        for url in urls:
            board = careers.board_from_url(url, company)
            if board is None:
                continue
            try:
                postings = careers._READERS[board.platform](session, board, settings, wanted)
            except (SourceError, SourceBlocked) as exc:
                dead.setdefault(company, []).append((url, str(exc)[:70]))
                continue
            resolved[company] = {"url": url, "board": board.key, "postings": len(postings)}
            print(f"{company}: {url} -> {len(postings)} postings", flush=True)
            break
        else:
            print(f"{company}: no spelling answered", flush=True)

(ROOT / "data" / "checkpoint" / "probed-boards.json").write_text(
    json.dumps({"resolved": resolved, "dead": dead}, indent=1), encoding="utf-8")
print(f"\nresolved {len(resolved)} of {len(CANDIDATES)}")
