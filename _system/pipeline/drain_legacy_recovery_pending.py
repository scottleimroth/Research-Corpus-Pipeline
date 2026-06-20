#!/usr/bin/env python3
"""Drain review/recovery-pending via central disposition transactions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import config  # noqa: E402
from file_disposition_engine import drain_legacy_recovery_pending_files  # noqa: E402


def main() -> int:
    config.ensure_dirs()
    allow_paid = "--no-paid-api" not in sys.argv
    allow_opus = "--allow-opus" in sys.argv and allow_paid
    summary = drain_legacy_recovery_pending_files(
        allow_paid_api=allow_paid,
        allow_opus_resolution=allow_opus,
        allow_accept_to_db=False,
    )
    print(json.dumps(summary, indent=2))
    return 0 if int(summary.get("review_recovery_pending_remaining") or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
