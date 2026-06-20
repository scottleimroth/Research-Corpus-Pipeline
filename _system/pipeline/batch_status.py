#!/usr/bin/env python3
"""Read batch / ledger status from staging files (read-only)."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_constants import EVAL_PLANS_DIR, EVAL_STAGING  # noqa: E402
from eval_ledger import load_events, latest_status_by_paper  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Evaluation batch status (read-only)")
    parser.add_argument("--ledger", action="store_true", default=True)
    args = parser.parse_args()

    print(f"Staging dir: {EVAL_STAGING}")
    meta = EVAL_PLANS_DIR / "last_batch_meta.json"
    if meta.exists():
        print("Last batch meta:", meta.read_text(encoding="utf-8"))
    else:
        print("No last_batch_meta.json")

    plans = sorted(EVAL_PLANS_DIR.glob("*.json")) if EVAL_PLANS_DIR.exists() else []
    print(f"Plan files: {len(plans)}")
    for p in plans[:5]:
        data = json.loads(p.read_text(encoding="utf-8"))
        print(f"  {p.name}: {data.get('request_count', '?')} requests")

    events = load_events()
    print(f"Ledger events: {len(events)}")
    latest = latest_status_by_paper()
    if latest:
        counts = {}
        for s in latest.values():
            counts[s] = counts.get(s, 0) + 1
        print("Latest status counts:", counts)


if __name__ == "__main__":
    main()
