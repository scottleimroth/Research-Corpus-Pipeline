"""Batch orchestrator: resolve review/resolution candidates via the central disposition engine."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import papers_db
from child_document_matching import (
    ChildDocumentRunContext,
    finalize_pending_child_documents,
    match_pending_for_new_parents,
    pending_folder_stats,
)
from file_disposition_engine import (
    FINAL_ACTIONS,
    DispositionCounts,
    DispositionRequest,
    decide_file_disposition,
    execute_file_disposition,
    iter_resolver_candidates,
    list_staging_candidates,
)
from review_resolution import RESOLUTION_LOG_DIR, ResolutionAudit, TECHNICAL_REVIEW
from review_retry_tracking import cleanup_orphan_ingest_artifacts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_db_rows_by_filename(filename: str) -> list[dict[str, Any]]:
    name_low = filename.lower()
    stem_low = Path(filename).stem.lower()
    matches: list[dict[str, Any]] = []
    for meta in papers_db.get_all_metadata():
        pid = meta.get("id")
        if not pid:
            continue
        paper = papers_db.get_paper(pid)
        if not paper:
            continue
        fi = paper.get("file_info") or {}
        for key in ("original_filename", "renamed_filename"):
            val = (fi.get(key) or "").lower()
            if val == name_low or val == stem_low + ".pdf":
                matches.append(paper)
                break
        else:
            if pid.lower() == stem_low or stem_low.startswith(pid.lower()):
                matches.append(paper)
    return matches


def _materialize_row_full_text(paper_id: str, *, dry_run: bool) -> bool:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, file_info, full_text FROM papers WHERE id = ?",
            (paper_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    from materialize_full_text import _full_text_materialized, _materialize_preview

    if _full_text_materialized(row["full_text"]):
        return False
    preview = _materialize_preview(row)
    if preview.get("status") != "extracted" or dry_run:
        return preview.get("status") == "extracted" and dry_run
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            "UPDATE papers SET full_text = ? WHERE id = ?",
            (json.dumps(preview["payload"], ensure_ascii=False), paper_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def _maybe_repair_linked_db_rows(
    path: Path,
    *,
    allow_db_write: bool,
    dry_run: bool,
    audit: ResolutionAudit,
    counts: dict[str, int],
) -> None:
    if not allow_db_write or dry_run:
        return
    for paper in _find_db_rows_by_filename(path.name):
        if _materialize_row_full_text(paper["id"], dry_run=False):
            counts["db_rows_repaired"] += 1
            audit.record(
                action="DB_ROW_REPAIRED",
                source=str(path),
                paper_id=paper["id"],
                final_action="DB_ROW_REPAIRED",
            )


def _resolve_candidate(
    source_context: str,
    path: Path,
    *,
    run_id: str,
    audit: ResolutionAudit,
    counts: DispositionCounts,
    summary_counts: dict[str, int],
    dry_run: bool,
    allow_paid_api: bool,
    allow_opus: bool,
    allow_db_write: bool,
    allow_file_move: bool,
    child_ctx: ChildDocumentRunContext,
) -> dict[str, Any]:
    _maybe_repair_linked_db_rows(
        path,
        allow_db_write=allow_db_write,
        dry_run=dry_run,
        audit=audit,
        counts=summary_counts,
    )
    if not dry_run and allow_file_move:
        cleanup_orphan_ingest_artifacts(path, config.SOURCE_PDFS)

    req = DispositionRequest(
        path=path,
        source_context=source_context,
        run_id=run_id,
        allow_paid_api=allow_paid_api,
        allow_opus_resolution=allow_opus,
        allow_db_write=allow_db_write,
        allow_file_move=allow_file_move,
        dry_run=dry_run,
    )
    decision = decide_file_disposition(req, child_ctx=child_ctx)
    execution = execute_file_disposition(decision, req, child_ctx=child_ctx, counts=counts)
    result = {**decision, **execution}
    action = str(result.get("action") or "")

    audit.record(
        action=f"{'planned' if dry_run else 'executed'}:{action}",
        source=str(path),
        source_context=source_context,
        final_action=action,
        reason=result.get("reason"),
        classification=result.get("classification"),
        destination=result.get("dest") or result.get("moved_to_staging") or result.get("stop_path"),
        linked_parent=result.get("linked_parent"),
        dry_run=dry_run,
    )
    summary_counts["candidates_scanned"] += 1
    key = f"action_{action.lower()}"
    summary_counts[key] = summary_counts.get(key, 0) + 1
    return result


def run_review_resolver(
    *,
    plan_only: bool = False,
    execute: bool = False,
    allow_db_write: bool = False,
    allow_pdf_move: bool = False,
    allow_paid_api: bool = False,
    allow_opus_resolution: bool = False,
) -> dict[str, Any]:
    dry_run = plan_only or not execute
    if execute and not allow_pdf_move:
        return {"ok": False, "error": "--execute requires --allow-pdf-move"}

    allow_opus = bool(allow_opus_resolution and allow_paid_api and not dry_run)
    allow_sonnet = bool(allow_paid_api and not dry_run)
    run_id = f"review-resolver-{datetime.now():%Y%m%d-%H%M%S}"
    audit = ResolutionAudit(run_id=run_id)
    disposition_counts = DispositionCounts()
    summary_counts: dict[str, int] = {
        "candidates_scanned": 0,
        "db_rows_repaired": 0,
    }
    child_ctx = ChildDocumentRunContext()
    items: list[dict[str, Any]] = []

    stop_model_before = (
        len(list(config.REVIEW_RECOVERY_PENDING.glob("*")))
        if config.REVIEW_RECOVERY_PENDING.exists()
        else 0
    )
    stop_technical_before = (
        len(list(config.REVIEW_TECHNICAL_FAILURE.glob("*.pdf")))
        if config.REVIEW_TECHNICAL_FAILURE.exists()
        else 0
    )

    for source_context, path in iter_resolver_candidates():
        result = _resolve_candidate(
            source_context,
            path,
            run_id=run_id,
            audit=audit,
            counts=disposition_counts,
            summary_counts=summary_counts,
            dry_run=dry_run,
            allow_paid_api=allow_sonnet,
            allow_opus=allow_opus,
            allow_db_write=allow_db_write and not dry_run,
            allow_file_move=allow_pdf_move and not dry_run,
            child_ctx=child_ctx,
        )
        items.append(
            {
                "source_context": source_context,
                "filename": path.name,
                "final_action": result.get("action"),
                "reason": result.get("reason"),
                "classification": result.get("classification"),
                "dry_run": dry_run,
            }
        )

    if not dry_run:
        if child_ctx.run_paper_ids:
            match_pending_for_new_parents(list(child_ctx.run_paper_ids), child_ctx)
        finalize_pending_child_documents(child_ctx, run_completed_normally=True)

    pending_stats = pending_folder_stats()
    from source_pdf_ownership import build_audit

    ownership = build_audit()
    stop_model_after = (
        len(list(config.REVIEW_RECOVERY_PENDING.glob("*")))
        if config.REVIEW_RECOVERY_PENDING.exists()
        else 0
    )
    stop_technical_after = (
        len(list(TECHNICAL_REVIEW.glob("*.pdf"))) if TECHNICAL_REVIEW.exists() else 0
    )

    summary = {
        "created_at": _now(),
        "mode": "plan_only" if dry_run else "execute",
        "run_id": run_id,
        "allow_paid_api": allow_sonnet,
        "allow_opus": allow_opus,
        "allow_db_write": allow_db_write and not dry_run,
        "allow_pdf_move": allow_pdf_move and not dry_run,
        "disposition_engine": "file_disposition_engine",
        "final_actions_enum": sorted(FINAL_ACTIONS),
        **disposition_counts.to_dict(),
        **summary_counts,
        "stop_model_unavailable_before": stop_model_before,
        "stop_model_unavailable_after": stop_model_after,
        "stop_technical_failure_before": stop_technical_before,
        "stop_technical_failure_after": stop_technical_after,
        "pending_child_documents_count": pending_stats["count"],
        "oldest_pending_child_document_days": pending_stats["oldest_days"],
        "staging_candidate_count": len(list_staging_candidates()),
        "source_ownership_unowned": ownership.get("unowned_total"),
        "source_ownership_ambiguous": ownership.get("ambiguous_total"),
        "items_sample": items[:40],
        "items_total": len(items),
        "audit_log_dir": str(audit.log_dir),
    }
    audit.summary.update(summary)
    audit.finalize()
    out_path = RESOLUTION_LOG_DIR / run_id / "review_resolver_summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["summary_path"] = str(out_path)

    ok = ownership.get("unowned_total", 1) == 0 and ownership.get("ambiguous_total", 1) == 0
    return {"ok": ok, "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve review/resolution candidates via the central disposition engine"
    )
    parser.add_argument("--plan-only", action="store_true", help="Dry-run; report disposition actions only")
    parser.add_argument("--execute", action="store_true", help="Apply disposition engine decisions")
    parser.add_argument("--allow-db-write", action="store_true", help="Allow DB full_text repair for linked rows")
    parser.add_argument("--allow-pdf-move", action="store_true", help="Required with --execute for moves/deletes")
    parser.add_argument("--allow-paid-api", action="store_true", help="Allow Sonnet recovery inside disposition engine")
    parser.add_argument("--allow-opus-resolution", action="store_true", help="Allow Opus escalation inside disposition engine")
    args = parser.parse_args()

    if not args.plan_only and not args.execute:
        args.plan_only = True

    result = run_review_resolver(
        plan_only=args.plan_only,
        execute=args.execute,
        allow_db_write=args.allow_db_write,
        allow_pdf_move=args.allow_pdf_move,
        allow_paid_api=args.allow_paid_api,
        allow_opus_resolution=args.allow_opus_resolution,
    )
    print(json.dumps(result, indent=2, default=str))
    raise SystemExit(0 if result.get("ok", True) else 1)


if __name__ == "__main__":
    main()
