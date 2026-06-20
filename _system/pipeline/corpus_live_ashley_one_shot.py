"""Ashley/Timmers one-shot parent lifecycle live workflow.

This is the first promoted `--workflow ingest --execute --workflow-target Ashley`
path. It is intentionally narrow: Ashley only, no API calls, no evaluation, and
no child/chapter records.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
import papers_db
from corpus_execute_guard import load_run_plan, resolve_plan_path
from corpus_general_parent_stages import (
    run_general_book_toc_chapter_preview_item,
    run_general_parent_validation_preview,
)

RUN_ID = "20260528-214737"
TARGET_ALIAS = "Ashley"
ASHLEY_FILENAME = "Ashley, Richard(Editor)_Timmers, Renee(Contributor) - The Ro.pdf"
ASHLEY_ID = "AshleyTimmers_2017_RoutledgeCompanion_book"
ASHLEY_TITLE = "The Routledge Companion to Music Cognition"
ASHLEY_EDITORS = ["Richard Ashley", "Renee Timmers"]
ASHLEY_YEAR = "2017"
ASHLEY_PUBLISHER = "Routledge"
MANAGED_FILENAME = f"{ASHLEY_ID}.pdf"
MANAGED_RELATIVE_PATH = f"source-pdfs/{MANAGED_FILENAME}"


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


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return val if isinstance(val, dict) else {}


def _table_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])


def _row_exists(conn: sqlite3.Connection, paper_id: str) -> bool:
    return conn.execute("SELECT 1 FROM papers WHERE id = ? LIMIT 1", (paper_id,)).fetchone() is not None


def _row_raw(conn: sqlite3.Connection, paper_id: str) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM papers WHERE id = ? LIMIT 1", (paper_id,)).fetchone()
    return dict(row) if row else None


def _row_decoded(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    out = dict(raw)
    for key in papers_db.JSON_FIELDS:
        if key in out:
            out[key] = _json_obj(out.get(key))
    return out


def _same_work_matches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, document_type
        FROM papers
        WHERE id != ?
          AND (
            basic_metadata LIKE ?
            OR file_info LIKE ?
            OR file_info LIKE ?
          )
        ORDER BY id
        LIMIT 10
        """,
        (
            ASHLEY_ID,
            f"%{ASHLEY_TITLE}%",
            f"%{ASHLEY_FILENAME}%",
            "%a65cf17b39a8baa82c1a2d32b5b224fdaa34dd89633bca65ead0c21b83ba80c7%",
        ),
    ).fetchall()
    return [{"id": row["id"], "document_type": row["document_type"]} for row in rows]


