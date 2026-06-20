"""Read-only exports from papers.db.

Supports metadata CSV/XLSX exports that exclude `full_text` by default, plus a
JSONL export for rows with materialised full text. The tool never writes to the
database and defaults to dry-run.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import papers_db  # noqa: E402

EXPORT_DIR = config.MASTER_PROJECT / "exports"


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


def _json_value(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


def _full_text_payload(raw: Any) -> tuple[str, dict[str, Any]]:
    obj = _json_obj(raw)
    if obj:
        return str(obj.get("text") or obj.get("full_text") or obj.get("content") or ""), _json_obj(obj.get("extraction"))
    return str(raw or ""), {}


def _metadata_record(row: sqlite3.Row, *, include_full_text: bool) -> dict[str, Any]:
    basic = _json_obj(row["basic_metadata"])
    file_info = _json_obj(row["file_info"])
    text, extraction = _full_text_payload(row["full_text"])
    record: dict[str, Any] = {
        "id": row["id"],
        "timestamp_added": row["timestamp_added"],
        "document_type": row["document_type"],
        "title": basic.get("title"),
        "authors": json.dumps(basic.get("authors"), ensure_ascii=False) if isinstance(basic.get("authors"), (list, dict)) else basic.get("authors"),
        "year": basic.get("year"),
        "doi": basic.get("doi"),
        "journal": basic.get("journal") or basic.get("venue"),
        "source_pdf": file_info.get("filepath") or file_info.get("managed_pdf_relative_path"),
        "full_text_present": bool(extraction) or bool(text),
        "full_text_chars": len(text),
        "extraction_status": extraction.get("status"),
        "ocr_needed": extraction.get("ocr_needed"),
        "rating": row["rating"],
        "tags": row["tags"],
    }
    if include_full_text:
        record["full_text"] = text
    return record


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_rows(limit: int | None) -> list[sqlite3.Row]:
    sql = f"SELECT {','.join(papers_db.ALL_COLUMNS)} FROM papers ORDER BY id"
    if limit:
        sql += " LIMIT ?"
    conn = _connect()
    try:
        if limit:
            return conn.execute(sql, (limit,)).fetchall()
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _default_path(kind: str, suffix: str) -> Path:
    return EXPORT_DIR / f"papers-{kind}-{datetime.now():%Y%m%d-%H%M%S}.{suffix}"


def export_metadata_csv(rows: list[sqlite3.Row], out: Path, *, include_full_text: bool) -> int:
    records = [_metadata_record(row, include_full_text=include_full_text) for row in rows]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0].keys()) if records else ["id"])
        writer.writeheader()
        writer.writerows(records)
    return len(records)


def export_metadata_xlsx(rows: list[sqlite3.Row], out: Path, *, include_full_text: bool) -> int:
    try:
        import openpyxl  # type: ignore
    except ImportError as exc:
        raise RuntimeError("XLSX export requires openpyxl; install it or use CSV") from exc
    records = [_metadata_record(row, include_full_text=include_full_text) for row in rows]
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "papers"
    headers = list(records[0].keys()) if records else ["id"]
    ws.append(headers)
    for record in records:
        ws.append([record.get(header) for header in headers])
    wb.save(out)
    return len(records)


def export_full_text_jsonl(rows: list[sqlite3.Row], out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            text, extraction = _full_text_payload(row["full_text"])
            if not text and not extraction:
                continue
            basic = _json_obj(row["basic_metadata"])
            payload = {
                "id": row["id"],
                "title": basic.get("title"),
                "year": basic.get("year"),
                "document_type": row["document_type"],
                "text": text,
                "extraction": extraction,
            }
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            count += 1
    return count


def run(args: argparse.Namespace) -> dict[str, Any]:
    rows = _load_rows(args.limit)
    out = Path(args.out) if args.out else _default_path(args.kind, "jsonl" if args.kind == "full-text-jsonl" else args.kind)
    preview = {
        "mode": "dry_run" if args.dry_run else "export",
        "kind": args.kind,
        "row_limit": args.limit,
        "selected_rows": len(rows),
        "output_path": str(out),
        "include_full_text": bool(args.include_full_text),
        "side_effects": {"db_writes": "none", "pdf_operations": "none", "api_calls": "none", "evaluation": "none"},
    }
    if args.dry_run:
        return {"ok": True, **preview, "written_rows": 0}
    if args.kind == "csv":
        written = export_metadata_csv(rows, out, include_full_text=args.include_full_text)
    elif args.kind == "xlsx":
        written = export_metadata_xlsx(rows, out, include_full_text=args.include_full_text)
    elif args.kind == "full-text-jsonl":
        written = export_full_text_jsonl(rows, out)
    else:
        raise ValueError(f"unsupported export kind: {args.kind}")
    return {"ok": True, **preview, "written_rows": written}


def main() -> None:
    parser = argparse.ArgumentParser(description="Export corpus metadata/full_text from papers.db")
    parser.add_argument("--kind", choices=["csv", "xlsx", "full-text-jsonl"], required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--include-full-text", action="store_true", help="Only valid for metadata CSV/XLSX; disabled by default")
    parser.add_argument("--dry-run", action="store_true", help="Preview export without writing an output file")
    args = parser.parse_args()
    if args.kind == "full-text-jsonl" and args.include_full_text:
        raise SystemExit("--include-full-text is only for metadata CSV/XLSX; JSONL always includes text")
    result = run(args)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
