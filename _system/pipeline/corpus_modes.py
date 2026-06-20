"""Sonnet-only mode definitions (A/B/C deprecated for evaluation)."""

from __future__ import annotations

from typing import Any

from cost_estimate import estimate_option
from eval_constants import MODEL_SONNET

SUPPORTED_EVAL_MODES = frozenset({"A", "a"})
DEPRECATED_EVAL_MODES = frozenset({"B", "C", "b", "c"})


def normalize_eval_mode(mode: str | None) -> str:
    """Return canonical mode letter. B/C are rejected; default A (Sonnet)."""
    m = (mode or "A").strip().upper()
    if m in DEPRECATED_EVAL_MODES:
        raise ValueError(
            f"Mode {m} is deprecated. Final evaluation is Claude Sonnet only (use --mode A or omit)."
        )
    if m not in SUPPORTED_EVAL_MODES:
        raise ValueError(f"Unsupported mode {mode!r}. Use --mode A (Claude Sonnet only).")
    return "A"


def build_mode_options(prescan: dict[str, Any], standard: dict[str, Any]) -> dict[str, dict[str, Any]]:
    n = prescan["pdf_count"]
    est_a = estimate_option(
        n,
        name="A",
        first_pass_model=MODEL_SONNET,
        first_pass_calls=1,
        redteam_fraction=0.55,
        batch=True,
        cache_savings_input=0.35,
        output_reduction=0.20,
    )
    return {
        "A": {
            "label": "Claude Sonnet (only supported evaluator)",
            "same_depth_first_pass": True,
            "cost_usd_est": est_a["cost_usd"],
            "runtime_hours_est": est_a["wall_clock_hours_est"],
            "quality_note": "Sonnet-only evaluation matches current DB standard.",
            "quality_warning": "",
            "est": est_a,
        },
    }


def recommend_mode(options: dict[str, dict[str, Any]], standard: dict[str, Any]) -> dict[str, str]:
    return {
        "mode": "A",
        "reason": "Claude Sonnet is the only supported final evaluator (current DB standard).",
    }
