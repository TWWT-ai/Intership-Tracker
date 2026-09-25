"""The scheduled run: the daily scan, then the user's layout re-applied.

`main.py daily` rebuilds the Internships sheet one row per programme with every
function. The user keeps technical programmes only, with Company merged down each
company's group, so that filter runs on the fresh sheet afterwards. It verifies
the result and replaces the workbook only if every check passes.
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
sys.exit(layout.returncode)
