"""Which recruiting platform sits behind each 'custom' careers site.

One robots-checked GET per host through the pipeline's own Fetcher. Reads the
page for platform fingerprints only; writes scratchpad/fingerprints.json.
"""
import csv
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(r"D:\School\Personal\Intership tracker")
SCRATCH = Path(__file__).parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from sources.fetch import Session, SourceBlocked, SourceError  # noqa: E402
from tracker import config  # noqa: E402

FINGERPRINTS = [
    ("oleeo", r"tal\.net|oleeo"),
    ("avature", r"avature"),
    ("phenom", r"phenompeople|phenom\.com|cdn\.phenompeople|ph-page"),
    ("successfactors", r"successfactors|sapsf|career[0-9]*\.sapsf|jobs\.sap\.com"),
    ("eightfold", r"eightfold\.ai|pcsx"),
    ("workday", r"myworkdayjobs|myworkdaysite"),
    ("greenhouse", r"greenhouse\.io|gh_jid|grnh\.se"),
    ("lever", r"lever\.co"),
    ("ashby", r"ashbyhq"),
    ("smartrecruiters", r"smartrecruiters"),
    ("icims", r"icims"),
    ("taleo", r"taleo"),
    ("oracle", r"oraclecloud\.com/hcmUI"),
    ("gti", r"groupgti|gtisolutions|tal\.net/.*gti"),
    ("jobvite", r"jobvite"),
    ("teamtailor", r"teamtailor"),
    ("pinpoint", r"pinpointhq"),
    ("beamery", r"beamery"),
    ("radancy", r"radancy|tmpwebeng|jobs\.[a-z-]+\.com/.*search-jobs|talentbrew"),
    ("cornerstone", r"csod\.com"),
    ("bamboohr", r"bamboohr"),
    ("hibob", r"hibob"),
    ("workable", r"workable\.com"),
    ("personio", r"personio"),
    ("recruitee", r"recruitee"),
    ("salesforce", r"my\.site\.com|force\.com"),
    ("linkedin", r"linkedin\.com"),
]

rows = list(csv.DictReader((SCRATCH / "companies-searched.csv").open(encoding="utf-8-sig")))
targets = []
for r in rows:
    if not r["Coverage"].startswith("Trackr only - careers site"):
        continue
    for host in [h.strip() for h in r["Employer site"].split(";") if h.strip()]:
        targets.append((r["Company"], host))

last_run = json.loads((config.STATE_DIR / "last_run.json").read_text(encoding="utf-8"))["jobs"]
example_url = {}
for job in last_run:
    for s in job.get("sources", []):
        url = s.get("application_url") or ""
        host = urlparse(url).netloc.lower()
        if host and (job["company"], host) not in example_url:
            example_url[(job["company"], host)] = url

results = []
with Session() as session:
    for company, host in targets:
        url = example_url.get((company, host), f"https://{host}/")
        entry = {"company": company, "host": host, "url": url}
        try:
            response = session.http.get(url)
            text = response.text or ""
            final = response.url
            entry["status"] = response.status
            entry["final_url"] = final
            hay = f"{final}\n{text[:400000]}"
            entry["platforms"] = [name for name, pat in FINGERPRINTS if re.search(pat, hay, re.I)]
        except SourceBlocked as exc:
            entry["status"], entry["error"] = "blocked", str(exc)[:160]
        except SourceError as exc:
            entry["status"], entry["error"] = "error", str(exc)[:160]
        except Exception as exc:  # noqa: BLE001
            entry["status"], entry["error"] = "error", f"{type(exc).__name__}: {str(exc)[:140]}"
        results.append(entry)
        print(f"{entry.get('status')!s:8} {company[:28]:28} {host[:40]:40} {','.join(entry.get('platforms', [])) or entry.get('error', '')[:60]}", flush=True)

(SCRATCH / "fingerprints.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
print("\nplatform counts (companies):")
seen = {}
for e in results:
    for p in e.get("platforms", []) or ["(none)"]:
        seen.setdefault(p, set()).add(e["company"])
for p, cs in sorted(seen.items(), key=lambda kv: -len(kv[1])):
    print(f"  {len(cs):4} {p}")
print("status:", Counter(str(e.get("status")) for e in results))
