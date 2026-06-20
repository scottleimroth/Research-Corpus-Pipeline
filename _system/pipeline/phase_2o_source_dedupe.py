"""Phase 2o: source-pdfs hash-dedupe with redirect-then-delete.

For each group of source-pdfs files with the same content_hash:
  1. Find ALL DB references (file_info.renamed_filename + supplements.items[].filename)
  2. Pick canonical filename:
       - Most DB references wins
       - Tiebreak: prefer 'author_year_title.pdf' style over '1-s2.0-...' over 'unknown_unknown_...'
       - Final tiebreak: shorter name
  3. Redirect every DB reference from non-canonical names to canonical
  4. Delete non-canonical files from disk

If a group has zero DB references (entirely unowned), keep the cleanest-named one,
delete the rest. No DB update needed.

Usage:
  python pipeline/phase_2o_source_dedupe.py             # dry-run
  python pipeline/phase_2o_source_dedupe.py --execute   # apply
"""
import argparse
import hashlib
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
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


def name_quality_score(name):
    """Lower is better. Cleaner human-readable names win."""
    score = 0
    n = name.lower()
    if n.startswith("1-s2.0-") or n.startswith("1_s2_0_"):
        score += 1000
    if "unknown_unknown" in n:
        score += 2000
    if re.search(r"\(\d\)", name):  # ' (1).pdf' etc
        score += 500
    if re.search(r"_main", n):
        score += 200
    if re.search(r"^[a-z]+_\d{4}_", n):  # author_year_ prefix
        score -= 500
    if re.search(r"^[a-z]+_[a-z]+_", n):
        score -= 100
    score += len(name)  # mild preference for shorter
    return score


def pick_canonical(filenames, db_ref_counts):
    """Pick the canonical filename from a hash-dupe group."""
    # Sort by (-db_refs, name_quality_score) - more refs first, lower score first
    scored = [(name, db_ref_counts.get(name, 0), name_quality_score(name)) for name in filenames]
    scored.sort(key=lambda x: (-x[1], x[2]))
    return scored[0][0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print(f"Phase 2o: source-pdfs hash-dedupe (mode: {'EXECUTE' if args.execute else 'DRY RUN'})")
    print("=" * 70)

    # Hash all source-pdfs
    print("Hashing source-pdfs/...")
    pdfs = sorted(config.SOURCE_PDFS.glob("*.pdf"))
    hash_to_files = defaultdict(list)
    for i, p in enumerate(pdfs):
        if i and i % 400 == 0:
            print(f"  hashed {i}/{len(pdfs)}")
        hash_to_files[sha256_file(p)].append(p.name)
    print(f"  {len(pdfs)} files, {len(hash_to_files)} unique hashes")

    dupe_groups = {h: names for h, names in hash_to_files.items() if len(names) > 1}
    print(f"  {len(dupe_groups)} hash-dupe groups")

    # Build DB reference count: filename -> [(paper_id, ref_type, paper_dict)]
    print("Building DB reference map...")
    all_meta = papers_db.get_all_metadata()
    refs = defaultdict(list)
    for p in all_meta:
        pid = p.get("id")
        fi = p.get("file_info") or {}
        if isinstance(fi, dict):
            fn = fi.get("renamed_filename")
            if fn:
                refs[fn].append((pid, "main", p))
        supp = p.get("supplements") or {}
        items = supp.get("items") if isinstance(supp, dict) else []
        for idx, it in enumerate(items or []):
            if isinstance(it, dict):
                fn = it.get("filename")
                if fn:
                    refs[fn].append((pid, f"supplement[{idx}]", p))
    print(f"  {len(refs)} unique filenames referenced by DB")

    # Plan
    plan = []  # (group_hash, canonical, [non_canonical], [(paper_id, ref_type, old_fn)])
    files_to_delete = []
    db_updates = []  # (paper_id, field, old_fn, new_fn) where field is "main" or supplement index
    for h, names in dupe_groups.items():
        ref_counts = {n: len(refs.get(n, [])) for n in names}
        canonical = pick_canonical(names, ref_counts)
        non_canon = [n for n in names if n != canonical]
        for nc in non_canon:
            files_to_delete.append(nc)
            for pid, rtype, paper in refs.get(nc, []):
                db_updates.append((pid, rtype, nc, canonical))
        plan.append((h, canonical, non_canon, ref_counts))

    print()
    print(f"Files to delete:    {len(files_to_delete)}")
    print(f"DB reference updates: {len(db_updates)}")
    print()

    # Show sample
    print("Sample groups (first 10):")
    for h, canonical, non_canon, ref_counts in plan[:10]:
        print(f"  hash {h[:10]}...")
        print(f"    KEEP: {canonical}  ({ref_counts.get(canonical, 0)} DB refs)")
        for nc in non_canon[:3]:
            print(f"    DEL : {nc}  ({ref_counts.get(nc, 0)} DB refs)")
        if len(non_canon) > 3:
            print(f"    DEL : ...and {len(non_canon) - 3} more")
    print()

    if db_updates:
        print("Sample DB updates (first 10):")
        for pid, rtype, old, new in db_updates[:10]:
            print(f"  {pid} {rtype}: {old} -> {new}")
        print()

    if not args.execute:
        print("DRY RUN. Pass --execute to apply.")
        return

    # Execute - DB updates first, then file deletes
    from papers_db import _serialize_field
    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        # Group updates by paper to avoid clobbering when a paper has both main and supp refs
        by_paper = defaultdict(list)
        for pid, rtype, old, new in db_updates:
            by_paper[pid].append((rtype, old, new))
        by_id = {p.get("id"): p for p in all_meta}
        for pid, updates in by_paper.items():
            paper = by_id.get(pid)
            if not paper:
                continue
            fi = paper.get("file_info") or {}
            if not isinstance(fi, dict):
                fi = {}
            supp = paper.get("supplements") or {}
            items = supp.get("items") if isinstance(supp, dict) else []
            items = list(items or [])
            changed = False
            for rtype, old, new in updates:
                if rtype == "main":
                    fi["renamed_filename"] = new
                    changed = True
                elif rtype.startswith("supplement["):
                    idx = int(re.search(r"\[(\d+)\]", rtype).group(1))
                    if 0 <= idx < len(items) and isinstance(items[idx], dict):
                        items[idx]["filename"] = new
                        changed = True
            if changed:
                supp_dict = dict(supp) if isinstance(supp, dict) else {}
                supp_dict["items"] = items
                supp_dict["files"] = [it["filename"] for it in items
                                      if isinstance(it, dict) and it.get("filename")]
                conn.execute(
                    "UPDATE papers SET file_info = ?, supplements = ? WHERE id = ?",
                    (_serialize_field(fi), _serialize_field(supp_dict), pid))
        conn.commit()
        print(f"DB updates committed: {len(by_paper)} paper rows")
    finally:
        conn.close()

    # Delete non-canonical files
    deleted = 0
    failed = []
    for fn in files_to_delete:
        p = config.SOURCE_PDFS / fn
        try:
            if p.exists():
                p.unlink()
                deleted += 1
        except Exception as e:
            failed.append((fn, str(e)))

    print(f"Files deleted: {deleted}")
    if failed:
        print(f"Delete failures: {len(failed)}")
        for fn, e in failed[:5]:
            print(f"  {fn}: {e}")

    # Invalidate text cache (filenames changed)
    cache = ROOT / ".source-pdfs-text-cache.json"
    if cache.exists():
        cache.unlink()
        print(f"Invalidated stale text cache: {cache.name}")

    print()
    print("Phase 2o done.")


if __name__ == "__main__":
    main()
