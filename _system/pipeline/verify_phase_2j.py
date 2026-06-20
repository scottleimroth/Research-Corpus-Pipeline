"""Verify Phase 2j (orphan supplement backfill) post-execute state.

Uses papers_db API rather than raw SQL (DB stores each field in its own column).
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import papers_db  # noqa: E402

# Strict orphan suffix patterns (same as backfill_supplements.SUFFIX_PATTERNS)
ORPHAN_SUFFIX_RX = re.compile(
    r"(__supplement$|_supplement_\d+$|_supplement$|_Supplement$|_supp\d+$)"
)


def is_real_orphan(pid):
    return bool(ORPHAN_SUFFIX_RX.search(pid))


def main():
    all_meta = papers_db.get_all_metadata()
    print(f"Total paper rows: {len(all_meta)}")

    # Strict orphans (matching backfill's SUFFIX_PATTERNS)
    real_orphans = [p for p in all_meta if is_real_orphan(p.get("id", ""))]
    # Loose supplement-like ids (anything mentioning 'supplement', etc.)
    loose = [p for p in all_meta if re.search(
        r"_supp|_Supplement|SupplementalMaterial|SupplementalText|BrainHeartSupp",
        p.get("id", ""))]
    parent_like = [p for p in loose if not is_real_orphan(p.get("id", ""))]

    print(f"Real orphans (matchable by backfill): {len(real_orphans)}")
    print(f"Parent-like ids containing 'Supplement' (not orphans): {len(parent_like)}")
    for p in parent_like:
        print(f"  - {p.get('id')} (parent, NOT orphan)")
    print()
    print(f"Sample real orphans:")
    for p in real_orphans[:25]:
        fi = p.get("file_info") or {}
        has_file = bool(fi.get("renamed_filename") if isinstance(fi, dict) else None)
        print(f"  - {p.get('id')} (file_info: {'yes' if has_file else 'NO'})")

    # Sample parent supplements.items[] check
    sample_parents = [
        "Banellis2020", "Bretherton2019", "CandiaRivera2022a",
        "CostaFaidella2011", "Hunt_SupplementalMaterial",
    ]
    print("\nSample parent supplements.items[] check:")
    by_id = {p.get("id"): p for p in all_meta}
    for pid in sample_parents:
        p = by_id.get(pid)
        if not p:
            print(f"  {pid}: NOT FOUND")
            continue
        supp = p.get("supplements") or {}
        items = supp.get("items") if isinstance(supp, dict) else []
        items = items or []
        print(f"  {pid}: {len(items)} supplement item(s)")
        for it in items:
            fn = (it.get("filename") or it.get("original_filename") or "?") if isinstance(it, dict) else "?"
            print(f"      - {fn}")


if __name__ == "__main__":
    main()
