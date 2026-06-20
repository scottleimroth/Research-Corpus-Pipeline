# Research Papers Pipeline: Core Tenets and Common Errors to Avoid

## Purpose

This document defines the core operating principles for the research papers pipeline and lists the common error types that must be actively avoided in future development, debugging, refactoring, and agent prompts.

The governing principle is:

> **One input file → one evidence package → one canonical disposition decision → one final action.**

No module should independently decide whether to ingest, delete, retry, hold, review, evaluate, or route a file. All file fate decisions must flow through the central evidence and disposition path.

---

# Part 1: Common Errors That Must Be Avoided

## 1. Logic & Branching Errors

### Over-broad logic bugs
New gates, guards, or filters are applied too aggressively and break valid cases.

Avoid by:
- Writing positive acceptance criteria and negative rejection criteria separately.
- Testing known valid research articles, reference materials, child documents, and junk/corrupt files.
- Ensuring a new guard does not become a blunt global block.

### Unreachable/dead-branch logic
Conditionals are always false, always true, or can never be reached because an earlier branch captures the case.

Avoid by:
- Reviewing branch order.
- Adding invariant checks for expected branch coverage.
- Removing dead branches rather than leaving misleading fallback code.

### Incomplete wiring
Functions, guards, validators, or recovery helpers are defined but never called by the actual live path.

Avoid by:
- Tracing the real execution path from staging file to final action.
- Checking that every new function is called from the central transaction path.
- Adding validation that fails if the central gate can be bypassed.

### Dead code
Unused variables, constants, helper functions, old routing paths, and stale compatibility branches remain after refactors.

Avoid by:
- Removing obsolete paths after centralising logic.
- Not leaving old review/recovery routes as callable active code.
- Treating dead code as a future bug source, not harmless clutter.

---

## 2. Data Safety & Transaction Errors

### Data-loss footgun
An empty scope, empty filter, or failed selector falls through to a broad whole-DB or whole-folder operation.

Avoid by:
- Requiring explicit non-empty target sets for destructive actions.
- Adding dry-run summaries before execute-mode destructive operations.
- Aborting if a selector unexpectedly resolves to all rows or no rows.

### Transaction-order errors
DB/source actions happen before extraction, identity, coverage, and article-type acceptance are proven safe.

Avoid by enforcing this order:
1. Preflight duplicate check.
2. Extraction/OCR/cache.
3. EvidencePackage.
4. Identity gate.
5. Evidence coverage gate.
6. Article-type guard.
7. Disposition decision.
8. DB/source transaction only if accepted or linkable.
9. Evaluation/red-team only if evidence supports it.
10. Audit/report.

### Data-safety errors
Paths risk unowned source files, poisoned rows, bad source names, incomplete rows, false deletion, or broken source ownership.

Avoid by:
- Making DB/source operations atomic or rollback-safe.
- Auditing before delete, merge, or rename.
- Running source ownership checks after repair operations.
- Never deleting canonical owned PDFs unless a safe merge/delete transaction updates ownership correctly.

---

## 3. State, Lifecycle & Routing Errors

### Architecture / lifecycle errors
No single authoritative path controls file fate from intake to final action.

Avoid by:
- Requiring all file fate decisions to pass through the central disposition transaction.
- Removing independent routing logic from helper modules.
- Treating “move to folder” as an action authorised by a disposition decision, not as a decision itself.

### State-machine errors
Files sit between states, retry, loop, or get held without a terminal outcome.

Avoid by:
- Defining finite terminal actions.
- Requiring every processed file to finish as accepted, linked, held as valid pending child, deleted with audit, or stopped only by true system/user interruption.
- Prohibiting undefined intermediate lifecycle states.

### Recovery-loop errors
Blocked files are retried or renamed instead of immediately recovered, adjudicated, or terminally handled.

Avoid by:
- Treating coverage blocks as triggers for immediate recovery/adjudication.
- Adding per-run guards against retrying the same file/hash for the same reason.
- Forbidding blind rename-and-retry loops.

