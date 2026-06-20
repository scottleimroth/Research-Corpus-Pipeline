"""Central file disposition: one file in → one adjudication decision → one final action."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import config
from path_governance import MAX_FILENAME, safe_destination_for_file

FINAL_ACTIONS = frozenset(
    {
        "INGEST_RESEARCH_PAPER",
        "INGEST_NON_RATABLE_REFERENCE",
        "LINK_CHILD_TO_PARENT",
        "HOLD_PENDING_PARENT",
        "DELETE_CONFIRMED_DUPLICATE",
        "DELETE_CONFIRMED_JUNK",
        "DELETE_TRUE_CORRUPT_UNUSABLE",
        "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
        "SYSTEM_INTEGRITY_FAILURE",
    }
)

# Task B: public terminal enum (only completed-run outcomes).
TERMINAL_ACTIONS = frozenset(
    {
        "ACCEPT_RESEARCH",
        "ACCEPT_REFERENCE",
        "LINK_CHILD",
        "HOLD_PENDING_CHILD",
        "DELETE_DUPLICATE",
        "DELETE_JUNK",
        "DELETE_CORRUPT",
        "DELETE_UNRECOVERABLE",
        "SYSTEM_INTEGRITY_FAILURE",
    }
)

INTERNAL_TO_TERMINAL = {
    "INGEST_RESEARCH_PAPER": "ACCEPT_RESEARCH",
    "INGEST_NON_RATABLE_REFERENCE": "ACCEPT_REFERENCE",
    "LINK_CHILD_TO_PARENT": "LINK_CHILD",
    "HOLD_PENDING_PARENT": "HOLD_PENDING_CHILD",
    "DELETE_CONFIRMED_DUPLICATE": "DELETE_DUPLICATE",
    "DELETE_CONFIRMED_JUNK": "DELETE_JUNK",
    "DELETE_TRUE_CORRUPT_UNUSABLE": "DELETE_CORRUPT",
    "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION": "DELETE_UNRECOVERABLE",
    "SYSTEM_INTEGRITY_FAILURE": "SYSTEM_INTEGRITY_FAILURE",
}

TERMINAL_TO_INTERNAL = {v: k for k, v in INTERNAL_TO_TERMINAL.items()}

# Backward-compatible adjudication labels.
ADJUDICATION_ACTIONS = dict(INTERNAL_TO_TERMINAL)

_LEGACY_STOP_TO_FINAL = {
    "STOP_MODEL_UNAVAILABLE": "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
    "STOP_TRUE_TECHNICAL_FAILURE": "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
}

DELETE_ACTIONS = frozenset(
    {
        "DELETE_CONFIRMED_DUPLICATE",
        "DELETE_CONFIRMED_JUNK",
        "DELETE_TRUE_CORRUPT_UNUSABLE",
        "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
    }
)
INGEST_ACTIONS = frozenset({"INGEST_RESEARCH_PAPER", "INGEST_NON_RATABLE_REFERENCE"})

STAGING_METADATA_SUFFIXES = (".review-retry.json", ".meta.json")

RECOVERY_TO_FINAL = {
    "RECOVERED_RESEARCH_PAPER_MOVE_TO_STAGING": "INGEST_RESEARCH_PAPER",
    "RECOVERED_NON_RATABLE_REFERENCE_MOVE_TO_STAGING": "INGEST_NON_RATABLE_REFERENCE",
    "CHILD_DOCUMENT_TO_PENDING_PARENT": "HOLD_PENDING_PARENT",
    "DELETE_DUPLICATE": "DELETE_CONFIRMED_DUPLICATE",
    "DELETE_JUNK": "DELETE_CONFIRMED_JUNK",
    "TECHNICAL_FAILURE_UNRECOVERABLE": "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
    "API_OR_MODEL_UNAVAILABLE": "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
    "MODEL_RECOVERY_REQUIRED": "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
}

CLASSIFICATION_TO_ACTION = {
    "CONFIRMED_DUPLICATE": "DELETE_CONFIRMED_DUPLICATE",
    "TRUE_CORRUPT_UNUSABLE": "DELETE_TRUE_CORRUPT_UNUSABLE",
    "HTML_OR_TEXT_JUNK": "DELETE_CONFIRMED_JUNK",
    "MIME_OR_WRAPPER_JUNK": "DELETE_CONFIRMED_JUNK",
    "VALID_PDF_STANDALONE_RESEARCH": "INGEST_RESEARCH_PAPER",
    "VALID_PDF_NON_RATABLE_REFERENCE": "INGEST_NON_RATABLE_REFERENCE",
    "VALID_NONPDF_REFERENCE": "INGEST_NON_RATABLE_REFERENCE",
    "HTML_OR_TEXT_REFERENCE": "INGEST_NON_RATABLE_REFERENCE",
}

CHILD_CLASSIFICATIONS = frozenset(
    {
        "VALID_PDF_CHILD_DOCUMENT",
        "VALID_NONPDF_CHILD_DOCUMENT",
        "VALID_NONPDF_DATASET_OR_MEDIA_SUPPLEMENT",
        "HTML_OR_TEXT_CHILD_DOCUMENT",
        "MIME_OR_WRAPPER_RECOVERABLE",
        "ARCHIVE_CHILD_DOCUMENT",
    }
)

LEGACY_REVIEW_FOLDERS = (
    config.REVIEW_NEEDS_METADATA,
    config.REVIEW_EVAL_FAILED,
    config.REVIEW_EVAL_ERROR,
    config.REVIEW_INSUFFICIENT_TEXT,
    config.REVIEW_DUPLICATE_AMBIGUOUS,
    config.REVIEW_UNIDENTIFIED,
    config.REVIEW_SUPPLEMENTS,
    config.REVIEW_SUPPLEMENTS_PENDING,
    config.REVIEW_DIR / "quarantine-orphan-source",
    config.REVIEW_DIR / "child-documents-pending-parent",
    config.DUPLICATES_DIR,
    config.RECOVERED_DELETION_AUDIT,
)

# Legacy dust folders — drain only, never active routing targets.
REVIEW_DUST_FOLDERS = (
    config.REVIEW_TECHNICAL_FAILURE,
    config.REVIEW_RECOVERY_PENDING,
    *LEGACY_REVIEW_FOLDERS,
)

RESOLVER_CANDIDATE_FOLDERS: tuple[tuple[Path, str], ...] = (
    *((folder, f"legacy-{folder.name}") for folder in LEGACY_REVIEW_FOLDERS if folder.parent == config.REVIEW_DIR),
    *((folder, str(folder.relative_to(config.BASE)).replace("\\", "/")) for folder in LEGACY_REVIEW_FOLDERS if folder.parent != config.REVIEW_DIR),
    (config.REVIEW_RECOVERY_PENDING, "recovery-pending"),
    (config.REVIEW_TECHNICAL_FAILURE, "technical-failure"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_final_action(action: str) -> str:
    action = _LEGACY_STOP_TO_FINAL.get(action, action)
    if action in TERMINAL_TO_INTERNAL:
        return TERMINAL_TO_INTERNAL[action]
    return action


def to_terminal_action(internal_action: str) -> str:
    return INTERNAL_TO_TERMINAL.get(normalize_final_action(internal_action), normalize_final_action(internal_action))


def adjudication_action_label(internal_action: str) -> str:
    return to_terminal_action(internal_action)


def is_staging_metadata_file(name: str) -> bool:
    low = name.lower()
    return low.endswith(".json") and (
        name.endswith(STAGING_METADATA_SUFFIXES[0])
        or name.endswith(STAGING_METADATA_SUFFIXES[1])
        or low.endswith(".json")
    )


def is_staging_candidate(path: Path) -> bool:
    path = Path(path)
    if not path.is_file():
        return False
    if path.name.startswith("."):
        return False
    if path.suffix.lower() == ".json":
        return False
    return True


def list_staging_candidates() -> list[Path]:
    if not config.STAGING.exists():
        return []
    return sorted(p for p in config.STAGING.iterdir() if is_staging_candidate(p))


list_root_staging_pdfs = list_staging_candidates  # backward-compatible alias for PDF-era callers


def _sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def write_file_disposition_audit(record: dict[str, Any]) -> None:
    """Audit-before-delete: one JSONL record per deleted/rejected file."""
    audit_dir = config.FILE_DISPOSITION_AUDIT_DIR
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"dispositions-{_now()[:10]}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _audit_log(entry: dict[str, Any]) -> None:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_dir = config.LOGS_DIR / "disposition-audit"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"disposition-{_now()[:10]}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _build_delete_audit_record(
    path: Path,
    *,
    action: str,
    reason: str,
    evidence: dict[str, Any],
    req: DispositionRequest | None = None,
    decision_source: str = "file_disposition_engine",
    confidence: str = "high",
    notes: str = "",
) -> dict[str, Any]:
    sniff = evidence.get("sniff") or evidence.get("ladder", {}).get("sniff") or {}
    ladder = evidence.get("ladder") or evidence.get("recovery_ladder") or {}
    dup = (evidence.get("duplicate_check") or evidence.get("duplicate") or {})
    return {
        "run_id": (req.run_id if req else "") or evidence.get("run_id") or "",
        "timestamp": _now(),
        "original_path": str(path),
        "original_folder": str(path.parent),
        "filename": path.name,
        "sha256": evidence.get("sha256") or dup.get("sha256") or _sha256_file(path),
        "size_bytes": path.stat().st_size if path.exists() else evidence.get("size_bytes"),
        "detected_content_type": sniff.get("content_type") or evidence.get("content_type") or path.suffix.lower(),
        "final_action": adjudication_action_label(action),
        "reason_category": reason.split(":")[0] if reason else action.lower(),
        "confidence": confidence,
        "deterministic_evidence": {k: v for k, v in evidence.items() if k not in {"ladder", "recovery_ladder"}},
        "extraction_attempts": ladder.get("extraction_attempts") or evidence.get("extraction_attempts") or [],
        "ocr_or_visual_attempted": bool(ladder.get("ocr_attempted") or evidence.get("ocr_attempted")),
        "sonnet_attempted": bool(ladder.get("sonnet_attempted") or evidence.get("sonnet_attempted")),
        "opus_attempted": bool(ladder.get("opus_attempted") or evidence.get("opus_attempted")),
        "model_available": bool(req.allow_paid_api if req else evidence.get("model_available")),
        "duplicate_match_target": dup.get("existing_id") or dup.get("match_target"),
        "parent_match_attempted": bool(evidence.get("parent_match_attempted")),
        "parent_match_result": evidence.get("parent_match_result"),
        "deleted_path": str(path),
        "decision_source": decision_source,
        "notes": notes,
    }


def _deterministic_terminal_when_model_blocked(
    classified: dict[str, Any],
    *,
    is_child: bool,
    has_parent: bool,
) -> str:
    classification = str(classified.get("classification") or "")
    if classification == "CONFIRMED_DUPLICATE":
        return "DELETE_CONFIRMED_DUPLICATE"
    if classification == "TRUE_CORRUPT_UNUSABLE":
        return "DELETE_TRUE_CORRUPT_UNUSABLE"
    if classification in {"HTML_OR_TEXT_JUNK", "MIME_OR_WRAPPER_JUNK"}:
        return "DELETE_CONFIRMED_JUNK"
    if classification in CHILD_CLASSIFICATIONS or is_child:
        return "LINK_CHILD_TO_PARENT" if has_parent else "HOLD_PENDING_PARENT"
    return "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION"


@dataclass
class DispositionRequest:
    path: Path
    source_context: str = "staging"
    run_id: str = ""
    allow_paid_api: bool = False
    allow_opus_resolution: bool = False
    allow_db_write: bool = False
    allow_file_move: bool = True
    allow_accept_to_db: bool = False
    dry_run: bool = False


@dataclass
class DispositionDecision:
    path: str
    filename: str
    action: str
    terminal_action: str
    reason: str
    source_context: str
    run_id: str
    evidence: dict[str, Any]
    needs_ingest: bool = False
    ingest_kind: str | None = None
    parent_id: str | None = None
    child_type: str | None = None
    recovered_metadata: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DispositionDecision:
        action = normalize_final_action(str(raw.get("action") or ""))
        return cls(
            path=str(raw.get("path") or ""),
            filename=str(raw.get("filename") or Path(raw.get("path") or "").name),
            action=action,
            terminal_action=to_terminal_action(action),
            reason=str(raw.get("reason") or ""),
            source_context=str(raw.get("source_context") or "staging"),
            run_id=str(raw.get("run_id") or ""),
            evidence=dict(raw.get("evidence") or {}),
            needs_ingest=bool(raw.get("needs_ingest")),
            ingest_kind=raw.get("ingest_kind"),
            parent_id=raw.get("parent_id"),
            child_type=raw.get("child_type"),
            recovered_metadata=raw.get("recovered_metadata"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "filename": self.filename,
            "action": self.action,
            "terminal_action": self.terminal_action,
            "reason": self.reason,
            "source_context": self.source_context,
            "run_id": self.run_id,
            "evidence": self.evidence,
            "needs_ingest": self.needs_ingest,
            "ingest_kind": self.ingest_kind,
            "parent_id": self.parent_id,
            "child_type": self.child_type,
            "recovered_metadata": self.recovered_metadata or {},
        }


@dataclass
class DispositionResult:
    decision: DispositionDecision
    executed: bool
    terminal_action: str
    deleted: bool = False
    linked_parent: str | None = None
    pending_dest: str | None = None
    moved_to_staging: str | None = None
    system_integrity_failure: bool = False
    needs_ingest: bool = False
    audit_written: bool = False
    extra: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            **self.decision.to_dict(),
            "executed": self.executed,
            "terminal_action": self.terminal_action,
            "deleted": self.deleted,
            "linked_parent": self.linked_parent,
            "pending_dest": self.pending_dest,
            "moved_to_staging": self.moved_to_staging,
            "system_integrity_failure": self.system_integrity_failure,
            "needs_ingest": self.needs_ingest,
            "audit_written": self.audit_written,
        }
        if self.extra:
            out.update(self.extra)
        return out


@dataclass
class DispositionCounts:
    processed: int = 0
    ingest_research: int = 0
    ingest_non_ratable: int = 0
    linked_child: int = 0
    hold_pending: int = 0
    deleted_duplicate: int = 0
    deleted_junk: int = 0
    deleted_corrupt: int = 0
    deleted_unrecoverable: int = 0
    system_integrity_failure: int = 0
    moved_to_staging: int = 0
    needs_ingest: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "processed": self.processed,
            "ingest_research": self.ingest_research,
            "ingest_non_ratable": self.ingest_non_ratable,
            "linked_child": self.linked_child,
            "hold_pending": self.hold_pending,
            "deleted_duplicate": self.deleted_duplicate,
            "deleted_junk": self.deleted_junk,
            "deleted_corrupt": self.deleted_corrupt,
            "deleted_unrecoverable": self.deleted_unrecoverable,
            "system_integrity_failure": self.system_integrity_failure,
            "moved_to_staging": self.moved_to_staging,
            "needs_ingest": self.needs_ingest,
        }


def map_recovery_decision(decision: str) -> str:
    return normalize_final_action(RECOVERY_TO_FINAL.get(decision, "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION"))


def map_classification_to_action(
    classification: str,
    *,
    has_parent: bool = False,
    is_child_filename: bool = False,
) -> str | None:
    if classification in CHILD_CLASSIFICATIONS or (
        is_child_filename and classification not in {"CONFIRMED_DUPLICATE", "TRUE_CORRUPT_UNUSABLE", "HTML_OR_TEXT_JUNK", "MIME_OR_WRAPPER_JUNK"}
    ):
        return "LINK_CHILD_TO_PARENT" if has_parent else "HOLD_PENDING_PARENT"
    if classification == "MODEL_RECOVERY_REQUIRED":
        return None
    return CLASSIFICATION_TO_ACTION.get(classification)


def _finish_disposition_with_llm(req: DispositionRequest, path: Path, result: dict[str, Any]) -> dict[str, Any]:
    if req.source_context in {"staging", "staging-ingest-failure"}:
        from staging_disposition_llm import apply_staging_disposition_llm

        result = apply_staging_disposition_llm(
            path,
            result,
            source_context=req.source_context,
            allow_paid_api=req.allow_paid_api,
            allow_opus=req.allow_opus_resolution,
        )
    return result


def decide_file_disposition(
    req: DispositionRequest,
    *,
    child_ctx: Any | None = None,
) -> dict[str, Any]:
    """Inspect file and return exactly one final action."""
    from supplement_content_classifier import classify_supplement_file

    path = Path(req.path)
    result: dict[str, Any] = {
        "path": str(path),
        "filename": path.name,
        "source_context": req.source_context,
        "run_id": req.run_id,
        "action": "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
        "classification": "",
        "document_type": "",
        "reason": "",
        "evidence": {},
        "recovered_metadata": {},
        "parent_id": None,
        "needs_ingest": False,
        "ingest_kind": None,
    }
    if not path.exists():
        result.update(action="DELETE_TRUE_CORRUPT_UNUSABLE", reason="file_missing")
        return result

    classified = classify_supplement_file(
        path,
        allow_paid_api=req.allow_paid_api and not req.dry_run,
        allow_opus=req.allow_opus_resolution and req.allow_paid_api and not req.dry_run,
        owned_canonical_only=True,
    )
    classification = str(classified.get("classification") or "")
    result["classification"] = classification
    result["evidence"] = classified.get("evidence") or {}
    result["sniff"] = classified.get("sniff") or {}

    from child_document_matching import detect_child_document_type, find_parent_for_child, is_child_document

    child_type = detect_child_document_type(path.name)
    is_child = bool(child_type) or is_child_document(path.name)
    parent = find_parent_for_child(path.name, child_ctx) if child_ctx or True else None
    if parent:
        result["parent_id"] = parent.get("id")
        result["evidence"]["parent_match_attempted"] = True
        result["evidence"]["parent_match_result"] = parent.get("id")

    action = map_classification_to_action(
        classification,
        has_parent=bool(parent),
        is_child_filename=is_child,
    )

    if action:
        result["action"] = action
        result["reason"] = f"classification:{classification.lower()}"
        result["child_type"] = child_type or "supplement"
        if action in INGEST_ACTIONS:
            result["needs_ingest"] = True
            result["ingest_kind"] = action
            pdf_evidence = (classified.get("evidence") or {}).get("pdf") or {}
            result["recovered_metadata"] = pdf_evidence.get("recovered_metadata") or {}
            corpus_cls = pdf_evidence.get("corpus_classification") or {}
            result["document_type"] = corpus_cls.get("document_type") or ""
        return _finish_disposition_with_llm(req, path, result)

    if classification == "MODEL_RECOVERY_REQUIRED" or not action:
        if path.suffix.lower() != ".pdf" and req.allow_paid_api and not req.dry_run:
            result.update(
                action=action or "HOLD_PENDING_PARENT",
                reason="non_pdf_pending_llm_disposition",
                child_type=child_type or "supplement",
            )
            return _finish_disposition_with_llm(req, path, result)
        if not req.allow_paid_api:
            terminal = _deterministic_terminal_when_model_blocked(
                classified,
                is_child=is_child,
                has_parent=bool(parent),
            )
            result.update(
                action=terminal,
                reason="deterministic_terminal_model_unavailable",
                confidence="medium",
            )
            if terminal in INGEST_ACTIONS:
                result["needs_ingest"] = True
                result["ingest_kind"] = terminal
            return _finish_disposition_with_llm(req, path, result)
        from recovery_ladder import run_recovery_ladder

        ladder = run_recovery_ladder(
            path,
            allow_paid_api=True,
            allow_opus=req.allow_opus_resolution,
            dry_run=req.dry_run,
        )
        recovery_decision = str(ladder.get("decision") or "")
        final_action = map_recovery_decision(recovery_decision)
        result["action"] = final_action
        result["reason"] = str(ladder.get("evidence", {}).get("reason") or recovery_decision)
        result["evidence"] = {**(result.get("evidence") or {}), "ladder": ladder}
        result["recovered_metadata"] = ladder.get("recovered_metadata") or {}
        if final_action in INGEST_ACTIONS:
            result["needs_ingest"] = True
            result["ingest_kind"] = final_action
        if final_action == "HOLD_PENDING_PARENT":
            result["child_type"] = ladder.get("evidence", {}).get("child_type") or child_type or "supplement"
        if final_action == "LINK_CHILD_TO_PARENT" and not parent:
            parent = find_parent_for_child(path.name, child_ctx)
            result["parent_id"] = parent.get("id") if parent else None
            if not parent:
                result["action"] = "HOLD_PENDING_PARENT"
        return _finish_disposition_with_llm(req, path, result)

    result["reason"] = result.get("reason") or f"unmapped_classification:{classification}"
    return _finish_disposition_with_llm(req, path, result)


def decide_file(
    file_path: Path | str,
    context: dict[str, Any] | None = None,
    *,
    child_ctx: Any | None = None,
) -> DispositionDecision:
    """Step 1: decide terminal action for one file (may use Sonnet/Opus when enabled)."""
    ctx = context or {}
    req = DispositionRequest(
        path=Path(file_path),
        source_context=str(ctx.get("source_context") or "staging"),
        run_id=str(ctx.get("run_id") or ""),
        allow_paid_api=bool(ctx.get("allow_paid_api")),
        allow_opus_resolution=bool(ctx.get("allow_opus_resolution")),
        allow_db_write=bool(ctx.get("allow_db_write")),
        allow_accept_to_db=bool(ctx.get("allow_accept_to_db")),
        allow_file_move=bool(ctx.get("allow_file_move", True)),
        dry_run=bool(ctx.get("dry_run")),
    )
    raw = decide_file_disposition(req, child_ctx=child_ctx or ctx.get("child_ctx"))
    raw["action"] = normalize_final_action(str(raw.get("action") or ""))
    return DispositionDecision.from_dict({**raw, "source_context": req.source_context, "run_id": req.run_id})


def execute_decision(
    decision: DispositionDecision | dict[str, Any],
    context: dict[str, Any] | None = None,
    *,
    child_ctx: Any | None = None,
    counts: DispositionCounts | None = None,
) -> DispositionResult:
    """Step 2: execute one disposition decision (never routes to review dust)."""
    ctx = context or {}
    dec = decision if isinstance(decision, DispositionDecision) else DispositionDecision.from_dict(decision)
    req = DispositionRequest(
        path=Path(dec.path),
        source_context=dec.source_context,
        run_id=dec.run_id,
        allow_paid_api=bool(ctx.get("allow_paid_api")),
        allow_opus_resolution=bool(ctx.get("allow_opus_resolution")),
        allow_db_write=bool(ctx.get("allow_db_write")),
        allow_accept_to_db=bool(ctx.get("allow_accept_to_db")),
        allow_file_move=bool(ctx.get("allow_file_move", True)),
        dry_run=bool(ctx.get("dry_run")),
    )
    payload = dec.to_dict()
    action = dec.action
    if (
        action in INGEST_ACTIONS
        and not req.allow_accept_to_db
        and dec.source_context.startswith(("review-drain", "legacy-recovery-pending"))
    ):
        # Review-drain never hard-deletes ingestable files. Intake remains staged and
        # marked for ingest so DB/source commit can happen in the canonical ingest path.
        payload["reason"] = f"accept_requires_ingest_queue:{dec.reason}"
    executed = execute_file_disposition(payload, req, child_ctx=child_ctx or ctx.get("child_ctx"), counts=counts)
    terminal = to_terminal_action(str(executed.get("action") or action))
    return DispositionResult(
        decision=DispositionDecision.from_dict({**payload, **executed, "action": normalize_final_action(str(executed.get("action") or action))}),
        executed=bool(executed.get("executed", not req.dry_run)),
        terminal_action=terminal,
        deleted=bool(executed.get("deleted")),
        linked_parent=executed.get("linked_parent"),
        pending_dest=executed.get("dest"),
        moved_to_staging=executed.get("moved_to_staging"),
        system_integrity_failure=bool(executed.get("system_integrity_failure")),
        needs_ingest=bool(executed.get("needs_ingest")),
        extra=executed,
    )


def audit_decision(result: DispositionResult, context: dict[str, Any] | None = None) -> None:
    """Step 3: persist audit record for terminal outcomes."""
    ctx = context or {}
    if result.audit_written:
        return
    dec = result.decision
    req = DispositionRequest(
        path=Path(dec.path),
        source_context=dec.source_context,
        run_id=dec.run_id or str(ctx.get("run_id") or ""),
        allow_paid_api=bool(ctx.get("allow_paid_api")),
    )
    if result.deleted or dec.terminal_action.startswith("DELETE_"):
        write_file_disposition_audit(
            _build_delete_audit_record(
                Path(dec.path),
                action=dec.action,
                reason=dec.reason,
                evidence=dec.evidence,
                req=req,
                decision_source=str(ctx.get("decision_source") or "disposition_transaction"),
            )
        )
        result.audit_written = True
    elif result.system_integrity_failure:
        write_file_disposition_audit(
            _build_delete_audit_record(
                Path(dec.path),
                action="SYSTEM_INTEGRITY_FAILURE",
                reason=dec.reason,
                evidence=dec.evidence,
                req=req,
                confidence="low",
                notes="integrity gap; file retained",
                decision_source=str(ctx.get("decision_source") or "disposition_transaction"),
            )
        )
        result.audit_written = True


def disposition_transaction(
    file_path: Path | str,
    context: dict[str, Any] | None = None,
    *,
    child_ctx: Any | None = None,
    counts: DispositionCounts | None = None,
) -> dict[str, Any]:
    """One file → decide → execute → audit → return result dict."""
    ctx = dict(context or {})
    if ctx.get("execute", True) is False or ctx.get("dry_run"):
        ctx["dry_run"] = True
    decision = decide_file(file_path, ctx, child_ctx=child_ctx or ctx.get("child_ctx"))
    if not ctx.get("execute", True):
        return decision.to_dict()
    result = execute_decision(
        decision,
        ctx,
        child_ctx=child_ctx or ctx.get("child_ctx"),
        counts=counts or ctx.get("counts"),
    )
    audit_decision(result, ctx)
    out = result.to_dict()
    ladder = (decision.evidence or {}).get("ladder") or {}
    out["sonnet_attempted"] = bool(ladder.get("sonnet_attempted") or ladder.get("sonnet_recovery_calls"))
    out["opus_attempted"] = bool(ladder.get("opus_attempted") or ladder.get("opus_recovery_calls"))
    return out


def adjudicate_file_to_final_action(
    file_path: Path | str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backward-compatible alias for disposition_transaction."""
    return disposition_transaction(file_path, context)


