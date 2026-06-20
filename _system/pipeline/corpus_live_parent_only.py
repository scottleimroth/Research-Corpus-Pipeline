"""Cacioppo-only generalized parent-record live stage.

The default/no-gate path is a refusal preview: it validates that Cacioppo is
ready for a future single-row parent insert, then stops before any DB write.
Actual insertion is gated behind --allow-db-write and is not used by this step.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
import papers_db
from corpus_execute_guard import load_run_plan, resolve_plan_path

ALLOWED_RUN_ID = "20260528-203823"
CACIOPPO_ALIAS = "Cacioppo"
CACIOPPO_FILENAME = "(Cambridge Handbooks in Psychology) John T. Cacioppo (editor.pdf"
CACIOPPO_ID = "Cacioppo_2013_Handbook_book"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": ok, "detail": detail}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _db_row_exists(conn: sqlite3.Connection, paper_id: str) -> bool:
    return conn.execute("SELECT 1 FROM papers WHERE id = ? LIMIT 1", (paper_id,)).fetchone() is not None


def _db_row_by_id(conn: sqlite3.Connection, paper_id: str) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    for key in papers_db.JSON_FIELDS:
        if key in out and out[key]:
            try:
                out[key] = json.loads(out[key])
            except (TypeError, json.JSONDecodeError):
                pass
    return out


def _table_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])


def _non_target_digest(conn: sqlite3.Connection, target_id: str) -> str:
    conn.row_factory = sqlite3.Row
    h = hashlib.sha256()
    cols = papers_db.ALL_COLUMNS
    sql = f"SELECT {','.join(cols)} FROM papers WHERE id != ? ORDER BY id"
    for row in conn.execute(sql, (target_id,)):
        vals = [row[col] for col in cols]
        encoded = json.dumps(vals, ensure_ascii=False, sort_keys=False, default=str).encode("utf-8")
        h.update(len(encoded).to_bytes(8, "big"))
        h.update(encoded)
    return h.hexdigest()


def _pdf_snapshot() -> dict[str, Any]:
    h = hashlib.sha256()
    entries = []
    for root in (config.STAGING, config.SOURCE_PDFS):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.pdf")):
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = str(path.relative_to(config.BASE))
            entry = f"{rel}|{stat.st_size}|{stat.st_mtime_ns}"
            entries.append(entry)
            h.update(entry.encode("utf-8", errors="replace"))
            h.update(b"\n")
    return {"count": len(entries), "digest": h.hexdigest()}


def _same_work_matches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, document_type, basic_metadata, file_info
        FROM papers
        WHERE id != ?
          AND (
            basic_metadata LIKE ?
            OR basic_metadata LIKE ?
            OR file_info LIKE ?
          )
        ORDER BY id
        LIMIT 10
        """,
        (
            CACIOPPO_ID,
            "%HANDBOOK OF PSYCHOPHYSIOLOGY%",
            "%Handbook of Psychophysiology%",
            "%John T. Cacioppo%",
        ),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "document_type": row["document_type"],
        }
        for row in rows
    ]


def _create_sqlite_backup() -> dict[str, Any]:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = config.BACKUP_DIR / f"papers-{ts}-PRE-CACIOPPO-PARENT.db"
    source = sqlite3.connect(str(config.DB_PATH))
    backup = sqlite3.connect(str(dest))
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    size = dest.stat().st_size if dest.exists() else 0
    conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        row_count = _table_count(conn)
    finally:
        conn.close()
    return {
        "ok": quick_check == "ok" and size > 1024 * 1024,
        "path": str(dest),
        "bytes": size,
        "quick_check": quick_check,
        "row_count": row_count,
        "detail": "fresh PRE-CACIOPPO-PARENT backup created and verified",
    }


def _safe_id_ok(candidate_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Za-z]+_\d{4}_[A-Za-z0-9]+_book", candidate_id))


