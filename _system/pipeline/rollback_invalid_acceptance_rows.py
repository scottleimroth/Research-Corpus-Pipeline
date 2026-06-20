#!/usr/bin/env python3
"""Rollback DB rows and source PDFs that passed ingest without valid acceptance."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import config  # noqa: E402
import papers_db  # noqa: E402
from add_paper import rollback_accepted_paper  # noqa: E402
from document_evidence import decide_acceptance_from_paper  # noqa: E402
from eval_completion import is_ingest_evaluation_complete  # noqa: E402
from evidence_coverage import format_coverage_diagnostic  # noqa: E402

AUDIT_DIR = config.CORPUS_STORE / "audit" / "acceptance-rollbacks"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paper_ids_from_run(run_dir: Path) -> list[str]:
    progress = run_dir / "all_staging_ingest_progress.jsonl"
    if not progress.exists():
        return []
    ids: list[str] = []
    for line in progress.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("event") != "paper_done":
            continue
        if rec.get("status") in {"insufficient_evidence", "failed"} or rec.get("paper_id"):
            pid = rec.get("paper_id")
            if pid:
                ids.append(str(pid))
    return sorted(set(ids))


def is_invalid_acceptance_row(paper: dict) -> tuple[bool, str, dict]:
    acceptance = decide_acceptance_from_paper(paper)
    complete, complete_reason = is_ingest_evaluation_complete(paper)
    cov = acceptance.audit_fields or {}
    likely = cov.get("likely_journal_article")
    can_rate = cov.get("evidence_can_support_rating")
    can_na = cov.get("evidence_can_support_not_applicable")

    if likely and not can_rate and not can_na:
        if complete:
            return True, f"invalid_complete_journal_weak_coverage:{acceptance.reason}", acceptance.to_dict()
        if not acceptance.can_insert_db:
            return True, f"invalid_ingest_without_acceptance:{acceptance.action}", acceptance.to_dict()

    if not acceptance.can_insert_db and not complete:
        return True, f"orphan_row_without_acceptance:{acceptance.action}", acceptance.to_dict()

    if not acceptance.can_insert_db and complete:
        return True, f"complete_despite_blocked_acceptance:{acceptance.reason}", acceptance.to_dict()

    return False, complete_reason or "ok", acceptance.to_dict()


def scan_invalid_rows(*, paper_ids: list[str] | None = None) -> list[dict]:
    # None => explicit whole-DB audit. An (even empty) list => scan exactly those
    # ids. A run-scoped empty list must NEVER fall back to the whole DB, or a run
    # that inserted nothing would scan (and risk rolling back) the entire corpus.
    if paper_ids is None:
        targets = [p.get("id") for p in papers_db.get_all_metadata() if p.get("id")]
    else:
        targets = list(paper_ids)
    found: list[dict] = []
    for pid in targets:
        paper = papers_db.get_paper(pid)
        if not paper:
            continue
        invalid, reason, acceptance = is_invalid_acceptance_row(paper)
        if not invalid:
            continue
        from evidence_coverage import assess_evidence_coverage_from_paper

        coverage = assess_evidence_coverage_from_paper(paper)
        found.append(
            {
                "paper_id": pid,
                "reason": reason,
                "acceptance": acceptance,
                "coverage_diagnostic": format_coverage_diagnostic(coverage),
                "classification_rating": (paper.get("classification") or {}).get("rating"),
            }
        )
    return found


def rollback_rows(rows: list[dict], *, execute: bool, run_id: str = "") -> dict:
    rolled: list[dict] = []
    for row in rows:
        pid = row["paper_id"]
        if not execute:
            rolled.append({**row, "rolled_back": False, "dry_run": True})
            continue
        rb = rollback_accepted_paper(
            pid,
            reason=row.get("reason") or "invalid_acceptance",
            audit_context={"run_id": run_id, "acceptance": row.get("acceptance"), "scan": row},
        )
        rolled.append({**row, "rolled_back": True, "rollback": rb})
    summary = {
        "generated_at": _now(),
        "run_id": run_id,
        "execute": execute,
        "invalid_count": len(rows),
        "rolled_back_count": sum(1 for r in rolled if r.get("rolled_back")),
        "rows": rolled,
    }
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    out = AUDIT_DIR / f"rollback_scan_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["audit_path"] = str(out)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Rollback invalid acceptance DB rows")
    parser.add_argument("--run-id", default="", help="Limit to paper_ids from a staging run log")
    parser.add_argument("--paper-id", action="append", default=[])
    parser.add_argument("--all", action="store_true", help="Operate on the ENTIRE DB (required for any unscoped run)")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    # Scope precedence: explicit --paper-id / --run-id => scoped (never whole DB).
    # A whole-DB run is destructive at scale, so it must be opted into with --all;
    # an unscoped invocation without --all is refused (data-loss footgun guard).
    scoped = bool(args.paper_id) or bool(args.run_id)
    if not scoped and not args.all:
        print(
            "Refusing to operate on the whole DB without an explicit scope. "
            "Pass --run-id/--paper-id to scope, or --all to audit the entire DB.",
            file=sys.stderr,
        )
        return 2
    paper_ids: list[str] | None = list(args.paper_id) if args.paper_id else ([] if scoped else None)
    if args.run_id:
        run_dir = config.RUNS_DIR / args.run_id
        run_ids = _paper_ids_from_run(run_dir)
        paper_ids = sorted(set((paper_ids or []) + run_ids))

    rows = scan_invalid_rows(paper_ids=paper_ids)
    summary = rollback_rows(rows, execute=args.execute, run_id=args.run_id)
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))
    for row in summary.get("rows") or []:
        print(f"  {row.get('paper_id')}: {row.get('reason')} | {row.get('coverage_diagnostic')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
