"""Stage N: Koren-only staging archive pilot."""

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
from corpus_stage_j_koren_validation import KOREN_NAME
from corpus_stage_k_koren_live_parent import KOREN_HASH, KOREN_ID
from corpus_stage_m_koren_fileinfo_attach import MANAGED_FILENAME, MANAGED_RELATIVE_PATH


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


def _archive_destination() -> Path:
    base = (config.STAGING / "processed" / KOREN_ID).resolve()
    dest = (base / KOREN_NAME).resolve()
    if dest.parent != base or not str(base).startswith(str(config.STAGING.resolve())):
        raise RuntimeError(f"Unsafe archive destination: {dest}")
    return dest


def _managed_path() -> Path:
    return config.SOURCE_PDFS / MANAGED_FILENAME


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


def _koren_db_row() -> dict[str, Any] | None:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM papers WHERE id = ?", (KOREN_ID,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    out = dict(row)
    for key in ("basic_metadata", "file_info", "classification", "tags", "notes_and_flags"):
        out[key] = _json_obj(out.get(key))
    return out


def _db_row_attached(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    fi = row.get("file_info") or {}
    cl = row.get("classification") or {}
    return (
        row.get("id") == KOREN_ID
        and row.get("document_type") == "book"
        and fi.get("filepath") == MANAGED_RELATIVE_PATH
        and fi.get("renamed_filename") == MANAGED_FILENAME
        and fi.get("managed_pdf_sha256") == KOREN_HASH
        and cl.get("evaluation_status") == "deferred_not_evaluated_for_book_parent"
    )


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
    return {"count": len(entries), "digest": digest, "entries": entries}


def _snapshot_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    b = before.get("entries", {})
    a = after.get("entries", {})
    added = sorted(set(a) - set(b))
    removed = sorted(set(b) - set(a))
    changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    return {"added": added, "removed": removed, "changed": changed}


def _acquire_lock(run_dir: Path) -> Path:
    lock_path = run_dir.parent / ".corpus_pipeline.lock"
    payload = json.dumps(
        {
            "stage": "live-koren-staging-archive-only",
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
        f"DO NOT RUN YET: python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 --run-id {run_id} --execute-stage harris-machine-escalation-preview",
        "DO NOT RUN YET: python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 --execute-stage broader-ingest-pilot",
        f"DO NOT RUN YET: python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 --run-id {run_id} --execute-stage live-duplicate-staging-archive-only",
    ]


def _failure_summary(run_dir: Path, failures: list[str]) -> dict[str, Any]:
    return {
        "run_id": run_dir.name,
        "stage": "live-koren-staging-archive-only",
        "processed_only": [KOREN_NAME],
        "archived": False,
        "post_archive_validation": {"ok": False, "failures": failures},
        "recovery_notes": [
            "No automatic deletion, retry, or broad rollback was attempted.",
            "Inspect this artifact, the staging path, and the archive destination before rerunning.",
        ],
        "future_commands_preview_do_not_run_yet": _future_commands(run_dir.name),
        "side_effects": {
            "api_calls": "none",
            "evaluations": "none",
            "papers_db_writes": "none",
            "new_db_rows": "none",
            "existing_db_row_updates": "none",
            "managed_pdf_changes": "none",
            "id_changes": "none",
            "child_chapter_records_created": "none",
        },
    }


def _preconditions(plan: dict[str, Any], run_dir: Path) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    stage_k = _read_json(run_dir / "live_koren_parent_record_only.json")
    stage_l = _read_json(run_dir / "live_koren_pdf_copy_only.json")
    stage_m = _read_json(run_dir / "live_koren_fileinfo_attach_only.json")
    pdf_plan = _koren_plan(plan)
    staged = Path(pdf_plan["path"])
    managed = _managed_path()
    dest = _archive_destination()
    db_row = _koren_db_row()

    if not (stage_k.get("row_created") and stage_k.get("row_id") == KOREN_ID):
        failures.append("Stage K artifact does not show created Koren parent row")
    if not stage_l.get("post_copy_validation", {}).get("ok"):
        failures.append("Stage L artifact does not show successful Koren PDF copy")
    if not stage_m.get("post_write_validation", {}).get("ok"):
        failures.append("Stage M artifact does not show successful file_info attachment")
    if not _db_row_attached(db_row):
        failures.append("Koren DB row does not point to the attached managed PDF")
    if not managed.exists():
        failures.append(f"Managed PDF missing: {managed}")
    if not staged.exists():
        failures.append(f"Staged source PDF missing: {staged}")
    managed_hash = _sha256(managed) if managed.exists() else ""
    staged_hash = _sha256(staged) if staged.exists() else ""
    if managed_hash != KOREN_HASH:
        failures.append("Managed PDF hash mismatch")
    if staged_hash != KOREN_HASH:
        failures.append("Staged source PDF hash mismatch")
    if dest.exists():
        dest_hash = _sha256(dest)
        if dest_hash != KOREN_HASH:
            failures.append(f"Archive destination exists with different hash: {dest}")
        else:
            failures.append(f"Archive destination already exists with same hash; not overwriting: {dest}")
    return failures, {
        "staged": staged,
        "managed": managed,
        "archive_dest": dest,
        "managed_hash": managed_hash,
        "staged_hash": staged_hash,
        "db_row": db_row,
    }


def run_live_koren_staging_archive_only(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    failures, ctx = _preconditions(plan, run_dir)
    if failures:
        summary = _failure_summary(run_dir, failures)
        _write_reports(run_dir, summary)
        return summary

    lock_path: Path | None = None
    moved = False
    before_pdf = after_pdf = {}
    before_db = after_db = {}
    try:
        lock_path = _acquire_lock(run_dir)
        before_pdf = _pdf_snapshot()
        before_db = _db_digest()
        archive_dest = ctx["archive_dest"]
        archive_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(ctx["staged"]), str(archive_dest))
        moved = True
        after_pdf = _pdf_snapshot()
        after_db = _db_digest()
    except Exception as e:
        summary = _failure_summary(run_dir, [str(e)])
        _write_reports(run_dir, summary)
        return summary
    finally:
        _release_lock(lock_path)

    delta = _snapshot_delta(before_pdf, after_pdf)
    staged_rel = str(ctx["staged"].relative_to(config.BASE))
    archive_rel = str(ctx["archive_dest"].relative_to(config.BASE))
    validation_failures = []
    if not moved:
        validation_failures.append("archive move did not run")
    if ctx["staged"].exists():
        validation_failures.append("original staged source PDF still exists")
    if not ctx["archive_dest"].exists():
        validation_failures.append("archive file does not exist")
    archive_hash = _sha256(ctx["archive_dest"]) if ctx["archive_dest"].exists() else ""
    managed_hash_after = _sha256(ctx["managed"]) if ctx["managed"].exists() else ""
    if archive_hash != KOREN_HASH:
        validation_failures.append("archive file hash mismatch")
    if managed_hash_after != KOREN_HASH:
        validation_failures.append("managed PDF hash changed or is missing")
    if before_db != after_db:
        validation_failures.append("papers.db digest/count changed")
    if delta != {"added": [archive_rel], "removed": [staged_rel], "changed": []}:
        validation_failures.append(f"unexpected PDF snapshot delta: {delta}")

    summary = {
        "run_id": run_dir.name,
        "stage": "live-koren-staging-archive-only",
        "processed_only": [KOREN_NAME],
        "archived": not validation_failures,
        "original_staged_pdf_path": str(ctx["staged"]),
        "archive_destination": str(ctx["archive_dest"]),
        "managed_pdf_path": str(ctx["managed"]),
        "source_staged_pdf_hash_before": ctx["staged_hash"],
        "archived_pdf_hash": archive_hash,
        "managed_pdf_hash": managed_hash_after,
        "archive_file_exists": ctx["archive_dest"].exists(),
        "original_staged_pdf_exists_after": ctx["staged"].exists(),
        "managed_pdf_exists_after": ctx["managed"].exists(),
        "db_validation": {
            "before": before_db,
            "after": after_db,
            "papers_db_updated": False,
            "row_count_unchanged": before_db.get("count") == after_db.get("count"),
            "digest_unchanged": before_db.get("digest") == after_db.get("digest"),
        },
        "pdf_validation": {
            "before": {k: v for k, v in before_pdf.items() if k != "entries"},
            "after": {k: v for k, v in after_pdf.items() if k != "entries"},
            "delta": delta,
            "only_koren_staged_source_moved": delta == {"added": [archive_rel], "removed": [staged_rel], "changed": []},
            "no_pdfs_deleted": bool(delta.get("added")) and bool(delta.get("removed")) and not delta.get("changed"),
            "managed_pdf_unchanged": managed_hash_after == ctx["managed_hash"] == KOREN_HASH,
        },
        "post_archive_validation": {"ok": not validation_failures, "failures": validation_failures},
        "future_commands_preview_do_not_run_yet": _future_commands(run_dir.name),
        "side_effects": {
            "api_calls": "none",
            "evaluations": "none",
            "papers_db_writes": "none",
            "new_db_rows": "none",
            "existing_db_row_updates": "none",
            "pdf_archived_moves": [{"from": str(ctx["staged"]), "to": str(ctx["archive_dest"])}] if moved else [],
            "pdf_copies": "none",
            "pdf_deletes": "none",
            "managed_pdf_changes": "none",
            "id_changes": "none",
            "child_chapter_records_created": "none",
        },
    }
    _write_reports(run_dir, summary)
    return summary


def _write_reports(run_dir: Path, summary: dict[str, Any]) -> None:
    (run_dir / "live_koren_staging_archive_only.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "stage_live_koren_staging_archive_only_report.txt").write_text(render_live_koren_staging_archive_report(summary), encoding="utf-8")


def render_live_koren_staging_archive_report(summary: dict[str, Any]) -> str:
    dbv = summary.get("db_validation") or {}
    pdfv = summary.get("pdf_validation") or {}
    lines = ["Stage N Live Koren Staging Archive Only", "=" * 58]
    lines.append(f"Processed only: {summary.get('processed_only')}")
    lines.append(f"Original staged path: {summary.get('original_staged_pdf_path')}")
    lines.append(f"Archive destination: {summary.get('archive_destination')}")
    lines.append(f"Archived hash: {summary.get('archived_pdf_hash')}")
    lines.append(f"Managed hash: {summary.get('managed_pdf_hash')}")
    lines.append(f"Archive file exists: {summary.get('archive_file_exists')}")
    lines.append(f"Original staged path exists after: {summary.get('original_staged_pdf_exists_after')}")
    lines.append(f"Managed PDF exists after: {summary.get('managed_pdf_exists_after')}")
    lines.append(f"DB unchanged: count={dbv.get('row_count_unchanged')} digest={dbv.get('digest_unchanged')}")
    lines.append(f"Only Koren staged source moved: {pdfv.get('only_koren_staged_source_moved')}")
    lines.append(f"Managed PDF unchanged: {pdfv.get('managed_pdf_unchanged')}")
    lines.append(f"Post-archive validation: {summary.get('post_archive_validation')}")
    lines.append("")
    lines.append("Future commands preview (DO NOT RUN YET):")
    lines.extend(f"- {cmd}" for cmd in summary.get("future_commands_preview_do_not_run_yet", []))
    lines.append("")
    lines.append("No API calls, evaluations, DB writes, managed PDF changes, copies, deletes, ID changes, or child/chapter records were performed.")
    return "\n".join(lines) + "\n"