### Review/holding-folder errors
Review folders become lifecycle states instead of exceptional stop states or audit outputs.

Avoid by:
- Not routing ordinary completed-run files to review/technical-failure/recovery-pending.
- Keeping only one valid long-term hold: `pending-parent-child-documents/`, and only for valid child/support documents awaiting a future parent.
- Using audit records instead of folders for historical accountability.

### Stale-state reads
Globals, cached fields, status objects, or per-file variables are not reset or guarded between files.

Avoid by:
- Keeping per-file state inside explicit transaction/context objects.
- Clearing current_file and phase-specific status when phase changes.
- Avoiding module-level mutable state unless it is a deliberately managed cache.

---

## 4. Gate & Validation Coverage Errors

### Gate-enforcement errors
Checks diagnose problems but do not actually block downstream side effects.

Avoid by:
- Making gates return decision objects that authorise or prohibit exact actions.
- Preventing DB insert, source copy, evaluation, red-team skip, or delete unless the gate explicitly allows it.
- Validating that failed gates cannot fall through.

### Evaluation-readiness errors
The system evaluates or skips red-team before proving the evidence pack is good enough.

Avoid by:
- Requiring coverage_status and identity_status before evaluation.
- Blocking evaluation when evidence_can_support_rating is false.
- Blocking red-team skip unless non-ratable/not-applicable status is independently supported by article-type evidence.

### Validation coverage errors
Tests pass syntax/integrity checks while missing core pipeline invariants.

Avoid by validating invariants such as:
- PII/DOI/ISSN cannot become canonical title when better evidence exists.
- DB insert cannot happen before identity and coverage gates pass.
- Likely journal articles cannot be saved as NOT_APPLICABLE or NOT_RATABLE_REFERENCE_MATERIAL due to poor evidence.
- Parsed sections cannot replace raw full text.
- Delete actions require audit.
- Review-dust routing is inactive.
- Source ownership remains clean.

---

## 5. Performance & Redundancy Errors

### Performance design errors / redundant computation
Expensive operations, duplicated work, and DB loads repeat unnecessarily in hot paths instead of being cached or staged.

Avoid by:
- Hashing each file once per run.
- Caching DB-owned source hash indexes.
- Caching extraction/OCR by file hash.
- Avoiding full DB/source audits inside per-file loops.
- Avoiding repeated pending-child full scans.

### Duplicate-detection errors
Duplicate logic is inefficient, matches a file to itself, or is mixed with unrelated routing logic.

Avoid by:
- Performing exact hash dedupe before OCR/model work.
- Comparing staging hashes against DB-owned canonical hashes and other staging files, not the same file.
- Keeping duplicate detection deterministic and separate from fuzzy/model adjudication.

---

## 6. Data Contract & Classification Errors

### Evidence-contract errors
Different modules use different definitions of “the text”, “the title”, “the document type”, and “is this evaluable?”

Avoid by:
- Building one EvidencePackage per file.
- Passing EvidencePackage through identity, coverage, article-type, disposition, DB, and evaluation stages.
- Not letting downstream modules re-guess from partial raw fields.

### Extraction/OCR classification errors
Tool failure, scanned PDFs, empty extraction, wrong paths, and true corrupt files are not cleanly separated.

Avoid by distinguishing:
- TEXT_OK
- TRUE_EMPTY_TEXT
- OCR_REQUIRED
- OCR_FAILED
- EXTRACTOR_UNAVAILABLE
- EXTRACTOR_CRASHED
- WRONG_PATH_OR_MISSING_FILE
- UNSUPPORTED_CONTENT_TYPE
- CORRUPT_OR_UNREADABLE

### Metadata/identity errors
Bad identifiers, weak metadata, filenames, titles, and first-page signals are not ranked and validated under one identity contract.

