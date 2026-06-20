"""Phase 2q: restore pdf-missing rows from unowned source-pdfs + p4p.

Pre-condition: Phase 2o has run (source-pdfs is hash-deduped).

For each row tagged 'pdf-missing':
  Step A: detect cross-row dupes. If another non-pdf-missing row has matching
          title + authors + year + DOI, MERGE this row into that one (transfer
          tags) and delete this row.
  Step B: if no cross-row match, search unowned source-pdfs + p4p-new for the
          PDF using tiers 1, 2, 5, 3, 6, 4 (same as recover_v2).
  Step C: if found in unowned source-pdfs: link and clear pdf-missing.
  Step D: if found in p4p: copy to source-pdfs, link, clear pdf-missing.
  Step E: still no match -> leave pdf-missing tag.

Builds new p4p text cache once. Reuses source-pdfs cache (rebuilds if missing).

Usage:
  python pipeline/phase_2q_restore_missing.py             # dry-run
  python pipeline/phase_2q_restore_missing.py --execute   # apply
"""
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
import config  # noqa: E402
import papers_db  # noqa: E402
from recover_v2 import (  # noqa: E402
    build_text_cache, match_against_index, get_full_text, norm,
    SOURCE_CACHE, P4P_CACHE, P4P, id_to_filename_guesses,
)


def has_missing_tag(paper):
    tags = paper.get("tags") or []
    if isinstance(tags, list):
        return "pdf-missing" in tags
    if isinstance(tags, str):
        return "pdf-missing" in tags
    return False


def cross_row_dupe_match(missing_paper, all_meta):
    """Find a non-missing row that's the same paper. Match by title+authors+year+DOI."""
    bm_m = missing_paper.get("basic_metadata") or {}
    m_title = norm(bm_m.get("title") or "")
    m_year = str(bm_m.get("year") or "")
    m_doi = ((missing_paper.get("identifiers") or {}).get("doi") or "").lower()
    m_authors = bm_m.get("authors") or []
    if isinstance(m_authors, str):
        m_authors = [a.strip() for a in re.split(r"[;,]", m_authors)]
    m_surnames = set()
    for a in m_authors[:3]:
        if isinstance(a, str) and a.split():
            m_surnames.add(norm(a.split()[-1]))
    m_surnames = {s for s in m_surnames if len(s) >= 4}

    for p in all_meta:
        if p is missing_paper:
            continue
        if has_missing_tag(p):
            continue  # don't match against other pdf-missing rows
        bm = p.get("basic_metadata") or {}
        p_title = norm(bm.get("title") or "")
        p_year = str(bm.get("year") or "")
        p_doi = ((p.get("identifiers") or {}).get("doi") or "").lower()

        # DOI exact match
        if m_doi and p_doi and m_doi == p_doi:
            return p
        # Title+year+authors match
        if m_title and p_title and m_year and p_year:
            if m_year == p_year:
                tr = SequenceMatcher(None, m_title, p_title).ratio()
                if tr >= 0.92:
                    return p
                if m_surnames and tr >= 0.7:
                    p_authors = bm.get("authors") or []
                    if isinstance(p_authors, str):
                        p_authors = [a.strip() for a in re.split(r"[;,]", p_authors)]
                    p_surnames = set()
                    for a in p_authors[:3]:
                        if isinstance(a, str) and a.split():
                            p_surnames.add(norm(a.split()[-1]))
                    p_surnames = {s for s in p_surnames if len(s) >= 4}
                    if m_surnames & p_surnames:
                        return p
    return None


