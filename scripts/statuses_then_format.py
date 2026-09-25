"""The status-sync run: apply the email evidence, then the user's layout.

`main.py statuses` rewrites the Internships sheet one row per programme when it
changes anything, so the technical-programmes layout is re-applied afterwards -
but only then: that step reads a freshly written sheet, never an already
formatted one. `--dry-run` is passed through and writes nothing at all.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
extra = [a for a in sys.argv[1:] if a in ("--dry-run",) or a.startswith("--evidence")]

sync = subprocess.run([sys.executable, str(ROOT / "main.py"), "statuses", *extra], cwd=ROOT)
if "--dry-run" in extra or sync.returncode != 0:
    # 2 = nothing needed writing, 3 = skipped because the workbook was busy.
    sys.exit(0 if sync.returncode in (0, 2) else sync.returncode)
layout = subprocess.run([sys.executable, str(ROOT / "data" / "checkpoint" / "technical_programmes.py"),
                         "--after-daily"], cwd=ROOT)
sys.exit(layout.returncode)
