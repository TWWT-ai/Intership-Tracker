"""Read the boards probe_boards.py resolved, and append their target-area rows.

Only the newly found boards are read; the 205 already swept are left alone. Rows
are appended to data/checkpoint/target-roles.json, which the sheet builder reads.
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

SWEEP = ROOT / "data" / "checkpoint" / "target-roles.json"
PROBED = ROOT / "data" / "checkpoint" / "probed-boards.json"

# The screen and settings are the sweep's own, imported without re-running it.
source = (ROOT / "data" / "checkpoint" / "target_roles_scan.py").read_text(encoding="utf-8")
head = source.split("boards = {b.key: b for b in careers.discover_boards(settings)")[0]
namespace = {"__name__": "scan", "__file__": str(ROOT / "data" / "checkpoint" / "target_roles_scan.py")}
exec(compile(head, "target_roles_scan", "exec"), namespace)
wanted, settings = namespace["wanted"], namespace["settings"]

probed = json.loads(PROBED.read_text(encoding="utf-8"))["resolved"]
boards = [careers.board_from_url(v["url"], company) for company, v in probed.items() if v["postings"]]
boards = [b for b in boards if b is not None]
print(f"newly found boards to read: {len(boards)} ({', '.join(b.company for b in boards)})")

payload = json.loads(SWEEP.read_text(encoding="utf-8"))
known = {(r["company"], r["title"], r["location"]) for r in payload["rows"]}
added = 0
with Session() as session:
    for host in ("boards-api.greenhouse.io", "api.ashbyhq.com", "apply.workable.com", "api.lever.co"):
        session.limiter.configure(host, settings.get("rate_limit_seconds") or 1.0)
    for board in boards:
        try:
            postings = careers._READERS[board.platform](session, board, settings, wanted)
        except (SourceError, SourceBlocked) as exc:
            payload["failed"].append({"company": board.company, "board": board.key, "why": str(exc)[:160]})
            print(f"{board.company}: FAILED {str(exc)[:80]}")
            continue
        kept = 0
        for p in postings:
            title = p.get("title") or ""
            if not wanted.screen(title):
                continue
            row = {"company": board.company, "platform": careers.PLATFORM_NAMES[board.platform],
                   "board": board.key, "title": title, "location": p.get("location") or "",
                   "team": p.get("team") or "", "url": p.get("apply_url") or p.get("url") or "",
                   "description": p.get("description") or "", "deadline": str(p.get("deadline") or ""),
                   "posted": str(p.get("posted") or "")}
            if (row["company"], row["title"], row["location"]) in known:
                continue
            payload["rows"].append(row)
            known.add((row["company"], row["title"], row["location"]))
            kept += 1
        added += kept
        print(f"{board.company}: {len(postings)} postings, {kept} in target areas")

SWEEP.write_text(json.dumps(payload, indent=1), encoding="utf-8")
print(f"\nadded {added} rows; the sweep file now holds {len(payload['rows'])}")
