#!/usr/bin/env python3
"""Identify and restore staging PDFs falsely deleted by self-staging-hash bug."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import config  # noqa: E402
from path_governance import MAX_FILENAME, safe_destination_for_file  # noqa: E402

RUN_ID = "all-staging-gui-20260531-094549"
RUN_DIR = config.LOGS_DIR / "runs" / RUN_ID
STAGING = config.STAGING
REPORT_PATH = config.LOGS_DIR / "recovery" / "false_duplicate_deletion_restore_report.json"

RECOVERY_SEARCH_DIRS = [
    ROOT,
    ROOT.parent,
    ROOT.parent / "CorpusPipeline-LIVECCopy",
    ROOT.parent / "papers-for-processing",
    ROOT.parent / "papers",
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _base_stem(name: str) -> str:
    return re.sub(r"(_\d+)+$", "", Path(name).stem)


def _load_false_deleted() -> list[dict]:
    progress = RUN_DIR / "all_staging_ingest_progress.jsonl"
    seen: set[str] = set()
    out: list[dict] = []

    for line in progress.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        if o.get("event") != "paper_done":
            continue
        reason = str(o.get("reason") or "")
        status = str(o.get("status") or "")
        if status != "confirmed_duplicate_deleted" or "ingest_failed_disposition_stop" not in reason:
            continue
        fin = o.get("recovery_final") or {}
        if not fin.get("deleted_duplicate"):
            continue
        ladder = (fin.get("evidence") or {}).get("recovery_ladder") or {}
        le = ladder.get("evidence") or {}
        if le.get("match") != "staging_hash_identical":
            continue
        fn = str(o.get("filename") or "")
        if not fn or fn in seen:
            continue
        seen.add(fn)
        rec = o.get("recovery") or {}
        out.append(
            {
                "filename": fn,
                "original_staging_path": str(STAGING / fn),
                "recovery_renamed_path": rec.get("recovered_to_staging"),
                "deleted_path": fin.get("path"),
                "sha256": le.get("sha256"),
                "size_bytes": le.get("file_size"),
                "match": le.get("match"),
                "ladder_step": le.get("ladder_step"),
            }
        )
    return out


def _audit_paths_for_deleted(deleted_path: str | None) -> list[str]:
    if not deleted_path:
        return []
    hits: list[str] = []
    name = Path(deleted_path).name
    audit_dirs = [
        config.FILE_DISPOSITION_AUDIT_DIR,
        config.LOGS_DIR / "disposition-audit",
    ]
    for audit_dir in audit_dirs:
        if not audit_dir.exists():
            continue
        for jsonl in sorted(audit_dir.glob("*.jsonl")):
            try:
                for line in jsonl.read_text(encoding="utf-8").splitlines():
                    if name in line and "DELETE" in line:
                        hits.append(str(jsonl))
                        break
            except OSError:
                continue
    return hits


def _owned_source_hashes() -> set[str]:
    from preflight_hash_cache import build_owned_source_hash_index

    hashes, _ = build_owned_source_hash_index()
    return set(hashes.keys())


def _staging_by_hash() -> dict[str, str]:
    idx: dict[str, str] = {}
    for p in STAGING.glob("*.pdf"):
        try:
            idx[_sha256(p)] = str(p)
        except OSError:
            continue
    return idx


def _find_source_by_hash(digest: str) -> Path | None:
    for root in RECOVERY_SEARCH_DIRS:
        if not root.exists():
            continue
        globs = [root.glob("*.pdf")]
        if root.name != "source-pdfs":
            globs.append(root.rglob("*.pdf"))
        for g in globs:
            for p in g:
                if "CorpusStore" in p.parts and "source-pdfs" in p.parts:
                    continue
                try:
                    if _sha256(p) == digest:
                        return p
                except OSError:
                    continue
    return None


def main() -> int:
    false_deleted = _load_false_deleted()
    owned = _owned_source_hashes()
    staging_by_hash = _staging_by_hash()

    records: list[dict] = []
    counts = {
        "restored": 0,
        "already_present": 0,
        "already_in_db": 0,
        "not_found": 0,
        "hash_mismatch": 0,
    }

    for item in false_deleted:
        fn = item["filename"]
        sha = (item.get("sha256") or "").lower()
        rec = {
            **item,
            "audit_record_paths": _audit_paths_for_deleted(item.get("deleted_path")),
            "status": "not_found",
            "restore_source": None,
        }

        if sha in owned:
            rec["status"] = "already_in_db"
            counts["already_in_db"] += 1
            records.append(rec)
            continue

        if sha in staging_by_hash:
            path = Path(staging_by_hash[sha])
            rec["status"] = "already_present"
            rec["staging_path"] = str(path)
            rec["staging_sha256"] = sha
            try:
                rec["staging_size_bytes"] = path.stat().st_size
            except OSError:
                pass
            if path.name != fn:
                rec["note"] = f"present as {path.name}"
            counts["already_present"] += 1
            records.append(rec)
            continue

        source = _find_source_by_hash(sha)
        if source is None:
            rec["status"] = "not_found"
            rec["search_clue"] = (
                f"Search papers-for-processing or CorpusPipeline-LIVECCopy for {_base_stem(fn)!r}; sha256={sha}"
            )
            counts["not_found"] += 1
            records.append(rec)
            continue

        dest = safe_destination_for_file(
            source,
            STAGING,
            reason="false_duplicate_restore",
            desired_filename=fn,
            max_filename=MAX_FILENAME - len(".review-retry.json"),
        )
        STAGING.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        got = _sha256(dest)
        if got != sha:
            dest.unlink(missing_ok=True)
            rec["status"] = "hash_mismatch"
            rec["restore_source"] = str(source)
            rec["found_sha256"] = got
            counts["hash_mismatch"] += 1
        else:
            rec["status"] = "restored"
            rec["restore_source"] = str(source)
            rec["restored_path"] = str(dest)
            counts["restored"] += 1
        records.append(rec)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": RUN_ID,
        "bug": "self_staging_hash_duplicate_deletion",
        "false_deleted_identified": len(false_deleted),
        **counts,
        "records": records,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "records"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
