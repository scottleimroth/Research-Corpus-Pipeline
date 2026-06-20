"""Map existing scripts to new one-command corpus pipeline stages."""

from __future__ import annotations

REUSED_DIRECTLY = {
    "add_paper.py": "Ingest logic for hash/metadata/text extraction/duplicate checks (needs wrapper for no-side-effect planning)",
    "metadata_extractors.py": "Tiered metadata extraction",
    "papers_db.py": "DB interface and backup",
    "evaluate_paper.py": "Same-depth evaluation input + validation + canonical ratings",
    "eval_constants.py": "Option B constants and flags",
    "eval_schema_v2.py": "Compact schema validation",
    "eval_prompts.py": "Prompt caching plan",
    "eval_routing.py": "Model escalation rules",
    "eval_redteam_policy.py": "Conditional red-team",
    "eval_batch_common.py": "Batch request and custom_id",
    "eval_ledger.py": "Idempotent run ledger",
    "cost_estimate.py": "A/B/C estimates",
}

NEEDS_WRAPPER_OR_REPLACEMENT = {
    "add_paper.py": "Currently performs writes/moves inline; needs pure planning mode for run_corpus_pipeline",
    "batch_submit.py": "Needs mode selection and integrated run orchestration hooks",
    "batch_collect.py": "Needs save-isolation path after approval (validated-only writes)",
    "eval_runner.py": "Needs full end-to-end stage transitions and resume semantics",
    "phase_2* scripts": "Historical maintenance scripts; not one-command UX",
}


def build_reuse_report() -> dict:
    return {
        "reused_directly": REUSED_DIRECTLY,
        "needs_wrapper_or_replacement": NEEDS_WRAPPER_OR_REPLACEMENT,
    }
