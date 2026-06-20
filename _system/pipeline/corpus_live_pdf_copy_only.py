"""Cacioppo-only managed PDF copy preview/live path.

The default path is a refusal preview: validate source, parent row, and
destination, then stop before copying unless --allow-pdf-copy is present.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
import papers_db

ALLOWED_RUN_ID = "20260528-203823"
CACIOPPO_ALIAS = "Cacioppo"
CACIOPPO_FILENAME = "(Cambridge Handbooks in Psychology) John T. Cacioppo (editor.pdf"
CACIOPPO_ID = "Cacioppo_2013_Handbook_book"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": ok, "detail": detail}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return val if isinstance(val, dict) else {}


def _get_parent_row() -> dict[str, Any] | None:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM papers WHERE id = ? LIMIT 1", (CACIOPPO_ID,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    out = dict(row)
    for key in papers_db.JSON_FIELDS:
        if key in out:
            out[key] = _json_obj(out.get(key))
    return out


def _db_digest() -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()]
        h = hashlib.sha256()
        for row in conn.execute(f"SELECT {','.join(cols)} FROM papers ORDER BY id"):
            vals = [row[col] for col in cols]
            encoded = json.dumps(vals, ensure_ascii=False, default=str).encode("utf-8")
            h.update(len(encoded).to_bytes(8, "big"))
            h.update(encoded)
        return {"count": count, "digest": h.hexdigest()}
    finally:
        conn.close()


def _pdf_snapshot() -> dict[str, Any]:
    entries = {}
    for root in (config.STAGING, config.SOURCE_PDFS):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.pdf")):
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = str(path.relative_to(config.BASE))
            entries[rel] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest()
    return {"count": len(entries), "digest": digest, "entries": entries}


def _snapshot_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    b = before.get("entries", {})
    a = after.get("entries", {})
    return {
        "added": sorted(set(a) - set(b)),
        "removed": sorted(set(b) - set(a)),
        "changed": sorted(k for k in set(a) & set(b) if a[k] != b[k]),
    }


def _safe_destination() -> Path:
    dest = (config.SOURCE_PDFS / f"{CACIOPPO_ID}.pdf").resolve()
    source_dir = config.SOURCE_PDFS.resolve()
    if dest.parent != source_dir:
        raise RuntimeError(f"unsafe managed PDF destination: {dest}")
    return dest


def _acquire_lock(run_dir: Path) -> Path:
    lock_path = run_dir / "orchestrated_cacioppo_pdf_copy_only.lock"
    payload = json.dumps(
        {
            "created_at": _now(),
            "stage": "orchestrated-cacioppo-pdf-copy-only",
            "run_id": run_dir.name,
            "process_only": CACIOPPO_FILENAME,
            "pid": os.getpid(),
        },
        indent=2,
    )
    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return lock_path


def _release_lock(lock_path: Path | None) -> None:
    if lock_path and lock_path.exists():
        lock_path.unlink()


def validate_pdf_copy_preconditions(args) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}

    checks.append(_result("--workflow ingest", getattr(args, "workflow", None) == "ingest", f"workflow={getattr(args, 'workflow', None)!r}"))
    checks.append(_result("--execute present", bool(getattr(args, "execute", False)), "required"))
    checks.append(_result("--live-pdf-copy-only Cacioppo", getattr(args, "live_pdf_copy_only", "") == CACIOPPO_ALIAS, f"value={getattr(args, 'live_pdf_copy_only', '')!r}"))
    checks.append(_result("--run-id exact", getattr(args, "run_id", "") == ALLOWED_RUN_ID, f"run_id={getattr(args, 'run_id', '')!r}"))
    checks.append(_result("--mode A", getattr(args, "mode", None) == "A", f"mode={getattr(args, 'mode', None)!r}"))
    checks.append(_result("--max-papers 5", getattr(args, "max_papers", None) == 5, f"max_papers={getattr(args, 'max_papers', None)!r}"))
    checks.append(_result("--allow-paid-api absent", not bool(getattr(args, "allow_paid_api", False)), "evaluation disabled"))
    checks.append(_result("--allow-db-write absent", not bool(getattr(args, "allow_db_write", False)), "DB writes disabled"))
    checks.append(_result("--allow-staging-archive absent", not bool(getattr(args, "allow_staging_archive", False)), "staging archive disabled"))

    run_dir = config.RUNS_DIR / args.run_id
    details["run_dir"] = run_dir
    checks.append(_result("run directory exists", run_dir.exists(), str(run_dir)))
    parent_artifact = run_dir / "orchestrated_cacioppo_parent_only_live.json"
    checks.append(_result("Cacioppo parent live artifact exists", parent_artifact.exists(), str(parent_artifact)))
    if not run_dir.exists() or not parent_artifact.exists():
        return False, checks, details

    parent_live = _load_json(parent_artifact)
    details["parent_live_artifact"] = parent_live
    checks.append(_result("Cacioppo parent row created", parent_live.get("row_created") is True, str(parent_live.get("row_created"))))
    checks.append(_result("Cacioppo parent ID matches", parent_live.get("proposed_safe_id") == CACIOPPO_ID, str(parent_live.get("proposed_safe_id"))))

    parent_row = _get_parent_row()
    details["parent_row"] = parent_row
    checks.append(_result("Cacioppo DB row exists", parent_row is not None, CACIOPPO_ID))
    if parent_row:
        checks.append(_result("Cacioppo DB document_type book", parent_row.get("document_type") == "book", str(parent_row.get("document_type"))))
        checks.append(_result("Cacioppo DB row has no managed filepath yet", not (_json_obj(parent_row.get("file_info")).get("filepath")), "filepath should be empty before copy"))

    parent_preview = parent_live.get("parent_row_preview") or {}
    file_info = parent_preview.get("file_info") or {}
    source = Path(file_info.get("staged_path") or "")
    expected_hash = file_info.get("staged_sha256")
    details["source"] = source
    details["expected_hash"] = expected_hash
    checks.append(_result("staged Cacioppo PDF exists", source.exists(), str(source)))
    source_hash = _sha256(source) if source.exists() else ""
    details["source_hash"] = source_hash
    checks.append(_result("staged Cacioppo hash matches artifact", source_hash == expected_hash, str(expected_hash)))

    try:
        dest = _safe_destination()
        destination_safe = True
        destination_detail = str(dest)
    except RuntimeError as exc:
        dest = config.SOURCE_PDFS / f"{CACIOPPO_ID}.pdf"
        destination_safe = False
        destination_detail = str(exc)
    details["destination"] = dest
    checks.append(_result("managed destination is safe", destination_safe, destination_detail))
    checks.append(_result("no existing managed PDF collision", not dest.exists(), str(dest)))

    return all(c["ok"] for c in checks), checks, details


def _write_artifacts(run_dir: Path, summary: dict[str, Any]) -> dict[str, str]:
    if summary.get("status") == "live_copy_completed":
        json_path = run_dir / "orchestrated_cacioppo_pdf_copy_only_live.json"
        report_path = run_dir / "orchestrated_cacioppo_pdf_copy_only_live_report.txt"
    else:
        json_path = run_dir / "orchestrated_cacioppo_pdf_copy_only_preview.json"
        report_path = run_dir / "orchestrated_cacioppo_pdf_copy_only_preview_report.txt"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(render_pdf_copy_report(summary), encoding="utf-8")
    return {"json": str(json_path), "report": str(report_path)}


def render_pdf_copy_report(summary: dict[str, Any]) -> str:
    title = (
        "Orchestrated Cacioppo Managed PDF Copy Live"
        if summary.get("status") == "live_copy_completed"
        else "Orchestrated Cacioppo Managed PDF Copy Refusal Preview"
    )
    lines = [
        title,
        "=" * 58,
        f"Run ID: {summary.get('run_id')}",
        f"Status: {summary.get('status')}",
        f"Processed only: {summary.get('processed_only')}",
        f"Source: {summary.get('source_staged_pdf')}",
        f"Source hash: {summary.get('source_staged_pdf_hash')}",
        f"Destination: {summary.get('managed_pdf_destination')}",
        "",
        "Actions",
        "-" * 58,
        f"DB action: {summary.get('db_action')}",
        f"PDF action: {summary.get('pdf_action')}",
        f"API/evaluation action: {summary.get('api_evaluation_action')}",
        f"Not processed: {summary.get('not_processed')}",
        "",
        f"Next command (DO NOT RUN YET): {summary.get('next_live_copy_command_do_not_run_yet')}",
    ]
    return "\n".join(lines) + "\n"


def run_ingest_workflow_live_pdf_copy_only(args) -> dict[str, Any]:
    ok, checks, details = validate_pdf_copy_preconditions(args)
    run_dir = Path(details.get("run_dir") or (config.RUNS_DIR / (args.run_id or ALLOWED_RUN_ID)))
    source = details.get("source")
    dest = details.get("destination") or (config.SOURCE_PDFS / f"{CACIOPPO_ID}.pdf")
    source_hash = details.get("source_hash", "")
    summary = {
        "created_at": _now(),
        "run_id": args.run_id,
        "stage": "orchestrated-cacioppo-pdf-copy-only",
        "processed_only": [CACIOPPO_FILENAME],
        "not_processed": [
            "(Oxford Handbooks) Susan Hallam, Ian Cross, Michael Thaut (e.pdf",
            "Ashley, Richard(Editor)_Timmers, Renee(Contributor) - The Ro.pdf",
            "Levitin-The Psychology of Music- Rhythm and Movement-2017-An.pdf",
        ],
        "checks": checks,
        "source_staged_pdf": str(source) if source else None,
        "source_staged_pdf_hash": source_hash,
        "managed_pdf_destination": str(dest),
        "db_action": "none",
        "pdf_action": "none",
        "api_evaluation_action": "none",
        "next_live_copy_command_do_not_run_yet": (
            "python pipeline\\run_corpus_pipeline.py --workflow ingest --mode A "
            "--selection-profile broader --pilot-profile safe-live --max-papers 5 "
            "--run-id 20260528-203823 --execute --live-pdf-copy-only Cacioppo --allow-pdf-copy"
        ),
        "side_effects": {
            "db_writes": "none",
            "api_calls": "none",
            "evaluations": "none",
        },
    }

    if not ok:
        summary["status"] = "precondition_failed"
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifact_paths": paths, "message": "Cacioppo PDF copy preconditions failed."}

    if not bool(getattr(args, "allow_pdf_copy", False)):
        summary.update(
            {
                "status": "refused_before_pdf_copy_missing_allow_pdf_copy",
                "ready_for_live_copy": True,
                "pdf_action": "none (refused before copy; --allow-pdf-copy absent)",
                "destination_preview": {
                    "path": str(dest),
                    "exists": dest.exists(),
                    "parent": str(dest.parent),
                },
            }
        )
        paths = _write_artifacts(run_dir, summary)
        return {
            "ok": True,
            "summary": summary,
            "artifact_paths": paths,
            "message": "Cacioppo managed PDF destination validated; refused before copy because --allow-pdf-copy is absent.",
        }

    lock_path: Path | None = None
    try:
        lock_path = _acquire_lock(run_dir)
        before_db = _db_digest()
        before_pdf = _pdf_snapshot()
        config.SOURCE_PDFS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        copied_hash = _sha256(dest)
        after_pdf = _pdf_snapshot()
        after_db = _db_digest()
    except Exception as exc:
        summary.update({"status": "live_copy_failed", "error": str(exc)})
        paths = _write_artifacts(run_dir, summary)
        return {"ok": False, "summary": summary, "artifact_paths": paths, "message": "Cacioppo PDF copy failed; inspect artifact."}
    finally:
        _release_lock(lock_path)

    delta = _snapshot_delta(before_pdf, after_pdf)
    expected_added = str(dest.relative_to(config.BASE))
    failures = []
    if copied_hash != source_hash:
        failures.append("managed copy hash does not match source")
    if delta != {"added": [expected_added], "removed": [], "changed": []}:
        failures.append(f"unexpected PDF snapshot delta: {delta}")
    if before_db != after_db:
        failures.append("papers.db changed during PDF copy stage")

    summary.update(
        {
            "status": "live_copy_completed",
            "ready_for_live_copy": False,
            "pdf_action": "copied one Cacioppo staged PDF to managed destination",
            "managed_copied_pdf_hash": copied_hash,
            "pdf_validation": {
                "before": before_pdf,
                "after": after_pdf,
                "delta": delta,
                "exactly_one_pdf_added": delta == {"added": [expected_added], "removed": [], "changed": []},
                "source_hash_matches_destination": copied_hash == source_hash,
            },
            "db_validation": {
                "before": before_db,
                "after": after_db,
                "papers_db_unchanged": before_db == after_db,
            },
            "post_copy_failures": failures,
            "side_effects": {
                "db_writes": "none",
                "api_calls": "none",
                "evaluations": "none",
                "pdf_operations": "one managed PDF copy only",
            },
        }
    )
    paths = _write_artifacts(run_dir, summary)
    return {
        "ok": not failures,
        "summary": summary,
        "artifact_paths": paths,
        "message": "Cacioppo managed PDF copied." if not failures else "Cacioppo PDF copy completed but validation failed; inspect artifact.",
    }
