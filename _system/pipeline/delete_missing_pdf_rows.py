"""Export and delete DB rows whose source PDF is not on disk.

This is an active cleanup tool for the policy that papers.db should describe
the PDFs actually on hand. It only deletes rows that still have no existing
source PDF after reconciliation tooling has found no exact safe fix.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import papers_db  # noqa: E402
import reconcile_source_pdf_paths  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _deserialize(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in papers_db.ALL_COLUMNS:
        value = row[col]
        if col in papers_db.JSON_FIELDS:
            out[col] = _json_obj(value) if value else None
        else:
            out[col] = value
    return out


def _source_for_row(row: sqlite3.Row) -> Path | None:
    file_info = _json_obj(row["file_info"])
    rel = file_info.get("filepath") or file_info.get("managed_pdf_relative_path")
    if not rel:
        return None
    raw = Path(str(rel))
    return raw if raw.is_absolute() else config.MASTER_PROJECT / raw


def _category(row: sqlite3.Row) -> str | None:
    file_info = _json_obj(row["file_info"])
    source = _source_for_row(row)
    if not file_info:
        return "missing_file_info"
    if source is None:
        return "missing_filepath"
    if not source.exists():
        return "stale_path_no_file_on_disk"
    return None


def _pdf_snapshot_digest() -> dict[str, Any]:
    h = hashlib.sha256()
    count = 0
    for root in (config.STAGING, config.SOURCE_PDFS):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.pdf")):
            stat = path.stat()
            rel = str(path.relative_to(config.BASE))
            payload = f"{rel}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8", errors="replace")
            h.update(len(payload).to_bytes(8, "big"))
            h.update(payload)
            count += 1
    return {"count": count, "digest": h.hexdigest()}


def _create_backup() -> dict[str, Any]:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.BACKUP_DIR / f"papers-{datetime.now():%Y%m%d-%H%M%S}-PRE-DELETE-MISSING-PDF-ROWS.db"
    source = sqlite3.connect(str(config.DB_PATH))
    backup = sqlite3.connect(str(dest))
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    size = dest.stat().st_size if dest.exists() else 0
    conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        row_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
    finally:
        conn.close()
    return {
        "ok": dest.exists() and size > 1024 * 1024 and quick_check == "ok",
        "path": str(dest),
        "bytes": size,
        "quick_check": quick_check,
        "row_count": row_count,
    }


def _audit_path() -> Path:
    out_dir = config.MASTER_PROJECT / "deleted-row-audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"deleted_missing_pdf_rows_{datetime.now():%Y%m%d-%H%M%S}.jsonl"


def build_plan() -> dict[str, Any]:
    reconcile_plan = reconcile_source_pdf_paths.build_plan()
    safe_fix_ids = {item["id"] for item in reconcile_plan.get("safe_fixes") or []}
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT {','.join(papers_db.ALL_COLUMNS)} FROM papers ORDER BY id").fetchall()
    finally:
        conn.close()

    categories: Counter[str] = Counter()
    delete_rows: list[sqlite3.Row] = []
    held_rows: list[dict[str, Any]] = []
    for row in rows:
        category = _category(row)
        if not category:
            continue
        categories[category] += 1
        if row["id"] in safe_fix_ids:
            held_rows.append({"id": row["id"], "reason": "safe_path_fix_still_available", "category": category})
            continue
        # At this point the row has no existing PDF path and reconciliation
        # found no exact safe match. The row leaves active DB, with full audit.
        delete_rows.append(row)

    return {
        "created_at": _now(),
        "mode": "execute-ready",
        "unresolved_count": sum(categories.values()),
        "categories": dict(categories),
        "reconciliation_safe_fix_count": len(safe_fix_ids),
        "delete_count": len(delete_rows),
        "delete_ids": [row["id"] for row in delete_rows],
        "held_rows": held_rows,
        "candidate_rejections": reconcile_plan.get("candidate_rejections", {}),
        "side_effects": {"db_writes": "none yet", "pdf_operations": "none", "api_calls": "none", "evaluation": "none"},
    }


def execute(plan: dict[str, Any]) -> dict[str, Any]:
    delete_ids = set(plan.get("delete_ids") or [])
    if not delete_ids:
        return {"ok": True, "status": "no_rows_to_delete", "backup": None, "audit_export_path": None}

    backup = _create_backup()
    if not backup.get("ok"):
        return {"ok": False, "status": "refused_backup_failed", "backup": backup}

    audit_path = _audit_path()
    before_pdf = _pdf_snapshot_digest()
    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        before_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        rows = conn.execute(
            f"SELECT {','.join(papers_db.ALL_COLUMNS)} FROM papers WHERE id IN ({','.join(['?'] * len(delete_ids))}) ORDER BY id",
            tuple(sorted(delete_ids)),
        ).fetchall()
        if len(rows) != len(delete_ids):
            raise RuntimeError("delete target row count changed before export")
        with audit_path.open("w", encoding="utf-8") as f:
            for row in rows:
                payload = {
                    "deleted_at": _now(),
                    "delete_reason": "source PDF missing/unresolvable; re-add later by placing PDF into staging",
                    "row": _deserialize(row),
                }
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        conn.executemany("DELETE FROM papers WHERE id = ?", [(paper_id,) for paper_id in sorted(delete_ids)])
        conn.commit()
        after_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
    finally:
        conn.close()

    after_pdf = _pdf_snapshot_digest()
    failures: list[str] = []
    if before_count - after_count != len(delete_ids):
        failures.append("row deletion count mismatch")
    if before_pdf != after_pdf:
        failures.append("PDF snapshot changed")
    if not audit_path.exists() or sum(1 for _ in audit_path.open("r", encoding="utf-8")) != len(delete_ids):
        failures.append("audit export missing or wrong line count")

    return {
        "ok": not failures,
        "status": "completed" if not failures else "post_delete_validation_failed",
        "backup": backup,
        "audit_export_path": str(audit_path),
        "deleted_count": len(delete_ids),
        "db_validation": {
            "before_count": before_count,
            "after_count": after_count,
            "expected_after_count": before_count - len(delete_ids),
            "row_count_delta_ok": before_count - after_count == len(delete_ids),
            "pdf_snapshot_unchanged": before_pdf == after_pdf,
            "failures": failures,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete active DB rows whose source PDFs are missing")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-db-write", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    plan = build_plan()
    result: dict[str, Any] = {"ok": True, "mode": "dry_run", "plan": plan}
    if args.execute:
        if not args.allow_db_write:
            result = {"ok": False, "mode": "execute", "status": "refused_missing_allow_db_write", "plan": plan}
        else:
            result = {"ok": True, "mode": "execute", "plan": plan, "delete": execute(plan)}
            result["ok"] = bool(result["delete"].get("ok"))

    if args.summary_only:
        delete = result.get("delete", {})
        compact = {
            "ok": result.get("ok"),
            "mode": result.get("mode"),
            "status": result.get("status") or delete.get("status"),
            "unresolved_count": plan.get("unresolved_count"),
            "categories": plan.get("categories"),
            "delete_count": plan.get("delete_count"),
            "deleted_count": delete.get("deleted_count", 0),
            "held_rows": plan.get("held_rows"),
            "candidate_rejections": plan.get("candidate_rejections"),
            "backup": delete.get("backup"),
            "audit_export_path": delete.get("audit_export_path"),
            "db_validation": delete.get("db_validation"),
            "sample_delete_ids": (plan.get("delete_ids") or [])[:20],
        }
        print(json.dumps(compact, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
