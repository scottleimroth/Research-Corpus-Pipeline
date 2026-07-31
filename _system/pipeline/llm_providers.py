"""Multi-provider LLM client wrapper.

Supports Anthropic Claude, OpenAI GPT, DeepSeek with a unified interface.
Used by evaluate_paper_v2.py and abtest_models.py.

Env vars:
  ANTHROPIC_API_KEY
  OPENAI_API_KEY
  DEEPSEEK_API_KEY
  OPENROUTER_API_KEY
  LOCAL_OPENAI_BASE_URL (no default; local mode is configured via SETUP)
"""
import os
import time
import urllib.error
import urllib.request
import json
from pathlib import Path

# Network timeout for paid LLM API calls (seconds). Prevents GUI appearing frozen.
LLM_REQUEST_TIMEOUT_SEC = 120.0
# Local GGUF/Ollama-style runs can be much slower than cloud APIs on full-paper
# prompts, so keep a separate timeout instead of weakening cloud failure handling.
LOCAL_LLM_REQUEST_TIMEOUT_SEC = float(os.environ.get("LOCAL_LLM_REQUEST_TIMEOUT_SEC", "900"))
def _local_profile_value(name: str, default: str) -> str:
    try:
        profile_path = Path(__file__).resolve().parent.parent / "corpus_profile.json"
        data = json.loads(profile_path.read_text(encoding="utf-8-sig"))
        value = str(data.get(name) or "").strip()
        return value or default
    except Exception:
        return default


# No baked-in address default. The previous fallback (Ollama's localhost:11434)
# assumed a server that need not exist, so a half-configured local mode probed
# a dead port instead of failing informatively -- an address default that can
# rot is the same bug class as the outage it hides. Local mode is configured
# explicitly: SETUP's local/free mode writes local_openai_base_url into
# _system/corpus_profile.json, or set the LOCAL_OPENAI_BASE_URL env var.
# Unset = lane not configured: local_openai_available() reports False and
# selecting the lane raises LLMConfigError.
LOCAL_OPENAI_BASE_URL = os.environ.get(
    "LOCAL_OPENAI_BASE_URL",
    _local_profile_value("local_openai_base_url", ""),
).rstrip("/")
LOCAL_OPENAI_MODEL_ID = os.environ.get(
    "LOCAL_OPENAI_MODEL_ID",
    _local_profile_value("local_openai_model_id", "qwen3:8b"),
)
LOCAL_OPENAI_API_KEY = os.environ.get("LOCAL_OPENAI_API_KEY", "lemonade")
LOCAL_OPENAI_NO_THINK = os.environ.get("LOCAL_OPENAI_NO_THINK", "1").strip().lower() not in {"0", "false", "no", "off"}
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
LOCAL_MODEL_KEY = "geekom-qwen3-30b-local"
_LOCAL_AVAILABILITY_CACHE: dict[str, tuple[float, bool]] = {}


class LLMConfigError(RuntimeError):
    """Misconfiguration retries cannot fix -- call_llm re-raises it without retrying."""

# Model registry: short_name -> (provider, model_id, cost_per_M_in, cost_per_M_out)
# cost in USD per million tokens. Locals are $0.
MODELS = {
    "sonnet-4.5":       ("anthropic", "claude-sonnet-4-5-20250929", 3.00, 15.00),
    "haiku-4.5":        ("anthropic", "claude-haiku-4-5-20251001", 1.00, 5.00),
    "gpt-5-nano":       ("openai", "gpt-5-nano", 0.05, 0.40),
    "gpt-5-mini":       ("openai", "gpt-5-mini", 0.25, 2.00),
    "gpt-5":            ("openai", "gpt-5", 1.25, 10.00),
    "gpt-4o-mini":      ("openai", "gpt-4o-mini", 0.15, 0.60),
    "deepseek-v3":      ("deepseek", "deepseek-chat", 0.27, 1.10),
    "deepseek-reasoner":("deepseek", "deepseek-reasoner", 0.55, 2.19),
    "deepseek-v4-pro":  ("deepseek", "deepseek-v4-pro", 0.55, 2.19),
    "openrouter-deepseek-v4-pro": ("openrouter", "deepseek/deepseek-v4-pro", 0.55, 2.19),
    "openrouter-qwen3-vl-32b": ("openrouter", "qwen/qwen3-vl-32b-instruct", 0.104, 0.416),
    "openrouter-kimi-k2.5": ("openrouter", "moonshotai/kimi-k2.5", 0.40, 1.90),
    "opus-4":           ("anthropic", "claude-opus-4-20250514", 15.00, 75.00),
    # Local OpenAI-compatible endpoint (free, zero cost); the URL comes from
    # SETUP / corpus_profile.json or LOCAL_OPENAI_BASE_URL -- see comment above.
    LOCAL_MODEL_KEY:     ("local-openai", LOCAL_OPENAI_MODEL_ID, 0.0, 0.0),
}


