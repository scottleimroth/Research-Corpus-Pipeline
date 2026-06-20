#!/usr/bin/env python3
"""Double-click launcher for all-staging Sonnet ingest + Opus review resolution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from launcher_cleanup import (
    PROJECT_ROOT,
    SYSTEM_ROOT,
    ensure_project_cwd,
    make_run_id,
    request_stop,
    run_pre_launch_cleanup,
)

RUN_PREFIX = "all-staging-run"


def _pause_if_double_click() -> None:
    if os.environ.get("CORPUS_RUNNER_NO_PAUSE"):
        return
    if "--no-pause" in sys.argv:
        return
    if sys.platform == "win32":
        try:
            input("\nPress Enter to close...")
        except (EOFError, KeyboardInterrupt):
            pass


def _print_cost(summary: dict) -> None:
    est = summary.get("estimated_total_cost_usd")
    act = summary.get("actual_cost_so_far_usd") or summary.get("total_cost_usd")
    rate = summary.get("usd_to_aud_rate")
    if est is not None:
        if rate:
            est_aud = summary.get("estimated_total_cost_aud") or round(float(est) * float(rate), 2)
            print(f"  Estimated cost: ${float(est):.2f} USD / ${float(est_aud):.2f} AUD", flush=True)
        else:
            print(f"  Estimated cost: ${float(est):.2f} USD", flush=True)
    if act is not None:
        if rate:
            act_aud = summary.get("actual_cost_so_far_aud") or summary.get("total_cost_aud")
            if act_aud is not None:
                print(f"  Actual cost: ${float(act):.2f} USD / ${float(act_aud):.2f} AUD", flush=True)
            else:
                print(f"  Actual cost: ${float(act):.2f} USD", flush=True)
        else:
            print(f"  Actual cost: ${float(act):.2f} USD", flush=True)


def _print_run_summary(run_dir: Path) -> None:
    live_path = run_dir / "all_staging_ingest_live.json"
    if not live_path.exists():
        return
    try:
        summary = json.loads(live_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    print("\nRun summary:", flush=True)
    print(f"  Status: {summary.get('status')}", flush=True)
    print(f"  Raw staging candidates: {summary.get('raw_staging_candidates_before_preflight')}", flush=True)
    print(f"  Preflight duplicates deleted: {summary.get('preflight_duplicates_deleted')}", flush=True)
    print(f"  Processed unique candidates: {summary.get('processed_count')} / {summary.get('unique_candidates_to_process')}", flush=True)

    for phase in ("pre_review", "post_review"):
        start = summary.get(f"{phase}_review_items_start")
        if start is None:
            continue
        checked = summary.get(f"{phase}_review_items_resolved")
        remaining = summary.get(f"{phase}_review_items_remaining_technical_failure")
        print(f"  {phase} review: checked={checked} start={start} remaining={remaining}", flush=True)

    if summary.get("post_review_moved_to_staging_for_next_run"):
        print(
            f"  Moved to staging (next run): {summary.get('post_review_moved_to_staging_for_next_run')}",
            flush=True,
        )

    _print_cost(summary)


def main() -> int:
    ensure_project_cwd()

    profile_file = SYSTEM_ROOT / "corpus_profile.json"
    paid_ai_required = True
    api_mode = "openrouter"
    if profile_file.exists():
        try:
            profile = json.loads(profile_file.read_text(encoding="utf-8"))
            api_mode = str(profile.get("api_mode") or "openrouter").lower()
            paid_ai_required = api_mode not in {"ollama_local", "local", "none"}
        except (OSError, json.JSONDecodeError):
            paid_ai_required = True

    if paid_ai_required:
        sys.path.insert(0, str(SYSTEM_ROOT / "pipeline"))
        from pipeline_llm import paid_api_ready

        ok_paid, msg = paid_api_ready()
        if not ok_paid:
            print(f"ERROR: {msg}", flush=True)
            _pause_if_double_click()
            return 1

    run_id = make_run_id(RUN_PREFIX)
    run_dir = SYSTEM_ROOT / "logs" / "runs" / run_id

    cleanup = run_pre_launch_cleanup(exclude_run_ids={run_id})
    if cleanup["logs"]["warnings"] or cleanup["backups"]["warnings"]:
        print("Pre-run cleanup completed with warnings (run continues).", flush=True)

    cmd = [
        sys.executable,
        "pipeline/run_corpus_pipeline.py",
        "--workflow",
        "ingest",
        "--mode",
        "A",
        "--workflow-target",
        "all-staging",
        "--run-id",
        run_id,
        "--execute",
        "--allow-db-write",
        "--allow-pdf-copy",
    ]
    if paid_ai_required:
        cmd.append("--allow-paid-api")
        if api_mode == "anthropic":
            cmd.append("--allow-opus-resolution")
    else:
        cmd.append("--allow-local-ollama")

    print(f"Run ID: {run_id}", flush=True)
    print(f"Log folder: {run_dir}", flush=True)
    print("Preflight dedupe first; post-run review resolution enabled.", flush=True)
    print(f"Command: {' '.join(cmd)}", flush=True)
    print("", flush=True)

    code = 1
    try:
        proc = subprocess.run(cmd, cwd=SYSTEM_ROOT)
        code = proc.returncode
    except KeyboardInterrupt:
        print("\nCtrl+C — stop requested.", flush=True)
        request_stop(run_dir)
        code = 130
    except Exception as e:
        print(f"ERROR: Failed to start pipeline: {e}", flush=True)
        _pause_if_double_click()
        return 1

    _print_run_summary(run_dir)

    if code in (0, 130):
        print(f"\nStopped/finished. Logs: {run_dir}", flush=True)
    else:
        print(f"\nFAILED: Exit code {code}. Check logs: {run_dir}", flush=True)

    _pause_if_double_click()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
