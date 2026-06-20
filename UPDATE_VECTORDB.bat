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
echo Updating vector index (incremental).
echo.
call _system\launcher\run_python.cmd pipeline\update_vector_db.py %*
if errorlevel 1 (
  echo.
  echo UPDATE_VECTORDB failed.
  pause
  exit /b 1
)
echo.
echo UPDATE_VECTORDB complete.
pause
