#!/usr/bin/env python3
"""Backfill text + evaluation completeness for existing DB rows."""

from __future__ import annotations

import argparse
import json
import sys

# Line-buffered stdout so long runs show progress in log files.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import anthropic  # noqa: E402
import config  # noqa: E402
import papers_db  # noqa: E402
from document_corpus_classification import apply_non_ratable_reference_classification, is_non_ratable_document_type  # noqa: E402
from document_text_extraction import extract_document_text  # noqa: E402
from eval_completion import is_ingest_evaluation_complete, is_research_rating  # noqa: E402
from evaluate_paper import _fast_backfill_red_team_notes, build_evaluation_input, evaluate_one  # noqa: E402
from llm_providers import MODELS  # noqa: E402

EVAL_MODEL_ALIASES = {
    "haiku": "haiku-4.5",
    "sonnet": "sonnet-4.5",
    "deepseek": "deepseek-v4-pro",
    "deepseek-v4": "deepseek-v4-pro",
    "deepseek-v3": "deepseek-v3",
}


def _resolve_eval_model(name: str) -> str:
    key = (name or "deepseek-v4-pro").strip().lower()
    key = EVAL_MODEL_ALIASES.get(key, key)
    if key not in MODELS:
        raise ValueError(f"Unknown --eval-model {name!r}. Choose from: {sorted(MODELS)}")
    return key


def _source_path(paper: dict[str, Any]) -> Path | None:
    fi = paper.get("file_info") or {}
    rel = str(fi.get("filepath") or "").strip()
    if rel:
        p = Path(rel)
        if not p.is_absolute():
            p = config.MASTER_PROJECT / p
        if p.exists():
            return p
    renamed = str(fi.get("renamed_filename") or "").strip()
    if renamed:
        p = config.SOURCE_PDFS / renamed
        if p.exists():
            return p
    return None


def _raw_text_len(paper: dict[str, Any]) -> int:
    ft = paper.get("full_text") or {}
    raw = str(ft.get("raw_full_text") or ft.get("full") or ft.get("text") or "")
    return len(raw.strip())


def _evaluable_text_len(paper: dict[str, Any]) -> int:
    text, _plan = build_evaluation_input(paper, ignore_coverage_gate=True)
    return len(text or "")


def _refresh_text_if_needed(paper: dict[str, Any], *, execute: bool, min_chars: int) -> tuple[bool, str]:
    pid = str(paper.get("id") or "")
    current = _raw_text_len(paper)
    if current >= min_chars:
        return False, f"text_ok:{current}"
    src = _source_path(paper)
    if not src:
        return False, "source_missing"
    ex = extract_document_text(src, attempt_ocr=True, use_cache=True)
    text = str(ex.text or "")
    text_len = int(ex.text_length or len(text))
    if text_len <= 0:
        return False, f"extract_failed:{ex.extractor_status}"
    if not execute:
        return True, f"would_update_text:{current}->{text_len}"

    latest = papers_db.get_paper(pid) or paper
    ft = dict(latest.get("full_text") or {})
    ft["raw_full_text"] = text
    ft["total_chars"] = text_len
    ft["page_count"] = ex.page_count
    ft["extraction_method"] = ex.extraction_source
    ft["extraction"] = {
        "extraction_source": ex.extraction_source,
        "extractor_status": ex.extractor_status,
        "text_length": text_len,
        "page_count": ex.page_count,
    }
    sections = ft.get("sections")
    if not isinstance(sections, dict) or not sections:
        ft["sections"] = {"full": text}
    latest["full_text"] = ft
    papers_db.save_paper(latest)
    return True, f"updated_text:{current}->{text_len}"


def _patch_missing_red_team_notes(paper: dict[str, Any], *, execute: bool) -> tuple[bool, str]:
    cl = paper.get("classification") or {}
    rating = str(cl.get("rating") or "").strip()
    justification = str(cl.get("rating_justification") or "").strip()
    red_team = str(cl.get("red_team_notes") or "").strip()
    if not (rating and justification and not red_team and is_research_rating(rating)):
        return False, "no_red_team_patch_needed"
    if not execute:
        return True, "would_patch_red_team_notes"
    latest = papers_db.get_paper(str(paper.get("id") or "")) or paper
    cl2 = dict(latest.get("classification") or {})
    cl2["red_team_notes"] = _fast_backfill_red_team_notes(cl2)
    cl2["red_team_survival"] = cl2.get("red_team_survival")
    latest["classification"] = cl2
    latest.setdefault("notes_and_flags", {})["evaluation_tier"] = "haiku_backfill_patched"
    papers_db.save_paper(latest)
    return True, "patched_red_team_notes"


