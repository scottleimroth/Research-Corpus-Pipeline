"""Tier E supplement matcher - extend backfill with smarter id matching + PDF content reading.

Targets the 12 unmatched orphans left after Phase 2j:
  MejiaMejia_Supplement, Lukowska2024_Supplement,
  Bree_Sustained_neural_rhythms_reveal_endogenous_supplement_1/2,
  Psychophysiological_Measurement_and_Meaning_Cog_supplement_1,
  Sugawara_Climate_Change_2013___The_Physical_Sci_supplement_1,
  _liwi_ska_Kowalska_WHO_Environmental_Noise_Guid_supplement_1,
  ...plus truncated dupes.

Strategies:
  E1: orphan-id-prefix-of-parent-id (handles truncation: Teixeira_Borges_Scaling -> Teixeira_Borges_et_al___2019___Scaling...)
  E2: surname-prefix variants (Bree -> vanBree, Kowalska -> SliwinskaKowalska)
  E3: PDF first-page text -> title+authors -> fuzzy match against DB

Also: --delete-no-file-info purges the 43 rows that have no file_info (useless orphans).

Usage:
  python pipeline/tier_e_matcher.py                       # dry-run
  python pipeline/tier_e_matcher.py --execute             # apply E1+E2+E3 matches
  python pipeline/tier_e_matcher.py --delete-no-file-info # also delete no-file-info orphans
"""
import argparse
import re
import sqlite3
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import papers_db  # noqa: E402
from backfill_supplements import (  # noqa: E402
    SUFFIX_PATTERNS, strip_supplement_suffix, find_orphan_rows,
    build_supplement_item, merge_into_parent, _is_orphan_id,
)

NAME_PREFIXES = ["van", "von", "de", "del", "della", "der", "den", "di", "le", "la"]