def _stage_e_item(stage_e: dict[str, Any]) -> dict[str, Any] | None:
    for item in stage_e.get("items", []):
        if item.get("name") == CACIOPPO_FILENAME:
            return item
    return None


def _workflow_item(workflow: dict[str, Any]) -> dict[str, Any] | None:
    for item in workflow.get("items", []):
        if item.get("name") == CACIOPPO_FILENAME:
            return item
    return None


def _simulation_cacioppo_phase(simulation: dict[str, Any]) -> dict[str, Any] | None:
    for phase in simulation.get("phases", []):
        if phase.get("filename") == CACIOPPO_FILENAME:
            return phase
    return None


def _duplicate_reassessment(stage_e_item: dict[str, Any], workflow_item: dict[str, Any]) -> dict[str, Any]:
    duplicate = stage_e_item.get("duplicate_version_resolution") or {}
    route = workflow_item.get("route") or {}
    no_duplicate = (
        duplicate.get("decision") == "book_or_edited_volume_candidate"
        and not duplicate.get("matched_rows")
        and route.get("route_type") == "book_parent_record_candidate"
        and not route.get("duplicate_log_only")
    )
    return {
        "status": "no_duplicate_signal" if no_duplicate else "blocked_duplicate_or_ambiguous_signal",
        "stage_e_decision": duplicate.get("decision"),
        "stage_e_confidence": duplicate.get("confidence"),
        "matched_rows": duplicate.get("matched_rows") or [],
        "workflow_route_type": route.get("route_type"),
    }


def _validated_metadata(phase: dict[str, Any]) -> dict[str, Any]:
    validation = phase.get("parent_validation_general") or {}
    metadata = validation.get("candidate_parent_metadata") or {}
    isbn_candidates = metadata.get("isbn_candidates") or []
    return {
        "title": metadata.get("title") or "HANDBOOK OF PSYCHOPHYSIOLOGY, FOURTH EDITION",
        "authors": ["John T. Cacioppo"],
        "year": str(metadata.get("year") or "2013"),
        "edition": "Fourth Edition",
        "publisher": "Cambridge University Press",
        "doi": metadata.get("doi"),
        "isbn": isbn_candidates[:4],
        "source_note": "Generated from prior generalized Stage G/J simulation artifacts; parent-only preview.",
    }


def _build_parent_row(metadata: dict[str, Any], pdf_plan: dict[str, Any], run_id: str) -> dict[str, Any]:
    file_info = {
        "original_filename": CACIOPPO_FILENAME,
        "staged_path": pdf_plan["path"],
        "staged_sha256": pdf_plan.get("sha256"),
        "filepath": None,
        "renamed_filename": None,
        "managed_pdf_action": "not_copied_or_moved_in_parent_record_stage",
    }
    return {
        "id": CACIOPPO_ID,
        "timestamp_added": _now(),
        "document_type": "book",
        "basic_metadata": metadata,
        "identifiers": {
            "doi": metadata.get("doi"),
            "isbn": metadata.get("isbn") or [],
            "pubmed_id": None,
            "orcid_ids": [],
            "issn": None,
        },
        "abstract_keywords": {"abstract": "", "keywords": []},
        "ethics_and_consent": {},
        "sample_characteristics": {},
        "replication_and_preregistration": {},
        "methodology": {},
        "statistical_analysis": {},
        "quality_indicators": {},
        "measurement_quality": {},
        "stimulus_and_procedures": {},
        "analysis_pipeline": {},
        "reporting_standards": {},
        "meta_analysis_specific": {},
        "supplements": {},
        "notes_and_flags": {
            "created_by": "orchestrated-live-parent-only",
            "source_run_id": run_id,
            "parent_candidate": "Cacioppo",
            "guideline_policy": "guideline_mismatch_or_new_guideline_needed_candidate is metadata/routing, not exclusion",
            "child_chapter_policy": "deferred_until_parent_record_is_established",
            "pdf_policy": "staged PDF retained; no managed copy/move in this stage",
            "evaluation_policy": "not evaluated in parent-record pilot",
        },
        "classification": {
            "document_type": "book",
            "evaluation_status": "deferred_not_evaluated_for_book_parent",
            "rating": None,
            "red_team_survival": None,
            "route_status": "created_by_cacioppo_parent_record_pilot",
        },
        "tags": ["guideline_mismatch_or_new_guideline_needed_candidate"],
        "file_info": file_info,
        "full_text": None,
    }


