"""Safely materialise full text from canonical source PDFs into papers.db.

Default mode is dry-run. Live mode is deliberately narrow and requires
`--execute --allow-db-write`; it creates a SQLite backup first and validates
that only target rows' `full_text` cells changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import papers_db  # noqa: E402

PILOT_IDS = [
    "Barrett_2016_Handbook_book",
    "Reuter_2016_Neuroeconomics_edited",
    "Cacioppo_2013_Handbook_book",
    "AshleyTimmers_2017_RoutledgeCompanion_book",
]
MIN_TEXT_CHARS = 500


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
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _text_len(raw: Any) -> int:
    obj = _json_obj(raw)
    if obj:
        return len(str(obj.get("text") or obj.get("full_text") or obj.get("content") or ""))
    return len(str(raw or ""))


def _full_text_materialized(raw: Any) -> bool:
    obj = _json_obj(raw)
    if obj.get("extraction"):
        return True
    return _text_len(raw) > 0


def _pdf_snapshot_digest() -> dict[str, Any]:
    h = hashlib.sha256()
    count = 0
    for root in (config.STAGING, config.SOURCE_PDFS):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.pdf")):
            stat = path.stat()
            rel = str(path.relative_to(config.BASE))
            payload = f"{rel}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8", errors="replace")
            h.update(len(payload).to_bytes(8, "big"))
            h.update(payload)
            count += 1
    return {"count": count, "digest": h.hexdigest()}


def _run_pdftotext(pdf_path: Path) -> tuple[str, str, float]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            ["pdftotext", str(pdf_path), "-"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except FileNotFoundError:
        return "", "pdftotext_not_found", time.perf_counter() - started
    except subprocess.TimeoutExpired:
        return "", "pdftotext_timeout", time.perf_counter() - started
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        return proc.stdout or "", (proc.stderr or f"pdftotext_exit_{proc.returncode}").strip(), elapsed
    return proc.stdout or "", "", elapsed


def _db_digest(conn: sqlite3.Connection, *, target_ids: set[str]) -> dict[str, str]:
    conn.row_factory = sqlite3.Row
    h_non_full_text = hashlib.sha256()
    h_non_target_full_text = hashlib.sha256()
    cols_without_full_text = [c for c in papers_db.ALL_COLUMNS if c != "full_text"]
    for row in conn.execute(f"SELECT {','.join(papers_db.ALL_COLUMNS)} FROM papers ORDER BY id"):
        vals = [row[col] for col in cols_without_full_text]
        encoded = json.dumps(vals, ensure_ascii=False, default=str).encode("utf-8")
        h_non_full_text.update(len(encoded).to_bytes(8, "big"))
        h_non_full_text.update(encoded)
        if row["id"] not in target_ids:
            encoded_full_text = json.dumps([row["id"], row["full_text"]], ensure_ascii=False, default=str).encode("utf-8")
            h_non_target_full_text.update(len(encoded_full_text).to_bytes(8, "big"))
            h_non_target_full_text.update(encoded_full_text)
    return {
        "non_full_text_digest": h_non_full_text.hexdigest(),
        "non_target_full_text_digest": h_non_target_full_text.hexdigest(),
    }


def _create_backup() -> dict[str, Any]:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.BACKUP_DIR / f"papers-{datetime.now():%Y%m%d-%H%M%S}-PRE-FULLTEXT-MATERIALIZE.db"
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
        row_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
    finally:
        conn.close()
    return {
        "ok": dest.exists() and size > 1024 * 1024 and quick_check == "ok",
        "path": str(dest),
        "bytes": size,
        "quick_check": quick_check,
        "row_count": row_count,
    }


def _row_source_path(row: sqlite3.Row) -> tuple[Path | None, dict[str, Any]]:
    file_info = _json_obj(row["file_info"])
    rel = file_info.get("filepath") or file_info.get("managed_pdf_relative_path")
    if not rel:
        return None, file_info
    raw_path = Path(rel)
    source = raw_path if raw_path.is_absolute() else config.MASTER_PROJECT / raw_path
    return source, file_info


def _row_has_existing_source(row: sqlite3.Row) -> bool:
    source, _ = _row_source_path(row)
    return bool(source and source.exists())


def _select_rows(conn: sqlite3.Connection, *, ids: list[str], limit: int, force: bool) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    selected: list[sqlite3.Row] = []
    selected_ids: set[str] = set()
    if ids:
        for paper_id in ids:
            row = conn.execute(
                "SELECT id, document_type, basic_metadata, file_info, full_text FROM papers WHERE id = ?",
                (paper_id,),
            ).fetchone()
            if row and _row_has_existing_source(row) and (force or not _full_text_materialized(row["full_text"])):
                selected.append(row)
                selected_ids.add(row["id"])
            if len(selected) >= limit:
                return selected[:limit]
    for row in conn.execute("SELECT id, document_type, basic_metadata, file_info, full_text FROM papers ORDER BY id"):
        if row["id"] in selected_ids:
            continue
        if _row_has_existing_source(row) and (force or not _full_text_materialized(row["full_text"])):
            selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _materialize_preview(row: sqlite3.Row) -> dict[str, Any]:
    source, file_info = _row_source_path(row)
    if source is None:
        return {"id": row["id"], "status": "missing_file_info_path", "text_length": 0, "ocr_needed": True}
    if not source.exists():
        return {"id": row["id"], "status": "source_pdf_missing", "source_pdf": str(source), "text_length": 0, "ocr_needed": True}
    source_hash = _sha256(source)
    text, error, seconds = _run_pdftotext(source)
    text_length = len(text)
    ocr_needed = bool(error) or text_length < MIN_TEXT_CHARS
    status = "ocr_needed" if ocr_needed else "extracted"
    payload = {
        "text": text,
        "extraction": {
            "status": status,
            "method": "pdftotext",
            "extracted_at": _now(),
            "text_length": text_length,
            "ocr_needed": ocr_needed,
            "error": error,
            "source_pdf": str(source.relative_to(config.BASE)) if source.is_relative_to(config.BASE) else str(source),
            "source_sha256": source_hash,
            "elapsed_seconds": round(seconds, 3),
            "paid_api_used": False,
            "evaluation_run": False,
        },
    }
    return {
        "id": row["id"],
        "status": status,
        "source_pdf": str(source),
        "source_sha256": source_hash,
        "file_info_hash": file_info.get("managed_pdf_sha256") or file_info.get("sha256"),
        "hash_matches_file_info": not (file_info.get("managed_pdf_sha256") or file_info.get("sha256"))
        or (file_info.get("managed_pdf_sha256") or file_info.get("sha256")) == source_hash,
        "text_length": text_length,
        "method": "pdftotext",
        "ocr_needed": ocr_needed,
        "error": error,
        "elapsed_seconds": round(seconds, 3),
        "payload": payload,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    ids = [part.strip() for part in (args.ids or "").split(",") if part.strip()]
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        rows = _select_rows(conn, ids=ids, limit=args.limit, force=args.force)
    finally:
        conn.close()
    previews = [_materialize_preview(row) for row in rows]
    if args.extracted_only:
        previews = [p for p in previews if p.get("status") == "extracted"]
    selected_ok = [p for p in previews if p.get("status") in {"extracted", "ocr_needed"} and p.get("hash_matches_file_info")]
    skipped_requested_ids = []
    if ids:
        selected_id_set = {row["id"] for row in rows}
        skipped_requested_ids = [paper_id for paper_id in ids if paper_id not in selected_id_set]
    summary: dict[str, Any] = {
        "created_at": _now(),
        "mode": "execute" if args.execute else "dry_run",
        "limit": args.limit,
        "selected_ids": [p["id"] for p in previews],
        "skipped_requested_ids": skipped_requested_ids,
        "rows": [{k: v for k, v in p.items() if k != "payload"} for p in previews],
        "side_effects": {"api_calls": "none", "evaluation": "none", "pdf_operations": "none"},
    }
    if not args.execute:
        summary["db_writes"] = "none"
        return {"ok": True, "summary": summary}
    if not args.allow_db_write:
        summary["status"] = "refused_missing_allow_db_write"
        return {"ok": False, "summary": summary, "message": "--execute requires --allow-db-write"}
    if not previews:
        summary["status"] = "no_eligible_rows"
        summary["db_writes"] = "none"
        return {"ok": True, "summary": summary, "message": "No eligible rows found."}
    if len(selected_ok) != len(previews):
        summary["status"] = "refused_failed_preview_validation"
        return {"ok": False, "summary": summary, "message": "Not all selected rows passed extraction/hash validation."}
    backup = _create_backup()
    summary["backup"] = backup
    if not backup.get("ok"):
        summary["status"] = "refused_backup_failed"
        return {"ok": False, "summary": summary, "message": "Backup failed validation."}
    target_ids = {p["id"] for p in previews}
    before_pdf = _pdf_snapshot_digest()
    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        before = _db_digest(conn, target_ids=target_ids)
        before_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        for preview in previews:
            conn.execute(
                "UPDATE papers SET full_text = ? WHERE id = ?",
                (json.dumps(preview["payload"], ensure_ascii=False), preview["id"]),
            )
        conn.commit()
        after = _db_digest(conn, target_ids=target_ids)
        after_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        read_back = []
        for preview in previews:
            row = conn.execute("SELECT id, full_text FROM papers WHERE id = ?", (preview["id"],)).fetchone()
            obj = _json_obj(row["full_text"] if row else None)
            extraction = obj.get("extraction") or {}
            read_back.append(
                {
                    "id": preview["id"],
                    "text_length": len(str(obj.get("text") or "")),
                    "method": extraction.get("method"),
                    "ocr_needed": extraction.get("ocr_needed"),
                    "status": extraction.get("status"),
                }
            )
    finally:
        conn.close()
    failures: list[str] = []
    if before_count != after_count:
        failures.append("row count changed")
    if before["non_full_text_digest"] != after["non_full_text_digest"]:
        failures.append("non-full_text columns changed")
    if before["non_target_full_text_digest"] != after["non_target_full_text_digest"]:
        failures.append("non-target full_text changed")
    after_pdf = _pdf_snapshot_digest()
    if before_pdf != after_pdf:
        failures.append("PDF snapshot changed")
    summary.update(
        {
            "status": "completed" if not failures else "post_write_validation_failed",
            "db_writes": "target full_text cells only",
            "backup": backup,
            "db_validation": {
                "before_count": before_count,
                "after_count": after_count,
                "row_count_unchanged": before_count == after_count,
                "non_full_text_columns_unchanged": before["non_full_text_digest"] == after["non_full_text_digest"],
                "non_target_full_text_unchanged": before["non_target_full_text_digest"] == after["non_target_full_text_digest"],
                "pdf_snapshot_unchanged": before_pdf == after_pdf,
                "pdf_snapshot_before": before_pdf,
                "pdf_snapshot_after": after_pdf,
                "failures": failures,
            },
            "materialized_rows": read_back,
        }
    )
    return {"ok": not failures, "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialise DB full_text from source-pdfs")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--ids", default="", help="Comma-separated paper IDs to prioritise/select")
    parser.add_argument("--force", action="store_true", help="Re-extract rows that already have full_text")
    parser.add_argument("--execute", action="store_true", help="Write selected rows' full_text after backup")
    parser.add_argument("--allow-db-write", action="store_true", help="Required with --execute")
    parser.add_argument("--allow-local-ocr", action="store_true", help="Reserved; OCR is not run by this tool yet")
    parser.add_argument("--extracted-only", action="store_true", help="Write only rows whose local extraction produced usable text")
    parser.add_argument("--summary-only", action="store_true", help="Print compact JSON summary without per-row extraction payloads")
    args = parser.parse_args()
    if args.allow_local_ocr:
        raise SystemExit("--allow-local-ocr is reserved for a future one-file OCR test; not implemented here")
    result = run(args)
    if args.summary_only:
        summary = result.get("summary", {})
        rows = summary.get("rows", [])
        compact = {
            "ok": result.get("ok"),
            "status": summary.get("status"),
            "mode": summary.get("mode"),
            "limit": summary.get("limit"),
            "selected_count": len(summary.get("selected_ids", [])),
            "backup": summary.get("backup"),
            "db_validation": summary.get("db_validation"),
            "status_counts": {
                status: sum(1 for row in rows if row.get("status") == status)
                for status in sorted({row.get("status") for row in rows})
            },
            "ocr_needed_count": sum(1 for row in rows if row.get("ocr_needed")),
            "min_text_length": min((int(row.get("text_length") or 0) for row in rows), default=0),
            "max_text_length": max((int(row.get("text_length") or 0) for row in rows), default=0),
            "sample_ids": summary.get("selected_ids", [])[:10],
        }
        print(json.dumps(compact, indent=2, ensure_ascii=False))
        raise SystemExit(0 if result.get("ok") else 1)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
