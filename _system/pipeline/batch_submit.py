#!/usr/bin/env python3
"""
Plan or submit Anthropic Message Batches for evaluation (dry-run by default).

Usage:
  python batch_submit.py --dry-run --count 10
  python batch_submit.py --paper-id Smith2024 --dry-run
  python batch_submit.py --submit --count 5   # requires EVAL_ALLOW_LIVE_API=1

No papers.db writes in any mode.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_batch_common import build_first_pass_request, write_plan_file  # noqa: E402
from eval_constants import ALLOW_LIVE_API, MODEL_SONNET, USE_SCHEMA_V2  # noqa: E402
from eval_prompts import caching_report  # noqa: E402
from evaluate_paper import build_evaluation_input, get_unevaluated_papers  # noqa: E402
import papers_db  # noqa: E402


def build_user_content(paper_id: str, paper: dict, text: str) -> str:
    bm = paper.get("basic_metadata", {})
    title = bm.get("title", "Unknown")
    authors = bm.get("authors", [])
    year = bm.get("year", "")
    journal = bm.get("journal", "")
    author_str = ", ".join(authors) if isinstance(authors, list) else str(authors)
    header = f"Title: {title}\nAuthors: {author_str}\nYear: {year}\nJournal: {journal}\n\n"
    return (
        f"Evaluate this paper. Return the COMPLETE JSON schema.\n\n"
        f"{header}{text}\n\n"
        f"Return ONLY the JSON evaluation. No markdown fences."
    )


def plan_requests(paper_ids: list[str], model: str) -> list[dict]:
    requests = []
    for pid in paper_ids:
        paper = papers_db.get_paper(pid)
        if not paper:
            continue
        text, plan = build_evaluation_input(paper)
        if plan.get("strategy") == "insufficient_text" or not text:
            continue
        user_content = build_user_content(pid, paper, text)
        req = build_first_pass_request(
            pid,
            model=model,
            user_content=user_content,
            schema_v2=USE_SCHEMA_V2,
        )
        req["meta"]["input_plan"] = plan
        requests.append(req)
    return requests


def main():
    parser = argparse.ArgumentParser(description="Submit or plan evaluation batches")
    parser.add_argument("--submit", action="store_true", help="Submit batch to Anthropic (live)")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--paper-id", type=str)
    parser.add_argument("--model", type=str, default=MODEL_SONNET)
    args = parser.parse_args()

    dry_run = not args.submit  # default: dry-run
    if args.submit and not ALLOW_LIVE_API:
        print("ERROR: --submit requires EVAL_ALLOW_LIVE_API=1")
        sys.exit(1)

    if args.paper_id:
        ids = [args.paper_id]
    else:
        ids = [p["id"] for p in get_unevaluated_papers()[: args.count]]

    requests = plan_requests(ids, args.model)
    plan_path = write_plan_file("first_pass_plan", requests)
    print(f"Planned {len(requests)} first-pass requests -> {plan_path}")
    print(json.dumps(caching_report(), indent=2))

    if dry_run:
        print("\nDRY RUN: no batch submitted, no API calls, no DB writes.")
        return

    # Live submit path (disabled unless explicitly enabled)
    import anthropic  # noqa: WPS433

    client = anthropic.Anthropic()
    batch_requests = [
        {"custom_id": r["custom_id"], "params": r["params"]} for r in requests
    ]
    batch = client.messages.batches.create(requests=batch_requests)
    meta_path = plan_path.with_name("last_batch_meta.json")
    meta_path.write_text(
        json.dumps({"batch_id": batch.id, "count": len(batch_requests)}, indent=2),
        encoding="utf-8",
    )
    print(f"Submitted batch {batch.id}")


if __name__ == "__main__":
    main()