def _db_digest(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    cols = papers_db.ALL_COLUMNS
    h = hashlib.sha256()
    for row in conn.execute(f"SELECT {','.join(cols)} FROM papers ORDER BY id"):
        vals = [row[col] for col in cols]
        encoded = json.dumps(vals, ensure_ascii=False, default=str).encode("utf-8")
        h.update(len(encoded).to_bytes(8, "big"))
        h.update(encoded)
    return {"count": _table_count(conn), "digest": h.hexdigest()}


def _non_target_digest(conn: sqlite3.Connection) -> str:
    conn.row_factory = sqlite3.Row
    cols = papers_db.ALL_COLUMNS
    h = hashlib.sha256()
    for row in conn.execute(f"SELECT {','.join(cols)} FROM papers WHERE id != ? ORDER BY id", (ASHLEY_ID,)):
        vals = [row[col] for col in cols]
        encoded = json.dumps(vals, ensure_ascii=False, default=str).encode("utf-8")
        h.update(len(encoded).to_bytes(8, "big"))
        h.update(encoded)
    return h.hexdigest()


def _pdf_snapshot() -> dict[str, Any]:
    entries = {}
    for root in (config.STAGING, config.SOURCE_PDFS):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.pdf")):
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = str(path.relative_to(config.BASE))
            entries[rel] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest()
    return {"count": len(entries), "digest": digest, "entries": entries}


def _snapshot_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    b = before.get("entries", {})
    a = after.get("entries", {})
    return {
        "added": sorted(set(a) - set(b)),
        "removed": sorted(set(b) - set(a)),
        "changed": sorted(k for k in set(a) & set(b) if a[k] != b[k]),
    }


def _create_sqlite_backup(label: str) -> dict[str, Any]:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = config.BACKUP_DIR / f"papers-{ts}-{label}.db"
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
    ok = dest.exists() and size > 1024 * 1024 and quick_check == "ok"
    return {"ok": ok, "path": str(dest), "bytes": size, "quick_check": quick_check, "row_count": row_count}


def _managed_path() -> Path:
    dest = (config.SOURCE_PDFS / MANAGED_FILENAME).resolve()
    if dest.parent != config.SOURCE_PDFS.resolve():
        raise RuntimeError(f"unsafe managed PDF destination: {dest}")
    return dest


def _archive_destination() -> Path:
    base = (config.STAGING / "processed" / ASHLEY_ID).resolve()
    dest = (base / ASHLEY_FILENAME).resolve()
    if dest.parent != base or not str(base).startswith(str(config.STAGING.resolve())):
        raise RuntimeError(f"unsafe archive destination: {dest}")
    return dest


def _acquire_lock(run_dir: Path) -> tuple[Path, Path]:
    global_lock = config.RUNS_DIR / ".corpus_pipeline.lock"
    run_lock = run_dir / "orchestrated_ashley_one_shot.lock"
    payload = json.dumps(
        {
            "created_at": _now(),
            "stage": "orchestrated-ashley-one-shot-live-workflow",
            "run_id": run_dir.name,
            "process_only": ASHLEY_FILENAME,
            "pid": os.getpid(),
        },
        indent=2,
    )
    fd = os.open(str(global_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        fd = os.open(str(run_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        if global_lock.exists():
            global_lock.unlink()
        raise
    return global_lock, run_lock


def _release_locks(locks: tuple[Path, Path] | None) -> None:
    if not locks:
        return
    for lock_path in reversed(locks):
        if lock_path.exists():
            lock_path.unlink()


def _workflow_item(workflow: dict[str, Any]) -> dict[str, Any] | None:
    for item in workflow.get("items", []):
        if item.get("name") == ASHLEY_FILENAME:
            return item
    return None


def _stage_e_item(stage_e: dict[str, Any]) -> dict[str, Any] | None:
    for item in stage_e.get("items", []):
        if item.get("name") == ASHLEY_FILENAME:
            return item
    return None


def _ashley_simulation_phase(simulation: dict[str, Any]) -> dict[str, Any] | None:
    for phase in simulation.get("phases", []):
        if phase.get("filename") == ASHLEY_FILENAME:
            return phase
    return None


def _build_parent_row(pdf_plan: dict[str, Any], validation: dict[str, Any], run_id: str) -> dict[str, Any]:
    metadata = validation.get("candidate_parent_metadata") or {}
    isbn = [
        value
        for value in metadata.get("isbn_candidates", [])
        if isinstance(value, str) and value.replace("-", "").isdigit() and len(value.replace("-", "")) in {10, 13}
    ][:4]
    file_info = {
        "original_filename": ASHLEY_FILENAME,
        "staged_path": pdf_plan["path"],
        "staged_sha256": pdf_plan.get("sha256"),
        "filepath": None,
        "renamed_filename": None,
        "managed_pdf_action": "not_copied_or_moved_in_parent_record_stage",
    }
    return {
        "id": ASHLEY_ID,
        "timestamp_added": _now(),
        "document_type": "book",
        "basic_metadata": {
            "title": ASHLEY_TITLE,
            "authors": ASHLEY_EDITORS,
            "editors": ASHLEY_EDITORS,
            "year": ASHLEY_YEAR,
            "edition": None,
            "publisher": ASHLEY_PUBLISHER,
            "doi": None,
            "isbn": isbn,
            "source_note": "Created by Ashley/Timmers one-shot parent ingest after generalized validation.",
        },
        "identifiers": {"doi": None, "isbn": isbn, "pubmed_id": None, "orcid_ids": [], "issn": None},
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
            "created_by": "orchestrated-ashley-one-shot-live-workflow",
            "source_run_id": run_id,
            "parent_candidate": "Ashley/Timmers",
            "child_chapter_policy": "deferred; no child/chapter records created",
            "evaluation_policy": "not evaluated in parent lifecycle pilot",
        },
        "classification": {
            "document_type": "book",
            "evaluation_status": "deferred_not_evaluated_for_book_parent",
            "rating": None,
            "red_team_survival": None,
            "route_status": "created_by_ashley_one_shot_live_workflow",
        },
        "tags": ["edited_volume_parent_candidate", "guideline_mismatch_or_new_guideline_needed_candidate"],
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


def _file_info_patch(existing: dict[str, Any], managed_path: Path, staged_hash: str) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = dict(existing)
    changed = {
        "filepath": MANAGED_RELATIVE_PATH,
        "renamed_filename": MANAGED_FILENAME,
        "managed_pdf_filename": MANAGED_FILENAME,
        "managed_pdf_relative_path": MANAGED_RELATIVE_PATH,
        "managed_pdf_absolute_path": str(managed_path),
        "managed_pdf_sha256": staged_hash,
        "managed_pdf_attached_at": _now(),
        "managed_pdf_attachment_stage": "orchestrated-ashley-one-shot-live-workflow",
        "managed_pdf_action": "copied_then_attached_in_one_shot_workflow",
    }
    updated.update(changed)
    return updated, changed


def _write_artifacts(run_dir: Path, summary: dict[str, Any]) -> dict[str, str]:
    json_path = run_dir / "orchestrated_ashley_one_shot_live.json"
    report_path = run_dir / "orchestrated_ashley_one_shot_live_report.txt"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(render_ashley_one_shot_report(summary), encoding="utf-8")
    return {"json": str(json_path), "report": str(report_path)}


def render_ashley_one_shot_report(summary: dict[str, Any]) -> str:
    lines = [
        "Orchestrated Ashley/Timmers One-Shot Live Workflow",
        "=" * 58,
        f"Run ID: {summary.get('run_id')}",
        f"Status: {summary.get('status')}",
        f"Processed only: {summary.get('processed_only')}",
        f"Parent ID: {summary.get('parent_id')}",
        "",
        "Phases",
        "-" * 58,
    ]
    for phase in summary.get("phases", []):
        lines.append(f"- {phase['phase']}: {phase['status']}")
    lines.extend(
        [
            "",
            "Safety",
            "-" * 58,
            f"DB action: {summary.get('db_action')}",
            f"PDF action: {summary.get('pdf_action')}",
            f"API/evaluation action: {summary.get('api_evaluation_action')}",
            f"Child/chapter records: {summary.get('child_chapter_records')}",
        ]
    )
    return "\n".join(lines) + "\n"


def run_ashley_one_shot_live_workflow(args) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []
    run_dir = config.RUNS_DIR / (args.run_id or RUN_ID)
    summary: dict[str, Any] = {
        "created_at": _now(),
        "run_id": args.run_id,
        "stage": "orchestrated-ashley-one-shot-live-workflow",
        "processed_only": [ASHLEY_FILENAME],
        "not_processed": [],
        "parent_id": ASHLEY_ID,
        "checks": checks,
        "phases": phases,
        "db_action": "none",
        "pdf_action": "none",
        "api_evaluation_action": "none",
        "child_chapter_records": "none",
        "side_effects": {"api_calls": "none", "evaluations": "none", "child_chapter_records": "none"},
    }

    def fail(status: str, message: str) -> dict[str, Any]:
        summary["status"] = status
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifact_paths": paths, "message": message}

    plan_path = resolve_plan_path(run_id=args.run_id, plan_path=args.plan_path, runs_dir=args.runs_dir)
    checks.extend(
        [
            _result("--workflow ingest", getattr(args, "workflow", None) == "ingest", f"workflow={getattr(args, 'workflow', None)!r}"),
            _result("--execute present", bool(getattr(args, "execute", False)), "required"),
            _result("--workflow-target Ashley", getattr(args, "workflow_target", "") == TARGET_ALIAS, f"target={getattr(args, 'workflow_target', '')!r}"),
            _result("--run-id exact", getattr(args, "run_id", "") == RUN_ID, f"run_id={getattr(args, 'run_id', '')!r}"),
            _result("--mode A", getattr(args, "mode", None) == "A", f"mode={getattr(args, 'mode', None)!r}"),
            _result("--selection-profile broader", getattr(args, "selection_profile", "") == "broader", f"selection_profile={getattr(args, 'selection_profile', '')!r}"),
            _result("--pilot-profile safe-live", getattr(args, "pilot_profile", "") == "safe-live", f"pilot_profile={getattr(args, 'pilot_profile', '')!r}"),
            _result("--max-papers 5", getattr(args, "max_papers", None) == 5, f"max_papers={getattr(args, 'max_papers', None)!r}"),
            _result("--allow-paid-api absent", not bool(getattr(args, "allow_paid_api", False)), "API/evaluation disabled"),
            _result("--allow-db-write present", bool(getattr(args, "allow_db_write", False)), "required for parent insert and file_info attach"),
            _result("--allow-pdf-copy present", bool(getattr(args, "allow_pdf_copy", False)), "required for managed PDF copy"),
            _result("--allow-staging-archive present", bool(getattr(args, "allow_staging_archive", False)), "required for staging archive"),
            _result("run_plan.json selected", plan_path is not None and plan_path.exists() if plan_path else False, str(plan_path) if plan_path else "missing"),
        ]
    )
    if not all(c["ok"] for c in checks):
        phases.append({"phase": "preflight", "status": "failed"})
        return fail("precondition_failed", "Ashley one-shot preconditions failed before side effects.")

    run_dir = plan_path.parent
    summary["run_id"] = run_dir.name
    artifacts = {
        "workflow": run_dir / "workflow_plan.json",
        "stage_e": run_dir / "metadata_duplicate_resolution_preview.json",
        "simulation": run_dir / "live_workflow_simulation.json",
    }
    for label, path in artifacts.items():
        checks.append(_result(f"{label} artifact exists", path.exists(), str(path)))
    if not all(c["ok"] for c in checks):
        phases.append({"phase": "preflight", "status": "failed"})
        return fail("precondition_failed", "Ashley artifacts missing before side effects.")

    run_plan = load_run_plan(plan_path)
    workflow = _load_json(artifacts["workflow"])
    stage_e = _load_json(artifacts["stage_e"])
    simulation = _load_json(artifacts["simulation"])
    pdf_by_name = {p.get("name"): p for p in run_plan.get("pdf_plans", [])}
    pdf_plan = pdf_by_name.get(ASHLEY_FILENAME)
    workflow_item = _workflow_item(workflow)
    stage_e_item = _stage_e_item(stage_e)
    simulation_phase = _ashley_simulation_phase(simulation)
    summary["not_processed"] = sorted(name for name in pdf_by_name if name != ASHLEY_FILENAME)
    checks.extend(
        [
            _result("Ashley selected in run_plan", pdf_plan is not None, ASHLEY_FILENAME),
            _result("Ashley present in workflow_plan", workflow_item is not None, "workflow_plan.json"),
            _result("Ashley present in Stage E", stage_e_item is not None, "metadata_duplicate_resolution_preview.json"),
            _result("Ashley simulation phase exists", simulation_phase is not None, "live_workflow_simulation.json"),
        ]
    )
    if not pdf_plan or not workflow_item or not stage_e_item or not simulation_phase:
        phases.append({"phase": "preflight", "status": "failed"})
        return fail("precondition_failed", "Ashley plan artifacts failed validation before side effects.")

    source = Path(pdf_plan["path"])
    managed = _managed_path()
    archive_dest = _archive_destination()
    source_hash = _sha256(source) if source.exists() else ""
    checks.extend(
        [
            _result("Ashley source PDF exists in staging", source.exists(), str(source)),
            _result("Ashley source hash matches run plan", source_hash == pdf_plan.get("sha256"), str(pdf_plan.get("sha256"))),
            _result("managed destination absent", not managed.exists(), str(managed)),
            _result("archive destination absent", not archive_dest.exists(), str(archive_dest)),
            _result("simulation validation passed", simulation_phase.get("validation_pass") is True, str(simulation_phase.get("validation_pass"))),
            _result("simulation one-shot ready", simulation_phase.get("ready_for_one_shot_live_workflow") is True, str(simulation_phase.get("ready_for_one_shot_live_workflow"))),
        ]
    )
    duplicate = (stage_e_item.get("duplicate_version_resolution") or {})
    checks.append(
        _result(
            "duplicate reassessment no_duplicate_signal",
            duplicate.get("decision") == "book_or_edited_volume_candidate" and not duplicate.get("matched_rows"),
            json.dumps(duplicate, ensure_ascii=False),
        )
    )
    if not all(c["ok"] for c in checks):
        phases.append({"phase": "preflight", "status": "failed"})
        return fail("precondition_failed", "Ashley preflight failed before side effects.")
    phases.append({"phase": "preflight", "status": "passed"})

    locks: tuple[Path, Path] | None = None
    try:
        locks = _acquire_lock(run_dir)
        stage_g = run_general_book_toc_chapter_preview_item(pdf_plan=pdf_plan, stage_e_item=stage_e_item)
        validation = run_general_parent_validation_preview(
            pdf_plan=pdf_plan,
            stage_g_item=stage_g,
            stage_e_item=stage_e_item,
            workflow_route=workflow_item.get("route") or {},
            db_path=config.DB_PATH.resolve(),
        )
        summary["stage_g_general"] = stage_g
        summary["parent_validation_general"] = validation
        if validation.get("validation_pass") is not True or validation.get("ready_for_future_parent_record_pilot") is not True:
            phases.append({"phase": "parent_validation", "status": "failed"})
            return fail("parent_validation_failed", "Ashley live validation failed; stopped before side effects.")
        phases.append({"phase": "parent_validation", "status": "passed"})

        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        try:
            id_collision = _row_exists(conn, ASHLEY_ID)
            same_work = _same_work_matches(conn)
            before_insert_count = _table_count(conn)
            before_insert_non_target_digest = _non_target_digest(conn)
        finally:
            conn.close()
        checks.extend(
            [
                _result("Ashley parent ID has no DB collision", not id_collision, ASHLEY_ID),
                _result("no existing same-work DB match", not same_work, json.dumps(same_work, ensure_ascii=False)),
            ]
        )
        if id_collision or same_work:
            phases.append({"phase": "parent_record_create", "status": "failed"})
            return fail("parent_insert_precondition_failed", "Ashley DB collision/same-work validation failed; stopped before write.")

        parent_row = _build_parent_row(pdf_plan, validation, run_dir.name)
        valid, errors = papers_db.validate_paper(parent_row)
        checks.append(_result("Ashley parent row schema valid", valid, "; ".join(errors) if errors else "valid"))
        if not valid:
            phases.append({"phase": "parent_record_create", "status": "failed"})
            return fail("parent_row_schema_failed", "Ashley parent row schema validation failed; stopped before write.")

        before_parent_pdf = _pdf_snapshot()
        parent_backup = _create_sqlite_backup("PRE-ASHLEY-PARENT")
        if not parent_backup.get("ok"):
            phases.append({"phase": "parent_record_create", "status": "failed"})
            return fail("parent_backup_failed", "Ashley parent backup failed; stopped before DB write.")
        conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            if _row_exists(conn, ASHLEY_ID):
                conn.rollback()
                raise RuntimeError(f"{ASHLEY_ID} already exists")
            _insert_parent_row(conn, parent_row)
            conn.commit()
            read_back = _row_decoded(_row_raw(conn, ASHLEY_ID))
            after_insert_count = _table_count(conn)
            after_insert_non_target_digest = _non_target_digest(conn)
        finally:
            conn.close()
        after_parent_pdf = _pdf_snapshot()
        read_back_valid, read_back_errors = papers_db.validate_paper(read_back or {})
        parent_failures = []
        if after_insert_count != before_insert_count + 1:
            parent_failures.append("DB row count did not increase by exactly one")
        if before_insert_non_target_digest != after_insert_non_target_digest:
            parent_failures.append("non-target DB rows changed during parent insert")
        if before_parent_pdf != after_parent_pdf:
            parent_failures.append("PDF snapshot changed during parent insert")
        if not read_back_valid:
            parent_failures.append(f"read-back row invalid: {'; '.join(read_back_errors)}")
        if parent_failures:
            phases.append({"phase": "parent_record_create", "status": "failed"})
            summary["parent_insert_failures"] = parent_failures
            return fail("parent_insert_postcheck_failed", "Ashley parent insert post-check failed; stopped.")
        phases.append({"phase": "parent_record_create", "status": "completed"})
        summary["db_action"] = "inserted one Ashley/Timmers book parent row"
        summary["parent_backup"] = parent_backup

        before_copy_db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        try:
            before_copy_db_digest = _db_digest(before_copy_db)
        finally:
            before_copy_db.close()
        before_copy_pdf = _pdf_snapshot()
        config.SOURCE_PDFS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, managed)
        managed_hash = _sha256(managed)
        after_copy_pdf = _pdf_snapshot()
        after_copy_db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        try:
            after_copy_db_digest = _db_digest(after_copy_db)
        finally:
            after_copy_db.close()
        copy_delta = _snapshot_delta(before_copy_pdf, after_copy_pdf)
        expected_added = str(managed.relative_to(config.BASE))
        copy_failures = []
        if managed_hash != source_hash:
            copy_failures.append("managed PDF hash does not match source")
        if copy_delta != {"added": [expected_added], "removed": [], "changed": []}:
            copy_failures.append(f"unexpected PDF delta during managed copy: {copy_delta}")
        if before_copy_db_digest != after_copy_db_digest:
            copy_failures.append("papers.db changed during managed PDF copy")
        if copy_failures:
            phases.append({"phase": "managed_pdf_copy", "status": "failed"})
            summary["managed_pdf_copy_failures"] = copy_failures
            return fail("managed_pdf_copy_postcheck_failed", "Ashley managed PDF copy post-check failed; stopped.")
        phases.append({"phase": "managed_pdf_copy", "status": "completed"})
        summary["pdf_action"] = "copied one Ashley staged PDF to managed source-pdfs destination"

        fileinfo_backup = _create_sqlite_backup("PRE-ASHLEY-FILEINFO")
        if not fileinfo_backup.get("ok"):
            phases.append({"phase": "fileinfo_attach", "status": "failed"})
            return fail("fileinfo_backup_failed", "Ashley file_info backup failed; stopped before DB update.")
        conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            before_fileinfo_count = _table_count(conn)
            before_fileinfo_non_target_digest = _non_target_digest(conn)
            raw_row = _row_raw(conn, ASHLEY_ID)
            decoded = _row_decoded(raw_row)
            existing_file_info = (decoded or {}).get("file_info") or {}
            if existing_file_info.get("filepath"):
                conn.rollback()
                raise RuntimeError("Ashley file_info already has managed filepath")
            updated_file_info, changed_file_info = _file_info_patch(existing_file_info, managed, source_hash)
            conn.execute("UPDATE papers SET file_info = ? WHERE id = ?", (json.dumps(updated_file_info, ensure_ascii=False), ASHLEY_ID))
            conn.commit()
            read_back_fileinfo = _row_decoded(_row_raw(conn, ASHLEY_ID))
            after_fileinfo_count = _table_count(conn)
            after_fileinfo_non_target_digest = _non_target_digest(conn)
        finally:
            conn.close()
        file_info = (read_back_fileinfo or {}).get("file_info") or {}
        fileinfo_failures = []
        if after_fileinfo_count != before_fileinfo_count:
            fileinfo_failures.append("DB row count changed during file_info attach")
        if before_fileinfo_non_target_digest != after_fileinfo_non_target_digest:
            fileinfo_failures.append("non-target DB rows changed during file_info attach")
        if file_info.get("filepath") != MANAGED_RELATIVE_PATH or file_info.get("managed_pdf_sha256") != source_hash:
            fileinfo_failures.append("file_info read-back did not match managed PDF attachment")
        if fileinfo_failures:
            phases.append({"phase": "fileinfo_attach", "status": "failed"})
            summary["fileinfo_failures"] = fileinfo_failures
            return fail("fileinfo_attach_postcheck_failed", "Ashley file_info attach post-check failed; stopped.")
        phases.append({"phase": "fileinfo_attach", "status": "completed"})
        summary["fileinfo_backup"] = fileinfo_backup
        summary["file_info_patch"] = changed_file_info
        summary["db_action"] = "inserted one Ashley parent row and updated only its file_info"

        before_archive_db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        try:
            before_archive_db_digest = _db_digest(before_archive_db)
        finally:
            before_archive_db.close()
        before_archive_pdf = _pdf_snapshot()
        archive_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(archive_dest))
        archive_hash = _sha256(archive_dest)
        after_archive_pdf = _pdf_snapshot()
        after_archive_db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        try:
            after_archive_db_digest = _db_digest(after_archive_db)
        finally:
            after_archive_db.close()
        archive_delta = _snapshot_delta(before_archive_pdf, after_archive_pdf)
        expected_removed = str(source.relative_to(config.BASE))
        expected_archive_added = str(archive_dest.relative_to(config.BASE))
        archive_failures = []
        if archive_hash != source_hash:
            archive_failures.append("archive hash does not match source")
        if archive_delta != {"added": [expected_archive_added], "removed": [expected_removed], "changed": []}:
            archive_failures.append(f"unexpected PDF delta during staging archive: {archive_delta}")
        if before_archive_db_digest != after_archive_db_digest:
            archive_failures.append("papers.db changed during staging archive")
        if archive_failures:
            phases.append({"phase": "staging_archive", "status": "failed"})
            summary["archive_failures"] = archive_failures
            return fail("staging_archive_postcheck_failed", "Ashley staging archive post-check failed; inspect artifacts.")
        phases.append({"phase": "staging_archive", "status": "completed"})
        summary["pdf_action"] = "copied one managed PDF and archived one staged Ashley PDF"

        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        try:
            final_row = _row_decoded(_row_raw(conn, ASHLEY_ID))
        finally:
            conn.close()
        completion_checks = {
            "db_row_exists": final_row is not None,
            "managed_pdf_exists": managed.exists(),
            "managed_pdf_hash_ok": managed.exists() and _sha256(managed) == source_hash,
            "staged_pdf_absent": not source.exists(),
            "archive_pdf_exists": archive_dest.exists(),
            "archive_pdf_hash_ok": archive_dest.exists() and _sha256(archive_dest) == source_hash,
            "file_info_attached": ((final_row or {}).get("file_info") or {}).get("filepath") == MANAGED_RELATIVE_PATH,
            "evaluation_deferred": ((final_row or {}).get("classification") or {}).get("evaluation_status") == "deferred_not_evaluated_for_book_parent",
        }
        phases.append({"phase": "completion_report", "status": "passed" if all(completion_checks.values()) else "failed"})
        summary.update(
            {
                "status": "live_workflow_completed" if all(completion_checks.values()) else "completion_check_failed",
                "source_staged_pdf": str(source),
                "managed_pdf": str(managed),
                "archive_pdf": str(archive_dest),
                "source_sha256": source_hash,
                "managed_sha256": managed_hash,
                "archive_sha256": archive_hash,
                "completion_checks": completion_checks,
                "api_evaluation_action": "none",
                "child_chapter_records": "none",
                "side_effects": {
                    "db_writes": "one parent row insert plus one file_info update on that row",
                    "pdf_operations": "one managed copy plus one staging archive move",
                    "api_calls": "none",
                    "evaluations": "none",
                    "child_chapter_records": "none",
                },
            }
        )
        paths = _write_artifacts(run_dir, summary)
        return {
            "ok": summary["status"] == "live_workflow_completed",
            "summary": summary,
            "artifact_paths": paths,
            "message": "Ashley one-shot live workflow completed." if summary["status"] == "live_workflow_completed" else "Ashley completion check failed; inspect artifact.",
        }
    except Exception as exc:
        summary.update({"status": "live_workflow_failed", "error": str(exc)})
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifact_paths": paths, "message": "Ashley one-shot live workflow failed; inspect artifact."}
    finally:
        _release_locks(locks)
