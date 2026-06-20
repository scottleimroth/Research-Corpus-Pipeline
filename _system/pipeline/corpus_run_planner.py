"""Build staged corpus pipeline run plans (safe/read-only actions only)."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from corpus_ingest_scaffold import MACHINE_FIRST_EXCEPTION_POLICIES
from corpus_modes import build_mode_options, normalize_eval_mode
from corpus_pilot_selection import PILOT_PROFILES, resolve_pilot_profile, select_safe_live_pdfs


MODE_PROFILES = {
    "A": {
        "first_pass": "Claude Sonnet evaluation (only supported final evaluator)",
        "schema": "full schema v1 (current DB standard)",
        "red_team": "Sonnet red-team pass for research papers; skip for non-ratable",
        "routing": "Sonnet-only; live evaluation requires --allow-paid-api",
    },
}


def resolve_mode(requested: str | None, recommendation: dict[str, str]) -> str:
    if not requested or requested == "recommended":
        return recommendation["mode"]
    return normalize_eval_mode(requested)


SELECTION_PROFILES: dict[str, dict[str, Any]] = {
    "narrow": {
        "label": "Journal/review/protocol only",
        "allowed_types": {"journal_article", "review_article", "protocol"},
        "exclude_supplement_standalone": True,
        "mixed_batch": False,
        "description": "journal_article, review_article, protocol; not already_in_db; text_likely",
    },
    "broader": {
        "label": "Mixed batch with machine escalation",
        "allowed_types": {"journal_article", "review_article", "protocol", "book", "edited_volume", "other_or_unknown"},
        "exclude_supplement_standalone": True,
        "mixed_batch": True,
        "description": (
            "journal/review/protocol + book + edited_volume + other_or_unknown; "
            "not already_in_db; text_likely; supplements/chapters excluded unless linked"
        ),
    },
}

BROADER_MIX_BUCKETS = ("book", "edited_volume", "other_or_unknown", "article")


def resolve_selection_profile(name: str | None) -> str:
    profile = (name or "narrow").strip().lower()
    if profile not in SELECTION_PROFILES:
        return "narrow"
    return profile


def _pdf_eligible_for_profile(pdf: dict[str, Any], profile_name: str) -> bool:
    profile = SELECTION_PROFILES[profile_name]
    if pdf.get("already_in_db"):
        return False
    if not pdf.get("text_likely"):
        return False
    doc_type = pdf.get("document_type_classification", "other_or_unknown")
    if doc_type not in profile["allowed_types"]:
        return False
    if profile["exclude_supplement_standalone"] and doc_type in {"supplement", "book_chapter", "thesis", "poster_or_abstract"}:
        return False
    return True


def _bucket_for_broader_mix(pdf: dict[str, Any]) -> str:
    doc_type = pdf.get("document_type_classification", "other_or_unknown")
    if doc_type == "book":
        return "book"
    if doc_type == "edited_volume":
        return "edited_volume"
    if doc_type == "other_or_unknown":
        return "other_or_unknown"
    return "article"


def _selected_pdfs_broader(prescan: dict[str, Any], max_papers: int) -> list[dict[str, Any]]:
    """Select a mixed batch: round-robin across book, edited_volume, other_or_unknown, article."""
    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k in BROADER_MIX_BUCKETS}
    for pdf in prescan.get("pdfs", []):
        if not _pdf_eligible_for_profile(pdf, "broader"):
            continue
        buckets[_bucket_for_broader_mix(pdf)].append(pdf)

    selected: list[dict[str, Any]] = []
    indices = {k: 0 for k in BROADER_MIX_BUCKETS}
    while len(selected) < max_papers:
        added = False
        for bucket in BROADER_MIX_BUCKETS:
            idx = indices[bucket]
            if idx < len(buckets[bucket]):
                selected.append(buckets[bucket][idx])
                indices[bucket] = idx + 1
                added = True
                if len(selected) >= max_papers:
                    break
        if not added:
            break
    return selected


def _selected_pdfs(
    prescan: dict[str, Any],
    max_papers: int | None,
    selection_profile: str = "narrow",
    pilot_profile: str = "none",
) -> list[dict[str, Any]]:
    pdfs = prescan.get("pdfs", [])
    profile_name = resolve_selection_profile(selection_profile)
    pilot_name = resolve_pilot_profile(pilot_profile)

    if pilot_name == "safe-live":
        if profile_name != "broader":
            profile_name = "broader"
        selected, _audit = select_safe_live_pdfs(
            prescan,
            max_papers,
            pdf_eligible=_pdf_eligible_for_profile,
        )
        return selected

    if max_papers is None:
        return [p for p in pdfs if _pdf_eligible_for_profile(p, profile_name)]

    if profile_name == "broader":
        return _selected_pdfs_broader(prescan, max_papers)

    selected = []
    for pdf in pdfs:
        if not _pdf_eligible_for_profile(pdf, profile_name):
            continue
        selected.append(pdf)
        if len(selected) >= max_papers:
            break
    return selected


def _excluded_pdfs(
    prescan: dict[str, Any],
    selected: list[dict[str, Any]],
    selection_profile: str = "narrow",
) -> list[dict[str, Any]]:
    profile_name = resolve_selection_profile(selection_profile)
    profile = SELECTION_PROFILES[profile_name]
    selected_names = {p["name"] for p in selected}
    excluded = []
    for pdf in prescan.get("pdfs", []):
        if pdf["name"] in selected_names:
            continue
        reason = []
        if pdf.get("already_in_db"):
            reason.append("already_in_db:" + ",".join(pdf.get("already_in_db_signals", [])))
        doc_type = pdf.get("document_type_classification", "other_or_unknown")
        if doc_type not in profile["allowed_types"]:
            reason.append(f"document_type={doc_type}")
        elif profile["exclude_supplement_standalone"] and doc_type in {"supplement", "book_chapter", "thesis", "poster_or_abstract"}:
            reason.append(f"document_type={doc_type}")
        if not pdf.get("text_likely"):
            reason.append("text_not_likely")
        if reason:
            excluded.append({"name": pdf["name"], "reason": "; ".join(reason)})
    return excluded[:25]


def _duplicate_status(pdf: dict[str, Any], duplicate_examples: list[list[str]]) -> str:
    for group in duplicate_examples:
        if pdf["name"] in group:
            return "hash_duplicate_in_staging"
    return "no_hash_duplicate_in_staging_prescan"


def _planned_destination(pdf: dict[str, Any], managed_pdf_dir: str = "") -> str:
    dest_dir = Path(managed_pdf_dir) if managed_pdf_dir else config.SOURCE_PDFS
    return str(dest_dir / "<canonical_filename_after_metadata_validation>.pdf")


def _planned_pdf_action(pdf: dict[str, Any]) -> str:
    if pdf.get("size_bytes", 0) < 1_000:
        return "escalate_invalid_or_too_small_pdf"
    return "plan_ingest_after_validation_no_move_yet"


def _planned_eval_action(mode: str) -> dict[str, str]:
    profile = MODE_PROFILES[mode]
    return {
        "same_depth_first_pass": "required_for_every_pdf",
        "input_policy": "full_text_when_feasible_else_quality_preserving_evidence_pack",
        "blind_truncation": "forbidden",
        "batch_api": "planned_not_submitted",
        "prompt_caching": "planned_for_rubric_system_prompts",
        "schema": profile["schema"],
        "model_routing": profile["routing"],
        "red_team_policy": profile["red_team"],
    }


def _planned_escalation_route(pdf: dict[str, Any]) -> list[str]:
    route = ["normal_automated_processing"]
    if not pdf.get("text_likely"):
        route.extend(MACHINE_FIRST_EXCEPTION_POLICIES["ocr_needed"])
    if _planned_pdf_action(pdf).startswith("escalate"):
        route.append("human_review_only_if_machine_checks_fail")
    return route


def build_preflight_gates(
    *,
    prescan: dict[str, Any],
    selected_mode: str,
    max_papers: int | None,
    args,
) -> list[dict[str, Any]]:
    paid_requested = False  # execute path is disabled in this scaffold
    db_exists = prescan["existing_db_detected"]
    blank = prescan["blank_db_mode"]
    gates = [
        {
            "gate": "backup_before_write",
            "status": "future_required",
            "detail": "A fresh backup is required before any DB write or PDF move.",
        },
        {
            "gate": "db_path",
            "status": "pass" if db_exists or blank else "fail",
            "detail": "Existing DB found or blank corpus mode requested.",
        },
        {
            "gate": "staging_path",
            "status": "pass" if Path(prescan["staging_dir"]).exists() else "fail",
            "detail": prescan["staging_dir"],
        },
        {
            "gate": "managed_pdf_folder",
            "status": "exists" if Path(args.managed_pdf_dir or config.SOURCE_PDFS).exists() else "planned_create",
            "detail": args.managed_pdf_dir or str(config.SOURCE_PDFS),
        },
        {
            "gate": "review_folder",
            "status": "exists" if Path(args.review_dir or config.REVIEW_DIR).exists() else "planned_create",
            "detail": args.review_dir or str(config.REVIEW_DIR),
        },
        {
            "gate": "run_ledger_folder",
            "status": "exists" if Path(args.runs_dir or config.RUNS_DIR).exists() else "planned_create",
            "detail": args.runs_dir or str(config.RUNS_DIR),
        },
        {
            "gate": "api_key_for_paid_evaluation",
            "status": "not_required_for_plan" if not paid_requested else ("pass" if os.environ.get("ANTHROPIC_API_KEY") else "fail"),
            "detail": "Paid evaluation is not enabled in this scaffold.",
        },
        {
            "gate": "selected_mode_confirmed",
            "status": "pass",
            "detail": selected_mode,
        },
        {
            "gate": "max_papers_limit",
            "status": "pass" if max_papers is not None else "not_limited",
            "detail": max_papers if max_papers is not None else "No plan limit supplied.",
        },
        {
            "gate": "overlapping_pipeline_process",
            "status": "not_detected_by_lock",
            "detail": "Future live mode will create a run lock; this plan mode does not.",
        },
    ]
    return gates


def build_run_plan(
    *,
    prescan: dict[str, Any],
    standard: dict[str, Any],
    recommendation: dict[str, str],
    requested_mode: str | None,
    max_papers: int | None,
    args,
) -> dict[str, Any]:
    selected_mode = resolve_mode(requested_mode, recommendation)
    selection_profile = resolve_selection_profile(getattr(args, "selection_profile", None))
    pilot_profile = resolve_pilot_profile(getattr(args, "pilot_profile", None))
    pilot_audit = None
    if getattr(args, "workflow_target", "") == "all-staging":
        selected = list(prescan.get("pdfs", []))
    elif pilot_profile == "safe-live":
        if selection_profile != "broader":
            selection_profile = "broader"
        selected, pilot_audit = select_safe_live_pdfs(
            prescan,
            max_papers,
            pdf_eligible=_pdf_eligible_for_profile,
        )
    else:
        selected = _selected_pdfs(
            prescan,
            max_papers,
            selection_profile=selection_profile,
            pilot_profile=pilot_profile,
        )
    scoped_prescan = dict(prescan)
    scoped_prescan["pdf_count"] = len(selected)
    options = build_mode_options(scoped_prescan, standard)
    selected_option = options[selected_mode]

    pdf_plans = []
    duplicate_examples = prescan.get("likely_duplicate_examples", [])
    for idx, pdf in enumerate(selected, 1):
        pdf_plans.append(
            {
                "index": idx,
                "name": pdf["name"],
                "path": pdf["path"],
                "sha256": pdf["sha256"],
                "size_bytes": pdf["size_bytes"],
                "document_type_classification": pdf.get("document_type_classification", "other_or_unknown"),
                "document_type_reasons": pdf.get("document_type_reasons", []),
                "already_in_db": pdf.get("already_in_db", False),
                "already_in_db_signals": pdf.get("already_in_db_signals", []),
                "text_extraction_check": "planned_pdftotext_then_ocr_if_needed",
                "text_likely": pdf["text_likely"],
                "duplicate_check_status": _duplicate_status(pdf, duplicate_examples),
                "metadata_route": "doi/title-page/pdf-metadata/crossref/full-text-agent-if-needed",
                "safe_id_route": "generate_candidate_then_validate_reject_synthetic_or_unsafe",
                "proposed_ingest_action": _planned_pdf_action(pdf),
                "proposed_managed_pdf_destination": _planned_destination(pdf, args.managed_pdf_dir),
                "proposed_db_action": "validate_candidate_row_and_schema_then_insert_after_backup_and_approval",
                "proposed_evaluation_action": _planned_eval_action(selected_mode),
                "review_escalation_route": _planned_escalation_route(pdf),
            }
        )

    warning = ""
    if selected_option.get("quality_warning"):
        warning = (
            "Evaluation mode is Sonnet-only (A). Modes B/C are deprecated and rejected. "
            f"Existing DB standard appears to be {recommendation['mode']}/high red-team coverage."
        )

    return {
        "created_at": datetime.now().isoformat(),
        "read_only": True,
        "selected_mode": selected_mode,
        "requested_mode": requested_mode or "recommended",
        "recommended_mode": recommendation,
        "quality_warning": warning,
        "selected_pdf_count": len(selected),
        "selection_profile": selection_profile,
        "selection_profile_label": SELECTION_PROFILES[selection_profile]["label"],
        "pilot_profile": pilot_profile,
        "pilot_profile_label": PILOT_PROFILES.get(pilot_profile, {}).get("label", ""),
        "pilot_selection_audit": pilot_audit,
        "total_staging_pdf_count": prescan["pdf_count"],
        "estimated_api_calls": selected_option["est"]["api_calls"],
        "estimated_cost_usd": selected_option["cost_usd_est"],
        "estimated_runtime_hours": selected_option["runtime_hours_est"],
        "expected_red_team_policy": MODE_PROFILES[selected_mode]["red_team"],
        "mode_option": selected_option,
        "excluded_pdf_examples": _excluded_pdfs(prescan, selected, selection_profile=selection_profile),
        "preflight_gates": build_preflight_gates(
            prescan=prescan,
            selected_mode=selected_mode,
            max_papers=max_papers,
            args=args,
        ),
        "pdf_plans": pdf_plans,
        "safety": {
            "api_calls": "none",
            "db_writes": "none",
            "pdf_moves": "none",
            "id_changes": "none",
        },
    }


def write_run_plan(plan: dict[str, Any], *, runs_dir: str = "") -> tuple[Path, Path]:
    base = Path(runs_dir) if runs_dir else config.RUNS_DIR
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = base / stamp
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = base / f"{stamp}-{suffix:02d}"
    run_dir.mkdir(parents=True, exist_ok=False)

    plan_path = run_dir / "run_plan.json"
    report_path = run_dir / "run_report.txt"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    report_path.write_text(render_run_report(plan), encoding="utf-8")
    return plan_path, report_path


def render_run_report(plan: dict[str, Any]) -> str:
    lines = [
        "Corpus Pipeline Run Plan (READ-ONLY)",
        "=" * 40,
        f"Selected mode: {plan['selected_mode']}",
        f"Selection profile: {plan.get('selection_profile', 'narrow')} ({plan.get('selection_profile_label', '')})",
        f"Pilot profile: {plan.get('pilot_profile', 'none')} ({plan.get('pilot_profile_label', '')})",
        f"Selected PDFs: {plan['selected_pdf_count']} / {plan['total_staging_pdf_count']}",
        f"Estimated API calls: {plan['estimated_api_calls']}",
        f"Estimated cost: ${plan['estimated_cost_usd']}",
        f"Estimated runtime: {plan['estimated_runtime_hours']}h",
        f"Red-team policy: {plan['expected_red_team_policy']}",
    ]
    if plan.get("quality_warning"):
        lines.extend(["", "WARNING:", plan["quality_warning"]])

    lines.append("")
    lines.append("Preflight gates:")
    for gate in plan["preflight_gates"]:
        lines.append(f"- {gate['gate']}: {gate['status']} ({gate['detail']})")

    lines.append("")
    lines.append("Selected PDFs:")
    for p in plan["pdf_plans"]:
        lines.append(
            f"- {p['index']}. {p['name']} | duplicate={p['duplicate_check_status']} | "
            f"doc_type={p.get('document_type_classification')} | ingest={p['proposed_ingest_action']}"
        )

    if plan.get("excluded_pdf_examples"):
        lines.append("")
        lines.append("Excluded examples:")
        for e in plan["excluded_pdf_examples"][:10]:
            lines.append(f"- {e['name']} | {e['reason']}")

    lines.extend(
        [
            "",
            "Safety:",
            "- No live API calls",
            "- No papers.db writes",
            "- No PDF moves/renames/deletes",
            "- No ID changes",
        ]
    )
    return "\n".join(lines) + "\n"
