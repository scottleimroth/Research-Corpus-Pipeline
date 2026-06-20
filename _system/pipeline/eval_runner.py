#!/usr/bin/env python3
"""
Single-process evaluation orchestrator (dry-run default).

- One paper at a time when live mode enabled later
- Skips already-evaluated papers
- Logs to eval_jobs.jsonl
- No parallel workers
- No DB writes unless EVAL_ALLOW_DB_WRITE=1

Usage:
  python eval_runner.py --dry-run --count 10
  python eval_runner.py --paper-id X --dry-run
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_constants import ALLOW_DB_WRITE, ALLOW_LIVE_API  # noqa: E402
from eval_ledger import append_event, is_saved  # noqa: E402
from eval_redteam_policy import should_red_team  # noqa: E402
from eval_routing import choose_first_pass_model, needs_sonnet_escalation  # noqa: E402
from evaluate_paper import build_evaluation_input, get_unevaluated_papers, is_already_evaluated  # noqa: E402
import papers_db  # noqa: E402


def plan_paper(paper_id: str, *, pilot_haiku: bool = False) -> dict:
    paper = papers_db.get_paper(paper_id)
    if not paper:
        return {"paper_id": paper_id, "error": "not_found"}
    if is_already_evaluated(paper) or is_saved(paper_id):
        return {"paper_id": paper_id, "action": "skip", "reason": "already_evaluated"}

    text, input_plan = build_evaluation_input(paper)
    if input_plan.get("strategy") == "insufficient_text":
        return {"paper_id": paper_id, "action": "skip", "reason": "insufficient_text"}

    model = choose_first_pass_model(pilot_haiku_validated=pilot_haiku)
    plan = {
        "paper_id": paper_id,
        "action": "evaluate",
        "first_pass_model": model,
        "input_plan": input_plan,
        "input_chars": len(text),
        "escalation": "deferred_until_first_pass_result",
        "red_team": "conditional_after_first_pass",
    }
    return plan


def main():
    parser = argparse.ArgumentParser(description="Evaluation orchestrator")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--live", action="store_true", help="Run live API (requires flags)")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--paper-id", type=str)
    parser.add_argument("--pilot-haiku", action="store_true")
    args = parser.parse_args()

    live = args.live
    if live and (not ALLOW_LIVE_API or not ALLOW_DB_WRITE):
        print("ERROR: --live requires EVAL_ALLOW_LIVE_API=1 and EVAL_ALLOW_DB_WRITE=1")
        sys.exit(1)

    if args.paper_id:
        ids = [args.paper_id]
    else:
        ids = [p["id"] for p in get_unevaluated_papers()[: args.count]]

    plans = []
    for pid in ids:
        p = plan_paper(pid, pilot_haiku=args.pilot_haiku)
        plans.append(p)
        append_event(pid, "queued", extra={"dry_run": not live, "plan": p})
        if p.get("action") == "skip":
            append_event(pid, "skipped", extra={"reason": p.get("reason")})
            print(f"SKIP {pid}: {p.get('reason')}")
        else:
            ip = p.get("input_plan", {})
            print(
                f"PLAN {pid}: {ip.get('strategy')} {ip.get('input_chars')}c "
                f"model={p.get('first_pass_model')}"
            )

    if not live:
        print(f"\nDRY RUN: planned {len(plans)} papers. No API calls, no DB writes.")
        return

    # Live path intentionally not implemented in this phase — use evaluate_paper.py
    # per paper until batch pipeline is approved.
    print("Live orchestration deferred; use evaluate_paper.py --paper-id after approval.")


if __name__ == "__main__":
    main()
