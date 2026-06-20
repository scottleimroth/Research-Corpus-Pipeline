"""Cacioppo-only file_info attachment preview/live path.

The default path is a refusal preview: validate the parent row and managed PDF,
show the file_info patch, and stop before updating papers.db unless
--allow-db-write is present.
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
import papers_db

ALLOWED_RUN_ID = "20260528-203823"
CACIOPPO_ALIAS = "Cacioppo"
CACIOPPO_FILENAME = "(Cambridge Handbooks in Psychology) John T. Cacioppo (editor.pdf"
CACIOPPO_ID = "Cacioppo_2013_Handbook_book"
CACIOPPO_HASH = "6808405eb7e55ae50b1f36cc8f922b1012bd9209a04083ab8bb5db4f64655293"
MANAGED_FILENAME = f"{CACIOPPO_ID}.pdf"
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


def _table_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])


def _non_target_digest(conn: sqlite3.Connection) -> str:
    conn.row_factory = sqlite3.Row
    h = hashlib.sha256()
    cols = papers_db.ALL_COLUMNS
    for row in conn.execute(f"SELECT {','.join(cols)} FROM papers WHERE id != ? ORDER BY id", (CACIOPPO_ID,)):
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
    return {"count": len(entries), "digest": digest}


def _create_backup() -> dict[str, Any]:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = config.BACKUP_DIR / f"papers-{ts}-PRE-CACIOPPO-FILEINFO.db"
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
        "detail": "fresh PRE-CACIOPPO-FILEINFO backup created and verified" if ok else "backup failed validation",
    }


def _acquire_lock(run_dir: Path) -> Path:
    lock_path = run_dir / "orchestrated_cacioppo_fileinfo_only.lock"
    payload = json.dumps(
        {
            "created_at": _now(),
            "stage": "orchestrated-cacioppo-fileinfo-only",
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


def _file_info_patch(existing: dict[str, Any], managed_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = dict(existing)
    changed = {
        "filepath": MANAGED_RELATIVE_PATH,
        "renamed_filename": MANAGED_FILENAME,
        "managed_pdf_filename": MANAGED_FILENAME,
        "managed_pdf_relative_path": MANAGED_RELATIVE_PATH,
        "managed_pdf_absolute_path": str(managed_path),
        "managed_pdf_sha256": CACIOPPO_HASH,
        "managed_pdf_attached_at": _now(),
        "managed_pdf_attachment_stage": "orchestrated-cacioppo-fileinfo-only",
        "managed_pdf_action": "copied_in_orchestrated_pdf_copy_attached_in_fileinfo_stage",
    }
    updated.update(changed)
    return updated, changed


def _read_back_ok(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    file_info = row.get("file_info") or {}
    classification = row.get("classification") or {}
    return (
        row.get("id") == CACIOPPO_ID
        and row.get("document_type") == "book"
        and classification.get("evaluation_status") == "deferred_not_evaluated_for_book_parent"
        and file_info.get("original_filename") == CACIOPPO_FILENAME
        and file_info.get("staged_sha256") == CACIOPPO_HASH
        and file_info.get("filepath") == MANAGED_RELATIVE_PATH
        and file_info.get("renamed_filename") == MANAGED_FILENAME
        and file_info.get("managed_pdf_sha256") == CACIOPPO_HASH
    )


def validate_fileinfo_preconditions(args) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}

    checks.append(_result("--workflow ingest", getattr(args, "workflow", None) == "ingest", f"workflow={getattr(args, 'workflow', None)!r}"))
    checks.append(_result("--execute present", bool(getattr(args, "execute", False)), "required"))
    checks.append(_result("--live-fileinfo-only Cacioppo", getattr(args, "live_fileinfo_only", "") == CACIOPPO_ALIAS, f"value={getattr(args, 'live_fileinfo_only', '')!r}"))
    checks.append(_result("--run-id exact", getattr(args, "run_id", "") == ALLOWED_RUN_ID, f"run_id={getattr(args, 'run_id', '')!r}"))
    checks.append(_result("--mode A", getattr(args, "mode", None) == "A", f"mode={getattr(args, 'mode', None)!r}"))
    checks.append(_result("--max-papers 5", getattr(args, "max_papers", None) == 5, f"max_papers={getattr(args, 'max_papers', None)!r}"))
    checks.append(_result("--allow-paid-api absent", not bool(getattr(args, "allow_paid_api", False)), "evaluation disabled"))
    checks.append(_result("--allow-pdf-copy absent", not bool(getattr(args, "allow_pdf_copy", False)), "PDF operations disabled"))
    checks.append(_result("--allow-staging-archive absent", not bool(getattr(args, "allow_staging_archive", False)), "staging archive disabled"))

    run_dir = config.RUNS_DIR / args.run_id
    details["run_dir"] = run_dir
    checks.append(_result("run directory exists", run_dir.exists(), str(run_dir)))
    parent_artifact = run_dir / "orchestrated_cacioppo_parent_only_live.json"
    copy_artifact = run_dir / "orchestrated_cacioppo_pdf_copy_only_live.json"
    checks.append(_result("Cacioppo parent live artifact exists", parent_artifact.exists(), str(parent_artifact)))
    checks.append(_result("Cacioppo PDF copy artifact exists", copy_artifact.exists(), str(copy_artifact)))
    if not run_dir.exists() or not parent_artifact.exists() or not copy_artifact.exists():
        return False, checks, details

    parent_live = _load_json(parent_artifact)
    copy_live = _load_json(copy_artifact)
    details["parent_live_artifact"] = parent_live
    details["copy_live_artifact"] = copy_live
    checks.append(_result("Cacioppo parent row created", parent_live.get("row_created") is True, str(parent_live.get("row_created"))))
    checks.append(_result("Cacioppo PDF copy completed", copy_live.get("status") == "live_copy_completed", str(copy_live.get("status"))))
    checks.append(_result("Cacioppo copied hash matches", copy_live.get("managed_copied_pdf_hash") == CACIOPPO_HASH, str(copy_live.get("managed_copied_pdf_hash"))))

    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        raw_row = _row_raw(conn, CACIOPPO_ID)
    finally:
        conn.close()
    row = _row_decoded(raw_row)
    details["row_raw"] = raw_row
    details["row_decoded"] = row
    checks.append(_result("Cacioppo DB row exists", row is not None, CACIOPPO_ID))
    if row:
        file_info = row.get("file_info") or {}
        checks.append(_result("Cacioppo DB document_type book", row.get("document_type") == "book", str(row.get("document_type"))))
        checks.append(_result("file_info not already attached", not file_info.get("filepath"), str(file_info.get("filepath"))))

    managed = config.SOURCE_PDFS / MANAGED_FILENAME
    staged = Path((parent_live.get("parent_row_preview") or {}).get("file_info", {}).get("staged_path") or "")
    details["managed"] = managed
    details["staged"] = staged
    checks.append(_result("managed Cacioppo PDF exists", managed.exists(), str(managed)))
    managed_hash = _sha256(managed) if managed.exists() else ""
    details["managed_hash"] = managed_hash
    checks.append(_result("managed Cacioppo hash matches", managed_hash == CACIOPPO_HASH, CACIOPPO_HASH))
    checks.append(_result("staged Cacioppo PDF still exists", staged.exists(), str(staged)))
    staged_hash = _sha256(staged) if staged.exists() else ""
    details["staged_hash"] = staged_hash
    checks.append(_result("staged Cacioppo hash matches", staged_hash == CACIOPPO_HASH, CACIOPPO_HASH))

    existing_file_info = (row or {}).get("file_info") or {}
    updated_file_info, changed_file_info = _file_info_patch(existing_file_info, managed)
    details["updated_file_info"] = updated_file_info
    details["changed_file_info"] = changed_file_info

    return all(c["ok"] for c in checks), checks, details


def _write_artifacts(run_dir: Path, summary: dict[str, Any]) -> dict[str, str]:
    if summary.get("status") == "live_fileinfo_attach_completed":
        json_path = run_dir / "orchestrated_cacioppo_fileinfo_only_live.json"
        report_path = run_dir / "orchestrated_cacioppo_fileinfo_only_live_report.txt"
    else:
        json_path = run_dir / "orchestrated_cacioppo_fileinfo_only_preview.json"
        report_path = run_dir / "orchestrated_cacioppo_fileinfo_only_preview_report.txt"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(render_fileinfo_report(summary), encoding="utf-8")
    return {"json": str(json_path), "report": str(report_path)}


def render_fileinfo_report(summary: dict[str, Any]) -> str:
    title = (
        "Orchestrated Cacioppo File Info Attach Live"
        if summary.get("status") == "live_fileinfo_attach_completed"
        else "Orchestrated Cacioppo File Info Attach Refusal Preview"
    )
    lines = [
        title,
        "=" * 58,
        f"Run ID: {summary.get('run_id')}",
        f"Status: {summary.get('status')}",
        f"Processed only: {summary.get('processed_only')}",
        "",
        "file_info fields previewed",
        "-" * 58,
    ]
    for key, value in (summary.get("file_info_patch") or {}).items():
        lines.append(f"{key}: {value}")
    lines.extend(
        [
            "",
            "Actions",
            "-" * 58,
            f"DB action: {summary.get('db_action')}",
            f"PDF action: {summary.get('pdf_action')}",
            f"API/evaluation action: {summary.get('api_evaluation_action')}",
            f"Backup: {summary.get('backup')}",
            "",
            f"Next command (DO NOT RUN YET): {summary.get('next_live_fileinfo_command_do_not_run_yet')}",
        ]
    )
    return "\n".join(lines) + "\n"


def run_ingest_workflow_live_fileinfo_only(args) -> dict[str, Any]:
    ok, checks, details = validate_fileinfo_preconditions(args)
    run_dir = Path(details.get("run_dir") or (config.RUNS_DIR / (args.run_id or ALLOWED_RUN_ID)))
    summary = {
        "created_at": _now(),
        "run_id": args.run_id,
        "stage": "orchestrated-cacioppo-fileinfo-only",
        "processed_only": [CACIOPPO_ID],
        "not_processed": [
            "(Oxford Handbooks) Susan Hallam, Ian Cross, Michael Thaut (e.pdf",
            "Ashley, Richard(Editor)_Timmers, Renee(Contributor) - The Ro.pdf",
            "Levitin-The Psychology of Music- Rhythm and Movement-2017-An.pdf",
        ],
        "checks": checks,
        "file_info_patch": details.get("changed_file_info"),
        "file_info_after_preview": details.get("updated_file_info"),
        "staged_pdf_hash": details.get("staged_hash"),
        "managed_pdf_hash": details.get("managed_hash"),
        "db_action": "none",
        "pdf_action": "none",
        "api_evaluation_action": "none",
        "backup": None,
        "next_live_fileinfo_command_do_not_run_yet": (
            "python pipeline\\run_corpus_pipeline.py --workflow ingest --mode A "
            "--selection-profile broader --pilot-profile safe-live --max-papers 5 "
            "--run-id 20260528-203823 --execute --live-fileinfo-only Cacioppo --allow-db-write"
        ),
        "side_effects": {
            "db_writes": "none",
            "pdf_operations": "none",
            "api_calls": "none",
            "evaluations": "none",
            "child_chapter_records": "none",
        },
    }

    if not ok:
        summary["status"] = "precondition_failed"
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifact_paths": paths, "message": "Cacioppo file_info preconditions failed."}

    if not bool(getattr(args, "allow_db_write", False)):
        summary.update(
            {
                "status": "refused_before_fileinfo_db_write_missing_allow_db_write",
                "ready_for_live_fileinfo_attach": True,
                "db_action": "none (refused before file_info update; --allow-db-write absent)",
                "backup": "not created in refusal preview; fresh PRE-CACIOPPO-FILEINFO backup required before live attach",
            }
        )
        paths = _write_artifacts(run_dir, summary)
        return {
            "ok": True,
            "summary": summary,
            "artifact_paths": paths,
            "message": "Cacioppo file_info patch validated; refused before DB write because --allow-db-write is absent.",
        }

    lock_path: Path | None = None
    try:
        lock_path = _acquire_lock(run_dir)
        backup = _create_backup()
        if not backup.get("ok"):
            raise RuntimeError("fresh PRE-CACIOPPO-FILEINFO backup failed validation")
        before_pdf = _pdf_snapshot()
        conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            before_count = _table_count(conn)
            before_digest = _non_target_digest(conn)
            before_raw = _row_raw(conn, CACIOPPO_ID)
            before_row = _row_decoded(before_raw)
            if before_row is None:
                conn.rollback()
                raise RuntimeError(f"{CACIOPPO_ID} missing before file_info attach")
            updated_file_info, changed_file_info = _file_info_patch(before_row.get("file_info") or {}, details["managed"])
            conn.execute(
                "UPDATE papers SET file_info = ? WHERE id = ?",
                (json.dumps(updated_file_info, ensure_ascii=False), CACIOPPO_ID),
            )
            conn.commit()
            after_count = _table_count(conn)
            after_digest = _non_target_digest(conn)
            read_back = _row_decoded(_row_raw(conn, CACIOPPO_ID))
        finally:
            conn.close()
        after_pdf = _pdf_snapshot()
    except Exception as exc:
        summary.update({"status": "live_fileinfo_attach_failed", "error": str(exc), "backup": locals().get("backup")})
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifact_paths": paths, "message": "Cacioppo file_info attach failed; inspect artifact."}
    finally:
        _release_lock(lock_path)

    failures = []
    if before_count != after_count:
        failures.append("DB row count changed during file_info attach")
    if before_digest != after_digest:
        failures.append("non-target DB row digest changed")
    if not _read_back_ok(read_back):
        failures.append("Cacioppo row did not read back with expected file_info")
    if before_pdf != after_pdf:
        failures.append("PDF snapshot changed during file_info attach")

    summary.update(
        {
            "status": "live_fileinfo_attach_completed",
            "ready_for_live_fileinfo_attach": False,
            "db_action": "updated file_info for Cacioppo parent row only",
            "backup": backup,
            "file_info_patch": changed_file_info,
            "file_info_after_preview": read_back.get("file_info") if read_back else None,
            "db_validation": {
                "before_count": before_count,
                "after_count": after_count,
                "row_count_unchanged": before_count == after_count,
                "non_target_digest_before": before_digest,
                "non_target_digest_after": after_digest,
                "no_non_target_rows_changed": before_digest == after_digest,
                "row_read_back_ok": _read_back_ok(read_back),
            },
            "pdf_validation": {
                "before": before_pdf,
                "after": after_pdf,
                "no_pdfs_moved_copied_renamed_deleted": before_pdf == after_pdf,
            },
            "post_write_failures": failures,
            "side_effects": {
                "db_writes": "updated Cacioppo file_info only",
                "pdf_operations": "none",
                "api_calls": "none",
                "evaluations": "none",
                "child_chapter_records": "none",
            },
        }
    )
    paths = _write_artifacts(run_dir, summary)
    return {
        "ok": not failures,
        "summary": summary,
        "artifact_paths": paths,
        "message": "Cacioppo file_info attached." if not failures else "Cacioppo file_info attach completed but validation failed; inspect artifact.",
    }