def _ensure_non_ratable_rating(paper_id: str, paper: dict[str, Any], *, execute: bool) -> tuple[bool, str]:
    if not is_non_ratable_document_type(paper.get("document_type")):
        return False, "not_non_ratable_type"
    title = str((paper.get("basic_metadata") or {}).get("title") or "")
    out = apply_non_ratable_reference_classification(
        paper_id,
        document_type=paper.get("document_type"),
        reasons=["db_completeness_backfill"],
        title=title,
    )
    if not execute:
        return True, "would_apply_non_ratable_rating"
    return bool(out.get("ok")), "applied_non_ratable_rating" if out.get("ok") else f"non_ratable_failed:{out}"


def run(
    *,
    execute: bool,
    allow_paid_api: bool,
    min_text_chars: int,
    min_eval_chars: int,
    limit: int | None,
    fast_eval: bool,
    eval_model: str,
    text_only: bool,
    record_items: bool,
) -> dict[str, Any]:
    rows = papers_db.get_all_metadata()
    if limit:
        rows = rows[: int(limit)]
    client = None  # evaluate_one uses pipeline_llm (DeepSeek-first escalation)

    summary: dict[str, Any] = {
        "execute": execute,
        "allow_paid_api": allow_paid_api,
        "fast_eval": fast_eval,
        "eval_model": eval_model,
        "text_only": text_only,
        "rows_scanned": 0,
        "already_complete": 0,
        "text_backfilled": 0,
        "text_backfill_failed": 0,
        "red_team_patched": 0,
        "eval_skipped_no_text": 0,
        "eval_attempted": 0,
        "eval_fixed": 0,
        "eval_failed": 0,
        "still_incomplete": 0,
        "reasons": {},
        "failures_sample": [],
        "items": [] if record_items else None,
    }

    for meta in rows:
        pid = str(meta.get("id") or "")
        if not pid:
            continue
        summary["rows_scanned"] += 1
        if summary["rows_scanned"] % 100 == 0:
            print(
                f"  ... scanned {summary['rows_scanned']} rows | "
                f"text+{summary['text_backfilled']} | eval {summary['eval_fixed']}/{summary['eval_attempted']} | "
                f"incomplete {summary['still_incomplete']}",
                flush=True,
            )
        paper = papers_db.get_paper(pid) or {}
        item: dict[str, Any] | None = {"paper_id": pid} if record_items else None

        changed_text, text_msg = _refresh_text_if_needed(paper, execute=execute, min_chars=min_text_chars)
        if item is not None:
            item["text"] = text_msg
        if changed_text:
            summary["text_backfilled"] += 1
            print(f"  text backfill: {pid} ({text_msg})", flush=True)
            paper = papers_db.get_paper(pid) or paper
        elif text_msg.startswith("extract_failed") or text_msg == "source_missing":
            summary["text_backfill_failed"] += 1

        complete, reason = is_ingest_evaluation_complete(paper)
        if complete:
            summary["already_complete"] += 1
            if item is not None:
                item["complete"] = True
                summary["items"].append(item)
            continue

        rating = str(((paper.get("classification") or {}).get("rating") or "")).strip()
        if (not rating) and is_non_ratable_document_type(paper.get("document_type")):
            ok, msg = _ensure_non_ratable_rating(pid, paper, execute=execute)
            if item is not None:
                item["non_ratable_fix"] = msg
            paper = papers_db.get_paper(pid) or paper
            complete, reason = is_ingest_evaluation_complete(paper)
            if complete:
                summary["already_complete"] += 1
                if item is not None:
                    item["complete"] = True
                    summary["items"].append(item)
                continue

        patched, patch_msg = _patch_missing_red_team_notes(paper, execute=execute)
        if patched:
            summary["red_team_patched"] += 1
            print(f"  red-team patch: {pid} ({patch_msg})", flush=True)
            paper = papers_db.get_paper(pid) or paper
            complete, reason = is_ingest_evaluation_complete(paper)
            if complete:
                summary["eval_fixed"] += 1
                if item is not None:
                    item["complete"] = True
                    item["patch"] = patch_msg
                    summary["items"].append(item)
                continue

        if text_only:
            summary["still_incomplete"] += 1
            summary["reasons"][reason] = int(summary["reasons"].get(reason, 0)) + 1
            if len(summary["failures_sample"]) < 50:
                summary["failures_sample"].append({"paper_id": pid, "reason": reason, "text": text_msg})
            if item is not None:
                item["complete"] = False
                item["reason"] = reason
                summary["items"].append(item)
            continue

        if not allow_paid_api:
            summary["still_incomplete"] += 1
            summary["reasons"][reason] = int(summary["reasons"].get(reason, 0)) + 1
            if len(summary["failures_sample"]) < 50:
                summary["failures_sample"].append({"paper_id": pid, "reason": reason})
            if item is not None:
                item["complete"] = False
                item["reason"] = reason
                summary["items"].append(item)
            continue

        eval_len = _evaluable_text_len(paper)
        if eval_len < min_eval_chars:
            reason = f"insufficient_text_for_eval:{eval_len}"
            summary["eval_skipped_no_text"] += 1
            summary["still_incomplete"] += 1
            summary["reasons"][reason] = int(summary["reasons"].get(reason, 0)) + 1
            if len(summary["failures_sample"]) < 50:
                summary["failures_sample"].append({"paper_id": pid, "reason": reason})
            if item is not None:
                item["complete"] = False
                item["reason"] = reason
                summary["items"].append(item)
            continue

        summary["eval_attempted"] += 1
        if execute and client is not None:
            print(f"  eval: {pid} ({eval_model}, {eval_len}c)", flush=True)
            ev = evaluate_one(
                client,
                pid,
                force=True,
                require_research_rating=True,
                allow_blocked_eval=True,
                min_text_chars=min_eval_chars,
                fast_mode=fast_eval,
                fast_model_key=eval_model if fast_eval else None,
            )
            if item is not None:
                item["eval"] = {
                    "ok": bool(ev.get("ok")) if isinstance(ev, dict) else bool(ev),
                    "usage": ev if isinstance(ev, dict) else {},
                }
        elif item is not None:
            item["eval"] = {"ok": False, "usage": {}, "planned": True}

        paper = papers_db.get_paper(pid) or paper
        complete, reason = is_ingest_evaluation_complete(paper)
        if complete:
            summary["eval_fixed"] += 1
        else:
            summary["eval_failed"] += 1
            summary["still_incomplete"] += 1
            summary["reasons"][reason] = int(summary["reasons"].get(reason, 0)) + 1
            if len(summary["failures_sample"]) < 50:
                summary["failures_sample"].append({"paper_id": pid, "reason": reason})

        if item is not None:
            item["complete"] = complete
            item["reason"] = reason
            summary["items"].append(item)

    # Text-only runs succeed if we processed rows; eval completeness optional.
    if text_only:
        summary["ok"] = True
    else:
        summary["ok"] = summary["still_incomplete"] == 0
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill DB completeness for ingest text/evaluation.")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-paid-api", action="store_true")
    parser.add_argument("--min-text-chars", type=int, default=200)
    parser.add_argument("--min-eval-chars", type=int, default=200)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", type=str, default="")
    parser.add_argument(
        "--fast",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Single-pass fast ratings with synthetic red-team notes (default: on).",
    )
    parser.add_argument(
        "--eval-model",
        default="",
        help="Model key from llm_providers (default: corpus profile / config.EVAL_MODEL_KEY).",
    )
    parser.add_argument("--text-only", action="store_true", help="Only backfill extractable text; skip API eval.")
    parser.add_argument(
        "--full-items",
        action="store_true",
        help="Include per-row items in JSON (off by default on --execute for speed).",
    )
    args = parser.parse_args()

    record_items = bool(args.full_items) or not args.execute
    fast_eval = bool(args.fast) and not args.text_only
    import config

    eval_model = _resolve_eval_model(str(args.eval_model or config.EVAL_MODEL_KEY))

    out = run(
        execute=bool(args.execute),
        allow_paid_api=bool(args.allow_paid_api),
        min_text_chars=int(args.min_text_chars),
        min_eval_chars=int(args.min_eval_chars),
        limit=args.limit,
        fast_eval=fast_eval,
        eval_model=eval_model,
        text_only=bool(args.text_only),
        record_items=record_items,
    )
    if args.out:
        p = Path(args.out)
        if not p.is_absolute():
            p = config.MASTER_PROJECT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "items"}, indent=2, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
