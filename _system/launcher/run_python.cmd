@echo off
REM Run a command with project venv + portable bundled tools on PATH.
REM If api_keys.env.enc exists, prompts for master passphrase once per launch.
setlocal
cd /d "%~dp0\.."
set "TOOLS=%CD%\tools"
if exist "%TOOLS%\poppler\Library\bin" set "PATH=%TOOLS%\poppler\Library\bin;%PATH%"
if exist "%TOOLS%\tesseract" set "PATH=%TOOLS%\tesseract;%PATH%"
if exist "%TOOLS%\ghostscript\bin" set "PATH=%TOOLS%\ghostscript\bin;%PATH%"
if exist "venv\Scripts" set "PATH=%CD%\venv\Scripts;%PATH%"

set "NEED_SECRET=0"
if /I "%~1"=="launcher\Corpus_Pipeline_Runner.py" set "NEED_SECRET=1"
if /I "%~1"=="launcher\Run_All_Staging.py" set "NEED_SECRET=1"
if /I "%~1"=="pipeline\run_corpus_pipeline.py" set "NEED_SECRET=1"

if "%NEED_SECRET%"=="1" (
  if exist "secrets\api_keys.env.enc" (
    if not defined ANTHROPIC_API_KEY if not defined DEEPSEEK_API_KEY if not defined OPENROUTER_API_KEY if not defined OPENAI_API_KEY (
      if exist "venv\Scripts\python.exe" (
        "venv\Scripts\python.exe" secrets_vault.py run-unlocked %*
        endlocal & exit /b %ERRORLEVEL%
      )
    )
  )
)

if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" %*
) else (
  python %*
)
endlocal & exit /b %ERRORLEVEL%
