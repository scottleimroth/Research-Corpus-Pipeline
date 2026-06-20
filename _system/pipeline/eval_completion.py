"""Evaluation completion checks for ingest and completion reports."""

from __future__ import annotations

from evaluate_paper import normalize_rating_label

RESEARCH_RATINGS = frozenset({"landmark", "strong", "adequate", "weak", "flawed"})
NON_RATABLE_RATINGS = frozenset({"not_applicable", "not_ratable_reference_material"})
SONNET_UNAVAILABLE_MSG = "Evaluation cannot run: required API key for the configured eval model is missing."


def is_non_ratable_rating(rating: str | None) -> bool:
    r = normalize_rating_label(rating) if rating else ""
    return r in NON_RATABLE_RATINGS


def is_research_rating(rating: str | None) -> bool:
    r = normalize_rating_label(rating) if rating else ""
    return r in RESEARCH_RATINGS


def is_ingest_evaluation_complete(paper: dict) -> tuple[bool, str]:
    """
    Return (complete, reason_code).
    Research papers need rating + justification + red_team_notes.
    Non-ratable material needs not_ratable_reference_material/not_applicable + justification.

    Completion is gated on the central AcceptanceDecision, not the model label
    alone: a likely journal article whose evidence cannot support a rating can
    never be "complete". Coverage and acceptance are each computed once here and
    reused (this runs once per row in completion audits).
    """
    from document_corpus_classification import is_non_ratable_document_type
    from document_evidence import decide_acceptance_from_paper
    from evidence_coverage import assess_evidence_coverage_from_paper

    cl = paper.get("classification") or {}
    if not isinstance(cl, dict):
        return False, "missing_classification"

    rating = normalize_rating_label(cl.get("rating"))
    justification = str(cl.get("rating_justification") or "").strip()
    red_team = str(cl.get("red_team_notes") or "").strip()

    coverage = assess_evidence_coverage_from_paper(paper)
    # No stored body (e.g. legacy/reference rows, synthetic fixtures): the
    # acceptance gate has nothing to assess, so fall back to label-based checks.
    has_evidence = bool(coverage.get("raw_text_length")) or bool(coverage.get("section_count"))
    acceptance = decide_acceptance_from_paper(paper, coverage=coverage) if has_evidence else None
    likely_journal = bool(coverage.get("likely_journal_article"))
    can_na = bool(coverage.get("evidence_can_support_not_applicable"))

    if is_non_ratable_document_type(paper.get("document_type")) and is_non_ratable_rating(rating) and justification:
        if acceptance and likely_journal and not acceptance.can_save_not_applicable:
            return False, f"acceptance_blocked_non_ratable:{acceptance.reason}"
        return True, "non_ratable_complete"

    if not rating:
        if acceptance and not acceptance.can_insert_db:
            return False, f"acceptance_blocked:{acceptance.action}:{acceptance.reason}"
        return False, "missing_rating"
    if not justification:
        return False, "missing_rating_justification"

    if is_non_ratable_rating(rating):
        if likely_journal and not can_na:
            return False, f"insufficient_evidence_for_non_ratable:{coverage.get('coverage_status')}"
        if red_team.startswith("Skipped") and likely_journal:
            return False, "red_team_skipped_for_likely_journal_article"
        return True, "non_ratable_complete"

    if is_research_rating(rating):
        if acceptance and not acceptance.can_evaluate:
            return False, f"acceptance_blocked_eval:{acceptance.reason}"
        if not red_team:
            return False, "missing_red_team_notes"
        return True, "research_eval_complete"

    return False, f"invalid_or_unknown_rating:{rating}"


def assert_sonnet_available() -> None:
    """Backward-compatible name: checks keys for the configured evaluation model."""
    import config
    from eval_model_config import assert_eval_api_key

    try:
        assert_eval_api_key(config.EVAL_MODEL_KEY)
    except RuntimeError as exc:
        raise RuntimeError(SONNET_UNAVAILABLE_MSG) from exc
