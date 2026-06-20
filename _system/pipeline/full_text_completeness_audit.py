"""Read-only full-text completeness audit for papers.db."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

PILOT_IDS = {
    "Koren": "Barrett_2016_Handbook_book",
    "Harris": "Reuter_2016_Neuroeconomics_edited",
    "Cacioppo": "Cacioppo_2013_Handbook_book",
    "Ashley": "AshleyTimmers_2017_RoutledgeCompanion_book",
}


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
    if not raw:
        return 0
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return len(raw)
    else:
        value = raw
    if isinstance(value, dict):
        return len(str(value.get("text") or value.get("full_text") or value.get("content") or ""))
    return len(str(value))


def _extraction(raw: Any) -> dict[str, Any]:
    obj = _json_obj(raw)
    return _json_obj(obj.get("extraction")) if obj else {}


def _materialized(raw: Any) -> bool:
    return bool(_extraction(raw)) or _text_len(raw) > 0


def audit_full_text() -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, document_type, file_info, full_text FROM papers ORDER BY id").fetchall()
    finally:
        conn.close()
    total = len(rows)
    present = 0
    missing = 0
    very_short = 0
    pdf_exists_text_missing = 0
    source_pdf_missing = 0
    likely_ocr_needed = 0
    extraction_status_counts: dict[str, int] = {}
    pilot_status: dict[str, Any] = {}
    examples: list[dict[str, Any]] = []
    for row in rows:
        text_chars = _text_len(row["full_text"])
        extraction = _extraction(row["full_text"])
        is_materialized = _materialized(row["full_text"])
        status = str(extraction.get("status") or ("legacy_text" if text_chars > 0 else "missing"))
        extraction_status_counts[status] = extraction_status_counts.get(status, 0) + 1
        file_info = _json_obj(row["file_info"])
        filepath = file_info.get("filepath") or file_info.get("managed_pdf_relative_path")
        source = config.MASTER_PROJECT / filepath if filepath else None
        source_exists = bool(source and source.exists())
        if is_materialized:
            present += 1
        else:
            missing += 1
        if is_materialized and text_chars < 500:
            very_short += 1
        if source and not source_exists:
            source_pdf_missing += 1
        if source_exists and not is_materialized:
            pdf_exists_text_missing += 1
            if len(examples) < 10:
                examples.append({"id": row["id"], "reason": "pdf_exists_text_missing", "filepath": filepath})
        if source_exists and ((is_materialized and extraction.get("ocr_needed")) or (not is_materialized and text_chars < 500)):
            likely_ocr_needed += 1
        for label, paper_id in PILOT_IDS.items():
            if row["id"] == paper_id:
                pilot_status[label] = {
                    "id": paper_id,
                    "source_pdf_exists": source_exists,
                    "full_text_chars": text_chars,
                    "full_text_present": is_materialized,
                    "extraction_status": status,
                    "likely_ocr_needed": source_exists and ((is_materialized and extraction.get("ocr_needed")) or (not is_materialized and text_chars < 500)),
                }
    return {
        "read_only": True,
        "total_rows": total,
        "full_text_present": present,
        "full_text_missing": missing,
        "full_text_very_short_lt_500": very_short,
        "pdf_exists_but_text_missing": pdf_exists_text_missing,
        "source_pdf_missing": source_pdf_missing,
        "likely_ocr_needed_lt_500_with_pdf": likely_ocr_needed,
        "extraction_status_counts": extraction_status_counts,
        "pilot_status": pilot_status,
        "actionable_examples": examples,
        "side_effects": {"db_writes": "none", "pdf_operations": "none", "api_calls": "none", "evaluation": "none"},
    }


def main() -> None:
    audit = audit_full_text()
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
