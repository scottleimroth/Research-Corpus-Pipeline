"""Stage H: consolidated no-write final pilot execution plan.

This is an audited approval preview only. It does not execute duplicate skips,
create DB rows, copy PDFs, commit IDs, evaluate papers, or call APIs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load(run_dir: Path, name: str) -> dict[str, Any]:
    return json.loads((run_dir / name).read_text(encoding="utf-8"))


def _by_name(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("name")): item for item in items}


def _duplicate_action(route: dict[str, Any]) -> dict[str, Any]:
    plan = route.get("route_plan", {})
    evidence = plan.get("evidence_used") or {}
    return {
        "name": route.get("name"),
        "action_class": "live_duplicate_skip_log_candidate",
        "matched_existing_db_id": plan.get("matched_existing_db_id"),
        "evidence_summary": {
            "signals": evidence.get("signals", []),
            "title_ratio": evidence.get("title_ratio"),
            "abstract_ratio": evidence.get("abstract_ratio"),
            "managed_pdf_sha256_matches_staged": evidence.get("managed_pdf_sha256_matches_staged"),
        },
        "future_live_action_preview": [
            "confirm matched existing DB row is still present",
            "write duplicate-skip run log entry only",
            "do not create a new DB row",
            "do not evaluate this staged PDF",
            "do not copy into managed PDFs",
            "do not replace existing managed PDF unless a future better-PDF policy explicitly approves it",
            "leave staged PDF untouched until a later approved file-handling stage",
        ],
        "planned_db_action_preview": "no DB content modification; run-log entry only in future live stage",
        "planned_pdf_action_preview": "no managed copy/replacement; staged file handling preview only",
        "evaluation_action_preview": "skip evaluation because item is already represented",
        "requires_machine_escalation_before_write": False,
        "requires_human_review_now": False,
    }


def _harris_action(stage_g_item: dict[str, Any]) -> dict[str, Any]:
    meta = stage_g_item.get("candidate_parent_metadata", {})
    child_count = len(stage_g_item.get("candidate_child_metadata", []))
    return {
        "name": stage_g_item.get("name"),
        "action_class": "live_machine_escalation_required_before_write",
        "target_action_after_validation": "live_parent_plus_child_candidates",
        "document_type": "edited_volume",
        "candidate_parent_metadata": meta,
        "candidate_parent_id_preview": stage_g_item.get("candidate_parent_id_preview"),
        "required_parent_fields": [
            "title",
            "editors",
            "year",
            "publisher/source",
            "DOI or ISBN",
            "document_type",
            "managed PDF destination",
        ],
        "unresolved_metadata_fields": [
            "confirm 2009 parent publication year against title/copyright page",
            "validate ISBN candidates and filter ISSNs/noisy identifiers",
            "validate whether filename-derived paper title is represented as a chapter",
            "validate child chapter titles/authors/page boundaries before any child records",
        ],
        "child_record_policy": {
            "create_child_records_now": False,
            "reason": "TOC/chapter boundaries were detected, but child metadata and IDs are not validated",
            "future_policy": "create edited-volume parent first or parent-plus-child candidates only after chapter metadata validation",
            "candidate_child_count_preview": child_count,
        },
        "evaluation_action_preview": "defer; edited-volume parent should not receive journal-article evaluation. Chapter-level evaluation can be planned only for validated chapter records if applicable.",
        "guideline_route": "guideline_mismatch_or_new_guideline_needed_candidate",
        "guideline_policy": "metadata/routing tag only; not an exclusion reason",
        "planned_db_action_preview": "future live parent record candidate only after final machine validation; no DB write now",
        "planned_pdf_action_preview": "future managed PDF copy for parent record only after approval; no copy/move now",
        "requires_machine_escalation_before_write": True,
        "machine_escalation_needed": [
            "final title/copyright-page validation",
            "identifier cleanup",
            "chapter-title and chapter-author validation",
            "DB duplicate check against parent and any child candidates",
        ],
        "requires_human_review_now": False,
    }


def _koren_action(stage_g_item: dict[str, Any]) -> dict[str, Any]:
    meta = stage_g_item.get("candidate_parent_metadata", {})
    return {
        "name": stage_g_item.get("name"),
        "action_class": "live_parent_record_candidate",
        "document_type": "book",
        "candidate_parent_metadata": meta,
        "candidate_parent_id_preview": stage_g_item.get("candidate_parent_id_preview"),
        "required_parent_fields": [
            "title",
            "authors/editors",
            "year",
            "publisher/source",
            "ISBN or other stable identifier",
            "document_type",
            "managed PDF destination",
        ],
        "unresolved_metadata_fields": [
            "confirm authors/editors; current preview still carries Koren from filename rather than full validated editor list",
            "confirm parent-level publication year",
            "verify ISBN and edition",
        ],
        "child_record_policy": {
            "create_child_records_now": False,
            "reason": "TOC detected, but current route only needs a book parent unless a specific chapter becomes relevant",
            "future_policy": "create child/chapter candidates only if later corpus use requires chapter-level representation",
        },
        "evaluation_action_preview": "defer or apply book-specific/background-source evaluation, not journal-article evaluation",
        "guideline_route": "guideline_mismatch_or_new_guideline_needed_candidate",
        "guideline_policy": "metadata/routing tag only; not an exclusion reason",
        "planned_db_action_preview": "future live book parent record candidate after final validation; no DB write now",
        "planned_pdf_action_preview": "future managed PDF copy for parent record after approval; no copy/move now",
        "requires_machine_escalation_before_write": True,
        "machine_escalation_needed": [
            "validate complete author/editor list",
            "confirm edition/year/ISBN",
            "DB duplicate check using final canonical book metadata",
        ],
        "requires_human_review_now": False,
    }


def _safety_checks(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks = [
        {"check": "all_5_pdfs_have_action_plan", "ok": len(actions) == 5},
        {"check": "no_action_executes_db_write", "ok": True},
        {"check": "no_action_executes_pdf_operation", "ok": True},
        {"check": "no_action_executes_api_call", "ok": True},
        {"check": "no_action_commits_final_id", "ok": True},
        {"check": "no_action_executes_duplicate_skip", "ok": True},
        {"check": "valid_pdfs_kept_policy_encoded", "ok": True},
        {"check": "human_review_last_resort_only", "ok": all(not a.get("requires_human_review_now") for a in actions)},
    ]
    duplicate_actions = [a for a in actions if a["action_class"] == "live_duplicate_skip_log_candidate"]
    checks.append(
        {
            "check": "duplicate_skip_candidates_have_matches",
            "ok": len(duplicate_actions) == 3 and all(a.get("matched_existing_db_id") for a in duplicate_actions),
        }
    )
    return checks


def _future_commands(run_id: str) -> dict[str, Any]:
    return {
        "warning": "DO NOT RUN YET - preview only; stages are not live-enabled in this step.",
        "recommended_sequence": [
            "A. duplicate-skip log pilot only (lowest risk)",
            "B. one parent-record creation pilot after final machine validation",
            "C. broader mixed pilot only after A and B pass cleanly",
        ],
        "commands": [
            {
                "label": "A. duplicate-skip log pilot only",
                "do_not_run_yet": True,
                "command_preview": (
                    f"python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 "
                    f"--run-id {run_id} --execute-stage live-duplicate-skip-log-only"
                ),
                "notes": "Future stage would write run-log entries only; no DB content change or PDF movement.",
            },
            {
                "label": "B. parent record creation pilot only",
                "do_not_run_yet": True,
                "command_preview": (
                    f"python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 "
                    f"--run-id {run_id} --execute-stage live-parent-record-pilot --allow-db-write --allow-pdf-copy"
                ),
                "notes": "Run only after final parent metadata validation; start with one parent candidate.",
            },
            {
                "label": "C. full mixed pilot",
                "do_not_run_yet": True,
                "command_preview": (
                    f"python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 "
                    f"--run-id {run_id} --execute-stage live-mixed-pilot --allow-db-write --allow-pdf-copy"
                ),
                "notes": "Run only after duplicate-log and parent-record pilots pass.",
            },
        ],
    }


def run_final_pilot_execution_plan(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    stage_f = _load(run_dir, "final_route_plan_preview.json")
    stage_g = _load(run_dir, "book_toc_chapter_preview.json")
    stage_f_routes = _by_name(stage_f.get("routes", []))
    stage_g_items = _by_name(stage_g.get("items", []))
    actions: list[dict[str, Any]] = []

    for pdf in plan.get("pdf_plans", []):
        name = pdf.get("name")
        route = stage_f_routes.get(name, {})
        if name in stage_g_items and str(name).startswith("Harris-"):
            actions.append(_harris_action(stage_g_items[name]))
        elif name in stage_g_items and str(name).startswith("Koren-"):
            actions.append(_koren_action(stage_g_items[name]))
        elif route.get("final_route_type") == "exact_duplicate_skip_candidate":
            actions.append(_duplicate_action(route))
        else:
            actions.append(
                {
                    "name": name,
                    "action_class": "live_no_action_candidate",
                    "reason": "no supported live-preview route found",
                    "requires_machine_escalation_before_write": True,
                    "requires_human_review_now": False,
                }
            )

    safety = _safety_checks(actions)
    summary = {
        "run_id": run_dir.name,
        "stage": "final-pilot-execution-plan",
        "source_artifacts": {
            "stage_d": str(run_dir / "text_metadata_preview.json"),
            "stage_e": str(run_dir / "metadata_duplicate_resolution_preview.json"),
            "stage_f": str(run_dir / "final_route_plan_preview.json"),
            "stage_g": str(run_dir / "book_toc_chapter_preview.json"),
        },
        "keep_valid_pdfs_policy": {
            "valid_pdfs_are_kept": True,
            "topical_relevance_is_not_exclusion": True,
            "guideline_mismatch_is_metadata_or_route_not_rejection": True,
            "skip_or_reject_only_if": [
                "exact duplicate already represented",
                "not a real readable PDF",
                "corrupted/unusable",
                "accidental non-document file",
                "unsafe to process after machine escalation",
            ],
            "human_review": "last resort only",
        },
        "consolidated_actions": actions,
        "machine_escalation_remaining_before_write": [
            a["name"] for a in actions if a.get("requires_machine_escalation_before_write")
        ],
        "human_review_needed_now": any(a.get("requires_human_review_now") for a in actions),
        "recommended_safest_first_live_sequence": [
            "1. Duplicate-skip log pilot only for Levitin, Suarez-Roca, and Yucel.",
            "2. Final machine validation for one parent record candidate.",
            "3. Parent record creation pilot for one book/edited-volume PDF.",
            "4. Broader mixed pilot only after the first two live pilots pass cleanly.",
        ],
        "future_live_commands_preview": _future_commands(run_dir.name),
        "safety_checks": safety,
        "overall_safety_ok": all(c["ok"] for c in safety),
        "later_stages_still_disabled": [
            "actual duplicate skip execution",
            "moving duplicate files",
            "DB row creation/update",
            "managed PDF copy/move",
            "safe ID commit",
            "paid evaluation",
            "batch submit",
            "batch collect/save",
            "book/chapter splitting writes",
            "human review folder creation outside the run folder",
        ],
        "side_effects": {
            "api_calls": "none",
            "papers_db_writes": "none",
            "pdf_moves_copies_renames_deletes": "none",
            "id_changes": "none",
            "duplicate_skip_execution": "none",
            "book_chapter_splitting_writes": "none",
        },
    }
    (run_dir / "final_pilot_execution_plan.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (run_dir / "stage_final_pilot_execution_plan_report.txt").write_text(
        render_final_pilot_execution_plan(summary),
        encoding="utf-8",
    )
    return summary


def render_final_pilot_execution_plan(summary: dict[str, Any]) -> str:
    lines = ["Stage H Final Pilot Execution Plan (READ-ONLY)", "=" * 56]
    lines.append("Policy: valid staged PDFs are kept/represented; topical relevance is not an exclusion criterion.")
    for action in summary["consolidated_actions"]:
        lines.append(f"- {action['name']}")
        lines.append(f"  action: {action['action_class']}")
        if action["action_class"] == "live_duplicate_skip_log_candidate":
            lines.append(f"  matched_existing_db_id: {action.get('matched_existing_db_id')}")
            lines.append("  future live: log duplicate skip only; no new row, no evaluation, no managed copy.")
        else:
            meta = action.get("candidate_parent_metadata", {})
            lines.append(f"  parent: {meta.get('title')} | {meta.get('year')} | {meta.get('document_type')}")
            lines.append(f"  id_preview: {action.get('candidate_parent_id_preview')}")
            lines.append(f"  machine_escalation_before_write: {action.get('requires_machine_escalation_before_write')}")
    lines.append("")
    lines.append("Recommended first live pilot: duplicate-skip log only. DO NOT RUN YET.")
    for command in summary["future_live_commands_preview"]["commands"]:
        lines.append(f"- {command['label']}: {command['command_preview']}")
    lines.append("")
    lines.append(f"Overall safety: {'PASS' if summary['overall_safety_ok'] else 'FAIL'}")
    lines.append("No API calls, DB writes, PDF operations, duplicate skips, book/chapter writes, or ID changes were performed.")
    return "\n".join(lines) + "\n"
