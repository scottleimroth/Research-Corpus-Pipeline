@echo off
cd /d "%~dp0"
if not exist "_system\venv\Scripts\python.exe" (
  echo.
  echo Project venv not found. Run SETUP.bat once on this machine first.
  echo.
  pause
  exit /b 1
)
if "%~1"=="" (
  echo.
  echo Usage: RUN_METACHECK_ONE.bat PAPER_ID
  echo.
  pause
  exit /b 1
)
call _system\launcher\run_python.cmd pipeline\run_metacheck_for_paper.py --paper-id %1 %2 %3 %4 %5 %6 %7 %8 %9
if errorlevel 1 (
  echo.
  echo MetaCheck run failed. Check service availability and logs.
  pause
  exit /b 1
)
echo.
echo MetaCheck run complete.
pause
