"""Unified DeepSeek-first LLM calls with escalation to Sonnet/Opus."""

from __future__ import annotations

import json
import re
from typing import Any

import config
from eval_model_config import (
    VISION_ESCALATION_MIN_CONFIDENCE,
    build_eval_escalation_ladder,
)
from llm_providers import LLM_REQUEST_TIMEOUT_SEC, MODELS, call_llm
from llm_providers import LOCAL_MODEL_KEY


def _strip_json_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1]
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
    return t


def parse_json_from_llm(text: str) -> dict[str, Any]:
    raw = _strip_json_fences(text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            return json.loads(m.group(0))
        raise


def call_llm_escalation(
    system: str,
    user_text: str,
    *,
    max_tokens: int = 4096,
    start_key: str | None = None,
    keep_local_private: bool | None = None,
    prefer_local: bool = False,
    include_local_fallback: bool = True,
    include_anthropic_fallback: bool = True,
) -> tuple[str, dict[str, Any], str]:
    """Return (text, usage, model_key_used). Raises if entire ladder fails."""
    ladder = build_eval_escalation_ladder(
        profile=config.CORPUS_PROFILE,
        start_key=start_key,
        keep_local_private=keep_local_private,
        prefer_local=prefer_local,
        include_local_fallback=include_local_fallback,
        include_anthropic_fallback=include_anthropic_fallback,
    )
    if not ladder:
        raise RuntimeError("No evaluation models available. Run SETUP.bat and choose OpenRouter, DeepSeek, OpenAI, Anthropic, or local/free mode.")

    last_err: Exception | None = None
    first_model = ladder[0]
    if first_model == LOCAL_MODEL_KEY:
        reason = "keep-local/private route" if keep_local_private else "local-preferred route"
        print(f"    LLM local route selected ({reason}): {LOCAL_MODEL_KEY}", flush=True)
    for model_key in ladder:
        try:
            if model_key == LOCAL_MODEL_KEY and last_err is not None:
                print(
                    f"    LLM local fallback firing after upstream failure: {last_err}",
                    flush=True,
                )
            text, usage = call_llm(model_key, system, user_text, max_tokens=max_tokens)
            usage = dict(usage or {})
            usage["model_key"] = model_key
            if model_key == LOCAL_MODEL_KEY:
                usage["fallback_trigger"] = (
                    "keep_local_private"
                    if keep_local_private
                    else ("prefer_local" if prefer_local else "upstream_failure")
                )
            return text, usage, model_key
        except Exception as exc:
            last_err = exc
            print(f"    LLM {model_key} failed: {exc}", flush=True)
            continue
    raise RuntimeError(f"LLM escalation exhausted ({ladder}): {last_err}") from last_err


def call_llm_json_escalation(
    system: str,
    user_text: str,
    *,
    max_tokens: int = 4096,
    start_key: str | None = None,
    keep_local_private: bool | None = None,
    prefer_local: bool = False,
    include_local_fallback: bool = True,
    include_anthropic_fallback: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    text, usage, model_key = call_llm_escalation(
        system,
        user_text,
        max_tokens=max_tokens,
        start_key=start_key,
        keep_local_private=keep_local_private,
        prefer_local=prefer_local,
        include_local_fallback=include_local_fallback,
        include_anthropic_fallback=include_anthropic_fallback,
    )
    return parse_json_from_llm(text), usage, model_key


# Default vision ladder: cheap first pass, Sonnet when Haiku is uncertain or unparseable.
VISION_DEFAULT_MODELS = (
    MODELS["haiku-4.5"][1],
    MODELS["sonnet-4.5"][1],
)


def public_vision_available() -> bool:
    if str(config.CORPUS_API_MODE or "").lower() == "openrouter" and config.OPENROUTER_API_KEY:
        return True
    if str(config.CORPUS_API_MODE or "").lower() == "openai" and getattr(config, "OPENAI_API_KEY", ""):
        return True
    if config.ANTHROPIC_API_KEY:
        return True
    return False


def _anthropic_blocks_to_openai_content(content_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in content_blocks:
        btype = block.get("type")
        if btype == "text":
            out.append({"type": "text", "text": str(block.get("text") or "")})
        elif btype == "image":
            source = block.get("source") if isinstance(block.get("source"), dict) else {}
            data = str(source.get("data") or "")
            media_type = str(source.get("media_type") or "image/png")
            if data:
                out.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{data}"},
                    }
                )
    return out


def call_openrouter_vision_json(
    *,
    system: str,
    content_blocks: list[dict[str, Any]],
    max_tokens: int = 700,
    model_key: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any], str | None]:
    from openai import OpenAI
    from llm_providers import LLM_REQUEST_TIMEOUT_SEC, MODELS, OPENROUTER_BASE_URL

    if not config.OPENROUTER_API_KEY:
        return None, {"error": "no_openrouter_key"}, None
    model_key = model_key or str(config.CORPUS_PROFILE.get("vision_model_key") or "openrouter-qwen3-vl-32b")
    if model_key not in MODELS:
        model_key = "openrouter-qwen3-vl-32b"
    provider, model_id, cin, cout = MODELS[model_key]
    if provider != "openrouter":
        return None, {"error": f"not_openrouter_vision_model:{model_key}"}, None
    client = OpenAI(
        api_key=config.OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
        timeout=LLM_REQUEST_TIMEOUT_SEC,
        default_headers={
            "HTTP-Referer": "https://github.com/scottleimroth/Research-Corpus-Pipeline",
            "X-Title": "Research-Corpus-Pipeline",
        },
    )
    try:
        resp = client.chat.completions.create(
            model=model_id,
            max_tokens=max_tokens,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": _anthropic_blocks_to_openai_content(content_blocks)},
            ],
            timeout=LLM_REQUEST_TIMEOUT_SEC,
        )
        text = resp.choices[0].message.content or ""
        data = parse_json_from_llm(text)
        in_tok = getattr(resp.usage, "prompt_tokens", 0) if resp.usage else 0
        out_tok = getattr(resp.usage, "completion_tokens", 0) if resp.usage else 0
        return data, {
            "provider": "openrouter",
            "model": model_id,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost_usd": (in_tok * cin + out_tok * cout) / 1_000_000,
        }, model_id
    except Exception as exc:
        return None, {"error": f"openrouter_vision_error:{exc}"}, model_id


