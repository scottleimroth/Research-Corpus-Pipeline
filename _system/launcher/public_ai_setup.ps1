param(
    [Parameter(Mandatory=$true)]
    [string]$SystemRoot
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $SystemRoot
$ProfileFile = Join-Path $SystemRoot "corpus_profile.json"
$SecretsVault = Join-Path $SystemRoot "secrets_vault.py"
$Python = Join-Path $SystemRoot "venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

function Write-Info($msg) { Write-Host $msg -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [..] $msg" -ForegroundColor Yellow }

function Save-Profile($apiMode, $evalKey, $visionKey, $description, $evalEscalation) {
    $profile = [ordered]@{
        profile = "public"
        api_mode = $apiMode
        eval_model_key = $evalKey
        eval_escalation = $evalEscalation
        vision_model_key = $visionKey
        allow_anthropic = $true
        allow_openai = $true
        description = $description
    }
    $profile | ConvertTo-Json -Depth 6 | Set-Content -Path $ProfileFile -Encoding UTF8
}

function Save-Key($name, $value) {
    if (-not $value) { return }
    $payload = "$name=$value"
    $payload | & $Python $SecretsVault encrypt-secrets-stdin
    if ($LASTEXITCODE -ne 0) {
        throw "Could not save encrypted API key."
    }
}

function Explain-Choices {
    Write-Host ""
    Write-Info "Plain-English guide"
    Write-Host ""
    Write-Host "This program uses AI for two jobs:"
    Write-Host "  1. Reading/rating normal paper text."
    Write-Host "  2. Looking at scanned/image PDFs when text extraction is unclear."
    Write-Host ""
    Write-Host "Recommended for most people:"
    Write-Host "  OpenRouter. It is one account/key that can use cheap text and vision models."
    Write-Host "  You can add a small amount of credit, such as 5-10 USD, and set limits."
    Write-Host ""
    Write-Host "Other choices:"
    Write-Host "  DeepSeek: cheap and good for normal text PDFs, but weak/no vision fallback."
    Write-Host "  OpenAI: easy if you already have it, but usually more expensive."
    Write-Host "  Anthropic: strong quality, but usually more expensive."
    Write-Host "  Local/free: no API bill, but needs local AI software and is harder to set up."
    Write-Host ""
}

Write-Host ""
Write-Info "[Public AI setup]"
Write-Host "Choose how this public corpus should use AI models."
Write-Host ""

while ($true) {
    Write-Host "1. Cheapest recommended: OpenRouter"
    Write-Host "2. DeepSeek only"
    Write-Host "3. OpenAI"
    Write-Host "4. Anthropic"
    Write-Host "5. Local/free mode (advanced)"
    Write-Host "6. I'm not sure - explain this"
    Write-Host ""
    $choice = Read-Host "Choose 1-6"

    if ($choice -eq "6") {
        Explain-Choices
        continue
    }

    if ($choice -eq "1") {
        Write-Host ""
        Write-Info "OpenRouter setup"
        Write-Host "1. Go to https://openrouter.ai"
        Write-Host "2. Create an account"
        Write-Host "3. Add a small credit amount, e.g. 5-10 USD"
        Write-Host "4. Create an API key"
        Write-Host "5. Paste it below"
        Write-Host ""
        $key = Read-Host "Paste OPENROUTER_API_KEY (starts with sk-or-), or press Enter to skip"
        Save-Profile "openrouter" "openrouter-deepseek-v4-pro" "openrouter-qwen3-vl-32b" "Public mode using OpenRouter cheap text + vision models." @("openrouter-deepseek-v4-pro", "openrouter-kimi-k2.5")
        if ($key) { Save-Key "OPENROUTER_API_KEY" $key }
        Write-Ok "Configured public OpenRouter mode."
        break
    }

    if ($choice -eq "2") {
        Write-Host ""
        Write-Info "DeepSeek setup"
        Write-Host "DeepSeek is cheap and good for normal text PDFs."
        Write-Host "It is not the best choice for scanned/image PDFs because vision support is limited."
        Write-Host "1. Go to https://platform.deepseek.com"
        Write-Host "2. Create an API key"
        Write-Host "3. Paste it below"
        Write-Host ""
        $key = Read-Host "Paste DEEPSEEK_API_KEY, or press Enter to skip"
        Save-Profile "deepseek" "deepseek-v4-pro" "" "Public mode using direct DeepSeek. Cheap text evaluation; limited vision fallback." @("deepseek-v4-pro")
        if ($key) { Save-Key "DEEPSEEK_API_KEY" $key }
        Write-Ok "Configured public DeepSeek mode."
        break
    }

    if ($choice -eq "3") {
        Write-Host ""
        Write-Info "OpenAI setup"
        Write-Host "Use this if you already have an OpenAI API key."
        Write-Host "It supports text and vision, but may cost more than OpenRouter/DeepSeek."
        Write-Host "1. Go to https://platform.openai.com/api-keys"
        Write-Host "2. Create an API key"
        Write-Host "3. Paste it below"
        Write-Host ""
        $key = Read-Host "Paste OPENAI_API_KEY, or press Enter to skip"
        Save-Profile "openai" "gpt-5-mini" "gpt-5-mini" "Public mode using OpenAI if the user already has an OpenAI key." @("gpt-5-mini", "gpt-5-nano")
        if ($key) { Save-Key "OPENAI_API_KEY" $key }
        Write-Ok "Configured public OpenAI mode."
        break
    }

    if ($choice -eq "4") {
        Write-Host ""
        Write-Info "Anthropic setup"
        Write-Host "Use this if you already have an Anthropic/Claude API key."
        Write-Host "Quality is strong, but it is usually more expensive."
        Write-Host "1. Go to https://console.anthropic.com/settings/keys"
        Write-Host "2. Create an API key"
        Write-Host "3. Paste it below"
        Write-Host ""
        $key = Read-Host "Paste ANTHROPIC_API_KEY, or press Enter to skip"
        Save-Profile "anthropic" "sonnet-4.5" "" "Public mode using Anthropic/Claude for users who already have it." @("sonnet-4.5", "haiku-4.5")
        if ($key) { Save-Key "ANTHROPIC_API_KEY" $key }
        Write-Ok "Configured public Anthropic mode."
        break
    }

    if ($choice -eq "5") {
        Write-Host ""
        Write-Info "Local/free mode"
        Write-Host "This avoids API bills, but it needs local AI software and may be slower/weaker."
        Write-Host "Choose this only if you already know how to run Ollama or a local OpenAI-compatible model."
        Save-Profile "ollama_local" "qwen3-8b" "" "Public local/free mode. Requires local model setup by the user." @("qwen3-8b")
        Write-Ok "Configured local/free mode."
        break
    }

    Write-Warn "Please choose a number from 1 to 6."
}
