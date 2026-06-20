"""Read-only audit for DB rows whose source PDF is missing or ambiguous."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


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


def _source_pdf_names() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not config.SOURCE_PDFS.exists():
        return out
    for path in config.SOURCE_PDFS.glob("*.pdf"):
        out.setdefault(path.name.lower(), []).append(str(path))
    return out


def audit_source_pdf_reconciliation() -> dict[str, Any]:
    by_name = _source_pdf_names()
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, basic_metadata, file_info FROM papers ORDER BY id").fetchall()
    finally:
        conn.close()

    categories = {
        "file_info_missing": 0,
        "db_path_missing": 0,
        "noncanonical_existing_path": 0,
        "path_points_to_absent_file": 0,
        "likely_legacy_or_orphan": 0,
    }
    examples: dict[str, list[dict[str, Any]]] = {key: [] for key in categories}
    proposed_fixes: list[dict[str, Any]] = []

    for row in rows:
        file_info = _json_obj(row["file_info"])
        rel = file_info.get("filepath") or file_info.get("managed_pdf_relative_path")
        category = None
        if not file_info:
            category = "file_info_missing"
        elif not rel:
            category = "db_path_missing"
        else:
            raw_path = Path(rel)
            source = raw_path if raw_path.is_absolute() else config.MASTER_PROJECT / raw_path
            if source.exists():
                try:
                    source.resolve().relative_to(config.SOURCE_PDFS.resolve())
                except ValueError:
                    category = "noncanonical_existing_path"
                else:
                    continue
            else:
                category = "path_points_to_absent_file"
                basename = Path(rel).name.lower()
                matches = by_name.get(basename, [])
                if len(matches) == 1:
                    proposed_fixes.append(
                        {
                            "id": row["id"],
                            "current_filepath": rel,
                            "candidate_filepath": str(Path(matches[0]).relative_to(config.MASTER_PROJECT)),
                            "reason": "exact basename exists once under source-pdfs",
                            "do_not_run_yet": True,
                        }
                    )

        if category:
            categories[category] += 1
            if str(row["id"]).startswith(("orphan_", "1_s2_0_", "BF")) or "Unknown" in str(row["id"]):
                categories["likely_legacy_or_orphan"] += 1
                if len(examples["likely_legacy_or_orphan"]) < 10:
                    examples["likely_legacy_or_orphan"].append({"id": row["id"], "category": category, "filepath": rel})
            if len(examples[category]) < 10:
                examples[category].append({"id": row["id"], "filepath": rel})

    return {
        "read_only": True,
        "categories": categories,
        "examples": examples,
        "safe_fix_preview_count": len(proposed_fixes),
        "safe_fix_preview_do_not_run_yet": proposed_fixes[:25],
        "side_effects": {"db_writes": "none", "pdf_operations": "none", "api_calls": "none", "evaluation": "none"},
    }


def main() -> None:
    print(json.dumps(audit_source_pdf_reconciliation(), indent=2))


if __name__ == "__main__":
    main()