def _delete_file(
    path: Path,
    *,
    action: str,
    reason: str,
    evidence: dict[str, Any],
    dry_run: bool,
    req: DispositionRequest | None = None,
) -> None:
    from child_document_matching import _delete_corrupt_pending_child
    from review_resolution import delete_confirmed_duplicate, ResolutionAudit

    action = normalize_final_action(action)
    if dry_run:
        return
    audit_record = _build_delete_audit_record(
        path,
        action=action,
        reason=reason,
        evidence=evidence,
        req=req,
        confidence=str(evidence.get("confidence") or "high"),
    )
    write_file_disposition_audit(audit_record)
    if action in {
        "DELETE_CONFIRMED_JUNK",
        "DELETE_TRUE_CORRUPT_UNUSABLE",
        "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
    }:
        try:
            import intake_rejected

            intake_rejected.archive_before_delete(
                path,
                action=action,
                reason=reason,
                evidence=evidence,
                run_id=(req.run_id if req else "") or str(evidence.get("run_id") or ""),
            )
        except Exception:
            pass
    if action == "DELETE_CONFIRMED_DUPLICATE":
        if reason.startswith("preflight_") and evidence.get("sha256"):
            path.unlink(missing_ok=True)
        else:
            digest = str(evidence.get("sha256") or "").strip().lower() or None
            delete_confirmed_duplicate(
                path,
                audit=ResolutionAudit(),
                dry_run=False,
                digest=digest,
            )
    else:
        _delete_corrupt_pending_child(path, f"disposition:{action.lower()}:{reason}")
    _audit_log(
        {
            "timestamp": _now(),
            "action": action,
            "path": str(path),
            "reason": reason,
            "evidence": evidence,
        }
    )


