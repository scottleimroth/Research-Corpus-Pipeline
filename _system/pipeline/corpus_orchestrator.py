"""Internal orchestration layer for the corpus ingest workflow.

Consolidates proven pilot stages (A–T) into a single user-facing workflow while
keeping granular stage modules available for debugging. Plan-only mode is the
default; live execution requires explicit gates and is not enabled here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from corpus_prescan import classify_document_type
from corpus_pilot_selection import assess_live_pilot_suitability
from corpus_run_planner import (
    SELECTION_PROFILES,
    build_run_plan,
    render_run_report,
    resolve_selection_profile,
    write_run_plan,
    _pdf_eligible_for_profile,
)
from corpus_stage_d_preview import run_text_metadata_preview
from corpus_stage_e_preview import run_metadata_duplicate_resolution_preview

# ---------------------------------------------------------------------------
# Internal phases (user does not call these individually)
# ---------------------------------------------------------------------------

INTERNAL_PHASES: list[dict[str, Any]] = [
    {"id": "prescan", "label": "Pre-scan staging", "pilot_stage": None, "execute_stage": None, "side_effects": False},
    {"id": "mode_select", "label": "Mode selection (Sonnet-only A)", "pilot_stage": None, "execute_stage": None, "side_effects": False},
    {"id": "run_plan", "label": "Build run plan", "pilot_stage": None, "execute_stage": None, "side_effects": False},
    {"id": "preflight", "label": "Preflight checks", "pilot_stage": "A", "execute_stage": "preflight", "side_effects": False},
    {"id": "backup", "label": "DB backup before writes", "pilot_stage": "B", "execute_stage": "preflight-backup", "side_effects": True},
    {"id": "text_metadata_preview", "label": "Text/metadata extraction preview", "pilot_stage": "D", "execute_stage": "text-metadata-preview", "side_effects": False},
    {"id": "metadata_duplicate_resolution", "label": "Duplicate resolution preview", "pilot_stage": "E", "execute_stage": "metadata-duplicate-resolution-preview", "side_effects": False},
    {"id": "route_plan", "label": "Final route/action plan", "pilot_stage": "F", "execute_stage": "final-route-plan-preview", "side_effects": False},
    {"id": "book_toc_chapter_preview", "label": "Book/TOC/chapter escalation", "pilot_stage": "G", "execute_stage": "book-toc-chapter-preview", "side_effects": False},
    {"id": "parent_validation", "label": "Generalized parent-record validation", "pilot_stage": "J/O", "execute_stage": "general-parent-validation-preview (internal); koren/harris previews for debugging", "side_effects": False},
    {"id": "duplicate_skip_log", "label": "Duplicate-skip audit log", "pilot_stage": "I", "execute_stage": "live-duplicate-skip-log-only", "side_effects": True},
    {"id": "parent_record_create", "label": "Generalized parent DB row insert", "pilot_stage": "K/P", "execute_stage": "orchestrated live parent lifecycle; koren/harris stages for debugging", "side_effects": True},
    {"id": "source_pdf_move", "label": "Move staged PDF to source-pdfs", "pilot_stage": "L/Q", "execute_stage": "orchestrated live parent lifecycle; koren/harris stages for debugging", "side_effects": True},
    {"id": "fileinfo_attach", "label": "file_info attachment", "pilot_stage": "M/R", "execute_stage": "orchestrated live parent lifecycle; koren/harris stages for debugging", "side_effects": True},
    {"id": "staging_cleanup_check", "label": "Verify original staging PDF is gone", "pilot_stage": "N/S", "execute_stage": "orchestrated live parent lifecycle; koren/harris stages for debugging", "side_effects": False},
    {"id": "final_report", "label": "Final workflow report", "pilot_stage": "T", "execute_stage": "final-pilot-report-preview", "side_effects": False},
]

PILOT_STAGE_TO_PHASE: dict[str, str] = {
    "A": "preflight",
    "B": "backup",
    "C": "run_plan",
    "D": "text_metadata_preview",
    "E": "metadata_duplicate_resolution",
    "F": "route_plan",
    "G": "book_toc_chapter_preview",
    "H": "route_plan",
    "I": "duplicate_skip_log",
    "J": "parent_validation",
    "K": "parent_record_create",
    "L": "source_pdf_move",
    "M": "fileinfo_attach",
    "N": "staging_cleanup_check",
    "O": "parent_validation",
    "P": "parent_record_create",
    "Q": "source_pdf_move",
    "R": "fileinfo_attach",
    "S": "staging_cleanup_check",
    "T": "final_report",
}

# ---------------------------------------------------------------------------
# Workflow state model
# ---------------------------------------------------------------------------

WORKFLOW_STATUSES = (
    "planned",
    "duplicate_skip_log_only",
    "parent_record_ready",
    "parent_record_created",
    "pdf_moved_to_source",
    "fileinfo_attached",
    "staging_source_removed",
    "completed",
    "completed_duplicate_skip_log_only",
    "machine_escalation_needed",
    "human_review_last_resort",
)

# ---------------------------------------------------------------------------
# Route model (proven in pilot)
# ---------------------------------------------------------------------------

ROUTE_TYPES = (
    "exact_duplicate_skip_candidate",
    "book_parent_record_candidate",
    "edited_volume_parent_record_candidate",
    "parent_plus_child_chapter_candidates",
    "machine_escalation_required_before_write",
    "ingest_new_record_candidate",
    "related_version_link_candidate",
    "completed_parent_record_flow",
    "completed_duplicate_skip_log_only",
)

SELECTION_PROFILE_TYPES = SELECTION_PROFILES["narrow"]["allowed_types"]

STAGE_E_DUPLICATE_ROUTE = {
    "exact_duplicate_skip": "exact_duplicate_skip_candidate",
    "duplicate_better_pdf_candidate": "related_version_link_candidate",
    "related_version_candidate": "related_version_link_candidate",
}

# ---------------------------------------------------------------------------
# Current-corpus policy
# ---------------------------------------------------------------------------

CORPUS_POLICY: dict[str, Any] = {
    "keep_valid_staged_pdfs": (
        "Valid PDFs intentionally placed in staging should be kept and represented "
        "unless exact duplicate, corrupted/unreadable, accidental non-document, "
        "or unsafe after machine escalation."
    ),
    "topical_relevance": "not_an_exclusion_criterion",
    "guideline_mismatch": "metadata_routing_tag_not_rejection",
    "child_chapter_records": (
        "deferred_optional_later; normal ingest creates/attaches the parent record first "
        "and child/chapter records must not block parent ingest"
    ),
    "parent_evaluation": "deferred_for_book_and_edited_volume_parents",
    "human_review": "last_resort_only_after_machine_escalation",
    "duplicate_exact_skip": "log_only_no_db_pdf_eval_action_unless_future_archive_policy",
    "successful_pdf_storage": (
        "single_canonical_pdf_only; move the staged PDF to CorpusStore/source-pdfs/<final_id>.pdf, "
        "update file_info, and leave no successful duplicate in papers-staging/processed"
    ),
    "safety": {
        "backup_before_any_db_write": True,
        "hash_validation": True,
        "db_digest_checks": True,
        "run_lock": True,
        "fail_closed_on_ambiguity": True,
    },
}

LIVE_GATES = (
    {"gate": "--allow-db-write", "required_for": ["parent_record_create", "fileinfo_attach"]},
    {"gate": "--allow-pdf-copy", "required_for": ["source_pdf_move"], "note": "authorizes the final staged-PDF move into source-pdfs"},
    {"gate": "--allow-paid-api", "required_for": ["evaluation"], "default": "not_enabled"},
)

PARENT_ITEM_LIFECYCLE = [
    {
        "phase": "preflight",
        "action": "validate run artifacts, selected item, hashes, run lock, and no duplicate signal",
        "side_effect": "none",
        "required_gate": None,
    },
    {
        "phase": "book_toc_chapter_preview",
        "action": "read-only book/TOC/chapter escalation and parent metadata extraction",
        "side_effect": "none",
        "required_gate": None,
    },
    {
        "phase": "parent_validation",
        "action": "generalized read-only parent validation; fail closed on ambiguous metadata",
        "side_effect": "none",
        "required_gate": None,
    },
    {
        "phase": "parent_record_create",
        "action": "create exactly one parent DB row after backup and digest checks",
        "side_effect": "one DB row insert",
        "required_gate": "--allow-db-write",
    },
    {
        "phase": "source_pdf_move",
        "action": "move staged PDF to CorpusStore/source-pdfs/<parent_id>.pdf and verify hash",
        "side_effect": "one PDF move; no duplicate processed/archive copy",
        "required_gate": "--allow-pdf-copy",
    },
    {
        "phase": "fileinfo_attach",
        "action": "attach managed PDF file_info to the parent row after backup and digest checks",
        "side_effect": "one DB row update limited to file_info",
        "required_gate": "--allow-db-write",
    },
    {
        "phase": "staging_cleanup_check",
        "action": "verify original papers-staging path is gone and no processed/archive duplicate exists",
        "side_effect": "none",
        "required_gate": None,
    },
    {
        "phase": "completion_report",
        "action": "read-only completion checkpoint; blocked unless Sonnet eval or valid non-ratable classification complete",
        "side_effect": "run artifacts only",
        "required_gate": None,
    },
]

DUPLICATE_ITEM_LIFECYCLE = [
    {
        "phase": "preflight",
        "action": "validate run artifacts, selected item, hash, and exact duplicate evidence",
        "side_effect": "none",
        "required_gate": None,
    },
    {
        "phase": "duplicate_skip_log",
        "action": "accept exact duplicate skip into run-level audit artifact",
        "side_effect": "run artifacts only",
        "required_gate": None,
    },
    {
        "phase": "final_report",
        "action": "read-only final report",
        "side_effect": "run artifacts only",
        "required_gate": None,
    },
]

BOOK_LIKE_MARKERS = ("handbook", "guilford", "fourth edition", "emotions")
EDITED_VOLUME_MARKERS = ("neuroeconomics", "springer", "edited by", "studies in neuroscience")


def _best_stage_e_match_id(stage_e_item: dict[str, Any]) -> str | None:
    matches = stage_e_item.get("duplicate_version_resolution", {}).get("matched_rows") or []
    if matches:
        return matches[0].get("id")
    investigated = stage_e_item.get("existing_db_matches_investigated") or []
    if investigated:
        return investigated[0].get("id")
    return None


def _duplicate_route_from_stage_e(stage_e_item: dict[str, Any]) -> dict[str, Any]:
    dup = stage_e_item.get("duplicate_version_resolution") or {}
    decision = dup.get("decision", "")
    matched_id = _best_stage_e_match_id(stage_e_item)
    evidence = {
        "decision": decision,
        "confidence": dup.get("confidence"),
        "rationale": dup.get("rationale"),
        "matched_existing_db_id": matched_id,
        "signals": (dup.get("matched_rows") or [{}])[0].get("comparison", {}).get("signals", [])
        if dup.get("matched_rows")
        else [],
    }
    if decision == "exact_duplicate_skip":
        return {
            "route_type": "exact_duplicate_skip_candidate",
            "workflow_status": "duplicate_skip_log_only",
            "planned_db_action": "none (duplicate-skip log only)",
            "planned_pdf_action": "none (staged file retained unless future duplicate-archive policy)",
            "planned_evaluation_action": "none (already represented in corpus)",
            "evaluation_deferred": True,
            "child_records_deferred": True,
            "duplicate_log_only": True,
            "matched_existing_db_id": matched_id,
            "machine_escalation_needed": False,
            "human_review_needed": False,
            "stage_e_resolution": evidence,
            "internal_phases": [
                "text_metadata_preview",
                "metadata_duplicate_resolution",
                "route_plan",
                "duplicate_skip_log",
                "final_report",
            ],
        }
    if decision in {"duplicate_better_pdf_candidate", "related_version_candidate"}:
        return {
            "route_type": STAGE_E_DUPLICATE_ROUTE[decision],
            "workflow_status": "machine_escalation_needed",
            "planned_db_action": "none until related-version policy validated",
            "planned_pdf_action": "none until related-version/better-PDF policy validated",
            "planned_evaluation_action": "none until route confirmed",
            "evaluation_deferred": True,
            "child_records_deferred": True,
            "duplicate_log_only": False,
            "matched_existing_db_id": matched_id,
            "machine_escalation_needed": True,
            "human_review_needed": False,
            "stage_e_resolution": evidence,
            "internal_phases": [
                "text_metadata_preview",
                "metadata_duplicate_resolution",
                "route_plan",
                "final_report",
            ],
        }
    return {}


def _apply_stage_e_route(
    base_route: dict[str, Any],
    stage_e_item: dict[str, Any],
    prescan_doc_type: str = "",
) -> dict[str, Any]:
    """Re-route using read-only Stage E duplicate resolution when available."""
    dup = stage_e_item.get("duplicate_version_resolution") or {}
    decision = dup.get("decision", "")
    if decision in STAGE_E_DUPLICATE_ROUTE or decision == "exact_duplicate_skip":
        override = _duplicate_route_from_stage_e(stage_e_item)
        if override:
            return override
    if decision == "book_or_edited_volume_candidate":
        resolved = (stage_e_item.get("document_type_resolution") or {}).get("resolved_document_type", "")
        stage_d = stage_e_item.get("stage_d_document_type") or prescan_doc_type or resolved
        doc_type = resolved or stage_d
        route_type = (
            "edited_volume_parent_record_candidate"
            if doc_type in {"edited_volume", "book_chapter"}
            else "book_parent_record_candidate"
        )
        return {
            **base_route,
            "route_type": route_type,
            "workflow_status": "machine_escalation_needed",
            "planned_db_action": "insert parent row after validation (--allow-db-write)",
            "planned_pdf_action": "move staged PDF to source-pdfs/<parent_id>.pdf and verify original staging path is gone (--allow-pdf-copy)",
            "planned_evaluation_action": "deferred_not_evaluated_for_book_or_edited_volume_parent",
            "evaluation_deferred": True,
            "machine_escalation_needed": True,
            "stage_e_resolution": {"decision": decision, "confidence": dup.get("confidence")},
        }
    if decision == "supplement_or_child_record_candidate":
        return {
            **base_route,
            "route_type": "machine_escalation_required_before_write",
            "workflow_status": "machine_escalation_needed",
            "planned_db_action": "none until supplement/child linkage validated",
            "planned_pdf_action": "none until route confirmed",
            "planned_evaluation_action": "deferred until route confirmed",
            "evaluation_deferred": True,
            "machine_escalation_needed": True,
            "stage_e_resolution": {"decision": decision, "confidence": dup.get("confidence")},
        }
    if decision == "new_record_candidate":
        route = dict(base_route)
        route["stage_e_resolution"] = {
            "decision": decision,
            "confidence": dup.get("confidence"),
            "rationale": dup.get("rationale"),
            "matched_existing_db_id": None,
        }
        route.pop("route_may_change_after", None)
        return route
    return base_route


def _build_selection_shortfall(
    prescan: dict[str, Any],
    run_plan: dict[str, Any],
    max_papers: int | None,
) -> dict[str, Any]:
    """Explain why fewer than max_papers were selected under the active profile."""
    profile_name = resolve_selection_profile(run_plan.get("selection_profile"))
    profile = SELECTION_PROFILES[profile_name]
    selected_names = {p["name"] for p in run_plan.get("pdf_plans", [])}
    requested = max_papers if max_papers is not None else len(selected_names)
    selected_count = len(selected_names)
    buckets: dict[str, list[str]] = {
        "already_in_db": [],
        "document_type_excluded_by_selection_profile": [],
        "supplement_or_child_item_excluded": [],
        "text_not_likely_extractable": [],
        "already_archived_in_prior_run": [],
        "eligible_but_not_selected_due_to_max_papers": [],
    }
    if profile_name == "narrow":
        buckets["book_edited_volume_requires_parent_path"] = []
        buckets["other_or_unknown_requires_machine_escalation"] = []
    processed_root = config.STAGING / "processed"
    archived_names: set[str] = set()
    if processed_root.exists():
        for p in processed_root.rglob("*.pdf"):
            archived_names.add(p.name)

    eligible_not_selected: list[str] = []
    for pdf in prescan.get("pdfs", []):
        name = pdf["name"]
        if name in selected_names:
            continue
        doc_type = pdf.get("document_type_classification", "other_or_unknown")
        if name in archived_names:
            buckets["already_archived_in_prior_run"].append(name)
        elif pdf.get("already_in_db"):
            buckets["already_in_db"].append(name)
        elif doc_type in {"supplement", "book_chapter", "thesis", "poster_or_abstract"}:
            buckets["supplement_or_child_item_excluded"].append(name)
        elif doc_type not in profile["allowed_types"]:
            if profile_name == "narrow" and doc_type in {"book", "edited_volume"}:
                buckets["book_edited_volume_requires_parent_path"].append(name)
            elif profile_name == "narrow" and doc_type == "other_or_unknown":
                buckets["other_or_unknown_requires_machine_escalation"].append(name)
            else:
                buckets["document_type_excluded_by_selection_profile"].append(name)
        elif not pdf.get("text_likely"):
            buckets["text_not_likely_extractable"].append(name)
        elif _pdf_eligible_for_profile(pdf, profile_name):
            eligible_not_selected.append(name)

    if len(eligible_not_selected) > max(0, requested - selected_count):
        buckets["eligible_but_not_selected_due_to_max_papers"] = eligible_not_selected

    counts = {k: len(v) for k, v in buckets.items()}
    return {
        "requested_max_papers": requested,
        "selected_count": selected_count,
        "shortfall": max(0, requested - selected_count),
        "selection_profile": profile_name,
        "selection_profile_label": profile["label"],
        "selection_profile_description": profile["description"],
        "total_staging_pdfs": prescan.get("pdf_count", 0),
        "breakdown_counts": counts,
        "breakdown_examples": {k: v[:5] for k, v in buckets.items() if v},
        "explanation": (
            f"Requested {requested} PDFs but only {selected_count} matched profile '{profile_name}'."
            if selected_count < requested
            else f"All {selected_count} requested PDFs under profile '{profile_name}' were selected."
        ),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _item_lifecycle(route: dict[str, Any]) -> dict[str, Any]:
    """Return the internal lifecycle a future live workflow would execute."""
    route_type = route.get("route_type", "")
    if route.get("duplicate_log_only") or route_type == "exact_duplicate_skip_candidate":
        return {
            "lifecycle_type": "duplicate_skip_log_only",
            "status": "ready_for_orchestrated_duplicate_log",
            "steps": DUPLICATE_ITEM_LIFECYCLE,
            "fail_closed_until": [],
        }

    if route_type in {
        "book_parent_record_candidate",
        "edited_volume_parent_record_candidate",
        "parent_plus_child_chapter_candidates",
    }:
        return {
            "lifecycle_type": "parent_ingest",
            "status": "requires_generalized_parent_validation_before_write",
            "steps": PARENT_ITEM_LIFECYCLE,
            "fail_closed_until": [
                "generalized parent validation passes for this item",
                "duplicate reassessment is no_duplicate_signal",
                "proposed parent ID has no DB collision",
                "fresh DB backup is created before DB writes",
                "all requested side-effect gates are explicit",
            ],
        }

    return {
        "lifecycle_type": "unresolved_or_article_ingest",
        "status": "not_enabled_for_first_full_live_workflow",
        "steps": [
            {
                "phase": "route_plan",
                "action": "resolve item-specific route before enabling side effects",
                "side_effect": "none",
                "required_gate": None,
            }
        ],
        "fail_closed_until": ["route is promoted to a supported duplicate or parent lifecycle"],
    }


def _book_like_reclassification(name: str, size_bytes: int) -> str | None:
    low = name.lower()
    doc_type, _ = classify_document_type(name)
    if doc_type in {"book", "edited_volume", "book_chapter"}:
        return doc_type
    if size_bytes > 8_000_000 and any(m in low for m in ("handbook", "kinetic study", "pegilation", "neuroeconomics")):
        if "koren" in low or any(m in low for m in BOOK_LIKE_MARKERS):
            return "book"
        if "harris" in low or any(m in low for m in EDITED_VOLUME_MARKERS):
            return "edited_volume"
    return None


def _infer_route(pdf: dict[str, Any], prescan_pdf: dict[str, Any] | None) -> dict[str, Any]:
    """Plan-only route inference from prescan + run-plan fields."""
    name = pdf.get("name", "")
    prescan_pdf = prescan_pdf or {}
    signals = prescan_pdf.get("already_in_db_signals") or pdf.get("already_in_db_signals") or []
    doc_type = pdf.get("document_type_classification") or prescan_pdf.get("document_type_classification", "other_or_unknown")
    reclassified = _book_like_reclassification(name, int(pdf.get("size_bytes") or prescan_pdf.get("size_bytes") or 0))

    if signals or pdf.get("already_in_db"):
        return {
            "route_type": "exact_duplicate_skip_candidate",
            "workflow_status": "duplicate_skip_log_only",
            "planned_db_action": "none (duplicate-skip log only)",
            "planned_pdf_action": "none (staged file retained unless future duplicate-archive policy)",
            "planned_evaluation_action": "none (already represented in corpus)",
            "evaluation_deferred": True,
            "child_records_deferred": True,
            "duplicate_log_only": True,
            "matched_existing_db_id_preview": None,
            "machine_escalation_needed": False,
            "human_review_needed": False,
            "internal_phases": ["text_metadata_preview", "metadata_duplicate_resolution", "route_plan", "duplicate_skip_log", "final_report"],
        }

    if reclassified == "book" or doc_type == "book":
        return {
            "route_type": "book_parent_record_candidate",
            "workflow_status": "machine_escalation_needed",
            "planned_db_action": "insert book parent row after validation (--allow-db-write)",
            "planned_pdf_action": "move staged PDF to source-pdfs/<parent_id>.pdf and verify original staging path is gone (--allow-pdf-copy)",
            "planned_evaluation_action": "deferred_not_evaluated_for_book_parent",
            "evaluation_deferred": True,
            "child_records_deferred": True,
            "duplicate_log_only": False,
            "machine_escalation_needed": True,
            "human_review_needed": False,
            "internal_phases": [
                "text_metadata_preview",
                "metadata_duplicate_resolution",
                "route_plan",
                "book_toc_chapter_preview",
                "parent_validation",
                "parent_record_create",
                "source_pdf_move",
                "fileinfo_attach",
                "staging_cleanup_check",
                "final_report",
            ],
        }

    if reclassified == "edited_volume" or doc_type in {"edited_volume", "book_chapter"}:
        route = "edited_volume_parent_record_candidate"
        if doc_type == "book_chapter":
            route = "parent_plus_child_chapter_candidates"
        return {
            "route_type": route,
            "workflow_status": "machine_escalation_needed",
            "planned_db_action": "insert edited-volume parent row first; child records deferred (--allow-db-write)",
            "planned_pdf_action": "move staged PDF to source-pdfs/<parent_id>.pdf and verify original staging path is gone (--allow-pdf-copy)",
            "planned_evaluation_action": "deferred_not_evaluated_for_edited_volume_parent",
            "evaluation_deferred": True,
            "child_records_deferred": True,
            "duplicate_log_only": False,
            "machine_escalation_needed": True,
            "human_review_needed": False,
            "internal_phases": [
                "text_metadata_preview",
                "metadata_duplicate_resolution",
                "route_plan",
                "book_toc_chapter_preview",
                "parent_validation",
                "parent_record_create",
                "source_pdf_move",
                "fileinfo_attach",
                "staging_cleanup_check",
                "final_report",
            ],
        }

    if doc_type not in {"journal_article", "review_article", "protocol"}:
        return {
            "route_type": "machine_escalation_required_before_write",
            "workflow_status": "machine_escalation_needed",
            "planned_db_action": "none until document type and metadata validated",
            "planned_pdf_action": "none until route confirmed",
            "planned_evaluation_action": "deferred until ingest route confirmed",
            "evaluation_deferred": True,
            "child_records_deferred": True,
            "duplicate_log_only": False,
            "machine_escalation_needed": True,
            "human_review_needed": False,
            "internal_phases": [
                "text_metadata_preview",
                "metadata_duplicate_resolution",
                "route_plan",
                "book_toc_chapter_preview",
                "final_report",
            ],
        }

    return {
        "route_type": "ingest_new_record_candidate",
        "workflow_status": "planned",
        "planned_db_action": "insert new row after validation (--allow-db-write)",
        "planned_pdf_action": "move staged PDF to source-pdfs/<final_id>.pdf after validation (--allow-pdf-copy)",
        "planned_evaluation_action": "Claude Sonnet evaluation required for completion (--allow-paid-api)",
        "evaluation_deferred": False,
        "child_records_deferred": True,
        "duplicate_log_only": False,
        "machine_escalation_needed": False,
        "human_review_needed": False,
        "route_may_change_after": "metadata_duplicate_resolution may re-route to exact_duplicate_skip_candidate or book/edited-volume parent candidate",
        "internal_phases": [
            "text_metadata_preview",
            "metadata_duplicate_resolution",
            "route_plan",
            "book_toc_chapter_preview",
            "duplicate_skip_log",
            "parent_record_create",
            "source_pdf_move",
            "fileinfo_attach",
            "staging_cleanup_check",
            "final_report",
        ],
    }


def _safety_summary(items: list[dict[str, Any]], run_plan: dict[str, Any]) -> dict[str, Any]:
    def _needs_db(item: dict[str, Any]) -> bool:
        if item["route"].get("duplicate_log_only"):
            return False
        action = item["route"]["planned_db_action"]
        return not action.startswith("none")

    needs_db = any(_needs_db(i) for i in items)
    needs_pdf = any(
        not i["route"].get("duplicate_log_only") and "--allow-pdf-copy" in i["route"]["planned_pdf_action"]
        for i in items
    )
    needs_eval = any(not i["route"].get("evaluation_deferred", False) for i in items)
    needs_backup = needs_db or needs_pdf
    machine_escalation = [i["name"] for i in items if i["route"]["machine_escalation_needed"]]
    human_review = [i["name"] for i in items if i["route"]["human_review_needed"]]
    duplicate_only = [i["name"] for i in items if i["route"]["duplicate_log_only"]]

    return {
        "plan_only": True,
        "db_writes_would_be_required": needs_db,
        "pdf_move_to_source_would_be_required": needs_pdf,
        "successful_processed_archive_would_be_created": False,
        "paid_api_would_be_required": needs_eval,
        "backup_would_be_created": needs_backup,
        "human_review_needed": human_review,
        "machine_escalation_remaining": machine_escalation,
        "duplicate_log_only_items": duplicate_only,
        "estimated_cost_usd_if_evaluation_enabled": run_plan.get("estimated_cost_usd"),
        "estimated_runtime_hours_if_evaluation_enabled": run_plan.get("estimated_runtime_hours"),
        "live_gates_required": {
            "allow_db_write": needs_db,
            "allow_pdf_copy": needs_pdf,
            "allow_paid_api": needs_eval,
        },
        "current_side_effects": {
            "api_calls": "none",
            "db_writes": "none",
            "pdf_operations": "none",
            "id_changes": "none",
        },
    }


def build_future_live_command(
    *,
    run_id: str,
    mode: str,
    max_papers: int,
    selection_profile: str = "broader",
    pilot_profile: str = "safe-live",
) -> dict[str, str]:
    base = (
        f"python pipeline/run_corpus_pipeline.py --workflow ingest --mode {mode} "
        f"--selection-profile {selection_profile} --pilot-profile {pilot_profile} "
        f"--max-papers {max_papers} --run-id {run_id} --execute"
    )
    gated = base + " --allow-db-write --allow-pdf-copy"
    return {
        "plan_only_command": (
            f"python pipeline/run_corpus_pipeline.py --workflow ingest --mode {mode} "
            f"--selection-profile {selection_profile} --pilot-profile {pilot_profile} "
            f"--max-papers {max_papers} --plan-only"
        ),
        "future_live_command_do_not_run_yet": gated,
        "future_live_with_evaluation_do_not_run_yet": gated + " --allow-paid-api",
        "note": "Full live workflow remains fail-closed until the orchestrated dispatcher is promoted beyond item-scoped pilot paths.",
    }


def build_workflow_plan(
    *,
    prescan: dict[str, Any],
    standard: dict[str, Any],
    recommendation: dict[str, str],
    requested_mode: str | None,
    max_papers: int | None,
    args,
    run_plan: dict[str, Any] | None = None,
    run_id: str = "",
    stage_e_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build consolidated workflow plan (read-only)."""
    if run_plan is None:
        run_plan = build_run_plan(
            prescan=prescan,
            standard=standard,
            recommendation=recommendation,
            requested_mode=requested_mode,
            max_papers=max_papers,
            args=args,
        )

    stage_e_by_name = {i["name"]: i for i in (stage_e_summary or {}).get("items", [])}
    prescan_by_name = {p["name"]: p for p in prescan.get("pdfs", [])}
    items: list[dict[str, Any]] = []
    for pdf in run_plan.get("pdf_plans", []):
        base_route = _infer_route(pdf, prescan_by_name.get(pdf["name"]))
        stage_e_item = stage_e_by_name.get(pdf["name"])
        route = (
            _apply_stage_e_route(base_route, stage_e_item, prescan_doc_type=pdf.get("document_type_classification", ""))
            if stage_e_item
            else base_route
        )
        items.append(
            {
                "index": pdf.get("index"),
                "name": pdf["name"],
                "path": pdf.get("path"),
                "sha256": pdf.get("sha256"),
                "document_type_prescan": pdf.get("document_type_classification"),
                "route": route,
                "lifecycle": _item_lifecycle(route),
                "pilot_suitability": (
                    assess_live_pilot_suitability(
                        name=pdf["name"],
                        prescan_doc_type=pdf.get("document_type_classification", ""),
                        route=route,
                        stage_e_item=stage_e_item,
                    )
                    if run_plan.get("pilot_profile") == "safe-live"
                    else None
                ),
            }
        )

    ingest_candidates = [i for i in items if not i["route"].get("duplicate_log_only")]
    eval_eligible_count = sum(1 for i in ingest_candidates if not i["route"].get("evaluation_deferred"))
    selection_shortfall = _build_selection_shortfall(prescan, run_plan, max_papers)
    duplicate_matches = [
        {
            "staged_filename": i["name"],
            "matched_existing_db_id": i["route"].get("matched_existing_db_id"),
            "stage_e_decision": (i["route"].get("stage_e_resolution") or {}).get("decision"),
            "route_type": i["route"]["route_type"],
        }
        for i in items
        if i["route"].get("duplicate_log_only") or i["route"]["route_type"] == "exact_duplicate_skip_candidate"
    ]

    safety = _safety_summary(items, run_plan)
    phase_ids = []
    for item in items:
        for phase in item["route"]["internal_phases"]:
            if phase not in phase_ids:
                phase_ids.append(phase)
    ordered_phases = [p for p in INTERNAL_PHASES if p["id"] in phase_ids]

    return {
        "created_at": _now(),
        "workflow": "ingest",
        "mode": "plan_only",
        "read_only": True,
        "run_id": run_id,
        "selected_mode": run_plan.get("selected_mode"),
        "selection_profile": run_plan.get("selection_profile", "narrow"),
        "selection_profile_label": run_plan.get("selection_profile_label", ""),
        "pilot_profile": run_plan.get("pilot_profile", "none"),
        "pilot_profile_label": run_plan.get("pilot_profile_label", ""),
        "pilot_selection_audit": run_plan.get("pilot_selection_audit"),
        "pilot_suitability_summary": (
            {
                "all_suitable": all(
                    i.get("pilot_suitability", {}).get("suitable_for_first_orchestrated_live_test", False)
                    for i in items
                ),
                "items": [i["pilot_suitability"] for i in items if i.get("pilot_suitability")],
            }
            if run_plan.get("pilot_profile") == "safe-live"
            else None
        ),
        "selected_pdf_count": run_plan.get("selected_pdf_count"),
        "corpus_policy": CORPUS_POLICY,
        "internal_phases": ordered_phases,
        "pilot_stage_mapping": PILOT_STAGE_TO_PHASE,
        "workflow_statuses": list(WORKFLOW_STATUSES),
        "route_types": list(ROUTE_TYPES),
        "items": items,
        "orchestrated_lifecycle_model": {
            "description": "Each selected PDF is handled as one internal lifecycle; users call --workflow ingest rather than per-item stages.",
            "duplicate_lifecycle": DUPLICATE_ITEM_LIFECYCLE,
            "parent_lifecycle": PARENT_ITEM_LIFECYCLE,
            "debugging_note": "Granular stage commands remain available for diagnosis, but normal operation should use the workflow entrypoint.",
        },
        "stage_e_integrated": stage_e_summary is not None,
        "duplicate_matches": duplicate_matches,
        "selection_shortfall": selection_shortfall,
        "run_plan_summary": {
            "estimated_api_calls": eval_eligible_count,
            "estimated_cost_usd": run_plan.get("estimated_cost_usd") if eval_eligible_count else 0,
            "estimated_runtime_hours": run_plan.get("estimated_runtime_hours") if eval_eligible_count else 0,
            "quality_warning": run_plan.get("quality_warning"),
            "eval_eligible_count_after_stage_e": eval_eligible_count,
        },
        "safety_summary": safety,
        "live_gates": LIVE_GATES,
        "granular_stages_preserved": True,
        "granular_stage_modules": [
            "corpus_stage_d_preview.py through corpus_stage_t_final_pilot_report.py",
            "corpus_execute_guard.py (stage dispatcher for debugging)",
            "item-scoped orchestrated pilot modules: corpus_live_duplicate_only.py, corpus_live_parent_only.py, corpus_live_pdf_copy_only.py, corpus_live_fileinfo_only.py, corpus_live_staging_archive_only.py",
        ],
        "proven_pilot_reference_run": "20260528-203823",
        "proven_pilot_outcomes_summary": {
            "completed_parent_lifecycle": ["Cacioppo_2013_Handbook_book"],
            "duplicate_skips": ["Levitin2018"],
            "pending_parent_candidates": [
                "Ashley/Timmers",
                "Hallam/Cross/Thaut (requires title-page/OCR fix)",
            ],
        },
    }


