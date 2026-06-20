"""Stage M: attach Koren managed PDF metadata to the existing parent row."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
import papers_db
from corpus_stage_j_koren_validation import KOREN_NAME
from corpus_stage_k_koren_live_parent import KOREN_HASH, KOREN_ID, EXPECTED_EDITORS

MANAGED_FILENAME = f"{KOREN_ID}.pdf"
MANAGED_RELATIVE_PATH = f"source-pdfs/{MANAGED_FILENAME}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"Required artifact missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _koren_plan(plan: dict[str, Any]) -> dict[str, Any]:
    matches = [p for p in plan.get("pdf_plans", []) if p.get("name") == KOREN_NAME]
    if len(matches) != 1:
        raise RuntimeError("Koren PDF plan not found exactly once")
    return matches[0]


def _table_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])


def _row_raw(conn: sqlite3.Connection, paper_id: str) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    return dict(row) if row else None


def _row_decoded(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    out = dict(raw)
    for key in papers_db.JSON_FIELDS:
        if key in out:
            out[key] = _json_obj(out.get(key))
    return out


def _non_target_digest(conn: sqlite3.Connection) -> str:
    conn.row_factory = sqlite3.Row
    h = hashlib.sha256()
    cols = papers_db.ALL_COLUMNS
    for row in conn.execute(f"SELECT {','.join(cols)} FROM papers WHERE id != ? ORDER BY id", (KOREN_ID,)):
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
                st = path.stat()
            except OSError:
                continue
            rel = str(path.relative_to(config.BASE))
            entries[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest()
    return {"count": len(entries), "digest": digest}


def _create_backup() -> dict[str, Any]:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = config.BACKUP_DIR / f"papers-{ts}-PRE-KOREN-FILEINFO.db"
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
    return {
        "ok": ok,
        "path": str(dest),
        "bytes": size,
        "quick_check": quick_check,
        "row_count": row_count,
        "detail": "fresh PRE-KOREN-FILEINFO backup created and verified" if ok else "backup failed validation",
    }


def _acquire_lock(run_dir: Path) -> Path:
    lock_path = run_dir.parent / ".corpus_pipeline.lock"
    payload = json.dumps(
        {
            "stage": "live-koren-fileinfo-attach-only",
            "run_id": run_dir.name,
            "created_at": _now(),
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


def _future_commands(run_id: str) -> list[str]:
    return [
        f"DO NOT RUN YET: python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 --run-id {run_id} --execute-stage live-koren-staging-archive-only",
        f"DO NOT RUN YET: python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 --run-id {run_id} --execute-stage harris-machine-escalation-preview",
        "DO NOT RUN YET: python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 --execute-stage broader-ingest-pilot",
    ]


def _file_info_patch(existing: dict[str, Any], managed_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = dict(existing)
    changed = {
        "filepath": MANAGED_RELATIVE_PATH,
        "renamed_filename": MANAGED_FILENAME,
        "managed_pdf_filename": MANAGED_FILENAME,
        "managed_pdf_relative_path": MANAGED_RELATIVE_PATH,
        "managed_pdf_absolute_path": str(managed_path),
        "managed_pdf_sha256": KOREN_HASH,
        "managed_pdf_attached_at": _now(),
        "managed_pdf_attachment_stage": "live-koren-fileinfo-attach-only",
        "managed_pdf_action": "copied_in_stage_l_attached_in_stage_m",
    }
    updated.update(changed)
    return updated, changed


def _preconditions(plan: dict[str, Any], run_dir: Path) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    stage_k = _read_json(run_dir / "live_koren_parent_record_only.json")
    stage_l = _read_json(run_dir / "live_koren_pdf_copy_only.json")
    pdf_plan = _koren_plan(plan)
    staged = Path(pdf_plan["path"])
    managed = config.SOURCE_PDFS / MANAGED_FILENAME
    if not (stage_k.get("row_created") and stage_k.get("row_id") == KOREN_ID):
        failures.append("Stage K artifact does not show created Koren parent row")
    if not (stage_l.get("post_copy_validation", {}).get("ok") and stage_l.get("managed_copied_pdf_hash") == KOREN_HASH):
        failures.append("Stage L artifact does not show a valid managed Koren PDF copy")
    if not staged.exists():
        failures.append(f"Staged Koren PDF missing: {staged}")
    if not managed.exists():
        failures.append(f"Managed Koren PDF missing: {managed}")
    staged_hash = _sha256(staged) if staged.exists() else ""
    managed_hash = _sha256(managed) if managed.exists() else ""
    if staged_hash != KOREN_HASH:
        failures.append("Staged Koren PDF hash mismatch")
    if managed_hash != KOREN_HASH:
        failures.append("Managed Koren PDF hash mismatch")
    return failures, {
        "stage_k": stage_k,
        "stage_l": stage_l,
        "staged": staged,
        "managed": managed,
        "staged_hash": staged_hash,
        "managed_hash": managed_hash,
    }


def _read_back_ok(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    bm = row.get("basic_metadata") or {}
    fi = row.get("file_info") or {}
    cl = row.get("classification") or {}
    return (
        row.get("id") == KOREN_ID
        and row.get("document_type") == "book"
        and bm.get("title") == "Handbook of Emotions"
        and bm.get("authors") == EXPECTED_EDITORS
        and str(bm.get("year")) == "2016"
        and bm.get("publisher") == "The Guilford Press"
        and "978-1-4625-2534-8" in (bm.get("isbn") or [])
        and bm.get("edition") == "Fourth Edition"
        and cl.get("evaluation_status") == "deferred_not_evaluated_for_book_parent"
        and fi.get("original_filename") == KOREN_NAME
        and fi.get("staged_sha256") == KOREN_HASH
        and fi.get("filepath") == MANAGED_RELATIVE_PATH
        and fi.get("renamed_filename") == MANAGED_FILENAME
        and fi.get("managed_pdf_sha256") == KOREN_HASH
    )


def _failure_summary(run_dir: Path, failures: list[str], backup: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "run_id": run_dir.name,
        "stage": "live-koren-fileinfo-attach-only",
        "processed_only": [KOREN_ID],
        "row_updated": False,
        "backup": backup,
        "post_write_validation": {"ok": False, "failures": failures},
        "recovery_notes": [
            "No automatic retry or broad rollback was attempted.",
            "Inspect this artifact, the backup, and the Koren row before rerunning.",
        ],
        "future_commands_preview_do_not_run_yet": _future_commands(run_dir.name),
        "side_effects": {
            "api_calls": "none",
            "evaluations": "none",
            "new_db_rows": "none",
            "pdf_moves_copies_renames_deletes": "none",
            "child_chapter_records_created": "none",
        },
    }


def run_live_koren_fileinfo_attach_only(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    failures, ctx = _preconditions(plan, run_dir)
    if failures:
        summary = _failure_summary(run_dir, failures)
        _write_reports(run_dir, summary)
        return summary

    lock_path: Path | None = None
    conn: sqlite3.Connection | None = None
    backup = None
    try:
        lock_path = _acquire_lock(run_dir)
        before_pdf = _pdf_snapshot()
        backup = _create_backup()
        if not backup.get("ok"):
            raise RuntimeError("Fresh PRE-KOREN-FILEINFO backup failed validation")
        conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        before_count = _table_count(conn)
        before_non_target = _non_target_digest(conn)
        before_raw = _row_raw(conn, KOREN_ID)
        if before_raw is None:
            raise RuntimeError(f"{KOREN_ID} missing before file_info update")
        before_decoded = _row_decoded(before_raw)
        file_info_before = before_decoded.get("file_info") if before_decoded else {}
        file_info_after, changed_fields = _file_info_patch(file_info_before or {}, ctx["managed"])
        conn.execute(
            "UPDATE papers SET file_info = ? WHERE id = ?",
            (json.dumps(file_info_after, ensure_ascii=False), KOREN_ID),
        )
        conn.commit()
        after_count = _table_count(conn)
        after_non_target = _non_target_digest(conn)
        after_raw = _row_raw(conn, KOREN_ID)
        after_decoded = _row_decoded(after_raw)
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        summary = _failure_summary(run_dir, [str(e)], backup)
        _write_reports(run_dir, summary)
        return summary
    finally:
        if conn is not None:
            conn.close()
        _release_lock(lock_path)

    after_pdf = _pdf_snapshot()
    changed_columns = [
        col
        for col in papers_db.ALL_COLUMNS
        if (before_raw or {}).get(col) != (after_raw or {}).get(col)
    ]
    validation_failures = []
    if before_count != 3373 or after_count != 3373:
        validation_failures.append(f"DB row count was not stable at 3373: {before_count}->{after_count}")
    if changed_columns != ["file_info"]:
        validation_failures.append(f"Unexpected target row changed columns: {changed_columns}")
    if before_non_target != after_non_target:
        validation_failures.append("Non-target row digest changed")
    if before_pdf != after_pdf:
        validation_failures.append("PDF snapshot changed during DB-only attach stage")
    if not _read_back_ok(after_decoded):
        validation_failures.append("Koren row did not read back cleanly after file_info update")
    if not ctx["managed"].exists() or _sha256(ctx["managed"]) != KOREN_HASH:
        validation_failures.append("Managed PDF missing or hash mismatch after DB update")
    if not ctx["staged"].exists() or _sha256(ctx["staged"]) != KOREN_HASH:
        validation_failures.append("Staged PDF missing or hash mismatch after DB update")

    summary = {
        "run_id": run_dir.name,
        "stage": "live-koren-fileinfo-attach-only",
        "processed_only": [KOREN_ID],
        "backup": backup,
        "row_updated": not validation_failures,
        "row_id": KOREN_ID,
        "file_info_fields_added_or_changed": changed_fields,
        "file_info_before": file_info_before,
        "file_info_after": (after_decoded or {}).get("file_info"),
        "db_validation": {
            "before_count": before_count,
            "after_count": after_count,
            "row_count_remained_3373": before_count == 3373 and after_count == 3373,
            "changed_columns_for_target": changed_columns,
            "only_target_file_info_changed": changed_columns == ["file_info"],
            "non_target_digest_before": before_non_target,
            "non_target_digest_after": after_non_target,
            "no_non_target_rows_changed": before_non_target == after_non_target,
            "row_read_back_cleanly": _read_back_ok(after_decoded),
        },
        "pdf_validation": {
            "before": before_pdf,
            "after": after_pdf,
            "no_pdfs_moved_copied_renamed_deleted": before_pdf == after_pdf,
            "managed_pdf_exists": ctx["managed"].exists(),
            "managed_pdf_hash": _sha256(ctx["managed"]) if ctx["managed"].exists() else "",
            "managed_pdf_hash_matches": ctx["managed"].exists() and _sha256(ctx["managed"]) == KOREN_HASH,
            "staged_pdf_exists": ctx["staged"].exists(),
            "staged_pdf_hash": _sha256(ctx["staged"]) if ctx["staged"].exists() else "",
        },
        "post_write_validation": {"ok": not validation_failures, "failures": validation_failures},
        "future_commands_preview_do_not_run_yet": _future_commands(run_dir.name),
        "side_effects": {
            "api_calls": "none",
            "evaluations": "none",
            "papers_db_writes": "one file_info update on Barrett_2016_Handbook_book",
            "new_db_rows": "none",
            "existing_db_row_updates": [KOREN_ID],
            "pdf_moves_copies_renames_deletes": "none",
            "id_changes": "none",
            "child_chapter_records_created": "none",
            "sqlite_bookkeeping_note": "SQLite may update normal journal/WAL bookkeeping for the committed file_info update",
        },
    }
    _write_reports(run_dir, summary)
    return summary


def _write_reports(run_dir: Path, summary: dict[str, Any]) -> None:
    (run_dir / "live_koren_fileinfo_attach_only.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "stage_live_koren_fileinfo_attach_only_report.txt").write_text(render_live_koren_fileinfo_attach_report(summary), encoding="utf-8")


def render_live_koren_fileinfo_attach_report(summary: dict[str, Any]) -> str:
    backup = summary.get("backup") or {}
    dbv = summary.get("db_validation") or {}
    pdfv = summary.get("pdf_validation") or {}
    lines = ["Stage M Live Koren File-Info Attach Only", "=" * 58]
    lines.append(f"Processed only: {summary.get('processed_only')}")
    lines.append(f"Backup: {backup.get('path')} ({backup.get('bytes')} bytes) ok={backup.get('ok')}")
    lines.append(f"Row updated: {summary.get('row_updated')} id={summary.get('row_id')}")
    lines.append(f"Fields changed: {summary.get('file_info_fields_added_or_changed')}")
    lines.append(f"DB count: {dbv.get('before_count')} -> {dbv.get('after_count')}")
    lines.append(f"Only target file_info changed: {dbv.get('only_target_file_info_changed')}")
    lines.append(f"No non-target rows changed: {dbv.get('no_non_target_rows_changed')}")
    lines.append(f"Managed PDF hash matches: {pdfv.get('managed_pdf_hash_matches')}")
    lines.append(f"Staged PDF exists: {pdfv.get('staged_pdf_exists')}")
    lines.append(f"No PDF operations: {pdfv.get('no_pdfs_moved_copied_renamed_deleted')}")
    lines.append(f"Post-write validation: {summary.get('post_write_validation')}")
    lines.append("")
    lines.append("Future commands preview (DO NOT RUN YET):")
    lines.extend(f"- {cmd}" for cmd in summary.get("future_commands_preview_do_not_run_yet", []))
    lines.append("")
    lines.append("No API calls, evaluations, new DB rows, ID changes, PDF operations, or child/chapter records were performed.")
    return "\n".join(lines) + "\n"
