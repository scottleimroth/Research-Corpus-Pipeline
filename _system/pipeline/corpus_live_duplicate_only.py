"""Orchestrated duplicate-skip log-only live step.

This is intentionally narrower than the full ingest workflow. It accepts only
the Levitin exact-duplicate decision into run-level audit artifacts and performs
no DB writes, PDF operations, API calls, evaluation, or ID changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from corpus_execute_guard import load_run_plan, resolve_plan_path

ALLOWED_RUN_ID = "20260528-203823"
LEVITIN_FILENAME = "Levitin-The Psychology of Music- Rhythm and Movement-2017-An.pdf"
LEVITIN_DB_ID = "Levitin2018"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": ok, "detail": detail}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _db_id_exists(db_path: Path, paper_id: str) -> bool:
    if not db_path.exists():
        return False
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT 1 FROM papers WHERE id = ? LIMIT 1", (paper_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def _workflow_item(workflow: dict[str, Any], filename: str) -> dict[str, Any] | None:
    for item in workflow.get("items", []):
        if item.get("name") == filename:
            return item
    return None


def _stage_e_item(stage_e: dict[str, Any], filename: str) -> dict[str, Any] | None:
    for item in stage_e.get("items", []):
        if item.get("name") == filename:
            return item
    return None


def _simulation_preflight_ok(simulation: dict[str, Any]) -> bool:
    if simulation.get("simulation_ok") is not True:
        return False
    for phase in simulation.get("phases", []):
        if phase.get("phase_id") == "A":
            return phase.get("preflight_ok") is True
    return False


def _acquire_lock(lock_path: Path) -> int:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(lock_path), flags)
    payload = {
        "created_at": _now(),
        "purpose": "orchestrated_live_duplicate_only",
        "run_id": ALLOWED_RUN_ID,
        "process_only": LEVITIN_FILENAME,
    }
    os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
    return fd


def _release_lock(fd: int, lock_path: Path) -> None:
    os.close(fd)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def validate_live_duplicate_only_preconditions(args) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    """Validate Levitin-only duplicate evidence without side effects."""
    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}

    checks.append(_result("--workflow ingest", getattr(args, "workflow", None) == "ingest", f"workflow={getattr(args, 'workflow', None)!r}"))
    checks.append(_result("--execute present", bool(getattr(args, "execute", False)), "required"))
    checks.append(_result("--live-duplicate-only present", bool(getattr(args, "live_duplicate_only", False)), "required"))
    checks.append(_result("--run-id exact", getattr(args, "run_id", "") == ALLOWED_RUN_ID, f"run_id={getattr(args, 'run_id', '')!r}"))
    checks.append(_result("--mode A", getattr(args, "mode", None) == "A", f"mode={getattr(args, 'mode', None)!r}"))
    checks.append(_result("--max-papers 5", getattr(args, "max_papers", None) == 5, f"max_papers={getattr(args, 'max_papers', None)!r}"))

    for gate, flag in (
        ("allow_paid_api", "--allow-paid-api"),
        ("allow_db_write", "--allow-db-write"),
        ("allow_pdf_copy", "--allow-pdf-copy"),
        ("allow_staging_archive", "--allow-staging-archive"),
    ):
        checks.append(_result(f"{flag} absent", not bool(getattr(args, gate, False)), "side-effect gate refused"))

    plan_path = resolve_plan_path(run_id=args.run_id, plan_path=args.plan_path, runs_dir=args.runs_dir)
    checks.append(_result("run_plan.json selected", plan_path is not None, str(plan_path) if plan_path else "missing"))
    if plan_path is None or not plan_path.exists():
        return False, checks, details

    run_dir = plan_path.parent
    details["run_dir"] = str(run_dir)
    required_artifacts = {
        "workflow_plan": run_dir / "workflow_plan.json",
        "stage_e": run_dir / "metadata_duplicate_resolution_preview.json",
        "simulation": run_dir / "live_workflow_simulation.json",
    }
    for label, path in required_artifacts.items():
        checks.append(_result(f"{label} artifact exists", path.exists(), str(path)))
    if not all(path.exists() for path in required_artifacts.values()):
        return False, checks, details

    run_plan = load_run_plan(plan_path)
    workflow = _load_json(required_artifacts["workflow_plan"])
    stage_e = _load_json(required_artifacts["stage_e"])
    simulation = _load_json(required_artifacts["simulation"])
    details.update({"run_plan": run_plan, "workflow": workflow, "stage_e": stage_e, "simulation": simulation})

    checks.append(_result("prior simulation ok", _simulation_preflight_ok(simulation), "simulation_ok and phase A preflight_ok must be true"))

    pdf_plans = {p.get("name"): p for p in run_plan.get("pdf_plans", [])}
    levitin_plan = pdf_plans.get(LEVITIN_FILENAME)
    checks.append(_result("Levitin selected in run_plan", levitin_plan is not None, LEVITIN_FILENAME))
    if levitin_plan is None:
        return False, checks, details

    parent_names = sorted(name for name in pdf_plans if name != LEVITIN_FILENAME)
    details["parents_not_processed"] = parent_names

    levitin_path = Path(levitin_plan["path"])
    checks.append(_result("Levitin PDF exists in staging", levitin_path.exists(), str(levitin_path)))
    if levitin_path.exists():
        on_disk_hash = _sha256(levitin_path)
        details["levitin_on_disk_sha256"] = on_disk_hash
        checks.append(_result("Levitin sha256 matches run_plan", on_disk_hash == levitin_plan.get("sha256"), levitin_plan.get("sha256", "")))

    workflow_item = _workflow_item(workflow, LEVITIN_FILENAME)
    checks.append(_result("Levitin present in workflow_plan", workflow_item is not None, "workflow_plan.json"))
    route = (workflow_item or {}).get("route") or {}
    checks.append(_result("workflow route exact duplicate", route.get("route_type") == "exact_duplicate_skip_candidate", str(route.get("route_type"))))
    checks.append(_result("workflow status duplicate log-only", route.get("workflow_status") == "duplicate_skip_log_only", str(route.get("workflow_status"))))
    checks.append(_result("workflow matched DB ID", route.get("matched_existing_db_id") == LEVITIN_DB_ID, str(route.get("matched_existing_db_id"))))

    se_item = _stage_e_item(stage_e, LEVITIN_FILENAME)
    checks.append(_result("Levitin present in Stage E", se_item is not None, "metadata_duplicate_resolution_preview.json"))
    duplicate_resolution = (se_item or {}).get("duplicate_version_resolution") or {}
    checks.append(_result("Stage E exact_duplicate_skip", duplicate_resolution.get("decision") == "exact_duplicate_skip", str(duplicate_resolution.get("decision"))))
    checks.append(_result("Stage E high confidence", duplicate_resolution.get("confidence") == "high", str(duplicate_resolution.get("confidence"))))
    matched_rows = duplicate_resolution.get("matched_rows") or []
    matched_ids = {str(row.get("id")) for row in matched_rows if isinstance(row, dict)}
    checks.append(_result("Stage E matched Levitin2018", LEVITIN_DB_ID in matched_ids, ",".join(sorted(matched_ids)) or "none"))

    db_path = Path(args.db_path).resolve() if args.db_path else config.DB_PATH.resolve()
    details["db_path"] = str(db_path)
    checks.append(_result("matched DB ID exists read-only", _db_id_exists(db_path, LEVITIN_DB_ID), str(db_path)))

    return all(c["ok"] for c in checks), checks, details


def render_live_duplicate_only_report(audit: dict[str, Any]) -> str:
    item = audit["item"]
    lines = [
        "Orchestrated Live Duplicate-Skip Log Only",
        "=" * 48,
        f"Run ID: {audit['run_id']}",
        f"Decision: {item['decision']}",
        f"Staged filename: {item['staged_filename']}",
        f"Matched existing DB ID: {item['matched_existing_db_id']}",
        f"Stage E decision: {item['stage_e_decision']} ({item['stage_e_confidence']})",
        "",
        "Actions",
        "-" * 48,
        f"DB action: {item['db_action']}",
        f"PDF action: {item['pdf_action']}",
        f"Evaluation action: {item['evaluation_action']}",
        f"ID action: {item['id_action']}",
        f"Future optional action: {item['future_optional_action']}",
        "",
        "Safety",
        "-" * 48,
        "Processed only Levitin.",
        "Parent candidates were not processed.",
        "No papers.db writes, PDF operations, API calls, evaluation, or ID changes occurred.",
    ]
    return "\n".join(lines) + "\n"


def run_ingest_workflow_live_duplicate_only(args) -> dict[str, Any]:
    """Run the single permitted live side-effect: run-artifact duplicate audit."""
    ok, checks, details = validate_live_duplicate_only_preconditions(args)
    run_dir = Path(details.get("run_dir") or (config.RUNS_DIR / (args.run_id or ALLOWED_RUN_ID)))
    if not ok:
        return {
            "ok": False,
            "checks": checks,
            "artifact_paths": {},
            "message": "Levitin duplicate-only preconditions failed; no audit artifact written.",
        }

    lock_path = run_dir / "orchestrated_live_duplicate_only.lock"
    if lock_path.exists():
        return {
            "ok": False,
            "checks": checks + [_result("run lock absent", False, str(lock_path))],
            "artifact_paths": {},
            "message": "Duplicate-only run lock already exists; refusing.",
        }

    fd = _acquire_lock(lock_path)
    try:
        workflow_item = _workflow_item(details["workflow"], LEVITIN_FILENAME) or {}
        route = workflow_item.get("route") or {}
        stage_e_item = _stage_e_item(details["stage_e"], LEVITIN_FILENAME) or {}
        duplicate_resolution = stage_e_item.get("duplicate_version_resolution") or {}
        pdf_plan = {p["name"]: p for p in details["run_plan"].get("pdf_plans", [])}[LEVITIN_FILENAME]

        audit = {
            "created_at": _now(),
            "run_id": args.run_id,
            "stage": "orchestrated-live-duplicate-only",
            "read_only_except_run_artifact": True,
            "simulation_artifact": str(run_dir / "live_workflow_simulation.json"),
            "workflow_plan_artifact": str(run_dir / "workflow_plan.json"),
            "stage_e_artifact": str(run_dir / "metadata_duplicate_resolution_preview.json"),
            "lock": {"path": str(lock_path), "acquired": True, "released_after_write": True},
            "item": {
                "staged_filename": LEVITIN_FILENAME,
                "staged_path": pdf_plan["path"],
                "sha256": pdf_plan.get("sha256"),
                "matched_existing_db_id": LEVITIN_DB_ID,
                "decision": "accepted_exact_duplicate_skip",
                "workflow_route_type": route.get("route_type"),
                "workflow_status": route.get("workflow_status"),
                "stage_e_decision": duplicate_resolution.get("decision"),
                "stage_e_confidence": duplicate_resolution.get("confidence"),
                "evidence_summary": {
                    "workflow_matched_db_id": route.get("matched_existing_db_id"),
                    "stage_e_matched_ids": [
                        row.get("id")
                        for row in duplicate_resolution.get("matched_rows", [])
                        if isinstance(row, dict) and row.get("id")
                    ],
                    "staged_pdf_hash_verified": True,
                    "matched_db_id_exists_read_only": True,
                },
                "db_action": "none",
                "pdf_action": "none",
                "evaluation_action": "none",
                "id_action": "none",
                "future_optional_action": "staged file can later be archived only after explicit approval",
            },
            "parents_not_processed": details.get("parents_not_processed", []),
            "checks": checks,
            "side_effects": {
                "api_calls": "none",
                "db_writes": "none",
                "pdf_operations": "none",
                "id_changes": "none",
                "evaluation": "none",
            },
        }

        json_path = run_dir / "orchestrated_live_duplicate_only.json"
        report_path = run_dir / "orchestrated_live_duplicate_only_report.txt"
        json_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
        report_path.write_text(render_live_duplicate_only_report(audit), encoding="utf-8")

        return {
            "ok": True,
            "audit": audit,
            "checks": checks,
            "artifact_paths": {"json": str(json_path), "report": str(report_path)},
            "message": "Accepted Levitin exact duplicate skip into run-level audit artifacts only.",
        }
    finally:
        _release_lock(fd, lock_path)