def _insert_parent_row(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    valid, errors = papers_db.validate_paper(row)
    if not valid:
        raise RuntimeError(f"Paper validation failed: {'; '.join(errors)}")
    cols = papers_db.ALL_COLUMNS
    placeholders = ",".join(["?"] * len(cols))
    conn.execute(f"INSERT INTO papers ({','.join(cols)}) VALUES ({placeholders})", papers_db._paper_to_row(row))


def _acquire_lock(run_dir: Path) -> Path:
    lock_path = run_dir / "orchestrated_live_parent_only.lock"
    payload = json.dumps(
        {
            "created_at": _now(),
            "stage": "orchestrated-live-parent-only",
            "run_id": run_dir.name,
            "process_only": CACIOPPO_FILENAME,
            "pid": os.getpid(),
        },
        indent=2,
    )
    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return lock_path


def _release_lock(lock_path: Path | None) -> None:
    if lock_path and lock_path.exists():
        lock_path.unlink()


def validate_parent_only_preconditions(args) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}

    checks.append(_result("--workflow ingest", getattr(args, "workflow", None) == "ingest", f"workflow={getattr(args, 'workflow', None)!r}"))
    checks.append(_result("--execute present", bool(getattr(args, "execute", False)), "required"))
    checks.append(_result("--live-parent-only Cacioppo", getattr(args, "live_parent_only", "") == CACIOPPO_ALIAS, f"value={getattr(args, 'live_parent_only', '')!r}"))
    checks.append(_result("--run-id exact", getattr(args, "run_id", "") == ALLOWED_RUN_ID, f"run_id={getattr(args, 'run_id', '')!r}"))
    checks.append(_result("--mode A", getattr(args, "mode", None) == "A", f"mode={getattr(args, 'mode', None)!r}"))
    checks.append(_result("--max-papers 5", getattr(args, "max_papers", None) == 5, f"max_papers={getattr(args, 'max_papers', None)!r}"))
    checks.append(_result("--allow-paid-api absent", not bool(getattr(args, "allow_paid_api", False)), "evaluation disabled"))
    checks.append(_result("--allow-pdf-copy absent", not bool(getattr(args, "allow_pdf_copy", False)), "PDF operations disabled"))
    checks.append(_result("--allow-staging-archive absent", not bool(getattr(args, "allow_staging_archive", False)), "PDF archive disabled"))

    plan_path = resolve_plan_path(run_id=args.run_id, plan_path=args.plan_path, runs_dir=args.runs_dir)
    checks.append(_result("run_plan.json selected", plan_path is not None, str(plan_path) if plan_path else "missing"))
    if plan_path is None or not plan_path.exists():
        return False, checks, details

    run_dir = plan_path.parent
    details["run_dir"] = run_dir
    artifacts = {
        "workflow": run_dir / "workflow_plan.json",
        "stage_e": run_dir / "metadata_duplicate_resolution_preview.json",
        "simulation": run_dir / "live_workflow_simulation.json",
    }
    for label, path in artifacts.items():
        checks.append(_result(f"{label} artifact exists", path.exists(), str(path)))
    if not all(path.exists() for path in artifacts.values()):
        return False, checks, details

    run_plan = load_run_plan(plan_path)
    workflow = _load_json(artifacts["workflow"])
    stage_e = _load_json(artifacts["stage_e"])
    simulation = _load_json(artifacts["simulation"])
    details.update({"run_plan": run_plan, "workflow": workflow, "stage_e": stage_e, "simulation": simulation})

    checks.append(_result("prior simulation ok", simulation.get("simulation_ok") is True, str(simulation.get("simulation_ok"))))
    cacioppo_phase = _simulation_cacioppo_phase(simulation)
    checks.append(_result("Cacioppo simulation phase exists", cacioppo_phase is not None, "phase C"))
    if cacioppo_phase:
        checks.append(_result("Cacioppo validation passed", cacioppo_phase.get("validation_pass") is True, str(cacioppo_phase.get("validation_pass"))))
        checks.append(
            _result(
                "Cacioppo ready for parent pilot",
                cacioppo_phase.get("ready_for_future_parent_record_pilot") is True,
                str(cacioppo_phase.get("ready_for_future_parent_record_pilot")),
            )
        )
    details["cacioppo_phase"] = cacioppo_phase

    pdf_plans = {p.get("name"): p for p in run_plan.get("pdf_plans", [])}
    pdf_plan = pdf_plans.get(CACIOPPO_FILENAME)
    checks.append(_result("Cacioppo selected in run_plan", pdf_plan is not None, CACIOPPO_FILENAME))
    if not pdf_plan:
        return False, checks, details
    details["pdf_plan"] = pdf_plan
    details["not_processed"] = sorted(name for name in pdf_plans if name != CACIOPPO_FILENAME)

    path = Path(pdf_plan["path"])
    checks.append(_result("Cacioppo PDF exists in staging", path.exists(), str(path)))
    if path.exists():
        disk_hash = _sha256(path)
        checks.append(_result("Cacioppo sha256 matches", disk_hash == pdf_plan.get("sha256"), pdf_plan.get("sha256", "")))

    workflow_item = _workflow_item(workflow)
    stage_e_item = _stage_e_item(stage_e)
    checks.append(_result("Cacioppo present in workflow_plan", workflow_item is not None, "workflow_plan.json"))
    checks.append(_result("Cacioppo present in Stage E", stage_e_item is not None, "metadata_duplicate_resolution_preview.json"))
    if workflow_item and stage_e_item:
        dup = _duplicate_reassessment(stage_e_item, workflow_item)
        details["duplicate_reassessment"] = dup
        checks.append(_result("duplicate reassessment no_duplicate_signal", dup["status"] == "no_duplicate_signal", dup["status"]))

    metadata = _validated_metadata(cacioppo_phase or {})
    details["validated_metadata"] = metadata
    details["proposed_safe_id"] = CACIOPPO_ID
    checks.append(_result("proposed safe ID format", _safe_id_ok(CACIOPPO_ID), CACIOPPO_ID))

    db_path = Path(args.db_path).resolve() if args.db_path else config.DB_PATH.resolve()
    details["db_path"] = db_path
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        id_collision = _db_row_exists(conn, CACIOPPO_ID)
        row_count = _table_count(conn)
        same_work = _same_work_matches(conn)
    finally:
        conn.close()
    details["db_row_count_before_preview"] = row_count
    details["same_work_matches"] = same_work
    checks.append(_result("proposed safe ID has no DB collision", not id_collision, CACIOPPO_ID))
    checks.append(_result("no parent/child record for same work", not same_work, json.dumps(same_work, ensure_ascii=False)))

    row = _build_parent_row(metadata, pdf_plan, args.run_id)
    valid, errors = papers_db.validate_paper(row)
    details["parent_row_preview"] = row
    checks.append(_result("parent row schema preview valid", valid, "; ".join(errors) if errors else "valid"))

    return all(c["ok"] for c in checks), checks, details


