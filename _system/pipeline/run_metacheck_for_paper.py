#!/usr/bin/env python3
"""Run MetaCheck for one existing corpus paper and store compact evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from metacheck_integration import run_metacheck_for_paper_id  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run MetaCheck sidecar for one paper")
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--modules", default="", help="Comma-separated MetaCheck modules")
    parser.add_argument("--force", action="store_true", help="Ignore cached ok result")
    args = parser.parse_args()

    modules = [x.strip() for x in args.modules.split(",") if x.strip()] or None
    result = run_metacheck_for_paper_id(args.paper_id, modules=modules, force=args.force)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