def execute_file_disposition(
    decision: dict[str, Any],
    req: DispositionRequest,
    *,
    child_ctx: Any | None = None,
    counts: DispositionCounts | None = None,
) -> dict[str, Any]:
    """Execute one disposition decision."""
    from child_document_matching import detect_child_document_type, link_child_to_parent, route_child_to_pending
    from review_retry_tracking import record_staging_move
    from staging_metadata import remove_internal_meta

    path = Path(decision["path"])
    action = normalize_final_action(str(decision.get("action") or ""))
    reason = str(decision.get("reason") or "")
    evidence = decision.get("evidence") or {}
    out: dict[str, Any] = {"action": action, "path": str(path), "executed": not req.dry_run}

    if counts:
        counts.processed += 1

    if action in DELETE_ACTIONS:
        if not req.dry_run and req.allow_file_move:
            _delete_file(path, action=action, reason=reason, evidence=evidence, dry_run=False, req=req)
            remove_internal_meta(path)
        if counts:
            if action == "DELETE_CONFIRMED_DUPLICATE":
                counts.deleted_duplicate += 1
            elif action == "DELETE_TRUE_CORRUPT_UNUSABLE":
                counts.deleted_corrupt += 1
            elif action == "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION":
                counts.deleted_unrecoverable += 1
            else:
                counts.deleted_junk += 1
        out["deleted"] = True
        return out

    if action == "SYSTEM_INTEGRITY_FAILURE":
        write_file_disposition_audit(
            _build_delete_audit_record(
                path,
                action=action,
                reason=reason,
                evidence=evidence,
                req=req,
                confidence="low",
                notes="file retained; system integrity gap",
            )
        )
        if counts:
            counts.system_integrity_failure += 1
        out["system_integrity_failure"] = True
        return out

    if action == "HOLD_PENDING_PARENT":
        child_type = decision.get("child_type") or detect_child_document_type(path.name) or "supplement"
        if req.dry_run:
            out["dest"] = str(config.PENDING_PARENT_CHILD_DOCUMENTS / path.name)
        if not req.dry_run and req.allow_file_move:
            dest = route_child_to_pending(path, child_type=child_type, reason=f"disposition:{reason}")
            out["dest"] = str(dest) if dest else None
            remove_internal_meta(path)
        if counts:
            counts.hold_pending += 1
        return out

    if action == "LINK_CHILD_TO_PARENT":
        child_type = decision.get("child_type") or detect_child_document_type(path.name) or "supplement"
        parent_id = decision.get("parent_id")
        from child_document_matching import find_parent_for_child

        parent = find_parent_for_child(path.name, child_ctx) if child_ctx else find_parent_for_child(path.name)
        if parent and not req.dry_run and req.allow_file_move:
            if link_child_to_parent(path, parent, child_type=child_type):
                out["linked_parent"] = parent.get("id")
                remove_internal_meta(path)
                if counts:
                    counts.linked_child += 1
                return out
        child_type = decision.get("child_type") or detect_child_document_type(path.name) or "supplement"
        if not req.dry_run and req.allow_file_move:
            dest = route_child_to_pending(path, child_type=child_type, reason=f"disposition:no_parent:{reason}")
            out["dest"] = str(dest) if dest else None
            remove_internal_meta(path)
        if counts:
            counts.hold_pending += 1
        out["action"] = "HOLD_PENDING_PARENT"
        return out

    if action in INGEST_ACTIONS:
        if (not str(req.source_context).startswith("staging")) and req.allow_file_move and not req.dry_run:
            config.STAGING.mkdir(parents=True, exist_ok=True)
            dest = safe_destination_for_file(
                path,
                config.STAGING,
                reason=f"disposition:{reason}",
                max_filename=MAX_FILENAME - len(".review-retry.json"),
            )
            # If the file is already this exact staging path, treat it as
            # already queued for ingest; do not suffix-rename itself.
            if dest.resolve() != path.resolve():
                shutil.move(str(path), str(dest))
                record_staging_move(
                    dest,
                    source_review_folder=req.source_context,
                    original_review_reason=reason,
                    recovered_metadata=decision.get("recovered_metadata") or {},
                )
                path = dest
                out["moved_to_staging"] = str(dest)
                if counts:
                    counts.moved_to_staging += 1
        out["needs_ingest"] = True
        out["ingest_kind"] = action
        out["path"] = str(path)
        if counts:
            counts.needs_ingest += 1
            if action == "INGEST_RESEARCH_PAPER":
                counts.ingest_research += 1
            else:
                counts.ingest_non_ratable += 1
        return out

    out["error"] = f"unknown_action:{action}"
    return out