def write_workflow_artifacts(
    workflow: dict[str, Any],
    run_plan: dict[str, Any],
    *,
    runs_dir: str = "",
    run_dir: Path | None = None,
) -> tuple[Path, Path, Path, Path]:
    """Write workflow plan, workflow report, run plan, and run report under run folder."""
    if run_dir is None:
        plan_path, report_path = write_run_plan(run_plan, runs_dir=runs_dir)
        run_dir = plan_path.parent
    else:
        run_dir.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "run_plan.json"
        report_path = run_dir / "run_report.txt"
        plan_path.write_text(json.dumps(run_plan, indent=2), encoding="utf-8")
        report_path.write_text(render_run_report(run_plan), encoding="utf-8")

    workflow["run_id"] = run_dir.name
    wf_json = run_dir / "workflow_plan.json"
    wf_txt = run_dir / "workflow_report.txt"
    wf_json.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
    wf_txt.write_text(render_workflow_report(workflow), encoding="utf-8")
    workflow["artifact_paths"] = {
        "run_plan": str(plan_path),
        "run_report": str(report_path),
        "workflow_plan": str(wf_json),
        "workflow_report": str(wf_txt),
    }
    wf_json.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
    return plan_path, report_path, wf_json, wf_txt


def render_workflow_report(workflow: dict[str, Any]) -> str:
    lines = [
        "Corpus Ingest Workflow Plan (READ-ONLY)",
        "=" * 48,
        f"Workflow: {workflow.get('workflow')}",
        f"Mode: {workflow.get('selected_mode')} | Profile: {workflow.get('selection_profile', 'narrow')} | "
        f"Pilot: {workflow.get('pilot_profile', 'none')} | Plan-only: {workflow.get('read_only')}",
        f"Run ID: {workflow.get('run_id') or '(assigned on write)'}",
        f"Selected PDFs: {workflow.get('selected_pdf_count')}",
        "",
        "User experience (target)",
        "-" * 48,
        "1. Add PDFs to papers-staging",
        "2. Run: python pipeline/run_corpus_pipeline.py --workflow ingest --mode A --max-papers N --plan-only",
        "3. Review this report",
        "4. When ready, run future live command with explicit gates (not enabled yet)",
        "",
        "Internal phases (handled automatically in live mode)",
        "-" * 48,
    ]
    for phase in workflow.get("internal_phases", []):
        pilot = phase.get("pilot_stage") or "—"
        lines.append(f"- {phase['id']}: {phase['label']} (pilot stage {pilot})")

    lines.extend(["", "Per-PDF planned routes", "-" * 48])
    for item in workflow.get("items", []):
        route = item["route"]
        lines.extend(
            [
                f"\n{item['index']}. {item['name']}",
                f"   Route:        {route['route_type']}",
                f"   Status:       {route['workflow_status']}",
                f"   DB action:    {route['planned_db_action']}",
                f"   PDF action:   {route['planned_pdf_action']}",
                f"   Evaluation:   {route['planned_evaluation_action']}",
                f"   Eval deferred:{route['evaluation_deferred']}",
                f"   Child defer:  {route['child_records_deferred']}",
                f"   Dup log-only: {route['duplicate_log_only']}",
            ]
        )
        if route.get("matched_existing_db_id"):
            lines.append(f"   Matched DB ID:{route['matched_existing_db_id']}")
        if route.get("stage_e_resolution"):
            se = route["stage_e_resolution"]
            lines.append(f"   Stage E:      {se.get('decision')} ({se.get('confidence')})")
        if route.get("route_may_change_after"):
            lines.append(f"   Note:         {route['route_may_change_after']}")
        ps = item.get("pilot_suitability")
        if ps:
            lines.append(f"   Pilot fit:    {'suitable' if ps.get('suitable_for_first_orchestrated_live_test') else 'unsuitable'}")
            for reason in ps.get("suitability_reasons", [])[:2]:
                lines.append(f"                 {reason}")
        lifecycle = item.get("lifecycle") or {}
        if lifecycle:
            lines.append(f"   Lifecycle:    {lifecycle.get('lifecycle_type')} ({lifecycle.get('status')})")
            for step in (lifecycle.get("steps") or [])[:4]:
                gate = f" gate={step.get('required_gate')}" if step.get("required_gate") else ""
                lines.append(f"                 - {step.get('phase')}: {step.get('action')}{gate}")
            if len(lifecycle.get("steps") or []) > 4:
                lines.append("                 - ...")

    lifecycle_model = workflow.get("orchestrated_lifecycle_model") or {}
    if lifecycle_model:
        lines.extend(["", "Orchestrated item lifecycle", "-" * 48])
        lines.append(lifecycle_model.get("description", ""))
        lines.append("Duplicate lifecycle:")
        for step in lifecycle_model.get("duplicate_lifecycle", []):
            gate = f" [{step.get('required_gate')}]" if step.get("required_gate") else ""
            lines.append(f"- {step.get('phase')}: {step.get('action')}{gate}")
        lines.append("Parent lifecycle:")
        for step in lifecycle_model.get("parent_lifecycle", []):
            gate = f" [{step.get('required_gate')}]" if step.get("required_gate") else ""
            lines.append(f"- {step.get('phase')}: {step.get('action')}{gate}")

    audit = workflow.get("pilot_selection_audit")
    if audit:
        lines.extend(["", "Safe-live pilot selection audit", "-" * 48])
        lines.append(f"Parent candidates ranked: {len(audit.get('ranked_parent_candidates', []))}")
        lines.append(f"Selected: {audit.get('selected_count')} (parents: {audit.get('parent_candidate_count')})")
        if audit.get("duplicate_control_added"):
            lines.append(f"Duplicate control: {audit['duplicate_control_added']}")
        if audit.get("excluded_hash_like"):
            lines.append(f"Excluded hash-like: {len(audit['excluded_hash_like'])}")
        if audit.get("excluded_other_or_unknown"):
            lines.append(f"Excluded other_or_unknown: {len(audit['excluded_other_or_unknown'])}")

    shortfall = workflow.get("selection_shortfall", {})
    if shortfall:
        lines.extend(
            [
                "",
                "Selection shortfall",
                "-" * 48,
                shortfall.get("explanation", ""),
                f"Requested: {shortfall.get('requested_max_papers')} | Selected: {shortfall.get('selected_count')} | Shortfall: {shortfall.get('shortfall')}",
                f"Profile: {shortfall.get('selection_profile')} — {shortfall.get('selection_profile_description')}",
            ]
        )
        for key, count in (shortfall.get("breakdown_counts") or {}).items():
            if count:
                examples = (shortfall.get("breakdown_examples") or {}).get(key, [])
                ex = f" e.g. {examples[0]}" if examples else ""
                lines.append(f"- {key}: {count}{ex}")

    if workflow.get("duplicate_matches"):
        lines.extend(["", "Duplicate matches (Stage E)", "-" * 48])
        for dm in workflow["duplicate_matches"]:
            lines.append(
                f"- {dm['staged_filename']} → {dm.get('matched_existing_db_id')} "
                f"({dm.get('stage_e_decision')})"
            )

    summary = workflow.get("run_plan_summary", {})
    lines.extend(
        [
            "",
            "Cost/time if evaluation enabled later",
            "-" * 48,
            f"Estimated API calls: {summary.get('estimated_api_calls')}",
            f"Estimated cost: ${summary.get('estimated_cost_usd')}",
            f"Estimated runtime: {summary.get('estimated_runtime_hours')}h",
        ]
    )
    if summary.get("quality_warning"):
        lines.append(f"WARNING: {summary['quality_warning']}")

    safety = workflow.get("safety_summary", {})
    lines.extend(
        [
            "",
            "Safety summary",
            "-" * 48,
            f"DB writes would be required:        {safety.get('db_writes_would_be_required')}",
            f"PDF move-to-source would be required: {safety.get('pdf_move_to_source_would_be_required')}",
            f"Successful processed archive created: {safety.get('successful_processed_archive_would_be_created')}",
            f"Paid API would be required:         {safety.get('paid_api_would_be_required')}",
            f"Backup would be created:            {safety.get('backup_would_be_created')}",
            f"Machine escalation remaining:       {safety.get('machine_escalation_remaining') or 'none'}",
            f"Human review needed:                {safety.get('human_review_needed') or 'none'}",
            f"Duplicate log-only items:           {safety.get('duplicate_log_only_items') or 'none'}",
        ]
    )

    policy = workflow.get("corpus_policy", {})
    lines.extend(
        [
            "",
            "Current-corpus policy",
            "-" * 48,
            f"- Keep valid staged PDFs: {policy.get('keep_valid_staged_pdfs')}",
            f"- Topical relevance: {policy.get('topical_relevance')}",
            f"- Guideline mismatch: {policy.get('guideline_mismatch')}",
            f"- Child/chapter records: {policy.get('child_chapter_records')}",
            f"- Parent evaluation: {policy.get('parent_evaluation')}",
            f"- Human review: {policy.get('human_review')}",
        ]
    )

    lines.extend(
        [
            "",
            "Granular pilot stages preserved",
            "-" * 48,
            "Stages D–T remain available via --execute-stage for debugging.",
            "This workflow layer orchestrates them internally in future live mode.",
            "",
            "Current step side effects: NONE",
            "- No API calls",
            "- No papers.db writes",
            "- No PDF moves/copies/archives/deletes",
            "- No ID changes",
        ]
    )
    return "\n".join(lines) + "\n"