def call_openai_vision_json(
    *,
    system: str,
    content_blocks: list[dict[str, Any]],
    max_tokens: int = 700,
    model_key: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any], str | None]:
    from openai import OpenAI
    from llm_providers import LLM_REQUEST_TIMEOUT_SEC, MODELS

    if not getattr(config, "OPENAI_API_KEY", ""):
        return None, {"error": "no_openai_key"}, None
    model_key = model_key or str(config.CORPUS_PROFILE.get("vision_model_key") or "gpt-5-mini")
    if model_key not in MODELS:
        model_key = "gpt-5-mini"
    provider, model_id, cin, cout = MODELS[model_key]
    if provider != "openai":
        return None, {"error": f"not_openai_vision_model:{model_key}"}, None
    client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=LLM_REQUEST_TIMEOUT_SEC)
    try:
        resp = client.chat.completions.create(
            model=model_id,
            max_tokens=max_tokens,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": _anthropic_blocks_to_openai_content(content_blocks)},
            ],
            timeout=LLM_REQUEST_TIMEOUT_SEC,
        )
        text = resp.choices[0].message.content or ""
        data = parse_json_from_llm(text)
        in_tok = getattr(resp.usage, "prompt_tokens", 0) if resp.usage else 0
        out_tok = getattr(resp.usage, "completion_tokens", 0) if resp.usage else 0
        return data, {
            "provider": "openai",
            "model": model_id,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost_usd": (in_tok * cin + out_tok * cout) / 1_000_000,
        }, model_id
    except Exception as exc:
        return None, {"error": f"openai_vision_error:{exc}"}, model_id


