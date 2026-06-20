"""
Shared constants for Option B evaluation pipeline (dry-run safe defaults).
"""
from pathlib import Path

import config  # noqa: E402

# Staging paths (no DB writes from batch tools by default)
EVAL_STAGING = config.STAGING / "eval-batch"
EVAL_PLANS_DIR = EVAL_STAGING / "plans"
EVAL_RESULTS_DIR = EVAL_STAGING / "results"
EVAL_LEDGER_PATH = EVAL_STAGING / "eval_jobs.jsonl"
EVAL_COST_REPORT_PATH = EVAL_STAGING / "cost_estimate.json"

# Models (routing — production evaluate_paper.py unchanged until wired)
MODEL_SONNET = "claude-sonnet-4-5-20250929"
MODEL_HAIKU = "claude-haiku-4-5-20251001"

# Pass types
PASS_FIRST = "first_pass"
PASS_ESCALATION = "sonnet_escalation"
PASS_REDTEAM = "red_team"

# Schema versions
SCHEMA_V1 = "v1_full"
SCHEMA_V2 = "v2_compact"

# Pricing USD per million tokens (verify against current Anthropic pricing)
PRICE = {
    MODEL_SONNET: {"input": 3.00, "output": 15.00, "batch_discount": 0.50},
    MODEL_HAIKU: {"input": 0.80, "output": 4.00, "batch_discount": 0.50},
}

# Prompt cache: fraction of repeated prefix tokens billed at cache read rate (estimate)
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_FRACTION_OF_PREFIX = 0.90
CACHE_READ_RATE_FACTOR = 0.10  # ~90% cheaper than base input on cache hits

# Calibration from 10-paper batch (single-run, full_text_within_context, Sonnet×2)
CALIBRATION = {
    "papers": 10,
    "calls_per_paper": 2,
    "avg_input_tokens_per_call": 41_000,
    "avg_output_tokens_per_call": 3_700,
    "avg_input_chars": 72_000,
    "cost_usd_single_run_10": 3.50,
    "cost_usd_with_duplicates_10": 7.00,
}

# Feature flags (env overrides; all off by default)
def _env_bool(name: str, default: bool = False) -> bool:
    import os
    v = os.environ.get(name, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


USE_SCHEMA_V2 = _env_bool("EVAL_USE_SCHEMA_V2", False)
USE_BATCH_API = _env_bool("EVAL_USE_BATCH_API", False)
USE_PROMPT_CACHE = _env_bool("EVAL_USE_PROMPT_CACHE", False)
USE_CONDITIONAL_REDTEAM = _env_bool("EVAL_USE_CONDITIONAL_REDTEAM", False)
USE_MODEL_ROUTING = _env_bool("EVAL_USE_MODEL_ROUTING", False)
ALLOW_LIVE_API = _env_bool("EVAL_ALLOW_LIVE_API", False)
ALLOW_DB_WRITE = _env_bool("EVAL_ALLOW_DB_WRITE", False)
