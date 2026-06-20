"""Diagnose the 23 multi-owned PDFs.

For each placeholder filename claimed by 2+ DB rows:
  - List affected DB row ids
  - Show basic_metadata completeness (title/year/doi/authors)
  - Show full_text length (if populated, we can re-extract metadata)
  - Guess the actual PDF in source-pdfs based on the DB row's id

Heuristic for guessing actual PDF:
  Many ids like "1_s2_0_S1388245710003585_main" map directly to a filename
  "1-s2.0-S1388245710003585-main.pdf" with underscore<->hyphen swaps.
  Similarly "BF03326573" maps to "bf03326573.pdf" or similar Springer file.
"""
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
import papers_db  # noqa: E402
import config  # noqa: E402


def id_to_likely_filename(pid):
    """Try various transforms of the paper id to guess its original filename."""
    candidates = set()
    candidates.add(pid + ".pdf")
    # Underscore -> hyphen swap (common for Elsevier ids)
    candidates.add(pid.replace("_", "-") + ".pdf")
    # Lowercase
    candidates.add(pid.lower() + ".pdf")
    candidates.add(pid.lower().replace("_", "-") + ".pdf")
    # Springer BF ids: BF02974263 -> BF02974263.pdf
    if re.match(r"^[A-Z]{2,4}\d+", pid):
        candidates.add(pid + ".pdf")
    return candidates


def main():
    print("Building DB owner map...")
    all_meta = papers_db.get_all_metadata()
    fname_owners = defaultdict(list)
    by_id = {}
    for p in all_meta:
        pid = p.get("id")
        if pid:
            by_id[pid] = p
        fi = p.get("file_info") or {}
        if isinstance(fi, dict):
            fn = fi.get("renamed_filename")
            if fn:
                fname_owners[fn].append(pid)

    multi = {fn: ids for fn, ids in fname_owners.items() if len(ids) >= 2}
    print(f"Found {len(multi)} multi-owned filenames\n")

    # Fetch full_text lengths separately (heavy column, fetch lazily)
    conn = sqlite3.connect(str(config.DB_PATH))
    # Pull all source-pdfs filenames once
    source_files = {p.name.lower() for p in (config.SOURCE_PDFS).glob("*.pdf")}

    for fn, ids in sorted(multi.items(), key=lambda x: -len(x[1])):
        print("=" * 70)
        print(f"PLACEHOLDER: {fn}  ({len(ids)} DB rows claim it)")
        print("=" * 70)
        for pid in ids:
            paper = by_id.get(pid, {})
            bm = paper.get("basic_metadata") or {}
            title = (bm.get("title") or "").strip()
            year = bm.get("year")
            doi = (paper.get("identifiers") or {}).get("doi") if isinstance(paper.get("identifiers"), dict) else None
            authors = bm.get("authors") or []

            # full_text length
            row = conn.execute("SELECT full_text FROM papers WHERE id = ?", (pid,)).fetchone()
            ftxt = row[0] if row else None
            ft_len = len(ftxt) if ftxt else 0

            # Guess original filename
            guesses = id_to_likely_filename(pid)
            found = [g for g in guesses if g.lower() in source_files]

            print(f"  ROW: {pid}")
            print(f"    title:    {title[:80] or '(empty)'}")
            print(f"    year:     {year}    doi: {doi}")
            print(f"    authors:  {authors[:3] if authors else '(empty)'}")
            print(f"    full_text len: {ft_len}")
            print(f"    likely filenames in source-pdfs: {found if found else '(none found via id transform)'}")
        print()

    conn.close()


if __name__ == "__main__":
    main()
