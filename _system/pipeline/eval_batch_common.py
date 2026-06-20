"""
Shared batch request planning utilities (dry-run safe).
"""
import json
from pathlib import Path

from eval_constants import EVAL_PLANS_DIR, PASS_ESCALATION, PASS_FIRST, PASS_REDTEAM
from eval_prompts import build_system_message_eval, build_system_message_redteam, prompt_hash


def make_custom_id(paper_id: str, pass_type: str, model: str, phash: str) -> str:
    """Deterministic custom_id for Anthropic Message Batches."""
    safe_id = paper_id.replace("|", "_")
    return f"{safe_id}|{pass_type}|{model}|{phash}"


def parse_custom_id(custom_id: str) -> dict:
    parts = custom_id.split("|")
    if len(parts) != 4:
        return {"raw": custom_id}
    return {
        "paper_id": parts[0],
        "pass_type": parts[1],
        "model": parts[2],
        "prompt_hash": parts[3],
    }


def build_first_pass_request(
    paper_id: str,
    *,
    model: str,
    user_content: str,
    max_tokens: int = 4096,
    schema_v2: bool = True,
) -> dict:
    system_blocks = build_system_message_eval(schema_v2=schema_v2)
    phash = prompt_hash(json.dumps(system_blocks, sort_keys=True))
    return {
        "custom_id": make_custom_id(paper_id, PASS_FIRST, model, phash),
        "params": {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": [
                {
                    "role": "user",
                    "content": user_content,
                }
            ],
        },
        "meta": {
            "paper_id": paper_id,
            "pass_type": PASS_FIRST,
            "model": model,
            "prompt_hash": phash,
        },
    }


def build_redteam_request(
    paper_id: str,
    *,
    model: str,
    user_content: str,
    max_tokens: int = 2048,
) -> dict:
    system_blocks = build_system_message_redteam()
    phash = prompt_hash(json.dumps(system_blocks, sort_keys=True))
    return {
        "custom_id": make_custom_id(paper_id, PASS_REDTEAM, model, phash),
        "params": {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": [{"role": "user", "content": user_content}],
        },
        "meta": {
            "paper_id": paper_id,
            "pass_type": PASS_REDTEAM,
            "model": model,
            "prompt_hash": phash,
        },
    }


def write_plan_file(plan_name: str, requests: list[dict]) -> Path:
    EVAL_PLANS_DIR.mkdir(parents=True, exist_ok=True)
    path = EVAL_PLANS_DIR / f"{plan_name}.json"
    path.write_text(
        json.dumps(
            {
                "request_count": len(requests),
                "requests": requests,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path