Avoid by:
- Rejecting PII, DOI-only, ISSN-only, copyright, header/footer, unknown, and untitled strings as canonical titles when better evidence exists.
- Ranking title candidates from Crossref, first page, OCR, filename, and PDF metadata.
- Storing PII/DOI/ISSN as identifiers, not titles.

### Article-type classification errors
Poor evidence or poor metadata is mistaken for “not applicable” or “reference material”.

Avoid by:
- Treating likely journal articles with poor evidence as insufficient evidence/recovery/adjudication, not not-applicable.
- Reserving NOT_RATABLE_REFERENCE_MATERIAL for genuine books, manuals, handbooks, standards, protocols, dictionaries, tutorials, and reference texts.
- Reserving NOT_APPLICABLE for genuinely out-of-scope/non-corpus material, not failed extraction or weak metadata.

---

## 7. Model & Prompting Errors

### Model-trust errors
Model outputs are accepted without deterministic reconciliation against evidence, identity, and article-type signals.

Avoid by:
- Checking model output against EvidencePackage.
- Rejecting schema-valid but semantically contradictory outputs.
- Escalating or marking insufficient evidence when model output conflicts with deterministic evidence.

### Prompting / planning errors
Prompts are too incremental and symptom-focused instead of forcing full-chain invariant design.

Avoid by:
- Starting from the lifecycle invariant.
- Asking for chain tracing before implementation.
- Asking for senior-code-review rejection points.
- Requiring validation of invariants, not just the current incident.

---

## 8. System, Status & Terminology Errors

### Shell/environment mistakes
Commands intended for Bash are used in PowerShell, or vice versa.

Avoid by:
- Writing Windows-compatible commands when working in the Windows repo.
- Being explicit about shell context.
- Avoiding shell-specific syntax unless the environment is known.

### Logging/status errors
Logs and GUI statuses are ambiguous, stale, misleading, or describe partial states as final states.

Avoid by:
- Logging phase, current action, gate status, and final action separately.
- Not printing “Added” until acceptance and DB/source transaction have succeeded.
- Not showing “Done” while processing.
- Clearing stale current_file labels when phase changes.

### Terminology errors
Terms like failed, terminal failure, recovery pending, candidate, and review are too ambiguous and allow design drift.

Avoid by using strict vocabulary:
- accepted research
- accepted reference
- linked child
- held pending child
- deleted duplicate
- deleted junk
- deleted corrupt
- deleted unrecoverable with audit
- stopped by user
- failed integrity

### Repair-scope errors
Fixes handle only the current incident instead of auditing the whole DB for the same class of defect.

Avoid by:
- Repairing the live defect path.
- Auditing the current run damage.
- Auditing the whole DB for deterministic cases of the same defect class.
- Marking only genuinely uncertain cases.

---

# Part 2: Core Tenets of the Research Papers Pipeline

## 1. One file, one central decision, one final action

Every input file must pass through one canonical decision path. No separate script, helper, resolver, or folder should independently decide delete, retry, review, ingest, or evaluate.

## 2. No review dust piles

Ordinary files must not be parked indefinitely in review, technical-failure, recovery-pending, needs-metadata, evaluation-failed, or quarantine folders. They must be resolved. The only valid long-term hold is valid child/support material waiting for a future parent.

## 3. Evidence before side effects

No file should be copied to canonical `source-pdfs`, inserted into the DB, evaluated, or marked complete until extraction, identity, coverage, and article-type checks have passed.

## 4. Diagnostics must control state

If the code detects bad evidence, bad identity, insufficient coverage, OCR failure, duplicate status, or extraction failure, that finding must control the next state. Logging a warning is not enough.

## 5. Full extraction first, sectioning second

The pipeline must extract the best available full document text before parsing sections. Section parsing must never replace or discard raw full text.

## 6. Coverage gate before DB insert and evaluation

A likely journal article with weak evidence, partial text, one bad section, missing OCR, or insufficient coverage must not be inserted as a normal paper or evaluated as if complete.

## 7. Identity gate before DB/source naming

