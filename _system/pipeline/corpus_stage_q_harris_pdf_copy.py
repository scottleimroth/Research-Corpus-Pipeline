"""Stage Q: Harris-only managed PDF copy pilot."""

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
from corpus_stage_o_harris_validation import HARRIS_ID_PREVIEW, HARRIS_NAME
from corpus_stage_p_harris_live_parent import HARRIS_HASH


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


def _harris_plan(plan: dict[str, Any]) -> dict[str, Any]:
    matches = [p for p in plan.get("pdf_plans", []) if p.get("name") == HARRIS_NAME]
    if len(matches) != 1:
        raise RuntimeError("Harris PDF plan not found exactly once")
    return matches[0]


def _read_stage_p(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "live_harris_parent_record_only.json"
    if not path.exists():
        raise RuntimeError(f"Stage P artifact missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _get_harris_db_row() -> dict[str, Any] | None:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM papers WHERE id = ?", (HARRIS_ID_PREVIEW,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    out = dict(row)
    for key in ("basic_metadata", "identifiers", "file_info", "classification", "tags", "notes_and_flags", "full_text"):
        if key in out:
            out[key] = _json_obj(out.get(key))
    return out


def _db_digest() -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        h = hashlib.sha256()
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()]
        for row in conn.execute(f"SELECT {','.join(cols)} FROM papers ORDER BY id"):
            vals = [row[col] for col in cols]
            encoded = json.dumps(vals, ensure_ascii=False, default=str).encode("utf-8")
            h.update(len(encoded).to_bytes(8, "big"))
            h.update(encoded)
        return {"count": count, "digest": h.hexdigest()}
    finally:
        conn.close()


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


def _safe_destination() -> Path:
    dest = (config.SOURCE_PDFS / f"{HARRIS_ID_PREVIEW}.pdf").resolve()
    source_dir = config.SOURCE_PDFS.resolve()
    if dest.parent != source_dir:
        raise RuntimeError(f"Unsafe managed PDF destination: {dest}")
    return dest


def _acquire_lock(run_dir: Path) -> Path:
    lock_path = run_dir.parent / ".corpus_pipeline.lock"
    payload = json.dumps(
        {
            "stage": "live-harris-pdf-copy-only",
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
        f"DO NOT RUN YET: python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 --run-id {run_id} --execute-stage live-harris-fileinfo-attach-only --allow-db-write",
        f"DO NOT RUN YET: python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 --run-id {run_id} --execute-stage live-harris-staging-archive-only --allow-staging-archive",
        f"DO NOT RUN YET: python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 --run-id {run_id} --execute-stage broader-ingest-pilot",
    ]


def _failure_summary(run_dir: Path, failures: list[str]) -> dict[str, Any]:
    return {
        "run_id": run_dir.name,
        "stage": "live-harris-pdf-copy-only",
        "processed_only": [HARRIS_NAME],
        "copied": False,
        "already_copied": False,
        "post_copy_validation": {"ok": False, "failures": failures},
        "recovery_notes": [
            "No automatic deletion, retry, or broad rollback was attempted.",
            "Inspect this artifact and the managed PDF destination before rerunning.",
        ],
        "future_commands_preview_do_not_run_yet": _future_commands(run_dir.name),
        "side_effects": {
            "api_calls": "none",
            "evaluations": "none",
            "papers_db_writes": "none",
            "new_db_rows": "none",
            "existing_db_row_updates": "none",
            "pdf_moves_renames_deletes": "none",
            "id_changes": "none",
            "child_chapter_records_created": "none",
        },
    }


def run_live_harris_pdf_copy_only(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    failures: list[str] = []
    stage_p = _read_stage_p(run_dir)
    if not (stage_p.get("row_created") and stage_p.get("row_id") == HARRIS_ID_PREVIEW):
        failures.append("Stage P artifact does not show the Harris parent row was created")
    pdf_plan = _harris_plan(plan)
    source = Path(pdf_plan["path"])
    if not source.exists():
        failures.append(f"Staged Harris PDF missing: {source}")
    source_hash = _sha256(source) if source.exists() else ""
    if source_hash != HARRIS_HASH or source_hash != pdf_plan.get("sha256"):
        failures.append("Staged Harris PDF hash does not match Stage P/run plan hash")
    db_row = _get_harris_db_row()
    if not db_row:
        failures.append(f"{HARRIS_ID_PREVIEW} does not exist in papers.db")
    elif db_row.get("id") != HARRIS_ID_PREVIEW or db_row.get("document_type") != "edited_volume":
        failures.append("Harris DB row did not read back as expected")
    dest = _safe_destination()
    if dest.exists():
        dest_hash = _sha256(dest)
        if dest_hash != source_hash:
            failures.append(f"Managed destination already exists with different hash: {dest}")
    else:
        dest_hash = ""
    if failures:
        summary = _failure_summary(run_dir, failures)
        _write_reports(run_dir, summary)
        return summary

    lock_path: Path | None = None
    copied = False
    already_copied = dest.exists()
    before_pdf = after_pdf = {}
    before_db = after_db = {}
    try:
        lock_path = _acquire_lock(run_dir)
        before_pdf = _pdf_snapshot()
        before_db = _db_digest()
        if not already_copied:
            config.SOURCE_PDFS.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            copied = True
        managed_hash = _sha256(dest) if dest.exists() else ""
        after_pdf = _pdf_snapshot()
        after_db = _db_digest()
    except Exception as e:
        summary = _failure_summary(run_dir, [str(e)])
        _write_reports(run_dir, summary)
        return summary
    finally:
        _release_lock(lock_path)

    delta = _snapshot_delta(before_pdf, after_pdf)
    expected_added = str(dest.relative_to(config.BASE))
    if copied:
        ok_pdf_delta = delta == {"added": [expected_added], "removed": [], "changed": []}
    else:
        ok_pdf_delta = delta == {"added": [], "removed": [], "changed": []}
    validation_failures = []
    if not source.exists():
        validation_failures.append("staged source PDF no longer exists")
    if not dest.exists():
        validation_failures.append("managed copy does not exist")
    if managed_hash != source_hash:
        validation_failures.append("managed copy hash does not match source hash")
    if not ok_pdf_delta:
        validation_failures.append(f"unexpected PDF snapshot delta: {delta}")
    if before_db != after_db:
        validation_failures.append("papers.db changed during copy-only stage")

    summary = {
        "run_id": run_dir.name,
        "stage": "live-harris-pdf-copy-only",
        "processed_only": [HARRIS_NAME],
        "row_id": HARRIS_ID_PREVIEW,
        "source_staged_pdf": str(source),
        "managed_pdf_destination": str(dest),
        "source_staged_pdf_hash": source_hash,
        "managed_copied_pdf_hash": managed_hash,
        "copied": copied,
        "already_copied": already_copied,
        "staged_pdf_still_exists": source.exists(),
        "db_update": {
            "papers_db_updated": False,
            "reason": "copy-only pilot; existing Stage P row already records managed destination as preview-only",
            "before": before_db,
            "after": after_db,
            "unchanged": before_db == after_db,
        },
        "pdf_validation": {
            "before": {k: v for k, v in before_pdf.items() if k != "entries"},
            "after": {k: v for k, v in after_pdf.items() if k != "entries"},
            "delta": delta,
            "only_harris_pdf_copied": (copied and delta["added"] == [expected_added]) or already_copied,
            "no_pdfs_moved_renamed_deleted": not delta["removed"],
            "no_unexpected_pdf_changes": ok_pdf_delta,
        },
        "post_copy_validation": {
            "ok": not validation_failures,
            "failures": validation_failures,
        },
        "future_commands_preview_do_not_run_yet": _future_commands(run_dir.name),
        "side_effects": {
            "api_calls": "none",
            "evaluations": "none",
            "papers_db_writes": "none",
            "new_db_rows": "none",
            "existing_db_row_updates": "none",
            "pdf_copies": [str(dest)] if copied else [],
            "pdf_moves_renames_deletes": "none",
            "id_changes": "none",
            "child_chapter_records_created": "none",
        },
    }
    _write_reports(run_dir, summary)
    return summary


def _write_reports(run_dir: Path, summary: dict[str, Any]) -> None:
    (run_dir / "live_harris_pdf_copy_only.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "stage_live_harris_pdf_copy_only_report.txt").write_text(render_live_harris_pdf_copy_report(summary), encoding="utf-8")


def render_live_harris_pdf_copy_report(summary: dict[str, Any]) -> str:
    lines = ["Stage Q Live Harris PDF Copy Only", "=" * 52]
    lines.append(f"Processed only: {summary.get('processed_only')}")
    lines.append(f"Destination: {summary.get('managed_pdf_destination')}")
    lines.append(f"Source hash: {summary.get('source_staged_pdf_hash')}")
    lines.append(f"Managed hash: {summary.get('managed_copied_pdf_hash')}")
    lines.append(f"Copied: {summary.get('copied')} already_copied={summary.get('already_copied')}")
    lines.append(f"Staged PDF still exists: {summary.get('staged_pdf_still_exists')}")
    lines.append(f"DB updated: {(summary.get('db_update') or {}).get('papers_db_updated')}")
    lines.append(f"Post-copy validation: {summary.get('post_copy_validation')}")
    lines.append("")
    lines.append("Future commands preview (DO NOT RUN YET):")
    lines.extend(f"- {cmd}" for cmd in summary.get("future_commands_preview_do_not_run_yet", []))
    lines.append("")
    lines.append("No API calls, evaluations, DB writes, ID changes, PDF moves/renames/deletes, or child/chapter records were performed.")
    return "\n".join(lines) + "\n"
