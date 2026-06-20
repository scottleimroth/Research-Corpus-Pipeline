# Optional embedded Python for machines without system Python (Windows amd64).
param(
    [string]$SystemRoot = ""
)

$ErrorActionPreference = "Stop"
if (-not $SystemRoot) {
    $LauncherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    $SystemRoot = Split-Path -Parent $LauncherRoot
}

$EmbedRoot = Join-Path $SystemRoot "tools\python-embed"
$EmbedExe = Join-Path $EmbedRoot "python.exe"

if (Test-Path $EmbedExe) {
    Write-Host "  [OK] Embedded Python already present" -ForegroundColor Green
    Write-Output $EmbedExe
    exit 0
}

try {
    $null = (python --version 2>&1)
    Write-Host "  [OK] System Python available; skipping embedded Python" -ForegroundColor Green
    Write-Output "python"
    exit 0
} catch {
    # continue to install embeddable package
}

Write-Host "  [..] Downloading embedded Python 3.12 into _system/tools/python-embed" -ForegroundColor Yellow
$zipUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
$tmpZip = Join-Path $env:TEMP ("python-embed-" + [guid]::NewGuid().ToString() + ".zip")
try {
    if (Test-Path $EmbedRoot) { Remove-Item $EmbedRoot -Recurse -Force }
    New-Item -ItemType Directory -Path $EmbedRoot -Force | Out-Null
    Invoke-WebRequest -Uri $zipUrl -OutFile $tmpZip -UseBasicParsing
    Expand-Archive -Path $tmpZip -DestinationPath $EmbedRoot -Force
    # Enable site-packages / venv creation from embeddable build
    $pth = Get-ChildItem $EmbedRoot -Filter "python*._pth" | Select-Object -First 1
    if ($pth) {
        $text = Get-Content $pth.FullName -Raw
        $text = $text -replace "#import site", "import site"
        Set-Content -Path $pth.FullName -Value $text -Encoding ASCII
    }
    Write-Host "  [OK] Embedded Python installed" -ForegroundColor Green
    Write-Output $EmbedExe
    exit 0
} catch {
    Write-Host "  [!!] Embedded Python install failed: $_" -ForegroundColor Red
    exit 1
} finally {
    Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
}