def local_openai_available(*, ttl_seconds: int = 30) -> bool:
    """True when the local OpenAI-compatible server lists the configured model.

    An unset LOCAL_OPENAI_BASE_URL means local mode is not configured: report
    unavailable so escalation ladders skip the lane (call_llm raises the
    actionable error if the lane is selected outright).
    """
    if not LOCAL_OPENAI_BASE_URL:
        return False
    now = time.time()
    cached = _LOCAL_AVAILABILITY_CACHE.get(LOCAL_OPENAI_MODEL_ID)
    if cached and (now - cached[0]) < ttl_seconds:
        return cached[1]
    url = f"{LOCAL_OPENAI_BASE_URL}/models"
    # Send the key: a gateway that requires one answers /models with 401, and an
    # unauthenticated probe would read that as "server down" and quietly skip the
    # local lane. Bare Ollama/llama.cpp ignore the header, so always sending it
    # is safe.
    request = urllib.request.Request(url)
    if LOCAL_OPENAI_API_KEY:
        request.add_header("Authorization", f"Bearer {LOCAL_OPENAI_API_KEY}")
    try:
        with urllib.request.urlopen(request, timeout=3) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        models = data.get("data") if isinstance(data, dict) else []
        ok = any(
            isinstance(item, dict)
            and LOCAL_OPENAI_MODEL_ID in {str(item.get("id") or ""), str(item.get("checkpoint") or "")}
            for item in (models or [])
        )
        if not ok and models:
            # Reachable, but the configured model id is not served -- almost
            # always a typo in SETUP. Name it instead of reporting "unavailable".
            print(
                f"    local model {LOCAL_OPENAI_MODEL_ID!r} not served by "
                f"{LOCAL_OPENAI_BASE_URL} (offers: "
                f"{', '.join(sorted(str(m.get('id')) for m in models if isinstance(m, dict))[:8])})",
                flush=True,
            )
    except urllib.error.HTTPError as exc:
        # 401/403 is a wrong-or-missing key, not an outage.
        if exc.code in (401, 403):
            print(
                f"    local model probe rejected by {LOCAL_OPENAI_BASE_URL} "
                f"(HTTP {exc.code}): check LOCAL_OPENAI_API_KEY",
                flush=True,
            )
        ok = False
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        ok = False
    _LOCAL_AVAILABILITY_CACHE[LOCAL_OPENAI_MODEL_ID] = (now, ok)
    return ok


