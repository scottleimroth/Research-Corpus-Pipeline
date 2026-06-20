"""Cacioppo-only staging archive preview/live path.

The default path is a refusal preview: validate the DB row, managed PDF, and
staged PDF, show the archive destination, and stop before moving the staged PDF
unless --allow-staging-archive is present.
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


def _db_digest() -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()]
        h = hashlib.sha256()
        for row in conn.execute(f"SELECT {','.join(cols)} FROM papers ORDER BY id"):
            vals = [row[col] for col in cols]
            encoded = json.dumps(vals, ensure_ascii=False, default=str).encode("utf-8")
            h.update(len(encoded).to_bytes(8, "big"))
            h.update(encoded)
        return {"count": count, "digest": h.hexdigest()}
    finally:
        conn.close()


def _cacioppo_db_row() -> dict[str, Any] | None:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM papers WHERE id = ? LIMIT 1", (CACIOPPO_ID,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    out = dict(row)
    for key in papers_db.JSON_FIELDS:
        if key in out:
            out[key] = _json_obj(out.get(key))
    return out


def _db_row_attached(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    file_info = row.get("file_info") or {}
    classification = row.get("classification") or {}
    return (
        row.get("id") == CACIOPPO_ID
        and row.get("document_type") == "book"
        and file_info.get("filepath") == MANAGED_RELATIVE_PATH
        and file_info.get("renamed_filename") == MANAGED_FILENAME
        and file_info.get("managed_pdf_sha256") == CACIOPPO_HASH
        and classification.get("evaluation_status") == "deferred_not_evaluated_for_book_parent"
    )


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


def _managed_path() -> Path:
    return config.SOURCE_PDFS / MANAGED_FILENAME


def _archive_destination() -> Path:
    base = (config.STAGING / "processed" / CACIOPPO_ID).resolve()
    dest = (base / CACIOPPO_FILENAME).resolve()
    if dest.parent != base or not str(base).startswith(str(config.STAGING.resolve())):
        raise RuntimeError(f"unsafe archive destination: {dest}")
    return dest


def _acquire_lock(run_dir: Path) -> Path:
    lock_path = run_dir / "orchestrated_cacioppo_staging_archive_only.lock"
    payload = json.dumps(
        {
            "created_at": _now(),
            "stage": "orchestrated-cacioppo-staging-archive-only",
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


def validate_staging_archive_preconditions(args) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}

    checks.append(_result("--workflow ingest", getattr(args, "workflow", None) == "ingest", f"workflow={getattr(args, 'workflow', None)!r}"))
    checks.append(_result("--execute present", bool(getattr(args, "execute", False)), "required"))
    checks.append(_result("--live-staging-archive-only Cacioppo", getattr(args, "live_staging_archive_only", "") == CACIOPPO_ALIAS, f"value={getattr(args, 'live_staging_archive_only', '')!r}"))
    checks.append(_result("--run-id exact", getattr(args, "run_id", "") == ALLOWED_RUN_ID, f"run_id={getattr(args, 'run_id', '')!r}"))
    checks.append(_result("--mode A", getattr(args, "mode", None) == "A", f"mode={getattr(args, 'mode', None)!r}"))
    checks.append(_result("--max-papers 5", getattr(args, "max_papers", None) == 5, f"max_papers={getattr(args, 'max_papers', None)!r}"))
    checks.append(_result("--allow-paid-api absent", not bool(getattr(args, "allow_paid_api", False)), "evaluation disabled"))
    checks.append(_result("--allow-db-write absent", not bool(getattr(args, "allow_db_write", False)), "DB writes disabled"))
    checks.append(_result("--allow-pdf-copy absent", not bool(getattr(args, "allow_pdf_copy", False)), "PDF copy disabled"))

    run_dir = config.RUNS_DIR / args.run_id
    details["run_dir"] = run_dir
    checks.append(_result("run directory exists", run_dir.exists(), str(run_dir)))
    fileinfo_artifact = run_dir / "orchestrated_cacioppo_fileinfo_only_live.json"
    checks.append(_result("Cacioppo file_info live artifact exists", fileinfo_artifact.exists(), str(fileinfo_artifact)))
    if not run_dir.exists() or not fileinfo_artifact.exists():
        return False, checks, details

    fileinfo_live = _load_json(fileinfo_artifact)
    details["fileinfo_live_artifact"] = fileinfo_live
    checks.append(_result("Cacioppo file_info attach completed", fileinfo_live.get("status") == "live_fileinfo_attach_completed", str(fileinfo_live.get("status"))))

    db_row = _cacioppo_db_row()
    details["db_row"] = db_row
    checks.append(_result("Cacioppo DB row exists", db_row is not None, CACIOPPO_ID))
    checks.append(_result("Cacioppo DB row points to managed PDF", _db_row_attached(db_row), CACIOPPO_ID))

    managed = _managed_path()
    details["managed"] = managed
    checks.append(_result("managed Cacioppo PDF exists", managed.exists(), str(managed)))
    managed_hash = _sha256(managed) if managed.exists() else ""
    details["managed_hash"] = managed_hash
    checks.append(_result("managed Cacioppo hash matches", managed_hash == CACIOPPO_HASH, CACIOPPO_HASH))

    staged_path = Path((db_row or {}).get("file_info", {}).get("staged_path") or "")
    details["staged"] = staged_path
    checks.append(_result("staged Cacioppo PDF exists", staged_path.exists(), str(staged_path)))
    staged_hash = _sha256(staged_path) if staged_path.exists() else ""
    details["staged_hash"] = staged_hash
    checks.append(_result("staged Cacioppo hash matches", staged_hash == CACIOPPO_HASH, CACIOPPO_HASH))

    try:
        archive_dest = _archive_destination()
        archive_dest_safe = True
        archive_dest_detail = str(archive_dest)
    except RuntimeError as exc:
        archive_dest = config.STAGING / "processed" / CACIOPPO_ID / CACIOPPO_FILENAME
        archive_dest_safe = False
        archive_dest_detail = str(exc)
    details["archive_dest"] = archive_dest
    checks.append(_result("archive destination is safe", archive_dest_safe, archive_dest_detail))
    checks.append(_result("archive destination does not already exist", not archive_dest.exists(), str(archive_dest)))

    return all(c["ok"] for c in checks), checks, details


def _write_artifacts(run_dir: Path, summary: dict[str, Any]) -> dict[str, str]:
    if summary.get("status") == "live_staging_archive_completed":
        json_path = run_dir / "orchestrated_cacioppo_staging_archive_only_live.json"
        report_path = run_dir / "orchestrated_cacioppo_staging_archive_only_live_report.txt"
    else:
        json_path = run_dir / "orchestrated_cacioppo_staging_archive_only_preview.json"
        report_path = run_dir / "orchestrated_cacioppo_staging_archive_only_preview_report.txt"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(render_staging_archive_report(summary), encoding="utf-8")
    return {"json": str(json_path), "report": str(report_path)}


def render_staging_archive_report(summary: dict[str, Any]) -> str:
    title = (
        "Orchestrated Cacioppo Staging Archive Live"
        if summary.get("status") == "live_staging_archive_completed"
        else "Orchestrated Cacioppo Staging Archive Refusal Preview"
    )
    lines = [
        title,
        "=" * 60,
        f"Run ID: {summary.get('run_id')}",
        f"Status: {summary.get('status')}",
        f"Processed only: {summary.get('processed_only')}",
        f"Staged source: {summary.get('staged_pdf_path')}",
        f"Archive destination: {summary.get('archive_destination')}",
        f"Managed PDF: {summary.get('managed_pdf_path')}",
        "",
        "Hash checks",
        "-" * 60,
        f"staged_pdf_hash: {summary.get('staged_pdf_hash')}",
        f"managed_pdf_hash: {summary.get('managed_pdf_hash')}",
        "",
        "Actions",
        "-" * 60,
        f"DB action: {summary.get('db_action')}",
        f"PDF action: {summary.get('pdf_action')}",
        f"API/evaluation action: {summary.get('api_evaluation_action')}",
        "",
        f"Next command (DO NOT RUN YET): {summary.get('next_live_archive_command_do_not_run_yet')}",
    ]
    return "\n".join(lines) + "\n"


def run_ingest_workflow_live_staging_archive_only(args) -> dict[str, Any]:
    ok, checks, details = validate_staging_archive_preconditions(args)
    run_dir = Path(details.get("run_dir") or (config.RUNS_DIR / (args.run_id or ALLOWED_RUN_ID)))
    staged = details.get("staged")
    managed = details.get("managed") or _managed_path()
    archive_dest = details.get("archive_dest") or (config.STAGING / "processed" / CACIOPPO_ID / CACIOPPO_FILENAME)
    summary = {
        "created_at": _now(),
        "run_id": args.run_id,
        "stage": "orchestrated-cacioppo-staging-archive-only",
        "processed_only": [CACIOPPO_FILENAME],
        "not_processed": [
            "(Oxford Handbooks) Susan Hallam, Ian Cross, Michael Thaut (e.pdf",
            "Ashley, Richard(Editor)_Timmers, Renee(Contributor) - The Ro.pdf",
            "Levitin-The Psychology of Music- Rhythm and Movement-2017-An.pdf",
        ],
        "checks": checks,
        "staged_pdf_path": str(staged) if staged else None,
        "archive_destination": str(archive_dest),
        "managed_pdf_path": str(managed),
        "staged_pdf_hash": details.get("staged_hash", ""),
        "managed_pdf_hash": details.get("managed_hash", ""),
        "db_action": "none",
        "pdf_action": "none",
        "api_evaluation_action": "none",
        "next_live_archive_command_do_not_run_yet": (
            "python pipeline\\run_corpus_pipeline.py --workflow ingest --mode A "
            "--selection-profile broader --pilot-profile safe-live --max-papers 5 "
            "--run-id 20260528-203823 --execute --live-staging-archive-only Cacioppo --allow-staging-archive"
        ),
        "side_effects": {
            "db_writes": "none",
            "api_calls": "none",
            "evaluations": "none",
        },
    }

    if not ok:
        summary["status"] = "precondition_failed"
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifact_paths": paths, "message": "Cacioppo staging archive preconditions failed."}

    if not bool(getattr(args, "allow_staging_archive", False)):
        summary.update(
            {
                "status": "refused_before_staging_archive_missing_allow_staging_archive",
                "ready_for_live_archive": True,
                "pdf_action": "none (refused before archive; --allow-staging-archive absent)",
                "archive_destination_preview": {
                    "path": str(archive_dest),
                    "exists": archive_dest.exists(),
                    "parent": str(archive_dest.parent),
                },
            }
        )
        paths = _write_artifacts(run_dir, summary)
        return {
            "ok": True,
            "summary": summary,
            "artifact_paths": paths,
            "message": "Cacioppo staging archive destination validated; refused before archive because --allow-staging-archive is absent.",
        }

    lock_path: Path | None = None
    try:
        lock_path = _acquire_lock(run_dir)
        before_db = _db_digest()
        before_pdf = _pdf_snapshot()
        archive_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(archive_dest))
        after_pdf = _pdf_snapshot()
        after_db = _db_digest()
    except Exception as exc:
        summary.update({"status": "live_staging_archive_failed", "error": str(exc)})
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifact_paths": paths, "message": "Cacioppo staging archive failed; inspect artifact."}
    finally:
        _release_lock(lock_path)

    delta = _snapshot_delta(before_pdf, after_pdf)
    staged_rel = str(staged.relative_to(config.BASE))
    archive_rel = str(archive_dest.relative_to(config.BASE))
    archive_hash = _sha256(archive_dest) if archive_dest.exists() else ""
    managed_hash_after = _sha256(managed) if managed.exists() else ""
    failures = []
    if staged.exists():
        failures.append("original staged source PDF still exists")
    if not archive_dest.exists():
        failures.append("archive destination file missing")
    if archive_hash != CACIOPPO_HASH:
        failures.append("archive destination hash mismatch")
    if managed_hash_after != CACIOPPO_HASH:
        failures.append("managed PDF hash changed or is missing")
    if before_db != after_db:
        failures.append("papers.db changed during archive stage")
    if delta != {"added": [archive_rel], "removed": [staged_rel], "changed": []}:
        failures.append(f"unexpected PDF snapshot delta: {delta}")

    summary.update(
        {
            "status": "live_staging_archive_completed",
            "ready_for_live_archive": False,
            "pdf_action": "moved one Cacioppo staged PDF to staging processed archive",
            "archive_pdf_hash": archive_hash,
            "managed_pdf_hash_after": managed_hash_after,
            "pdf_validation": {
                "before": before_pdf,
                "after": after_pdf,
                "delta": delta,
                "staged_source_removed": not staged.exists(),
                "archive_destination_exists": archive_dest.exists(),
                "archive_hash_matches": archive_hash == CACIOPPO_HASH,
                "managed_hash_unchanged": managed_hash_after == CACIOPPO_HASH,
            },
            "db_validation": {
                "before": before_db,
                "after": after_db,
                "papers_db_unchanged": before_db == after_db,
            },
            "post_archive_failures": failures,
            "side_effects": {
                "db_writes": "none",
                "api_calls": "none",
                "evaluations": "none",
                "pdf_operations": "one staging archive move only",
            },
        }
    )
    paths = _write_artifacts(run_dir, summary)
    return {
        "ok": not failures,
        "summary": summary,
        "artifact_paths": paths,
        "message": "Cacioppo staged PDF archived." if not failures else "Cacioppo staging archive completed but validation failed; inspect artifact.",
    }
