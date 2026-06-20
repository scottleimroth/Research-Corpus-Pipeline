"""Build user-facing plan report for run_corpus_pipeline pre-scan."""

from __future__ import annotations

from typing import Any

import config


ESCALATION_LADDER = [
    "Normal automated processing",
    "Deeper automated checks",
    "Full-document/full-text agent comparison",
    "External metadata checks where available",
    "OCR when extraction is poor",
    "LLM/agent conflict resolution for duplicates/preprint-final/supplements",
    "Human review only if confidence remains below threshold or action is unsafe",
]


def build_pipeline_plan_report(
    *,
    prescan: dict[str, Any],
    standard: dict[str, Any],
    options: dict[str, dict[str, Any]],
    recommended: dict[str, str],
    selected_mode: str | None,
    args,
) -> dict[str, Any]:
    selected = selected_mode or recommended["mode"]

    planned_stages = [
        "Pre-scan (free/read-only): staging inventory, text-likelihood, duplicate signals, DB standard detection, A/B/C estimates",
        "User mode confirmation: A/B/C quality-cost selection before any paid calls or DB writes",
        "Internal safety initialization: backup policy, hash policy, schema checks, idempotent ledger, resume guards",
        "Ingest stage (planned): hash -> text extraction -> metadata extraction -> conflict checks -> safe IDs -> validation",
        "Machine-first escalation ladder for exceptions; human review only final fallback",
        "Evaluation stage (planned): same-depth first pass for every PDF, full text or evidence pack, conditional red-team",
        "Store/report stage (planned): validated saves, run ledger, cost/tokens, outcome report",
    ]

    paths = {
        "staging_input": args.staging_dir or str(config.STAGING),
        "managed_pdf_output": args.managed_pdf_dir or str(config.SOURCE_PDFS),
        "review_escalation": args.review_dir or str(config.REVIEW_DIR),
        "database": args.db_path or str(config.DB_PATH),
        "backups": args.backups_dir or str(config.BACKUP_DIR),
        "logs": args.logs_dir or str(config.STAGING_LOGS),
        "runs_ledger": args.runs_dir or str(config.STAGING / "eval-batch"),
    }

    return {
        "mode_selected": selected,
        "mode_recommended": recommended,
        "prescan": prescan,
        "existing_db_standard": standard,
        "mode_options": options,
        "paths": paths,
        "planned_stages": planned_stages,
        "escalation_ladder": ESCALATION_LADDER,
        "human_review_trigger": (
            "Only when machine escalation steps fail, confidence remains below threshold, "
            "or destructive ambiguity cannot be safely resolved."
        ),
        "read_only": True,
    }
