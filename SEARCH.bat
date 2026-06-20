@echo off
cd /d "%~dp0"
if not exist "_system\venv\Scripts\python.exe" (
  echo.
  echo Project venv not found. Run SETUP.bat once on this machine first.
  echo.
  pause
  exit /b 1
)
set "PORT=8765"
if not "%~1"=="" set "PORT=%~1"
echo.
echo Starting local search UI at http://127.0.0.1:%PORT%
echo Press Ctrl+C in this window to stop.
echo.
start "" "http://127.0.0.1:%PORT%"
set "CORPUS_SEARCH_PORT=%PORT%"
call _system\launcher\run_python.cmd search\server.py
