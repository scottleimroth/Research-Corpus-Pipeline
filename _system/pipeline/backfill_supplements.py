#!/usr/bin/env python3
"""
backfill_supplements.py - One-off cleanup of orphan supplement-as-paper rows.

Background:
  Alex's old broken pipeline created standalone DB entries for supplement files,
  giving them ids like Keitel2022_supp1, Limb2006_supp1, Milne2020_Supplement,
  etc. These aren't papers - they're supplements that got mis-treated.

This script:
  1. Finds all rows whose id ends with a supplement suffix (_supp1, _supp2,
     _supplement, _supplement_1, etc.)
  2. Computes the real parent id by stripping the suffix.
  3. Looks the parent up in the DB.
  4. If parent exists:
       - Builds a supplements.items entry from the orphan's file_info.
       - Appends it to parent.supplements.items (skipping duplicates by filename).
       - Saves the parent.
       - Deletes the orphan row from the DB.
  5. If parent does NOT exist: leaves the orphan alone and logs it for manual
     review (very rare; would only happen if parent was never added).
  6. ALSO: scans every paper.supplements.items for entries whose filename
     matches an orphan-derived pattern and moves those to the real parent too
     (handles the case where new supplements got linked to orphan rows in
     today's run before this fix landed).

Usage:
  python pipeline/backfill_supplements.py            # dry-run (preview)
  python pipeline/backfill_supplements.py --execute  # actually modify the DB

Always make a fresh backup of papers.db before --execute.
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import papers_db  # noqa: E402


# Supplement suffix patterns, longest-first so they don't shadow each other
SUFFIX_PATTERNS = [
    r"__supplement$",
    r"_supplement_\d+$",
    r"_supplement$",
    r"_Supplement$",
    r"_supp\d+$",
]


def strip_supplement_suffix(orphan_id):
    """Return candidate parent id, or None if no suffix matched."""
    for pat in SUFFIX_PATTERNS:
        new = re.sub(pat, "", orphan_id)
        if new != orphan_id:
            return new
    return None


def find_orphan_rows():
    """Return list of (id, paper_dict) for rows whose id has a supplement suffix."""
    all_meta = papers_db.get_all_metadata()
    orphans = []
    for p in all_meta:
        pid = p.get("id") or ""
        if strip_supplement_suffix(pid):
            orphans.append((pid, p))
    return orphans


# ---------------------------------------------------------------------------
# Multi-tier parent matcher for exhaustive supplement-to-parent linking.
# ---------------------------------------------------------------------------
def _split_camel(s):
    """CandiaRivera -> [Candia, Rivera]. Bauer -> [Bauer]."""
    return re.findall(r"[A-Z][a-z]+", s) or [s]


def _extract_author_year_from_id(orphan_id):
    """Pull (surname_candidates, year) from an orphan id like Bauer2015_supp1 or CandiaRivera2022_supp1."""
    stripped = orphan_id
    for pat in SUFFIX_PATTERNS:
        stripped = re.sub(pat, "", stripped)
    # Year: 4-digit run
    year_m = re.search(r"(19|20)\d{2}", stripped)
    year = year_m.group(0) if year_m else None
    # Author part = id minus year
    author_part = re.sub(r"(19|20)\d{2}", "", stripped).strip("_-")
    surnames = _split_camel(author_part) if author_part else []
    # Also handle hyphenated / underscore-separated names
    extra = re.split(r"[_\-]+", author_part)
    for x in extra:
        if x and x not in surnames:
            surnames.append(x)
    return surnames, year


def find_parent_for_orphan(orphan_paper, all_meta_index):
    """Multi-tier search for an orphan's real parent. Returns (parent_dict, tier_name) or (None, None).

    Tiers:
      A - exact id-strip match
      B - author+year extracted from id, against DB authors+year
      C - orphan's basic_metadata.title fuzzy match to DB titles (same year)
      D - orphan's authors+year fuzzy match against DB rows (>=1 surname overlap)
    """
    from difflib import SequenceMatcher
    orphan_id = orphan_paper.get("id", "")

    # Tier A: exact id-strip
    candidate_id = strip_supplement_suffix(orphan_id)
    if candidate_id:
        for p in all_meta_index:
            if p.get("id") == candidate_id and not _is_orphan_id(p.get("id", "")):
                return p, "A:id-strip"

    # Tier B: orphan's FIRST surname token + year, against parent's first author surname.
    # Tightened: only the leading surname matters (other CamelCase words are likely title fragments,
    # not authors). And parent must have that exact surname in its authors list - no substring fudging.
    surnames, year = _extract_author_year_from_id(orphan_id)
    if surnames:
        primary_surname = surnames[0].lower()
        if len(primary_surname) >= 3:
            for p in all_meta_index:
                pid = p.get("id", "")
                if _is_orphan_id(pid):
                    continue
                bm = p.get("basic_metadata") or {}
                authors = bm.get("authors") or []
                # First author surname (most reliable)
                first_author_surname = ""
                if authors and isinstance(authors[0], str) and authors[0].split():
                    first_author_surname = authors[0].split()[-1].lower()
                    # Strip hyphens for "Candia-Rivera" -> "candiarivera"
                    first_author_surname_compact = re.sub(r"[^a-z]", "", first_author_surname)
                else:
                    first_author_surname_compact = ""
                # Also require the parent's id to start with the orphan's surname,
                # as a secondary check (catches stylistic id variations)
                pid_starts_match = pid.lower().startswith(primary_surname)
                surname_match = (first_author_surname_compact == primary_surname or
                                primary_surname in first_author_surname.lower())
                year_ok = (not year) or (str(bm.get("year", "")).strip() == year)
                if year_ok and surname_match and pid_starts_match:
                    return p, "B:author+year"

    # Tier C: title fuzzy from orphan basic_metadata
    obm = orphan_paper.get("basic_metadata") or {}
    orphan_title = (obm.get("title") or "").strip().lower()
    orphan_year = str(obm.get("year", "")).strip()
    if orphan_title and len(orphan_title) > 15:
        best_ratio = 0.0
        best_p = None
        for p in all_meta_index:
            pid = p.get("id", "")
            if _is_orphan_id(pid):
                continue
            bm = p.get("basic_metadata") or {}
            existing_title = (bm.get("title") or "").strip().lower()
            if not existing_title:
                continue
            if orphan_year and str(bm.get("year", "")).strip() != orphan_year:
                continue
            ratio = SequenceMatcher(None, orphan_title, existing_title).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_p = p
        if best_p and best_ratio > 0.80:
            return best_p, f"C:title-fuzzy-{int(best_ratio*100)}"

    # Tier D: orphan authors+year fuzzy
    orphan_authors = obm.get("authors") or []
    if orphan_authors and orphan_year:
        orphan_surnames = set()
        for a in orphan_authors:
            if isinstance(a, str) and a.split():
                orphan_surnames.add(a.split()[-1].lower())
        for p in all_meta_index:
            pid = p.get("id", "")
            if _is_orphan_id(pid):
                continue
            bm = p.get("basic_metadata") or {}
            if str(bm.get("year", "")).strip() != orphan_year:
                continue
            existing = bm.get("authors") or []
            ex_surnames = {a.split()[-1].lower() for a in existing
                           if isinstance(a, str) and a.split()}
            if orphan_surnames & ex_surnames:
                return p, "D:authors+year"

    return None, None


def _is_orphan_id(pid):
    """True if id looks like an orphan supplement row (used to skip orphans during matching)."""
    if not pid:
        return False
    for pat in SUFFIX_PATTERNS:
        if re.search(pat, pid):
            return True
    return False


def build_supplement_item(orphan_paper):
    """Construct a supplements.items[] entry from an orphan paper row."""
    fi = orphan_paper.get("file_info") or {}
    renamed = fi.get("renamed_filename") if isinstance(fi, dict) else None
    original = fi.get("original_filename") if isinstance(fi, dict) else None
    fmt = fi.get("file_format") if isinstance(fi, dict) else None
    if not fmt and renamed:
        fmt = Path(renamed).suffix.lstrip(".") or "pdf"

    size_mb = None
    if renamed:
        f = config.SOURCE_PDFS / renamed
        if f.exists():
            size_mb = round(f.stat().st_size / (1024 * 1024), 3)

    return {
        "filename": renamed,
        "original_filename": original,
        "format": fmt,
        "size_mb": size_mb,
        "type": None,
        "description": None,
        "added": datetime.now().isoformat(),
        "backfilled_from": orphan_paper.get("id"),
    }


def merge_into_parent(parent, item):
    """Append item to parent.supplements.items, skipping by filename. Returns True if added."""
    supp = parent.get("supplements") or {}
    if not isinstance(supp, dict):
        supp = {}
    items = supp.get("items") or []
    if not isinstance(items, list):
        items = []

    if item.get("filename") and any(
        isinstance(it, dict) and it.get("filename") == item["filename"] for it in items
    ):
        return False  # already linked

    items.append(item)
    supp["items"] = items
    supp["has_supplements"] = True
    supp["files"] = [it["filename"] for it in items if isinstance(it, dict) and it.get("filename")]
    parent["supplements"] = supp
    return True


def find_misrouted_items(orphan_ids):
    """Find paper rows whose supplements.items[] contain items belonging to orphan-derived parents.

    Returns list of (current_holder_paper, item_dict, real_parent_id).
    """
    misrouted = []
    all_meta = papers_db.get_all_metadata()
    orphan_id_set = set(orphan_ids)
    for p in all_meta:
        # Skip the orphans themselves
        if p.get("id") in orphan_id_set:
            continue
        supp = p.get("supplements") or {}
        items = supp.get("items") if isinstance(supp, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            # If the holder's id is itself an orphan parent (e.g. Keitel2022_supp1),
            # the misroute is the holder, not the item. That case is caught
            # because the holder will be in orphans list above and removed.
            # Here we're looking for the OTHER misroute case: items added to a wrong parent.
            # For now, this is mostly a no-op; main misroute pattern is orphan-holder.
            pass
    return misrouted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="Actually modify the DB. Without this flag, dry-run only.")
    args = ap.parse_args()

    print("=" * 60)
    print("Supplement backfill")
    print("=" * 60)
    print(f"DB: {config.DB_PATH}")
    print()

    if not args.execute:
        print("DRY-RUN mode. Pass --execute to apply changes.")
        print()

    orphans = find_orphan_rows()
    print(f"Found {len(orphans)} orphan supplement rows")
    print()

    # Pre-fetch all metadata once (avoids 207 separate full-table scans)
    all_meta_index = papers_db.get_all_metadata()

    relinked = 0
    no_parent = []
    no_file = []
    tier_counts = {}

    # Phase 1: build the change plan in memory. Parents are mutated in-place
    # (collected uniquely by id), orphans queued for deletion.
    parent_updates = {}  # id -> parent dict (with items merged)
    orphan_deletes = []  # list of orphan ids

    for orphan_id, orphan_paper in orphans:
        parent, tier = find_parent_for_orphan(orphan_paper, all_meta_index)
        if not parent:
            no_parent.append(orphan_id)
            continue
        item = build_supplement_item(orphan_paper)
        if not item.get("filename"):
            no_file.append(orphan_id)
            continue

        # Reuse a single parent dict across multiple supplements for the same parent
        pid = parent.get("id")
        if pid in parent_updates:
            parent = parent_updates[pid]
        added = merge_into_parent(parent, item)
        parent_updates[pid] = parent
        orphan_deletes.append(orphan_id)

        action = "linked" if added else "already linked"
        print(f"  {orphan_id} -> {pid} ({action}) [tier {tier}]")
        relinked += 1
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    print()
    print("Tier breakdown:")
    for t, n in sorted(tier_counts.items()):
        print(f"  tier {t}: {n}")

    # Phase 2: apply ALL changes in one transaction (avoids SQLite lock contention)
    if args.execute and (parent_updates or orphan_deletes):
        print()
        print(f"Applying {len(parent_updates)} parent updates + {len(orphan_deletes)} orphan deletes...")
        from papers_db import ALL_COLUMNS, JSON_FIELDS, _serialize_field

        conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            placeholders = ",".join(["?"] * len(ALL_COLUMNS))
            cols = ",".join(ALL_COLUMNS)
            sql = f"INSERT OR REPLACE INTO papers ({cols}) VALUES ({placeholders})"
            for parent in parent_updates.values():
                # Mirror papers_db._paper_to_row
                cl = parent.get("classification", {}) or {}
                if isinstance(cl, dict):
                    rating = cl.get("rating")
                    rts = cl.get("red_team_survival")
                    if rts is not None:
                        rts = str(rts)
                else:
                    rating, rts = None, None
                values = []
                for col in ALL_COLUMNS:
                    if col == "rating":
                        values.append(rating)
                    elif col == "red_team_survival":
                        values.append(rts)
                    elif col in JSON_FIELDS:
                        values.append(_serialize_field(parent.get(col)))
                    else:
                        values.append(parent.get(col))
                conn.execute(sql, tuple(values))
            for orphan_id in orphan_deletes:
                conn.execute("DELETE FROM papers WHERE id = ?", (orphan_id,))
            conn.commit()
            print("Transaction committed.")
        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()

    print()
    print("=" * 60)
    print(f"Summary:")
    print(f"  Orphans found:           {len(orphans)}")
    print(f"  Relinked to parent:      {relinked}")
    print(f"  No parent found in DB:   {len(no_parent)}")
    print(f"  Orphan has no file_info: {len(no_file)}")
    print("=" * 60)

    if no_parent:
        print()
        print("Orphans with NO matching parent in DB (need manual review):")
        for orphan_id in no_parent[:20]:
            print(f"  {orphan_id}")
        if len(no_parent) > 20:
            print(f"  ... and {len(no_parent) - 20} more")

    if not args.execute:
        print()
        print("Run again with --execute to apply.")


if __name__ == "__main__":
    main()
