"""
Model routing: DeepSeek-first escalation ladder with rare Anthropic fallback.

Ladder (default): deepseek-v4-pro → deepseek-reasoner → sonnet-4.5 → opus-4
Skipped automatically when the required API key is not set.
"""
from __future__ import annotations

from eval_model_config import build_eval_escalation_ladder

HIGH_VALUE_KEYWORDS = (
    "systematic review",
    "meta-analysis",
    "meta analysis",
    "randomized",
    "rct",
    "preregister",
    "landmark",
)

BORDERLINE_RATINGS = {"strong", "adequate"}
LOW_CONFIDENCE = {"low", "medium"}


def choose_first_pass_model(*, profile: dict | None = None) -> str:
    """First model in the configured escalation ladder."""
    ladder = build_eval_escalation_ladder(profile=profile)
    if not ladder:
        return "deepseek-v4-pro"
    return ladder[0]


def escalation_reasons_for_failure(
    *,
    validation_error: str | None = None,
    json_error: bool = False,
    api_error: bool = False,
    wrong_research_rating: bool = False,
) -> list[str]:
    reasons: list[str] = []
    if validation_error:
        reasons.append(f"validation_failed:{validation_error[:80]}")
    if json_error:
        reasons.append("json_parse_failed")
    if api_error:
        reasons.append("api_error")
    if wrong_research_rating:
        reasons.append("wrong_research_rating")
    return reasons


def needs_quality_escalation(
    first_pass_result: dict,
    *,
    input_plan: dict | None = None,
    validation_error: str | None = None,
) -> tuple[bool, list[str]]:
    """
    After a successful parse, decide if we should try the next tier (e.g. Reasoner).
    """
    reasons: list[str] = []
    if validation_error:
        return True, ["validation_failed"]

    cl = (first_pass_result or {}).get("classification") or {}
    rating = str(cl.get("rating") or "").lower()
    confidence = str(cl.get("confidence", "medium")).lower()

    if confidence in LOW_CONFIDENCE:
        reasons.append(f"confidence={confidence}")
    if rating in BORDERLINE_RATINGS:
        reasons.append(f"borderline_rating={rating}")

    justification = str(cl.get("rating_justification") or "")
    if len(justification) < 200:
        reasons.append("short_justification")

    if input_plan and input_plan.get("omitted"):
        critical = any(
            any(x in str(o).lower() for x in ("methods", "results", "discussion"))
            for o in input_plan["omitted"]
        )
        if critical:
            reasons.append("critical_sections_omitted")

    for kw in HIGH_VALUE_KEYWORDS:
        if kw in justification.lower():
            reasons.append(f"high_value_keyword:{kw}")
            break

    return bool(reasons), reasons


# Backward-compatible aliases
def choose_first_pass_model_legacy(*, pilot_haiku_validated: bool = False) -> str:
    return choose_first_pass_model()


def needs_sonnet_escalation(
    first_pass_result: dict,
    *,
    input_plan: dict | None = None,
    validation_error: str | None = None,
) -> tuple[bool, list[str]]:
    return needs_quality_escalation(
        first_pass_result,
        input_plan=input_plan,
        validation_error=validation_error,
    )
