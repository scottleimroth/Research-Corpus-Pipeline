"""Phase 2p: clean up papers-for-processing/.

Pre-condition: Phases 2o + 2q have run (source-pdfs is deduped, p4p restorations applied).

For each PDF in papers-for-processing/:
  - Compute sha256
  - If content_hash matches any source-pdfs file: DELETE from p4p (already in canonical)
  - If filename matches a DB-referenced filename: DELETE from p4p (DB already has it)
  - Else: MOVE to papers-staging/ for the add_paper pipeline to process

Usage:
  python pipeline/phase_2p_p4p_cleanup.py             # dry-run
  python pipeline/phase_2p_p4p_cleanup.py --execute   # apply
"""
import argparse
import hashlib
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
import config  # noqa: E402
import papers_db  # noqa: E402

P4P = ROOT / "papers-for-processing"
STAGING = ROOT / "papers-staging"


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print(f"Phase 2p: p4p cleanup (mode: {'EXECUTE' if args.execute else 'DRY RUN'})")
    print("=" * 70)

    # Hash source-pdfs
    print("Hashing source-pdfs/...")
    source_pdfs = sorted(config.SOURCE_PDFS.glob("*.pdf"))
    source_hash = {}
    for i, p in enumerate(source_pdfs):
        if i and i % 400 == 0:
            print(f"  source {i}/{len(source_pdfs)}")
        source_hash[sha256_file(p)] = p.name
    print(f"  {len(source_hash)} unique hashes")

    # Build DB-referenced filenames set
    all_meta = papers_db.get_all_metadata()
    db_filenames = set()
    for p in all_meta:
        fi = p.get("file_info") or {}
        if isinstance(fi, dict) and fi.get("renamed_filename"):
            db_filenames.add(fi["renamed_filename"])
        supp = p.get("supplements") or {}
        items = supp.get("items") if isinstance(supp, dict) else []
        for it in (items or []):
            if isinstance(it, dict) and it.get("filename"):
                db_filenames.add(it["filename"])

    p4p_pdfs = sorted(P4P.glob("*.pdf"))
    print(f"p4p files: {len(p4p_pdfs)}")

    to_delete = []
    to_stage = []
    by_class = defaultdict(int)
    for i, pdf in enumerate(p4p_pdfs):
        if i and i % 400 == 0:
            print(f"  p4p {i}/{len(p4p_pdfs)}")
        h = sha256_file(pdf)
        if h in source_hash:
            to_delete.append(pdf)
            by_class["hash-dupe-of-source"] += 1
        elif pdf.name in db_filenames:
            to_delete.append(pdf)
            by_class["filename-in-db"] += 1
        else:
            to_stage.append(pdf)
            by_class["new"] += 1

    print()
    print(f"To delete (already canonical): {len(to_delete)}")
    for k, v in by_class.items():
        print(f"  {k}: {v}")
    print(f"To move to staging:            {len(to_stage)}")
    print()

    if to_stage:
        print("Sample new files (first 20):")
        for p in to_stage[:20]:
            print(f"  {p.name}")
        print()

    if not args.execute:
        print("DRY RUN. Pass --execute to apply.")
        return

    # Execute
    STAGING.mkdir(parents=True, exist_ok=True)
    deleted = 0
    failed_del = []
    for p in to_delete:
        try:
            p.unlink()
            deleted += 1
        except Exception as e:
            failed_del.append((p.name, str(e)))
    moved = 0
    failed_move = []
    for p in to_stage:
        dst = STAGING / p.name
        if dst.exists():
            failed_move.append((p.name, "destination exists in staging"))
            continue
        try:
            shutil.move(str(p), str(dst))
            moved += 1
        except Exception as e:
            failed_move.append((p.name, str(e)))

    print(f"Deleted: {deleted}, Moved to staging: {moved}")
    if failed_del:
        print(f"Delete failures: {len(failed_del)}")
        for fn, e in failed_del[:5]:
            print(f"  {fn}: {e}")
    if failed_move:
        print(f"Move failures: {len(failed_move)}")
        for fn, e in failed_move[:5]:
            print(f"  {fn}: {e}")
    print("Phase 2p done.")


if __name__ == "__main__":
    main()
