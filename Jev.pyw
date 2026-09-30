"""Double-click to start Jev's desktop HUD (runs without a console window).

Put a shortcut to this file in shell:startup to start Jev when you log in.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"

if Path(sys.executable).resolve() != PYTHONW.resolve() and PYTHONW.exists():
    # Launched with the system Python: hand over to the project's virtualenv.
    subprocess.Popen([str(PYTHONW), "-m", "jev_assistant.gui"], cwd=ROOT,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
else:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from jev_assistant.gui.app import run

    run()
