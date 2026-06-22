# Public Release Operations

## 2026-06-22: AI Provider And Local Mode Setup

Changed the public release setup so downloaded copies can use OpenRouter, DeepSeek, OpenAI, Anthropic, or a local OpenAI-compatible model selected in `SETUP.bat`.

Why:

- Public users may not have Anthropic/OpenAI keys.
- OpenRouter is the recommended beginner/default route because it gives one key for cheap text evaluation plus vision-capable fallback models.
- Local/free mode must remain available, but testing showed it is too slow and schema-fragile to present as an easy default.

Implementation notes:

- `_system/launcher/public_ai_setup.ps1` writes `corpus_profile.json` without a UTF-8 BOM and records local endpoint/model ID when local mode is selected.
- `_system/pipeline/llm_providers.py` reads local endpoint/model ID from `corpus_profile.json`.
- `_system/pipeline/first_pass_finalize.py` now checks the configured evaluation ladder instead of requiring a paid Anthropic client.
- Local requests use `LOCAL_LLM_REQUEST_TIMEOUT_SEC` with a default of 900 seconds; cloud API calls keep the shorter 120-second timeout.
- `README.md` and `LOCAL_MODEL_SETUP.md` state that Anthropic is optional, OpenRouter is recommended, and local mode is advanced/slow.

Validation:

- Python compile checks passed for `config.py`, `llm_providers.py`, `first_pass_finalize.py`, and `corpus_live_all_staging.py`.
- Desktop local-mode test reached the configured Lemonade/Ollama-compatible local model, proving the local routing path works.
- The same local test took several minutes and failed strict evaluation JSON on the first attempt, so local mode remains documented as advanced/slow rather than recommended.

Operational constraints:

- For public users, recommend OpenRouter first.
- Local mode can be useful for experiments or privacy-sensitive no-cost runs, but it may take several minutes per paper attempt and may fail strict JSON depending on the local model.
- API-mode end-to-end ingest should be tested after setup with one staged PDF before broader public release.
