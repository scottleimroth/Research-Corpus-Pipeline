"""Read-only summary of remaining staged PDFs from a workflow plan."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize(run_id: str) -> dict[str, Any]:
    run_dir = config.RUNS_DIR / run_id
    workflow = _load(run_dir / "workflow_plan.json")
    run_plan = _load(run_dir / "run_plan.json")
    items = workflow.get("items", [])
    selected_names = {item["name"] for item in items}
    all_staging = [
        p
        for p in config.STAGING.glob("*.pdf")
        if p.is_file()
    ]
    route_counts = {
        "ready_for_normal_ingest": 0,
        "duplicate_log_only": 0,
        "machine_escalation_needed": 0,
        "deferred_child_supplement_unknown": 0,
        "already_in_db": 0,
    }
    for item in items:
        route = item.get("route") or {}
        if route.get("duplicate_log_only"):
            route_counts["duplicate_log_only"] += 1
        elif route.get("machine_escalation_needed"):
            route_counts["machine_escalation_needed"] += 1
        elif route.get("workflow_status") == "planned":
            route_counts["ready_for_normal_ingest"] += 1
    shortfall = workflow.get("selection_shortfall") or {}
    breakdown = shortfall.get("breakdown_counts") or {}
    route_counts["already_in_db"] = int(breakdown.get("already_in_db") or 0)
    route_counts["deferred_child_supplement_unknown"] = (
        int(breakdown.get("supplement_or_child_item_excluded") or 0)
        + int(breakdown.get("other_or_unknown_requires_machine_escalation") or 0)
        + int(breakdown.get("document_type_excluded_by_selection_profile") or 0)
        + int(breakdown.get("text_not_likely_extractable") or 0)
    )
    route_counts["ready_for_normal_ingest"] += int(breakdown.get("eligible_but_not_selected_due_to_max_papers") or 0)
    return {
        "read_only": True,
        "run_id": run_id,
        "remaining_staging_pdf_count": len(all_staging),
        "selected_in_plan": len(selected_names),
        "counts": route_counts,
        "selected_items": sorted(selected_names),
        "selection_shortfall_counts": breakdown,
        "requested_max_papers": shortfall.get("requested_max_papers"),
        "side_effects": {"db_writes": "none", "pdf_operations": "none", "api_calls": "none", "evaluation": "none"},
    }


def main() -> None:
    run_id = sys.argv[1] if len(sys.argv) > 1 else ""
    if not run_id:
        raise SystemExit("usage: python pipeline/staging_summary_audit.py <run-id>")
    print(json.dumps(summarize(run_id), indent=2))


if __name__ == "__main__":
    main()
