@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo SeedVC is not installed yet. Run setup first.
  pause
  exit /b 1
)
start "SeedVC Voice Changer" ".venv\Scripts\pythonw.exe" "gui.py"
