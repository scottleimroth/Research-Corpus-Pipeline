#!/usr/bin/env python3
"""
validate_db.py — Check papers database for entries that bypassed the pipeline.

Pipeline entries have: full_text.extraction_method, full_text.sections, timestamp_added
Contaminated entries are missing these fields.

Usage:
    python3 validate_db.py          # Report contaminated entries
    python3 validate_db.py --fix    # Remove contaminated entries and log them
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

# --- Portable path bootstrap (Phase 2 repath) ---
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from config import ERROR_LOG  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import papers_db  # noqa: E402


def validate():
    all_papers = papers_db.get_all_metadata()
    clean = []
    contaminated = []

    for p in all_papers:
        pid = p.get("id", "")
        ft = papers_db.load_fulltext(pid) if pid else {}
        has_method = bool(ft.get("extraction_method")) if isinstance(ft, dict) else False
        has_sections = bool(ft.get("sections")) if isinstance(ft, dict) else False
        has_timestamp = bool(p.get("timestamp_added"))

        ts = p.get("timestamp_added", "")
        if ts and ts >= "2026-02-25" and not has_method:
            contaminated.append(p)
        else:
            clean.append(p)

    return all_papers, clean, contaminated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="Remove contaminated entries")
    args = parser.parse_args()

    all_papers, clean, contaminated = validate()

    if not contaminated:
        print(f"All {len(all_papers)} entries are clean")
        return

    print(f"Found {len(contaminated)} potentially contaminated entries:")
    for p in contaminated:
        bm = p.get("basic_metadata", {})
        print(f"  - {p.get('id', '?')} | {(bm.get('title') or '?')[:60]} | ts={p.get('timestamp_added', '?')[:10]}")

    if args.fix:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a") as f:
            for p in contaminated:
                f.write(f"[{datetime.now().isoformat()}] CONTAMINATION_REMOVED | {p.get('id')} | {(p.get('basic_metadata',{}).get('title') or '?')[:80]}\n")

        conn = papers_db.get_connection()
        try:
            for p in contaminated:
                conn.execute("DELETE FROM papers WHERE id = ?", (p.get("id"),))
            conn.commit()
        finally:
            conn.close()

        remaining = papers_db.get_paper_count()
        print(f"\nRemoved {len(contaminated)} entries. {remaining} papers remain.")
        print(f"   Logged to {ERROR_LOG}")
    else:
        print(f"\nRun with --fix to remove them")


if __name__ == "__main__":
    main()