def call_llm(model_key, system, user_text, max_tokens=8192, max_retries=3):
    """Unified call. Returns (text, usage_dict). usage_dict has input_tokens, output_tokens, cost_usd."""
    if model_key not in MODELS:
        raise ValueError(f"unknown model: {model_key}. Known: {list(MODELS)}")
    provider, model_id, cin, cout = MODELS[model_key]

    for attempt in range(max_retries):
        try:
            if provider == "anthropic":
                import anthropic
                client = anthropic.Anthropic(timeout=LLM_REQUEST_TIMEOUT_SEC)
                resp = client.messages.create(
                    model=model_id,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user_text}],
                    timeout=LLM_REQUEST_TIMEOUT_SEC,
                )
                text = resp.content[0].text
                in_tok = resp.usage.input_tokens
                out_tok = resp.usage.output_tokens
            elif provider in ("openai", "deepseek", "openrouter", "local-openai"):
                from openai import OpenAI
                if provider == "openai":
                    client = OpenAI(timeout=LLM_REQUEST_TIMEOUT_SEC)
                elif provider == "deepseek":
                    client = OpenAI(
                        api_key=os.environ["DEEPSEEK_API_KEY"],
                        base_url="https://api.deepseek.com",
                        timeout=LLM_REQUEST_TIMEOUT_SEC,
                    )
                elif provider == "openrouter":
                    client = OpenAI(
                        api_key=os.environ["OPENROUTER_API_KEY"],
                        base_url=OPENROUTER_BASE_URL,
                        timeout=LLM_REQUEST_TIMEOUT_SEC,
                        default_headers={
                            "HTTP-Referer": "https://github.com/scottleimroth/Research-Corpus-Pipeline",
                            "X-Title": "Research-Corpus-Pipeline",
                        },
                    )
                elif provider == "local-openai":
                    if not LOCAL_OPENAI_BASE_URL:
                        raise LLMConfigError(
                            "Local mode is selected but no local model URL is "
                            "configured (there is no built-in default on "
                            "purpose). Run SETUP and choose Local/free mode, or "
                            "set local_openai_base_url in "
                            "_system/corpus_profile.json or the "
                            "LOCAL_OPENAI_BASE_URL environment variable."
                        )
                    if not local_openai_available():
                        raise RuntimeError(
                            f"local model unavailable at {LOCAL_OPENAI_BASE_URL} "
                            f"(model={LOCAL_OPENAI_MODEL_ID})"
                        )
                    print(
                        f"    LLM local fallback provider invoked: {model_key} "
                        f"({LOCAL_OPENAI_MODEL_ID} @ {LOCAL_OPENAI_BASE_URL})",
                        flush=True,
                    )
                    client = OpenAI(
                        api_key=LOCAL_OPENAI_API_KEY,
                        base_url=LOCAL_OPENAI_BASE_URL,
                        timeout=LOCAL_LLM_REQUEST_TIMEOUT_SEC,
                    )
                system_content = system
                user_content = user_text
                if provider == "local-openai" and LOCAL_OPENAI_NO_THINK:
                    no_think = "/no_think\n"
                    if not system_content.lstrip().startswith("/no_think"):
                        system_content = no_think + system_content
                request_timeout = (
                    LOCAL_LLM_REQUEST_TIMEOUT_SEC
                    if provider == "local-openai"
                    else LLM_REQUEST_TIMEOUT_SEC
                )
                resp = client.chat.completions.create(
                    model=model_id,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": user_content},
                    ],
                    timeout=request_timeout,
                )
                message = resp.choices[0].message
                text = message.content or ""
                # A 200 with empty content is the standard thinking-model failure:
                # the whole output budget went to reasoning and no answer was ever
                # emitted. Servers spell the field TWO ways -- vLLM with a qwen3-style
                # reasoning parser emits `reasoning`, llama.cpp/Ollama/LiteLLM emit
                # `reasoning_content` -- so both are checked. Checking only one lets
                # the guard silently not fire, and an empty string then travels on as
                # if it were the model's answer.
                if provider == "local-openai" and not text:
                    thinking = next(
                        (t for t in (getattr(message, "reasoning_content", None),
                                     getattr(message, "reasoning", None))
                         if isinstance(t, str) and t.strip()),
                        None,
                    )
                    if thinking:
                        finish = getattr(resp.choices[0], "finish_reason", None)
                        raise RuntimeError(
                            f"local model returned reasoning ({len(thinking)} chars) but no "
                            f"final content (finish_reason={finish}) - raise max_tokens"
                        )
                in_tok = getattr(resp.usage, "prompt_tokens", 0) if resp.usage else 0
                out_tok = getattr(resp.usage, "completion_tokens", 0) if resp.usage else 0
            else:
                raise ValueError(f"unknown provider: {provider}")

            cost = (in_tok * cin + out_tok * cout) / 1_000_000
            return text, {
                "provider": provider,
                "model": model_id,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost_usd": cost,
            }
        except LLMConfigError:
            raise
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            print(f"    {model_key} retry {attempt + 1}/{max_retries}: {e}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")
