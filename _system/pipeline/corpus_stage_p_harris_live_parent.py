"""Stage P: separately gated live Harris edited-volume parent-record creation."""

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
from corpus_stage_o_harris_validation import EXPECTED_DOI, EXPECTED_EDITORS, EXPECTED_ISBNS, EXPECTED_YEAR, HARRIS_ID_PREVIEW, HARRIS_NAME

HARRIS_HASH = "e99b0e4977097f3aac1f6d91326702cce58673ff4f2e6dbe9a7e7f9c7fe2d305"
EXPECTED_TITLE = "Neuroeconomics"
EXPECTED_PUBLISHER = "Springer"
EXPECTED_SERIES = "Studies in Neuroscience, Psychology and Behavioral Economics"
EXPECTED_TAG = "guideline_mismatch_or_new_guideline_needed_candidate"
EXPECTED_EVAL_STATUS = "deferred_not_evaluated_for_edited_volume_parent"


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_stage_o(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "harris_edited_volume_validation_preview.json"
    if not path.exists():
        raise RuntimeError(f"Stage O artifact missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _require(ok: bool, message: str, failures: list[str]) -> None:
    if not ok:
        failures.append(message)


def _is_safe_harris_id(candidate: str) -> bool:
    return bool(re.fullmatch(r"Reuter_2016_Neuroeconomics_edited", candidate))


def _validate_stage_o(stage_o: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    row = stage_o.get("proposed_parent_row_preview") or {}
    basic = row.get("basic_metadata") or {}
    file_info = row.get("file_info") or {}
    duplicate = stage_o.get("duplicate_reassessment") or {}
    id_preview = stage_o.get("safe_parent_id_preview") or {}
    policy = stage_o.get("parent_chapter_policy") or {}

    _require(stage_o.get("stage") == "harris-edited-volume-validation-preview", "Stage O artifact has wrong stage", failures)
    _require(stage_o.get("processed_only") == [HARRIS_NAME], "Stage O did not process only Harris", failures)
    _require(stage_o.get("live_readiness_decision") == "ready_for_parent_record_live_pilot", "Harris is not live-ready for parent record", failures)
    _require(duplicate.get("status") == "no_duplicate_signal", "Harris duplicate reassessment is not no_duplicate_signal", failures)
    _require(id_preview.get("candidate_id") == HARRIS_ID_PREVIEW, "Harris safe ID preview mismatch", failures)
    _require(row.get("id_preview") == HARRIS_ID_PREVIEW, "Proposed parent row ID mismatch", failures)
    _require(row.get("document_type") == "edited_volume", "document_type is not edited_volume", failures)
    _require(basic.get("title") == EXPECTED_TITLE, "title mismatch", failures)
    _require(basic.get("authors") == EXPECTED_EDITORS, "editor list mismatch", failures)
    _require(str(basic.get("year")) == EXPECTED_YEAR, "year mismatch", failures)
    _require(basic.get("publisher") == EXPECTED_PUBLISHER, "publisher mismatch", failures)
    _require(basic.get("source_or_series") == EXPECTED_SERIES, "series mismatch", failures)
    _require((basic.get("doi") or "").lower() == EXPECTED_DOI, "DOI mismatch", failures)
    isbns = set(basic.get("isbn") or [])
    _require(set(EXPECTED_ISBNS) <= isbns, "required ISBNs missing", failures)
    _require(file_info.get("original_filename") == HARRIS_NAME, "original filename mismatch", failures)
    _require(file_info.get("staged_sha256") == HARRIS_HASH, "staged hash mismatch", failures)
    _require(row.get("evaluation_status") == EXPECTED_EVAL_STATUS, "evaluation status mismatch", failures)
    _require("defer" in str(row.get("child_chapter_policy", "")).lower(), "child/chapter policy is not deferred", failures)
    _require(policy.get("chapter_metadata_reliable_for_future_child_records") is False, "chapter metadata unexpectedly marked reliable for child records", failures)
    _require(policy.get("recommended_representation") == "edited_volume_parent_record_candidate_only", "representation is not parent-only", failures)
    _require(EXPECTED_TAG in (row.get("tags") or []), "guideline mismatch tag missing", failures)
    _require(not file_info.get("renamed_filename"), "row implies managed PDF rename/copy", failures)
    _require(not file_info.get("filepath"), "row implies managed PDF path already exists", failures)
    _require(not row.get("full_text"), "row preview unexpectedly contains full_text/evaluation content", failures)
    _require(_is_safe_harris_id(HARRIS_ID_PREVIEW), "Harris ID failed safe-ID convention check", failures)
    _require(HARRIS_ID_PREVIEW == "Reuter_2016_Neuroeconomics_edited", "Harris ID is not the exact Stage O Neuroeconomics ID", failures)
    return row, failures


def _db_row_exists(conn: sqlite3.Connection, paper_id: str) -> bool:
    return conn.execute("SELECT 1 FROM papers WHERE id = ? LIMIT 1", (paper_id,)).fetchone() is not None


def _db_row_by_id(conn: sqlite3.Connection, paper_id: str) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    for key in papers_db.JSON_FIELDS:
        if key in out:
            out[key] = _json_obj(out.get(key))
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
                st = path.stat()
            except OSError:
                continue
            rel = str(path.relative_to(config.BASE))
            entry = f"{rel}|{st.st_size}|{st.st_mtime_ns}"
            entries.append(entry)
            h.update(entry.encode("utf-8", errors="replace"))
            h.update(b"\n")
    return {"count": len(entries), "digest": h.hexdigest()}


def _create_sqlite_backup(suffix: str) -> dict[str, Any]:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = config.BACKUP_DIR / f"papers-{ts}-{suffix}.db"
    source = sqlite3.connect(str(config.DB_PATH))
    backup = sqlite3.connect(str(dest))
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    size = dest.stat().st_size if dest.exists() else 0
    usable = False
    quick_check = ""
    row_count = None
    if dest.exists():
        conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        try:
            quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            row_count = _table_count(conn)
            usable = quick_check == "ok" and size > 1024 * 1024
        finally:
            conn.close()
    return {
        "ok": usable,
        "path": str(dest),
        "bytes": size,
        "quick_check": quick_check,
        "row_count": row_count,
        "detail": "fresh PRE-HARRIS-PARENT backup created and verified" if usable else "backup failed validation",
    }


def _acquire_lock(run_dir: Path) -> Path:
    lock_path = run_dir.parent / ".corpus_pipeline.lock"
    payload = json.dumps(
        {
            "stage": "live-harris-parent-record-only",
            "run_id": run_dir.name,
            "created_at": _now(),
            "pid": os.getpid(),
        },
        indent=2,
    )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(lock_path), flags)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return lock_path


def _release_lock(lock_path: Path | None) -> None:
    if lock_path and lock_path.exists():
        lock_path.unlink()


def _build_live_row(stage_row: dict[str, Any]) -> dict[str, Any]:
    basic = dict(stage_row["basic_metadata"])
    file_info = dict(stage_row["file_info"])
    file_info.update(
        {
            "filepath": None,
            "renamed_filename": None,
            "managed_pdf_action": "not_copied_or_moved_in_parent_record_pilot",
            "managed_pdf_destination_preview_only": file_info.pop("managed_pdf_destination_preview", None),
        }
    )
    return {
        "id": HARRIS_ID_PREVIEW,
        "timestamp_added": _now(),
        "document_type": "edited_volume",
        "basic_metadata": basic,
        "identifiers": {
            "doi": basic.get("doi") or None,
            "isbn": basic.get("isbn") or [],
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
            "created_by": "live-harris-parent-record-only",
            "source_run_id": None,
            "guideline_policy": "guideline_mismatch_or_new_guideline_needed_candidate is metadata/routing, not exclusion",
            "child_chapter_policy": stage_row.get("child_chapter_policy"),
            "pdf_policy": "staged PDF retained; no managed copy/move in this stage",
            "evaluation_policy": "not evaluated in parent-record pilot",
            "duplicate_policy": "Stage O duplicate reassessment returned no_duplicate_signal",
            "route_status": "created_by_harris_parent_record_pilot",
        },
        "classification": {
            "document_type": "edited_volume",
            "evaluation_status": stage_row.get("evaluation_status"),
            "rating": None,
            "red_team_survival": None,
            "route_status": "created_by_harris_parent_record_pilot",
        },
        "tags": stage_row.get("tags") or [],
        "file_info": file_info,
        "full_text": None,
    }


def _insert_live_row(conn: sqlite3.Connection, paper: dict[str, Any]) -> None:
    valid, errors = papers_db.validate_paper(paper)
    if not valid:
        raise RuntimeError(f"Paper validation failed: {'; '.join(errors)}")
    cols = papers_db.ALL_COLUMNS
    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT INTO papers ({','.join(cols)}) VALUES ({placeholders})"
    conn.execute(sql, papers_db._paper_to_row(paper))


def _future_commands(run_id: str) -> list[str]:
    return [
        f"DO NOT RUN YET: python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 --run-id {run_id} --execute-stage live-harris-pdf-copy-only --allow-pdf-copy",
        f"DO NOT RUN YET: python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 --run-id {run_id} --execute-stage live-harris-fileinfo-attach-only --allow-db-write",
        f"DO NOT RUN YET: python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 --run-id {run_id} --execute-stage live-harris-staging-archive-only --allow-staging-archive",
        f"DO NOT RUN YET: python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 --run-id {run_id} --execute-stage broader-ingest-pilot",
    ]


def run_live_harris_parent_record_only(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    stage_o = _read_stage_o(run_dir)
    stage_row, failures = _validate_stage_o(stage_o)
    if failures:
        summary = _failure_summary(run_dir, "pre_write_validation_failed", failures)
        _write_reports(run_dir, summary)
        return summary

    lock_path: Path | None = None
    backup_info: dict[str, Any] | None = None
    inserted = False
    recovery_notes: list[str] = []
    conn: sqlite3.Connection | None = None
    before_count = after_count = None
    before_digest = after_digest = ""
    before_pdf = after_pdf = {}
    read_back = None
    live_row = _build_live_row(stage_row)
    live_row["notes_and_flags"]["source_run_id"] = run_dir.name

    try:
        lock_path = _acquire_lock(run_dir)
        before_pdf = _pdf_snapshot()
        backup_info = _create_sqlite_backup("PRE-HARRIS-PARENT")
        if not backup_info.get("ok"):
            raise RuntimeError("Fresh PRE-HARRIS-PARENT backup failed validation")

        conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")

        before_count = _table_count(conn)
        before_digest = _non_target_digest(conn, HARRIS_ID_PREVIEW)
        if _db_row_exists(conn, HARRIS_ID_PREVIEW):
            conn.rollback()
            summary = _failure_summary(run_dir, "id_collision_before_write", [f"{HARRIS_ID_PREVIEW} already exists"], backup_info)
            _write_reports(run_dir, summary)
            return summary

        _insert_live_row(conn, live_row)
        conn.commit()
        inserted = True

        after_count = _table_count(conn)
        after_digest = _non_target_digest(conn, HARRIS_ID_PREVIEW)
        read_back = _db_row_by_id(conn, HARRIS_ID_PREVIEW)
    except Exception as e:
        if conn is not None and not inserted:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        recovery_notes.append(str(e))
        summary = _failure_summary(run_dir, "live_write_failed", recovery_notes, backup_info)
        _write_reports(run_dir, summary)
        return summary
    finally:
        if conn is not None:
            conn.close()
        _release_lock(lock_path)

    after_pdf = _pdf_snapshot()
    post_failures = _post_write_failures(
        before_count=before_count,
        after_count=after_count,
        before_digest=before_digest,
        after_digest=after_digest,
        before_pdf=before_pdf,
        after_pdf=after_pdf,
        read_back=read_back,
    )
    summary = {
        "run_id": run_dir.name,
        "stage": "live-harris-parent-record-only",
        "processed_only": [HARRIS_NAME],
        "backup": backup_info,
        "lock_released": not (lock_path and lock_path.exists()),
        "pre_write_validation": {"ok": True, "failures": []},
        "row_created": post_failures == [],
        "row_id": HARRIS_ID_PREVIEW,
        "row_metadata_saved": {
            "document_type": live_row["document_type"],
            "basic_metadata": live_row["basic_metadata"],
            "identifiers": live_row["identifiers"],
            "file_info": live_row["file_info"],
            "classification": live_row["classification"],
            "tags": live_row["tags"],
            "notes_and_flags": live_row["notes_and_flags"],
        },
        "db_validation": {
            "before_count": before_count,
            "after_count": after_count,
            "exactly_one_row_added": after_count == before_count + 1 if before_count is not None and after_count is not None else False,
            "new_row_id": read_back.get("id") if read_back else None,
            "non_target_digest_before": before_digest,
            "non_target_digest_after": after_digest,
            "no_non_target_rows_changed": before_digest == after_digest,
            "row_read_back_cleanly": _read_back_ok(read_back),
            "schema_validation": "passed",
        },
        "pdf_validation": {
            "before": before_pdf,
            "after": after_pdf,
            "no_pdfs_moved_copied_renamed_deleted": before_pdf == after_pdf,
        },
        "post_write_failures": post_failures,
        "future_commands_preview_do_not_run_yet": _future_commands(run_dir.name),
        "side_effects": {
            "api_calls": "none",
            "evaluations": "none",
            "papers_db_writes": "one new Harris parent row inserted",
            "existing_db_row_updates": "none",
            "new_db_rows": [HARRIS_ID_PREVIEW],
            "pdf_moves_copies_renames_deletes": "none",
            "id_changes": "assigned single new Harris parent row ID only",
            "child_chapter_records_created": "none",
            "sqlite_bookkeeping_note": "SQLite may update normal journal/WAL bookkeeping for the committed insert",
        },
    }
    _write_reports(run_dir, summary)
    return summary


def _read_back_ok(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    bm = row.get("basic_metadata") or {}
    fi = row.get("file_info") or {}
    cl = row.get("classification") or {}
    nf = row.get("notes_and_flags") or {}
    return (
        row.get("id") == HARRIS_ID_PREVIEW
        and row.get("document_type") == "edited_volume"
        and bm.get("title") == EXPECTED_TITLE
        and bm.get("authors") == EXPECTED_EDITORS
        and str(bm.get("year")) == EXPECTED_YEAR
        and bm.get("publisher") == EXPECTED_PUBLISHER
        and bm.get("source_or_series") == EXPECTED_SERIES
        and bm.get("doi") == EXPECTED_DOI
        and set(EXPECTED_ISBNS) <= set(bm.get("isbn") or [])
        and fi.get("original_filename") == HARRIS_NAME
        and fi.get("staged_sha256") == HARRIS_HASH
        and fi.get("managed_pdf_action") == "not_copied_or_moved_in_parent_record_pilot"
        and cl.get("evaluation_status") == EXPECTED_EVAL_STATUS
        and "defer" in str(nf.get("child_chapter_policy", "")).lower()
    )


def _post_write_failures(**kwargs: Any) -> list[str]:
    failures: list[str] = []
    before_count = kwargs["before_count"]
    after_count = kwargs["after_count"]
    read_back = kwargs["read_back"]
    _require(after_count == before_count + 1 if before_count is not None and after_count is not None else False, "DB row count did not increase by exactly one", failures)
    _require(kwargs["before_digest"] == kwargs["after_digest"], "non-target row digest changed", failures)
    _require(kwargs["before_pdf"] == kwargs["after_pdf"], "PDF snapshot changed", failures)
    _require(_read_back_ok(read_back), "new Harris row did not read back cleanly", failures)
    return failures


def _failure_summary(run_dir: Path, status: str, failures: list[str], backup_info: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "run_id": run_dir.name,
        "stage": "live-harris-parent-record-only",
        "processed_only": [HARRIS_NAME],
        "status": status,
        "row_created": False,
        "row_id": None,
        "backup": backup_info,
        "pre_write_validation": {"ok": False, "failures": failures},
        "post_write_failures": failures,
        "recovery_notes": [
            "No automatic retry or broad rollback was attempted.",
            "Inspect this artifact and the DB state before rerunning; rerun will stop if the target ID already exists.",
        ],
        "future_commands_preview_do_not_run_yet": _future_commands(run_dir.name),
        "side_effects": {
            "api_calls": "none",
            "evaluations": "none",
            "papers_db_writes": "none" if status != "live_write_failed" else "unknown; inspect DB state",
            "existing_db_row_updates": "none",
            "pdf_moves_copies_renames_deletes": "none",
            "child_chapter_records_created": "none",
        },
    }


def _write_reports(run_dir: Path, summary: dict[str, Any]) -> None:
    (run_dir / "live_harris_parent_record_only.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "stage_live_harris_parent_record_only_report.txt").write_text(render_live_harris_parent_report(summary), encoding="utf-8")


def render_live_harris_parent_report(summary: dict[str, Any]) -> str:
    lines = ["Stage P Live Harris Parent Record Only", "=" * 56]
    lines.append(f"Processed only: {summary.get('processed_only')}")
    backup = summary.get("backup") or {}
    lines.append(f"Backup: {backup.get('path')} ({backup.get('bytes')} bytes) ok={backup.get('ok')}")
    lines.append(f"Row created: {summary.get('row_created')} id={summary.get('row_id')}")
    dbv = summary.get("db_validation") or {}
    if dbv:
        lines.append(f"DB count: {dbv.get('before_count')} -> {dbv.get('after_count')}")
        lines.append(f"Exactly one row added: {dbv.get('exactly_one_row_added')}")
        lines.append(f"No non-target rows changed: {dbv.get('no_non_target_rows_changed')}")
        lines.append(f"Row read-back cleanly: {dbv.get('row_read_back_cleanly')}")
    pdfv = summary.get("pdf_validation") or {}
    if pdfv:
        lines.append(f"No PDF moves/copies/renames/deletes: {pdfv.get('no_pdfs_moved_copied_renamed_deleted')}")
    lines.append(f"Post-write failures: {summary.get('post_write_failures')}")
    lines.append("")
    lines.append("Future commands preview (DO NOT RUN YET):")
    lines.extend(f"- {cmd}" for cmd in summary.get("future_commands_preview_do_not_run_yet", []))
    lines.append("")
    lines.append("No API calls, evaluations, PDF operations, existing-row updates, or child/chapter records were performed.")
    return "\n".join(lines) + "\n"
