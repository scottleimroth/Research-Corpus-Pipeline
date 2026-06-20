"""Recycle weird/orphan/unknown ID rows back through staging ingest.

Policy: export full row handoff, move source PDF to papers-staging, delete DB row.
No ID renaming, no PDF deletion, no API calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import papers_db  # noqa: E402
import source_pdf_ownership  # noqa: E402


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


def _safe_dest(path: Path, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    candidate = root / path.name
    if not candidate.exists():
        return candidate
    stem = path.stem
    suffix = path.suffix
    for i in range(2, 10000):
        candidate = root / f"{stem}__reprocess_{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not create non-overwriting destination for {path}")


def _create_backup() -> dict[str, Any]:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.BACKUP_DIR / f"papers-{datetime.now():%Y%m%d-%H%M%S}-PRE-WEIRD-ID-RECYCLE.db"
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


def _pdf_owner_map() -> dict[str, set[str]]:
    """Map normalized source-pdf rel path -> strong owner paper IDs."""
    rows = source_pdf_ownership._db_rows()
    source_paths = source_pdf_ownership._source_pdfs()
    refs, _ = source_pdf_ownership._build_refs(rows)
    source_by_rel = {
        (source_pdf_ownership._rel_to_master(path) or "").lower(): path for path in source_paths
    }
    source_by_name: dict[str, list[Path]] = defaultdict(list)
    for path in source_paths:
        source_by_name[path.name.lower()].append(path)
    source_hashes = source_pdf_ownership._source_hashes_if_needed(source_paths, refs)
    source_matches: dict[str, list[dict[str, Any]]] = {
        (source_pdf_ownership._rel_to_master(path) or "").lower(): [] for path in source_paths
    }
    for ref in refs:
        if ref["is_hash_field"]:
            for path in source_hashes.get(str(ref["value"]).lower(), []):
                rel = (source_pdf_ownership._rel_to_master(path) or "").lower()
                source_matches[rel].append({**ref, "match_type": "hash_exact"})
            continue
        rel = (ref.get("resolved_rel") or "").lower()
        if rel in source_by_rel:
            match_type = "canonical_path" if ref.get("is_canonical_path_field") else "path_exact"
            source_matches[rel].append({**ref, "match_type": match_type})
        basename = str(ref.get("basename") or "").lower()
        if source_pdf_ownership.PDF_EXT_RX.search(basename):
            for path in source_by_name.get(basename, []):
                rel = (source_pdf_ownership._rel_to_master(path) or "").lower()
                source_matches[rel].append(
                    {
                        **ref,
                        "match_type": "weak_id_filename" if ref.get("is_weak") else "filename_exact",
                    }
                )
    owners: dict[str, set[str]] = {}
    for rel, matches in source_matches.items():
        strong = [m for m in matches if not m.get("is_weak")]
        owners[rel] = {m["id"] for m in strong}
    return owners


def _load_target_ids(audit_path: Path) -> list[str]:
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    return [row["paper_id"] for row in payload.get("rows") or []]


def build_plan(audit_path: Path) -> dict[str, Any]:
    target_ids = _load_target_ids(audit_path)
    target_set = set(target_ids)
    owners = _pdf_owner_map()

    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        before_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        rows_by_id = {
            row["id"]: row
            for row in conn.execute(
                f"SELECT {','.join(papers_db.ALL_COLUMNS)} FROM papers WHERE id IN ({','.join(['?'] * len(target_ids))})",
                tuple(target_ids),
            ).fetchall()
        }
    finally:
        conn.close()

    recycle: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    missing_ids = [pid for pid in target_ids if pid not in rows_by_id]

    for paper_id in target_ids:
        if paper_id not in rows_by_id:
            skipped.append({"paper_id": paper_id, "reason": "row_not_found_in_db"})
            continue
        row = rows_by_id[paper_id]
        source = _source_for_row(row)
        if source is None:
            skipped.append({"paper_id": paper_id, "reason": "missing_source_pdf_path"})
            continue
        if not source.exists():
            skipped.append({"paper_id": paper_id, "reason": "source_pdf_missing_on_disk", "source": str(source)})
            continue
        rel = (
            source_pdf_ownership._rel_to_master(source) or str(source.relative_to(config.MASTER_PROJECT))
        ).replace("\\", "/").lower()
        owner_ids = owners.get(rel, set())
        external_owners = sorted(owner_ids - target_set)
        if external_owners:
            skipped.append(
                {
                    "paper_id": paper_id,
                    "reason": "shared_pdf_with_non_recycle_row",
                    "source_pdf": rel,
                    "external_owner_ids": external_owners,
                }
            )
            continue
        recycle.append(
            {
                "paper_id": paper_id,
                "source_pdf": rel,
                "source_path": str(source),
                "co_recycle_owner_ids": sorted(owner_ids & target_set),
            }
        )

    return {
        "created_at": _now(),
        "audit_source": str(audit_path),
        "target_count": len(target_ids),
        "db_rows_before": before_count,
        "recycle_count": len(recycle),
        "skip_count": len(skipped),
        "missing_ids": missing_ids,
        "recycle": recycle,
        "skipped": skipped,
        "side_effects": {"db_writes": "none", "pdf_moves": "none", "api_calls": "none", "evaluation": "none"},
    }


def execute(plan: dict[str, Any]) -> dict[str, Any]:
    recycle = plan.get("recycle") or []
    target_ids = _load_target_ids(Path(plan["audit_source"]))
    target_set = set(target_ids)
    recycle_ids = {item["paper_id"] for item in recycle}
    if not recycle_ids and not target_set:
        return {"ok": True, "status": "nothing_to_recycle", "backup": None}

    backup = _create_backup()
    if not backup.get("ok"):
        return {"ok": False, "status": "refused_backup_failed", "backup": backup}

    handoff_dir = config.RUNS_DIR
    handoff_dir.mkdir(parents=True, exist_ok=True)
    handoff_path = handoff_dir / f"weird-id-recycle-handoff-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    manifest_path = handoff_dir / f"weird-id-recycle-moves-{datetime.now():%Y%m%d-%H%M%S}.jsonl"

    conn = sqlite3.connect(str(config.DB_PATH), timeout=120)
    conn.row_factory = sqlite3.Row
    try:
        all_target_rows = conn.execute(
            f"SELECT {','.join(papers_db.ALL_COLUMNS)} FROM papers WHERE id IN ({','.join(['?'] * len(target_ids))}) ORDER BY id",
            tuple(sorted(target_ids)),
        ).fetchall()
        if len(all_target_rows) != len([pid for pid in target_ids if pid in {r['id'] for r in all_target_rows}]):
            pass
        with handoff_path.open("w", encoding="utf-8") as f:
            for row in all_target_rows:
                payload = {
                    "exported_at": _now(),
                    "recycle_action": "recycle" if row["id"] in recycle_ids else "skipped_in_plan",
                    "row": _deserialize(row),
                }
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")

        moved_pdfs: dict[str, str] = {}
        move_log: list[dict[str, Any]] = []
        import shutil

        for item in recycle:
            src = Path(item["source_path"])
            rel = item["source_pdf"]
            if rel in moved_pdfs:
                item["staging_pdf"] = moved_pdfs[rel]
                continue
            if not src.exists():
                raise RuntimeError(f"source PDF disappeared before move: {src}")
            dest = _safe_dest(src, config.STAGING)
            shutil.move(str(src), str(dest))
            moved_pdfs[rel] = str(dest)
            move_log.append({"source": str(src), "destination": str(dest), "paper_id": item["paper_id"]})
            item["staging_pdf"] = str(dest)

        with manifest_path.open("w", encoding="utf-8") as f:
            for entry in move_log:
                f.write(json.dumps({"moved_at": _now(), **entry}, ensure_ascii=False) + "\n")

        conn.execute("BEGIN IMMEDIATE")
        before_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        conn.executemany("DELETE FROM papers WHERE id = ?", [(pid,) for pid in sorted(recycle_ids)])
        conn.commit()
        after_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
    finally:
        conn.close()

    failures: list[str] = []
    if before_count - after_count != len(recycle_ids):
        failures.append("row deletion count mismatch")
    if not handoff_path.exists():
        failures.append("handoff export missing")
    quick_conn = sqlite3.connect(str(config.DB_PATH))
    try:
        quick_check = str(quick_conn.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        quick_conn.close()
    if quick_check != "ok":
        failures.append(f"quick_check={quick_check}")

    return {
        "ok": not failures,
        "status": "completed" if not failures else "post_recycle_validation_failed",
        "backup": backup,
        "handoff_path": str(handoff_path),
        "move_manifest_path": str(manifest_path),
        "db_rows_before": before_count,
        "db_rows_after": after_count,
        "rows_recycled": len(recycle_ids),
        "pdfs_moved": len(moved_pdfs),
        "skipped": plan.get("skipped") or [],
        "failures": failures,
        "side_effects": {
            "db_writes": "delete recycled rows only",
            "pdf_operations": "move source-pdfs to staging only",
            "pdf_deletes": "none",
            "api_calls": "none",
            "evaluation": "none",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Recycle weird/orphan/unknown ID rows to staging")
    parser.add_argument(
        "--audit-json",
        type=str,
        default=str(config.RUNS_DIR / "weird-id-audit-readonly.json"),
        help="Path to weird-id audit JSON with target rows",
    )
    parser.add_argument("--execute", action="store_true", help="Run backup, export, move PDFs, delete rows")
    args = parser.parse_args()
    audit_path = Path(args.audit_json)
    plan = build_plan(audit_path)
    print(json.dumps({k: v for k, v in plan.items() if k not in ("recycle", "skipped")}, indent=2))
    print(f"recycle_count={plan['recycle_count']} skip_count={plan['skip_count']}")
    if args.execute:
        result = execute(plan)
        print(json.dumps(result, indent=2))
        if not result.get("ok"):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
