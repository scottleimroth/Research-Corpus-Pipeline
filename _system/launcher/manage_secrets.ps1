# Read/write folder-local encrypted API secrets (portable keys).
param(
    [string]$SystemRoot = "",
    [ValidateSet("configure", "migrate-from-user-env", "migrate-plaintext")]
    [string]$Mode = "configure"
)

$ErrorActionPreference = "Stop"
if (-not $SystemRoot) {
    $LauncherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    $SystemRoot = Split-Path -Parent $LauncherRoot
}

$SecretsDir = Join-Path $SystemRoot "secrets"
$EncFile = Join-Path $SecretsDir "api_keys.env.enc"
$LegacyFile = Join-Path $SecretsDir "api_keys.env"
$ProfileFile = Join-Path $SystemRoot "corpus_profile.json"
$ExampleFile = Join-Path $SecretsDir "api_keys.env.example"
$VaultPy = Join-Path $SystemRoot "secrets_vault.py"
$VenvPython = Join-Path $SystemRoot "venv\Scripts\python.exe"

function Write-Ok($m)   { Write-Host '  [OK]' $m -ForegroundColor Green }
function Write-Warn($m) { Write-Host '  [..]' $m -ForegroundColor Yellow }
function Write-Bad($m)  { Write-Host '  [!!]' $m -ForegroundColor Red }
function Write-JsonNoBom($path, $obj, $depth = 8) {
    $json = ($obj | ConvertTo-Json -Depth $depth) + [Environment]::NewLine
    [System.IO.File]::WriteAllText($path, $json, [System.Text.UTF8Encoding]::new($false))
}

if (-not (Test-Path $SecretsDir)) { New-Item -ItemType Directory -Path $SecretsDir -Force | Out-Null }

$profile = "personal"
if (Test-Path $ProfileFile) {
    try {
        $obj = Get-Content $ProfileFile -Raw | ConvertFrom-Json
        if ($obj.profile) { $profile = [string]$obj.profile }
    } catch { }
}

function Set-ProfileFields([string]$apiMode, [string]$evalModelKey) {
    $obj = [ordered]@{
        profile = $profile
        description = "Portable corpus. API keys in _system/secrets/api_keys.env.enc (AES-256); passphrase never stored in folder."
        api_mode = $apiMode
        eval_model_key = $evalModelKey
    }
    if (Test-Path $ProfileFile) {
        try {
            $existing = Get-Content $ProfileFile -Raw | ConvertFrom-Json
            if ($existing.description) { $obj.description = [string]$existing.description }
        } catch { }
    }
    Write-JsonNoBom $ProfileFile $obj 8
}

if (-not (Test-Path $ExampleFile)) {
    @(
        "# Keys are stored encrypted as api_keys.env.enc (AES-256).",
        "# Run SETUP.bat - supports DEEPSEEK_API_KEY and/or ANTHROPIC_API_KEY.",
        "# DEEPSEEK_API_KEY=sk-...",
        "# ANTHROPIC_API_KEY=sk-ant-..."
    ) | Set-Content -Path $ExampleFile -Encoding UTF8
}

function Get-VaultPython {
    if (Test-Path $VenvPython) { return $VenvPython }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "Python not found. Run SETUP.bat first."
}

function Save-EncryptedSecrets([hashtable]$secrets) {
    if ($secrets.Count -eq 0) { throw "No secrets to save" }
    $py = Get-VaultPython
    if (-not (Test-Path $VaultPy)) { throw "secrets_vault.py not found" }
    $lines = @()
    foreach ($k in $secrets.Keys) {
        $lines += "$k=$($secrets[$k])"
    }
    $payload = $lines -join "`n"
    if (Test-Path $EncFile) {
        $payload | & $py $VaultPy merge-secrets-stdin
    } else {
        $payload | & $py $VaultPy encrypt-secrets-stdin
    }
    if ($LASTEXITCODE -ne 0 -and -not (Test-Path $EncFile)) {
        throw "Encryption failed"
    }
    Write-Ok "Saved encrypted secrets to _system/secrets/api_keys.env.enc"
    if (Test-Path $LegacyFile) {
        Remove-Item $LegacyFile -Force -ErrorAction SilentlyContinue
        Write-Ok "Removed obsolete plaintext api_keys.env"
    }
}

function Read-LegacyPlaintext {
    $out = @{}
    if (-not (Test-Path $LegacyFile)) { return $out }
    foreach ($line in Get-Content $LegacyFile) {
        $line = $line.Trim()
        if ($line.StartsWith("#") -or -not $line.Contains("=")) { continue }
        $parts = $line.Split("=", 2)
        $out[$parts[0].Trim()] = $parts[1].Trim().Trim('"').Trim("'")
    }
    return $out
}

function Prompt-Key([string]$label, [string]$prefix, [switch]$Optional) {
    while ($true) {
        $resp = Read-Host $label
        if (-not $resp) {
            if ($Optional) { return "" }
            Write-Bad "Key required (or press Ctrl+C to skip setup)"
            continue
        }
        if ($prefix -and -not $resp.StartsWith($prefix)) {
            Write-Bad "Key must start with $prefix"
            continue
        }
        return $resp.Trim()
    }
}