Canonical titles, paper IDs, and source filenames must come from validated identity evidence. PII, DOI, ISSN, headers, copyright lines, and metadata junk must never become the title when better evidence exists.

## 8. Article-type guard before classification acceptance

Likely journal articles must not become NOT_APPLICABLE or NOT_RATABLE_REFERENCE_MATERIAL merely because metadata or extraction is poor. Poor evidence means recovery/adjudication/insufficient evidence, not a false non-ratable label.

## 9. Model output must be reconciled

Sonnet/Opus output is not automatically truth. Model ratings and classifications must be checked against deterministic evidence, article-type signals, identity quality, and coverage health.

## 10. Red-team skip must be justified

Red-team should only be skipped when the document is genuinely non-ratable or not applicable, and that status is supported by deterministic article-type evidence.

## 11. Closed-loop adjudication

If a file cannot be accepted, it must be recovered, linked, held as valid pending child, deleted with audit as duplicate/junk/corrupt/unrecoverable, or stopped only for a true system/integrity failure.

## 12. Pending-child is the only real hold state

Supplements, errata, appendices, datasets, code, HTML/MIME support docs, and other child documents may wait for a parent. Standalone papers should not.

## 13. Audit before delete or merge

Any deletion, duplicate removal, merge, source rename, or DB repair must leave an audit trail with filename, hash, reason, evidence, action, and ownership status.

## 14. Source ownership must always be clean

Every canonical source PDF must be owned by a DB row. There must be no unowned source files, no ambiguous ownership, and no missing source paths.

## 15. Transactional DB/source operations

DB row creation and source PDF movement/renaming must be atomic or rollback-safe. No partial rows, orphan source copies, or poisoned DB records should survive a failed operation.

## 16. Dedupe must be exact, safe, and cheap

Hash dedupe comes before OCR/model work. Source hashes should be cached. A file must never be treated as a duplicate of itself.

## 17. Expensive work must be cached and staged

No repeated OCR for the same hash. No repeated source rehashing. No repeated full pending scans. No model calls for exact duplicates.

## 18. Validation must enforce invariants, not just syntax

The validation suite must fail if core rules can be violated, such as PII titles, DB insert before coverage acceptance, one-section evaluations, review dust routing, or delete without audit.

## 19. GUI/reporting must reflect real state

The GUI should clearly distinguish dedupe, extraction, coverage block, accepted, deleted, pending child, stopped, and integrity failure. It must not say “Done” or “Added” prematurely.

## 20. No later cleanup for current design flaws

If a class of issue appears, fix the central architecture immediately. Do not patch repeated symptoms one by one.

## 21. Evaluation is only for evaluable evidence

A paper should only be rated when the evidence pack can fairly support a rating. Otherwise it needs recovery/adjudication or a terminal insufficient-evidence action.

## 22. Reference material is preserved but not misused

Books, manuals, handbooks, standards, dictionaries, protocols, and tutorials can be kept as non-ratable reference material, but journal articles must not be mislabelled as reference material.

## 23. Staging is an intake queue, not storage

A completed run should not leave ordinary files in staging. Files should be accepted, deleted with audit, linked, held as valid child, or stopped only under genuine interruption/integrity failure.

## 24. Every module delegates to the central engine

No caller should independently move, delete, copy, retry, review, or insert. All file fate decisions must go through the central evidence/disposition transaction.

---

# Part 3: Compact Review Checklist

Before accepting any future code change, check:

- Does this change preserve the single central file-fate path?
- Can it create a side effect before evidence, identity, coverage, and article-type gates pass?
- Can it leave files in staging, review, or recovery without a terminal action?
- Can it evaluate from partial evidence?
- Can it trust model output without deterministic reconciliation?
- Can it delete without audit?
- Can it create unowned or ambiguous source files?
- Can it repeat expensive work in a hot path?
- Can it pass validation while violating a core invariant?
- Does it solve the whole error class, not just the current symptom?

