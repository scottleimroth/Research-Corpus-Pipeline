"""Stage I: run-level duplicate-skip audit log only.

This is the lowest-risk live pilot: it accepts duplicate-skip decisions into
run artifacts only. It does not write papers.db, modify existing paper rows,
move/copy/delete PDFs, call APIs, evaluate papers, or commit IDs.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

DUPLICATE_NAMES = {
    "Levitin-The Psychology of Music- Rhythm and Movement-2017-An.pdf",
    "Suarez‐Roca-Comprehensive Physiology-2022-Comprehensive Phys.pdf",
    "Yücel-Best practices for fNIRS publications-2021-Neurophot_1.pdf",
}

NON_PROCESSED_NAMES = {
    "Harris-Effect of pegylation on pharmaceuticals-2003-Nature R.pdf",
    "Koren-A kinetic study of protein-protein interactions-1976-B.pdf",
}


def _load_final_plan(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "final_pilot_execution_plan.json").read_text(encoding="utf-8"))


def _future_commands(run_id: str) -> list[dict[str, Any]]:
    return [
        {
            "label": "optional future duplicate archive/move command",
            "do_not_run_yet": True,
            "command_preview": (
                f"python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 "
                f"--run-id {run_id} --execute-stage live-duplicate-archive-only --allow-pdf-copy"
            ),
        },
        {
            "label": "optional future Koren parent-record pilot command",
            "do_not_run_yet": True,
            "command_preview": (
                f"python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 "
                f"--run-id {run_id} --execute-stage live-parent-record-pilot --parent-candidate Koren "
                "--allow-db-write --allow-pdf-copy"
            ),
        },
        {
            "label": "optional future Harris machine-escalation continuation command",
            "do_not_run_yet": True,
            "command_preview": (
                f"python pipeline\\run_corpus_pipeline.py --execute --mode A --max-papers 5 "
                f"--run-id {run_id} --execute-stage book-toc-chapter-preview"
            ),
        },
    ]


def _validate_actions(actions: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    by_name = {str(a.get("name")): a for a in actions}
    if set(by_name) != DUPLICATE_NAMES | NON_PROCESSED_NAMES:
        errors.append("final plan does not contain exactly the expected 5 pilot PDFs")

    for name in DUPLICATE_NAMES:
        action = by_name.get(name)
        if not action:
            errors.append(f"missing duplicate action: {name}")
            continue
        if action.get("action_class") != "live_duplicate_skip_log_candidate":
            errors.append(f"{name} is not a duplicate-skip log candidate")
        if not action.get("matched_existing_db_id"):
            errors.append(f"{name} has no matched existing DB ID")

    for name in NON_PROCESSED_NAMES:
        action = by_name.get(name)
        if action and action.get("action_class") == "live_duplicate_skip_log_candidate":
            errors.append(f"{name} must not be processed by duplicate-skip stage")

    return not errors, errors


def run_live_duplicate_skip_log_only(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    final_plan = _load_final_plan(run_dir)
    actions = final_plan.get("consolidated_actions", [])
    ok, errors = _validate_actions(actions)
    if not ok:
        raise RuntimeError("duplicate-skip log preconditions failed: " + "; ".join(errors))

    accepted = []
    for action in actions:
        name = action.get("name")
        if name not in DUPLICATE_NAMES:
            continue
        accepted.append(
            {
                "staged_filename": name,
                "matched_existing_db_id": action.get("matched_existing_db_id"),
                "evidence_summary": action.get("evidence_summary", {}),
                "decision": "accepted_exact_duplicate_skip",
                "db_action": "none",
                "pdf_action": "none",
                "evaluation_action": "none",
                "id_action": "none",
                "future_optional_action": "staged file can later be moved to duplicate archive only after explicit approval",
            }
        )

    summary = {
        "run_id": run_dir.name,
        "stage": "live-duplicate-skip-log-only",
        "timestamp": datetime.now().isoformat(),
        "source_artifact": str(run_dir / "final_pilot_execution_plan.json"),
        "accepted_duplicate_skip_decisions": accepted,
        "not_processed_by_design": sorted(NON_PROCESSED_NAMES),
        "fail_closed_guards": {
            "db_writes_allowed": False,
            "pdf_actions_allowed": False,
            "api_calls_allowed": False,
            "evaluations_allowed": False,
            "id_changes_allowed": False,
            "parent_record_creation_allowed": False,
            "mixed_pilot_allowed": False,
        },
        "future_commands_preview_do_not_run_yet": _future_commands(run_dir.name),
        "side_effects": {
            "api_calls": "none",
            "papers_db_writes": "none",
            "existing_db_row_modifications": "none",
            "new_db_rows": "none",
            "pdf_moves_copies_renames_deletes": "none",
            "id_changes": "none",
            "evaluations": "none",
            "batch_jobs": "none",
        },
    }
    (run_dir / "live_duplicate_skip_log.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "stage_live_duplicate_skip_log_report.txt").write_text(render_live_duplicate_skip_log_report(summary), encoding="utf-8")
    return summary


def render_live_duplicate_skip_log_report(summary: dict[str, Any]) -> str:
    lines = ["Stage I Live Duplicate-Skip Log Only", "=" * 46]
    lines.append("Run-level audit artifact only. No DB rows or PDFs were modified.")
    for item in summary["accepted_duplicate_skip_decisions"]:
        lines.append(f"- {item['staged_filename']}")
        lines.append(f"  matched_existing_db_id: {item['matched_existing_db_id']}")
        lines.append(f"  decision: {item['decision']}")
        lines.append("  actions: DB=none, PDF=none, evaluation=none, ID=none")
    lines.append("")
    lines.append(f"Not processed by design: {summary['not_processed_by_design']}")
    lines.append("")
    lines.append("Future commands shown but DO NOT RUN YET:")
    for command in summary["future_commands_preview_do_not_run_yet"]:
        lines.append(f"- {command['label']}: {command['command_preview']}")
    lines.append("")
    lines.append("No API calls, papers.db writes, existing-row modifications, new rows, PDF operations, evaluations, batch jobs, or ID changes occurred.")
    return "\n".join(lines) + "\n"
