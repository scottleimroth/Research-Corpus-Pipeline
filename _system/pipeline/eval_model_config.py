"""Resolve evaluation model key, API id, and escalation ladder (DeepSeek-first).

Cost routing policy (quality-preserving):
- Use the cheapest model that can do the job; escalate only on parse failure,
  invalid schema, or low confidence thresholds below.
- Text eval: DeepSeek Pro → Reasoner → Sonnet → Opus → local Geekom only as outage fallback.
- Staging vision: Haiku → Sonnet (confidence / JSON gate); Opus only for
  destructive deletes or very low confidence (see staging_needs_opus_adjudication).
- Full paper text on scans: ocrmypdf (not LLM).
"""

from __future__ import annotations

import os

from llm_providers import LOCAL_MODEL_KEY, MODELS, local_openai_available

DEFAULT_EVAL_MODEL_KEY = "deepseek-v4-pro"
ANTHROPIC_EVAL_MODEL_KEY = "sonnet-4.5"
OPENROUTER_EVAL_MODEL_KEY = "openrouter-deepseek-v4-pro"

# Default: Pro → Reasoner when stuck → Sonnet → Opus → local outage fallback.
ESCALATION_LADDER_DEFAULT = (
    "deepseek-v4-pro",
    "deepseek-reasoner",
    "sonnet-4.5",
    "opus-4",
    LOCAL_MODEL_KEY,
)

# Staging / recovery vision (see pipeline_llm.VISION_DEFAULT_MODELS).
VISION_ESCALATION_MIN_CONFIDENCE = 0.90
# Opus: second opinion on deletes unless very confident; ingest rarely needs Opus.
STAGING_OPUS_DELETE_CONFIDENCE_BELOW = 0.95
STAGING_OPUS_INGEST_CONFIDENCE_BELOW = 0.70


def staging_needs_opus_adjudication(decision: str, confidence: float) -> bool:
    """Whether staging should spend on Opus after Haiku/Sonnet vision."""
    action = (decision or "").strip().upper()
    if action.startswith("DELETE_"):
        return confidence < STAGING_OPUS_DELETE_CONFIDENCE_BELOW
    return confidence < STAGING_OPUS_INGEST_CONFIDENCE_BELOW


def default_eval_model_key(api_mode: str) -> str:
    mode = (api_mode or "deepseek").lower()
    if mode == "anthropic":
        return ANTHROPIC_EVAL_MODEL_KEY
    if mode == "openrouter":
        return OPENROUTER_EVAL_MODEL_KEY
    return DEFAULT_EVAL_MODEL_KEY


def resolve_eval_model_key(*, profile: dict | None = None, api_mode: str = "deepseek") -> str:
    env_key = os.environ.get("EVAL_MODEL_KEY", "").strip()
    if env_key:
        return env_key
    if profile and str(profile.get("eval_model_key") or "").strip():
        return str(profile["eval_model_key"]).strip()
    return default_eval_model_key(api_mode)


def resolve_escalation_ladder(*, profile: dict | None = None) -> tuple[str, ...]:
    if profile and isinstance(profile.get("eval_escalation"), list):
        keys = [str(k).strip() for k in profile["eval_escalation"] if str(k).strip()]
        if keys:
            return tuple(keys)
    return ESCALATION_LADDER_DEFAULT


def resolve_model_api_id(model_key: str) -> str:
    if model_key not in MODELS:
        raise ValueError(f"Unknown eval model key: {model_key}. Known: {sorted(MODELS)}")
    return MODELS[model_key][1]


def model_provider(model_key: str) -> str:
    if model_key not in MODELS:
        raise ValueError(f"Unknown eval model key: {model_key}")
    return MODELS[model_key][0]


def eval_api_key_available(model_key: str) -> bool:
    provider = model_provider(model_key)
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    if provider == "deepseek":
        return bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY", "").strip())
    if provider == "openrouter":
        return bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
    if provider == "local-openai":
        return local_openai_available()
    return True


def build_eval_escalation_ladder(
    *,
    profile: dict | None = None,
    start_key: str | None = None,
    include_anthropic_fallback: bool = True,
    include_local_fallback: bool = True,
    keep_local_private: bool | None = None,
    prefer_local: bool = False,
) -> list[str]:
    """Ordered model keys to try; filters to models with API keys available."""
    if keep_local_private is None:
        keep_local_private = bool(
            os.environ.get("CORPUS_LLM_KEEP_LOCAL", "").strip().lower() in {"1", "true", "yes", "on"}
            or (profile and profile.get("keep_local_private"))
        )

    if keep_local_private:
        keys = [LOCAL_MODEL_KEY]
    else:
        keys = list(resolve_escalation_ladder(profile=profile))
        if include_local_fallback and LOCAL_MODEL_KEY not in keys:
            keys.append(LOCAL_MODEL_KEY)
        if prefer_local and LOCAL_MODEL_KEY in keys:
            keys = [LOCAL_MODEL_KEY] + [k for k in keys if k != LOCAL_MODEL_KEY]
    if start_key and start_key in keys:
        idx = keys.index(start_key)
        keys = keys[idx:] + keys[:idx]
    elif start_key:
        keys = [start_key] + [k for k in keys if k != start_key]
    if not include_anthropic_fallback:
        keys = [k for k in keys if model_provider(k) != "anthropic"]
    if not include_local_fallback and not keep_local_private:
        keys = [k for k in keys if k != LOCAL_MODEL_KEY]
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key in seen or key not in MODELS:
            continue
        if not eval_api_key_available(key):
            continue
        seen.add(key)
        out.append(key)
    return out


def assert_eval_api_key(model_key: str) -> None:
    if not eval_api_key_available(model_key):
        provider = model_provider(model_key)
        if provider == "anthropic":
            raise RuntimeError("ANTHROPIC_API_KEY is required for Anthropic evaluation.")
        if provider == "deepseek":
            raise RuntimeError("DEEPSEEK_API_KEY is required for DeepSeek evaluation.")
        if provider == "openai":
            raise RuntimeError("OPENAI_API_KEY is required for OpenAI evaluation.")
        if provider == "openrouter":
            raise RuntimeError("OPENROUTER_API_KEY is required for OpenRouter evaluation.")
        if provider == "local-openai":
            raise RuntimeError("Local Lemonade model is unavailable for local evaluation.")
        raise RuntimeError(f"API key not available for model {model_key}")
