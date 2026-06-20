"""Read-only storage policy audit for completed pilot PDFs.

Audits only the Koren, Harris, Cacioppo, and Ashley pilot records. It reports
processed-folder duplicates that already have a canonical source-pdfs file and
matching DB file_info, then prints a cleanup preview without deleting anything.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import papers_db  # noqa: E402


TARGETS = [
    {
        "label": "Koren",
        "id": "Barrett_2016_Handbook_book",
        "original_filename": "Koren-A kinetic study of protein-protein interactions-1976-B.pdf",
    },
    {
        "label": "Harris",
        "id": "Reuter_2016_Neuroeconomics_edited",
        "original_filename": "Harris-Effect of pegylation on pharmaceuticals-2003-Nature R.pdf",
    },
    {
        "label": "Cacioppo",
        "id": "Cacioppo_2013_Handbook_book",
        "original_filename": "(Cambridge Handbooks in Psychology) John T. Cacioppo (editor.pdf",
    },
    {
        "label": "Ashley",
        "id": "AshleyTimmers_2017_RoutledgeCompanion_book",
        "original_filename": "Ashley, Richard(Editor)_Timmers, Renee(Contributor) - The Ro.pdf",
    },
]


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


def _row_by_id(conn: sqlite3.Connection, paper_id: str) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM papers WHERE id = ? LIMIT 1", (paper_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    for key in papers_db.JSON_FIELDS:
        if key in out:
            out[key] = _json_obj(out[key])
    return out


def _processed_pdfs() -> list[Path]:
    root = config.STAGING / "processed"
    if not root.exists():
        return []
    return sorted(root.rglob("*.pdf"))


def audit_storage_policy() -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    processed = _processed_pdfs()
    items: list[dict[str, Any]] = []
    cleanup_preview: list[dict[str, Any]] = []
    try:
        for target in TARGETS:
            paper_id = target["id"]
            row = _row_by_id(conn, paper_id)
            file_info = (row or {}).get("file_info") or {}
            canonical = config.SOURCE_PDFS / f"{paper_id}.pdf"
            canonical_exists = canonical.exists()
            canonical_hash = _sha256(canonical) if canonical_exists else ""
            db_filepath = file_info.get("filepath")
            db_hash = file_info.get("managed_pdf_sha256") or file_info.get("sha256")
            db_file_info_matches = (
                row is not None
                and db_filepath == f"source-pdfs/{paper_id}.pdf"
                and bool(canonical_hash)
                and (not db_hash or db_hash == canonical_hash)
            )
            duplicate_candidates = []
            for path in processed:
                if path.name != target["original_filename"] and paper_id not in str(path):
                    continue
                path_hash = _sha256(path)
                duplicate = canonical_exists and path_hash == canonical_hash
                candidate = {
                    "path": str(path),
                    "sha256": path_hash,
                    "matches_source_pdf": duplicate,
                    "cleanup_safe_preview": duplicate and db_file_info_matches,
                }
                duplicate_candidates.append(candidate)
                if candidate["cleanup_safe_preview"]:
                    cleanup_preview.append(
                        {
                            "label": target["label"],
                            "processed_duplicate": str(path),
                            "canonical_source_pdf": str(canonical),
                            "sha256": path_hash,
                            "do_not_run_yet": f'Remove-Item "{path}"',
                        }
                    )
            items.append(
                {
                    "label": target["label"],
                    "id": paper_id,
                    "db_row_exists": row is not None,
                    "db_file_info_matches_source": db_file_info_matches,
                    "canonical_source_pdf": str(canonical),
                    "canonical_source_exists": canonical_exists,
                    "canonical_sha256": canonical_hash,
                    "processed_duplicates": duplicate_candidates,
                }
            )
    finally:
        conn.close()
    return {
        "read_only": True,
        "policy": "successful PDFs should have one canonical copy in CorpusStore/source-pdfs and no duplicate in papers-staging/processed",
        "targets": [target["label"] for target in TARGETS],
        "items": items,
        "duplicate_processed_pdf_count": sum(len(item["processed_duplicates"]) for item in items),
        "cleanup_preview_count": len(cleanup_preview),
        "cleanup_preview_do_not_run_yet": cleanup_preview,
        "side_effects": {"db_writes": "none", "pdf_moves_copies_deletes": "none", "api_calls": "none", "evaluation": "none"},
    }


def main() -> None:
    audit = audit_storage_policy()
    print("=" * 72)
    print("Storage Policy Audit (READ-ONLY)")
    print("=" * 72)
    print(audit["policy"])
    print(f"Targets: {', '.join(audit['targets'])}")
    print(f"Duplicate processed PDFs found: {audit['duplicate_processed_pdf_count']}")
    print(f"Cleanup preview entries: {audit['cleanup_preview_count']}")
    for item in audit["items"]:
        print(
            f"- {item['label']}: db={item['db_row_exists']} "
            f"source={item['canonical_source_exists']} "
            f"file_info_ok={item['db_file_info_matches_source']} "
            f"processed_duplicates={len(item['processed_duplicates'])}"
        )
    if audit["cleanup_preview_do_not_run_yet"]:
        print("\nCleanup preview (DO NOT RUN YET):")
        for entry in audit["cleanup_preview_do_not_run_yet"]:
            print(f"  {entry['label']}: {entry['do_not_run_yet']}")
    print("\nSafety state: READ-ONLY")
    print("- No DB writes")
    print("- No PDF moves/copies/deletes")
    print("- No API calls")
    print("- No evaluation")


if __name__ == "__main__":
    main()
