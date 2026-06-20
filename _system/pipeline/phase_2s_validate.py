"""Phase 2s: validation pass after cleanup.

Checks:
  V1) All file_info.renamed_filename point at a file that exists in source-pdfs
  V2) No filename is claimed by multiple DB rows (no multi-owned)
  V3) No source-pdfs file has content-hash dupes
  V4) Tags pdf-missing count matches expected
  V5) No truncated-id pairs surviving (ids sharing >=50 char prefix with short tail diff)
  V6) supplements.items[].filename all resolve to existing files

Read-only audit. Prints PASS/FAIL/COUNT for each check.
"""
import hashlib
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
import config  # noqa: E402
import papers_db  # noqa: E402


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    print("=" * 70)
    print("Phase 2s: validation")
    print("=" * 70)

    all_meta = papers_db.get_all_metadata()
    source_pdfs = {p.name for p in config.SOURCE_PDFS.glob("*.pdf")}
    print(f"DB rows: {len(all_meta)}")
    print(f"source-pdfs files: {len(source_pdfs)}")
    print()

    # V1
    v1_misses = []
    for p in all_meta:
        fi = p.get("file_info") or {}
        if isinstance(fi, dict):
            fn = fi.get("renamed_filename")
            if fn and fn not in source_pdfs:
                v1_misses.append((p.get("id"), fn))
    print(f"V1 file_info.renamed_filename -> existing source-pdf: "
          f"{'PASS' if not v1_misses else f'FAIL ({len(v1_misses)} broken pointers)'}")
    for pid, fn in v1_misses[:10]:
        print(f"    {pid} -> {fn}  (missing)")

    # V2
    fname_owners = defaultdict(list)
    for p in all_meta:
        fi = p.get("file_info") or {}
        if isinstance(fi, dict):
            fn = fi.get("renamed_filename")
            if fn:
                fname_owners[fn].append(p.get("id"))
    multi = {fn: ids for fn, ids in fname_owners.items() if len(ids) >= 2}
    print(f"V2 no multi-owned filenames: "
          f"{'PASS' if not multi else f'FAIL ({len(multi)} multi-owned)'}")
    for fn, ids in list(multi.items())[:5]:
        print(f"    {fn}: claimed by {len(ids)} rows")

    # V3 - content hash dupes (expensive, skip with note)
    if "--full" in sys.argv:
        print("V3 hashing source-pdfs (full check)...")
        hash_to_files = defaultdict(list)
        for i, p in enumerate(sorted(config.SOURCE_PDFS.glob("*.pdf"))):
            if i and i % 400 == 0:
                print(f"    {i}/{len(source_pdfs)}")
            hash_to_files[sha256_file(p)].append(p.name)
        dupes = {h: fs for h, fs in hash_to_files.items() if len(fs) > 1}
        print(f"V3 no source-pdfs content-hash dupes: "
              f"{'PASS' if not dupes else f'FAIL ({len(dupes)} dupe groups)'}")
        for h, fs in list(dupes.items())[:5]:
            print(f"    hash {h[:10]}... -> {fs}")
    else:
        print("V3 source-pdfs content-hash dupes: SKIPPED (use --full to enable)")

    # V4
    missing_count = sum(
        1 for p in all_meta
        if isinstance(p.get("tags"), list) and "pdf-missing" in p["tags"]
    )
    print(f"V4 pdf-missing rows: {missing_count}")

    # V5
    ids = sorted(p.get("id", "") for p in all_meta if p.get("id"))
    trunc_pairs = []
    for i in range(len(ids) - 1):
        a, b = ids[i], ids[i + 1]
        if not a or not b or a == b:
            continue
        prefix = 0
        n = min(len(a), len(b))
        while prefix < n and a[prefix] == b[prefix]:
            prefix += 1
        if prefix >= 50 and (len(a) - prefix) <= 4 and (len(b) - prefix) <= 4:
            trunc_pairs.append((a, b))
    print(f"V5 no surviving truncated-id pairs: "
          f"{'PASS' if not trunc_pairs else f'FAIL ({len(trunc_pairs)} pairs)'}")
    for a, b in trunc_pairs[:5]:
        print(f"    {a}  ~  {b}")

    # V6
    supp_misses = []
    for p in all_meta:
        supp = p.get("supplements") or {}
        items = supp.get("items") if isinstance(supp, dict) else []
        for it in (items or []):
            if isinstance(it, dict):
                fn = it.get("filename")
                if fn and fn not in source_pdfs:
                    supp_misses.append((p.get("id"), fn))
    print(f"V6 supplements.items[].filename -> existing: "
          f"{'PASS' if not supp_misses else f'FAIL ({len(supp_misses)} broken)'}")
    for pid, fn in supp_misses[:10]:
        print(f"    {pid} -> {fn}")

    print()
    print("Validation complete.")


if __name__ == "__main__":
    main()
