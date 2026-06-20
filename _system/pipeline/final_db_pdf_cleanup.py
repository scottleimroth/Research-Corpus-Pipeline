"""Final DB/PDF/full_text cleanup under strict local-only policy.

This tool is intentionally conservative but active:
- one pre-cleanup DB backup before DB writes
- no API/evaluation/PDF deletion
- PDF moves only from source-pdfs to staging/review or staging root
- DB writes limited to duplicate/missing-row deletes and full_text updates
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import papers_db  # noqa: E402
import source_pdf_ownership  # noqa: E402

MIN_TEXT_CHARS = 500
MAX_AUTO_AMBIGUOUS = 250
MAX_AUTO_UNOWNED = 300


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


def _text_and_extraction(raw: Any) -> tuple[str, dict[str, Any]]:
    obj = _json_obj(raw)
    if obj:
        return str(obj.get("text") or obj.get("full_text") or obj.get("content") or ""), _json_obj(obj.get("extraction"))
    return str(raw or ""), {}


def _materialized_usable(raw: Any) -> bool:
    text, extraction = _text_and_extraction(raw)
    return bool(text) and len(text) >= MIN_TEXT_CHARS and not extraction.get("ocr_needed")


def _source_path(file_info: dict[str, Any]) -> Path | None:
    rel = file_info.get("filepath") or file_info.get("managed_pdf_relative_path") or file_info.get("managed_pdf_absolute_path")
    if not rel:
        return None
    raw = Path(str(rel))
    return raw if raw.is_absolute() else config.MASTER_PROJECT / raw


def _rel_to_master(path: Path) -> str:
    return str(path.resolve().relative_to(config.MASTER_PROJECT.resolve())).replace("\\", "/")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _create_backup() -> dict[str, Any]:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.BACKUP_DIR / f"papers-{datetime.now():%Y%m%d-%H%M%S}-FINAL-PRE-CLEANUP.db"
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


def _load_rows() -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(f"SELECT {','.join(papers_db.ALL_COLUMNS)} FROM papers ORDER BY id").fetchall()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in papers_db.ALL_COLUMNS:
        value = row[col]
        out[col] = _json_obj(value) if col in papers_db.JSON_FIELDS else value
    return out


def _is_legacy_id(paper_id: str) -> bool:
    return bool(
        str(paper_id).startswith(("orphan_", "1_s2_0_", "BF"))
        or "Unknown" in str(paper_id)
        or re.match(r"^_|\d{8,}_|^\d+$", str(paper_id))
    )


def _metadata_score(row: sqlite3.Row) -> int:
    basic = _json_obj(row["basic_metadata"])
    text, extraction = _text_and_extraction(row["full_text"])
    score = 0
    if not _is_legacy_id(row["id"]):
        score += 30
    if basic.get("doi"):
        score += 12
    if basic.get("title") and len(str(basic.get("title"))) >= 12:
        score += 10
    if basic.get("year"):
        score += 6
    authors = basic.get("authors")
    if isinstance(authors, list) and authors:
        score += 8
    elif isinstance(authors, str) and authors.strip():
        score += 5
    if row["document_type"]:
        score += 3
    if len(text) >= MIN_TEXT_CHARS:
        score += 12
    if extraction.get("status") == "extracted":
        score += 5
    for col in ("classification", "tags", "abstract_keywords"):
        if _json_obj(row[col]):
            score += 2
    return score


def _ownership_maps(audit: dict[str, Any]) -> tuple[dict[str, list[str]], list[str], list[str]]:
    examples = audit.get("examples") or {}
    source_to_ids: dict[str, list[str]] = defaultdict(list)
    for cls in ("owned_canonical_path", "owned_filename_exact", "owned_hash_exact", "ambiguous_multi_match"):
        # Examples are truncated, so rebuild from source_pdf_ownership internals for full coverage below.
        pass
    rows = _load_rows()
    source_paths = source_pdf_ownership._source_pdfs()  # noqa: SLF001
    refs, _ = source_pdf_ownership._build_refs(rows)  # noqa: SLF001
    source_by_rel = {(source_pdf_ownership._rel_to_master(p) or "").lower(): p for p in source_paths}  # noqa: SLF001
    source_by_name: dict[str, list[Path]] = defaultdict(list)
    for path in source_paths:
        source_by_name[path.name.lower()].append(path)
    source_hashes = source_pdf_ownership._source_hashes_if_needed(source_paths, refs)  # noqa: SLF001
    for ref in refs:
        if ref["is_hash_field"]:
            for path in source_hashes.get(str(ref["value"]).lower(), []):
                source_to_ids[(source_pdf_ownership._rel_to_master(path) or "").lower()].append(ref["id"])  # noqa: SLF001
            continue
        rel = (ref.get("resolved_rel") or "").lower()
        if rel in source_by_rel and not ref.get("is_weak"):
            source_to_ids[rel].append(ref["id"])
        basename = str(ref.get("basename") or "").lower()
        if basename.endswith(".pdf") and not ref.get("is_weak"):
            for path in source_by_name.get(basename, []):
                source_to_ids[(source_pdf_ownership._rel_to_master(path) or "").lower()].append(ref["id"])  # noqa: SLF001
    source_to_ids = {rel: sorted(set(ids)) for rel, ids in source_to_ids.items()}
    all_rels = sorted(source_by_rel)
    unowned = [rel for rel in all_rels if rel not in source_to_ids]
    ambiguous = [rel for rel, ids in source_to_ids.items() if len(ids) > 1]
    return source_to_ids, unowned, ambiguous


def _choose_canonical(row_ids: list[str], row_by_id: dict[str, sqlite3.Row]) -> tuple[str, list[str], dict[str, Any]]:
    scored = sorted((( _metadata_score(row_by_id[row_id]), row_id) for row_id in row_ids), reverse=True)
    canonical = scored[0][1]
    duplicates = [row_id for _, row_id in scored[1:]]
    return canonical, duplicates, {"scores": [{"id": row_id, "score": score} for score, row_id in scored]}


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


def _run_pdftotext(path: Path, timeout: int = 180) -> tuple[str, str, float]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except FileNotFoundError:
        return "", "pdftotext_not_found", time.perf_counter() - started
    except subprocess.TimeoutExpired:
        return "", "pdftotext_timeout", time.perf_counter() - started
    if proc.returncode != 0:
        return proc.stdout or "", (proc.stderr or f"pdftotext_exit_{proc.returncode}").strip(), time.perf_counter() - started
    return proc.stdout or "", "", time.perf_counter() - started


def _run_ocr(path: Path, timeout: int = 900) -> tuple[str, str, float]:
    started = time.perf_counter()
    if not shutil.which("ocrmypdf"):
        return "", "ocrmypdf_not_found", 0
    if not shutil.which("tesseract"):
        return "", "tesseract_not_found", 0
    with tempfile.TemporaryDirectory(prefix="papers_ocr_") as td:
        out_pdf = Path(td) / "ocr.pdf"
        sidecar = Path(td) / "ocr.txt"
        try:
            proc = subprocess.run(
                [
                    "ocrmypdf",
                    "--skip-text",
                    "--output-type",
                    "pdf",
                    "--sidecar",
                    str(sidecar),
                    "--tesseract-timeout",
                    "300",
                    "--jobs",
                    "1",
                    str(path),
                    str(out_pdf),
                ],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "", "ocrmypdf_timeout", time.perf_counter() - started
        if proc.returncode != 0:
            return sidecar.read_text(encoding="utf-8", errors="replace") if sidecar.exists() else "", (proc.stderr or f"ocrmypdf_exit_{proc.returncode}").strip(), time.perf_counter() - started
        return sidecar.read_text(encoding="utf-8", errors="replace") if sidecar.exists() else "", "", time.perf_counter() - started


def build_plan() -> dict[str, Any]:
    audit = source_pdf_ownership.build_audit()
    source_to_ids, unowned, ambiguous = _ownership_maps(audit)
    rows = _load_rows()
    row_by_id = {row["id"]: row for row in rows}
    duplicate_delete_ids: list[str] = []
    duplicate_decisions = []
    for rel in ambiguous:
        ids = [paper_id for paper_id in source_to_ids[rel] if paper_id in row_by_id]
        canonical, dupes, evidence = _choose_canonical(ids, row_by_id)
        duplicate_delete_ids.extend(dupes)
        duplicate_decisions.append({"source_pdf": rel, "canonical_id": canonical, "delete_duplicate_ids": dupes, "evidence": evidence})
    ambiguous_keep_ids = {decision["canonical_id"] for decision in duplicate_decisions}
    owned_ids = set()
    for ids in source_to_ids.values():
        owned_ids.update(ids)
    missing_db_ids = [row["id"] for row in rows if row["id"] not in owned_ids and row["id"] not in ambiguous_keep_ids]
    delete_ids = sorted(set(duplicate_delete_ids + missing_db_ids))
    unowned_move_paths = [str((config.MASTER_PROJECT / rel).resolve()) for rel in unowned]
    return {
        "created_at": _now(),
        "db_rows_before": len(rows),
        "source_pdfs_before": len(source_pdf_ownership._source_pdfs()),  # noqa: SLF001
        "ownership": {
            "owned_total": audit["owned_total"],
            "unowned_total": len(unowned),
            "ambiguous_total": len(ambiguous),
        },
        "duplicate_decisions": duplicate_decisions,
        "delete_ids": delete_ids,
        "delete_count": len(delete_ids),
        "missing_db_delete_count": len(missing_db_ids),
        "duplicate_delete_count": len(set(duplicate_delete_ids)),
        "unowned_move_paths": unowned_move_paths,
        "unowned_move_count": len(unowned_move_paths),
        "refuse": (
            len(ambiguous) > MAX_AUTO_AMBIGUOUS
            or len(unowned) > MAX_AUTO_UNOWNED
            or not audit["owned_total"]
        ),
        "side_effects": {"db_writes": "none yet", "pdf_moves": "none yet", "api_calls": "none", "evaluation": "none"},
    }


def execute(plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("refuse"):
        return {"ok": False, "status": "refused_mass_ambiguous_or_unowned", "plan_counts": {k: plan[k] for k in ("delete_count", "unowned_move_count")}}
    backup = _create_backup()
    if not backup.get("ok"):
        return {"ok": False, "status": "refused_backup_failed", "backup": backup}
    run_dir = config.RUNS_DIR / f"final-cleanup-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    deleted_audit = run_dir / "deleted_rows.jsonl"
    moved_manifest = run_dir / "moved_pdfs.jsonl"
    ocr_manifest = run_dir / "ocr_manifest.jsonl"
    before_pdf_count = sum(1 for _ in config.BASE.rglob("*.pdf"))

    delete_ids = sorted(set(plan["delete_ids"]))
    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    deleted_rows = []
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        before_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        if delete_ids:
            rows = conn.execute(
                f"SELECT {','.join(papers_db.ALL_COLUMNS)} FROM papers WHERE id IN ({','.join(['?'] * len(delete_ids))}) ORDER BY id",
                tuple(delete_ids),
            ).fetchall()
            with deleted_audit.open("w", encoding="utf-8") as f:
                for row in rows:
                    deleted_rows.append(row["id"])
                    f.write(json.dumps({"deleted_at": _now(), "row": _row_to_dict(row)}, ensure_ascii=False) + "\n")
            conn.executemany("DELETE FROM papers WHERE id = ?", [(paper_id,) for paper_id in delete_ids])
        conn.commit()
        after_delete_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
    finally:
        conn.close()

    moved = []
    move_errors = []
    for src_s in plan["unowned_move_paths"]:
        src = Path(src_s)
        if not src.exists():
            move_errors.append({"source": src_s, "error": "source_missing"})
            continue
        dest = _safe_dest(src, config.STAGING)
        shutil.move(str(src), str(dest))
        moved.append({"source": src_s, "destination": str(dest), "reason": "unowned_source_pdf_reprocess"})
    with moved_manifest.open("w", encoding="utf-8") as f:
        for item in moved + move_errors:
            f.write(json.dumps({"handled_at": _now(), **item}, ensure_ascii=False) + "\n")

    # Refresh rows after deletes/moves, then extract/OCR remaining missing/short rows with existing source PDFs.
    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    ocr_attempted = 0
    ocr_completed = 0
    ocr_failed = 0
    moved_review = []
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        rows = conn.execute("SELECT id, file_info, full_text FROM papers ORDER BY id").fetchall()
        targets = []
        for row in rows:
            file_info = _json_obj(row["file_info"])
            source = _source_path(file_info)
            if not source or not source.exists():
                continue
            text, extraction = _text_and_extraction(row["full_text"])
            if len(text) < MIN_TEXT_CHARS or extraction.get("ocr_needed") or not text:
                targets.append((row, source))
        conn.execute("BEGIN IMMEDIATE")
        with ocr_manifest.open("w", encoding="utf-8") as f:
            for row, source in targets:
                source_hash = _sha256(source)
                text, error, seconds = _run_pdftotext(source)
                method = "pdftotext"
                if error or len(text) < MIN_TEXT_CHARS:
                    ocr_attempted += 1
                    text, error, seconds = _run_ocr(source)
                    method = "ocrmypdf+tesseract"
                if not error and len(text) >= MIN_TEXT_CHARS:
                    payload = {
                        "text": text,
                        "extraction": {
                            "status": "extracted",
                            "method": method,
                            "extracted_at": _now(),
                            "text_length": len(text),
                            "ocr_needed": False,
                            "error": "",
                            "source_pdf": _rel_to_master(source) if source.is_relative_to(config.MASTER_PROJECT) else str(source),
                            "source_sha256": source_hash,
                            "elapsed_seconds": round(seconds, 3),
                            "paid_api_used": False,
                            "evaluation_run": False,
                        },
                    }
                    conn.execute("UPDATE papers SET full_text = ? WHERE id = ?", (json.dumps(payload, ensure_ascii=False), row["id"]))
                    if method.startswith("ocr"):
                        ocr_completed += 1
                    f.write(json.dumps({"id": row["id"], "source": str(source), "method": method, "status": "completed", "text_length": len(text)}, ensure_ascii=False) + "\n")
                else:
                    ocr_failed += 1
                    # Unreadable/problem PDFs leave active DB and move to review.
                    review_dest = _safe_dest(source, config.STAGING / "review")
                    moved_review.append({"id": row["id"], "source": str(source), "destination": str(review_dest), "error": error or "ocr_text_too_short"})
                    conn.execute("DELETE FROM papers WHERE id = ?", (row["id"],))
                    f.write(json.dumps({"id": row["id"], "source": str(source), "method": method, "status": "failed_move_review_delete_row", "error": error, "text_length": len(text)}, ensure_ascii=False) + "\n")
        conn.commit()
        final_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        conn.close()
    for item in moved_review:
        src = Path(item["source"])
        if src.exists():
            shutil.move(str(src), item["destination"])

    after_pdf_count = sum(1 for _ in config.BASE.rglob("*.pdf"))
    failures = []
    if after_pdf_count != before_pdf_count:
        failures.append("total PDF count changed unexpectedly")
    if quick_check != "ok":
        failures.append(f"quick_check={quick_check}")
    return {
        "ok": not failures,
        "status": "completed" if not failures else "post_cleanup_validation_failed",
        "backup": backup,
        "run_dir": str(run_dir),
        "deleted_rows_audit": str(deleted_audit),
        "moved_manifest": str(moved_manifest),
        "ocr_manifest": str(ocr_manifest),
        "db_rows_before": before_count,
        "db_rows_after_delete_phase": after_delete_count,
        "db_rows_after": final_count,
        "deleted_count": len(deleted_rows) + len(moved_review),
        "deleted_ids_sample": (deleted_rows + [item["id"] for item in moved_review])[:80],
        "source_pdfs_moved_to_staging": len(moved),
        "problem_pdfs_moved_to_review": len(moved_review),
        "ocr_attempted": ocr_attempted,
        "ocr_completed": ocr_completed,
        "ocr_failed": ocr_failed,
        "pdf_count_before": before_pdf_count,
        "pdf_count_after": after_pdf_count,
        "quick_check": quick_check,
        "failures": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Final DB/PDF/full_text cleanup")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-db-write", action="store_true")
    parser.add_argument("--allow-pdf-move", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    plan = build_plan()
    result: dict[str, Any] = {"ok": True, "mode": "dry_run", "plan": plan}
    if args.execute:
        if not args.allow_db_write or not args.allow_pdf_move:
            result = {"ok": False, "mode": "execute", "status": "refused_missing_required_gate", "plan": plan}
        else:
            result = {"ok": True, "mode": "execute", "plan": plan, "cleanup": execute(plan)}
            result["ok"] = bool(result["cleanup"].get("ok"))
    if args.summary_only:
        cleanup = result.get("cleanup", {})
        print(json.dumps({
            "ok": result.get("ok"),
            "mode": result.get("mode"),
            "status": result.get("status") or cleanup.get("status"),
            "plan": {
                "db_rows_before": plan["db_rows_before"],
                "source_pdfs_before": plan["source_pdfs_before"],
                "owned": plan["ownership"]["owned_total"],
                "unowned": plan["ownership"]["unowned_total"],
                "ambiguous": plan["ownership"]["ambiguous_total"],
                "delete_count": plan["delete_count"],
                "missing_db_delete_count": plan["missing_db_delete_count"],
                "duplicate_delete_count": plan["duplicate_delete_count"],
                "unowned_move_count": plan["unowned_move_count"],
                "refuse": plan["refuse"],
            },
            "cleanup": cleanup,
        }, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