def _write_artifacts(run_dir: Path, summary: dict[str, Any]) -> dict[str, str]:
    if summary.get("status") == "live_insert_completed":
        json_path = run_dir / "orchestrated_cacioppo_parent_only_live.json"
        report_path = run_dir / "orchestrated_cacioppo_parent_only_live_report.txt"
    else:
        json_path = run_dir / "orchestrated_cacioppo_parent_only_preview.json"
        report_path = run_dir / "orchestrated_cacioppo_parent_only_preview_report.txt"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(render_parent_only_report(summary), encoding="utf-8")
    return {"json": str(json_path), "report": str(report_path)}


def render_parent_only_report(summary: dict[str, Any]) -> str:
    title = (
        "Orchestrated Cacioppo Parent-Only Live Insert"
        if summary.get("status") == "live_insert_completed"
        else "Orchestrated Cacioppo Parent-Only Refusal Preview"
    )
    lines = [
        title,
        "=" * 56,
        f"Run ID: {summary.get('run_id')}",
        f"Processed only: {summary.get('processed_only')}",
        f"Status: {summary.get('status')}",
        f"Ready for live insert: {summary.get('ready_for_live_insert')}",
        f"Proposed ID: {summary.get('proposed_safe_id')}",
        "",
        "Validated metadata",
        "-" * 56,
    ]
    metadata = summary.get("validated_metadata") or {}
    for key in ("title", "authors", "year", "edition", "publisher", "isbn"):
        lines.append(f"{key}: {metadata.get(key)}")
    lines.extend(
        [
            "",
            "Safety",
            "-" * 56,
            f"DB action: {summary.get('db_action')}",
            f"PDF action: {summary.get('pdf_action')}",
            f"API/evaluation action: {summary.get('api_evaluation_action')}",
            f"ID action: {summary.get('id_action')}",
            f"Not processed: {summary.get('not_processed')}",
            "",
            f"Next command (DO NOT RUN YET): {summary.get('next_live_insert_command_do_not_run_yet')}",
        ]
    )
    return "\n".join(lines) + "\n"