def merge_tags(t1, t2, remove=None):
    def to_list(t):
        if not t:
            return []
        if isinstance(t, list):
            return t
        if isinstance(t, str):
            try:
                v = json.loads(t)
                return v if isinstance(v, list) else [t]
            except Exception:
                return [t]
        return []
    out = []
    seen = set()
    remove = set(remove or [])
    for x in to_list(t1) + to_list(t2):
        if isinstance(x, str) and x not in seen and x not in remove:
            seen.add(x)
            out.append(x)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print(f"Phase 2q: restore pdf-missing rows (mode: {'EXECUTE' if args.execute else 'DRY RUN'})")
    print("=" * 70)

    all_meta = papers_db.get_all_metadata()
    missing_rows = [p for p in all_meta if has_missing_tag(p)]
    print(f"pdf-missing rows: {len(missing_rows)}")

    # Step A: cross-row dupe detection
    print("Step A: cross-row dupe detection...")
    by_id = {p.get("id"): p for p in all_meta}
    cross_merges = []  # (missing_paper, canonical_paper)
    handled = set()
    for m in missing_rows:
        match = cross_row_dupe_match(m, all_meta)
        if match:
            cross_merges.append((m, match))
            handled.add(m.get("id"))
    print(f"  cross-row dupes (will merge): {len(cross_merges)}")
    for m, c in cross_merges[:10]:
        print(f"    DROP {m.get('id')} -> KEEP {c.get('id')}")

    # Build text caches
    print("Loading source-pdfs cache...")
    source_index = build_text_cache(config.SOURCE_PDFS, SOURCE_CACHE)
    source_filenames_lower = {fn.lower(): fn for fn in source_index.keys()}
    print(f"  source-pdfs: {len(source_index)}")
    print("Loading p4p cache...")
    p4p_index = build_text_cache(P4P, P4P_CACHE)
    p4p_filenames_lower = {fn.lower(): fn for fn in p4p_index.keys()}
    print(f"  p4p: {len(p4p_index)}")

    # Pool of unowned source-pdfs filenames (no DB row references them)
    fname_refs = defaultdict(int)
    for p in all_meta:
        fi = p.get("file_info") or {}
        if isinstance(fi, dict):
            fn = fi.get("renamed_filename")
            if fn:
                fname_refs[fn] += 1
        supp = p.get("supplements") or {}
        items = supp.get("items") if isinstance(supp, dict) else []
        for it in (items or []):
            if isinstance(it, dict):
                fn = it.get("filename")
                if fn:
                    fname_refs[fn] += 1
    unowned_source_index = {fn: e for fn, e in source_index.items() if fname_refs.get(fn, 0) == 0}
    unowned_source_filenames_lower = {fn.lower(): fn for fn in unowned_source_index.keys()}
    print(f"  unowned source-pdfs (after refs): {len(unowned_source_index)}")

    # Steps B-D: match against unowned source-pdfs + p4p
    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    source_matches = []  # (pid, real_fn, tier)
    p4p_matches = []     # (pid, p4p_fn, tier)
    still_missing = []
    for m in missing_rows:
        if m.get("id") in handled:
            continue
        pid = m.get("id")
        fulltext = get_full_text(pid, conn)
        # Try unowned source-pdfs
        match = match_against_index(m, fulltext, unowned_source_index, None,
                                    unowned_source_filenames_lower)
        if match[0]:
            source_matches.append((pid, match[0], match[1]))
            continue
        # Try p4p
        match = match_against_index(m, fulltext, p4p_index, None, p4p_filenames_lower)
        if match[0]:
            p4p_matches.append((pid, match[0], match[1]))
            continue
        still_missing.append(pid)

    print()
    print(f"Restored from unowned source-pdfs: {len(source_matches)}")
    print(f"Restored from p4p (will copy):     {len(p4p_matches)}")
    print(f"Cross-row merged:                  {len(cross_merges)}")
    print(f"Still pdf-missing:                 {len(still_missing)}")
    print()

    if source_matches:
        print("Sample source restorations:")
        for pid, fn, t in source_matches[:10]:
            print(f"  {pid} -> {fn}  [{t}]")
        print()
    if p4p_matches:
        print("Sample p4p restorations:")
        for pid, fn, t in p4p_matches[:10]:
            print(f"  {pid} -> {fn}  [{t}]")
        print()

    if not args.execute:
        conn.close()
        print("DRY RUN. Pass --execute to apply.")
        return

    from papers_db import _serialize_field, ALL_COLUMNS, JSON_FIELDS

    try:
        # Apply cross-row merges (transfer tags + supplements + DOI from missing to canonical, delete missing)
        for missing, canonical in cross_merges:
            tags = merge_tags(canonical.get("tags"), missing.get("tags"),
                              remove=["pdf-missing"])
            # Merge supplements: canonical first, append missing's not-already-there
            c_supp = canonical.get("supplements") or {}
            m_supp = missing.get("supplements") or {}
            c_items = (c_supp.get("items") if isinstance(c_supp, dict) else []) or []
            m_items = (m_supp.get("items") if isinstance(m_supp, dict) else []) or []
            seen_fns = {it.get("filename") for it in c_items if isinstance(it, dict)}
            merged_items = list(c_items)
            for it in m_items:
                if isinstance(it, dict) and it.get("filename") not in seen_fns:
                    merged_items.append(it)
                    seen_fns.add(it.get("filename"))
            merged_supp = dict(c_supp) if isinstance(c_supp, dict) else {}
            merged_supp["items"] = merged_items
            merged_supp["files"] = [it["filename"] for it in merged_items
                                    if isinstance(it, dict) and it.get("filename")]
            if merged_items:
                merged_supp["has_supplements"] = True
            # DOI: take from missing if canonical doesn't have one
            c_ids = canonical.get("identifiers") or {}
            m_ids = missing.get("identifiers") or {}
            if isinstance(c_ids, dict) and isinstance(m_ids, dict):
                if not c_ids.get("doi") and m_ids.get("doi"):
                    c_ids = dict(c_ids)
                    c_ids["doi"] = m_ids["doi"]
            conn.execute(
                "UPDATE papers SET tags = ?, supplements = ?, identifiers = ? WHERE id = ?",
                (_serialize_field(tags), _serialize_field(merged_supp),
                 _serialize_field(c_ids), canonical.get("id")))
            conn.execute("DELETE FROM papers WHERE id = ?", (missing.get("id"),))

        # Source-pdf restorations: update file_info, clear pdf-missing tag
        for pid, fn, tier in source_matches:
            paper = by_id[pid]
            fi = paper.get("file_info") or {}
            if not isinstance(fi, dict):
                fi = {}
            fi["renamed_filename"] = fn
            fi.pop("pdf_missing", None)
            fi.pop("was_pointing_at", None)
            fi.pop("nulled_at", None)
            fi["restored_via"] = tier
            fi["restored_at"] = datetime.now().isoformat()
            tags = merge_tags(paper.get("tags"), [], remove=["pdf-missing"])
            conn.execute("UPDATE papers SET file_info = ?, tags = ? WHERE id = ?",
                         (_serialize_field(fi), _serialize_field(tags), pid))

        # P4P restorations: copy file, update file_info, clear tag
        for pid, p4p_fn, tier in p4p_matches:
            src = P4P / p4p_fn
            dst = config.SOURCE_PDFS / p4p_fn
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
            paper = by_id[pid]
            fi = paper.get("file_info") or {}
            if not isinstance(fi, dict):
                fi = {}
            fi["renamed_filename"] = p4p_fn
            fi.pop("pdf_missing", None)
            fi.pop("was_pointing_at", None)
            fi.pop("nulled_at", None)
            fi["restored_via"] = tier + " (from p4p)"
            fi["restored_at"] = datetime.now().isoformat()
            tags = merge_tags(paper.get("tags"), [], remove=["pdf-missing"])
            conn.execute("UPDATE papers SET file_info = ?, tags = ? WHERE id = ?",
                         (_serialize_field(fi), _serialize_field(tags), pid))

        conn.commit()
        print(f"Applied: {len(cross_merges)} merges, {len(source_matches)} source restorations, {len(p4p_matches)} p4p restorations")
    finally:
        conn.close()

    # Invalidate caches (filenames changed)
    if SOURCE_CACHE.exists():
        SOURCE_CACHE.unlink()
        print(f"Invalidated stale cache: {SOURCE_CACHE.name}")

    print("Phase 2q done.")


if __name__ == "__main__":
    main()
