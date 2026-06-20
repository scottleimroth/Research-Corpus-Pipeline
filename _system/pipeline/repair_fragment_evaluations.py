#!/usr/bin/env python3
"""Repair fragmentary-evidence misclassifications from staging ingest runs."""

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
from add_paper import parse_sections  # noqa: E402
from document_text_extraction import extract_document_text  # noqa: E402
from evidence_coverage import assess_evidence_coverage, format_coverage_diagnostic  # noqa: E402
from repair_poisoned_article_identity import _source_path  # noqa: E402

AUDIT_DIR = config.CORPUS_STORE / "audit" / "evidence-coverage-repairs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paper_ids_from_run(run_dir: Path) -> list[str]:
    progress = run_dir / "all_staging_ingest_progress.jsonl"
    if not progress.exists():
        return []
    ids: list[str] = []
    for line in progress.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("event") != "paper_done":
            continue
        rating = str(rec.get("classification_rating") or "")
        if rating in {"not_applicable", "not_ratable_reference_material"}:
            pid = rec.get("paper_id")
            if pid:
                ids.append(str(pid))
    return sorted(set(ids))


def repair_paper(paper_id: str, *, execute: bool, allow_paid_api: bool = False) -> dict:
    paper = papers_db.get_paper(paper_id)
    if not paper:
        return {"paper_id": paper_id, "ok": False, "error": "missing"}

    cl = dict(paper.get("classification") or {})
    old_rating = cl.get("rating")
    src = _source_path(paper)
    rec = {
        "paper_id": paper_id,
        "old_rating": old_rating,
        "source_pdf": str(src) if src else None,
        "execute": execute,
    }

    if not src or not src.exists():
        rec.update(ok=False, error="source_pdf_missing")
        return rec

    extraction = extract_document_text(src, attempt_ocr=True)
    full_text = extraction.text or ""
    sections = parse_sections(full_text) if full_text else {"full": ""}
    bm = paper.get("basic_metadata") or {}
    from document_evidence import score_journal_article_signals

    article_signals = score_journal_article_signals(
        text=full_text,
        filename=str((paper.get("file_info") or {}).get("renamed_filename") or ""),
        identity={"title": bm.get("title"), "identity_status": bm.get("identity_status")},
        page_count=extraction.page_count,
    )
    coverage = assess_evidence_coverage(
        raw_text=full_text,
        sections=sections,
        extraction=extraction.to_dict(),
        article_signals=article_signals,
        identity={"title": bm.get("title")},
        document_type=paper.get("document_type"),
        filename=str((paper.get("file_info") or {}).get("renamed_filename") or ""),
    )
    rec["coverage"] = coverage
    rec["coverage_diagnostic"] = format_coverage_diagnostic(coverage)

    if execute:
        ft = dict(paper.get("full_text") or {})
        ft["raw_full_text"] = full_text
        ft["sections"] = sections
        ft["total_chars"] = len(full_text)
        ft["page_count"] = extraction.page_count
        ft["coverage"] = coverage
        ft["coverage_status"] = coverage.get("coverage_status")
        ft["extraction"] = extraction.to_dict()
        paper["full_text"] = ft

        cl["rating"] = None
        cl.pop("guard_blocked_rating", None)
        cl.pop("rating_justification", None)
        cl["red_team_notes"] = None
        cl["red_team_survival"] = None
        paper["classification"] = cl

        notes = dict(paper.get("notes_and_flags") or {})
        if coverage.get("evidence_can_support_rating"):
            notes["reevaluation_required"] = f"fragment_eval_repair:{coverage.get('coverage_status')}"
        else:
            notes.pop("reevaluation_required", None)
            notes["coverage_terminal"] = coverage.get("coverage_status")
            notes["coverage_terminal_reason"] = coverage.get("required_recovery_action")
        notes["coverage_status"] = coverage.get("coverage_status")
        notes["fragment_eval_repair_at"] = _now()
        paper["notes_and_flags"] = notes
        papers_db.save_paper(paper)

        if allow_paid_api and coverage.get("evidence_can_support_rating"):
            import anthropic
            from evaluate_paper import evaluate_one

            client = anthropic.Anthropic()
            ev = evaluate_one(client, paper_id, force=True, require_research_rating=True)
            rec["reevaluated"] = bool(ev.get("ok"))
            paper = papers_db.get_paper(paper_id) or {}
            rec["new_rating"] = (paper.get("classification") or {}).get("rating")
            if ev.get("ok"):
                notes = dict(paper.get("notes_and_flags") or {})
                notes.pop("reevaluation_required", None)
                notes["coverage_resolved"] = _now()
                paper["notes_and_flags"] = notes
                papers_db.save_paper(paper)

    rec["ok"] = True
    rec["final_status"] = "marked_for_reevaluation" if not rec.get("reevaluated") else "reevaluated"
    return rec


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair fragmentary evidence misclassifications")
    parser.add_argument("--run-id", default="all-staging-gui-20260531-141633")
    parser.add_argument("--paper-id", action="append", default=[])
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-paid-api", action="store_true")
    args = parser.parse_args()

    run_dir = config.RUNS_DIR / args.run_id
    targets = args.paper_id or _paper_ids_from_run(run_dir)
    results = [repair_paper(pid, execute=args.execute, allow_paid_api=args.allow_paid_api) for pid in targets]

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = AUDIT_DIR / f"repair_{ts}.json"
    payload = {"run_id": args.run_id, "targets": targets, "results": results, "generated_at": _now()}
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"ok": all(r.get("ok") for r in results), "report": str(out), "count": len(results)}, indent=2))
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