def stop_file_with_final_action(
    path: Path,
    *,
    action: str,
    reason: str,
    source_context: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a terminal disposition when ingest cannot continue (never routes to review dust)."""
    action = normalize_final_action(action)
    if action not in FINAL_ACTIONS:
        action = "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION"
    req = DispositionRequest(path=Path(path), source_context=source_context)
    decision = {
        "path": str(path),
        "filename": Path(path).name,
        "source_context": source_context,
        "action": action,
        "reason": reason,
        "evidence": {**(evidence or {}), "forced_stop_context": source_context},
    }
    return execute_file_disposition(decision, req)


def disposition_file(
    path: Path,
    *,
    source_context: str = "staging",
    run_id: str = "",
    allow_paid_api: bool = False,
    allow_opus_resolution: bool = False,
    allow_db_write: bool = False,
    allow_file_move: bool = True,
    dry_run: bool = False,
    child_ctx: Any | None = None,
    counts: DispositionCounts | None = None,
) -> dict[str, Any]:
    """Decide and execute disposition for one file."""
    req = DispositionRequest(
        path=Path(path),
        source_context=source_context,
        run_id=run_id,
        allow_paid_api=allow_paid_api,
        allow_opus_resolution=allow_opus_resolution,
        allow_db_write=allow_db_write,
        allow_file_move=allow_file_move,
        dry_run=dry_run,
    )
    decision = decide_file_disposition(req, child_ctx=child_ctx)
    execution = execute_file_disposition(decision, req, child_ctx=child_ctx, counts=counts)
    result = {**decision, **execution}
    result["adjudication_action"] = adjudication_action_label(str(result.get("action") or ""))
    return result


def _iter_folder_candidates(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and not p.name.endswith(".meta.json"))


def iter_resolver_candidates() -> list[tuple[str, Path]]:
    """Yield (source_context, path) for every legacy review/resolution candidate file."""
    seen: set[str] = set()
    items: list[tuple[str, Path]] = []
    for folder, label in RESOLVER_CANDIDATE_FOLDERS:
        if not folder.exists():
            continue
        for path in _iter_folder_candidates(folder):
            if path.suffix.lower() == ".json":
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            items.append((f"review:{label}", path))
    return items


def _clean_orphan_review_sidecars(*, dry_run: bool = True) -> int:
    removed = 0
    for folder in REVIEW_DUST_FOLDERS:
        if not folder.exists():
            continue
        for sidecar in folder.glob("*.reason.json"):
            pdf_name = sidecar.name[: -len(".reason.json")]
            pdf = folder / pdf_name
            if not pdf.exists():
                if not dry_run:
                    sidecar.unlink(missing_ok=True)
                    retry = folder / f"{pdf_name}.review-retry.json"
                    retry.unlink(missing_ok=True)
                removed += 1
    return removed


def count_review_dust_pdfs() -> int:
    total = 0
    for folder in REVIEW_DUST_FOLDERS:
        if folder.exists():
            total += len(list(folder.rglob("*.pdf")))
    return total


def drain_legacy_recovery_pending_files(
    *,
    run_id: str = "legacy-recovery-pending-drain",
    allow_paid_api: bool = True,
    allow_opus_resolution: bool = True,
    allow_accept_to_db: bool = False,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Resolve all PDFs in review/recovery-pending via disposition_transaction."""
    config.ensure_dirs()
    folder = config.REVIEW_RECOVERY_PENDING
    paths = sorted(folder.glob("*.pdf")) if folder.exists() else []
    results: list[dict[str, Any]] = []
    sonnet_calls = opus_calls = 0

    def log(msg: str) -> None:
        if log_fn:
            log_fn(msg)
        else:
            print(msg, flush=True)

    _clean_orphan_review_sidecars(dry_run=False)
    for path in paths:
        log(f"Adjudicating legacy recovery-pending: {path.name}")
        tx = disposition_transaction(
            path,
            {
                "source_context": "legacy-recovery-pending",
                "run_id": run_id,
                "allow_paid_api": allow_paid_api,
                "allow_opus_resolution": allow_opus_resolution,
                "allow_accept_to_db": allow_accept_to_db,
                "execute": True,
            },
        )
        if tx.get("sonnet_attempted"):
            sonnet_calls += 1
        if tx.get("opus_attempted"):
            opus_calls += 1
        results.append(
            {
                "filename": path.name,
                "terminal_action": tx.get("terminal_action"),
                "deleted": tx.get("deleted"),
                "pending_dest": tx.get("pending_dest"),
                "linked_parent": tx.get("linked_parent"),
                "sonnet_attempted": tx.get("sonnet_attempted"),
                "opus_attempted": tx.get("opus_attempted"),
            }
        )
    _clean_orphan_review_sidecars(dry_run=False)
    remaining = len(list(folder.glob("*.pdf"))) if folder.exists() else 0
    return {
        "resolved": len(results),
        "results": results,
        "review_recovery_pending_remaining": remaining,
        "sonnet_calls": sonnet_calls,
        "opus_calls": opus_calls,
        "review_dust_pdfs_remaining": count_review_dust_pdfs(),
    }


def drain_review_dust_piles(
    *,
    execute: bool = False,
    allow_paid_api: bool = False,
    allow_opus_resolution: bool = False,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Deterministically drain legacy review dust (no new dust piles)."""
    from child_document_matching import ChildDocumentRunContext

    dry_run = not execute
    counts = DispositionCounts()
    child_ctx = ChildDocumentRunContext()
    actions: list[dict[str, Any]] = []
    reported_needs_model: list[str] = []

    def log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    orphan_sidecars = _clean_orphan_review_sidecars(dry_run=dry_run)
    log(f"Orphan review sidecars removed: {orphan_sidecars}")

    for folder in REVIEW_DUST_FOLDERS:
        if not folder.exists():
            continue
        label = folder.name if folder.parent == config.REVIEW_DIR else str(folder.relative_to(config.BASE))
        for path in list(_iter_folder_candidates(folder)):
            if path.suffix.lower() == ".json":
                continue
            tx = disposition_transaction(
                path,
                {
                    "source_context": f"review-drain:{label}",
                    "run_id": "review-dust-drain",
                    "allow_paid_api": allow_paid_api and execute,
                    "allow_opus_resolution": allow_opus_resolution and allow_paid_api and execute,
                    "allow_accept_to_db": False,
                    "execute": execute,
                    "dry_run": dry_run,
                },
                child_ctx=child_ctx,
                counts=counts,
            )
            actions.append(
                {
                    "source": label,
                    "filename": path.name,
                    "terminal_action": tx.get("terminal_action"),
                    "action": tx.get("action"),
                }
            )

    _clean_orphan_review_sidecars(dry_run=dry_run)
    return {
        "dry_run": dry_run,
        "orphan_sidecars_removed": orphan_sidecars,
        "reported_needs_model": reported_needs_model,
        "counts": counts.to_dict(),
        "review_dust_pdfs_remaining": count_review_dust_pdfs(),
        "actions_sample": actions[:30],
        "actions_total": len(actions),
    }


def normalize_legacy_locations(
    *,
    execute: bool = False,
    allow_paid_api: bool = False,
    allow_opus_resolution: bool = False,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run disposition engine over legacy review/recovery folders and staging."""
    return drain_review_dust_piles(
        execute=execute,
        allow_paid_api=allow_paid_api,
        allow_opus_resolution=allow_opus_resolution,
        log_fn=log_fn,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Drain legacy review dust via disposition engine")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-paid-api", action="store_true")
    parser.add_argument("--allow-opus-resolution", action="store_true")
    args = parser.parse_args(argv)
    config.ensure_dirs()
    summary = drain_review_dust_piles(
        execute=args.execute,
        allow_paid_api=args.allow_paid_api,
        allow_opus_resolution=args.allow_opus_resolution and args.allow_paid_api,
        log_fn=print,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
