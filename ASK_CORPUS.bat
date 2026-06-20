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
  echo Usage: ASK_CORPUS.bat "your query" [--mode structured^|semantic]
  echo Example: ASK_CORPUS.bat "papers about rhythmic prediction violations" --mode semantic --top 8
  pause
  exit /b 1
)
call _system\launcher\run_python.cmd pipeline\search_corpus.py %*
pause
