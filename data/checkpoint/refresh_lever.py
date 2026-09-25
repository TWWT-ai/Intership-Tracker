"""Re-read the Lever boards in the target-roles sweep, now that the reader keeps
Lever's requirement lists ("What We Require: ... Graduating in 2028").

Only Lever boards are read; every other row in target-roles.json is left alone.
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

SWEEP = ROOT / "data" / "checkpoint" / "target-roles.json"
source = (ROOT / "data" / "checkpoint" / "target_roles_scan.py").read_text(encoding="utf-8")
head = source.split("boards = {b.key: b for b in careers.discover_boards(settings)")[0]
namespace = {"__name__": "scan", "__file__": str(ROOT / "data" / "checkpoint" / "target_roles_scan.py")}
exec(compile(head, "target_roles_scan", "exec"), namespace)
wanted, settings = namespace["wanted"], namespace["settings"]

payload = json.loads(SWEEP.read_text(encoding="utf-8"))
lever = {}
for row in payload["rows"]:
    if row["board"].startswith("lever:"):
        lever.setdefault(row["board"], row["company"])
print(f"Lever boards to re-read: {len(lever)} ({', '.join(sorted(set(lever.values())))})")

fresh, failed = [], []
with Session() as session:
    session.limiter.configure("api.lever.co", 1.0)
    session.limiter.configure("api.eu.lever.co", 1.0)
    for key, company in sorted(lever.items()):
        _, region, token = key.split(":", 2)
        board = careers.Board("lever", (region, token), company)
        try:
            postings = careers._READERS["lever"](session, board, settings, wanted)
        except (SourceError, SourceBlocked) as exc:
            failed.append(key)
            print(f"{company}: FAILED {str(exc)[:80]} - its old rows are kept")
            continue
        kept = [{"company": company, "platform": "Lever", "board": key, "title": p.get("title") or "",
                 "location": p.get("location") or "", "team": p.get("team") or "",
                 "url": p.get("apply_url") or p.get("url") or "", "description": p.get("description") or "",
                 "deadline": str(p.get("deadline") or ""), "posted": str(p.get("posted") or "")}
                for p in postings if wanted.screen(p.get("title") or "")]
        fresh.extend(kept)
        print(f"{company}: {len(postings)} postings, {kept.__len__()} in target areas")

replaced = {k for k in lever if k not in failed}
payload["rows"] = [r for r in payload["rows"] if r["board"] not in replaced] + fresh
SWEEP.write_text(json.dumps(payload, indent=1), encoding="utf-8")
print(f"sweep now holds {len(payload['rows'])} rows")
