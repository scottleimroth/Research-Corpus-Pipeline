"""Run cost estimation and token-based pricing for Sonnet/Opus."""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

import config

SONNET_INPUT_USD_PER_MILLION = 3.00
SONNET_OUTPUT_USD_PER_MILLION = 15.00
HAIKU_INPUT_USD_PER_MILLION = 1.00
HAIKU_OUTPUT_USD_PER_MILLION = 5.00


def fetch_usd_to_aud_rate() -> tuple[float, str]:
    """Fetch USD→AUD once; fallback to env/config constant."""
    try:
        req = urllib.request.Request(
            "https://api.frankfurter.app/latest?from=USD&to=AUD",
            headers={"User-Agent": "CorpusPipeline/1.0"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            rate = float(data["rates"]["AUD"])
            if rate > 0:
                return rate, "live"
    except Exception:
        pass
    fallback = float(os.environ.get("USD_TO_AUD", str(config.USD_TO_AUD)))
    return fallback, "fallback"


def sonnet_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return round(
        (input_tokens / 1_000_000) * SONNET_INPUT_USD_PER_MILLION
        + (output_tokens / 1_000_000) * SONNET_OUTPUT_USD_PER_MILLION,
        6,
    )


def haiku_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return round(
        (input_tokens / 1_000_000) * HAIKU_INPUT_USD_PER_MILLION
        + (output_tokens / 1_000_000) * HAIKU_OUTPUT_USD_PER_MILLION,
        6,
    )


def opus_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return round(
        (input_tokens / 1_000_000) * config.OPUS_INPUT_USD_PER_MTOK
        + (output_tokens / 1_000_000) * config.OPUS_OUTPUT_USD_PER_MTOK,
        6,
    )


def usd_to_aud(usd: float, rate: float | None) -> float | None:
    if rate is None or rate <= 0:
        return None
    return round(usd * rate, 2)


def update_summary_costs(summary: dict[str, Any]) -> None:
    """Populate standard cost fields on the live summary dict."""
    rate = summary.get("usd_to_aud_rate")
    if rate is None:
        rate = float(os.environ.get("USD_TO_AUD", str(config.USD_TO_AUD)))
        summary["usd_to_aud_rate"] = rate

    processed = int(summary.get("processed_count") or len(summary.get("items") or []))
    total = int(summary.get("starting_staging_count") or 0)

    estimated_total = summary.get("estimated_total_cost_usd")
    if estimated_total is None:
        new_candidates = summary.get("new_ingest_candidates")
        if new_candidates is not None:
            estimated_total = round(float(new_candidates) * config.SONNET_ESTIMATE_USD_PER_PAPER, 4)
            summary["estimated_total_cost_usd"] = estimated_total

    sonnet_usd = float(summary.get("sonnet_cost_usd") or 0)
    haiku_usd = float(summary.get("haiku_cost_usd") or 0)
    opus_usd = float(summary.get("opus_cost_usd") or 0)
    pre_opus = float(summary.get("pre_review_opus_cost_usd") or 0)
    post_opus = float(summary.get("post_review_opus_cost_usd") or 0)
    if opus_usd == 0 and (pre_opus or post_opus):
        opus_usd = pre_opus + post_opus
        summary["opus_cost_usd"] = round(opus_usd, 4)

    in_tok = int(summary.get("actual_input_tokens_so_far") or 0)
    out_tok = int(summary.get("actual_output_tokens_so_far") or 0)
    actual_usd = round(sonnet_usd + haiku_usd + opus_usd, 4)

    estimated_so_far = None
    if estimated_total is not None and total > 0:
        estimated_so_far = round(float(estimated_total) * processed / total, 4)
    elif estimated_total is not None:
        estimated_so_far = float(estimated_total)

    aud_ok = rate and rate > 0

    summary.update(
        actual_input_tokens_so_far=in_tok,
        actual_output_tokens_so_far=out_tok,
        actual_cost_so_far_usd=actual_usd,
        total_cost_usd=actual_usd,
        estimated_cost_so_far_usd=estimated_so_far,
        estimated_total_cost_aud=usd_to_aud(float(estimated_total), rate) if estimated_total and aud_ok else None,
        estimated_cost_so_far_aud=usd_to_aud(float(estimated_so_far), rate) if estimated_so_far is not None and aud_ok else None,
        actual_cost_so_far_aud=usd_to_aud(actual_usd, rate) if aud_ok else None,
        total_cost_aud=usd_to_aud(actual_usd, rate) if aud_ok else None,
        sonnet_cost_usd=round(sonnet_usd, 4),
        opus_cost_usd=round(opus_usd, 4),
        sonnet_cost_usd_estimate=estimated_so_far if estimated_so_far is not None else estimated_total,
    )


def accumulate_sonnet_usage(summary: dict[str, Any], *, input_tokens: int, output_tokens: int) -> None:
    summary["actual_input_tokens_so_far"] = int(summary.get("actual_input_tokens_so_far") or 0) + input_tokens
    summary["actual_output_tokens_so_far"] = int(summary.get("actual_output_tokens_so_far") or 0) + output_tokens
    add = sonnet_cost_usd(input_tokens, output_tokens)
    summary["sonnet_cost_usd"] = round(float(summary.get("sonnet_cost_usd") or 0) + add, 6)
    update_summary_costs(summary)


def accumulate_vision_usage(
    summary: dict[str, Any], *, input_tokens: int, output_tokens: int, model_id: str = ""
) -> None:
    """Route vision token costs to Haiku vs Sonnet buckets by model id."""
    mid = (model_id or "").lower()
    if "haiku" in mid:
        summary["haiku_input_tokens"] = int(summary.get("haiku_input_tokens") or 0) + input_tokens
        summary["haiku_output_tokens"] = int(summary.get("haiku_output_tokens") or 0) + output_tokens
        add = haiku_cost_usd(input_tokens, output_tokens)
        summary["haiku_cost_usd"] = round(float(summary.get("haiku_cost_usd") or 0) + add, 6)
        summary["actual_input_tokens_so_far"] = int(summary.get("actual_input_tokens_so_far") or 0) + input_tokens
        summary["actual_output_tokens_so_far"] = int(summary.get("actual_output_tokens_so_far") or 0) + output_tokens
    else:
        accumulate_sonnet_usage(summary, input_tokens=input_tokens, output_tokens=output_tokens)
        return
    update_summary_costs(summary)


def accumulate_opus_usage(summary: dict[str, Any], *, input_tokens: int, output_tokens: int, phase: str = "") -> None:
    add = opus_cost_usd(input_tokens, output_tokens)
    summary["opus_cost_usd"] = round(float(summary.get("opus_cost_usd") or 0) + add, 6)
    if phase == "pre_review":
        summary["pre_review_opus_cost_usd"] = round(float(summary.get("pre_review_opus_cost_usd") or 0) + add, 6)
    elif phase == "post_review":
        summary["post_review_opus_cost_usd"] = round(float(summary.get("post_review_opus_cost_usd") or 0) + add, 6)
    summary["opus_input_tokens"] = int(summary.get("opus_input_tokens") or 0) + input_tokens
    summary["opus_output_tokens"] = int(summary.get("opus_output_tokens") or 0) + output_tokens
    update_summary_costs(summary)
