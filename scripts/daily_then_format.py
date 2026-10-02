"""The scheduled run: the daily scan, then the user's layout re-applied.

`main.py daily` rebuilds the Internships sheet one row per programme with every
function, new jobs first. The user keeps technical programmes only, so that
filter runs on the fresh sheet afterwards, keeping the order and merging nothing. It verifies
the result and replaces the workbook only if every check passes.

Last, the hackathon workbook is re-rendered from its store (no network), so its
day counts and deadline colours are current every morning.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

scan = subprocess.run([sys.executable, str(ROOT / "main.py"), "daily"], cwd=ROOT)
if scan.returncode != 0:
    sys.exit(scan.returncode)
layout = subprocess.run([sys.executable, str(ROOT / "data" / "checkpoint" / "technical_programmes.py"),
                         "--after-daily"], cwd=ROOT)
if layout.returncode != 0:
    sys.exit(layout.returncode)
# 3 = hackathons.xlsx open in Excel: skipped, not a failure of the scan.
render = subprocess.run([sys.executable, str(ROOT / "main.py"), "events"], cwd=ROOT)
sys.exit(0 if render.returncode in (0, 3) else render.returncode)
