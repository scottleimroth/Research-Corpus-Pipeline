# CorpusPipeline Tenets and Error Guardrails

This file records operational tenets and concrete regressions that must not recur.

## Core Tenets

1. Preserve user data over throughput.
2. Never silently delete potentially valid staging files.
3. Prefer deterministic routing and explicit status over implicit retries.
4. Keep behavior portable and path-safe across LIVE/BACKUP/PUBLIC.
5. Any terminal delete must be auditable and reversible where possible.

## Never Again - Known Failure Modes

1. **Staging rename cascade (`unknown_unknown...`)**
   - Symptom: same file repeatedly re-queued with increasingly generic names.
   - Harm: destroys filename signals (thesis/manual/supplement cues), increases misclassification risk.
   - Guardrail: no re-queue rename loops for staging-origin files; preserve original filename when possible.

2. **Coverage block forced into terminal deletion**
   - Symptom: `REJECT_NEEDS_RECOVERY`/`PARTIAL_NEEDS_RECOVERY` routed into terminal unrecoverable delete.
   - Harm: valid files removed instead of retained for recovery or next run.
   - Guardrail: coverage/evidence blocks must go through recovery path and default to returned-to-staging unless truly unrecoverable.

3. **False integrity failure from valid extraction**
   - Symptom: `extractor_status=TEXT_OK` with short text flagged as `system_integrity_failure`.
   - Harm: run fails integrity despite normal pipeline conditions.
   - Guardrail: reserve `system_integrity_failure` for tool/path/runtime integrity issues, not content sufficiency.

4. **Early acceptance gate before full extraction**
   - Symptom: preflight acceptance blocks DB insert prior to full extraction attempt.
   - Harm: valid papers never reach full-text/coverage chance.
   - Guardrail: when preflight block is recoverable (`PARTIAL_NEEDS_RECOVERY`), continue to full extraction before final reject.

5. **Unclear destination after terminal action**
   - Symptom: user cannot tell where file went after pipeline run.
   - Harm: trust and recoverability degrade.
   - Guardrail: every non-success path must emit explicit destination/action in run artifacts.

## Runtime Safety Checks (Required)

1. Before run: verify no active ingest lock conflict.
2. During run: classify each item into one of:
   - added to DB
   - confirmed duplicate deleted
   - child linked/pending
   - returned to staging
   - terminal deleted (with explicit reason)
3. After run: fail only for real integrity issues; unresolved content should not auto-fail integrity.
4. Keep `papers-staging` outcomes explainable from `all_staging_ingest_report.json`.

## Change Protocol

When fixing ingest logic:
1. patch LIVE,
2. compile/smoke test,
3. sync same patch to BACKUP and PUBLIC,
4. run a controlled pass and inspect item-level outcomes.
