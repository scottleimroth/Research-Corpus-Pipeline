"""Read-only classifier for rows whose local PDF text extraction needs OCR."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

MIN_TEXT_CHARS = 500


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


def _text(raw: Any) -> str:
    obj = _json_obj(raw)
    if obj:
        return str(obj.get("text") or obj.get("full_text") or obj.get("content") or "")
    return str(raw or "")


def _source_path(file_info: dict[str, Any]) -> Path | None:
    rel = file_info.get("filepath") or file_info.get("managed_pdf_relative_path")
    if not rel:
        return None
    raw = Path(str(rel))
    return raw if raw.is_absolute() else config.MASTER_PROJECT / raw


def _classify(row: sqlite3.Row) -> tuple[str, dict[str, Any]]:
    full_text = _json_obj(row["full_text"])
    extraction = _json_obj(full_text.get("extraction"))
    text = _text(row["full_text"])
    file_info = _json_obj(row["file_info"])
    source = _source_path(file_info)
    source_exists = bool(source and source.exists())
    error = str(extraction.get("error") or "")
    text_len = int(extraction.get("text_length") or len(text))

    if not source_exists:
        category = "possible_file_issue"
    elif error and "syntax" in error.lower():
        category = "extraction_failed_or_corrupt"
    elif error and "exit" in error.lower():
        category = "extraction_failed_or_corrupt"
    elif text_len == 0 or text.strip("\f\r\n\t ") == "":
        category = "likely_scanned_image_only"
    elif text_len < MIN_TEXT_CHARS:
        lowered = text.lower()
        if any(marker in lowered for marker in ("supplement", "appendix", "cover", "copyright", "contents")):
            category = "too_short_non_paper_or_front_matter"
        else:
            category = "likely_scanned_image_only"
    else:
        category = "possible_file_issue"

    return category, {
        "id": row["id"],
        "text_length": text_len,
        "error": error,
        "source_exists": source_exists,
        "source_pdf": str(source) if source else None,
    }


def audit_ocr_needed() -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, file_info, full_text FROM papers ORDER BY id").fetchall()
    finally:
        conn.close()

    counts: Counter[str] = Counter()
    examples: dict[str, list[dict[str, Any]]] = {}
    total = 0
    for row in rows:
        obj = _json_obj(row["full_text"])
        extraction = _json_obj(obj.get("extraction"))
        text_len = len(_text(row["full_text"]))
        if not extraction.get("ocr_needed") and text_len >= MIN_TEXT_CHARS:
            continue
        if not extraction and text_len == 0:
            continue
        total += 1
        category, detail = _classify(row)
        counts[category] += 1
        examples.setdefault(category, [])
        if len(examples[category]) < 8:
            examples[category].append(detail)

    ocrmypdf = shutil.which("ocrmypdf")
    tesseract = shutil.which("tesseract")
    future_command = None
    if ocrmypdf and tesseract:
        future_command = (
            "DO NOT RUN YET: python pipeline\\run_one_file_ocr.py "
            "--id <paper_id> --execute --allow-local-ocr --allow-db-write"
        )

    return {
        "read_only": True,
        "ocr_needed_rows": total,
        "categories": dict(counts),
        "examples": examples,
        "local_ocr_tools": {"ocrmypdf": ocrmypdf, "tesseract": tesseract},
        "future_ocr_command": future_command,
        "side_effects": {"db_writes": "none", "pdf_operations": "none", "api_calls": "none", "evaluation": "none"},
    }


def main() -> None:
    print(json.dumps(audit_ocr_needed(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
