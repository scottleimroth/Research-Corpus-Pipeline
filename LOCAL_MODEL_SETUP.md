# Local Model Setup

This guide is for using Research Corpus Pipeline without an API bill. The AI model runs on your own computer.

Local mode is advanced. If this feels confusing, use OpenRouter instead. OpenRouter is easier for most people and usually cheap for small corpuses.

## What Local Mode Means

Local mode means:

- no OpenRouter, OpenAI, Anthropic, or DeepSeek API key is required
- no per-paper API charge
- your computer does the AI work
- the local model becomes the paper evaluator
- setup is more technical
- processing can be much slower, often several minutes per paper attempt on large PDFs
- some local models fail the strict JSON format the evaluator requires
- scanned/image PDFs may be weaker than with paid vision models

## Computer Requirements

Local AI models need memory.

| Computer | What to expect |
|---|---|
| 8 GB RAM or less | Usually not practical. Use OpenRouter. |
| 16 GB RAM | Bare minimum for small models. Slow. |
| 32 GB RAM or more | Better. |
| Dedicated GPU | Faster if your local model software can use it. |

To check RAM on Windows:

1. Press `Ctrl+Shift+Esc`.
2. Click `Performance`.
3. Click `Memory`.
4. Look at the total memory amount.

## Option A: Ollama

Ollama is the simplest local option for most beginners.

### Step 1: Install Ollama

1. Go to `https://ollama.com`.
2. Click Download.
3. Choose Windows, macOS, or Linux.
4. Run the installer.
5. Restart your computer if the installer asks.

### Step 2: Download a Model

Open PowerShell or Command Prompt.

For a smaller model, run:

```powershell
ollama pull qwen3:8b
```

For a stronger but heavier model, if your computer can handle it:

```powershell
ollama pull qwen3.6:35b-a3b
```

The download can take several minutes.

### Step 3: Check the Model Works

Run:

```powershell
ollama run qwen3:8b
```

Type:

```text
Say hello in JSON.
```

If it replies, the model works.

Type this to exit:

```text
/bye
```

### Step 4: Run Pipeline Setup

1. Double-click `SETUP.bat`.
2. Choose `Local/free mode`.
3. When asked for the local OpenAI-compatible URL, use:

```text
http://localhost:11434/v1
```

4. When asked for the local model ID, use the exact model name you downloaded, for example:

```text
qwen3:8b
```

Then put PDFs in `papers-staging` and run `RUN.bat`.

## Option B: Lemonade

Use this if you already use Lemonade and it exposes an OpenAI-compatible server.

### Step 1: Start Lemonade

Open Lemonade and load the model you want to use.

Make sure the server/API port is enabled. The canonical Lemonade port is:

```text
http://localhost:13305/api/v1
```

(Some older Lemonade builds expose `http://localhost:11434/v1` instead, but note that 11434 is the default Ollama port too — confirm via `defaults.json` or `lemonade.exe status` which port your install actually uses.)

Use whichever one Lemonade shows on your computer.

### Step 2: Find the Exact Model ID

Open PowerShell and run:

```powershell
Invoke-RestMethod http://localhost:13305/api/v1/models | ConvertTo-Json -Depth 5
```

If your Lemonade server uses the older port `11434`, run:

```powershell
Invoke-RestMethod http://localhost:11434/v1/models | ConvertTo-Json -Depth 5
```

Look for the `id` field. Copy it exactly.

Example:

```text
hf.co/byteshape/Qwen3-30B-A3B-Instruct-2507-GGUF:Qwen3-30B-A3B-Instruct-2507-Q3_K_S-2.70bpw.gguf
```

### Step 3: Run Pipeline Setup

1. Double-click `SETUP.bat`.
2. Choose `Local/free mode`.
3. Paste the local URL Lemonade uses.
4. Paste the exact model ID from the `/models` output.

Do not edit `corpus_profile.json` by hand unless you know exactly what you are doing.

## How To Tell It Worked

After setup, run `RUN.bat`.

If there are no PDFs in `papers-staging`, the run should finish cleanly with:

```text
Status: done_clean
Processed unique candidates: 0 / 0
```

If there are PDFs in `papers-staging`, the pipeline should process them using the local model.

During a run, the console may show the internal evaluator key:

```text
geekom-qwen3-30b-local
```

That does not mean you need a Geekom computer. It is just the pipeline's internal name for "use the local OpenAI-compatible model selected during setup."

## Common Problems

### No local model URL is configured

The pipeline has no built-in guess for where your local server is — a wrong
built-in address is worse than an early, clear error. If you see an error
naming `local_openai_base_url` or `LOCAL_OPENAI_BASE_URL`, run `SETUP.bat`,
choose `Local/free mode`, and enter the URL and model ID again.

### Local model unavailable

This means the pipeline cannot see your local model server.

Check:

- Ollama or Lemonade is running.
- The URL is correct.
- The model ID is exact.
- The model is downloaded/loaded.

### Wrong model ID

Use the exact model name from:

```powershell
Invoke-RestMethod http://localhost:13305/api/v1/models | ConvertTo-Json -Depth 5
```

(Substitute `:11434/v1` if your Lemonade build still uses the old port.)

Do not guess the name.

### It is very slow

That is normal for local mode. If a paper takes more than about three minutes per attempt, local mode is probably not a good choice for that corpus. Use OpenRouter if you want easier and faster processing.

### Scanned PDFs are weak

Local text models are not always good at image/scanned PDFs. OpenRouter with a vision model is better for that case.

## Recommended Beginner Choice

For most beginners:

1. Try OpenRouter first.
2. Use local mode only if you specifically want no API costs and are comfortable installing local AI software.
