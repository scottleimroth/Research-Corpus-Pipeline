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
echo Building vector index (fresh rebuild). This can take a long time.
echo.
call _system\launcher\run_python.cmd pipeline\build_vector_db.py --fresh %*
if errorlevel 1 (
  echo.
  echo BUILD_VECTOR failed.
  pause
  exit /b 1
)
echo.
echo BUILD_VECTOR complete.
pause