def run_ingest_workflow_live_parent_only(args) -> dict[str, Any]:
    ok, checks, details = validate_parent_only_preconditions(args)
    run_dir = Path(details.get("run_dir") or (config.RUNS_DIR / (args.run_id or ALLOWED_RUN_ID)))
    lock_path: Path | None = None

    summary = {
        "created_at": _now(),
        "run_id": args.run_id,
        "stage": "orchestrated-cacioppo-parent-only",
        "processed_only": [CACIOPPO_FILENAME],
        "not_processed": details.get("not_processed", []),
        "checks": checks,
        "validated_metadata": details.get("validated_metadata"),
        "proposed_safe_id": details.get("proposed_safe_id", CACIOPPO_ID),
        "duplicate_reassessment": details.get("duplicate_reassessment"),
        "same_work_matches": details.get("same_work_matches"),
        "parent_row_preview": details.get("parent_row_preview"),
        "ready_for_live_insert": False,
        "db_action": "none",
        "pdf_action": "none",
        "api_evaluation_action": "none",
        "id_action": "none",
        "backup": None,
        "next_live_insert_command_do_not_run_yet": (
            "python pipeline\\run_corpus_pipeline.py --workflow ingest --mode A "
            "--selection-profile broader --pilot-profile safe-live --max-papers 5 "
            "--run-id 20260528-203823 --execute --live-parent-only Cacioppo --allow-db-write"
        ),
        "side_effects": {
            "api_calls": "none",
            "evaluations": "none",
            "pdf_operations": "none",
            "id_changes": "none",
        },
    }

    if not ok:
        summary["status"] = "precondition_failed"
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifact_paths": paths, "message": "Cacioppo parent-only preconditions failed."}

    if not bool(getattr(args, "allow_db_write", False)):
        summary.update(
            {
                "status": "refused_before_db_write_missing_allow_db_write",
                "ready_for_live_insert": True,
                "db_action": "none (refused before DB write; --allow-db-write absent)",
                "id_action": "none (proposed ID preview only)",
                "backup": "not created in refusal preview; fresh PRE-CACIOPPO-PARENT backup is required before live insert",
            }
        )
        paths = _write_artifacts(run_dir, summary)
        return {
            "ok": True,
            "summary": summary,
            "artifact_paths": paths,
            "message": "Cacioppo parent row is validated for future live insert; refused before DB write because --allow-db-write is absent.",
        }

    try:
        lock_path = _acquire_lock(run_dir)
        before_pdf = _pdf_snapshot()
        backup = _create_sqlite_backup()
        if not backup.get("ok"):
            raise RuntimeError("fresh PRE-CACIOPPO-PARENT backup failed validation")

        conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            before_count = _table_count(conn)
            before_digest = _non_target_digest(conn, CACIOPPO_ID)
            if _db_row_exists(conn, CACIOPPO_ID):
                conn.rollback()
                raise RuntimeError(f"{CACIOPPO_ID} already exists")
            same_work = _same_work_matches(conn)
            if same_work:
                conn.rollback()
                raise RuntimeError(f"same-work DB match exists: {same_work}")
            _insert_parent_row(conn, details["parent_row_preview"])
            conn.commit()
            read_back = _db_row_by_id(conn, CACIOPPO_ID)
            after_count = _table_count(conn)
            after_digest = _non_target_digest(conn, CACIOPPO_ID)
        finally:
            conn.close()
    except Exception as exc:
        summary.update({"status": "live_insert_failed", "error": str(exc), "backup": locals().get("backup")})
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifact_paths": paths, "message": "Cacioppo live insert failed; inspect artifact."}
    finally:
        _release_lock(lock_path)

    after_pdf = _pdf_snapshot()
    read_back_valid, read_back_errors = papers_db.validate_paper(read_back or {})
    post_write_failures: list[str] = []
    if after_count != before_count + 1:
        post_write_failures.append("DB row count did not increase by exactly one")
    if before_digest != after_digest:
        post_write_failures.append("non-target DB row digest changed")
    if not read_back or read_back.get("id") != CACIOPPO_ID:
        post_write_failures.append("new Cacioppo row did not read back by proposed ID")
    if not read_back_valid:
        post_write_failures.append(f"new Cacioppo row failed schema validation: {'; '.join(read_back_errors)}")
    if before_pdf != after_pdf:
        post_write_failures.append("PDF snapshot changed")

    summary.update(
        {
            "status": "live_insert_completed",
            "ready_for_live_insert": False,
            "row_created": read_back is not None and read_back.get("id") == CACIOPPO_ID,
            "db_action": "inserted one Cacioppo book parent row",
            "id_action": f"assigned {CACIOPPO_ID}",
            "backup": backup,
            "db_validation": {
                "before_count": before_count,
                "after_count": after_count,
                "exactly_one_row_added": after_count == before_count + 1,
                "row_read_back": read_back is not None,
                "row_schema_valid": read_back_valid,
                "non_target_digest_before": before_digest,
                "non_target_digest_after": after_digest,
                "no_existing_rows_changed": before_digest == after_digest,
                "only_new_row_id": CACIOPPO_ID if read_back and read_back.get("id") == CACIOPPO_ID else None,
            },
            "pdf_validation": {
                "before": before_pdf,
                "after": after_pdf,
                "no_pdfs_moved_copied_renamed_deleted": before_pdf == after_pdf,
            },
            "post_write_failures": post_write_failures,
            "child_chapter_records_created": [],
            "side_effects": {
                "api_calls": "none",
                "evaluations": "none",
                "pdf_operations": "none",
                "id_changes": f"assigned single new Cacioppo parent row ID only: {CACIOPPO_ID}",
                "child_chapter_records": "none",
            },
        }
    )
    paths = _write_artifacts(run_dir, summary)
    return {
        "ok": not post_write_failures,
        "summary": summary,
        "artifact_paths": paths,
        "message": "Cacioppo parent row inserted." if not post_write_failures else "Cacioppo row inserted but post-write validation failed; inspect artifact.",
    }
