"""Phase 2k: clean up papers-staging clutter.

Moves logs, audit artifacts, and stray batch6-7-8 manifests to archive/.
Keeps batch9/ untouched (those PDFs still need processing).
Keeps supplement PDFs at staging root (they're DB-linked from Phase 2j; we'll
decide whether to relocate after checking DB paths).

Dry-run by default. Pass --execute to actually move files.
"""
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "papers-staging"
ARCHIVE = ROOT / "archive" / f"papers-staging-{datetime.now():%Y-%m-%d}"

# Match by exact name or pattern
CLUTTER_DIRS = ["logs", "log-archive"]
CLUTTER_FILE_PATTERNS = [
    "*.log",
    "*.txt",
    "*.md",
    "audit_script.py",
    "db_audit_report.txt",
    "batch*_manifest.txt",
    "batch*_instructions.txt",
    "batch*_processing.log",
    "batch*_results.txt",
    "batch*_process.log",
    "batch*_main_process.log",
    "batch*_actual_process.log",
    "*.sentinel",
    ".checkpoint*",
    "pipeline-errors.log",
]
# Mystery PDFs at root that look like fugitive downloads - flag, don't move
MYSTERY_PATTERNS = ["download*.pdf", "ppomorski*.pdf", "2004-can-the-q-link*.pdf"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="actually move files")
    args = ap.parse_args()

    if not STAGING.exists():
        sys.exit(f"papers-staging not found at {STAGING}")

    to_move = []
    mystery = []
    seen_srcs = set()

    def _add(src, dst):
        if src in seen_srcs:
            return
        seen_srcs.add(src)
        to_move.append((src, dst))

    # Collect clutter dirs
    for d in CLUTTER_DIRS:
        p = STAGING / d
        if p.exists() and p.is_dir():
            _add(p, ARCHIVE / d)

    # Collect clutter files at staging root only (NOT recursively - don't touch batch9)
    for pat in CLUTTER_FILE_PATTERNS:
        for f in STAGING.glob(pat):
            if f.is_file():
                _add(f, ARCHIVE / f.name)

    # Flag mystery PDFs at root
    for pat in MYSTERY_PATTERNS:
        for f in STAGING.glob(pat):
            if f.is_file():
                mystery.append(f)

    print("=" * 60)
    print("Phase 2k: papers-staging cleanup")
    print("=" * 60)
    print(f"Source: {STAGING}")
    print(f"Archive target: {ARCHIVE}")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print()
    print(f"Items to archive: {len(to_move)}")
    for src, _ in to_move:
        print(f"  {src.relative_to(STAGING)}")
    print()
    print(f"Mystery PDFs at root (flagged, NOT moved):")
    for f in mystery:
        size_kb = f.stat().st_size // 1024
        print(f"  {f.name} ({size_kb} KB)")

    if not args.execute:
        print("\nDry run only. Pass --execute to move.")
        return

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    for src, dst in to_move:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print(f"  moved {src.name}")
    print("\nDone.")


if __name__ == "__main__":
    main()
