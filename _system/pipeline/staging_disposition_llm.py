"""LLM final review for staging disposition — always runs after deterministic proposal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_LLM_DISPOSITION_DECISIONS = frozenset(
    {
        "INGEST_RESEARCH_PAPER",
        "INGEST_NON_RATABLE_REFERENCE",
        "DELETE_CONFIRMED_JUNK",
        "DELETE_TRUE_CORRUPT_UNUSABLE",
        "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
        "HOLD_PENDING_PARENT",
    }
)

_STAGING_SOURCES = frozenset({"staging", "staging-ingest-failure"})

_CHILD_LLM_FALLBACK_CLASSIFICATIONS = frozenset(
    {
        "VALID_NONPDF_CHILD_DOCUMENT",
        "VALID_NONPDF_DATASET_OR_MEDIA_SUPPLEMENT",
    }
)


def staging_disposition_needs_llm(
    *,
    source_context: str,
    path: Path,
    classification: str,
    action: str,
    allow_paid_api: bool,
) -> bool:
    """True when a staging file needs LLM as the final check (not filename/extension rules)."""
    if not allow_paid_api:
        return False
    if str(source_context or "") not in _STAGING_SOURCES:
        return False
    if classification == "CONFIRMED_DUPLICATE" or action == "DELETE_CONFIRMED_DUPLICATE":
        return False
    # Clear PDF research papers go to ingest; acceptance/ingest path has its own LLM if blocked.
    if (
        path.suffix.lower() == ".pdf"
        and classification == "VALID_PDF_STANDALONE_RESEARCH"
        and action == "INGEST_RESEARCH_PAPER"
    ):
        return False
    # Everything else dropped in staging — especially non-PDF — gets LLM review of actual content.
    return True


def llm_staging_disposition_adjudication(
    staging_path: Path,
    *,
    disposition_context: dict[str, Any],
    allow_opus: bool,
) -> dict[str, Any]:
    """Vision/text LLM review of what a staging file actually is."""
    from corpus_live_all_staging import _llm_final_adjudication_for_block

    staging_path = Path(staging_path)
    block = {
        "task": "staging_disposition_review",
        "proposed_classification": disposition_context.get("classification"),
        "proposed_action": disposition_context.get("action"),
        "proposed_reason": disposition_context.get("reason"),
        "allowed_decisions": sorted(_LLM_DISPOSITION_DECISIONS),
        "rules": {
            "look_at_actual_content_not_filename_alone": True,
            "payslips_receipts_payroll_screenshots_personal_tax_spreadsheets": "DELETE_CONFIRMED_JUNK",
            "random_unrelated_images_or_office_files": "DELETE_CONFIRMED_JUNK",
            "journal_supplement_mmc_dataset_for_a_paper": "HOLD_PENDING_PARENT",
            "real_research_pdf_content_only": "INGEST_RESEARCH_PAPER",
            "manuals_bibliographies_worth_keeping_non_ratable": "INGEST_NON_RATABLE_REFERENCE",
            "corrupt_unreadable_only": "DELETE_TRUE_CORRUPT_UNUSABLE",
        },
    }

    base = _llm_final_adjudication_for_block(
        staging_path,
        block,
        client=None,
        allow_opus=allow_opus,
    )
    if not base.get("ok"):
        return base

    decision = str(base.get("decision") or "").strip().upper()
    if decision not in _LLM_DISPOSITION_DECISIONS:
        return {
            "ok": False,
            "error": f"invalid_disposition_decision:{decision or 'missing'}",
            "sonnet_usage": base.get("sonnet_usage"),
            "opus_usage": base.get("opus_usage"),
        }
    base["decision"] = decision
    return base


def apply_staging_disposition_llm(
    path: Path,
    result: dict[str, Any],
    *,
    source_context: str,
    allow_paid_api: bool,
    allow_opus: bool,
) -> dict[str, Any]:
    """Run LLM disposition review and merge into a disposition result dict."""
    path = Path(path)
    classification = str(result.get("classification") or "")
    action = str(result.get("action") or "")
    if not staging_disposition_needs_llm(
        source_context=source_context,
        path=path,
        classification=classification,
        action=action,
        allow_paid_api=allow_paid_api,
    ):
        return result

    llm = llm_staging_disposition_adjudication(
        path,
        disposition_context={
            "classification": classification,
            "action": action,
            "reason": result.get("reason"),
        },
        allow_opus=allow_opus and allow_paid_api,
    )
    result = dict(result)
    result["llm_disposition_adjudication"] = llm
    if not llm.get("ok"):
        err = str(llm.get("error") or "unknown")
        from child_document_matching import is_child_document

        # Supplements / child docs: hold for parent rather than fail the whole run on LLM hiccups.
        if (
            action == "HOLD_PENDING_PARENT"
            or classification in _CHILD_LLM_FALLBACK_CLASSIFICATIONS
            or is_child_document(path.name)
        ):
            result["action"] = "HOLD_PENDING_PARENT"
            result["reason"] = f"llm_disposition_fallback_hold:{err}"
            result["needs_ingest"] = False
            result["child_type"] = result.get("child_type") or "supplement"
            return result
        result["action"] = "SYSTEM_INTEGRITY_FAILURE"
        result["reason"] = f"llm_disposition_failed:{err}"
        return result

    decision = str(llm.get("decision") or "")
    result["action"] = decision
    result["reason"] = f"llm_disposition:{decision.lower()}"
    if decision in {"INGEST_RESEARCH_PAPER", "INGEST_NON_RATABLE_REFERENCE"}:
        result["needs_ingest"] = True
        result["ingest_kind"] = decision
    elif decision == "HOLD_PENDING_PARENT":
        result["needs_ingest"] = False
        result["child_type"] = result.get("child_type") or "supplement"
    else:
        result["needs_ingest"] = False
    return result
