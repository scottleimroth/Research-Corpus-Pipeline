@echo off
cd /d "%~dp0"
if not exist "_system\venv\Scripts\python.exe" (
  echo.
  echo Project venv not found. Run SETUP.bat once on this machine first.
  echo.
  pause
  exit /b 1
)
echo.
echo Starting required evidence-check helpers...
powershell -NoProfile -ExecutionPolicy Bypass -File "_system\launcher\start_metacheck_services.ps1"
echo.
call _system\launcher\run_python.cmd launcher\Corpus_Pipeline_Runner.py
if errorlevel 1 (
  echo.
  echo Corpus Pipeline Runner failed to start.
  pause
)