def _vision_result_needs_escalation(
    data: dict[str, Any] | None,
    *,
    min_confidence: float | None,
    valid_decisions: frozenset[str] | None,
) -> bool:
    if not data:
        return True
    decision = str(data.get("decision") or "").strip().upper()
    if valid_decisions is not None and decision not in valid_decisions:
        return True
    if min_confidence is not None:
        confidence = float(data.get("confidence") or 0.0)
        if confidence < min_confidence:
            return True
    return False


def call_anthropic_vision_json(
    *,
    system: str,
    content_blocks: list[dict[str, Any]],
    max_tokens: int = 700,
    models: tuple[str, ...] | None = None,
    min_confidence: float | None = VISION_ESCALATION_MIN_CONFIDENCE,
    valid_decisions: frozenset[str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any], str | None]:
    """Multimodal JSON via Anthropic. Default: Haiku then Sonnet; escalates on bad JSON or low confidence."""
    if str(config.CORPUS_API_MODE or "").lower() == "openrouter" and config.OPENROUTER_API_KEY:
        data, usage, model_id = call_openrouter_vision_json(
            system=system,
            content_blocks=content_blocks,
            max_tokens=max_tokens,
        )
        if not _vision_result_needs_escalation(
            data, min_confidence=min_confidence, valid_decisions=valid_decisions
        ):
            return data, usage, model_id
        return data, usage, model_id

    if str(config.CORPUS_API_MODE or "").lower() == "openai" and getattr(config, "OPENAI_API_KEY", ""):
        data, usage, model_id = call_openai_vision_json(
            system=system,
            content_blocks=content_blocks,
            max_tokens=max_tokens,
        )
        return data, usage, model_id

    import anthropic

    if not config.ANTHROPIC_API_KEY:
        return None, {"error": "no_anthropic_key"}, None

    if models is None:
        models = VISION_DEFAULT_MODELS
    elif len(models) == 1:
        # Single-model calls (e.g. Opus-only) should not auto-escalate on confidence.
        min_confidence = None
        valid_decisions = None

    client = anthropic.Anthropic(timeout=LLM_REQUEST_TIMEOUT_SEC)
    last_raw = ""
    attempts: list[dict[str, Any]] = []
    for idx, model_id in enumerate(models):
        is_last = idx >= len(models) - 1
        try:
            resp = client.messages.create(
                model=model_id,
                max_tokens=max_tokens,
                temperature=0,
                system=system,
                messages=[{"role": "user", "content": content_blocks}],
                timeout=LLM_REQUEST_TIMEOUT_SEC,
            )
            parts = [getattr(p, "text", "") for p in (resp.content or [])]
            raw = "\n".join(p for p in parts if p).strip()
            last_raw = raw
            data = parse_json_from_llm(raw)
            usage = {
                "input_tokens": int(getattr(resp.usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(resp.usage, "output_tokens", 0) or 0),
                "model": model_id,
            }
            attempts.append(usage)
            if _vision_result_needs_escalation(
                data, min_confidence=min_confidence, valid_decisions=valid_decisions
            ) and not is_last:
                continue
            if len(attempts) > 1:
                usage = {**usage, "vision_attempts": attempts}
            return data, usage, model_id
        except Exception:
            continue
    err_usage: dict[str, Any] = {"raw": last_raw[:1200]}
    if attempts:
        err_usage["vision_attempts"] = attempts
    return None, err_usage, None


def paid_api_ready() -> tuple[bool, str]:
    ladder = build_eval_escalation_ladder(profile=config.CORPUS_PROFILE, include_local_fallback=False)
    if ladder:
        return True, ladder[0]
    return False, "Run SETUP.bat and choose an AI provider. Recommended for public users: OpenRouter."
