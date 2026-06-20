param(
  [int]$GrobidPort = 8070,
  [int]$MetaCheckPort = 2005,
  [int]$DockerWaitSeconds = 180
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
  $PSNativeCommandUseErrorActionPreference = $false
}
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SystemRoot = Split-Path -Parent $ScriptRoot
$ImageDir = Join-Path $SystemRoot "tools\docker-images"
$PatchedMetaCheckHelpers = Join-Path $SystemRoot "tools\metacheck-plumber\helpers.R"
$GrobidImage = if ($env:GROBID_DOCKER_IMAGE) { $env:GROBID_DOCKER_IMAGE } else { "grobid/grobid:0.9.0" }
$MetaCheckImage = if ($env:METACHECK_DOCKER_IMAGE) { $env:METACHECK_DOCKER_IMAGE } else { "scienceverse/metacheck:sha-2d0eb12" }
$GrobidTar = Join-Path $ImageDir "grobid-0.9.0.tar"
$MetaCheckTar = Join-Path $ImageDir "metacheck-latest.tar"

function Test-HttpPort {
  param([string]$Url)
  try {
    $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
    return ($resp.StatusCode -lt 500)
  } catch {
    return $false
  }
}

function Docker-Available {
  try {
    & docker version --format '{{.Server.Version}}' *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function Docker-Cli-Available {
  try {
    & docker --version *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function Start-Docker-Desktop-If-Needed {
  if (Docker-Available) {
    return $true
  }

  $candidates = @(
    "C:\Program Files\Docker\Docker\Docker Desktop.exe",
    "$env:LOCALAPPDATA\Docker\Docker Desktop.exe"
  )
  $exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $exe) {
    return $false
  }

  Write-Host "Starting Docker Desktop..."
  try {
    Start-Process -FilePath $exe -WindowStyle Hidden | Out-Null
  } catch {
    Write-Host "Could not launch Docker Desktop: $($_.Exception.Message)"
    return $false
  }

  $deadline = (Get-Date).AddSeconds($DockerWaitSeconds)
  while ((Get-Date) -lt $deadline) {
    if (Docker-Available) {
      Write-Host "Docker Desktop is ready."
      return $true
    }
    Start-Sleep -Seconds 5
  }
  Write-Host "Docker Desktop did not become ready within $DockerWaitSeconds seconds."
  return $false
}

function Image-Exists {
  param([string]$Image)
  $id = & docker images -q $Image 2>$null | Select-Object -First 1
  return ($null -ne $id -and -not [string]::IsNullOrWhiteSpace([string]$id))
}

function Ensure-Image {
  param(
    [string]$Image,
    [string]$TarPath
  )
  if (Image-Exists $Image) {
    return $true
  }
  if (Test-Path $TarPath) {
    Write-Host "Loading bundled Docker image: $TarPath"
    docker load -i $TarPath | Out-Null
    if (Image-Exists $Image) {
      return $true
    }
  }
  Write-Host "Pulling Docker image: $Image"
  & docker pull $Image
  return (Image-Exists $Image)
}

function Container-Exists {
  param([string]$Name)
  $existing = & docker ps -a --filter "name=^/$Name$" --format "{{.Names}}" 2>$null
  $first = $existing | Select-Object -First 1
  return ($null -ne $first -and [string]$first -eq $Name)
}

function Container-Running {
  param([string]$Name)
  $running = & docker ps --filter "name=^/$Name$" --format "{{.Names}}" 2>$null
  $first = $running | Select-Object -First 1
  return ($null -ne $first -and [string]$first -eq $Name)
}

function Ensure-Container {
  param(
    [string]$Name,
    [string]$Image,
    [int]$HostPort,
    [int]$ContainerPort,
    [string[]]$ExtraArgs = @()
  )

  if (Container-Running $Name) {
    Write-Host "MetaCheck helper already running: $Name"
    return
  }

  if (Container-Exists $Name) {
    Write-Host "Starting existing MetaCheck helper: $Name"
    docker start $Name | Out-Null
    return
  }

  Write-Host "Creating MetaCheck helper: $Name ($Image)"
  $args = @("run", "-d", "--name", $Name, "-p", "${HostPort}:${ContainerPort}") + $ExtraArgs + @($Image)
  & docker @args | Out-Null
}

function Ensure-MetaCheck-Container-Patch {
  param([string]$Name)

  if (-not (Container-Exists $Name)) {
    return
  }
  if (-not (Test-Path $PatchedMetaCheckHelpers)) {
    Write-Host "Patched MetaCheck helper not found: $PatchedMetaCheckHelpers"
    return
  }

  if (-not (Container-Running $Name)) {
    docker start $Name | Out-Null
  }

  $localHash = (Get-FileHash -Algorithm SHA256 -Path $PatchedMetaCheckHelpers).Hash.ToLowerInvariant()
  $remoteHash = ""
  try {
    $remoteHash = (& docker exec $Name sha256sum /srv/metacheck/inst/plumber/utils/helpers.R 2>$null | Select-Object -First 1)
    if ($remoteHash) {
      $remoteHash = ([string]$remoteHash).Split(" ")[0].Trim().ToLowerInvariant()
    }
  } catch {
    $remoteHash = ""
  }
  if ($remoteHash -eq $localHash) {
    Write-Host "Local MetaCheck helper patch already applied: $Name"
    return
  }

  Write-Host "Applying local MetaCheck helper patch: $Name"
  & docker cp $PatchedMetaCheckHelpers "${Name}:/srv/metacheck/inst/plumber/utils/helpers.R" | Out-Null
  & docker restart $Name | Out-Null
}

$grobidUrl = "http://127.0.0.1:$GrobidPort/api/isalive"
$metacheckUrl = "http://127.0.0.1:$MetaCheckPort/__docs__/"

if (-not (Docker-Cli-Available)) {
  Write-Host "Docker command is not installed. Real MetaCheck cannot run."
  exit 0
}

if (-not (Start-Docker-Desktop-If-Needed)) {
  Write-Host "Docker is not running. Real MetaCheck cannot run."
  exit 0
}

try {
  if (-not (Ensure-Image -Image $GrobidImage -TarPath $GrobidTar)) {
    Write-Host "GROBID image unavailable. Real MetaCheck cannot run."
    exit 0
  }
  if (-not (Ensure-Image -Image $MetaCheckImage -TarPath $MetaCheckTar)) {
    Write-Host "MetaCheck image unavailable. Real MetaCheck cannot run."
    exit 0
  }

  if (-not (Test-HttpPort $grobidUrl)) {
    Ensure-Container `
      -Name "corpus-metacheck-grobid" `
      -Image $GrobidImage `
      -HostPort $GrobidPort `
      -ContainerPort 8070 `
      -ExtraArgs @("--init", "--ulimit", "core=0")
  }

  if (-not (Test-HttpPort $metacheckUrl)) {
    Ensure-Container `
      -Name "corpus-metacheck-service" `
      -Image $MetaCheckImage `
      -HostPort $MetaCheckPort `
      -ContainerPort 2005
  }

  Ensure-MetaCheck-Container-Patch -Name "corpus-metacheck-service"

  $deadline = (Get-Date).AddSeconds(180)
  do {
    Start-Sleep -Seconds 5
    $grobidOk = Test-HttpPort $grobidUrl
    $metacheckOk = Test-HttpPort $metacheckUrl
    if ($grobidOk -and $metacheckOk) {
      break
    }
  } while ((Get-Date) -lt $deadline)

  $grobidOk = Test-HttpPort $grobidUrl
  $metacheckOk = Test-HttpPort $metacheckUrl

  if ($grobidOk -and $metacheckOk) {
    Write-Host "MetaCheck helpers are ready."
  } else {
    Write-Host "MetaCheck helpers are not fully ready yet. Ingest preflight will block until they are reachable."
  }
  exit 0
} catch {
  Write-Host "Could not start MetaCheck helpers: $($_.Exception.Message)"
  Write-Host "Real MetaCheck cannot run until this is fixed."
  exit 0
}
