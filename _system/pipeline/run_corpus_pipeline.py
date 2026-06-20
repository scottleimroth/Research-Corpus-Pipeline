#!/usr/bin/env python3
"""
One-command corpus pipeline entrypoint.

Current implementation phase:
- read-only pre-scan (default)
- Sonnet-only option report
- explicit plan mode that writes safe run artifacts only
- consolidated ingest workflow plan (--workflow ingest --plan-only)
- guarded --execute-stage for granular pilot debugging (Stages A?T)
- all-staging live ingest (--workflow-target all-staging --execute with gates)
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpus_modes import build_mode_options, recommend_mode, normalize_eval_mode
from corpus_cacioppo_completion_report import run_cacioppo_completion_report
from corpus_execute_guard import run_limited_execute_stage
from corpus_live_all_staging import run_all_staging_live, run_all_staging_plan_only
from corpus_live_ashley_one_shot import run_ashley_one_shot_live_workflow
from corpus_live_research_one_shot import run_research_one_shot_live_workflow
from corpus_live_duplicate_only import run_ingest_workflow_live_duplicate_only
from corpus_live_fileinfo_only import run_ingest_workflow_live_fileinfo_only
from corpus_live_parent_only import run_ingest_workflow_live_parent_only
from corpus_live_pdf_copy_only import run_ingest_workflow_live_pdf_copy_only
from corpus_live_staging_archive_only import run_ingest_workflow_live_staging_archive_only
from corpus_live_workflow_simulator import run_ingest_workflow_simulation
from corpus_live_workflow_simulator import run_parent_validation_audit
from corpus_orchestrator import build_future_live_command
from corpus_orchestrator import run_ingest_workflow_plan_only
from corpus_plan_report import build_pipeline_plan_report
from corpus_prescan import run_prescan
from corpus_run_planner import build_run_plan, write_run_plan
from corpus_standard_detector import detect_db_standard


def _print_prescan(report: dict, args) -> None:
    prescan = report["prescan"]
    standard = report["existing_db_standard"]
    options = report["mode_options"]
    recommendation = report["mode_recommended"]

    print("=" * 72)
    print("Corpus Pipeline Pre-Scan (Read-Only)")
    print("=" * 72)
    print(f"Staging PDFs found: {prescan['pdf_count']}")
    print(f"Likely text-available PDFs: {prescan['text_likely_count']}")
    print(f"Likely duplicate groups in staging: {prescan['likely_duplicate_groups']}")
    print(f"Existing DB detected: {prescan['existing_db_detected']}")
    print(f"Blank DB mode: {prescan['blank_db_mode']}")
    if prescan["existing_db_detected"]:
        print(f"Current DB standard: {standard['summary_label']}")

    print("\nA/B/C options before paid calls:")
    for key in ("A",):
        opt = options[key]
        print(f"  {key}. {opt['label']}")
        print(f"     Same-depth first pass for every PDF: {opt['same_depth_first_pass']}")
        print(f"     Est. cost: ${opt['cost_usd_est']} | Est. runtime: {opt['runtime_hours_est']}h")
        print(f"     Quality note: {opt['quality_note']}")
        if opt.get("quality_warning"):
            print(f"     WARNING: {opt['quality_warning']}")

    print(f"\nRecommended mode: {recommendation['mode']} ? {recommendation['reason']}")
    if args.mode:
        print(f"Selected mode: {args.mode} (plan only, not executed)")

    print("\nExecution plan (scaffold):")
    for i, stage in enumerate(report["planned_stages"], 1):
        print(f"  {i}. {stage}")

    print("\nSafety state: READ-ONLY")
    print("- No live API calls")
    print("- No papers.db writes")
    print("- No PDF moves/renames/deletes")


def _print_plan(plan: dict, plan_path: Path, report_path: Path) -> None:
    print("=" * 72)
    print("Corpus Pipeline Run Plan (Read-Only)")
    print("=" * 72)
    print(f"Selected mode: {plan['selected_mode']}")
    print(f"Selected PDFs: {plan['selected_pdf_count']} / {plan['total_staging_pdf_count']}")
    print(f"Estimated API calls: {plan['estimated_api_calls']}")
    print(f"Estimated cost: ${plan['estimated_cost_usd']}")
    print(f"Estimated runtime: {plan['estimated_runtime_hours']}h")
    print(f"Red-team policy: {plan['expected_red_team_policy']}")
    if plan.get("quality_warning"):
        print(f"\nWARNING: {plan['quality_warning']}")

    print("\nPlanned ingest/evaluation actions:")
    for pdf_plan in plan["pdf_plans"]:
        print(
            f"  {pdf_plan['index']}. {pdf_plan['name']} | "
            f"doc_type={pdf_plan.get('document_type_classification')} | "
            f"duplicate={pdf_plan['duplicate_check_status']} | "
            f"ingest={pdf_plan['proposed_ingest_action']} | "
            f"eval={pdf_plan['proposed_evaluation_action']['input_policy']}"
        )

    print("\nPreflight gates:")
    for gate in plan["preflight_gates"]:
        print(f"  - {gate['gate']}: {gate['status']} ({gate['detail']})")

    print(f"\nPlan written: {plan_path}")
    print(f"Report written: {report_path}")
    print("\nSafety state: READ-ONLY")
    print("- No live API calls")
    print("- No papers.db writes")
    print("- No PDF moves/renames/deletes")


def _print_live_command_preview(plan: dict, plan_path: Path, args) -> None:
    run_id = plan_path.parent.name
    mode = plan["selected_mode"]
    max_papers = plan["selected_pdf_count"]
    dry = (
        f"python pipeline/run_corpus_pipeline.py --plan --mode {mode} "
        f"--max-papers {max_papers} --show-live-command"
    )
    live = (
        f"python pipeline/run_corpus_pipeline.py --execute --mode {mode} "
        f"--max-papers {max_papers} --run-id {run_id} "
        "--allow-paid-api --allow-db-write --allow-pdf-copy"
    )
    print("\nFuture command preview (DO NOT RUN YET):")
    print(f"  Safe dry-run/plan command: {dry}")
    print(f"  Guarded live pilot command: {live}")
    print("\nFlag meanings:")
    print("  --execute: enter guarded live path")
    print("  --mode: Sonnet-only (A; B/C deprecated)")
    print("  --max-papers: pilot limit; must be <= 5")
    print("  --run-id: points to the approved run_plan.json folder under logs/runs")
    print("  --allow-paid-api: required for live Claude Sonnet evaluation")
    print("  --allow-db-write: separate DB-write gate")
    print("  --allow-pdf-copy: separate managed-PDF copy/move gate")
    if plan.get("quality_warning"):
        print(f"\nWARNING: {plan['quality_warning']}")
    print(f"\nEstimate: ${plan['estimated_cost_usd']} / {plan['estimated_runtime_hours']}h")


def _print_simulation_result(result: dict) -> None:
    simulation = result["simulation"]
    print("=" * 72)
    print("Corpus Ingest Live Workflow Simulation (READ-ONLY)")
    print("=" * 72)
    print(f"Run ID: {simulation['run_id']}")
    print(f"Pilot profile: {simulation.get('pilot_profile')}")
    if simulation.get("workflow_target"):
        print(f"Workflow target: {simulation.get('workflow_target')}")
    print(f"Simulation OK: {simulation.get('simulation_ok')}")
    print("\nSimulated phase order:")
    for phase in simulation.get("phases", []):
        line = f"  [{phase.get('phase_id')}] {phase.get('label')}"
        if phase.get("filename"):
            line += f" ? {phase['filename'][:60]}"
        print(line)
        if "validation_pass" in phase:
            print(
                "       "
                f"validation_pass={phase.get('validation_pass')} "
                f"ready={phase.get('ready_for_future_parent_record_pilot')} "
                f"one_shot_ready={phase.get('ready_for_one_shot_live_workflow')}"
            )
        if phase.get("full_lifecycle_phases_detected"):
            print(f"       lifecycle={phase.get('full_lifecycle_phases_detected')}")
        if phase.get("simulation_refusals"):
            refused = [
                f"{item['phase']}({item['required_gate']})"
                for item in phase["simulation_refusals"]
            ]
            print(f"       refused_side_effect_phases={refused}")
        if phase.get("matched_existing_db_id"):
            print(f"       duplicate ? {phase['matched_existing_db_id']}")

    print("\nStill stubbed for live:")
    for item in simulation.get("still_stubbed", []):
        print(f"  - {item}")

    if simulation.get("future_one_shot_live_command_do_not_run_yet"):
        print("\nFuture one-shot live command (DO NOT RUN YET):")
        print(f"  {simulation['future_one_shot_live_command_do_not_run_yet']}")
    else:
        print("\nRecommended first true live sequence (DO NOT RUN YET):")
        for step in simulation.get("recommended_first_live_sequence", []):
            print(f"  {step['step']}. {step['action']}")

    print("\nArtifacts:")
    print(f"  {result['simulation_json_path']}")
    print(f"  {result['simulation_report_path']}")

    print("\nSafety state: READ-ONLY SIMULATION")
    print("- No live API calls")
    print("- No papers.db writes")
    print("- No PDF moves/copies/archives/deletes")
    print("- No ID changes")


def _print_ashley_one_shot_result(result: dict) -> None:
    summary = result.get("summary") or {}
    print("=" * 72)
    print("Corpus Ingest Ashley/Timmers One-Shot Live Workflow")
    print("=" * 72)
    print(f"Status: {'PASS' if result.get('ok') else 'FAIL'}")
    print(result.get("message", ""))
    print(f"Processed only: {summary.get('processed_only')}")
    print(f"Parent ID: {summary.get('parent_id')}")
    print("\nPhases:")
    for phase in summary.get("phases", []):
        print(f"  - {phase.get('phase')}: {phase.get('status')}")
    print("\nActions:")
    print(f"  db={summary.get('db_action')}")
    print(f"  pdf={summary.get('pdf_action')}")
    print(f"  api/eval={summary.get('api_evaluation_action')}")
    print(f"  child/chapter={summary.get('child_chapter_records')}")
    if result.get("artifact_paths"):
        print("\nArtifacts:")
        print(f"  {result['artifact_paths']['json']}")
        print(f"  {result['artifact_paths']['report']}")
    failed = [c for c in summary.get("checks", []) if not c.get("ok")]
    if failed:
        print("\nFailed checks:")
        for check in failed:
            print(f"  - {check['check']}: {check['detail']}")


def _print_parent_validation_audit_result(result: dict) -> None:
    print("=" * 72)
    print("Parent Candidate Validation Audit (READ-ONLY)")
    print("=" * 72)
    print(f"Status: {'PASS' if result.get('ok') else 'FAIL'}")
    print(f"Target: {result.get('target')} ? {result.get('filename')}")
    print(f"Validation pass: {result.get('validation_pass')}")
    print(f"Ready for future parent workflow: {result.get('ready_for_future_parent_record_pilot')}")
    print(f"Ready for one-shot live workflow: {result.get('ready_for_one_shot_live_workflow')}")
    print(f"Validation score: {result.get('validation_score')}")
    print(f"ID preview: {result.get('candidate_parent_id_preview')}")
    if result.get("validation_reasons"):
        print("\nReasons:")
        for reason in result["validation_reasons"]:
            print(f"  - {reason}")
    if result.get("validation_blockers"):
        print("\nBlockers:")
        for blocker in result["validation_blockers"]:
            print(f"  - {blocker}")
    failed = [c for c in result.get("checks", []) if not c.get("ok")]
    if failed:
        print("\nFailed checks:")
        for check in failed:
            print(f"  - {check['check']}: {check['detail']}")
    print("\nSafety state: READ-ONLY AUDIT")
    print("- No live API calls")
    print("- No papers.db writes")
    print("- No PDF moves/copies/archives/deletes")
    print("- No evaluation")


def _print_live_duplicate_only_result(result: dict) -> None:
    print("=" * 72)
    print("Corpus Ingest Live Duplicate-Skip Log Only")
    print("=" * 72)
    print(f"Status: {'PASS' if result.get('ok') else 'FAIL'}")
    print(result.get("message", ""))
    if result.get("audit"):
        item = result["audit"]["item"]
        print(f"\nProcessed: {item['staged_filename']}")
        print(f"Decision: {item['decision']}")
        print(f"Matched DB ID: {item['matched_existing_db_id']}")
        print(f"DB action: {item['db_action']}")
        print(f"PDF action: {item['pdf_action']}")
        print(f"Evaluation: {item['evaluation_action']}")
        print(f"ID action: {item['id_action']}")
        print("\nParent candidates not processed:")
        for parent in result["audit"].get("parents_not_processed", []):
            print(f"  - {parent}")

    if result.get("artifact_paths"):
        print("\nArtifacts:")
        print(f"  {result['artifact_paths']['json']}")
        print(f"  {result['artifact_paths']['report']}")

    failed = [c for c in result.get("checks", []) if not c.get("ok")]
    if failed:
        print("\nFailed checks:")
        for check in failed:
            print(f"  - {check['check']}: {check['detail']}")

    print("\nSafety state: RUN-ARTIFACT LOG ONLY")
    print("- No live API calls")
    print("- No papers.db writes")
    print("- No PDF moves/copies/archives/deletes")
    print("- No ID changes")


def _print_live_parent_only_result(result: dict) -> None:
    summary = result.get("summary") or {}
    live_completed = summary.get("status") == "live_insert_completed"
    print("=" * 72)
    print("Corpus Ingest Cacioppo Parent-Only Live Insert" if live_completed else "Corpus Ingest Cacioppo Parent-Only Refusal Preview")
    print("=" * 72)
    print(f"Status: {'PASS' if result.get('ok') else 'FAIL'}")
    print(result.get("message", ""))
    print(f"\nProcessed only: {summary.get('processed_only')}")
    print(f"Ready for live insert: {summary.get('ready_for_live_insert')}")
    print(f"Proposed safe ID: {summary.get('proposed_safe_id')}")
    metadata = summary.get("validated_metadata") or {}
    print("\nValidated metadata:")
    print(f"  title={metadata.get('title')}")
    print(f"  authors={metadata.get('authors')}")
    print(f"  year={metadata.get('year')}")
    print(f"  publisher={metadata.get('publisher')}")
    print("\nActions in this run:")
    print(f"  db={summary.get('db_action')}")
    print(f"  pdf={summary.get('pdf_action')}")
    print(f"  api/eval={summary.get('api_evaluation_action')}")
    print(f"  id={summary.get('id_action')}")
    print("\nNot processed:")
    for item in summary.get("not_processed") or []:
        print(f"  - {item}")
    if result.get("artifact_paths"):
        print("\nArtifacts:")
        print(f"  {result['artifact_paths']['json']}")
        print(f"  {result['artifact_paths']['report']}")
    failed = [c for c in summary.get("checks", []) if not c.get("ok")]
    if failed:
        print("\nFailed checks:")
        for check in failed:
            print(f"  - {check['check']}: {check['detail']}")
    print("\nNext command (DO NOT RUN YET):")
    print(f"  {summary.get('next_live_insert_command_do_not_run_yet')}")
    print("\nSafety state: SINGLE-PARENT DB INSERT ONLY" if live_completed else "\nSafety state: REFUSAL/PREVIEW ONLY")
    print("- No live API calls")
    if live_completed:
        print(f"- papers.db write: exactly one new parent row ({summary.get('proposed_safe_id')})")
    else:
        print("- No papers.db writes")
    print("- No PDF moves/copies/archives/deletes")
    if live_completed:
        print(f"- ID action: assigned single new parent ID ({summary.get('proposed_safe_id')})")
    else:
        print("- No ID changes")


def _print_live_pdf_copy_only_result(result: dict) -> None:
    summary = result.get("summary") or {}
    live_completed = summary.get("status") == "live_copy_completed"
    print("=" * 72)
    print("Corpus Ingest Cacioppo Managed PDF Copy Live" if live_completed else "Corpus Ingest Cacioppo Managed PDF Copy Refusal Preview")
    print("=" * 72)
    print(f"Status: {'PASS' if result.get('ok') else 'FAIL'}")
    print(result.get("message", ""))
    print(f"\nProcessed only: {summary.get('processed_only')}")
    print(f"Source: {summary.get('source_staged_pdf')}")
    print(f"Source hash: {summary.get('source_staged_pdf_hash')}")
    print(f"Destination: {summary.get('managed_pdf_destination')}")
    print("\nActions in this run:")
    print(f"  db={summary.get('db_action')}")
    print(f"  pdf={summary.get('pdf_action')}")
    print(f"  api/eval={summary.get('api_evaluation_action')}")
    print("\nNot processed:")
    for item in summary.get("not_processed") or []:
        print(f"  - {item}")
    if result.get("artifact_paths"):
        print("\nArtifacts:")
        print(f"  {result['artifact_paths']['json']}")
        print(f"  {result['artifact_paths']['report']}")
    failed = [c for c in summary.get("checks", []) if not c.get("ok")]
    if failed:
        print("\nFailed checks:")
        for check in failed:
            print(f"  - {check['check']}: {check['detail']}")
    print("\nNext command (DO NOT RUN YET):")
    print(f"  {summary.get('next_live_copy_command_do_not_run_yet')}")
    print("\nSafety state: SINGLE PDF COPY ONLY" if live_completed else "\nSafety state: REFUSAL/PREVIEW ONLY")
    print("- No live API calls")
    print("- No papers.db writes")
    print("- No evaluation")
    if live_completed:
        print("- PDF action: copied one managed Cacioppo PDF")
    else:
        print("- No PDF copy/move/archive/delete")


def _print_live_fileinfo_only_result(result: dict) -> None:
    summary = result.get("summary") or {}
    live_completed = summary.get("status") == "live_fileinfo_attach_completed"
    print("=" * 72)
    print("Corpus Ingest Cacioppo File Info Attach Live" if live_completed else "Corpus Ingest Cacioppo File Info Attach Refusal Preview")
    print("=" * 72)
    print(f"Status: {'PASS' if result.get('ok') else 'FAIL'}")
    print(result.get("message", ""))
    print(f"\nProcessed only: {summary.get('processed_only')}")
    print("\nfile_info fields previewed:")
    for key, value in (summary.get("file_info_patch") or {}).items():
        print(f"  {key}={value}")
    print("\nActions in this run:")
    print(f"  db={summary.get('db_action')}")
    print(f"  pdf={summary.get('pdf_action')}")
    print(f"  api/eval={summary.get('api_evaluation_action')}")
    print(f"  backup={summary.get('backup')}")
    if result.get("artifact_paths"):
        print("\nArtifacts:")
        print(f"  {result['artifact_paths']['json']}")
        print(f"  {result['artifact_paths']['report']}")
    failed = [c for c in summary.get("checks", []) if not c.get("ok")]
    if failed:
        print("\nFailed checks:")
        for check in failed:
            print(f"  - {check['check']}: {check['detail']}")
    print("\nNext command (DO NOT RUN YET):")
    print(f"  {summary.get('next_live_fileinfo_command_do_not_run_yet')}")
    print("\nSafety state: FILE_INFO DB UPDATE ONLY" if live_completed else "\nSafety state: REFUSAL/PREVIEW ONLY")
    if live_completed:
        print("- papers.db write: updated Cacioppo file_info only")
    else:
        print("- No papers.db writes")
    print("- No live API calls")
    print("- No evaluation")
    print("- No PDF copy/move/archive/delete")


def _print_live_staging_archive_only_result(result: dict) -> None:
    summary = result.get("summary") or {}
    live_completed = summary.get("status") == "live_staging_archive_completed"
    print("=" * 72)
    print("Corpus Ingest Cacioppo Staging Archive Live" if live_completed else "Corpus Ingest Cacioppo Staging Archive Refusal Preview")
    print("=" * 72)
    print(f"Status: {'PASS' if result.get('ok') else 'FAIL'}")
    print(result.get("message", ""))
    print(f"\nProcessed only: {summary.get('processed_only')}")
    print(f"Staged source: {summary.get('staged_pdf_path')}")
    print(f"Archive destination: {summary.get('archive_destination')}")
    print(f"Managed PDF: {summary.get('managed_pdf_path')}")
    print("\nHash checks:")
    print(f"  staged={summary.get('staged_pdf_hash')}")
    print(f"  managed={summary.get('managed_pdf_hash')}")
    print("\nActions in this run:")
    print(f"  db={summary.get('db_action')}")
    print(f"  pdf={summary.get('pdf_action')}")
    print(f"  api/eval={summary.get('api_evaluation_action')}")
    if result.get("artifact_paths"):
        print("\nArtifacts:")
        print(f"  {result['artifact_paths']['json']}")
        print(f"  {result['artifact_paths']['report']}")
    failed = [c for c in summary.get("checks", []) if not c.get("ok")]
    if failed:
        print("\nFailed checks:")
        for check in failed:
            print(f"  - {check['check']}: {check['detail']}")
    print("\nNext command (DO NOT RUN YET):")
    print(f"  {summary.get('next_live_archive_command_do_not_run_yet')}")
    print("\nSafety state: STAGING ARCHIVE MOVE ONLY" if live_completed else "\nSafety state: REFUSAL/PREVIEW ONLY")
    print("- No papers.db writes")
    print("- No live API calls")
    print("- No evaluation")
    if live_completed:
        print("- PDF action: moved one Cacioppo staged PDF to processed archive")
    else:
        print("- No PDF copy/move/archive/delete")


def _print_cacioppo_completion_result(result: dict) -> None:
    report = result.get("report") or {}
    print("=" * 72)
    print("Cacioppo Final Completion Checkpoint (READ-ONLY)")
    print("=" * 72)
    print(f"Status: {'PASS' if result.get('ok') else 'FAIL'}")
    print(result.get("message", ""))
    print(f"\nManaged PDF: {report.get('managed_pdf_path')}")
    print(f"Managed hash: {report.get('managed_pdf_hash')}")
    print(f"Archive path: {report.get('archive_path')}")
    print(f"Archive hash: {report.get('archive_hash')}")
    print("\nVerification:")
    for check in report.get("checks", []):
        status = "PASS" if check.get("ok") else "FAIL"
        print(f"  {status}: {check.get('check')} - {check.get('detail')}")
    if result.get("artifact_paths"):
        print("\nArtifacts:")
        print(f"  {result['artifact_paths']['json']}")
        print(f"  {result['artifact_paths']['report']}")
    print("\nSafety state: READ-ONLY REPORT")
    print("- No papers.db writes")
    print("- No PDF moves/copies/archives/deletes")
    print("- No API calls")
    print("- No evaluation")


def _print_all_staging_plan_result(result: dict) -> None:
    workflow = result["workflow"]
    commands = result["future_commands"]
    print("=" * 72)
    print("All-Staging Ingest Plan (Read-Only)")
    print("=" * 72)
    print(f"Run ID: {result['plan_path'].parent.name}")
    print(f"Selected mode: {workflow['selected_mode']}")
    print(f"Staging root PDFs: {workflow.get('staging_root_pdf_count')}")
    print(f"Already in DB (skip): {workflow.get('already_in_db_count')}")
    print(f"New ingest candidates: {workflow.get('new_ingest_candidates')}")
    print(f"Evaluator: {workflow.get('evaluator')} (Sonnet-only)")
    print(f"Estimated cost (new only): ~${workflow.get('estimated_cost_usd_new_only')}")
    print("\nArtifacts:")
    for label, path in (workflow.get("artifact_paths") or {}).items():
        print(f"  {label}: {path}")
    print("\nLive command:")
    print(f"  {commands['live_command']}")
    print("\nSafety state: READ-ONLY")


def _print_workflow_result(result: dict) -> None:
    workflow = result["workflow"]
    commands = result["future_commands"]
    print("=" * 72)
    print("Corpus Ingest Workflow (Plan-Only)")
    print("=" * 72)
    print(f"Run ID: {result['plan_path'].parent.name}")
    print(f"Selected mode: {workflow['selected_mode']}")
    print(f"Selection profile: {workflow.get('selection_profile', 'narrow')}")
    print(f"Pilot profile: {workflow.get('pilot_profile', 'none')}")
    print(f"Selected PDFs: {workflow['selected_pdf_count']}")
    print("\nPlanned routes:")
    for item in workflow["items"]:
        route = item["route"]
        print(
            f"  {item['index']}. {item['name']}\n"
            f"     route={route['route_type']} | status={route['workflow_status']}\n"
            f"     db={route['planned_db_action']}\n"
            f"     pdf={route['planned_pdf_action']}\n"
            f"     eval={route['planned_evaluation_action']}"
        )
        if route.get("matched_existing_db_id"):
            print(f"     matched_db_id={route['matched_existing_db_id']}")
        if route.get("stage_e_resolution"):
            se = route["stage_e_resolution"]
            print(f"     stage_e={se.get('decision')} ({se.get('confidence')})")
        ps = item.get("pilot_suitability")
        if ps:
            fit = "suitable" if ps.get("suitable_for_first_orchestrated_live_test") else "unsuitable"
            print(f"     pilot_fit={fit} | parent_validation={ps.get('parent_validation_required_before_write')}")

    if workflow.get("pilot_selection_audit"):
        audit = workflow["pilot_selection_audit"]
        print("\nSafe-live selection audit:")
        print(f"  Parent candidates ranked: {len(audit.get('ranked_parent_candidates', []))}")
        if audit.get("duplicate_control_added"):
            print(f"  Duplicate control: {audit['duplicate_control_added']}")

    if workflow.get("duplicate_matches"):
        print("\nDuplicate matches (Stage E):")
        for dm in workflow["duplicate_matches"]:
            print(f"  {dm['staged_filename']} ? {dm.get('matched_existing_db_id')} ({dm.get('stage_e_decision')})")

    shortfall = workflow.get("selection_shortfall", {})
    if shortfall.get("shortfall"):
        print("\nSelection shortfall:")
        print(f"  {shortfall.get('explanation')}")
        for key, count in (shortfall.get("breakdown_counts") or {}).items():
            if count:
                print(f"  - {key}: {count}")

    safety = workflow["safety_summary"]
    print("\nSafety summary:")
    print(f"  DB writes would be required: {safety['db_writes_would_be_required']}")
    print(f"  PDF move-to-source would be required: {safety['pdf_move_to_source_would_be_required']}")
    print(f"  Successful processed archive created: {safety['successful_processed_archive_would_be_created']}")
    print(f"  Paid API would be required: {safety['paid_api_would_be_required']}")
    print(f"  Backup would be created: {safety['backup_would_be_created']}")
    if safety.get("machine_escalation_remaining"):
        print(f"  Machine escalation: {', '.join(safety['machine_escalation_remaining'])}")
    if safety.get("duplicate_log_only_items"):
        print(f"  Duplicate log-only: {', '.join(safety['duplicate_log_only_items'])}")

    print("\nArtifacts:")
    print(f"  {result['workflow_plan_path']}")
    print(f"  {result['workflow_report_path']}")
    print(f"  {result['plan_path']}")
    print(f"  {result['report_path']}")

    print("\nFuture commands (DO NOT RUN YET):")
    print(f"  Plan-only: {commands['plan_only_command']}")
    print(f"  Live ingest: {commands['future_live_command_do_not_run_yet']}")
    print(f"  With evaluation: {commands['future_live_with_evaluation_do_not_run_yet']}")
    print(f"  Note: {commands['note']}")

    print("\nGranular stages D?T remain available via --execute-stage for debugging.")
    print("\nSafety state: READ-ONLY")
    print("- No live API calls")
    print("- No papers.db writes")
    print("- No PDF moves/copies/archives/deletes")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run corpus pipeline (scaffold)")
    parser.add_argument("--prescan", action="store_true", help="Run pre-scan only (default)")
    parser.add_argument(
        "--workflow",
        choices=["ingest"],
        help="Run consolidated internal workflow (plan-only unless --execute with gates)",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Plan/report only; no DB writes or PDF operations (default for --workflow ingest)",
    )
    parser.add_argument(
        "--selection-profile",
        choices=["narrow", "broader"],
        default="narrow",
        help="PDF selection profile: narrow (journal/review/protocol) or broader (mixed batch)",
    )
    parser.add_argument(
        "--pilot-profile",
        choices=["none", "safe-live"],
        default="none",
        help="Curated pilot selection: safe-live for first orchestrated live workflow test batch",
    )
    parser.add_argument("--plan", action="store_true", help="Write a safe staged run plan")
    parser.add_argument("--mode", choices=["A", "recommended"], help="Sonnet-only evaluation mode (A)")
    parser.add_argument("--max-papers", type=int, help="Limit plan to first N staged PDFs")
    parser.add_argument("--run-id", type=str, default="", help="Existing run folder id under logs/runs")
    parser.add_argument("--plan-path", type=str, default="", help="Explicit path to run_plan.json")
    parser.add_argument("--show-live-command", action="store_true", help="Print future guarded live command preview")
    parser.add_argument("--allow-paid-api", action="store_true", help="Required future gate for paid API calls")
    parser.add_argument(
        "--allow-local-ollama",
        action="store_true",
        help="Allow local-only ingest mode (no Anthropic key); uses Ollama metadata tier and skips paid Sonnet evaluation",
    )
    parser.add_argument(
        "--allow-opus-resolution",
        action="store_true",
        help="After all-staging ingest, resolve review/technical-failure with Opus (requires --allow-paid-api)",
    )
    parser.add_argument("--allow-db-write", action="store_true", help="Required future gate for DB writes")
    parser.add_argument("--allow-pdf-copy", action="store_true", help="Required future gate for moving staged PDF to source-pdfs")
    parser.add_argument("--allow-staging-archive", action="store_true", help="Legacy gate; successful ingest should not create processed/archive PDF copies")
    parser.add_argument(
        "--execute-stage",
        choices=[
            "preflight",
            "preflight-backup",
            "preflight-backup-plancheck",
            "text-metadata-preview",
            "metadata-duplicate-resolution-preview",
            "final-route-plan-preview",
            "book-toc-chapter-preview",
            "final-pilot-execution-plan",
            "live-duplicate-skip-log-only",
            "koren-titlepage-editor-preview",
            "koren-parent-validation-preview",
            "live-koren-parent-record-only",
            "live-koren-pdf-copy-only",
            "live-koren-fileinfo-attach-only",
            "live-koren-staging-archive-only",
            "harris-edited-volume-validation-preview",
            "live-harris-parent-record-only",
            "live-harris-pdf-copy-only",
            "live-harris-fileinfo-attach-only",
            "live-harris-staging-archive-only",
            "final-pilot-report-preview",
        ],
        help="Limited live-pilot stages currently allowed; all later stages remain disabled",
    )
    parser.add_argument("--staging-dir", type=str, default="")
    parser.add_argument("--db-path", type=str, default="")
    parser.add_argument("--managed-pdf-dir", type=str, default="")
    parser.add_argument("--review-dir", type=str, default="")
    parser.add_argument("--backups-dir", type=str, default="")
    parser.add_argument("--logs-dir", type=str, default="")
    parser.add_argument("--runs-dir", type=str, default="")
    parser.add_argument("--blank-corpus", action="store_true")
    parser.add_argument("--existing-corpus", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Live or simulation workflow entry")
    parser.add_argument(
        "--simulate-live-workflow",
        action="store_true",
        help="Simulate orchestrated live ingest workflow phase order with no side effects",
    )
    parser.add_argument(
        "--workflow-target",
        choices=["Ashley", "Hallam", "LanguageProfile2024", "all-staging"],
        default="",
        help="Workflow target: all-staging = every root-level staging PDF; LanguageProfile2024 = legacy single-paper pilot",
    )
    parser.add_argument(
        "--audit-parent-target",
        choices=["Hallam", "Juslin", "Pikovsky"],
        default="",
        help="Run a read-only generalized parent validation audit for a planned target",
    )
    parser.add_argument(
        "--live-duplicate-only",
        action="store_true",
        help="Accept only the curated Levitin duplicate-skip decision into run artifacts",
    )
    parser.add_argument(
        "--live-parent-only",
        choices=["Cacioppo"],
        default="",
        help="Run or preview one generalized parent-record candidate only",
    )
    parser.add_argument(
        "--live-pdf-copy-only",
        choices=["Cacioppo"],
        default="",
        help="Run or preview one managed PDF copy only",
    )
    parser.add_argument(
        "--live-fileinfo-only",
        choices=["Cacioppo"],
        default="",
        help="Run or preview one file_info attachment only",
    )
    parser.add_argument(
        "--live-staging-archive-only",
        choices=["Cacioppo"],
        default="",
        help="Run or preview one staging archive move only",
    )
    parser.add_argument(
        "--cacioppo-completion-report",
        action="store_true",
        help="Run read-only final Cacioppo completion checkpoint",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable pre-scan JSON")
    args = parser.parse_args()

    if args.audit_parent_target:
        if not args.run_id:
            print("--audit-parent-target requires --run-id pointing to an existing plan-only run folder")
            sys.exit(1)
        result = run_parent_validation_audit(args)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
            return
        _print_parent_validation_audit_result(result)
        sys.exit(0 if result.get("ok") else 1)

    if args.cacioppo_completion_report:
        result = run_cacioppo_completion_report(args)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
            return
        _print_cacioppo_completion_result(result)
        sys.exit(0 if result.get("ok") else 1)

    if args.workflow == "ingest" and args.execute:
        if getattr(args, "workflow_target", "") == "all-staging" and not getattr(args, "simulate_live_workflow", False):
            result = run_all_staging_live(args)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
                return
            summary = result.get("summary") or {}
            import intake_rejected

            intake_rejected.print_intake_summary_console(summary, artifacts=result.get("artifacts") or {})
            sys.exit(0 if result.get("ok") else 1)

        if getattr(args, "workflow_target", "") == "LanguageProfile2024" and not getattr(args, "simulate_live_workflow", False):
            result = run_research_one_shot_live_workflow(args)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
                return
            summary = result.get("summary") or {}
            print(f"\nLive Sonnet research pilot: {summary.get('status')}")
            if summary.get("paper_id"):
                print(f"  Paper ID: {summary['paper_id']}")
                print(f"  Title: {summary.get('title')}")
                print(f"  Rating: {summary.get('classification_rating')}")
                print(f"  Evaluation complete: {summary.get('evaluation_complete')}")
            if summary.get("error"):
                print(f"  Error: {summary['error']}")
            arts = result.get("artifacts") or {}
            if arts.get("json"):
                print(f"  Report: {arts['json']}")
            sys.exit(0 if result.get("ok") else 1)

        if getattr(args, "workflow_target", "") == "Ashley" and not getattr(args, "simulate_live_workflow", False):
            result = run_ashley_one_shot_live_workflow(args)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
                return
            _print_ashley_one_shot_result(result)
            sys.exit(0 if result.get("ok") else 1)

        if getattr(args, "live_staging_archive_only", ""):
            result = run_ingest_workflow_live_staging_archive_only(args)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
                return
            _print_live_staging_archive_only_result(result)
            sys.exit(0 if result.get("ok") else 1)

        if getattr(args, "live_fileinfo_only", ""):
            result = run_ingest_workflow_live_fileinfo_only(args)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
                return
            _print_live_fileinfo_only_result(result)
            sys.exit(0 if result.get("ok") else 1)

        if getattr(args, "live_pdf_copy_only", ""):
            result = run_ingest_workflow_live_pdf_copy_only(args)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
                return
            _print_live_pdf_copy_only_result(result)
            sys.exit(0 if result.get("ok") else 1)

        if getattr(args, "live_parent_only", ""):
            result = run_ingest_workflow_live_parent_only(args)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
                return
            _print_live_parent_only_result(result)
            sys.exit(0 if result.get("ok") else 1)

        if getattr(args, "live_duplicate_only", False):
            result = run_ingest_workflow_live_duplicate_only(args)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
                return
            _print_live_duplicate_only_result(result)
            sys.exit(0 if result.get("ok") else 1)

        if getattr(args, "simulate_live_workflow", False):
            if not args.run_id:
                print("--simulate-live-workflow requires --run-id pointing to an existing plan-only run folder")
                sys.exit(1)
            if not args.mode or args.mode == "recommended":
                print("--simulate-live-workflow requires explicit --mode A")
                sys.exit(1)
            try:
                normalize_eval_mode(args.mode)
            except ValueError as e:
                print(str(e))
                sys.exit(1)
            for gate in ("allow_paid_api", "allow_db_write", "allow_pdf_copy", "allow_staging_archive"):
                if getattr(args, gate, False):
                    print(f"Simulation refuses side-effect gate --{gate.replace('_', '-')}")
                    sys.exit(1)
            result = run_ingest_workflow_simulation(args)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
                return
            _print_simulation_result(result)
            sys.exit(0 if result.get("simulation_ok") else 1)

        run_id = args.run_id or "<run-id>"
        mode = normalize_eval_mode(args.mode if args.mode in {"A", "recommended"} else "A")
        max_papers = args.max_papers or 5
        preview = build_future_live_command(run_id=run_id, mode=mode, max_papers=max_papers)
        print("Corpus Ingest Workflow ? live execution is not enabled in this consolidation step.")
        print("\nFuture live workflow command (DO NOT RUN YET):")
        print(f"  {preview['future_live_command_do_not_run_yet']}")
        if not (args.allow_db_write and args.allow_pdf_copy):
            print("\nThat future command will also require:")
            print("  --allow-db-write --allow-pdf-copy")
        print(f"\n{preview['note']}")
        print("\nUse --workflow ingest --plan-only for safe planning.")
        print("Use --workflow ingest --execute --simulate-live-workflow --run-id <id> for phase simulation.")
        print("Use --execute-stage <stage> for granular pilot debugging.")
        sys.exit(1)

    if args.execute and not args.workflow:
        code, output = run_limited_execute_stage(args)
        print(output)
        sys.exit(code)

    prescan = run_prescan(
        staging_dir=args.staging_dir,
        db_path=args.db_path,
        blank_corpus=args.blank_corpus,
    )
    standard = detect_db_standard(db_path=args.db_path)
    options = build_mode_options(prescan, standard)
    recommendation = recommend_mode(options, standard)

    report = build_pipeline_plan_report(
        prescan=prescan,
        standard=standard,
        options=options,
        recommended=recommendation,
        selected_mode=None if args.mode == "recommended" else args.mode,
        args=args,
    )

    if args.workflow == "ingest":
        plan_only = args.plan_only or not args.execute
        if not args.mode or args.mode == "recommended":
            print("--workflow ingest requires explicit --mode A (DeepSeek-first evaluation)")
            sys.exit(1)
        try:
            normalize_eval_mode(args.mode)
        except ValueError as e:
            print(str(e))
            sys.exit(1)

        if getattr(args, "workflow_target", "") == "all-staging":
            if plan_only:
                result = run_all_staging_plan_only(
                    prescan=prescan,
                    standard=standard,
                    recommendation=recommendation,
                    requested_mode=args.mode,
                    args=args,
                )
            else:
                print("Live all-staging ingest requires --execute with --allow-db-write --allow-pdf-copy --allow-paid-api")
                sys.exit(1)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
                return
            if plan_only:
                _print_all_staging_plan_result(result)
            return

        if not plan_only:
            print("Live workflow requires explicit gates; use --workflow-target all-staging for operational ingest.")
            sys.exit(1)
        result = run_ingest_workflow_plan_only(
            prescan=prescan,
            standard=standard,
            recommendation=recommendation,
            requested_mode=args.mode,
            max_papers=args.max_papers,
            args=args,
        )
        if args.json:
            print(json.dumps(result, indent=2, default=str))
            return
        _print_workflow_result(result)
        return

    if args.plan:
        run_plan = build_run_plan(
            prescan=prescan,
            standard=standard,
            recommendation=recommendation,
            requested_mode=args.mode,
            max_papers=args.max_papers,
            args=args,
        )
        plan_path, report_path = write_run_plan(run_plan, runs_dir=args.runs_dir)
        if args.json:
            print(json.dumps({"plan_path": str(plan_path), "report_path": str(report_path), "plan": run_plan}, indent=2))
            return
        _print_plan(run_plan, plan_path, report_path)
        if args.show_live_command:
            _print_live_command_preview(run_plan, plan_path, args)
        return

    if args.json:
        print(json.dumps(report, indent=2))
        return

    _print_prescan(report, args)


if __name__ == "__main__":
    main()
