"""Stage F: read-only final route/action plan preview for the pilot run.

This stage is intentionally scoped to the current corpus and pilot pipeline. It
turns Stage E decisions into future route candidates, but performs no actions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ALLOWED_ROUTE_TYPES = {
    "ingest_new_record_candidate",
    "exact_duplicate_skip_candidate",
    "duplicate_better_pdf_candidate",
    "related_version_link_candidate",
    "book_or_edited_volume_machine_escalation",
    "book_chapter_or_child_record_machine_escalation",
    "supplement_or_child_record_machine_escalation",
    "unresolved_machine_escalation",
    "human_review_last_resort",
}

DUPLICATE_DECISION_TO_ROUTE = {
    "exact_duplicate_skip": "exact_duplicate_skip_candidate",
    "duplicate_better_pdf_candidate": "duplicate_better_pdf_candidate",
    "related_version_candidate": "related_version_link_candidate",
    "new_record_candidate": "ingest_new_record_candidate",
    "supplement_or_child_record_candidate": "supplement_or_child_record_machine_escalation",
    "book_or_edited_volume_candidate": "book_or_edited_volume_machine_escalation",
    "unresolved_needs_machine_escalation": "unresolved_machine_escalation",
    "unresolved_needs_human_review_after_machine_escalation": "human_review_last_resort",
}


def _stage_artifact(run_dir: Path, name: str) -> dict[str, Any]:
    path = run_dir / name
    return json.loads(path.read_text(encoding="utf-8"))


def _best_match(item: dict[str, Any]) -> dict[str, Any] | None:
    matches = item.get("duplicate_version_resolution", {}).get("matched_rows", [])
    return matches[0] if matches else None


def _match_summary(match: dict[str, Any] | None) -> dict[str, Any] | None:
    if not match:
        return None
    bm = match.get("basic_metadata", {})
    comp = match.get("comparison", {})
    return {
        "existing_db_id": match.get("id"),
        "existing_title": bm.get("title"),
        "existing_year": bm.get("year"),
        "existing_doi": bm.get("doi"),
        "signals": comp.get("signals", []),
        "title_ratio": comp.get("title_ratio"),
        "abstract_ratio": comp.get("abstract_ratio"),
        "managed_pdf_sha256_matches_staged": (comp.get("managed_pdf") or {}).get("sha256_matches_staged"),
    }


def _duplicate_skip_plan(item: dict[str, Any], route_type: str) -> dict[str, Any]:
    match = _best_match(item)
    evidence = _match_summary(match)
    return {
        "route_type": route_type,
        "matched_existing_db_id": evidence.get("existing_db_id") if evidence else None,
        "evidence_used": evidence,
        "future_live_actions_preview": [
            "confirm matched DB row is still present and unchanged",
            "log duplicate skip decision with evidence summary",
            "do not create a new DB row",
            "do not evaluate this staged PDF",
            "do not replace managed PDF unless a future better-PDF policy explicitly approves it",
            "optionally archive or mark staged duplicate only in a later live file-operation stage",
        ],
        "explicit_non_actions_now": [
            "no DB write",
            "no PDF move/copy/delete",
            "no ID change",
            "no evaluation",
            "no duplicate merge",
            "no PDF replacement",
        ],
        "sufficient_evidence": bool(evidence and route_type == "exact_duplicate_skip_candidate"),
        "log_entry_preview": {
            "event": "duplicate_skip_candidate",
            "staged_pdf": item.get("name"),
            "matched_existing_db_id": evidence.get("existing_db_id") if evidence else None,
            "decision_confidence": item.get("duplicate_version_resolution", {}).get("confidence"),
        },
    }


def _book_escalation_plan(item: dict[str, Any], route_type: str) -> dict[str, Any]:
    meta = item.get("metadata_resolution", {})
    doc = item.get("document_type_resolution", {})
    text_profile = item.get("text_comparison_profile", {})
    is_harris = item.get("name", "").startswith("Harris-")
    is_koren = item.get("name", "").startswith("Koren-")
    evidence_missing = [
        "high-confidence canonical title",
        "high-confidence author/editor list",
        "decision whether staged PDF is whole book/edited volume or contains target chapter(s)",
        "mapping from filename-derived article/chapter title to actual full-text location",
    ]
    if is_koren:
        evidence_missing.extend(["DOI/ISBN or stable bibliographic identifier", "publisher/title-page disambiguation beyond THE GUILFORD PRESS"])
    if is_harris:
        evidence_missing.append("confirmation whether Effect of pegylation on pharmaceuticals appears as a chapter inside the edited volume")

    machine_steps = [
        "extract and parse table of contents from the existing extracted text",
        "scan section/chapter headings for filename-derived title phrases and near-matches",
        "extract title-page, copyright-page, ISBN/DOI, editors, publisher, and year evidence",
        "build a chapter-candidate map without creating DB rows",
        "compare any identified chapter title against current DB metadata in read-only mode",
    ]
    if text_profile.get("chars", 0) > 500_000:
        machine_steps.append("run chunked full-text/TOC analysis because the extracted text is book-length")
    machine_steps.append("use a local or separately gated agent/LLM full-document read only if deterministic parsing cannot resolve title/chapter structure")

    return {
        "route_type": route_type,
        "likely_document_type": doc.get("resolved_document_type"),
        "current_metadata_preview": {
            "title": meta.get("title"),
            "authors_or_editors": meta.get("authors"),
            "year": meta.get("year"),
            "doi": meta.get("doi"),
            "source": meta.get("journal_or_source"),
            "confidence": meta.get("confidence"),
        },
        "evidence_available": doc.get("evidence", []),
        "evidence_missing": evidence_missing,
        "future_machine_steps_preview": machine_steps,
        "possible_future_outcomes": [
            "store as book/edited_volume if book-level metadata becomes high-confidence and relevant to corpus",
            "create child/chapter candidate route if the filename-derived target is found as a chapter",
            "link to an existing parent/child record if deterministic DB matching finds one",
            "skip or quarantine as non-target corpus item only after machine evidence supports that route",
        ],
        "ocr_or_text_needs": {
            "ocr_needed_now": False,
            "extra_text_work_needed": "TOC/chapter extraction and chunked book analysis, not OCR",
        },
        "human_review_last_resort_conditions": [
            "machine parsing cannot identify canonical title/editor/year after title-page, TOC, and chunk analysis",
            "machine comparison finds multiple plausible existing parent/child records with unresolved conflict",
            "the relevance of the book/chapter to the current psychophysiology/music/neuroscience corpus remains ambiguous after machine escalation",
        ],
        "explicit_non_actions_now": [
            "no book/edited-volume DB row",
            "no chapter/child DB row",
            "no PDF splitting",
            "no PDF move/copy/delete",
            "no final ID",
            "no paid API call",
        ],
    }


def _route_for_item(item: dict[str, Any]) -> dict[str, Any]:
    decision = item.get("duplicate_version_resolution", {}).get("decision")
    route_type = DUPLICATE_DECISION_TO_ROUTE.get(decision, "unresolved_machine_escalation")
    if route_type not in ALLOWED_ROUTE_TYPES:
        route_type = "unresolved_machine_escalation"

    if route_type in {"exact_duplicate_skip_candidate", "duplicate_better_pdf_candidate", "related_version_link_candidate"}:
        route_plan = _duplicate_skip_plan(item, route_type)
    elif route_type == "book_or_edited_volume_machine_escalation":
        route_plan = _book_escalation_plan(item, route_type)
    else:
        route_plan = {
            "route_type": route_type,
            "future_machine_steps_preview": [
                "review Stage E evidence",
                "perform additional deterministic metadata comparison",
                "escalate to human review only after machine options are exhausted",
            ],
            "explicit_non_actions_now": ["no DB write", "no PDF operation", "no final ID", "no API call"],
        }

    return {
        "name": item.get("name"),
        "path": item.get("path"),
        "stage_e_decision": decision,
        "stage_e_next_route": item.get("next_route"),
        "final_route_type": route_type,
        "metadata_snapshot": item.get("metadata_resolution", {}),
        "route_plan": route_plan,
        "requires_human_review_now": False,
    }


def _route_safety_check(route: dict[str, Any]) -> dict[str, Any]:
    route_type = route["final_route_type"]
    plan = route["route_plan"]
    checks = [
        {"check": "allowed_route_type", "ok": route_type in ALLOWED_ROUTE_TYPES},
        {"check": "no_db_write_required_now", "ok": True},
        {"check": "no_pdf_move_copy_required_now", "ok": True},
        {"check": "no_paid_api_required_now", "ok": True},
        {"check": "no_final_id_created_now", "ok": True},
        {"check": "report_only_and_reversible", "ok": True},
    ]
    if route_type == "exact_duplicate_skip_candidate":
        checks.append(
            {
                "check": "duplicate_skip_has_sufficient_evidence",
                "ok": bool(plan.get("matched_existing_db_id") and plan.get("sufficient_evidence")),
            }
        )
    if route_type == "book_or_edited_volume_machine_escalation":
        checks.append(
            {
                "check": "machine_escalation_not_treated_as_journal_article",
                "ok": plan.get("likely_document_type") in {"book", "edited_volume", "book_chapter"},
            }
        )
    return {
        "name": route["name"],
        "route_type": route_type,
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
    }


def run_final_route_plan_preview(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    stage_d_path = run_dir / "text_metadata_preview.json"
    stage_e_path = run_dir / "metadata_duplicate_resolution_preview.json"
    stage_d = _stage_artifact(run_dir, "text_metadata_preview.json")
    stage_e = _stage_artifact(run_dir, "metadata_duplicate_resolution_preview.json")
    expected_names = {p.get("name") for p in plan.get("pdf_plans", [])}
    stage_d_names = {i.get("name") for i in stage_d.get("items", [])}
    stage_e_names = {i.get("name") for i in stage_e.get("items", [])}
    routes = [_route_for_item(item) for item in stage_e.get("items", [])]
    safety = [_route_safety_check(route) for route in routes]
    summary = {
        "run_id": run_dir.name,
        "stage": "final-route-plan-preview",
        "scope": {
            "corpus": "current papers.db/staging psychophysiology-music-neuroscience corpus",
            "generalization": "not generalized to arbitrary fields/corpora in this version",
        },
        "source_artifacts": {
            "stage_d": str(stage_d_path),
            "stage_e": str(stage_e_path),
        },
        "artifact_consistency": {
            "stage_d_items_match_plan": stage_d_names == expected_names,
            "stage_e_items_match_plan": stage_e_names == expected_names,
            "plan_pdf_count": len(expected_names),
            "stage_d_item_count": len(stage_d_names),
            "stage_e_item_count": len(stage_e_names),
        },
        "routes": routes,
        "route_level_safety_checks": safety,
        "overall_safety_ok": all(s["ok"] for s in safety) and stage_d_names == expected_names and stage_e_names == expected_names,
        "later_stages_still_disabled": [
            "duplicate skip execution",
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
            "duplicate_merges": "none",
            "pdf_replacements": "none",
            "book_chapter_splitting_writes": "none",
        },
    }
    (run_dir / "final_route_plan_preview.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (run_dir / "stage_final_route_plan_preview_report.txt").write_text(
        render_final_route_plan_report(summary),
        encoding="utf-8",
    )
    return summary


def render_final_route_plan_report(summary: dict[str, Any]) -> str:
    lines = ["Stage F Final Route/Action Plan Preview (READ-ONLY)", "=" * 58]
    lines.append("Scope: current corpus only; no arbitrary-field/general-corpus design.")
    for route in summary["routes"]:
        lines.append(f"- {route['name']}")
        lines.append(f"  route: {route['final_route_type']}")
        meta = route.get("metadata_snapshot", {})
        lines.append(f"  metadata: {meta.get('title')} | {meta.get('year')} | {meta.get('doi')}")
        plan = route["route_plan"]
        if route["final_route_type"] == "exact_duplicate_skip_candidate":
            lines.append(f"  matched_existing_db_id: {plan.get('matched_existing_db_id')}")
            lines.append("  live preview: log duplicate skip; no new row; no evaluation; no PDF replacement without later policy.")
        elif route["final_route_type"] == "book_or_edited_volume_machine_escalation":
            lines.append(f"  likely_document_type: {plan.get('likely_document_type')}")
            lines.append(f"  missing: {plan.get('evidence_missing')}")
            lines.append("  live preview: machine TOC/title-page/chapter analysis before any human review.")
        lines.append(f"  human_review_now: {route['requires_human_review_now']}")
    lines.append("")
    lines.append(f"Overall safety: {'PASS' if summary['overall_safety_ok'] else 'FAIL'}")
    lines.append("No API calls, DB writes, PDF operations, duplicate skips, duplicate merges, PDF replacements, or ID changes were performed.")
    return "\n".join(lines) + "\n"
