"""Single research-paper live ingest with mandatory Claude Sonnet evaluation.

Promoted path: ``--workflow ingest --execute --workflow-target LanguageProfile2024``
with ``--allow-db-write --allow-pdf-copy --allow-paid-api --max-papers 1``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

import config
import papers_db
from add_paper import process_paper
from corpus_execute_guard import load_run_plan, resolve_plan_path
from corpus_modes import normalize_eval_mode
from eval_completion import is_ingest_evaluation_complete, SONNET_UNAVAILABLE_MSG
from evaluate_paper import evaluate_one

TARGET_ALIASES: dict[str, str] = {
    "LanguageProfile2024": "--Language-profile-among-Arabic-speaking-chil_2024_International-Journal-of-.pdf",
}


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


def _resolve_target_filename(args) -> tuple[str | None, str]:
    alias = (getattr(args, "workflow_target", "") or "").strip()
    if alias in TARGET_ALIASES:
        return TARGET_ALIASES[alias], alias
    plan_path = resolve_plan_path(
        run_id=getattr(args, "run_id", "") or "",
        plan_path="",
        runs_dir=getattr(args, "runs_dir", "") or "",
    )
    if not plan_path:
        return None, alias
    run_plan = load_run_plan(plan_path)
    pdfs = run_plan.get("pdf_plans") or []
    if len(pdfs) == 1:
        return pdfs[0]["name"], alias or pdfs[0]["name"]
    return None, alias


def _write_artifacts(run_dir: Path, summary: dict[str, Any]) -> dict[str, str]:
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "live_research_one_shot_report.json"
    txt_path = run_dir / "live_research_one_shot_report.txt"
    json_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    lines = [
        "Live research one-shot (Sonnet evaluation required)",
        "=" * 50,
        f"Status: {summary.get('status')}",
        f"Target: {summary.get('target_alias')} -> {summary.get('staging_filename')}",
        f"Paper ID: {summary.get('paper_id')}",
        f"Title: {summary.get('title')}",
    ]
    if summary.get("error"):
        lines.append(f"Error: {summary['error']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "txt": str(txt_path)}


def validate_preconditions(args) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    ctx: dict[str, Any] = {}

    try:
        mode = normalize_eval_mode(getattr(args, "mode", None) or "A")
    except ValueError as e:
        checks.append(_result("mode A (Sonnet-only)", False, str(e)))
        return False, checks, ctx
    checks.append(_result("mode A (Sonnet-only)", mode == "A", f"mode={mode!r}"))

    checks.append(_result("--max-papers 1", getattr(args, "max_papers", None) == 1, f"max={getattr(args, 'max_papers', None)!r}"))
    checks.append(_result("--allow-db-write", bool(getattr(args, "allow_db_write", False)), "required"))
    checks.append(_result("--allow-pdf-copy", bool(getattr(args, "allow_pdf_copy", False)), "required"))
    checks.append(_result("--allow-paid-api", bool(getattr(args, "allow_paid_api", False)), "required for Sonnet evaluation"))

    if not getattr(args, "run_id", ""):
        checks.append(_result("--run-id", False, "required — must reference plan-only run folder"))
        return False, checks, ctx
    checks.append(_result("--run-id", True, args.run_id))

    filename, alias = _resolve_target_filename(args)
    if not filename:
        checks.append(_result("workflow target", False, "could not resolve staging PDF from --workflow-target or run plan"))
        return False, checks, ctx
    staging = config.STAGING / filename
    ctx["staging_filename"] = filename
    ctx["target_alias"] = alias
    ctx["staging_path"] = staging
    checks.append(_result("staging PDF exists", staging.exists(), str(staging)))

    plan_path = resolve_plan_path(run_id=args.run_id, plan_path="", runs_dir=getattr(args, "runs_dir", "") or "")
    if plan_path:
        run_plan = load_run_plan(plan_path)
        ctx["run_plan"] = run_plan
        checks.append(_result("run plan loaded", True, str(plan_path)))
    elif alias not in TARGET_ALIASES:
        checks.append(_result("run plan loaded", False, "run plan not found"))

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    checks.append(_result("ANTHROPIC_API_KEY", bool(api_key), "required for Claude Sonnet"))
    if not api_key:
        ctx["sonnet_unavailable"] = True

    ok = all(c["ok"] for c in checks)
    return ok, checks, ctx


def run_research_one_shot_live_workflow(args) -> dict[str, Any]:
    run_id = getattr(args, "run_id", "") or f"live-sonnet-pilot-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir = Path(getattr(args, "runs_dir", "") or config.RUNS_DIR) / run_id
    summary: dict[str, Any] = {
        "created_at": _now(),
        "run_id": run_id,
        "workflow": "ingest_research_one_shot",
        "evaluator": config.EVAL_MODEL,
        "phases": [],
    }

    ok, checks, ctx = validate_preconditions(args)
    summary["precondition_checks"] = checks
    if not ok:
        if ctx.get("sonnet_unavailable"):
            summary["status"] = "failed_sonnet_unavailable"
            summary["error"] = SONNET_UNAVAILABLE_MSG
        else:
            summary["status"] = "failed_preconditions"
            summary["error"] = "Precondition checks failed"
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifacts": paths}

    staging: Path = ctx["staging_path"]
    filename = ctx["staging_filename"]
    summary["staging_filename"] = filename
    summary["target_alias"] = ctx.get("target_alias")

    before_count = papers_db.get_paper_count()
    staging_hash_before = _sha256(staging)
    summary["db_rows_before"] = before_count
    summary["staging_sha256_before"] = staging_hash_before

    backup_path = papers_db.backup(suffix="PRE-LIVE-SONNET-PILOT")
    summary["backup_path"] = str(backup_path) if backup_path else None
    summary["phases"].append({"phase": "backup", "status": "completed" if backup_path else "skipped"})

    process_args = {
        "extract_metadata": True,
        "no_claude": True,
        "no_ollama": True,
        "tags": "live-sonnet-pilot",
    }
    try:
        ingested = process_paper(staging, process_args)
    except Exception as e:
        summary["status"] = "failed_ingest"
        summary["error"] = str(e)
        summary["traceback"] = traceback.format_exc()
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifacts": paths}

    if not ingested:
        summary["status"] = "failed_ingest"
        summary["error"] = "process_paper returned False (duplicate, supplement, or metadata failure)"
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifacts": paths}

    summary["phases"].append({"phase": "ingest", "status": "completed"})

    after_count = papers_db.get_paper_count()
    if after_count != before_count + 1:
        summary["status"] = "failed_ingest"
        summary["error"] = f"Expected exactly one new DB row; before={before_count} after={after_count}"
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifacts": paths}

    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT id FROM papers ORDER BY rowid DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    paper_id = row[0] if row else None
    summary["paper_id"] = paper_id
    paper = papers_db.get_paper(paper_id) if paper_id else None
    summary["title"] = ((paper or {}).get("basic_metadata") or {}).get("title")

    try:
        client = anthropic.Anthropic()
        eval_result = evaluate_one(client, paper_id, force=False)
        eval_ok = bool(eval_result.get("ok")) if isinstance(eval_result, dict) else bool(eval_result)
    except RuntimeError as e:
        summary["status"] = "failed_sonnet_unavailable"
        summary["error"] = str(e)
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifacts": paths}
    except Exception as e:
        summary["status"] = "failed_evaluation"
        summary["error"] = str(e)
        summary["traceback"] = traceback.format_exc()
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifacts": paths}

    if not eval_ok:
        summary["status"] = "failed_evaluation"
        summary["error"] = "evaluate_one returned False"
        summary["phases"].append({"phase": "evaluation", "status": "failed"})
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifacts": paths}

    summary["phases"].append({"phase": "evaluation", "status": "completed"})

    paper = papers_db.get_paper(paper_id)
    complete, reason = is_ingest_evaluation_complete(paper or {})
    cl = (paper or {}).get("classification") or {}
    summary["evaluation_complete"] = complete
    summary["evaluation_complete_reason"] = reason
    summary["classification_rating"] = cl.get("rating")
    summary["phases"].append(
        {"phase": "completion_report", "status": "passed" if complete else "failed", "reason": reason}
    )

    if staging.exists():
        try:
            staging.unlink()
            summary["staging_removed"] = True
        except OSError as e:
            summary["staging_removed"] = False
            summary["staging_remove_error"] = str(e)
    else:
        summary["staging_removed"] = True

    summary["db_rows_after"] = papers_db.get_paper_count()
    summary["status"] = "live_workflow_completed" if complete else "completion_check_failed"
    paths = _write_artifacts(run_dir, summary)
    return {"ok": complete, "summary": summary, "artifacts": paths}
