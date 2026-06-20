# Research Corpus Pipeline setup.ps1
# Windows bootstrap. Run once per machine after copying the folder over.
#
# Usage:
#   cd path\to\Research-Corpus-Pipeline
#   .\setup.ps1
#
# What it does:
#   1. Reports OS + Python (system or embedded under _system/tools/python-embed)
#   2. Installs portable Poppler/Tesseract/Ghostscript into _system/tools/
#   3. Creates a Python venv inside the folder if none exists
#   4. Installs Python packages from requirements.txt into the venv
#   5. Configures encrypted AI provider key storage (_system/secrets/api_keys.env.enc)
#   6. Runs config.py self-check and reports green/red per item

$ErrorActionPreference = "Stop"
$LauncherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SystemRoot = Split-Path -Parent $LauncherRoot
$ProjectRoot = Split-Path -Parent $SystemRoot

function Write-Green($msg)  { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Yellow($msg) { Write-Host "  [..] $msg" -ForegroundColor Yellow }
function Write-Red($msg)    { Write-Host "  [!!] $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
$folderLabel = Split-Path -Leaf $ProjectRoot
Write-Host " Research Corpus Pipeline setup ($folderLabel)" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Folder: $ProjectRoot"
Write-Host ""

function Test-PortableBin($relPath) {
    return Test-Path (Join-Path $SystemRoot $relPath)
}

function Prepend-PortableToolPath {
    $dirs = @(
        (Join-Path $SystemRoot "tools\poppler\Library\bin"),
        (Join-Path $SystemRoot "tools\tesseract"),
        (Join-Path $SystemRoot "tools\ghostscript\bin"),
        (Join-Path $SystemRoot "tools\python-embed")
    )
    foreach ($p in $dirs) {
        if ((Test-Path $p) -and ($env:Path -notlike "*$p*")) {
            $env:Path = "$p;$env:Path"
        }
    }
}

# ---------- 1. OS + Python ----------
Write-Host "[1/6] Environment" -ForegroundColor Cyan
$winVer = (Get-CimInstance Win32_OperatingSystem).Caption
Write-Green "OS: $winVer"

$pythonLauncher = Join-Path $LauncherRoot "install_portable_python.ps1"
if (Test-Path $pythonLauncher) {
    $pythonForVenv = & $pythonLauncher -SystemRoot $SystemRoot | Select-Object -Last 1
    if ($LASTEXITCODE -ne 0) {
        Write-Red "Python bootstrap failed."
        exit 1
    }
} else {
    Write-Yellow "install_portable_python.ps1 missing; using system Python"
    $pythonForVenv = "python"
}
if ($pythonForVenv -eq "python") {
    try {
        $pyVer = (python --version 2>&1)
    } catch {
        Write-Red "Python not found. Install from https://www.python.org/downloads/ (3.12+) or re-run SETUP."
        exit 1
    }
} else {
    $pyVer = (& $pythonForVenv --version 2>&1)
}
Write-Green "Python: $pyVer"

# ---------- 2. Portable PDF/OCR tools ----------
Write-Host ""
Write-Host "[2/6] Portable PDF/OCR tools (_system/tools/)" -ForegroundColor Cyan
$toolsLauncher = Join-Path $LauncherRoot "install_portable_tools.ps1"
if (Test-Path $toolsLauncher) {
    & $toolsLauncher -SystemRoot $SystemRoot
    if ($LASTEXITCODE -ne 0) {
        Write-Red "Portable tool install failed. See output above."
        exit 1
    }
} else {
    Write-Yellow "install_portable_tools.ps1 missing; checking bundled tools under _system/tools/"
    $hasPoppler = Test-PortableBin "tools\poppler\Library\bin\pdftotext.exe"
    $hasTess = Test-PortableBin "tools\tesseract\tesseract.exe"
    $hasGs = Test-PortableBin "tools\ghostscript\bin\gswin64c.exe"
    if (-not ($hasPoppler -and $hasTess -and $hasGs)) {
        Write-Red "Bundled PDF/OCR tools not found. Copy _system/tools/ from LIVE or restore install_portable_tools.ps1."
        exit 1
    }
    Write-Green "Bundled PDF/OCR tools already present under _system/tools/"
}
Prepend-PortableToolPath

# ---------- 3. Python venv ----------
Write-Host ""
Write-Host "[3/6] Python virtual environment" -ForegroundColor Cyan
$venvPath = Join-Path $SystemRoot "venv"
if (Test-Path "$venvPath\Scripts\python.exe") {
    Write-Green "venv exists at $venvPath"
} else {
    Write-Yellow "Creating venv at $venvPath"
    & $pythonForVenv -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        Write-Red "venv creation failed."
        exit 1
    }
    Write-Green "venv created"
}
$venvPython = Join-Path $venvPath "Scripts\python.exe"
Prepend-PortableToolPath
if (Test-Path "$venvPath\Scripts") {
    $env:Path = "$venvPath\Scripts;$env:Path"
}

# ---------- 4. Python packages ----------
Write-Host ""
Write-Host "[4/6] Python packages (requirements.txt)" -ForegroundColor Cyan
$reqPath = Join-Path $LauncherRoot "requirements.txt"
if (-not (Test-Path $reqPath)) {
    Write-Red "requirements.txt not found at $reqPath"
    exit 1
}
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r $reqPath
if ($LASTEXITCODE -eq 0) {
    Write-Green "Python packages installed into venv"
} else {
    Write-Red "pip install failed. See output above."
}

# ocrmypdf (Python package - installed by requirements.txt; verify on PATH via venv)
$ocrCmd = Join-Path $venvPath "Scripts\ocrmypdf.exe"
if (Test-Path $ocrCmd) {
    Write-Green "ocrmypdf installed at $ocrCmd"
} else {
    Write-Red "ocrmypdf not found in venv after install"
}

# ---------- 4b. Optional MetaCheck advanced runtime ----------
Write-Host ""
Write-Host "[4b/6] Optional MetaCheck advanced runtime (Docker/GROBID)" -ForegroundColor Cyan
$metacheckRuntime = Join-Path $LauncherRoot "setup_metacheck_runtime.ps1"
if (Test-Path $metacheckRuntime) {
    & $metacheckRuntime -OfferInstall
} else {
    Write-Yellow "setup_metacheck_runtime.ps1 missing; Python MetaCheck-lite will still run"
}

# ---------- 5. Folder-local AI provider/key ----------
Write-Host ""
Write-Host "[5/6] Public AI provider setup (encrypted key storage)" -ForegroundColor Cyan
$publicAiSetup = Join-Path $LauncherRoot "public_ai_setup.ps1"
if (Test-Path $publicAiSetup) {
    & $publicAiSetup -SystemRoot $SystemRoot
} else {
    $secretsLauncher = Join-Path $LauncherRoot "manage_secrets.ps1"
    & $secretsLauncher -SystemRoot $SystemRoot -Mode configure
}
$keyFile = Join-Path $SystemRoot "secrets\api_keys.env.enc"
$legacyKeyFile = Join-Path $SystemRoot "secrets\api_keys.env"
$profileFile = Join-Path $SystemRoot "corpus_profile.json"
$apiMode = "anthropic"
$profileName = "personal"
if (Test-Path $profileFile) {
    try {
        $obj = Get-Content $profileFile -Raw | ConvertFrom-Json
        if ($obj.profile) { $profileName = [string]$obj.profile }
        if ($obj.api_mode) { $apiMode = [string]$obj.api_mode }
    } catch { }
}
$isPublicOllama = ($profileName -eq "public" -and $apiMode -eq "ollama_local")

# ---------- 6. config.py self-check ----------
Write-Host ""
Write-Host "[6/6] Pipeline config self-check" -ForegroundColor Cyan
& $venvPython (Join-Path $SystemRoot "config.py")

$binOk = (Test-PortableBin "tools\poppler\Library\bin\pdftotext.exe") `
    -and (Test-PortableBin "tools\tesseract\tesseract.exe") `
    -and (Test-PortableBin "tools\ghostscript\bin\gswin64c.exe") `
    -and (Test-Path $ocrCmd)
$apiOk = $isPublicOllama -or (Test-Path $keyFile)
# DeepSeek is default eval provider; vault may hold only DEEPSEEK_API_KEY.
$status = @{
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    folder = $ProjectRoot
    python = $pyVer
    venv = $venvPath
    binaries_ok = $binOk
    profile = $profileName
    api_mode = $apiMode
    encrypted_ai_key_vault_present = (Test-Path $keyFile)
    ready_for_gui = ($binOk -and (Test-Path "$venvPath\Scripts\python.exe"))
}
$statusDir = Join-Path $SystemRoot "CorpusStore"
if (-not (Test-Path $statusDir)) { New-Item -ItemType Directory -Path $statusDir | Out-Null }
$statusPath = Join-Path $statusDir "setup_status.json"
$status | ConvertTo-Json -Depth 4 | Set-Content -Path $statusPath -Encoding UTF8
Write-Green "Wrote setup status to CorpusStore\setup_status.json"

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " Setup complete." -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next step: double-click RUN.bat"
Write-Host ""
if (-not $apiOk) {
    Write-Yellow "Ingest/evaluation needs an AI provider key. Re-run SETUP and choose OpenRouter, DeepSeek, OpenAI, Anthropic, or local/free mode."
} elseif ($isPublicOllama) {
    Write-Yellow "PUBLIC profile is in local/free mode. Quality depends on the local model you install."
}
if (-not $binOk) {
    Write-Yellow "One or more portable PDF/OCR tools are still missing. Re-run SETUP (needs network on first run)."
}
Write-Host ""
Write-Host "Tools live under _system/tools/ - no machine-wide PATH changes required."
