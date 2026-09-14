"""Linux entry point for the shared SeedVC desktop controller."""

from __future__ import annotations

from pathlib import Path
import runpy
import sys


SHARED_CLIENT = Path(__file__).resolve().parent.parent / "windows-client"
sys.path.insert(0, str(SHARED_CLIENT))
runpy.run_path(str(SHARED_CLIENT / "gui.py"), run_name="__main__")
