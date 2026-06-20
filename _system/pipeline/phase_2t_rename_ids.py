"""Phase 2t: rename rows with weird/legacy ids to clean Author_Year_Keyword format.

Patterns of weird ids:
  - 1_s2_0_X_main      (Elsevier PII derived)
  - BF03326573         (Springer BF DOI suffix)
  - 26344727_X_s       (UOW Alma library IDs)
  - *UOW0034*          (UOW-prefixed library IDs)
  - orphan_X           (orphan placeholder ids)
  - 1471_2377_X        (BMC and similar journal+volume+article IDs)
  - 1866_1955_X        (BMC variants)
  - 1753_4631_X        (BMC variants)
  - s11062_X           (Springer s-prefix)
  - 1992_X, 2005_X, 2007_X  (year_id patterns)

For each weird-id row:
  - Generate new id using add_paper.generate_id() from basic_metadata
  - Resolve collisions with existing ids by appending _b, _c, etc.
  - UPDATE the primary key

Preserves all other data. Logs old->new mapping.

Usage:
  python pipeline/phase_2t_rename_ids.py             # dry-run
  python pipeline/phase_2t_rename_ids.py --execute   # apply
"""
import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
import config  # noqa: E402
import papers_db  # noqa: E402
from add_paper import generate_id, _extract_author_surname, is_unsafe_generated_id  # noqa: E402


# Legacy placeholder ids: do not auto-rename without trustworthy author metadata.
UNKNOWN_SUFFIX_ID_RX = re.compile(r"[A-Za-z]+Unknown$|_Year_Unknown$")
# Reliable target: Surname_YYYY_Keyword (not orphan_* filename stubs).
AUTHOR_YEAR_KEYWORD_RX = re.compile(r"^[A-Z][a-zA-Z]{2,30}_\d{4}_[A-Za-z][a-zA-Z]{2,50}$")
MALFORMED_LEADING_ID_RX = re.compile(r"^_[A-Za-z]")
# Truncated / garbage tails from filename or title fragments (unknow, situatio, etc.).
JUNK_KEYWORD_TAIL_RX = re.compile(
    r"(unknow|situatio|situation|international|handbook|auth|main)\d*$", re.IGNORECASE
)


WEIRD_ID_PATTERNS = [
    re.compile(r"^1_s2_0_"),
    re.compile(r"^BF\d{6,}$"),
    re.compile(r"^\d{8,}_\d{12,}"),
    re.compile(r"UOW\d+"),
    re.compile(r"^orphan_"),
    re.compile(r"^s\d{5}_\d+"),
    re.compile(r"^\d{4}_\d{4}_"),
    re.compile(r"^\d{4}_\d{5}_\d{3}$"),  # NNNN_NNNNN_NNN
    re.compile(r"^\d{4}_\d{4,}_\w+_S\d+$"),  # 1753_4631_4_S1_S1
    re.compile(r"^_pdf$"),  # literal "_pdf" id
    re.compile(r"^_[A-Z]"),  # ids starting with underscore + capital
    re.compile(r"[A-Za-z]+Unknown$"),  # *Unknown ids (AuthorNameUnknown pattern, year missing)
    re.compile(r"_Year_Unknown$"),  # Author_Mulder_Year_Unknown variant
]


def is_weird_id(pid):
    if not pid:
        return False
    for rx in WEIRD_ID_PATTERNS:
        if rx.search(pid):
            return True
    return False


def weird_id_category(pid):
    if not pid:
        return "missing_id"
    if pid.startswith("orphan_"):
        return "orphan_placeholder"
    if UNKNOWN_SUFFIX_ID_RX.search(pid):
        return "unknown_suffix"
    if MALFORMED_LEADING_ID_RX.match(pid):
        return "malformed_leading_underscore"
    if re.search(r"^1_s2_0_|^BF\d{6,}$|^s\d{5}_\d+|^\d{4}_\d{4,}_|UOW\d+", pid):
        return "legacy_source_identifier"
    return "other_weird"


