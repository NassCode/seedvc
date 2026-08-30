@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher was not found. Install a current 64-bit Python 3 release first.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating the SeedVC Python environment...
  py -3 -m venv .venv
  if errorlevel 1 goto :failed
)

echo Installing or updating dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo Setup complete. Double-click run-gui.bat to start SeedVC.
pause
exit /b 0

:failed
echo.
echo Setup failed. Review the error above.
pause
exit /b 1
