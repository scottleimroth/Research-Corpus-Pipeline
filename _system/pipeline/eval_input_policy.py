"""
Same-depth input policy documentation and helpers (read-only).

Every PDF receives first-pass assessment from full document content:
- full_text_within_context when canonical text fits budget, or
- evidence_pack built deterministically from full document (no blind truncation).

Re-exports build_evaluation_input from evaluate_paper.py.
"""
from evaluate_paper import (  # noqa: E402
    MAX_EVAL_INPUT_CHARS,
    build_evaluation_input,
    log_evaluation_input,
)

EVIDENCE_PACK_PRIORITY = [
    "title",
    "abstract",
    "introduction",
    "background",
    "methods",
    "participants",
    "sample",
    "measures",
    "instruments",
    "results",
    "discussion",
    "limitations",
    "conclusion",
    "table",
    "figure",
    "caption",
]

TRIM_LAST_SECTIONS = ["references", "bibliography", "acknowledgements", "supplement", "appendix"]


def policy_summary() -> dict:
    return {
        "same_depth_first_pass": True,
        "no_shallow_screening_gate": True,
        "no_blind_front_truncation": True,
        "max_eval_input_chars": MAX_EVAL_INPUT_CHARS,
        "strategies": {
            "full_text_within_context": "Send complete canonical full section when within budget",
            "evidence_pack": "Priority-ordered sections; references/supplements trimmed last",
        },
        "priority_sections": EVIDENCE_PACK_PRIORITY,
        "trim_last_only": TRIM_LAST_SECTIONS,
        "logging": [
            "db_full_text_chars",
            "input_chars",
            "strategy",
            "source",
            "blind_trunc",
            "omitted",
        ],
    }