function Configure-Keys {
    Write-Host ""
    Write-Host "Evaluation API (choose one):" -ForegroundColor Cyan
    Write-Host "  1) DeepSeek V4 Pro - recommended (default for paper ratings)"
    Write-Host "  2) Anthropic Sonnet - legacy default"
    Write-Host "  3) Both - DeepSeek for ratings, Anthropic for staging PDF vision"
    $choice = Read-Host "Choice [1]"
    if (-not $choice) { $choice = "1" }

    $secrets = @{}
    $apiMode = "deepseek"
    $evalKey = "deepseek-v4-pro"

    switch ($choice) {
        "2" {
            $apiMode = "anthropic"
            $evalKey = "sonnet-4.5"
            $k = Prompt-Key "Paste Anthropic sk-ant-... key" "sk-ant-"
            $secrets["ANTHROPIC_API_KEY"] = $k
        }
        "3" {
            $apiMode = "both"
            $evalKey = "deepseek-v4-pro"
            $ds = Prompt-Key "Paste DeepSeek API key" ""
            $secrets["DEEPSEEK_API_KEY"] = $ds
            $ant = Prompt-Key "Paste Anthropic sk-ant-... key (staging vision)" "sk-ant-"
            $secrets["ANTHROPIC_API_KEY"] = $ant
        }
        default {
            $apiMode = "deepseek"
            $evalKey = "deepseek-v4-pro"
            $ds = Prompt-Key "Paste DeepSeek API key (platform.deepseek.com)" ""
            $secrets["DEEPSEEK_API_KEY"] = $ds
        }
    }

    Save-EncryptedSecrets $secrets
    Set-ProfileFields $apiMode $evalKey
    Write-Ok "Profile: api_mode=$apiMode eval_model_key=$evalKey"
}

if ($Mode -eq "migrate-plaintext") {
    $legacy = Read-LegacyPlaintext
    if (Test-Path $EncFile) {
        Write-Ok "Encrypted secrets already present"
        if ($legacy.Count -gt 0 -and (Test-Path $LegacyFile)) {
            Remove-Item $LegacyFile -Force
        }
        exit 0
    }
    if ($legacy.Count -eq 0) {
        Write-Warn "No plaintext api_keys.env to migrate"
        exit 0
    }
    Write-Warn "Migrating plaintext keys to encrypted vault."
    Save-EncryptedSecrets $legacy
    exit 0
}

if ($Mode -eq "migrate-from-user-env") {
    if (Test-Path $EncFile) {
        Write-Ok "Encrypted vault already exists"
        exit 0
    }
    $legacy = Read-LegacyPlaintext
    if ($legacy.Count -gt 0) {
        Save-EncryptedSecrets $legacy
        exit 0
    }
    $secrets = @{}
    $userDs = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
    $userAnt = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "User")
    if ($userDs) { $secrets["DEEPSEEK_API_KEY"] = $userDs }
    if ($userAnt) { $secrets["ANTHROPIC_API_KEY"] = $userAnt }
    if ($secrets.Count -eq 0) {
        Write-Warn "No user DEEPSEEK_API_KEY or ANTHROPIC_API_KEY to migrate"
        exit 0
    }
    Save-EncryptedSecrets $secrets
    exit 0
}

Write-Host ""
Write-Host "Secrets: API keys (encrypted, folder-local)" -ForegroundColor Cyan

if (Test-Path $EncFile) {
    Write-Ok "Encrypted vault present at _system/secrets/api_keys.env.enc"
    $add = Read-Host "Add or replace a key? (deepseek/anthropic/n)"
    if ($add -match '^deepseek') {
        $k = Prompt-Key "Paste DeepSeek API key" ""
        Save-EncryptedSecrets @{ DEEPSEEK_API_KEY = $k }
        Set-ProfileFields "deepseek" "deepseek-v4-pro"
    } elseif ($add -match '^anthropic') {
        $k = Prompt-Key "Paste Anthropic sk-ant-... key" "sk-ant-"
        Save-EncryptedSecrets @{ ANTHROPIC_API_KEY = $k }
    }
    if (Test-Path $LegacyFile) { Remove-Item $LegacyFile -Force }
    exit 0
}

$legacy = Read-LegacyPlaintext
if ($legacy.Count -gt 0) {
    Write-Warn "Plaintext api_keys.env found - upgrading to encrypted storage."
    Save-EncryptedSecrets $legacy
    if ($legacy.ContainsKey("DEEPSEEK_API_KEY")) {
        Set-ProfileFields "deepseek" "deepseek-v4-pro"
    } else {
        Set-ProfileFields "anthropic" "sonnet-4.5"
    }
    exit 0
}

if ($profile -eq "public") {
    Write-Warn "PUBLIC corpus: DeepSeek (eval) or local Ollama only?"
    $hasApi = Read-Host "Configure DeepSeek API key for evaluation? (Y/n)"
    if ($hasApi -match '^[Nn]') {
        Set-ProfileFields "ollama_local" "deepseek-v4-pro"
        Write-Ok "PUBLIC profile: Ollama local mode (no paid eval API)."
        exit 0
    }
}

$userDs = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
if ($userDs -and -not (Test-Path $EncFile)) {
    $save = Read-Host "Save Windows user DEEPSEEK_API_KEY into encrypted vault? (Y/n)"
    if ($save -eq "" -or $save -match '^[Yy]') {
        Save-EncryptedSecrets @{ DEEPSEEK_API_KEY = $userDs }
        Set-ProfileFields "deepseek" "deepseek-v4-pro"
        exit 0
    }
}

Configure-Keys
