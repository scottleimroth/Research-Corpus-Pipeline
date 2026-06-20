#!/usr/bin/env python3
"""
dump_to_json.py — Export papers.db to JSON for backward compatibility.

Outputs to CorpusStore/papers-master-export.json
"""
import sys
import time
from pathlib import Path

# --- Portable path bootstrap (Phase 2 repath) ---
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import papers_db

OUTPUT = papers_db.BASE / "papers-master-export.json"


def main():
    print(f"Exporting papers.db to JSON...")
    t0 = time.time()
    count = papers_db.dump_all_to_json(str(OUTPUT))
    elapsed = time.time() - t0
    size_mb = OUTPUT.stat().st_size / (1024 * 1024)
    print(f"Exported {count} papers to {OUTPUT}")
    print(f"Size: {size_mb:.1f} MB in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