def is_reliable_author_year_keyword_id(paper_id):
    """True only for clean Author_Year_Keyword targets (not orphan_* stubs)."""
    if not paper_id or paper_id.startswith("orphan_"):
        return False
    if not AUTHOR_YEAR_KEYWORD_RX.match(paper_id):
        return False
    parts = paper_id.split("_")
    if len(parts) < 3:
        return False
    tail = parts[-1]
    if len(tail) < 4 or JUNK_KEYWORD_TAIL_RX.search(tail):
        return False
    return True


def rename_skip_reason(old_id, new_id, paper, authors):
    """Return skip reason string, or None if rename is safe to apply."""
    if MALFORMED_LEADING_ID_RX.match(old_id):
        return "malformed_id_needs_manual"
    if old_id.startswith("orphan_") and not is_reliable_author_year_keyword_id(new_id):
        return "orphan_id_needs_manual"
    if new_id.startswith("orphan_"):
        return "orphan_output_needs_manual"
    if is_unsafe_generated_id(new_id):
        return "unsafe_generated_id"
    if not is_reliable_author_year_keyword_id(new_id):
        return "unreliable_generated_id"
    bm = paper.get("basic_metadata") or {}
    year = str(bm.get("year") or "").strip()
    title = (bm.get("title") or "").strip()
    if not _extract_author_surname(authors) or not year.isdigit() or len(title) < 8:
        return "insufficient_metadata_for_rename"
    return None


def generate_new_id(paper, taken):
    """Generate a clean Author_Year_Keyword id, resolving collisions."""
    bm = paper.get("basic_metadata") or {}
    authors = bm.get("authors") or []
    if isinstance(authors, str):
        authors = [a.strip() for a in re.split(r"[;,]", authors)]
    year = bm.get("year")
    title = bm.get("title") or ""
    fi = paper.get("file_info") or {}
    pdf_filename = fi.get("renamed_filename") if isinstance(fi, dict) else None

    base = generate_id(authors, year, title, pdf_filename=pdf_filename)
    if base in taken:
        for suffix in "bcdefghijklmnop":
            candidate = f"{base}_{suffix}"
            if candidate not in taken:
                return candidate
        # Last resort
        i = 2
        while f"{base}_{i}" in taken:
            i += 1
        return f"{base}_{i}"
    return base


