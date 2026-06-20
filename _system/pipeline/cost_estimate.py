#!/usr/bin/env python3
"""
Read-only cost estimator for remaining evaluation queue.

Calibrated from 10-paper batch logs (Sonnet full_text_within_context × 2).

Usage:
  python cost_estimate.py
  python cost_estimate.py --remaining 871
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_constants import CALIBRATION, EVAL_COST_REPORT_PATH, MODEL_HAIKU, MODEL_SONNET, PRICE  # noqa: E402
from evaluate_paper import get_unevaluated_papers  # noqa: E402


def _cost(model: str, in_tok: int, out_tok: int, *, batch: bool = False) -> float:
    p = PRICE[model]
    disc = p["batch_discount"] if batch else 1.0
    return (in_tok / 1e6 * p["input"] * disc) + (out_tok / 1e6 * p["output"] * disc)


def estimate_option(
    n_papers: int,
    *,
    name: str,
    first_pass_model: str,
    first_pass_calls: int,
    redteam_fraction: float,
    escalation_fraction: float = 0.0,
    escalation_model: str | None = None,
    batch: bool = False,
    cache_savings_input: float = 0.0,
    output_reduction: float = 0.0,
) -> dict:
    in_per_call = CALIBRATION["avg_input_tokens_per_call"]
    out_per_call = CALIBRATION["avg_output_tokens_per_call"]

    if output_reduction:
        out_per_call = int(out_per_call * (1 - output_reduction))

    first_in = n_papers * first_pass_calls * in_per_call
    first_out = n_papers * first_pass_calls * out_per_call
    first_cost = _cost(first_pass_model, first_in, first_out, batch=batch)

    esc_cost = 0.0
    esc_in = esc_out = 0
    if escalation_fraction and escalation_model:
        esc_n = int(n_papers * escalation_fraction)
        esc_in = esc_n * in_per_call
        esc_out = esc_n * out_per_call
        esc_cost = _cost(escalation_model, esc_in, esc_out, batch=batch)

    rt_n = int(n_papers * redteam_fraction)
    rt_in = rt_n * in_per_call
    rt_out = rt_n * int(out_per_call * 0.6)  # red-team outputs shorter
    rt_cost = _cost(MODEL_SONNET, rt_in, rt_out, batch=batch)

    total_in = first_in + esc_in + rt_in
    total_out = first_out + esc_out + rt_out
    if cache_savings_input:
        total_in = int(total_in * (1 - cache_savings_input))

    total_cost = first_cost + esc_cost + rt_cost
    if cache_savings_input:
        # apply cache savings on rubric-heavy input portion (~40% of input)
        total_cost *= 1 - (cache_savings_input * 0.35)

    api_calls = (
        n_papers * first_pass_calls
        + int(n_papers * escalation_fraction)
        + rt_n
    )

    minutes_per_paper = 3.5  # from 10-paper batch ~35 min / 10
    if batch:
        wall_hours = max(2, (n_papers / 200) * 4)  # async batch throughput rough
    else:
        wall_hours = (n_papers * minutes_per_paper) / 60

    return {
        "option": name,
        "papers": n_papers,
        "api_calls": api_calls,
        "first_pass_model": first_pass_model,
        "escalation_fraction": escalation_fraction,
        "redteam_papers": rt_n,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cost_usd": round(total_cost, 2),
        "wall_clock_hours_est": round(wall_hours, 1),
        "batch_api": batch,
        "cache_savings_input_fraction": cache_savings_input,
        "output_reduction_fraction": output_reduction,
    }


def main():
    parser = argparse.ArgumentParser(description="Cost estimator (read-only)")
    parser.add_argument("--remaining", type=int, default=0)
    args = parser.parse_args()

    n = args.remaining or len(get_unevaluated_papers())
    per_paper_current = CALIBRATION["cost_usd_single_run_10"] / CALIBRATION["papers"]

    options = {
        "current_sonnet_immediate_redteam": estimate_option(
            n,
            name="Current (Sonnet eval + red-team all)",
            first_pass_model=MODEL_SONNET,
            first_pass_calls=1,
            redteam_fraction=1.0,
            batch=False,
        ),
        "option_a": estimate_option(
            n,
            name="Option A — Sonnet first-pass all, conditional red-team 45%",
            first_pass_model=MODEL_SONNET,
            first_pass_calls=1,
            redteam_fraction=0.45,
            batch=True,
            cache_savings_input=0.35,
            output_reduction=0.25,
        ),
        "option_b": estimate_option(
            n,
            name="Option B — Haiku first-pass all, Sonnet escalation 25%, red-team 18%",
            first_pass_model=MODEL_HAIKU,
            first_pass_calls=1,
            escalation_fraction=0.25,
            escalation_model=MODEL_SONNET,
            redteam_fraction=0.18,
            batch=True,
            cache_savings_input=0.40,
            output_reduction=0.40,
        ),
        "option_c": estimate_option(
            n,
            name="Option C — Haiku first-pass all, Sonnet escalation 15%, red-team 10%",
            first_pass_model=MODEL_HAIKU,
            first_pass_calls=1,
            escalation_fraction=0.15,
            escalation_model=MODEL_SONNET,
            redteam_fraction=0.10,
            batch=True,
            cache_savings_input=0.40,
            output_reduction=0.45,
        ),
    }

    report = {
        "remaining_papers": n,
        "calibration": CALIBRATION,
        "per_paper_current_sonnet_2pass_usd": round(per_paper_current, 3),
        "linear_extrapolation_current_usd": round(per_paper_current * n, 2),
        "duplicate_run_note": (
            "10-paper batch with interruptions cost ~$6-8 vs ~$3-4 clean; "
            "runner idempotency required."
        ),
        "options": options,
        "recommended": "option_b",
        "option_b_target_feasibility": options["option_b"]["cost_usd"] <= 120,
    }

    EVAL_COST_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVAL_COST_REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nWrote {EVAL_COST_REPORT_PATH}")


if __name__ == "__main__":
    main()
