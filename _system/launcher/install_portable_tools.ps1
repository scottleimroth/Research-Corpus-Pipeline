# Download/copy Poppler, Tesseract, Ghostscript into _system/tools for folder portability.
param(
    [string]$SystemRoot = ""
)

$ErrorActionPreference = "Stop"
if (-not $SystemRoot) {
    $LauncherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    $SystemRoot = Split-Path -Parent $LauncherRoot
}

$ToolsRoot = Join-Path $SystemRoot "tools"
$PopplerRoot = Join-Path $ToolsRoot "poppler"
$PopplerBin = Join-Path $PopplerRoot "Library\bin"
$TessRoot = Join-Path $ToolsRoot "tesseract"
$GsRoot = Join-Path $ToolsRoot "ghostscript"
$GsBin = Join-Path $GsRoot "bin"

function Write-Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "  [..] $m" -ForegroundColor Yellow }
function Write-Bad($m)  { Write-Host "  [!!] $m" -ForegroundColor Red }

function Test-Tool($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Ensure-Dir($p) {
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
}

Ensure-Dir $ToolsRoot

# --- Poppler (pdftotext) ---
if (Test-Path (Join-Path $PopplerBin "pdftotext.exe")) {
    Write-Ok "Portable Poppler already present"
} else {
    Write-Warn "Installing portable Poppler into _system/tools/poppler"
    $zipUrl = "https://github.com/oschwartz10612/poppler-windows/releases/download/v24.08.0-0/Release-24.08.0-0.zip"
    $tmpZip = Join-Path $env:TEMP ("poppler-" + [guid]::NewGuid().ToString() + ".zip")
    $tmpExtract = Join-Path $env:TEMP ("poppler-" + [guid]::NewGuid().ToString())
    try {
        Invoke-WebRequest -Uri $zipUrl -OutFile $tmpZip -UseBasicParsing
        Expand-Archive -Path $tmpZip -DestinationPath $tmpExtract -Force
        if (Test-Path $PopplerRoot) { Remove-Item $PopplerRoot -Recurse -Force }
        $inner = Get-ChildItem $tmpExtract -Directory | Select-Object -First 1
        Move-Item $inner.FullName $PopplerRoot
        Write-Ok "Poppler installed to _system/tools/poppler"
    } catch {
        Write-Bad "Poppler download failed: $_"
    } finally {
        Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
        Remove-Item $tmpExtract -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --- Tesseract ---
$tessExe = Join-Path $TessRoot "tesseract.exe"
if (Test-Path $tessExe) {
    Write-Ok "Portable Tesseract already present"
} else {
    Write-Warn "Installing Tesseract into _system/tools/tesseract"
    $installed = $false
    if (Test-Path "C:\Program Files\Tesseract-OCR\tesseract.exe") {
        Ensure-Dir $TessRoot
        Copy-Item "C:\Program Files\Tesseract-OCR\*" $TessRoot -Recurse -Force
        $installed = Test-Path $tessExe
    }
    if (-not $installed) {
        try {
            winget install --id UB-Mannheim.TesseractOCR --silent --accept-package-agreements --accept-source-agreements | Out-Null
            if (Test-Path "C:\Program Files\Tesseract-OCR\tesseract.exe") {
                Ensure-Dir $TessRoot
                Copy-Item "C:\Program Files\Tesseract-OCR\*" $TessRoot -Recurse -Force
                $installed = Test-Path $tessExe
            }
        } catch {
            Write-Bad "Tesseract install failed: $_"
        }
    }
    if ($installed) { Write-Ok "Tesseract copied to _system/tools/tesseract" } else { Write-Bad "Tesseract not available" }
}

# --- Ghostscript (gswin64c) ---
$gsExe = Join-Path $GsBin "gswin64c.exe"
if (Test-Path $gsExe) {
    Write-Ok "Portable Ghostscript already present"
} else {
    Write-Warn "Installing Ghostscript into _system/tools/ghostscript"
    $installed = $false
    $src = Get-ChildItem "C:\Program Files\gs" -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
    if ($src -and (Test-Path (Join-Path $src.FullName "bin\gswin64c.exe"))) {
        Ensure-Dir $GsRoot
        Copy-Item (Join-Path $src.FullName "bin") $GsBin -Recurse -Force
        $installed = Test-Path $gsExe
    }
    if (-not $installed) {
        try {
            winget install --id ArtifexSoftware.GhostScript --silent --accept-package-agreements --accept-source-agreements | Out-Null
            $src = Get-ChildItem "C:\Program Files\gs" -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
            if ($src) {
                Ensure-Dir $GsRoot
                Copy-Item (Join-Path $src.FullName "bin") $GsBin -Recurse -Force
                $installed = Test-Path $gsExe
            }
        } catch {
            Write-Bad "Ghostscript install failed: $_"
        }
    }
    if ($installed) { Write-Ok "Ghostscript copied to _system/tools/ghostscript" } else { Write-Bad "Ghostscript not available" }
}

# Refresh PATH for this session from portable tools
$pathAdds = @()
foreach ($p in @($PopplerBin, $TessRoot, $GsBin)) {
    if ((Test-Path $p) -and ($env:Path -notlike "*$p*")) { $pathAdds += $p }
}
if ($pathAdds.Count -gt 0) {
    $env:Path = ($pathAdds -join ";") + ";" + $env:Path
}

$ok = (Test-Path (Join-Path $PopplerBin "pdftotext.exe")) -and (Test-Path $tessExe) -and (Test-Path $gsExe)
if (-not $ok) {
    Write-Bad "One or more portable PDF/OCR tools are still missing after install."
    exit 1
}
Write-Ok "Portable PDF/OCR tools ready under _system/tools"
exit 0
