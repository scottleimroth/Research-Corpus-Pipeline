"""
Prompt construction with optional Anthropic prompt caching blocks.
Read-only / plan-building; no API calls unless ALLOW_LIVE_API set elsewhere.
"""
import hashlib

from evaluate_paper import (  # noqa: E402
    EVAL_SYSTEM_PROMPT,
    RED_TEAM_SYSTEM_PROMPT,
    build_eval_system_prompt,
    load_standards_rubric,
)
from eval_constants import USE_PROMPT_CACHE
from eval_schema_v2 import V2_SYSTEM_PROMPT_APPENDIX


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def build_eval_system_prompt_v2() -> str:
    return build_eval_system_prompt() + "\n" + V2_SYSTEM_PROMPT_APPENDIX


def build_redteam_system_prompt() -> str:
    return (
        RED_TEAM_SYSTEM_PROMPT
        + "\n\n## METHODOLOGICAL STANDARDS RUBRIC (for comparison)\n\n"
        + load_standards_rubric()
    )


def _cacheable_block(text: str) -> dict:
    block = {"type": "text", "text": text}
    if USE_PROMPT_CACHE:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def build_system_message_eval(*, schema_v2: bool = False) -> list[dict]:
    """
    System content as Anthropic message blocks.
    Cacheable prefix = static instructions + full rubric (repeated every call).
    """
    base = build_eval_system_prompt_v2() if schema_v2 else build_eval_system_prompt()
    rubric_marker = "## METHODOLOGICAL STANDARDS RUBRIC"
    if rubric_marker in base:
        head, tail = base.split(rubric_marker, 1)
        return [
            _cacheable_block(head + rubric_marker),
            _cacheable_block(tail),
        ]
    return [_cacheable_block(base)]


def build_system_message_redteam() -> list[dict]:
    text = build_redteam_system_prompt()
    rubric_marker = "## METHODOLOGICAL STANDARDS RUBRIC"
    if rubric_marker in text:
        head, tail = text.split(rubric_marker, 1)
        return [
            _cacheable_block(head + rubric_marker),
            _cacheable_block(tail),
        ]
    return [_cacheable_block(text)]


def caching_report() -> dict:
    """Read-only report on what can be cached."""
    eval_prompt = build_eval_system_prompt()
    rt_prompt = build_redteam_system_prompt()
    return {
        "eval_system_chars": len(eval_prompt),
        "redteam_system_chars": len(rt_prompt),
        "eval_system_hash": prompt_hash(eval_prompt),
        "redteam_system_hash": prompt_hash(rt_prompt),
        "cache_enabled_flag": USE_PROMPT_CACHE,
        "cacheable_per_call": [
            "evaluation system + distilled rubric (~83k+ chars in current build)",
            "red-team system + distilled rubric (similar size)",
        ],
        "estimated_prefix_tokens_per_call": int((len(eval_prompt) + len(rt_prompt)) / 2 / 3.5),
        "notes": (
            "Both passes repeat large rubric text; prompt caching should reduce "
            "input cost on cache hits for pass 2 and across papers in a batch window."
        ),
    }