def _norm(s):
    """Lowercase, strip diacritics, drop non-alnum."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def tier_e1_id_prefix(orphan_id, all_meta_index):
    """Orphan stripped id is a prefix of (or fuzzy-matches the prefix of) a real DB id."""
    stripped = orphan_id
    for pat in SUFFIX_PATTERNS:
        stripped = re.sub(pat, "", stripped)
    stripped_norm = _norm(stripped)
    if len(stripped_norm) < 8:
        return None
    candidates = []
    for p in all_meta_index:
        pid = p.get("id") or ""
        if _is_orphan_id(pid):
            continue
        pid_norm = _norm(pid)
        if pid_norm.startswith(stripped_norm) or stripped_norm.startswith(pid_norm[:len(stripped_norm)]):
            ratio = SequenceMatcher(None, stripped_norm, pid_norm[:len(stripped_norm)]).ratio()
            if ratio >= 0.85:
                candidates.append((ratio, p))
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1]


def tier_e2_name_prefix(orphan_id, all_meta_index):
    """Try van/de/le prefix variants on first surname token."""
    stripped = orphan_id
    for pat in SUFFIX_PATTERNS:
        stripped = re.sub(pat, "", stripped)
    # Pull first token (may be CamelCase or underscore-separated)
    first_tok = re.split(r"[_\-\s]+", stripped, 1)[0]
    if len(first_tok) < 4:
        return None
    first_tok_norm = _norm(first_tok)

    year_m = re.search(r"(19|20)\d{2}", stripped)
    year = year_m.group(0) if year_m else None

    for p in all_meta_index:
        pid = p.get("id") or ""
        if _is_orphan_id(pid):
            continue
        bm = p.get("basic_metadata") or {}
        authors = bm.get("authors") or []
        if not authors:
            continue
        # Get all surname tokens of first author
        first_author = authors[0] if isinstance(authors[0], str) else ""
        if not first_author:
            continue
        surname = first_author.split()[-1] if first_author.split() else ""
        surname_norm = _norm(surname)
        # Strip known prefixes from DB surname
        bare = surname_norm
        for px in NAME_PREFIXES:
            if bare.startswith(px):
                bare = bare[len(px):]
        # Also try stripping from orphan token
        orphan_bare = first_tok_norm
        for px in NAME_PREFIXES:
            if orphan_bare.startswith(px):
                orphan_bare = orphan_bare[len(px):]

        match = (
            surname_norm == first_tok_norm
            or bare == first_tok_norm
            or surname_norm == orphan_bare
            or bare == orphan_bare
        )
        if not match:
            continue

        # Year sanity
        if year and str(bm.get("year", "")).strip() != year:
            continue
        return p
    return None


def tier_e3_pdf_content(orphan_paper, all_meta_index):
    """Read first page of the supplement PDF and fuzzy-match title/authors against DB."""
    fi = orphan_paper.get("file_info") or {}
    renamed = fi.get("renamed_filename") if isinstance(fi, dict) else None
    if not renamed:
        return None
    pdf_path = config.SOURCE_PDFS / renamed
    if not pdf_path.exists():
        return None

    from pdf_content_guard import extract_pypdf_text_safely

    page1, err = extract_pypdf_text_safely(pdf_path, pages=1, tier="tier_e_pdf_content")
    if err:
        return None

    text_norm = _norm(page1)[:2000]
    if len(text_norm) < 50:
        return None

    best_ratio = 0.0
    best_p = None
    for p in all_meta_index:
        pid = p.get("id") or ""
        if _is_orphan_id(pid):
            continue
        bm = p.get("basic_metadata") or {}
        title = bm.get("title") or ""
        if len(title) < 15:
            continue
        title_norm = _norm(title)
        # Look for title as substring of first-page text
        if title_norm in text_norm:
            return p
        # Else partial fuzzy
        ratio = SequenceMatcher(None, title_norm[:200], text_norm[:200]).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_p = p
    if best_ratio >= 0.55 and best_p:
        return best_p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--delete-no-file-info", action="store_true",
                    help="also delete the 43 orphans that have no file_info")
    args = ap.parse_args()

    print("=" * 60)
    print("Tier E matcher")
    print("=" * 60)
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print()

    orphans = find_orphan_rows()
    print(f"Remaining orphans: {len(orphans)}")
    all_meta = papers_db.get_all_metadata()

    parent_updates = {}
    orphan_deletes = []
    no_file_info_orphans = []
    e1_hits = e2_hits = e3_hits = 0
    still_unmatched = []

    for orphan_id, orphan in orphans:
        fi = orphan.get("file_info") or {}
        if not (isinstance(fi, dict) and fi.get("renamed_filename")):
            no_file_info_orphans.append(orphan_id)
            continue

        match = tier_e1_id_prefix(orphan_id, all_meta)
        tier = None
        if match:
            tier = "E1:id-prefix"
            e1_hits += 1
        else:
            match = tier_e2_name_prefix(orphan_id, all_meta)
            if match:
                tier = "E2:name-prefix"
                e2_hits += 1
            else:
                match = tier_e3_pdf_content(orphan, all_meta)
                if match:
                    tier = "E3:pdf-content"
                    e3_hits += 1

        if not match:
            still_unmatched.append(orphan_id)
            print(f"  UNMATCHED: {orphan_id}")
            continue

        item = build_supplement_item(orphan)
        pid = match.get("id")
        parent = parent_updates.get(pid, match)
        added = merge_into_parent(parent, item)
        parent_updates[pid] = parent
        orphan_deletes.append(orphan_id)
        print(f"  {orphan_id} -> {pid} ({'linked' if added else 'already linked'}) [tier {tier}]")

    print()
    print("Tier breakdown:")
    print(f"  E1 id-prefix:   {e1_hits}")
    print(f"  E2 name-prefix: {e2_hits}")
    print(f"  E3 pdf-content: {e3_hits}")
    print()
    print(f"Orphans with no file_info: {len(no_file_info_orphans)}")
    print(f"Still unmatched (manual review): {len(still_unmatched)}")

    if args.execute and (parent_updates or orphan_deletes or
                         (args.delete_no_file_info and no_file_info_orphans)):
        from papers_db import ALL_COLUMNS, JSON_FIELDS, _serialize_field
        print()
        deletes = list(orphan_deletes)
        if args.delete_no_file_info:
            deletes.extend(no_file_info_orphans)
            print(f"INCLUDING --delete-no-file-info: +{len(no_file_info_orphans)} deletes")
        print(f"Applying {len(parent_updates)} parent updates + {len(deletes)} deletes...")

        conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            placeholders = ",".join(["?"] * len(ALL_COLUMNS))
            cols = ",".join(ALL_COLUMNS)
            sql = f"INSERT OR REPLACE INTO papers ({cols}) VALUES ({placeholders})"
            for parent in parent_updates.values():
                cl = parent.get("classification", {}) or {}
                if isinstance(cl, dict):
                    rating = cl.get("rating")
                    rts = cl.get("red_team_survival")
                    if rts is not None:
                        rts = str(rts)
                else:
                    rating, rts = None, None
                vals = []
                for col in ALL_COLUMNS:
                    if col == "rating":
                        vals.append(rating)
                    elif col == "red_team_survival":
                        vals.append(rts)
                    elif col in JSON_FIELDS:
                        vals.append(_serialize_field(parent.get(col)))
                    else:
                        vals.append(parent.get(col))
                conn.execute(sql, tuple(vals))
            for oid in deletes:
                conn.execute("DELETE FROM papers WHERE id = ?", (oid,))
            conn.commit()
            print("Transaction committed.")
        finally:
            conn.close()

    print()
    print("=" * 60)
    print("Done.")


if __name__ == "__main__":
    main()
