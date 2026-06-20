"""Broad source-PDF ownership audit and safe normalization.

This fixes the earlier path-only ownership mistake. A source PDF is considered
owned when it has reliable evidence from canonical DB paths, exact filename
fields, or exact stored hashes. ID-derived filenames are reported only as weak
evidence and are never enough for destructive cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import papers_db  # noqa: E402
from path_governance import safe_destination_for_file  # noqa: E402

CANONICAL_PATH_FIELDS = {"filepath", "managed_pdf_relative_path", "managed_pdf_absolute_path"}
FILENAME_FIELDS = {"renamed_filename", "original_filename", "managed_pdf_filename", "filename"}
HASH_FIELDS = {"sha256", "managed_pdf_sha256", "source_sha256", "staged_sha256"}
LINK_KEY_RX = re.compile(r"(file|path|pdf|filename|renamed|source|supplement|sha|hash)", re.I)
PDF_EXT_RX = re.compile(r"\.pdf$", re.I)
HEX_HASH_RX = re.compile(r"^[a-f0-9]{32,128}$", re.I)


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


def _db_rows() -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(f"SELECT {','.join(papers_db.ALL_COLUMNS)} FROM papers ORDER BY id").fetchall()
    finally:
        conn.close()


def _source_pdfs() -> list[Path]:
    if not config.SOURCE_PDFS.exists():
        return []
    return sorted(config.SOURCE_PDFS.rglob("*.pdf"), key=lambda path: str(path).lower())


def _rel_to_master(path: Path) -> str | None:
    try:
        return str(path.resolve().relative_to(config.MASTER_PROJECT.resolve())).replace("\\", "/")
    except ValueError:
        return None


def _source_path_from_value(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    raw = Path(str(value))
    return raw if raw.is_absolute() else config.MASTER_PROJECT / raw


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _id_filename(paper_id: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "_", str(paper_id).lower()).strip("_")
    return f"{stem}.pdf" if stem else ""


def _iter_pdf_like_values(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if "source_pdf_reconciliation" in prefix or prefix.endswith("source_pdf_reconciled_at"):
        return found
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            found.extend(_iter_pdf_like_values(child, child_prefix))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            found.extend(_iter_pdf_like_values(child, f"{prefix}[{idx}]"))
    elif isinstance(value, str):
        key = prefix.split(".")[-1]
        if key in {"reason", "basename", "source_pdf_reconciled_at"}:
            return found
        if LINK_KEY_RX.search(prefix) and (".pdf" in value.lower() or "/" in value or "\\" in value or HEX_HASH_RX.match(value.strip())):
            found.append((key, value))
    return found


def _build_refs(rows: list[sqlite3.Row]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    refs: list[dict[str, Any]] = []
    field_counts: Counter[str] = Counter()
    for row in rows:
        paper_id = row["id"]
        file_info = _json_obj(row["file_info"])
        for field, value in _iter_pdf_like_values(file_info):
            if value in (None, ""):
                continue
            value_s = str(value)
            basename = Path(value_s).name.lower()
            path = _source_path_from_value(value_s)
            rel = _rel_to_master(path) if path else None
            is_hash = field in HASH_FIELDS or bool(HEX_HASH_RX.match(value_s.strip()))
            if not (PDF_EXT_RX.search(basename) or rel or is_hash):
                continue
            field_counts[field] += 1
            refs.append(
                {
                    "id": paper_id,
                    "field": field,
                    "value": value_s,
                    "basename": basename,
                    "resolved_rel": rel,
                    "is_canonical_path_field": field in CANONICAL_PATH_FIELDS,
                    "is_filename_field": field in FILENAME_FIELDS,
                    "is_hash_field": is_hash,
                    "is_weak": False,
                }
            )
        supplements = _json_obj(row["supplements"])
        for field, value in _iter_pdf_like_values(supplements, prefix="supplements"):
            if value in (None, ""):
                continue
            value_s = str(value)
            basename = Path(value_s).name.lower()
            path = _source_path_from_value(value_s)
            if path and not path.exists() and PDF_EXT_RX.search(basename):
                path = config.SOURCE_PDFS / Path(value_s).name
            rel = _rel_to_master(path) if path else None
            is_hash = field in HASH_FIELDS or bool(HEX_HASH_RX.match(value_s.strip()))
            if not (PDF_EXT_RX.search(basename) or rel or is_hash):
                continue
            field_key = f"supplements.{field}" if not field.startswith("supplements.") else field
            field_counts[field_key] += 1
            refs.append(
                {
                    "id": paper_id,
                    "field": field_key,
                    "value": value_s,
                    "basename": basename,
                    "resolved_rel": rel,
                    "is_canonical_path_field": False,
                    "is_filename_field": field in FILENAME_FIELDS or "filename" in field_key,
                    "is_hash_field": is_hash,
                    "is_weak": False,
                }
            )
        weak = _id_filename(str(paper_id))
        if weak:
            refs.append(
                {
                    "id": paper_id,
                    "field": "id_derived_filename",
                    "value": weak,
                    "basename": weak,
                    "resolved_rel": None,
                    "is_canonical_path_field": False,
                    "is_filename_field": False,
                    "is_hash_field": False,
                    "is_weak": True,
                }
            )
    return refs, dict(field_counts)


def _source_hashes_if_needed(source_paths: list[Path], refs: list[dict[str, Any]]) -> dict[str, list[Path]]:
    hash_values = {str(ref["value"]).lower() for ref in refs if ref.get("is_hash_field")}
    if not hash_values:
        return {}
    out: dict[str, list[Path]] = defaultdict(list)
    for path in source_paths:
        digest = _sha256(path).lower()
        if digest in hash_values:
            out[digest].append(path)
    return out


def _resolve_child_document_owner_ids(path: Path, strong_matches: list[dict[str, Any]]) -> set[str]:
    """When multiple rows claim the same child PDF, pick one canonical parent."""
    row_ids = {m["id"] for m in strong_matches}
    if len(row_ids) <= 1:
        return row_ids
    basename = path.name.lower()
    if not any(tok in basename for tok in ("supplement", "supp", "appendix", "corrig", "erratum", "addendum", "retraction")):
        return row_ids

    def _score(paper_id: str) -> int:
        pid = paper_id.lower()
        score = 0
        if basename.startswith(pid.replace("_", "_")):
            score += 10
        stem = pid.split("_")[0]
        if stem and stem in basename:
            score += 3
        for m in strong_matches:
            if m["id"] != paper_id:
                continue
            field = str(m.get("field") or "")
            if field.endswith(".filename") and not field.endswith(".original_filename"):
                score += 5
            if "items[" in field and ".filename" in field:
                score += 4
        return score

    ranked = sorted(row_ids, key=lambda pid: (_score(pid), pid), reverse=True)
    best = ranked[0]
    if len(ranked) > 1 and _score(best) > _score(ranked[1]):
        return {best}
    return row_ids


def build_audit() -> dict[str, Any]:
    rows = _db_rows()
    source_paths = _source_pdfs()
    refs, field_counts = _build_refs(rows)
    source_by_rel = {(_rel_to_master(path) or "").lower(): path for path in source_paths}
    source_by_name: dict[str, list[Path]] = defaultdict(list)
    for path in source_paths:
        source_by_name[path.name.lower()].append(path)
    source_hashes = _source_hashes_if_needed(source_paths, refs)

    source_matches: dict[str, list[dict[str, Any]]] = {(_rel_to_master(path) or "").lower(): [] for path in source_paths}
    for ref in refs:
        if ref["is_hash_field"]:
            for path in source_hashes.get(str(ref["value"]).lower(), []):
                source_matches[(_rel_to_master(path) or "").lower()].append({**ref, "match_type": "hash_exact"})
            continue
        rel = (ref.get("resolved_rel") or "").lower()
        if rel in source_by_rel:
            match_type = "canonical_path" if ref.get("is_canonical_path_field") else "path_exact"
            source_matches[rel].append({**ref, "match_type": match_type})
        basename = str(ref.get("basename") or "").lower()
        if PDF_EXT_RX.search(basename):
            for path in source_by_name.get(basename, []):
                source_matches[(_rel_to_master(path) or "").lower()].append({**ref, "match_type": "weak_id_filename" if ref.get("is_weak") else "filename_exact"})

    source_class_counts: Counter[str] = Counter()
    source_examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unowned: list[str] = []
    ambiguous: list[str] = []
    for path in source_paths:
        rel = (_rel_to_master(path) or "").lower()
        matches = source_matches.get(rel, [])
        strong = [m for m in matches if not m.get("is_weak")]
        row_ids = {m["id"] for m in strong}
        if len(row_ids) > 1:
            row_ids = _resolve_child_document_owner_ids(path, strong)
            strong = [m for m in strong if m["id"] in row_ids]
        if any(m["match_type"] == "canonical_path" for m in strong):
            cls = "owned_canonical_path"
        elif any(m["match_type"] == "hash_exact" for m in strong):
            cls = "owned_hash_exact"
        elif len(row_ids) == 1 and strong:
            cls = "owned_filename_exact"
        elif len(row_ids) > 1:
            cls = "ambiguous_multi_match"
            ambiguous.append(str(path.relative_to(config.MASTER_PROJECT)).replace("\\", "/"))
        elif matches:
            cls = "unresolved_insufficient_evidence"
            unowned.append(str(path.relative_to(config.MASTER_PROJECT)).replace("\\", "/"))
        else:
            cls = "unowned_no_match"
            unowned.append(str(path.relative_to(config.MASTER_PROJECT)).replace("\\", "/"))
        source_class_counts[cls] += 1
        if len(source_examples[cls]) < 10:
            source_examples[cls].append(
                {
                    "source_pdf": str(path.relative_to(config.MASTER_PROJECT)).replace("\\", "/"),
                    "matches": [{k: m[k] for k in ("id", "field", "value", "match_type") if k in m} for m in matches[:8]],
                }
            )

    # DB row source state and normalization candidates.
    reliable_by_row: dict[str, set[str]] = defaultdict(set)
    match_fields_by_row: dict[tuple[str, str], list[str]] = defaultdict(list)
    for rel, matches in source_matches.items():
        strong = [m for m in matches if not m.get("is_weak")]
        ids = {m["id"] for m in strong}
        if len(ids) == 1:
            paper_id = next(iter(ids))
            reliable_by_row[paper_id].add(rel)
            for m in strong:
                match_fields_by_row[(paper_id, rel)].append(str(m["field"]))

    normalize: list[dict[str, Any]] = []
    db_source_counts: Counter[str] = Counter()
    db_examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        file_info = _json_obj(row["file_info"])
        current = file_info.get("filepath") or file_info.get("managed_pdf_relative_path") or file_info.get("managed_pdf_absolute_path")
        current_path = _source_path_from_value(current)
        current_rel = (_rel_to_master(current_path) or "").lower() if current_path else ""
        if current_rel and current_rel in source_by_rel:
            db_source_counts["db_has_existing_canonical_source"] += 1
            continue
        candidate_rels = sorted(reliable_by_row.get(row["id"], set()))
        if len(candidate_rels) == 1:
            rel = candidate_rels[0]
            normalize.append(
                {
                    "id": row["id"],
                    "current_filepath": current,
                    "candidate_filepath": _rel_to_master(source_by_rel[rel]),
                    "matched_fields": sorted(set(match_fields_by_row[(row["id"], rel)])),
                }
            )
            db_source_counts["db_safe_normalize_to_source"] += 1
        elif len(candidate_rels) > 1:
            db_source_counts["db_ambiguous_multiple_source_matches"] += 1
        else:
            db_source_counts["db_no_source_pdf_match"] += 1
        category = next(reversed(db_source_counts))
        if len(db_examples[category]) < 10:
            db_examples[category].append({"id": row["id"], "current_filepath": current})

    return {
        "read_only": True,
        "created_at": _now(),
        "source_pdfs_total": len(source_paths),
        "db_rows_total": len(rows),
        "db_rows_with_file_info": sum(1 for row in rows if _json_obj(row["file_info"])),
        "field_counts": field_counts,
        "ownership_counts": dict(source_class_counts),
        "owned_total": sum(source_class_counts[key] for key in ("owned_canonical_path", "owned_filename_exact", "owned_hash_exact")),
        "unowned_total": len(unowned),
        "ambiguous_total": len(ambiguous),
        "examples": dict(source_examples),
        "db_source_counts": dict(db_source_counts),
        "db_source_examples": dict(db_examples),
        "safe_normalization_count": len(normalize),
        "safe_normalizations": normalize,
        "unowned_source_pdfs": unowned,
        "ambiguous_source_pdfs": ambiguous,
        "side_effects": {"db_writes": "none", "pdf_moves": "none", "pdf_deletes": "none", "api_calls": "none", "evaluation": "none"},
    }


def _create_backup(label: str) -> dict[str, Any]:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.BACKUP_DIR / f"papers-{datetime.now():%Y%m%d-%H%M%S}-{label}.db"
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
    return {"ok": dest.exists() and size > 1024 * 1024 and quick_check == "ok", "path": str(dest), "bytes": size, "quick_check": quick_check, "row_count": row_count}


def _db_digest(conn: sqlite3.Connection, target_ids: set[str]) -> dict[str, str]:
    conn.row_factory = sqlite3.Row
    h_non_file_info = hashlib.sha256()
    h_non_target_file_info = hashlib.sha256()
    cols_without_file_info = [col for col in papers_db.ALL_COLUMNS if col != "file_info"]
    for row in conn.execute(f"SELECT {','.join(papers_db.ALL_COLUMNS)} FROM papers ORDER BY id"):
        encoded = json.dumps([row[col] for col in cols_without_file_info], ensure_ascii=False, default=str).encode("utf-8")
        h_non_file_info.update(len(encoded).to_bytes(8, "big"))
        h_non_file_info.update(encoded)
        if row["id"] not in target_ids:
            encoded_info = json.dumps([row["id"], row["file_info"]], ensure_ascii=False, default=str).encode("utf-8")
            h_non_target_file_info.update(len(encoded_info).to_bytes(8, "big"))
            h_non_target_file_info.update(encoded_info)
    return {"non_file_info_digest": h_non_file_info.hexdigest(), "non_target_file_info_digest": h_non_target_file_info.hexdigest()}


def normalize_file_info(audit: dict[str, Any]) -> dict[str, Any]:
    fixes = audit.get("safe_normalizations") or []
    if not fixes:
        return {"ok": True, "status": "no_safe_normalizations", "backup": None, "applied_count": 0}
    backup = _create_backup("PRE-SOURCE-PDF-OWNERSHIP-NORMALIZE")
    if not backup.get("ok"):
        return {"ok": False, "status": "refused_backup_failed", "backup": backup}
    target_ids = {fix["id"] for fix in fixes}
    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        before = _db_digest(conn, target_ids)
        before_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        for fix in fixes:
            row = conn.execute("SELECT id, file_info FROM papers WHERE id = ?", (fix["id"],)).fetchone()
            if not row:
                raise RuntimeError(f"missing target row {fix['id']}")
            file_info = _json_obj(row["file_info"])
            file_info["filepath"] = fix["candidate_filepath"]
            conn.execute("UPDATE papers SET file_info = ? WHERE id = ?", (json.dumps(file_info, ensure_ascii=False), fix["id"]))
        conn.commit()
        after = _db_digest(conn, target_ids)
        after_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        conn.close()
    failures = []
    if before_count != after_count:
        failures.append("row count changed")
    if before["non_file_info_digest"] != after["non_file_info_digest"]:
        failures.append("non-file_info columns changed")
    if before["non_target_file_info_digest"] != after["non_target_file_info_digest"]:
        failures.append("non-target file_info changed")
    if quick_check != "ok":
        failures.append(f"quick_check={quick_check}")
    return {
        "ok": not failures,
        "status": "completed" if not failures else "post_write_validation_failed",
        "backup": backup,
        "applied_count": len(fixes),
        "applied_ids_sample": sorted(target_ids)[:30],
        "db_validation": {
            "before_count": before_count,
            "after_count": after_count,
            "row_count_unchanged": before_count == after_count,
            "non_file_info_columns_unchanged": before["non_file_info_digest"] == after["non_file_info_digest"],
            "non_target_file_info_unchanged": before["non_target_file_info_digest"] == after["non_target_file_info_digest"],
            "quick_check": quick_check,
            "failures": failures,
        },
    }


def _deleted_rows_audit_path() -> Path:
    out_dir = config.MASTER_PROJECT / "deleted-row-audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"deleted_missing_pdf_rows_{datetime.now():%Y%m%d-%H%M%S}.jsonl"


def _deserialize_row(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in papers_db.ALL_COLUMNS:
        value = row[col]
        out[col] = _json_obj(value) if col in papers_db.JSON_FIELDS else value
    return out


def _missing_db_row_ids(audit: dict[str, Any]) -> list[str]:
    # Rebuild from the current DB so this cannot delete stale candidates from an old audit.
    rows = _db_rows()
    source_paths = _source_pdfs()
    refs, _ = _build_refs(rows)
    source_by_rel = {(_rel_to_master(path) or "").lower(): path for path in source_paths}
    source_by_name: dict[str, list[Path]] = defaultdict(list)
    for path in source_paths:
        source_by_name[path.name.lower()].append(path)
    source_hashes = _source_hashes_if_needed(source_paths, refs)
    reliable_by_row: dict[str, set[str]] = defaultdict(set)
    ambiguous_by_row: set[str] = set()
    source_matches: dict[str, list[dict[str, Any]]] = {(_rel_to_master(path) or "").lower(): [] for path in source_paths}
    for ref in refs:
        if ref["is_hash_field"]:
            for path in source_hashes.get(str(ref["value"]).lower(), []):
                source_matches[(_rel_to_master(path) or "").lower()].append({**ref, "match_type": "hash_exact"})
            continue
        rel = (ref.get("resolved_rel") or "").lower()
        if rel in source_by_rel:
            source_matches[rel].append({**ref, "match_type": "canonical_path" if ref.get("is_canonical_path_field") else "path_exact"})
        basename = str(ref.get("basename") or "").lower()
        if PDF_EXT_RX.search(basename):
            for path in source_by_name.get(basename, []):
                source_matches[(_rel_to_master(path) or "").lower()].append({**ref, "match_type": "weak_id_filename" if ref.get("is_weak") else "filename_exact"})
    for rel, matches in source_matches.items():
        strong = [m for m in matches if not m.get("is_weak")]
        ids = {m["id"] for m in strong}
        if len(ids) == 1:
            reliable_by_row[next(iter(ids))].add(rel)
        elif len(ids) > 1:
            ambiguous_by_row.update(ids)
    missing = []
    for row in rows:
        if row["id"] in reliable_by_row:
            continue
        if row["id"] in ambiguous_by_row:
            continue
        missing.append(row["id"])
    return sorted(missing)


def delete_missing_db_rows(audit: dict[str, Any]) -> dict[str, Any]:
    ids = _missing_db_row_ids(audit)
    if not ids:
        return {"ok": True, "status": "no_missing_db_rows", "backup": None, "deleted_count": 0, "audit_export_path": None}
    backup = _create_backup("PRE-DELETE-MISSING-PDF-ROWS")
    if not backup.get("ok"):
        return {"ok": False, "status": "refused_backup_failed", "backup": backup}
    audit_path = _deleted_rows_audit_path()
    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        before_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        rows = conn.execute(
            f"SELECT {','.join(papers_db.ALL_COLUMNS)} FROM papers WHERE id IN ({','.join(['?'] * len(ids))}) ORDER BY id",
            tuple(ids),
        ).fetchall()
        if len(rows) != len(ids):
            raise RuntimeError("missing-row delete target changed before export")
        with audit_path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(
                    json.dumps(
                        {
                            "deleted_at": _now(),
                            "delete_reason": "no reliable source-pdfs ownership match after corrected broad audit",
                            "row": _deserialize_row(row),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        conn.executemany("DELETE FROM papers WHERE id = ?", [(paper_id,) for paper_id in ids])
        conn.commit()
        after_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        conn.close()
    failures = []
    if before_count - after_count != len(ids):
        failures.append("row count delta mismatch")
    if quick_check != "ok":
        failures.append(f"quick_check={quick_check}")
    line_count = sum(1 for _ in audit_path.open("r", encoding="utf-8")) if audit_path.exists() else 0
    if line_count != len(ids):
        failures.append("deleted-row audit line count mismatch")
    return {
        "ok": not failures,
        "status": "completed" if not failures else "post_delete_validation_failed",
        "backup": backup,
        "deleted_count": len(ids),
        "deleted_ids_sample": ids[:50],
        "audit_export_path": str(audit_path),
        "db_validation": {
            "before_count": before_count,
            "after_count": after_count,
            "expected_after_count": before_count - len(ids),
            "row_count_delta_ok": before_count - after_count == len(ids),
            "quick_check": quick_check,
            "audit_line_count": line_count,
            "failures": failures,
        },
    }


def move_unowned_to_staging(audit: dict[str, Any], *, max_count: int) -> dict[str, Any]:
    unowned = audit.get("unowned_source_pdfs") or []
    if not unowned:
        return {"ok": True, "status": "no_unowned_source_pdfs", "moved_count": 0, "manifest_path": None}
    if len(unowned) > max_count:
        return {"ok": False, "status": "refused_unowned_count_exceeds_limit", "unowned_count": len(unowned), "max_count": max_count}
    manifest_dir = config.RUNS_DIR / "source-pdf-unowned-moves"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"unowned_source_pdf_moves_{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    moved = []
    skipped = []
    for rel in unowned:
        src = config.MASTER_PROJECT / rel
        if not src.exists():
            skipped.append({"source": rel, "reason": "source_missing_before_move"})
            continue
        dest = safe_destination_for_file(src, config.STAGING, reason="unowned_source_to_staging")
        shutil.move(str(src), str(dest))
        moved.append({"source": str(src), "destination": str(dest)})
    with manifest_path.open("w", encoding="utf-8") as f:
        for item in moved:
            f.write(json.dumps({"moved_at": _now(), **item}, ensure_ascii=False) + "\n")
        for item in skipped:
            f.write(json.dumps({"checked_at": _now(), **item}, ensure_ascii=False) + "\n")
    return {"ok": True, "status": "completed", "moved_count": len(moved), "skipped_count": len(skipped), "manifest_path": str(manifest_path), "skipped_sample": skipped[:20]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Broad source-PDF ownership audit and safe normalization")
    parser.add_argument("--normalize-file-info", action="store_true", help="Apply safe canonical filepath fixes")
    parser.add_argument("--delete-missing-db-rows", action="store_true", help="Delete DB rows with no reliable source-pdfs match")
    parser.add_argument("--move-unowned-to-staging", action="store_true", help="Move genuinely unowned source PDFs to staging")
    parser.add_argument("--allow-db-write", action="store_true")
    parser.add_argument("--allow-pdf-move", action="store_true")
    parser.add_argument("--max-move-count", type=int, default=200)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    audit = build_audit()
    result: dict[str, Any] = {"ok": True, "audit": audit}
    if args.normalize_file_info:
        if not args.allow_db_write:
            result = {"ok": False, "status": "refused_missing_allow_db_write", "audit": audit}
        else:
            norm = normalize_file_info(audit)
            result["normalize"] = norm
            result["ok"] = bool(norm.get("ok"))
    if args.delete_missing_db_rows and result.get("ok"):
        if not args.allow_db_write:
            result = {"ok": False, "status": "refused_missing_allow_db_write", "audit": audit}
        else:
            delete = delete_missing_db_rows(audit)
            result["delete_missing_db_rows"] = delete
            result["ok"] = bool(delete.get("ok"))
    if args.move_unowned_to_staging and result.get("ok"):
        if not args.allow_pdf_move:
            result = {"ok": False, "status": "refused_missing_allow_pdf_move", "audit": audit}
        else:
            move = move_unowned_to_staging(audit, max_count=args.max_move_count)
            result["move_unowned"] = move
            result["ok"] = bool(move.get("ok"))
    if args.summary_only:
        delete_preview_count = len(_missing_db_row_ids(audit)) if (args.delete_missing_db_rows or not args.normalize_file_info and not args.move_unowned_to_staging) else None
        compact = {
            "ok": result.get("ok"),
            "status": result.get("status"),
            "source_pdfs_total": audit["source_pdfs_total"],
            "db_rows_total": audit["db_rows_total"],
            "owned_total": audit["owned_total"],
            "unowned_total": audit["unowned_total"],
            "ambiguous_total": audit["ambiguous_total"],
            "ownership_counts": audit["ownership_counts"],
            "db_source_counts": audit["db_source_counts"],
            "safe_normalization_count": audit["safe_normalization_count"],
            "delete_missing_db_rows_preview_count": delete_preview_count,
            "field_counts": audit["field_counts"],
            "normalize": result.get("normalize"),
            "delete_missing_db_rows": result.get("delete_missing_db_rows"),
            "move_unowned": result.get("move_unowned"),
            "examples": {key: value[:3] for key, value in audit["examples"].items()},
        }
        print(json.dumps(compact, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
