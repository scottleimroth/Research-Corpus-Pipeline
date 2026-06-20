$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
  $PSNativeCommandUseErrorActionPreference = $false
}

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SystemRoot = Split-Path -Parent $ScriptRoot
$ImageDir = Join-Path $SystemRoot "tools\docker-images"
$GrobidImage = if ($env:GROBID_DOCKER_IMAGE) { $env:GROBID_DOCKER_IMAGE } else { "grobid/grobid:0.9.0" }
$MetaCheckImage = if ($env:METACHECK_DOCKER_IMAGE) { $env:METACHECK_DOCKER_IMAGE } else { "scienceverse/metacheck:sha-2d0eb12" }
$GrobidTar = Join-Path $ImageDir "grobid-0.9.0.tar"
$MetaCheckTar = Join-Path $ImageDir "metacheck-latest.tar"

function Docker-Available {
  try {
    & docker version --format '{{.Server.Version}}' *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function Image-Exists {
  param([string]$Image)
  $id = & docker images -q $Image 2>$null | Select-Object -First 1
  return ($null -ne $id -and -not [string]::IsNullOrWhiteSpace([string]$id))
}

function Ensure-Image {
  param([string]$Image)
  if (Image-Exists $Image) {
    return
  }
  Write-Host "Pulling $Image"
  docker pull $Image
}

if (-not (Docker-Available)) {
  Write-Host "Docker is not running. Start Docker Desktop, then run this again."
  exit 1
}

New-Item -ItemType Directory -Force -Path $ImageDir | Out-Null

Ensure-Image $GrobidImage
Ensure-Image $MetaCheckImage

Write-Host "Saving $GrobidImage -> $GrobidTar"
docker save -o $GrobidTar $GrobidImage

Write-Host "Saving $MetaCheckImage -> $MetaCheckTar"
docker save -o $MetaCheckTar $MetaCheckImage

Write-Host "Docker image bundle written to $ImageDir"
exit 0
