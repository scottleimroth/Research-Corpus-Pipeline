"""Safely reconcile missing DB source-PDF paths to existing source-pdfs files.

Default mode is a read-only audit. Live mode is deliberately narrow and
requires `--execute --allow-db-write`; it creates a SQLite backup first and
validates that only target rows' `file_info` cells changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import papers_db  # noqa: E402

MIN_TITLE_TOKENS = 4
MIN_TITLE_COVERAGE = 0.82

STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


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


def _text_len(raw: Any) -> int:
    obj = _json_obj(raw)
    if obj:
        return len(str(obj.get("text") or obj.get("full_text") or obj.get("content") or ""))
    return len(str(raw or ""))


def _full_text_materialized(raw: Any) -> bool:
    obj = _json_obj(raw)
    return bool(obj.get("extraction")) or _text_len(raw) > 0


def _source_for_rel(rel: str | None) -> Path | None:
    if not rel:
        return None
    raw = Path(rel)
    return raw if raw.is_absolute() else config.MASTER_PROJECT / raw


def _source_pdf_index() -> dict[str, list[Path]]:
    by_name: dict[str, list[Path]] = {}
    if not config.SOURCE_PDFS.exists():
        return by_name
    for path in config.SOURCE_PDFS.glob("*.pdf"):
        by_name.setdefault(path.name.lower(), []).append(path)
    return by_name


def _source_path_refs(rows: list[sqlite3.Row]) -> Counter[str]:
    refs: Counter[str] = Counter()
    for row in rows:
        file_info = _json_obj(row["file_info"])
        rel = file_info.get("filepath") or file_info.get("managed_pdf_relative_path")
        source = _source_for_rel(rel)
        if not source or not source.exists():
            continue
        try:
            normalized = str(source.resolve().relative_to(config.MASTER_PROJECT.resolve())).lower()
        except ValueError:
            continue
        refs[normalized] += 1
    return refs


def _path_rel(path: Path) -> str:
    return str(path.resolve().relative_to(config.MASTER_PROJECT.resolve())).replace("\\", "/")


def _title_tokens(title: Any) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", str(title or "").lower())
    return [token for token in tokens if len(token) > 2 and token not in STOPWORDS]


def _pdf_text_probe(path: Path) -> tuple[str, str]:
    try:
        proc = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "4", str(path), "-"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return "", "pdftotext_not_found"
    except subprocess.TimeoutExpired:
        return "", "pdftotext_timeout"
    if proc.returncode != 0:
        return proc.stdout or "", (proc.stderr or f"pdftotext_exit_{proc.returncode}").strip()
    return proc.stdout or "", ""


def _title_confirmed(title: Any, text: str) -> tuple[bool, dict[str, Any]]:
    tokens = _title_tokens(title)
    if len(tokens) < MIN_TITLE_TOKENS:
        return False, {"reason": "title_too_short_or_generic", "title_token_count": len(tokens)}
    text_norm = " ".join(re.findall(r"[a-z0-9]+", text.lower()))
    hits = sum(1 for token in tokens if token in text_norm)
    coverage = hits / len(tokens) if tokens else 0
    return coverage >= MIN_TITLE_COVERAGE, {
        "title_token_count": len(tokens),
        "title_token_hits": hits,
        "title_token_coverage": round(coverage, 3),
    }


def _year_confirmed(year: Any, text: str) -> bool:
    year_s = str(year or "").strip()
    return bool(re.fullmatch(r"\d{4}", year_s) and year_s in text[:12000])


def _author_tokens(authors: Any) -> list[str]:
    if isinstance(authors, str):
        authors = re.split(r"[;,]", authors)
    if not isinstance(authors, list):
        return []
    out: list[str] = []
    for author in authors:
        parts = re.findall(r"[a-z0-9]+", str(author).lower())
        if parts:
            out.append(parts[-1])
    return [token for token in out if len(token) > 2 and token not in STOPWORDS]


def _basename_tokens(path: Path) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", path.stem.lower()) if len(token) > 2 and token not in STOPWORDS}


def _candidate_names(file_info: dict[str, Any]) -> list[tuple[str, str]]:
    names: list[tuple[str, str]] = []
    for field in ("filepath", "managed_pdf_relative_path", "renamed_filename", "original_filename", "filename"):
        value = file_info.get(field)
        if value:
            names.append((field, Path(str(value)).name))
    return names


def _deep_basename_candidates(
    *,
    source_paths: list[Path],
    referenced_source_paths: Counter[str],
    basic_metadata: dict[str, Any],
) -> tuple[list[Path], str | None]:
    title_tokens = _title_tokens(basic_metadata.get("title"))
    author_tokens = _author_tokens(basic_metadata.get("authors"))
    year = str(basic_metadata.get("year") or "").strip()
    if len(title_tokens) < MIN_TITLE_TOKENS or not re.fullmatch(r"\d{4}", year):
        return [], "insufficient_title_or_year_for_deep_search"

    scored: list[tuple[int, Path]] = []
    for path in source_paths:
        rel = _path_rel(path).lower()
        if referenced_source_paths[rel] > 0:
            continue
        tokens = _basename_tokens(path)
        title_hits = sum(1 for token in title_tokens if token in tokens)
        author_hit = int(any(token in tokens for token in author_tokens))
        year_hit = int(year in tokens or year in path.stem)
        score = title_hits + author_hit + year_hit
        if score >= 4 and title_hits >= 2 and (author_hit or year_hit):
            scored.append((score, path))

    if len(scored) > 12:
        return [], "too_many_deep_basename_candidates"
    scored.sort(key=lambda item: (-item[0], item[1].name.lower()))
    return [path for _, path in scored], None


def _unresolved_category(row: sqlite3.Row) -> str | None:
    file_info = _json_obj(row["file_info"])
    rel = file_info.get("filepath") or file_info.get("managed_pdf_relative_path")
    source = _source_for_rel(rel)
    if not file_info:
        return "file_info_missing"
    if not rel:
        return "db_path_missing"
    if source and not source.exists():
        return "path_points_to_absent_file"
    return None


def _db_digest(conn: sqlite3.Connection, *, target_ids: set[str]) -> dict[str, str]:
    conn.row_factory = sqlite3.Row
    h_non_file_info = hashlib.sha256()
    h_non_target_file_info = hashlib.sha256()
    cols_without_file_info = [c for c in papers_db.ALL_COLUMNS if c != "file_info"]
    for row in conn.execute(f"SELECT {','.join(papers_db.ALL_COLUMNS)} FROM papers ORDER BY id"):
        vals = [row[col] for col in cols_without_file_info]
        encoded = json.dumps(vals, ensure_ascii=False, default=str).encode("utf-8")
        h_non_file_info.update(len(encoded).to_bytes(8, "big"))
        h_non_file_info.update(encoded)
        if row["id"] not in target_ids:
            encoded_file_info = json.dumps([row["id"], row["file_info"]], ensure_ascii=False, default=str).encode("utf-8")
            h_non_target_file_info.update(len(encoded_file_info).to_bytes(8, "big"))
            h_non_target_file_info.update(encoded_file_info)
    return {
        "non_file_info_digest": h_non_file_info.hexdigest(),
        "non_target_file_info_digest": h_non_target_file_info.hexdigest(),
    }


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
    dest = config.BACKUP_DIR / f"papers-{datetime.now():%Y%m%d-%H%M%S}-PRE-SOURCE-PDF-RECONCILE.db"
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


def build_plan() -> dict[str, Any]:
    by_name = _source_pdf_index()
    source_paths = sorted([path for paths in by_name.values() for path in paths], key=lambda path: path.name.lower())
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, basic_metadata, file_info, full_text FROM papers ORDER BY id").fetchall()
    finally:
        conn.close()

    categories: Counter[str] = Counter()
    candidate_rejections: Counter[str] = Counter()
    referenced_source_paths = _source_path_refs(rows)
    proposed: list[dict[str, Any]] = []
    unresolved_rows = [row for row in rows if not _full_text_materialized(row["full_text"]) and _unresolved_category(row)]

    for row in unresolved_rows:
        category = _unresolved_category(row) or "resolved"
        categories[category] += 1
        file_info = _json_obj(row["file_info"])
        basic_metadata = _json_obj(row["basic_metadata"])
        raw_matches: list[tuple[str, str, Path]] = []
        for field, name in _candidate_names(file_info):
            matches = by_name.get(name.lower(), [])
            if len(matches) == 1:
                raw_matches.append((field, name, matches[0]))

        if not raw_matches:
            deep_candidates, deep_rejection = _deep_basename_candidates(
                source_paths=source_paths,
                referenced_source_paths=referenced_source_paths,
                basic_metadata=basic_metadata,
            )
            confirmed: list[tuple[Path, dict[str, Any]]] = []
            for deep_candidate in deep_candidates:
                text, probe_error = _pdf_text_probe(deep_candidate)
                if probe_error:
                    continue
                title_ok, title_evidence = _title_confirmed(basic_metadata.get("title"), text)
                if title_ok and _year_confirmed(basic_metadata.get("year"), text):
                    confirmed.append((deep_candidate, title_evidence))
            if len(confirmed) != 1:
                candidate_rejections[deep_rejection or "no_exact_unique_basename_match"] += 1
                if len(confirmed) > 1:
                    candidate_rejections["ambiguous_deep_title_year_matches"] += 1
                continue
            candidate, title_evidence = confirmed[0]
            rel_candidate = _path_rel(candidate)
            proposed.append(
                {
                    "id": row["id"],
                    "category": category,
                    "current_filepath": file_info.get("filepath") or file_info.get("managed_pdf_relative_path"),
                    "candidate_filepath": rel_candidate,
                    "matched_fields": [{"field": "deep_basename_title_year", "basename": candidate.name}],
                    "evidence": {
                        "reason": "unreferenced source-pdfs basename candidate plus title/year confirmed in first 4 pages",
                        **title_evidence,
                        "year_confirmed": True,
                    },
                }
            )
            continue

        targets = {match[2] for match in raw_matches}
        if len(targets) != 1:
            candidate_rejections["conflicting_exact_basename_matches"] += 1
            continue

        candidate = next(iter(targets))
        rel_candidate = _path_rel(candidate)
        normalized_rel = rel_candidate.lower()
        if referenced_source_paths[normalized_rel] > 0:
            candidate_rejections["candidate_already_referenced"] += 1
            continue

        text, probe_error = _pdf_text_probe(candidate)
        if probe_error:
            candidate_rejections["pdf_text_probe_failed"] += 1
            continue
        title_ok, title_evidence = _title_confirmed(basic_metadata.get("title"), text)
        if not title_ok:
            candidate_rejections["title_not_confirmed_in_pdf_probe"] += 1
            continue
        year_ok = _year_confirmed(basic_metadata.get("year"), text)
        if not year_ok:
            candidate_rejections["year_not_confirmed_in_pdf_probe"] += 1
            continue

        proposed.append(
            {
                "id": row["id"],
                "category": category,
                "current_filepath": file_info.get("filepath") or file_info.get("managed_pdf_relative_path"),
                "candidate_filepath": rel_candidate.replace("\\", "/"),
                "matched_fields": [{"field": field, "basename": name} for field, name, _ in raw_matches],
                "evidence": {
                    "reason": "exact unique source-pdfs basename plus title/year confirmed in first 4 pages",
                    **title_evidence,
                    "year_confirmed": True,
                },
            }
        )

    target_counts = Counter(item["candidate_filepath"] for item in proposed)
    safe = [item for item in proposed if target_counts[item["candidate_filepath"]] == 1]
    duplicate_target_rejections = len(proposed) - len(safe)
    if duplicate_target_rejections:
        candidate_rejections["candidate_target_used_by_multiple_unresolved_rows"] += duplicate_target_rejections

    return {
        "created_at": _now(),
        "read_only": True,
        "source_pdf_count": sum(len(paths) for paths in by_name.values()),
        "unresolved_missing_full_text": len(unresolved_rows),
        "categories": dict(categories),
        "safe_fix_count": len(safe),
        "safe_fixes": safe,
        "candidate_rejections": dict(candidate_rejections),
        "side_effects": {"db_writes": "none", "pdf_operations": "none", "api_calls": "none", "evaluation": "none"},
    }


def apply_plan(plan: dict[str, Any]) -> dict[str, Any]:
    safe_fixes = plan.get("safe_fixes") or []
    if not safe_fixes:
        return {"ok": True, "status": "no_safe_fixes", "backup": None, "applied_count": 0}

    backup = _create_backup()
    if not backup.get("ok"):
        return {"ok": False, "status": "refused_backup_failed", "backup": backup}

    target_ids = {item["id"] for item in safe_fixes}
    before_pdf = _pdf_snapshot_digest()
    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        before = _db_digest(conn, target_ids=target_ids)
        before_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        for item in safe_fixes:
            row = conn.execute("SELECT id, file_info FROM papers WHERE id = ?", (item["id"],)).fetchone()
            if not row:
                raise RuntimeError(f"target row disappeared: {item['id']}")
            file_info = _json_obj(row["file_info"])
            file_info["filepath"] = item["candidate_filepath"]
            file_info["source_pdf_reconciled_at"] = _now()
            file_info["source_pdf_reconciliation_evidence"] = item["evidence"]
            file_info["source_pdf_reconciliation_matched_fields"] = item["matched_fields"]
            conn.execute("UPDATE papers SET file_info = ? WHERE id = ?", (json.dumps(file_info, ensure_ascii=False), item["id"]))
        conn.commit()
        after = _db_digest(conn, target_ids=target_ids)
        after_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
    finally:
        conn.close()

    after_pdf = _pdf_snapshot_digest()
    failures: list[str] = []
    if before_count != after_count:
        failures.append("row count changed")
    if before["non_file_info_digest"] != after["non_file_info_digest"]:
        failures.append("non-file_info columns changed")
    if before["non_target_file_info_digest"] != after["non_target_file_info_digest"]:
        failures.append("non-target file_info changed")
    if before_pdf != after_pdf:
        failures.append("PDF snapshot changed")

    return {
        "ok": not failures,
        "status": "completed" if not failures else "post_write_validation_failed",
        "backup": backup,
        "applied_count": len(safe_fixes),
        "applied_ids": sorted(target_ids),
        "db_validation": {
            "before_count": before_count,
            "after_count": after_count,
            "row_count_unchanged": before_count == after_count,
            "non_file_info_columns_unchanged": before["non_file_info_digest"] == after["non_file_info_digest"],
            "non_target_file_info_unchanged": before["non_target_file_info_digest"] == after["non_target_file_info_digest"],
            "pdf_snapshot_unchanged": before_pdf == after_pdf,
            "failures": failures,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely reconcile missing source-PDF paths")
    parser.add_argument("--execute", action="store_true", help="Write exact safe file_info path fixes after backup")
    parser.add_argument("--allow-db-write", action="store_true", help="Required with --execute")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()

    plan = build_plan()
    if not args.execute:
        result: dict[str, Any] = {"ok": True, "mode": "dry_run", "plan": plan}
    elif not args.allow_db_write:
        result = {"ok": False, "mode": "execute", "status": "refused_missing_allow_db_write", "plan": plan}
    else:
        result = {"ok": True, "mode": "execute", "plan": plan, "apply": apply_plan(plan)}
        result["ok"] = bool(result["apply"].get("ok"))

    if args.summary_only:
        plan = result.get("plan", {})
        apply = result.get("apply", {})
        compact = {
            "ok": result.get("ok"),
            "mode": result.get("mode"),
            "status": result.get("status") or apply.get("status"),
            "unresolved_missing_full_text": plan.get("unresolved_missing_full_text"),
            "categories": plan.get("categories"),
            "safe_fix_count": plan.get("safe_fix_count"),
            "applied_count": apply.get("applied_count", 0),
            "applied_ids": apply.get("applied_ids", []),
            "backup": apply.get("backup"),
            "db_validation": apply.get("db_validation"),
            "candidate_rejections": plan.get("candidate_rejections"),
            "sample_safe_ids": [item["id"] for item in (plan.get("safe_fixes") or [])[:10]],
        }
        print(json.dumps(compact, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