def require_backup_for_execute():
    backups = sorted(config.BACKUP_DIR.glob("papers-*.db"))
    if backups:
        return True
    print(
        "ABORT: No database backup found in "
        f"{config.BACKUP_DIR}. Create a papers-*.db backup before --execute."
    )
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print(f"Phase 2t: rename weird ids (mode: {'EXECUTE' if args.execute else 'DRY RUN'})")
    print("=" * 70)

    all_meta = papers_db.get_all_metadata()
    taken_ids = set(p.get("id") for p in all_meta if p.get("id"))
    weird_rows = [p for p in all_meta if is_weird_id(p.get("id"))]
    category_counts = Counter(weird_id_category(p.get("id")) for p in weird_rows)
    print(f"Total rows: {len(all_meta)}")
    print(f"Weird-id rows: {len(weird_rows)}")
    print("Weird-id categories:")
    for category, count in sorted(category_counts.items()):
        print(f"  {category}: {count}")
    print()

    plan = []  # (old_id, new_id, paper)
    skipped = []  # (old_id, reason)
    for p in weird_rows:
        old_id = p.get("id")
        bm = p.get("basic_metadata") or {}
        if not bm.get("title") and not bm.get("authors"):
            skipped.append((old_id, "no basic_metadata"))
            continue
        authors = bm.get("authors") or []
        if isinstance(authors, str):
            authors = [a.strip() for a in re.split(r"[;,]", authors)]
        if UNKNOWN_SUFFIX_ID_RX.search(old_id):
            skipped.append((old_id, "unknown_suffix_id"))
            continue
        if old_id.startswith("orphan_") and not _extract_author_surname(authors):
            skipped.append((old_id, "orphan_id_needs_manual"))
            continue
        if not _extract_author_surname(authors):
            skipped.append((old_id, "synthetic_author_metadata"))
            continue
        new_id = generate_new_id(p, taken_ids)
        skip_reason = rename_skip_reason(old_id, new_id, p, authors)
        if skip_reason:
            skipped.append((old_id, skip_reason))
            continue
        if new_id == old_id:
            skipped.append((old_id, "generated id same as old"))
            continue
        taken_ids.add(new_id)
        plan.append((old_id, new_id, p))

    print(f"Will rename: {len(plan)}")
    print(f"Skipped:     {len(skipped)}")
    skip_counts = Counter(reason for _, reason in skipped)
    if skip_counts:
        print("Skip reasons:")
        for reason, count in sorted(skip_counts.items()):
            print(f"  {reason}: {count}")
    print()

    # Show sample renames
    print("Sample renames (first 30):")
    for old, new, p in plan[:30]:
        title = ((p.get("basic_metadata") or {}).get("title") or "")[:50]
        print(f"  {old}")
        print(f"    -> {new}  ({title})")
    if len(plan) > 30:
        print(f"  ...and {len(plan) - 30} more")
    print()

    if skipped:
        print(f"Skipped rows (no metadata or collision):")
        for old, reason in skipped[:15]:
            print(f"  {old}: {reason}")
        if len(skipped) > 15:
            print(f"  ...and {len(skipped) - 15} more")
        print()

    if not args.execute:
        print("Guardrails active:")
        print("  - --execute is required before any ID rename")
        print("  - generated IDs must be reliable Author_Year_Keyword form")
        print("  - orphan/unknown/malformed IDs are skipped unless metadata is strong")
        print("  - a DB backup is required before execute mode")
        print("DRY RUN. Pass --execute to apply.")
        return
    if not require_backup_for_execute():
        return

    # Execute - SQLite doesn't easily rename primary keys, so we INSERT new + DELETE old
    # within a single transaction.
    from papers_db import ALL_COLUMNS, JSON_FIELDS, _serialize_field

    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        log = []
        for old_id, new_id, paper in plan:
            cl = paper.get("classification", {}) or {}
            if isinstance(cl, dict):
                rating = cl.get("rating")
                rts = cl.get("red_team_survival")
                if rts is not None:
                    rts = str(rts)
            else:
                rating, rts = None, None
            # Record previous id in notes_and_flags for traceability
            notes = paper.get("notes_and_flags") or {}
            if isinstance(notes, dict):
                notes = dict(notes)
            else:
                notes = {}
            previous = notes.get("previous_ids") or []
            if not isinstance(previous, list):
                previous = []
            previous.append({"id": old_id, "renamed_at": datetime.now().isoformat()})
            notes["previous_ids"] = previous
            paper["notes_and_flags"] = notes

            vals = []
            for col in ALL_COLUMNS:
                if col == "id":
                    vals.append(new_id)
                elif col == "rating":
                    vals.append(rating)
                elif col == "red_team_survival":
                    vals.append(rts)
                elif col in JSON_FIELDS:
                    vals.append(_serialize_field(paper.get(col)))
                else:
                    vals.append(paper.get(col))
            placeholders = ",".join(["?"] * len(ALL_COLUMNS))
            conn.execute(
                f"INSERT INTO papers ({','.join(ALL_COLUMNS)}) VALUES ({placeholders})",
                tuple(vals))
            conn.execute("DELETE FROM papers WHERE id = ?", (old_id,))
            log.append((old_id, new_id))
        conn.commit()
        print(f"Renamed {len(log)} rows.")
    finally:
        conn.close()

    # Write log
    log_path = ROOT / f"id-rename-log-{datetime.now():%Y-%m-%d-%H%M%S}.json"
    log_path.write_text(json.dumps(
        [{"old": o, "new": n} for o, n in log], indent=2), encoding="utf-8")
    print(f"Mapping log: {log_path.name}")


if __name__ == "__main__":
    main()
