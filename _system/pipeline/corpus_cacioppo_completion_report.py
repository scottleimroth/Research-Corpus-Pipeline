"""Cacioppo-only final completion checkpoint.

Read-only verification of the completed Cacioppo parent ingest flow. This report
does not write papers.db, move/copy/delete PDFs, call APIs, or evaluate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
import papers_db

RUN_ID = "20260528-203823"
CACIOPPO_ID = "Cacioppo_2013_Handbook_book"
CACIOPPO_FILENAME = "(Cambridge Handbooks in Psychology) John T. Cacioppo (editor.pdf"
CACIOPPO_HASH = "6808405eb7e55ae50b1f36cc8f922b1012bd9209a04083ab8bb5db4f64655293"
MANAGED_RELATIVE_PATH = f"source-pdfs/{CACIOPPO_ID}.pdf"


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


def _db_digest(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(papers)").fetchall()]
    h = hashlib.sha256()
    for row in conn.execute(f"SELECT {','.join(cols)} FROM papers ORDER BY id"):
        vals = [row[col] for col in cols]
        encoded = json.dumps(vals, ensure_ascii=False, default=str).encode("utf-8")
        h.update(len(encoded).to_bytes(8, "big"))
        h.update(encoded)
    return {"count": count, "digest": h.hexdigest()}


def _row_by_id(conn: sqlite3.Connection, paper_id: str) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM papers WHERE id = ? LIMIT 1", (paper_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    for key in papers_db.JSON_FIELDS:
        if key in out:
            out[key] = _json_obj(out.get(key))
    return out


def _child_record_candidates(conn: sqlite3.Connection) -> list[str]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id
        FROM papers
        WHERE id != ?
          AND (
            id LIKE ?
            OR basic_metadata LIKE ?
            OR file_info LIKE ?
          )
        ORDER BY id
        LIMIT 20
        """,
        (
            CACIOPPO_ID,
            f"{CACIOPPO_ID}%",
            "%HANDBOOK OF PSYCHOPHYSIOLOGY%",
            f"%{CACIOPPO_FILENAME}%",
        ),
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": ok, "detail": detail}


def render_cacioppo_completion_report(report: dict[str, Any]) -> str:
    lines = [
        "Cacioppo Final Completion Checkpoint (READ-ONLY)",
        "=" * 58,
        f"Run ID: {report.get('run_id')}",
        f"Complete: {report.get('complete')}",
        "",
        "Verification results",
        "-" * 58,
    ]
    for check in report.get("checks", []):
        status = "PASS" if check.get("ok") else "FAIL"
        lines.append(f"{status}: {check.get('check')} - {check.get('detail')}")
    lines.extend(
        [
            "",
            "Artifacts",
            "-" * 58,
            f"Managed PDF: {report.get('managed_pdf_path')}",
            f"Archive path: {report.get('archive_path')}",
            "",
            "Safety",
            "-" * 58,
            "No DB writes, PDF operations, API calls, evaluation, or child/chapter records were performed by this report.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_cacioppo_completion_report(args) -> dict[str, Any]:
    run_id = getattr(args, "run_id", "") or RUN_ID
    run_dir = config.RUNS_DIR / run_id
    managed = config.SOURCE_PDFS / f"{CACIOPPO_ID}.pdf"
    archive_path = config.STAGING / "processed" / CACIOPPO_ID / CACIOPPO_FILENAME
    original_staging_path = config.STAGING / CACIOPPO_FILENAME

    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        before_db = _db_digest(conn)
        row = _row_by_id(conn, CACIOPPO_ID)
        child_candidates = _child_record_candidates(conn)
        after_db = _db_digest(conn)
    finally:
        conn.close()

    file_info = (row or {}).get("file_info") or {}
    classification = (row or {}).get("classification") or {}
    managed_hash = _sha256(managed) if managed.exists() else ""
    archive_hash = _sha256(archive_path) if archive_path.exists() else ""

    checks = [
        _check("run directory exists", run_dir.exists(), str(run_dir)),
        _check("DB row exists", row is not None and row.get("id") == CACIOPPO_ID if row else False, CACIOPPO_ID),
        _check("file_info filepath", file_info.get("filepath") == MANAGED_RELATIVE_PATH, str(file_info.get("filepath"))),
        _check("file_info managed hash", file_info.get("managed_pdf_sha256") == CACIOPPO_HASH, str(file_info.get("managed_pdf_sha256"))),
        _check("managed PDF exists", managed.exists(), str(managed)),
        _check("managed PDF hash matches", managed_hash == CACIOPPO_HASH, managed_hash),
        _check("archive exists", archive_path.exists(), str(archive_path)),
        _check("archive hash matches managed PDF", archive_hash == managed_hash == CACIOPPO_HASH, archive_hash),
        _check("original staging path cleared", not original_staging_path.exists(), str(original_staging_path)),
        _check("DB count unchanged during report", before_db.get("count") == after_db.get("count"), f"{before_db.get('count')} -> {after_db.get('count')}"),
        _check("DB digest unchanged during report", before_db.get("digest") == after_db.get("digest"), before_db.get("digest", "")),
        _check("evaluation deferred", classification.get("evaluation_status") == "deferred_not_evaluated_for_book_parent", str(classification.get("evaluation_status"))),
        _check("no child/chapter records found", not child_candidates, json.dumps(child_candidates, ensure_ascii=False)),
    ]

    report = {
        "created_at": _now(),
        "run_id": run_id,
        "stage": "cacioppo-final-completion-checkpoint",
        "read_only": True,
        "processed_only": [CACIOPPO_ID],
        "db_row_id": CACIOPPO_ID,
        "managed_pdf_path": str(managed),
        "archive_path": str(archive_path),
        "original_staging_path": str(original_staging_path),
        "managed_pdf_hash": managed_hash,
        "archive_hash": archive_hash,
        "db_digest_before": before_db,
        "db_digest_after": after_db,
        "child_record_candidates": child_candidates,
        "checks": checks,
        "complete": all(check["ok"] for check in checks),
        "side_effects": {
            "db_writes": "none",
            "pdf_operations": "none",
            "api_calls": "none",
            "evaluations": "none",
            "child_chapter_records_created": "none",
        },
    }

    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "cacioppo_final_completion_checkpoint.json"
    report_path = run_dir / "cacioppo_final_completion_checkpoint_report.txt"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(render_cacioppo_completion_report(report), encoding="utf-8")

    return {
        "ok": report["complete"],
        "report": report,
        "artifact_paths": {"json": str(json_path), "report": str(report_path)},
        "message": "Cacioppo flow is complete." if report["complete"] else "Cacioppo completion checkpoint has failures.",
    }
