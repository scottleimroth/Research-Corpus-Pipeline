#!/usr/bin/env python3
"""Move unowned source-pdfs (no DB owner) to review quarantine. Read-only DB; no deletes."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from path_governance import safe_destination_for_file
from source_pdf_ownership import build_audit

QUARANTINE_DIR = config.REVIEW_DIR / "quarantine-orphan-source"


def quarantine_unowned(*, dry_run: bool = False) -> dict:
    audit = build_audit()
    unowned = audit.get("unowned_source_pdfs") or []
    moved: list[dict] = []
    skipped: list[dict] = []

    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    for rel in unowned:
        src = config.MASTER_PROJECT / rel.replace("/", "\\") if "/" in rel else config.SOURCE_PDFS / Path(rel).name
        if not src.exists():
            src = config.MASTER_PROJECT / rel
        if not src.exists():
            skipped.append({"source": rel, "reason": "file_missing"})
            continue
        dest = safe_destination_for_file(src, QUARANTINE_DIR, reason="orphan_source_quarantine")
        if dry_run:
            moved.append({"source": str(src), "destination": str(dest), "dry_run": True})
            continue
        shutil.move(str(src), str(dest))
        moved.append({"source": str(src), "destination": str(dest)})

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "unowned_before": len(unowned),
        "moved_count": len(moved),
        "skipped_count": len(skipped),
        "moved": moved,
        "skipped": skipped,
    }
    if not dry_run and moved:
        out = config.RUNS_DIR / f"quarantine-orphan-source-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        manifest["manifest_path"] = str(out)

    audit_after = build_audit()
    manifest["unowned_after"] = audit_after.get("unowned_total")
    return manifest


def main() -> None:
    dry = "--dry-run" in sys.argv
    result = quarantine_unowned(dry_run=dry)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("unowned_after", 1) == 0 or dry else 1)


if __name__ == "__main__":
    main()
