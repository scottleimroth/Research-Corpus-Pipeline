"""Phase 2w: detect and fix broken/placeholder/truncated titles in the DB.

These cause downstream dedupe to either match false positives (same junk title in
two unrelated papers) or fail to match real dupes (one row has clean title, the
other has filename-stub title).

Detection rules — a title is BROKEN if any of:
  T1) Empty / whitespace / None
  T2) Equal (case-insensitive) to the row's id
  T3) < 8 characters
  T4) Single-token CamelCase or no-space (e.g. "EmmysMirror", "ScoringCoupling",
       "MLTeamSportsPrediction", "FootballWeightedDynamic", "HydroCelGSN_10_10")
  T5) Looks like a filename / journal ID stub: starts with digits-and-dots,
       ends in ".pdf", matches Elsevier PII pattern (`1 s2.0 ...`), or matches
       `journal.NNNNN.YYYY*` pattern
  T6) Is just a single word equal to the first author surname (e.g. "fairclough")
  T7) Literal template strings: "download", "exact paper title", ".pdf", "i",
       "_pdf", "FootballWeightedDynamic"

Fix strategy (in order):
  F1) Read first 2 pages of the PDF via pypdf; pull lines that look like a title
      (longish, no all-caps shouting, not header/footer). Use first plausible.
  F2) Parse renamed_filename: take the part after the first year and convert
      underscores to spaces, title-case.
  F3) If both fail, set tag `title-needs-llm-extraction` and leave the title
      alone for a later LLM pass.

Dry-run by default. Pass --execute to apply.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
import config  # noqa: E402
import papers_db  # noqa: E402


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

JUNK_TITLES = {
    "download", "exact paper title", ".pdf", "i", "_pdf",
    "fairclough",  # auto-generated stub
}

ELSEVIER_PII_RX = re.compile(r"^\s*1\s+s2\.0\s+", re.IGNORECASE)
JOURNAL_ID_RX = re.compile(r"^\s*[a-z]+\.\d+\.\d+", re.IGNORECASE)
PURE_DIGITS_RX = re.compile(r"^[\d\s\.\-_/()]+$")
FILE_EXT_RX = re.compile(r"\.(pdf|docx?|html?|txt)$", re.IGNORECASE)
CAMEL_SINGLE_RX = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")  # no whitespace, single token
PDF_TITLE_HARD_REJECT_RX = [
    re.compile(r"©|copyright", re.IGNORECASE),
    re.compile(r"^author\(s\):", re.IGNORECASE),
    re.compile(r"\bdoi\b|\bissn\b", re.IGNORECASE),
    re.compile(r"\bvol(?:ume)?\b", re.IGNORECASE),
    re.compile(r"\bann\.\s*rev\.", re.IGNORECASE),
]


def get_title(p):
    bm = p.get("basic_metadata") or {}
    return (bm.get("title") or "").strip()


def get_first_author_surname(p):
    bm = p.get("basic_metadata") or {}
    authors = bm.get("authors") or []
    if isinstance(authors, str):
        authors = [a.strip() for a in re.split(r"[;,]", authors) if a.strip()]
    if not authors:
        return ""
    first = authors[0] if isinstance(authors[0], str) else str(authors[0])
    # take last word as surname (handles "John Smith" and "Smith, J.")
    if "," in first:
        return first.split(",")[0].strip().lower()
    parts = first.strip().split()
    return parts[-1].lower() if parts else ""


def title_looks_broken(title, paper_id, first_author_surname):
    """Return (is_broken, reason) tuple."""
    if not title:
        return True, "T1_empty"
    norm = title.strip().lower()
    if norm == (paper_id or "").lower():
        return True, "T2_equals_id"
    if len(norm) < 8:
        return True, "T3_too_short"
    if norm in JUNK_TITLES:
        return True, "T7_junk_literal"
    if FILE_EXT_RX.search(norm):
        return True, "T5_file_ext"
    if ELSEVIER_PII_RX.match(norm):
        return True, "T5_elsevier_pii"
    if JOURNAL_ID_RX.match(norm):
        return True, "T5_journal_id"
    if PURE_DIGITS_RX.match(norm):
        return True, "T5_pure_digits"
    if first_author_surname and norm == first_author_surname:
        return True, "T6_just_surname"
    # T4: single-token camel-case-ish, no whitespace
    if " " not in title.strip() and "_" not in title.strip():
        if CAMEL_SINGLE_RX.match(title.strip()):
            # Must be at least 12 chars to be plausibly a title-stub
            if len(title.strip()) >= 8:
                return True, "T4_camel_no_space"
    # T4b: single token with underscores, no spaces
    if " " not in title.strip() and "_" in title.strip():
        # e.g. "HydroCelGSN_10_10", "FootballWeightedDynamic"
        if len(title.strip()) < 60:  # real titles with spaces wouldn't be like this
            return True, "T4b_underscored_no_spaces"
    return False, ""


# ---------------------------------------------------------------------------
# Fix strategies
# ---------------------------------------------------------------------------

def try_pdf_title(pdf_path):
    """F1: read first 2 pages, pick plausible title line."""
    if not pdf_path.exists():
        return None
    from pdf_content_guard import extract_pypdf_text_safely

    text, err = extract_pypdf_text_safely(pdf_path, pages=2, tier="phase_2w_title")
    if err:
        return None
    try:
        lines = []
        for raw in text.split("\n"):
            s = raw.strip()
            if not s:
                continue
            lines.append(s)
        # Strictly reject publisher headers/bylines/copyright fragments.
        for s in lines[:20]:
            if 20 <= len(s) <= 200 and " " in s and not s.isupper():
                low = s.lower()
                if any(rx.search(s) for rx in PDF_TITLE_HARD_REJECT_RX):
                    continue
                # Skip lines that look like journal headers ("J. Clin. Neurophysiol")
                if re.match(r"^[A-Z]\.\s+[A-Z][a-z]+\.", s):
                    continue
                # Skip lines like "Downloaded from..."
                if low.startswith(("downloaded ", "received ", "accepted ",
                                   "published ", "doi ", "issn ", "vol.",
                                   "volume ", "page ", "copyright ",
                                   "author(s): ", "authors: ")):
                    continue
                # Skip page numbers / footers
                if re.match(r"^[\d\s\.\-]+$", s):
                    continue
                # Reject lines ending with punctuation fragments typical of headers
                if s.strip().endswith((",", ";", ":")):
                    continue
                return s
    except Exception:
        return None
    return None


YEAR_RX = re.compile(r"(?<!\d)(19[0-9]{2}|20[0-2][0-9])(?!\d)")


def try_filename_title(filename):
    """F2: parse renamed_filename = author_year_keyword.pdf into a title."""
    if not filename:
        return None
    stem = filename.replace(".pdf", "").replace(".PDF", "")
    # Look for a year token
    m = YEAR_RX.search(stem)
    if not m:
        return None
    after_year = stem[m.end():].lstrip("_- ")
    if not after_year:
        return None
    # Trim trailing _supplement_N or similar
    after_year = re.sub(r"_supplement_\d+$", "", after_year)
    after_year = re.sub(r"_part_\d+$", "", after_year)
    # Replace underscores with spaces, title-case
    words = re.split(r"[_\s]+", after_year)
    words = [w for w in words if w]
    if len(words) < 3:
        return None  # too few words to be a title
    return " ".join(w.capitalize() if not w.isupper() else w for w in words)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

TITLE_LLM_TAG = "title-needs-llm-extraction"


def add_title_llm_tag(tags_value):
    """Merge title-needs-llm-extraction without destroying structured tags."""
    if not tags_value:
        return {"custom_tags": [TITLE_LLM_TAG]}
    if isinstance(tags_value, list):
        if TITLE_LLM_TAG in tags_value:
            return tags_value
        return tags_value + [TITLE_LLM_TAG]
    if isinstance(tags_value, dict):
        out = dict(tags_value)
        custom = out.get("custom_tags")
        if not isinstance(custom, list):
            custom = []
        if TITLE_LLM_TAG not in custom:
            custom = list(custom) + [TITLE_LLM_TAG]
        out["custom_tags"] = custom
        return out
    if isinstance(tags_value, str):
        try:
            return add_title_llm_tag(json.loads(tags_value))
        except Exception:
            return {"custom_tags": [tags_value, TITLE_LLM_TAG]}
    return {"custom_tags": [TITLE_LLM_TAG]}


def require_backup_for_execute():
    """Refuse execute mode unless at least one DB backup exists."""
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
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--pdf-fix",
        action="store_true",
        help="Enable F1 PDF title extraction (disabled by default for safety)."
    )
    ap.add_argument("--no-pdf-fix", action="store_true",
                    help="Skip F1 PDF extraction (unreliable for many publishers). "
                         "All broken titles fall through to F2/F3 — typically tag for LLM pass.")
    args = ap.parse_args()
    use_pdf_fix = args.pdf_fix and not args.no_pdf_fix

    print("=" * 70)
    print(f"Phase 2w: fix broken titles (mode: {'EXECUTE' if args.execute else 'DRY RUN'})")
    print("=" * 70)
    print(f"F1 PDF extraction: {'ENABLED' if use_pdf_fix else 'DISABLED (safe default)'}")

    if args.execute and not require_backup_for_execute():
        return

    all_meta = papers_db.get_all_metadata()
    print(f"Total rows: {len(all_meta)}")

    broken = []
    for p in all_meta:
        pid = p.get("id")
        title = get_title(p)
        surname = get_first_author_surname(p)
        is_bad, reason = title_looks_broken(title, pid, surname)
        if is_bad:
            broken.append((p, title, reason))

    print(f"Broken titles detected: {len(broken)}")
    by_reason = {}
    for _, _, r in broken:
        by_reason[r] = by_reason.get(r, 0) + 1
    for r, n in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"    {n:4d}  {r}")
    print()

    if args.limit:
        broken = broken[:args.limit]

    fix_pdf = 0
    fix_filename = 0
    no_fix = 0
    updates = []  # (paper, old_title, new_title, source)

    for p, old_title, reason in broken:
        pid = p.get("id")
        fi = p.get("file_info") or {}
        fn = fi.get("renamed_filename") if isinstance(fi, dict) else None
        pdf_path = (config.SOURCE_PDFS / fn) if fn else None

        new_title = None
        source = None
        if pdf_path and pdf_path.exists() and use_pdf_fix:
            new_title = try_pdf_title(pdf_path)
            if new_title:
                source = "F1_pdf"
                fix_pdf += 1
        if not new_title and fn:
            new_title = try_filename_title(fn)
            if new_title:
                source = "F2_filename"
                fix_filename += 1
        if not new_title:
            no_fix += 1
            continue

        # Sanity: don't replace a longer title with a shorter one
        if old_title and len(new_title) <= len(old_title):
            no_fix += 1
            continue

        updates.append((p, old_title, new_title, source))

    print(f"Fix sources:")
    print(f"    {fix_pdf:4d}  F1 (PDF first-page extraction)")
    print(f"    {fix_filename:4d}  F2 (filename keyword parse)")
    print(f"    {no_fix:4d}  no fix (will tag title-needs-llm-extraction)")
    print()
    print(f"Updates to apply: {len(updates)}")
    print()
    print("Sample updates (first 10):")
    for p, old, new, src in updates[:10]:
        pid = p.get("id")
        print(f"  {pid}  [{src}]")
        print(f"    old: {old[:80]!r}")
        print(f"    new: {new[:80]!r}")
    print()

    if not args.execute:
        print("DRY RUN. Pass --execute to apply.")
        return

    # Apply updates
    for p, old, new, src in updates:
        bm = p.get("basic_metadata") or {}
        if not isinstance(bm, dict):
            bm = {}
        bm["title"] = new
        bm["_title_fix_source"] = src
        bm["_title_fix_prev"] = old
        p["basic_metadata"] = bm
        papers_db.save_paper(p)

    # Tag unfixable rows
    tagged = 0
    fixed_ids = {p.get("id") for p, _, _, _ in updates}
    for p, old, reason in broken:
        if p.get("id") in fixed_ids:
            continue
        p["tags"] = add_title_llm_tag(p.get("tags"))
        papers_db.save_paper(p)
        tagged += 1

    print(f"Updated {len(updates)} titles.")
    print(f"Tagged {tagged} unfixable rows with 'title-needs-llm-extraction'.")


if __name__ == "__main__":
    main()
