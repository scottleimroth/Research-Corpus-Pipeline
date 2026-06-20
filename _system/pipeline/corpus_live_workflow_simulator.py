"""Orchestrated live workflow simulation (no side effects).

Simulates phase order for curated safe-live pilots using generalized parent stages.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from corpus_execute_guard import load_run_plan, render_execute_preflight, resolve_plan_path
from corpus_general_parent_stages import (
    run_general_book_toc_chapter_preview_item,
    run_general_parent_validation_preview,
)

PARENT_PRIORITY = [
    "(Cambridge Handbooks in Psychology) John T. Cacioppo (editor.pdf",
    "(Oxford Handbooks) Susan Hallam, Ian Cross, Michael Thaut (e.pdf",
    "Ashley, Richard(Editor)_Timmers, Renee(Contributor) - The Ro.pdf",
]

DUPLICATE_CONTROL = "Levitin-The Psychology of Music- Rhythm and Movement-2017-An.pdf"
WORKFLOW_TARGET_ALIASES = {
    "Hallam": "(Oxford Handbooks) Susan Hallam, Ian Cross, Michael Thaut (e.pdf",
    "Juslin": "Professor Patrik N. Juslin (editor), Professor John Sloboda .pdf",
    "Pikovsky": "(Cambridge Nonlinear Science Series) Arkady Pikovsky, Michae.pdf",
    "Ashley": "Ashley, Richard(Editor)_Timmers, Renee(Contributor) - The Ro.pdf",
}

FUTURE_GATES = {
    "duplicate_skip_log_only": {
        "required": ["run lock", "workflow_plan.json + run_plan.json validation", "artifact hash checks"],
        "not_required": ["--allow-db-write", "--allow-pdf-copy", "--allow-staging-archive", "--allow-paid-api"],
        "note": "Stage I persists duplicate-skip audit in run artifacts only unless future duplicate-archive policy.",
    },
    "parent_record_create": {"required": ["--allow-db-write", "DB backup", "generalized validation_pass"]},
    "source_pdf_move": {"required": ["--allow-pdf-copy", "parent record exists"], "note": "move staged PDF to source-pdfs; do not keep processed/archive copy"},
    "staging_cleanup_check": {"required": ["source-pdfs hash verified", "original staging path absent"], "side_effect": "none"},
    "evaluation": {
        "required": ["--allow-paid-api"],
        "status": "disabled_deferred_for_book_edited_volume_parent_ingest_pilot",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": ok, "detail": detail}


def _target_filename(args) -> str | None:
    target = getattr(args, "workflow_target", "") or ""
    if not target:
        return None
    return WORKFLOW_TARGET_ALIASES.get(target, target)


def validate_simulation_preconditions(args) -> tuple[bool, list[dict[str, Any]], Path | None, dict[str, Any] | None, dict[str, Any] | None]:
    checks: list[dict[str, Any]] = []
    checks.append(_result("--execute present", bool(args.execute), "required"))
    checks.append(
        _result(
            "--simulate-live-workflow present",
            bool(getattr(args, "simulate_live_workflow", False)),
            "simulation-only; live workflow without this flag remains refused",
        )
    )
    for gate, label in (
        ("allow_paid_api", "--allow-paid-api"),
        ("allow_db_write", "--allow-db-write"),
        ("allow_pdf_copy", "--allow-pdf-copy"),
        ("allow_staging_archive", "--allow-staging-archive"),
    ):
        checks.append(_result(f"{label} absent", not bool(getattr(args, gate, False)), "simulation refuses side-effect gates"))

    explicit_mode = args.mode in {"A", "B", "C"}
    checks.append(_result("--mode explicit", explicit_mode, f"mode={args.mode!r}"))

    plan_path = resolve_plan_path(run_id=args.run_id, plan_path=args.plan_path, runs_dir=args.runs_dir)
    checks.append(_result("run plan selected", plan_path is not None, str(plan_path) if plan_path else "missing --run-id"))
    if plan_path is None or not plan_path.exists():
        return False, checks, None, None, None

    run_dir = plan_path.parent
    wf_path = run_dir / "workflow_plan.json"
    checks.append(_result("workflow_plan.json exists", wf_path.exists(), str(wf_path)))
    if not wf_path.exists():
        return False, checks, run_dir, None, None

    run_plan = load_run_plan(plan_path)
    workflow = json.loads(wf_path.read_text(encoding="utf-8"))
    target_name = _target_filename(args)
    checks.append(_result("pilot_profile safe-live", run_plan.get("pilot_profile") == "safe-live", run_plan.get("pilot_profile", "")))
    checks.append(
        _result(
            "workflow plan-only",
            workflow.get("read_only") is True and workflow.get("mode") == "plan_only",
            f"read_only={workflow.get('read_only')} mode={workflow.get('mode')}",
        )
    )

    lock_dir = Path(args.runs_dir) if args.runs_dir else config.RUNS_DIR
    lock_path = lock_dir / ".corpus_pipeline.lock"
    checks.append(_result("no existing run lock", not lock_path.exists(), str(lock_path)))

    selected_names = {pdf.get("name") for pdf in run_plan.get("pdf_plans", [])}
    if target_name:
        checks.append(_result("workflow target selected", target_name in selected_names, target_name))

    for pdf in run_plan.get("pdf_plans", []):
        if target_name and pdf.get("name") != target_name:
            continue
        p = Path(pdf["path"])
        checks.append(_result(f"PDF exists: {pdf['name'][:40]}", p.exists(), str(p)))

    ok = all(c["ok"] for c in checks)
    return ok, checks, run_dir, run_plan, workflow


def _load_stage_e(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "metadata_duplicate_resolution_preview.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {item["name"]: item for item in data.get("items", [])}


def _workflow_item_by_name(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in workflow.get("items", [])}


def _simulate_duplicate_skip_path(workflow_item: dict[str, Any]) -> dict[str, Any]:
    route = workflow_item.get("route") or {}
    return {
        "phase_id": "B",
        "label": "Duplicate-skip log-only path (Levitin)",
        "filename": workflow_item["name"],
        "route_type": route.get("route_type"),
        "matched_existing_db_id": route.get("matched_existing_db_id"),
        "stage_e_decision": (route.get("stage_e_resolution") or {}).get("decision"),
        "simulated_stages": ["preflight", "duplicate_skip_log"],
        "granular_stage_reference": "live-duplicate-skip-log-only (Stage I)",
        "koren_harris_validator_bypassed": True,
        "simulated_action": "Append duplicate-skip audit entry to run artifacts; no papers.db update",
        "future_gates": FUTURE_GATES["duplicate_skip_log_only"],
        "side_effects_in_simulation": False,
        "side_effects_in_future_live": "run artifact write only",
    }


def _simulate_parent_path(
    *,
    phase_id: str,
    label: str,
    pdf_plan: dict[str, Any],
    stage_e_item: dict[str, Any] | None,
    workflow_item: dict[str, Any],
    db_path: Path,
) -> dict[str, Any]:
    stage_g = run_general_book_toc_chapter_preview_item(pdf_plan=pdf_plan, stage_e_item=stage_e_item)
    validation = run_general_parent_validation_preview(
        pdf_plan=pdf_plan,
        stage_g_item=stage_g,
        stage_e_item=stage_e_item,
        workflow_route=workflow_item.get("route") or {},
        db_path=db_path,
    )
    duplicate = (stage_e_item or {}).get("duplicate_version_resolution") or {}
    duplicate_reassessment = {
        "status": "no_duplicate_signal"
        if duplicate.get("decision") == "book_or_edited_volume_candidate" and not duplicate.get("matched_rows")
        else "blocked_duplicate_or_ambiguous_signal",
        "stage_e_decision": duplicate.get("decision"),
        "stage_e_confidence": duplicate.get("confidence"),
        "matched_rows": duplicate.get("matched_rows") or [],
    }
    ready_for_one_shot = (
        validation.get("validation_pass") is True
        and validation.get("ready_for_future_parent_record_pilot") is True
        and duplicate_reassessment["status"] == "no_duplicate_signal"
    )
    simulation_refusals = [
        {
            "phase": "parent_record_create",
            "required_gate": "--allow-db-write",
            "refused_in_simulation": True,
            "side_effect_prevented": "one DB row insert",
        },
        {
            "phase": "source_pdf_move",
            "required_gate": "--allow-pdf-copy",
            "refused_in_simulation": True,
            "side_effect_prevented": "one staged PDF move to source-pdfs",
        },
        {
            "phase": "fileinfo_attach",
            "required_gate": "--allow-db-write",
            "refused_in_simulation": True,
            "side_effect_prevented": "one DB row update limited to file_info",
        },
        {
            "phase": "staging_cleanup_check",
            "required_gate": None,
            "refused_in_simulation": False,
            "side_effect_prevented": "none; verifies original staging path is gone and no processed/archive copy exists",
        },
    ]
    return {
        "phase_id": phase_id,
        "label": label,
        "filename": pdf_plan["name"],
        "simulated_stages": [
            "general_book_toc_chapter_preview (Stage G generalized)",
            "general_parent_validation_preview (Stage J/O generalized)",
        ],
        "koren_harris_specific_stages_bypassed": True,
        "stage_g_general": stage_g,
        "parent_validation_general": validation,
        "duplicate_reassessment": duplicate_reassessment,
        "validation_pass": validation.get("validation_pass"),
        "ready_for_future_parent_record_pilot": validation.get("ready_for_future_parent_record_pilot"),
        "ready_for_one_shot_live_workflow": ready_for_one_shot,
        "simulation_refusals": simulation_refusals,
        "full_lifecycle_phases_detected": [
            "parent_validation",
            "parent_record_create",
            "source_pdf_move",
            "fileinfo_attach",
            "staging_cleanup_check",
            "completion_report",
        ],
        "future_gates_before_write": {
            "parent_record_create": FUTURE_GATES["parent_record_create"],
            "source_pdf_move": FUTURE_GATES["source_pdf_move"],
            "staging_cleanup_check": FUTURE_GATES["staging_cleanup_check"],
            "evaluation": FUTURE_GATES["evaluation"],
        },
        "side_effects_in_simulation": False,
    }


def _future_one_shot_live_command(run_id: str, target: str) -> str:
    return (
        "python pipeline\\run_corpus_pipeline.py --workflow ingest --mode A "
        "--selection-profile broader --pilot-profile safe-live --max-papers 5 "
        f"--run-id {run_id} --execute --workflow-target {target} "
        "--allow-db-write --allow-pdf-copy"
    )


def _recommended_first_live_sequence(run_id: str) -> list[dict[str, Any]]:
    return [
        {
            "step": 1,
            "do_not_run_yet": True,
            "action": "Orchestrated duplicate-skip log-only for Levitin",
            "command_preview": (
                f"python pipeline\\run_corpus_pipeline.py --workflow ingest --execute "
                f"--mode A --run-id {run_id} --execute-stage live-duplicate-skip-log-only"
            ),
            "gates": ["run lock"],
        },
        {
            "step": 2,
            "do_not_run_yet": True,
            "action": "Generalized read-only parent validation for Cacioppo (if not already live-validated)",
            "command_preview": (
                f"python pipeline\\run_corpus_pipeline.py --workflow ingest --execute "
                f"--simulate-live-workflow --mode A --run-id {run_id}  # validation phase only in future live wiring"
            ),
            "gates": [],
        },
        {
            "step": 3,
            "do_not_run_yet": True,
            "action": "Cacioppo-only parent record creation",
            "command_preview": (
                f"python pipeline\\run_corpus_pipeline.py --workflow ingest --execute "
                f"--mode A --run-id {run_id} --allow-db-write  # generalized parent-record stage TBD"
            ),
            "gates": ["--allow-db-write", "validation_pass", "DB backup"],
        },
        {
            "step": 4,
            "do_not_run_yet": True,
            "action": "Cacioppo move to source-pdfs and file_info attach",
            "command_preview": (
                f"python pipeline\\run_corpus_pipeline.py --workflow ingest --execute "
                f"--mode A --run-id {run_id} --allow-db-write --allow-pdf-copy"
            ),
            "gates": ["--allow-db-write", "--allow-pdf-copy"],
        },
        {
            "step": 5,
            "do_not_run_yet": True,
            "action": "Only after Cacioppo path succeeds: repeat for Hallam, then Ashley",
            "command_preview": "Same gated sequence per parent candidate",
            "gates": ["per-item validation_pass"],
        },
    ]


def render_simulation_report(simulation: dict[str, Any]) -> str:
    lines = [
        "Corpus Ingest Live Workflow Simulation (READ-ONLY)",
        "=" * 52,
        f"Run ID: {simulation.get('run_id')}",
        f"Pilot profile: {simulation.get('pilot_profile')}",
        f"Workflow target: {simulation.get('workflow_target') or '(none)'}",
        f"Simulation OK: {simulation.get('simulation_ok')}",
        "",
        "Phase order",
        "-" * 52,
    ]
    for phase in simulation.get("phases", []):
        lines.append(f"\n[{phase.get('phase_id')}] {phase.get('label')}")
        if phase.get("filename"):
            lines.append(f"  File: {phase['filename']}")
        if "validation_pass" in phase:
            lines.append(f"  Validation pass: {phase.get('validation_pass')}")
            lines.append(f"  Ready for future parent-record pilot: {phase.get('ready_for_future_parent_record_pilot')}")
            lines.append(f"  Ready for one-shot live workflow: {phase.get('ready_for_one_shot_live_workflow')}")
        if phase.get("matched_existing_db_id"):
            lines.append(f"  Matched DB ID: {phase['matched_existing_db_id']}")
        if phase.get("skipped_items"):
            lines.append(f"  Skipped items: {phase['skipped_items']}")
        if phase.get("simulation_refusals"):
            for refusal in phase["simulation_refusals"]:
                lines.append(
                    "  Refused: "
                    f"{refusal['phase']} requires {refusal['required_gate']} "
                    f"({refusal['side_effect_prevented']})"
                )

    lines.extend(["", "Future live gates", "-" * 52])
    for key, gates in simulation.get("future_gates_summary", {}).items():
        lines.append(f"- {key}: {gates}")

    lines.extend(["", "Recommended first true live sequence (DO NOT RUN YET)", "-" * 52])
    if simulation.get("future_one_shot_live_command_do_not_run_yet"):
        lines.append(simulation["future_one_shot_live_command_do_not_run_yet"])
    for step in simulation.get("recommended_first_live_sequence", []):
        lines.append(f"{step['step']}. {step['action']}")
        lines.append(f"   Gates: {step.get('gates')}")
        lines.append(f"   Preview: {step.get('command_preview')}")

    lines.extend(
        [
            "",
            "Safety",
            "-" * 52,
            "This simulation performed no API calls, DB writes, PDF moves/copies/archives/deletes, or ID changes.",
            "Artifacts written are simulation reports only under the run folder.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_ingest_workflow_simulation(args) -> dict[str, Any]:
    ok, checks, run_dir, run_plan, workflow = validate_simulation_preconditions(args)
    if run_dir is None:
        run_dir = Path(args.runs_dir or config.RUNS_DIR) / (args.run_id or "unknown")

    db_path = Path(args.db_path).resolve() if args.db_path else config.DB_PATH.resolve()
    stage_e_by_name = _load_stage_e(run_dir)
    wf_by_name = _workflow_item_by_name(workflow or {})
    pdf_by_name = {p["name"]: p for p in (run_plan or {}).get("pdf_plans", [])}
    target_alias = getattr(args, "workflow_target", "") or ""
    target_name = _target_filename(args)

    phases: list[dict[str, Any]] = []
    phases.append(
        {
            "phase_id": "A",
            "label": "Preflight and run lock check",
            "simulated_stages": ["preflight"],
            "preflight_checks": checks,
            "preflight_ok": ok,
            "run_lock_absent": all(c["ok"] for c in checks if c["check"] == "no existing run lock"),
            "side_effects_in_simulation": False,
        }
    )

    if ok and workflow and run_plan and target_name:
        if target_name not in pdf_by_name or target_name not in wf_by_name:
            phases.append(
                {
                    "phase_id": "B",
                    "label": f"Target item lifecycle ({target_alias or target_name})",
                    "filename": target_name,
                    "error": "missing from run plan or workflow plan",
                    "side_effects_in_simulation": False,
                }
            )
        else:
            phases.append(
                _simulate_parent_path(
                    phase_id="B",
                    label=f"{target_alias or target_name} parent lifecycle simulation/refusal",
                    pdf_plan=pdf_by_name[target_name],
                    stage_e_item=stage_e_by_name.get(target_name),
                    workflow_item=wf_by_name[target_name],
                    db_path=db_path,
                )
            )
        phases.append(
            {
                "phase_id": "C",
                "label": "Items intentionally not processed",
                "skipped_items": [
                    name
                    for name in pdf_by_name
                    if name != target_name
                ],
                "side_effects_in_simulation": False,
            }
        )
    elif ok and workflow and run_plan:
        levitin = wf_by_name.get(DUPLICATE_CONTROL)
        if levitin:
            phases.append(_simulate_duplicate_skip_path(levitin))

        parent_labels = {
            PARENT_PRIORITY[0]: "Cacioppo parent candidate path",
            PARENT_PRIORITY[1]: "Hallam/Cross/Thaut parent candidate path",
            PARENT_PRIORITY[2]: "Ashley/Timmers parent candidate path",
        }
        phase_ids = ["C", "D", "E"]
        for phase_id, name in zip(phase_ids, PARENT_PRIORITY, strict=True):
            pdf_plan = pdf_by_name.get(name)
            wf_item = wf_by_name.get(name)
            if not pdf_plan or not wf_item:
                phases.append(
                    {
                        "phase_id": phase_id,
                        "label": parent_labels[name],
                        "filename": name,
                        "error": "missing from run plan or workflow plan",
                        "side_effects_in_simulation": False,
                    }
                )
                continue
            phases.append(
                _simulate_parent_path(
                    phase_id=phase_id,
                    label=parent_labels[name],
                    pdf_plan=pdf_plan,
                    stage_e_item=stage_e_by_name.get(name),
                    workflow_item=wf_item,
                    db_path=db_path,
                )
            )

    run_id = run_dir.name
    simulation = {
        "created_at": _now(),
        "run_id": run_id,
        "pilot_profile": (run_plan or {}).get("pilot_profile", ""),
        "workflow_target": target_alias,
        "workflow_target_filename": target_name,
        "simulation_mode": True,
        "simulation_ok": ok,
        "read_only": True,
        "side_effects": {
            "api_calls": "none",
            "db_writes": "none",
            "pdf_operations": "none",
            "id_changes": "none",
        },
        "preflight_checks": checks,
        "phases": phases,
        "future_gates_summary": FUTURE_GATES,
        "future_one_shot_live_command_do_not_run_yet": _future_one_shot_live_command(run_id, target_alias)
        if target_alias
        else None,
        "recommended_first_live_sequence": [] if target_alias else _recommended_first_live_sequence(run_id),
        "still_stubbed": [
            "full one-shot --workflow ingest --execute without --simulate-live-workflow",
            "targeted parent lifecycle live dispatcher still requires promotion from item-scoped pilot stages",
        ],
        "generalized": [
            "Stage G book/TOC/chapter preview via corpus_general_parent_stages",
            "Stage J/O parent validation via corpus_general_parent_stages",
        ],
    }

    sim_json = run_dir / "live_workflow_simulation.json"
    sim_txt = run_dir / "live_workflow_simulation_report.txt"
    sim_json.write_text(json.dumps(simulation, indent=2), encoding="utf-8")
    sim_txt.write_text(render_simulation_report(simulation), encoding="utf-8")

    simulation["artifact_paths"] = {"json": str(sim_json), "report": str(sim_txt)}
    return {
        "simulation": simulation,
        "simulation_ok": ok,
        "preflight_report": render_execute_preflight(checks, run_plan),
        "simulation_json_path": sim_json,
        "simulation_report_path": sim_txt,
    }


def run_parent_validation_audit(args) -> dict[str, Any]:
    target_alias = getattr(args, "audit_parent_target", "") or getattr(args, "workflow_target", "")
    target_name = WORKFLOW_TARGET_ALIASES.get(target_alias, target_alias or None)
    plan_path = resolve_plan_path(run_id=args.run_id, plan_path=args.plan_path, runs_dir=args.runs_dir)
    checks: list[dict[str, Any]] = [
        _result("--run-id/plan selected", plan_path is not None and plan_path.exists() if plan_path else False, str(plan_path) if plan_path else "missing"),
        _result("--allow-paid-api absent", not bool(getattr(args, "allow_paid_api", False)), "audit only"),
        _result("--allow-db-write absent", not bool(getattr(args, "allow_db_write", False)), "audit only"),
        _result("--allow-pdf-copy absent", not bool(getattr(args, "allow_pdf_copy", False)), "audit only"),
        _result("--allow-staging-archive absent", not bool(getattr(args, "allow_staging_archive", False)), "audit only"),
        _result("known workflow target", target_name is not None, getattr(args, "audit_parent_target", "")),
    ]
    if not plan_path or not plan_path.exists() or not target_name:
        return {"ok": False, "checks": checks, "message": "Parent validation audit preconditions failed."}

    run_dir = plan_path.parent
    run_plan = load_run_plan(plan_path)
    workflow_path = run_dir / "workflow_plan.json"
    checks.append(_result("workflow_plan.json exists", workflow_path.exists(), str(workflow_path)))
    stage_e_by_name = _load_stage_e(run_dir)
    workflow = json.loads(workflow_path.read_text(encoding="utf-8")) if workflow_path.exists() else {}
    wf_by_name = _workflow_item_by_name(workflow)
    pdf_by_name = {p["name"]: p for p in run_plan.get("pdf_plans", [])}
    checks.extend(
        [
            _result("target in run_plan", target_name in pdf_by_name, target_name),
            _result("target in workflow_plan", target_name in wf_by_name, target_name),
            _result("target in Stage E", target_name in stage_e_by_name, target_name),
        ]
    )
    if not all(c["ok"] for c in checks):
        return {"ok": False, "checks": checks, "message": "Parent validation audit target is missing from plan artifacts."}

    phase = _simulate_parent_path(
        phase_id="audit",
        label=f"{getattr(args, 'audit_parent_target', '')} parent validation audit",
        pdf_plan=pdf_by_name[target_name],
        stage_e_item=stage_e_by_name.get(target_name),
        workflow_item=wf_by_name[target_name],
        db_path=Path(args.db_path).resolve() if args.db_path else config.DB_PATH.resolve(),
    )
    return {
        "ok": phase.get("validation_pass") is True and phase.get("ready_for_future_parent_record_pilot") is True,
        "checks": checks,
        "target": getattr(args, "audit_parent_target", ""),
        "filename": target_name,
        "validation_pass": phase.get("validation_pass"),
        "ready_for_future_parent_record_pilot": phase.get("ready_for_future_parent_record_pilot"),
        "ready_for_one_shot_live_workflow": phase.get("ready_for_one_shot_live_workflow"),
        "validation_score": (phase.get("parent_validation_general") or {}).get("validation_score"),
        "validation_reasons": (phase.get("parent_validation_general") or {}).get("validation_reasons"),
        "validation_blockers": (phase.get("parent_validation_general") or {}).get("validation_blockers"),
        "candidate_parent_id_preview": (phase.get("parent_validation_general") or {}).get("candidate_parent_id_preview"),
        "side_effects": {"api_calls": "none", "db_writes": "none", "pdf_operations": "none", "evaluations": "none"},
    }