def run_ingest_workflow_plan_only(
    *,
    prescan: dict[str, Any],
    standard: dict[str, Any],
    recommendation: dict[str, str],
    requested_mode: str | None,
    max_papers: int | None,
    args,
) -> dict[str, Any]:
    """Top-level plan-only ingest workflow entry."""
    run_plan = build_run_plan(
        prescan=prescan,
        standard=standard,
        recommendation=recommendation,
        requested_mode=requested_mode,
        max_papers=max_papers,
        args=args,
    )
    plan_path, report_path = write_run_plan(run_plan, runs_dir=args.runs_dir)
    run_dir = plan_path.parent

    run_text_metadata_preview(run_plan, run_dir)
    stage_e_summary = run_metadata_duplicate_resolution_preview(run_plan, run_dir)

    workflow = build_workflow_plan(
        prescan=prescan,
        standard=standard,
        recommendation=recommendation,
        requested_mode=requested_mode,
        max_papers=max_papers,
        args=args,
        run_plan=run_plan,
        run_id=run_dir.name,
        stage_e_summary=stage_e_summary,
    )
    _, _, wf_json, wf_txt = write_workflow_artifacts(workflow, run_plan, run_dir=run_dir)
    commands = build_future_live_command(
        run_id=plan_path.parent.name,
        mode=workflow["selected_mode"],
        max_papers=workflow["selected_pdf_count"],
        selection_profile=workflow.get("selection_profile", "broader"),
        pilot_profile=workflow.get("pilot_profile", "safe-live"),
    )
    workflow["future_commands"] = commands
    wf_json.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
    return {
        "workflow": workflow,
        "run_plan": run_plan,
        "plan_path": plan_path,
        "report_path": report_path,
        "workflow_plan_path": wf_json,
        "workflow_report_path": wf_txt,
        "future_commands": commands,
    }
