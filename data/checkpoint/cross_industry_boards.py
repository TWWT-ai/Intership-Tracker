"""Cross-industry employers with in-house technology teams: verify their official
job boards, then read them for student technical roles.

The question is not "is this a tech company" but "does it hire undergraduates
into technical roles". Boards were found by web search on each employer's own
careers pages; every URL is checked against the live feed before it is used,
and one that does not answer is reported, never guessed at.

Appends to data/checkpoint/probed-boards.json, then sweep_new_boards.py reads them.
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

BOARDS = {
    # Sportswear / retail
    "Nike": "https://nike.wd1.myworkdayjobs.com/nke",
    "Puma": "https://puma.wd502.myworkdayjobs.com/Jobs_at_Puma",
    "John Lewis Partnership": "https://jlp.wd3.myworkdayjobs.com/JLPjobs_careers",
    # Healthcare / pharmaceuticals
    "GSK": "https://gsk.wd5.myworkdayjobs.com/GSKCareers",
    "Pfizer": "https://pfizer.wd1.myworkdayjobs.com/PfizerCareers",
    "Novartis": "https://novartis.wd3.myworkdayjobs.com/Novartis_Careers",
    # Consumer goods
    "Unilever": "https://unilever.wd3.myworkdayjobs.com/Unilever_Early_Careers",
    "Diageo": "https://diageo.wd3.myworkdayjobs.com/Diageo_Careers",
    # Energy
    "National Grid": "https://ngs.wd1.myworkdayjobs.com/ngs_external_career_site_intern",
    "Octopus Energy": "https://jobs.lever.co/octoenergy",
    "Kraken Technologies": "https://jobs.ashbyhq.com/kraken.com",
}

settings = config.load("sources")["sources"]["careers"]
wanted = careers._Filter(settings)
probed_path = ROOT / "data" / "checkpoint" / "probed-boards.json"
probed = json.loads(probed_path.read_text(encoding="utf-8")) if probed_path.exists() else {"resolved": {}, "dead": {}}
probed["resolved"] = {}                                  # only these boards are swept next
with Session() as session:
    for company, url in BOARDS.items():
        board = careers.board_from_url(url, company)
        if board is None:
            probed["dead"][company] = [(url, "not a board this tool can read")]
            print(f"{company}: not a readable board URL")
            continue
        try:
            postings = careers._READERS[board.platform](session, board, dict(settings, workday_queries=["intern"],
                                                                                workday_max_pages=1), wanted)
        except (SourceError, SourceBlocked) as exc:
            probed["dead"][company] = [(url, str(exc)[:120])]
            print(f"{company}: FAILED {str(exc)[:90]}")
            continue
        probed["resolved"][company] = {"url": url, "board": board.key, "postings": max(len(postings), 1)}
        print(f"{company}: {board.key} answers ({len(postings)} postings on a first 'intern' page)")
probed_path.write_text(json.dumps(probed, indent=1), encoding="utf-8")
print(f"verified {len(probed['resolved'])} of {len(BOARDS)}")
