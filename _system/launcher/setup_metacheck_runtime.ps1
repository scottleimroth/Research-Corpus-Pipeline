param(
  [switch]$OfferInstall
)

$ErrorActionPreference = "Stop"
$LauncherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SystemRoot = Split-Path -Parent $LauncherRoot
$ImageDir = Join-Path $SystemRoot "tools\docker-images"
$StatusPath = Join-Path $SystemRoot "CorpusStore\metacheck_runtime_status.json"
$GrobidImage = if ($env:GROBID_DOCKER_IMAGE) { $env:GROBID_DOCKER_IMAGE } else { "grobid/grobid:0.9.0" }
$MetaCheckImage = if ($env:METACHECK_DOCKER_IMAGE) { $env:METACHECK_DOCKER_IMAGE } else { "scienceverse/metacheck:sha-2d0eb12" }

function Command-Exists($Name) {
  return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Docker-Cli-Available {
  try {
    & docker --version *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function Docker-Engine-Available {
  try {
    & docker version --format '{{.Server.Version}}' *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function Image-Exists($Image) {
  if (-not (Docker-Engine-Available)) { return $false }
  $id = & docker images -q $Image 2>$null | Select-Object -First 1
  return ($null -ne $id -and -not [string]::IsNullOrWhiteSpace([string]$id))
}

function Wsl-Status {
  if (-not (Command-Exists "wsl.exe")) {
    return @{ available = $false; detail = "wsl.exe not found" }
  }
  try {
    $out = & wsl --status 2>&1
    return @{ available = $true; detail = ($out -join "`n") }
  } catch {
    return @{ available = $true; detail = "wsl status failed: $($_.Exception.Message)" }
  }
}

$dockerDesktopPaths = @(
  "C:\Program Files\Docker\Docker\Docker Desktop.exe",
  "$env:LOCALAPPDATA\Docker\Docker Desktop.exe"
)
$dockerDesktopPath = $dockerDesktopPaths | Where-Object { Test-Path $_ } | Select-Object -First 1
$wsl = Wsl-Status

if (-not (Docker-Cli-Available) -and $OfferInstall -and (Command-Exists "winget.exe")) {
  Write-Host "Docker Desktop is not installed. It is needed for advanced MetaCheck/GROBID mode."
  $answer = Read-Host "Install Docker Desktop with winget now? (y/N)"
  if ($answer -match "^(y|yes)$") {
    winget install -e --id Docker.DockerDesktop
  }
}

$bundleFiles = @(
  (Join-Path $ImageDir "grobid-0.9.0.tar"),
  (Join-Path $ImageDir "metacheck-latest.tar")
)
$bundlePresent = ($bundleFiles | Where-Object { Test-Path $_ }).Count -eq $bundleFiles.Count

if ((Docker-Engine-Available) -and (-not $bundlePresent) -and $OfferInstall) {
  $alreadyLoaded = (Image-Exists $GrobidImage) -and (Image-Exists $MetaCheckImage)
  if (-not $alreadyLoaded) {
    Write-Host "Advanced MetaCheck images are not bundled or loaded."
    $answer = Read-Host "Download and bundle them now? This may take several minutes and several GB. (y/N)"
    if ($answer -match "^(y|yes)$") {
      $bundleScript = Join-Path $LauncherRoot "bundle_metacheck_images.ps1"
      if (Test-Path $bundleScript) {
        & $bundleScript
      }
    }
  }
}

$status = [ordered]@{
  checked_at = (Get-Date).ToUniversalTime().ToString("o")
  docker_cli_available = Docker-Cli-Available
  docker_desktop_path = if ($dockerDesktopPath) { [string]$dockerDesktopPath } else { $null }
  docker_engine_running = Docker-Engine-Available
  wsl_available = [bool]$wsl.available
  wsl_detail = [string]$wsl.detail
  bundled_images_present = [bool]$bundlePresent
  grobid_image = $GrobidImage
  metacheck_image = $MetaCheckImage
  grobid_image_loaded = Image-Exists $GrobidImage
  metacheck_image_loaded = Image-Exists $MetaCheckImage
  advanced_metacheck_ready = $false
}
$status.advanced_metacheck_ready = (
  $status.docker_cli_available -and
  $status.docker_engine_running -and
  ($status.bundled_images_present -or ($status.grobid_image_loaded -and $status.metacheck_image_loaded))
)

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $StatusPath) | Out-Null
$status | ConvertTo-Json -Depth 5 | Set-Content -Path $StatusPath -Encoding UTF8

Write-Host ""
Write-Host "[MetaCheck runtime]"
if ($status.advanced_metacheck_ready) {
  Write-Host "  [OK] Advanced MetaCheck runtime appears ready."
} elseif ($status.docker_cli_available) {
  Write-Host "  [..] Docker is installed. Advanced MetaCheck images may need loading/pulling."
  Write-Host "       RUN.bat will try automatically; BUNDLE_METACHECK_IMAGES.bat can prepare portable image tar files."
} else {
  Write-Host "  [..] Docker Desktop is not installed. Python MetaCheck-lite will still run."
}
Write-Host "  Status: $StatusPath"

exit 0
