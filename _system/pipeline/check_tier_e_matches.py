"""Manually verify the Tier E matches that look suspicious.

Compares orphan id + supplement PDF first-page text against the proposed parent's
metadata (title, authors, year). For each pair, prints side-by-side so Scott can
eyeball whether the match is correct before --execute.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import papers_db  # noqa: E402

# (orphan_id, proposed_parent_id, tier)
PAIRS = [
    ("MejiaMejia_Supplement", "MejiaMejia2020", "E1:id-prefix"),
    ("Lukowska2024_Supplement", "Lukowska2018", "E3:pdf-content"),
    ("Bree_Sustained_neural_rhythms_reveal_endogenous_supplement_1", "vanBree2021", "E2:name-prefix"),
    ("Bree_Sustained_neural_rhythms_reveal_endogenous_supplement_2", "vanBree2021", "E2:name-prefix"),
    ("Psychophysiological_Measurement_and_Meaning_Cog_supplement_1", "Hodges2010", "E3:pdf-content"),
    ("_liwi_ska_Kowalska_WHO_Environmental_Noise_Guid_supplement_1", "SliwinskaKowalska2017", "E3:pdf-content"),
]


def pdf_first_page(filename):
    if not filename:
        return ""
    from pdf_content_guard import extract_pypdf_text_safely

    path = config.SOURCE_PDFS / filename
    if not path.exists():
        return f"(file not found at {path})"
    text, err = extract_pypdf_text_safely(path, pages=1, tier="check_tier_e_first_page")
    if err:
        return f"(read error: {err})"
    return text[:1500]


def main():
    by_id = {p.get("id"): p for p in papers_db.get_all_metadata()}
    for orphan_id, parent_id, tier in PAIRS:
        print("=" * 70)
        print(f"ORPHAN: {orphan_id}")
        print(f"PROPOSED PARENT: {parent_id}  [tier {tier}]")
        print("=" * 70)

        orphan = by_id.get(orphan_id)
        parent = by_id.get(parent_id)
        if not orphan:
            print(f"  orphan not in DB - already deleted? skipping")
            print()
            continue
        if not parent:
            print(f"  proposed parent NOT IN DB - SKIP (bad match)")
            print()
            continue

        # Orphan side
        obm = orphan.get("basic_metadata") or {}
        ofi = orphan.get("file_info") or {}
        renamed = ofi.get("renamed_filename") if isinstance(ofi, dict) else None
        print(f"  orphan title:    {obm.get('title','')[:120]}")
        print(f"  orphan year:     {obm.get('year','')}")
        print(f"  orphan PDF file: {renamed}")

        # Parent side
        pbm = parent.get("basic_metadata") or {}
        print(f"  parent title:    {pbm.get('title','')[:120]}")
        print(f"  parent year:     {pbm.get('year','')}")
        print(f"  parent authors:  {(pbm.get('authors') or [])[:3]}")

        # PDF first page
        text = pdf_first_page(renamed)
        print()
        print("  --- supplement PDF first page (truncated) ---")
        print("  " + text.replace("\n", "\n  ")[:1000])
        print()


if __name__ == "__main__":
    main()
