@echo off
cd /d "%~dp0"
echo.
echo CorpusPipeline setup - first run on this machine
echo Folder: %CD%
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_system\launcher\setup.ps1"
if errorlevel 1 (
  echo.
  echo Setup reported errors. Fix the items above, then run this again.
  pause
  exit /b 1
)
echo.
echo Setup finished. Double-click RUN.bat (you will enter your master passphrase each run).
pause
