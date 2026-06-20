"""
Conditional red-team policy (Option B).
Same-depth first pass always; red-team only when rules say so.
"""
SKIP_REDTEAM_RATINGS = {"weak", "flawed", "not_applicable", "not_ratable_reference_material"}
REDTEAM_RATINGS = {"landmark", "strong", "adequate"}


def should_red_team(
    first_pass_result: dict,
    *,
    escalation_reasons: list[str] | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """
    Returns (run_red_team, reason_code).
    reason_code is logged when skipped or run.
    """
    if force:
        return True, "forced"

    cl = (first_pass_result or {}).get("classification") or {}
    rating = cl.get("rating", "")
    confidence = str(cl.get("confidence", "medium")).lower()

    if rating.startswith("not_"):
        return False, "non_ratable_document"

    if rating in SKIP_REDTEAM_RATINGS:
        return False, f"clear_low_rating:{rating}"

    if rating in REDTEAM_RATINGS:
        return True, f"rating_tier:{rating}"

    if confidence == "low":
        return True, "low_confidence"

    if escalation_reasons:
        return True, "escalation_pending:" + ",".join(escalation_reasons[:3])

    return False, "default_skip"
