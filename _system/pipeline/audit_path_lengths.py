#!/usr/bin/env python3
"""Audit and repair Windows-portable corpus paths."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import config  # noqa: E402
import papers_db  # noqa: E402

PROJECT_ROOT = config.PROJECT_ROOT
from path_governance import (  # noqa: E402
    MAX_FILENAME,
    MAX_FULL_PATH,
    MAX_STEM,
    active_ingest_lock_present,
    file_token,
    reserve_unique_path,
    safe_destination_for_file,
    safe_runtime_rename,
    shorten_existing_filename,
    stable_token,
    validate_path_policy,
)
from source_pdf_ownership import _missing_db_row_ids, build_audit  # noqa: E402

STAGING_SIDECAR_SUFFIX = ".review-retry.json"
PENDING_SIDECAR_SUFFIX = ".meta.json"
AUDIT_DIR = config.CORPUS_STORE / "audit" / "path-governance"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


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


def _db_rows(paper_id: str | None = None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = f"SELECT {','.join(papers_db.ALL_COLUMNS)} FROM papers"
        params: tuple[Any, ...] = ()
        if paper_id:
            sql += " WHERE id = ?"
            params = (paper_id,)
        return conn.execute(sql + " ORDER BY id", params).fetchall()
    finally:
        conn.close()


def _rel(path: Path, root: Path | None = None) -> str:
    base = root or config.MASTER_PROJECT
    try:
        return str(path.resolve().relative_to(base.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _resolve_source_value(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    raw = Path(str(value))
    return raw if raw.is_absolute() else config.MASTER_PROJECT / raw


def _walk_file_refs(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            refs.extend(_walk_file_refs(child, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            refs.extend(_walk_file_refs(child, f"{prefix}[{idx}]"))
    elif isinstance(value, str):
        key = prefix.lower()
        if key.endswith("original_filename"):
            return refs
        if any(tok in key for tok in ("file", "path", "pdf", "filename", "source", "supplement")):
            if ".pdf" in value.lower() or "/" in value or "\\" in value:
                refs.append((prefix, value))
    return refs


def _db_indexes(paper_id: str | None = None) -> dict[str, Any]:
    canonical: dict[str, list[dict[str, Any]]] = defaultdict(list)
    supplement: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_refs: list[dict[str, Any]] = []
    for row in _db_rows(paper_id):
        fi = _json_obj(row["file_info"])
        current = fi.get("filepath") or fi.get("managed_pdf_relative_path") or fi.get("managed_pdf_absolute_path")
        path = _resolve_source_value(current) if current else None
        if path is None and fi.get("renamed_filename"):
            path = config.SOURCE_PDFS / str(fi["renamed_filename"])
        if path and path.suffix.lower() == ".pdf":
            rec = {"paper_id": row["id"], "field": "file_info", "row": row}
            if path.exists():
                canonical[str(path.resolve()).lower()].append(rec)
            else:
                missing_refs.append({"paper_id": row["id"], "field": "file_info.filepath", "value": current or fi.get("renamed_filename"), "resolved_path": str(path)})
        for ref_field, value in _walk_file_refs(fi, "file_info"):
            name = Path(str(value)).name
            if ".pdf" not in name.lower():
                continue
            candidate = config.SOURCE_PDFS / name
            if candidate.exists():
                canonical[str(candidate.resolve()).lower()].append({"paper_id": row["id"], "field": ref_field, "row": row})
        supp = _json_obj(row["supplements"])
        for ref_field, value in _walk_file_refs(supp, "supplements"):
            if ref_field.endswith(".original_filename"):
                continue
            name = Path(str(value)).name
            if ".pdf" not in name.lower():
                continue
            path = _resolve_source_value(value)
            if path and not path.exists():
                path = config.SOURCE_PDFS / name
            rec = {"paper_id": row["id"], "field": ref_field, "row": row}
            if path and path.exists():
                supplement[str(path.resolve()).lower()].append(rec)
    return {"canonical": canonical, "supplement": supplement, "missing_refs": missing_refs}


def _paired_data_path(sidecar: Path) -> Path | None:
    name = sidecar.name
    if name.endswith(STAGING_SIDECAR_SUFFIX):
        data_name = name[: -len(STAGING_SIDECAR_SUFFIX)]
        data = (config.STAGING / data_name) if sidecar.parent == config.STAGING_METADATA else (sidecar.parent / data_name)
        return data if data.exists() else None
    if name.endswith(PENDING_SIDECAR_SUFFIX):
        data = sidecar.parent / name[: -len(PENDING_SIDECAR_SUFFIX)]
        return data if data.exists() else None
    return None


def _owner_type(path: Path, indexes: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str]:
    key = str(path.resolve()).lower()
    canonical = indexes["canonical"].get(key, [])
    supplement = indexes["supplement"].get(key, [])
    if canonical:
        if len({x["paper_id"] for x in canonical}) == 1:
            return "db_owned_source_pdf", canonical[0], "db_owned_source_repair"
        return "db_owned_source_pdf_ambiguous", {"owners": canonical}, "unsafe_manual"
    if supplement:
        if len({x["paper_id"] for x in supplement}) == 1:
            return "source_pdf_supplement_child", supplement[0], "db_owned_source_repair"
        return "source_pdf_supplement_child_ambiguous", {"owners": supplement}, "unsafe_manual"
    if _is_relative_to(path, config.SOURCE_PDFS):
        return "source_pdf_unowned_or_unmatched", None, "unsafe_manual"
    if _is_relative_to(path, config.STAGING_METADATA) or path.name.endswith(STAGING_SIDECAR_SUFFIX):
        return "sidecar", None, "sidecar_pair_repair" if _paired_data_path(path) else "report_only"
    if _is_relative_to(path, config.PENDING_PARENT_CHILD_DOCUMENTS):
        if path.name.endswith(PENDING_SIDECAR_SUFFIX):
            return "sidecar", None, "sidecar_pair_repair" if _paired_data_path(path) else "report_only"
        return "pending_child_support_doc", None, "sidecar_pair_repair"
    if _is_relative_to(path, config.STAGING):
        return "staging_file", None, "safe_auto_repair"
    for folder, label in (
        (config.BACKUP_DIR, "backup"),
        (config.LOGS_DIR, "log"),
        (config.CORPUS_STORE / "audit", "audit"),
        (config.CORPUS_STORE / "cache", "cache"),
    ):
        if _is_relative_to(path, folder):
            return label, None, "report_only"
    if path.suffix.lower() in {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".csv"}:
        return "code_config", None, "report_only"
    return "unknown_runtime_file", None, "report_only"


def _max_filename_for(owner_type: str) -> int:
    if owner_type == "staging_file":
        return MAX_FILENAME - len(STAGING_SIDECAR_SUFFIX)
    if owner_type == "pending_child_support_doc":
        return MAX_FILENAME - len(PENDING_SIDECAR_SUFFIX)
    return MAX_FILENAME


def _safe_source_name(path: Path, owner: dict[str, Any] | None) -> str:
    if not owner:
        return shorten_existing_filename(path.name, title=path.stem, hash_source=str(path))
    row = owner["row"]
    bm = _json_obj(row["basic_metadata"])
    return shorten_existing_filename(
        path.name,
        authors=bm.get("authors"),
        year=bm.get("year"),
        title=bm.get("title"),
        hash_source=f"{row['id']}:{path.name}",
    )


def _proposed_path(path: Path, owner_type: str, owner: dict[str, Any] | None, max_full_path: int, max_filename: int) -> Path | None:
    if owner_type in {"db_owned_source_pdf", "source_pdf_supplement_child"}:
        desired = _safe_source_name(path, owner)
        return reserve_unique_path(path.parent, desired, content_token=stable_token(owner.get("paper_id") if owner else "", path.name), max_full_path=max_full_path, max_filename=max_filename, existing_ok=path)
    if owner_type == "staging_file":
        return safe_destination_for_file(path, path.parent, reason="path_length_repair", max_full_path=max_full_path, max_filename=max_filename)
    if owner_type == "pending_child_support_doc":
        return safe_destination_for_file(path, path.parent, reason="pending_child_path_repair", max_full_path=max_full_path, max_filename=max_filename)
    if owner_type == "sidecar":
        data = _paired_data_path(path)
        if data:
            data_owner = "staging_file" if _is_relative_to(data, config.STAGING) else "pending_child_support_doc"
            data_dest = _proposed_path(data, data_owner, None, max_full_path, _max_filename_for(data_owner))
            if data_dest:
                if path.name.endswith(STAGING_SIDECAR_SUFFIX):
                    return path.parent / f"{data_dest.name}{STAGING_SIDECAR_SUFFIX}"
                return data_dest.with_suffix(data_dest.suffix + PENDING_SIDECAR_SUFFIX)
        return reserve_unique_path(path.parent, shorten_existing_filename(path.name, title=path.stem, hash_source=str(path)), max_full_path=max_full_path, max_filename=max_filename, existing_ok=path)
    return None


def scan_paths(root: Path, *, max_full_path: int, max_filename: int, paper_id: str | None = None) -> dict[str, Any]:
    indexes = _db_indexes(paper_id)
    entries: list[dict[str, Any]] = []
    max_path = 0
    max_name = 0
    for path in sorted(root.rglob("*"), key=lambda p: str(p).lower()):
        if not path.is_file():
            continue
        owner_type, owner, default_safety = _owner_type(path, indexes)
        effective_max_name = min(max_filename, _max_filename_for(owner_type))
        check = validate_path_policy(path, max_full_path=max_full_path, max_filename=effective_max_name)
        max_path = max(max_path, check["full_path_length"])
        max_name = max(max_name, check["filename_length"])
        violations = list(check["violations"])
        if owner_type in {"log", "backup", "audit", "cache", "code_config", "unknown_runtime_file"}:
            violations = [v for v in violations if v in {"full_path_too_long", "filename_too_long"}]
        if owner_type == "source_pdf_unowned_or_unmatched" and not any(v in {"full_path_too_long", "filename_too_long"} for v in violations):
            violations = [v for v in violations if v in {"full_path_too_long", "filename_too_long"}]
        safety = default_safety if violations else "ok_no_repair"
        if owner_type == "source_pdf_unowned_or_unmatched" and not violations:
            safety = "report_only"
        proposed = None
        if violations and safety != "unsafe_manual":
            proposed = _proposed_path(path, owner_type, owner, max_full_path, effective_max_name)
            if not proposed:
                safety = "report_only"
        if owner_type == "staging_file" and safety == "safe_auto_repair":
            if active_ingest_lock_present(config.LOGS_DIR / "all_staging.ingest.lock"):
                safety = "unsafe_manual"
        entries.append(
            {
                "path": str(path),
                "relative_path": _rel(path, root),
                "filename": path.name,
                "full_path_length": check["full_path_length"],
                "filename_length": check["filename_length"],
                "violations": violations,
                "owner_type": owner_type,
                "db_paper_id": owner.get("paper_id") if owner else None,
                "sidecar_pair": str(_paired_data_path(path)) if owner_type == "sidecar" and _paired_data_path(path) else None,
                "proposed_safe_path": str(proposed) if proposed else None,
                "repair_safety_class": safety,
            }
        )
    return {"entries": entries, "missing_db_refs": indexes["missing_refs"], "max_path": max_path, "max_filename": max_name}


def _backup_db() -> dict[str, Any]:
    dest = config.BACKUP_DIR / f"papers-{_stamp()}-PRE-PATH-GOVERNANCE.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(config.DB_PATH))
    backup = sqlite3.connect(str(dest))
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    finally:
        conn.close()
    return {"path": str(dest), "quick_check": quick, "row_count": rows, "ok": dest.exists() and quick == "ok"}


def _replace_refs(value: Any, old_name: str, new_name: str, old_rel: str, new_rel: str) -> Any:
    if isinstance(value, dict):
        return {k: _replace_refs(v, old_name, new_name, old_rel, new_rel) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_refs(v, old_name, new_name, old_rel, new_rel) for v in value]
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        if value == old_name:
            return new_name
        if normalized == old_rel:
            return new_rel
        if Path(value).name == old_name and ("/" in value or "\\" in value):
            return str(Path(value).with_name(new_name)).replace("\\", "/")
    return value


def _repair_db_owned(entry: dict[str, Any], audit_fp) -> dict[str, Any]:
    old = Path(entry["path"])
    new = Path(entry["proposed_safe_path"])
    paper_id = entry.get("db_paper_id")
    if not paper_id:
        raise RuntimeError(f"missing DB owner for {old}")
    old_hash = file_token(old)
    old_rel = f"source-pdfs/{old.name}"
    new_rel = f"source-pdfs/{new.name}"
    moved = False
    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT file_info, supplements FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not row:
            raise RuntimeError(f"missing DB row {paper_id}")
        if old.resolve() != new.resolve():
            if new.exists():
                raise FileExistsError(new)
            shutil.move(str(old), str(new))
            moved = True
        fi = _json_obj(row["file_info"])
        if entry["owner_type"] == "db_owned_source_pdf":
            fi.setdefault("original_filename", old.name)
            fi["renamed_filename"] = new.name
            fi["filepath"] = new_rel
            if "managed_pdf_filename" in fi:
                fi["managed_pdf_filename"] = new.name
            if "managed_pdf_relative_path" in fi:
                fi["managed_pdf_relative_path"] = new_rel
            if "managed_pdf_absolute_path" in fi:
                fi["managed_pdf_absolute_path"] = str(new)
        fi = _replace_refs(fi, old.name, new.name, old_rel, new_rel)
        supp = _replace_refs(_json_obj(row["supplements"]), old.name, new.name, old_rel, new_rel)
        conn.execute("UPDATE papers SET file_info = ?, supplements = ? WHERE id = ?", (json.dumps(fi, ensure_ascii=False), json.dumps(supp, ensure_ascii=False), paper_id))
        conn.commit()
    except Exception:
        conn.rollback()
        if moved and new.exists() and not old.exists():
            shutil.move(str(new), str(old))
        raise
    finally:
        conn.close()
    new_hash = file_token(new)
    if old_hash != new_hash:
        raise RuntimeError(f"hash changed during DB-owned rename: {old}")
    record = {"at": _now(), "repair_type": entry["owner_type"], "paper_id": paper_id, "old_path": str(old), "new_path": str(new), "sha256": new_hash}
    audit_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def execute_repairs(plan: dict[str, Any]) -> dict[str, Any]:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    audit_jsonl = AUDIT_DIR / f"path_renames_{_stamp()}.jsonl"
    entries = [e for e in plan["entries"] if e["violations"] and e.get("proposed_safe_path")]
    db_entries = [e for e in entries if e["repair_safety_class"] == "db_owned_source_repair"]
    staging_entries = [e for e in entries if e["repair_safety_class"] == "safe_auto_repair" and e["owner_type"] == "staging_file"]
    pending_entries = [e for e in entries if e["repair_safety_class"] == "sidecar_pair_repair" and e["owner_type"] == "pending_child_support_doc"]
    backup = _backup_db() if db_entries else None
    if backup and not backup.get("ok"):
        raise RuntimeError("DB backup failed; refusing repairs")
    counts = Counter()
    repaired_paths: set[str] = set()
    with audit_jsonl.open("a", encoding="utf-8") as audit_fp:
        for entry in db_entries:
            rec = _repair_db_owned(entry, audit_fp)
            counts["db_owned_source_repairs"] += 1
            repaired_paths.add(str(Path(rec["old_path"]).resolve()).lower())
        for label, selected in (("staging_file", staging_entries), ("pending_child", pending_entries)):
            for entry in selected:
                old = Path(entry["path"])
                if str(old.resolve()).lower() in repaired_paths or not old.exists():
                    continue
                result = safe_runtime_rename(old, Path(entry["proposed_safe_path"]))
                audit_fp.write(json.dumps({"at": _now(), "repair_type": label, **result}, ensure_ascii=False) + "\n")
                counts["staging_repairs" if label == "staging_file" else "pending_child_repairs"] += 1
                counts["sidecar_repairs"] += len(result.get("sidecars") or [])
                repaired_paths.add(str(old.resolve()).lower())
    return {"audit_jsonl": str(audit_jsonl), "db_backup": backup, **dict(counts)}


def _ownership_status() -> dict[str, Any]:
    audit = build_audit()
    missing = _missing_db_row_ids(audit)
    ok = audit.get("unowned_total", 1) == 0 and audit.get("ambiguous_total", 1) == 0 and not missing
    return {"status": "PASS" if ok else "FAIL", "unowned_total": audit.get("unowned_total"), "ambiguous_total": audit.get("ambiguous_total"), "missing_db_rows": len(missing)}


def build_report(before: dict[str, Any], after: dict[str, Any] | None, *, execute_result: dict[str, Any] | None, args: argparse.Namespace) -> dict[str, Any]:
    final_scan = after or before
    over_before = [e for e in before["entries"] if e["violations"]]
    over_after = [e for e in final_scan["entries"] if e["violations"]]
    blocking_after = [e for e in over_after if e["repair_safety_class"] not in {"report_only"}]
    report_only = [e for e in over_after if e["repair_safety_class"] == "report_only"]
    unsafe = [e for e in before["entries"] if e["violations"] and e["repair_safety_class"] == "unsafe_manual"]
    before_owner = _ownership_status()
    after_owner = before_owner if not args.execute else _ownership_status()
    summary = {
        "mode": "execute" if args.execute else "plan_only",
        "total_files_scanned": len(before["entries"]),
        "max_path_before": before["max_path"],
        "max_filename_before": before["max_filename"],
        "max_path_after": final_scan["max_path"],
        "max_filename_after": final_scan["max_filename"],
        "files_over_policy_before": len(over_before),
        "files_over_policy_after": len(over_after),
        "blocking_files_over_policy_after": len(blocking_after),
        "db_owned_source_repairs_count": int((execute_result or {}).get("db_owned_source_repairs") or 0),
        "staging_repairs_count": int((execute_result or {}).get("staging_repairs") or 0),
        "pending_child_repairs_count": int((execute_result or {}).get("pending_child_repairs") or 0),
        "sidecar_repairs_count": int((execute_result or {}).get("sidecar_repairs") or 0),
        "logs_backups_report_only_count": sum(1 for e in report_only if e["owner_type"] in {"log", "backup"}),
        "unsafe_manual_count": len(unsafe),
        "missing_db_referenced_paths_count": len(final_scan.get("missing_db_refs") or []),
        "source_ownership_before": before_owner,
        "source_ownership_after": after_owner,
    }
    summary["final_portability"] = "PASS" if (
        summary["blocking_files_over_policy_after"] == 0
        and summary["unsafe_manual_count"] == 0
        and summary["missing_db_referenced_paths_count"] == 0
        and after_owner["status"] == "PASS"
    ) else "FAIL"
    return {
        "created_at": _now(),
        "policy": {"MAX_FULL_PATH": args.max_full_path, "MAX_FILENAME": args.max_filename, "MAX_STEM": MAX_STEM},
        "summary": summary,
        "execute_result": execute_result or {},
        "unsafe_manual": unsafe[:100],
        "report_only_violations": report_only[:100],
        "missing_db_refs": final_scan.get("missing_db_refs") or [],
        "entries": final_scan["entries"],
    }


def _write_report(report: dict[str, Any]) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_DIR / f"path_portability_report_{_stamp()}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit and repair Windows-safe corpus paths")
    parser.add_argument("--plan-only", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    parser.add_argument("--max-full-path", type=int, default=MAX_FULL_PATH)
    parser.add_argument("--max-filename", type=int, default=MAX_FILENAME)
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--include-logs", action="store_true", help="Compatibility flag; logs are always scanned report-only")
    parser.add_argument("--include-backups", action="store_true", help="Compatibility flag; backups are always scanned report-only")
    parser.add_argument("--paper-id", default="")
    args = parser.parse_args(argv)
    config.ensure_dirs()
    before = scan_paths(Path(args.root).resolve(), max_full_path=args.max_full_path, max_filename=args.max_filename, paper_id=args.paper_id or None)
    execute_result = None
    after = None
    if args.execute:
        unsafe = [e for e in before["entries"] if e["violations"] and e["repair_safety_class"] == "unsafe_manual"]
        if unsafe:
            report = build_report(before, None, execute_result=None, args=args)
            path = _write_report(report) if args.write_report else None
            print(json.dumps({"ok": False, "status": "refused_unsafe_manual", "unsafe_manual_count": len(unsafe), "report_path": str(path) if path else None}, indent=2))
            return 2
        execute_result = execute_repairs(before)
        after = scan_paths(Path(args.root).resolve(), max_full_path=args.max_full_path, max_filename=args.max_filename, paper_id=args.paper_id or None)
    report = build_report(before, after, execute_result=execute_result, args=args)
    path = _write_report(report) if args.write_report or args.execute else None
    print(json.dumps({"ok": report["summary"]["final_portability"] == "PASS", "summary": report["summary"], "report_path": str(path) if path else None}, indent=2))
    if not args.execute:
        return 0
    return 0 if report["summary"]["final_portability"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
