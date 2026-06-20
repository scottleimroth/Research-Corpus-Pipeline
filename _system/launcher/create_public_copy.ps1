# Create CorpusPipeline-PUBLIC from LIVE: same code/tools layout, empty DB, no owner API key.
param(
    [string]$LiveRoot = "",
    [string]$PublicRoot = ""
)

$ErrorActionPreference = "Stop"
if (-not $LiveRoot) {
    $LauncherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    $LiveRoot = Split-Path -Parent (Split-Path -Parent $LauncherRoot)
}
if (-not $PublicRoot) {
    $PublicRoot = Join-Path (Split-Path -Parent $LiveRoot) "CorpusPipeline-PUBLIC"
}

if (Test-Path $PublicRoot) {
    Write-Host "ERROR: $PublicRoot already exists. Remove or rename it first." -ForegroundColor Red
    exit 1
}

Write-Host "Copying LIVE -> PUBLIC (excluding venv and API key vaults)..." -ForegroundColor Cyan
$null = robocopy $LiveRoot $PublicRoot /E /XD venv /XF api_keys.env api_keys.env.enc /NFL /NDL /NJH /NJS /nc /ns /np
if ($LASTEXITCODE -ge 8) {
    Write-Host "ERROR: robocopy failed with exit $LASTEXITCODE" -ForegroundColor Red
    exit 1
}

$venvPython = Join-Path $LiveRoot "_system\venv\Scripts\python.exe"
$resetScript = Join-Path $PublicRoot "_system\pipeline\reset_public_corpus.py"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating temporary venv on PUBLIC for reset..." -ForegroundColor Yellow
    $pubSystem = Join-Path $PublicRoot "_system"
    $venvPython = Join-Path $pubSystem "venv\Scripts\python.exe"
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) { Write-Host "ERROR: Python not found." -ForegroundColor Red; exit 1 }
    & $py -m venv (Join-Path $pubSystem "venv")
    & $venvPython -m pip install -r (Join-Path $pubSystem "launcher\requirements.txt") -q
}

Write-Host "Resetting PUBLIC corpus (empty DB)..." -ForegroundColor Cyan
& $venvPython $resetScript
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host ""
Write-Host "Created: $PublicRoot" -ForegroundColor Green
Write-Host "Recipient: run SETUP.bat and enter their own AI provider API key."
