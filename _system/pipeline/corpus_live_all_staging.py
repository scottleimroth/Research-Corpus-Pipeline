"""All-staging live ingest: process every root-level PDF in papers-staging with Sonnet evaluation."""

from __future__ import annotations

import atexit
import base64
import io
import json
import mimetypes
import os
import signal
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import anthropic

import config
import papers_db
from add_paper import process_paper
from corpus_modes import normalize_eval_mode
from corpus_run_planner import build_run_plan, write_run_plan
from eval_completion import SONNET_UNAVAILABLE_MSG, is_ingest_evaluation_complete
from evaluate_paper import evaluate_one
from cost_tracking import (
    accumulate_opus_usage,
    accumulate_sonnet_usage,
    accumulate_vision_usage,
    fetch_usd_to_aud_rate,
    update_summary_costs,
)
from child_document_matching import (
    ChildDocumentRunContext,
    dedupe_pending_child_documents,
    finalize_pending_child_documents,
    retry_pending_child_documents,
)
import intake_rejected
from file_disposition_engine import (
    DispositionRequest,
    disposition_file,
    DispositionCounts,
    execute_file_disposition,
    list_staging_candidates,
    map_recovery_decision,
    stop_file_with_final_action,
    drain_review_dust_piles,
    count_review_dust_pdfs,
    normalize_final_action,
    disposition_transaction,
)

WORKFLOW_TARGET = "all-staging"
INGEST_TAG = "all-staging-ingest"
LOCK_PATH = config.LOGS_DIR / "all_staging.ingest.lock"

_lock_held = False


def _configure_stdio_for_ingest() -> None:
    """Keep Windows console encoding from crashing on PDF metadata characters."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


_configure_stdio_for_ingest()

# Per-run adjudication cache: sha256 -> {coverage_status, recovery_attempted, terminal}.
# Prevents reprocessing the same blocked file/hash for the same blocked reason
# within a single run (no blind retry loop).
_RUN_DECISION_CACHE: dict[str, dict[str, Any]] = {}


def reset_run_decision_cache() -> None:
    _RUN_DECISION_CACHE.clear()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _acceptance_block_snapshot() -> dict[str, Any] | None:
    """Consume the acceptance-gate block from the most recent process_paper call."""
    try:
        from add_paper import pop_last_acceptance_block
    except ImportError:
        return None
    block = pop_last_acceptance_block()
    return dict(block) if block else None


def _requires_acceptance_block_adjudication(block: dict[str, Any]) -> bool:
    """True when a gate-blocked file must be adjudicated before any retry/requeue.

    Duplicate/collision blocks are deterministic terminal decisions. Evidence
    and identity blocks must fall through the recovery path first:
    OCR/re-extract/deterministic metadata/model ladder -> one gate retry ->
    terminal disposition only if still rejected.
    """
    action = str(block.get("action") or "")
    return action in {"REJECT_DUPLICATE", "REJECT_NEEDS_RECOVERY", "REJECT_INSUFFICIENT_EVIDENCE"}


def _adjudicate_acceptance_block_if_needed(
    staging_path: Path,
    *,
    client: Any,
    allow_paid_api: bool,
    allow_opus: bool,
    supplement_ctx: ChildDocumentRunContext | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Adjudicate acceptance-gate rejections before any retry/terminal path."""
    block = _acceptance_block_snapshot()
    if not block or not staging_path.exists():
        return None, None
    if block.get("filename") != staging_path.name:
        return None, None
    if not _requires_acceptance_block_adjudication(block):
        return None, None
    adjudication = adjudicate_blocked_file(
        staging_path,
        block,
        client=client,
        allow_paid_api=allow_paid_api,
        allow_opus=allow_opus,
        supplement_ctx=supplement_ctx,
    )
    return block, adjudication


_LLM_ADJ_ALLOWED_DECISIONS = {
    "INGEST_RESEARCH_PAPER",
    "INGEST_NON_RATABLE_REFERENCE",
    "DELETE_CONFIRMED_JUNK",
    "DELETE_TRUE_CORRUPT_UNUSABLE",
    "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
}


def _extract_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _pdf_vision_inputs(staging_path: Path, *, max_pages: int = 2) -> list[dict[str, str]]:
    inputs: list[dict[str, str]] = []
    try:
        import pypdfium2 as pdfium
    except Exception:
        return inputs
    doc = None
    try:
        doc = pdfium.PdfDocument(str(staging_path))
        page_total = len(doc)
        for idx in range(min(max_pages, page_total)):
            page = doc[idx]
            try:
                pil = page.render(scale=1.8).to_pil()
                buf = io.BytesIO()
                pil.save(buf, format="PNG")
                inputs.append(
                    {
                        "label": f"pdf_page_{idx + 1}",
                        "media_type": "image/png",
                        "data": base64.b64encode(buf.getvalue()).decode("ascii"),
                    }
                )
            finally:
                close_page = getattr(page, "close", None)
                if callable(close_page):
                    try:
                        close_page()
                    except Exception:
                        pass
    except Exception:
        return []
    finally:
        if doc is not None:
            close_doc = getattr(doc, "close", None)
            if callable(close_doc):
                try:
                    close_doc()
                except Exception:
                    pass
    return inputs


def _image_vision_input(staging_path: Path) -> list[dict[str, str]]:
    try:
        from PIL import Image
    except Exception:
        return []
    try:
        with Image.open(staging_path) as im:
            im = im.convert("RGB")
            im.thumbnail((1800, 1800))
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return [
                {
                    "label": "image_preview",
                    "media_type": "image/png",
                    "data": base64.b64encode(buf.getvalue()).decode("ascii"),
                }
            ]
    except Exception:
        return []


def _staging_evidence_for_llm(staging_path: Path, *, attempt_ocr: bool = True) -> dict[str, Any]:
    from document_text_extraction import extract_document_text

    extraction = extract_document_text(staging_path, attempt_ocr=attempt_ocr, use_cache=True)
    full = str(extraction.text or "")
    excerpt = full
    if len(full) > 18000:
        excerpt = full[:12000] + "\n...[TRUNCATED]...\n" + full[-6000:]
    ext = staging_path.suffix.lower()
    mime = mimetypes.guess_type(staging_path.name)[0] or "application/octet-stream"
    vision_inputs: list[dict[str, str]] = []
    if ext == ".pdf":
        vision_inputs = _pdf_vision_inputs(staging_path, max_pages=2)
    elif ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}:
        vision_inputs = _image_vision_input(staging_path)
    return {
        "observations": {
            "filename": staging_path.name,
            "extension": ext,
            "mime_type": mime,
            "file_size_bytes": int(staging_path.stat().st_size) if staging_path.exists() else 0,
            "page_count": extraction.page_count,
            "text_length": extraction.text_length,
            "extractor_status": extraction.extractor_status,
            "extraction_source": extraction.extraction_source,
            "excerpt": excerpt,
        },
        "vision_inputs": vision_inputs,
    }


def _scan_like_pdf_observations(obs: dict[str, Any]) -> bool:
    """Heuristic: many pages but almost no extractable text (image-only / scan PDF)."""
    pages = int(obs.get("page_count") or 0)
    tlen = int(obs.get("text_length") or 0)
    if pages < 3:
        return False
    if tlen >= 200:
        return False
    if pages > 0 and (tlen / pages) < 15:
        return True
    return tlen < 200


def _llm_final_adjudication_for_block(
    staging_path: Path,
    block: dict[str, Any],
    *,
    client: Any,
    allow_opus: bool,
) -> dict[str, Any]:
    coverage = str(block.get("coverage_status") or "")
    is_blocked_adjudication = block.get("task") != "staging_disposition_review"
    skip_ocr = is_blocked_adjudication and coverage in {
        "INSUFFICIENT_FOR_EVALUATION",
        "PARTIAL_NEEDS_RECOVERY",
        "TRUE_EMPTY_TEXT",
    }
    payload = _staging_evidence_for_llm(staging_path, attempt_ocr=not skip_ocr)
    allowed_decisions = set(block.get("allowed_decisions") or _LLM_ADJ_ALLOWED_DECISIONS)
    if block.get("task") == "staging_disposition_review":
        system_prompt = (
            "You are a strict corpus-ingest adjudicator for files dropped in papers-staging/. "
            "Return JSON only. No prose. "
            "Choose exactly one decision from: "
            + ", ".join(sorted(allowed_decisions))
            + ". "
            "Judge from actual file content (and image preview when provided), not filename alone. "
            "Use HOLD_PENDING_PARENT only for genuine journal supplements/child documents (e.g. mmc). "
            "Use DELETE_CONFIRMED_JUNK for payslips, receipts, payroll screenshots, personal tax spreadsheets, "
            "and other clearly non-research files."
        )
    else:
        system_prompt = (
            "You are a strict corpus-ingest adjudicator for mixed file types. "
            "Return JSON only. No prose. "
            "Choose exactly one decision from: "
            "INGEST_RESEARCH_PAPER, INGEST_NON_RATABLE_REFERENCE, "
            "DELETE_CONFIRMED_JUNK, DELETE_TRUE_CORRUPT_UNUSABLE, "
            "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION."
        )
    if block.get("task") == "staging_disposition_review":
        user_prompt = json.dumps(
            {
                "task": "Final LLM check for a file in papers-staging/ before junk, pending-child, or ingest.",
                "deterministic_proposal": {
                    "classification": block.get("proposed_classification"),
                    "action": block.get("proposed_action"),
                    "reason": block.get("proposed_reason"),
                },
                "rules": block.get("rules") or {},
                "file_observations": payload.get("observations") or {},
                "vision_inputs_count": len(payload.get("vision_inputs") or []),
                "vision_input_labels": [v.get("label") for v in (payload.get("vision_inputs") or [])],
                "required_json_schema": {
                    "decision": "one of allowed decisions",
                    "confidence": "0..1 number",
                    "reason": "short string",
                    "signals_for": ["list of concise signals supporting the decision"],
                },
            },
            ensure_ascii=False,
        )
    else:
        user_prompt = json.dumps(
            {
                "task": "Final adjudication for a staging file blocked by deterministic acceptance gating.",
                "rules": {
                    "prefer_ingest_for_real_papers": True,
                    "receipts_vouchers_invoices_travel_confirmations": "DELETE_CONFIRMED_JUNK",
                    "delete_corrupt_only_when_unreadable_or_broken": True,
                    "if_uncertain_choose_best_supported_decision_not_review": True,
                },
                "blocked_acceptance": block,
                "file_observations": payload.get("observations") or {},
                "vision_inputs_count": len(payload.get("vision_inputs") or []),
                "vision_input_labels": [v.get("label") for v in (payload.get("vision_inputs") or [])],
                "required_json_schema": {
                    "decision": "one of allowed decisions",
                    "confidence": "0..1 number",
                    "reason": "short string",
                    "signals_for": ["list of concise signals supporting the decision"],
                },
            },
            ensure_ascii=False,
        )
    content_blocks: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
    for img in payload.get("vision_inputs") or []:
        data = str(img.get("data") or "")
        if not data:
            continue
        content_blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": str(img.get("media_type") or "image/png"),
                    "data": data,
                },
            }
        )

    from pipeline_llm import call_anthropic_vision_json, call_llm_json_escalation, public_vision_available

    obs = payload.get("observations") or {}
    scan_like = _scan_like_pdf_observations(obs)
    vision_inputs = payload.get("vision_inputs") or []
    ext = str(obs.get("extension") or staging_path.suffix.lower())
    # DeepSeek V4 Pro is text-only; scan PDFs and image drops need vision before guessing.
    use_vision_first = bool(
        vision_inputs
        and public_vision_available()
        and (scan_like or ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"})
    )
    from eval_model_config import VISION_ESCALATION_MIN_CONFIDENCE

    vision_kw = {
        "min_confidence": VISION_ESCALATION_MIN_CONFIDENCE,
        "valid_decisions": _LLM_ADJ_ALLOWED_DECISIONS,
    }

    first_usage: dict[str, Any] = {}
    first_data: dict[str, Any] | None = None
    first_model: str | None = None
    if use_vision_first:
        first_data, first_usage, first_model = call_anthropic_vision_json(
            system=system_prompt,
            content_blocks=content_blocks,
            max_tokens=700,
            **vision_kw,
        )
    else:
        try:
            first_data, first_usage, first_model = call_llm_json_escalation(
                system_prompt,
                user_prompt,
                max_tokens=700,
            )
        except Exception as exc:
            first_data = None
            first_usage = {"error": str(exc)}

        if not first_data and vision_inputs and public_vision_available():
            first_data, first_usage, first_model = call_anthropic_vision_json(
                system=system_prompt,
                content_blocks=content_blocks,
                max_tokens=700,
                **vision_kw,
            )

    if not first_data:
        return {
            "ok": False,
            "error": "llm_unparseable_response",
            "sonnet_usage": first_usage,
        }

    decision = str(first_data.get("decision") or "").strip().upper()
    confidence = float(first_data.get("confidence") or 0.0)
    if decision not in allowed_decisions:
        return {
            "ok": False,
            "error": f"invalid_decision:{decision or 'missing'}",
            "sonnet_usage": first_usage,
        }

    from eval_model_config import staging_needs_opus_adjudication

    if not (
        allow_opus
        and config.ANTHROPIC_API_KEY
        and staging_needs_opus_adjudication(decision, confidence)
    ):
        return {
            "ok": True,
            "decision": decision,
            "confidence": confidence,
            "final_model": first_model or "unknown",
            "sonnet": first_data,
            "sonnet_usage": first_usage,
            "scan_like_pdf": scan_like,
        }

    opus_data, opus_usage, opus_model = call_anthropic_vision_json(
        system=system_prompt,
        content_blocks=content_blocks,
        max_tokens=700,
        models=(config.OPUS_RESOLUTION_MODEL,),
    )
    if not opus_data:
        return {
            "ok": True,
            "decision": decision,
            "confidence": confidence,
            "final_model": first_model or "unknown",
            "sonnet": first_data,
            "sonnet_usage": first_usage,
            "opus_usage": opus_usage,
        }
    opus_decision = str(opus_data.get("decision") or "").strip().upper()
    opus_confidence = float(opus_data.get("confidence") or 0.0)
    if opus_decision not in allowed_decisions:
        return {
            "ok": False,
            "error": f"opus_invalid_decision:{opus_decision or 'missing'}",
            "sonnet_usage": first_usage,
            "opus_usage": opus_usage,
        }
    return {
        "ok": True,
        "decision": opus_decision,
        "confidence": opus_confidence,
        "final_model": opus_model or config.OPUS_RESOLUTION_MODEL,
        "sonnet": first_data,
        "opus": opus_data,
        "sonnet_usage": first_usage,
        "opus_usage": opus_usage,
        "scan_like_pdf": scan_like,
    }


def _apply_llm_adjudication_decision(
    staging_path: Path,
    llm: dict[str, Any],
    *,
    reason_prefix: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if not llm.get("ok"):
        return {
            "terminal": True,
            "system_integrity_failure": True,
            "action": "SYSTEM_INTEGRITY_FAILURE",
            "reason": f"{reason_prefix}:llm_adjudication_failed:{llm.get('error')}",
            "llm_adjudication": llm,
        }
    final_action = str(llm.get("decision") or "")
    if final_action in {"INGEST_RESEARCH_PAPER", "INGEST_NON_RATABLE_REFERENCE"}:
        scan_like = bool(llm.get("scan_like_pdf"))
        return {
            "routed": False,
            "recovered_to_staging": str(staging_path),
            "decision": final_action,
            "llm_force_retry_ingest": True,
            "llm_force_acceptance": True,
            "llm_force_full_ocr": scan_like,
            "llm_force_non_ratable": final_action == "INGEST_NON_RATABLE_REFERENCE",
            "reason": f"{reason_prefix}:{final_action.lower()}",
            "llm_adjudication": llm,
        }

    executed = stop_file_with_final_action(
        staging_path,
        action=final_action,
        reason=f"{reason_prefix}:{final_action.lower()}",
        source_context="staging-ingest-failure",
        evidence={**evidence, "llm_adjudication": llm},
    )
    terminal = {"terminal": True, "llm_adjudication": llm, **executed}
    if final_action == "DELETE_CONFIRMED_DUPLICATE":
        terminal["deleted_duplicate"] = True
    elif final_action == "DELETE_CONFIRMED_JUNK":
        terminal["deleted_junk"] = True
    elif final_action == "DELETE_TRUE_CORRUPT_UNUSABLE":
        terminal["deleted_corrupt"] = True
    elif final_action == "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION":
        terminal["deleted_unrecoverable"] = True
    return terminal


def adjudicate_blocked_file(
    staging_path: Path,
    block: dict[str, Any],
    *,
    client: Any = None,
    allow_paid_api: bool = False,
    allow_opus: bool = False,
    supplement_ctx: ChildDocumentRunContext | None = None,
) -> dict[str, Any]:
    """A coverage/acceptance block is not a final state — adjudicate it now.

    One file -> deterministic recovery + (optional) model ladder -> one terminal
    action. Guarded by a per-run hash cache so the same file/hash is never
    reprocessed for the same blocked reason (no blind retry).
    """
    staging_path = Path(staging_path)
    sha = str(block.get("sha256") or "")
    filename = str(block.get("filename") or staging_path.name or "")
    cache_key = sha or f"file:{filename}"
    cov_status = str(block.get("coverage_status") or "")
    blocked_signature = "|".join(
        [
            cov_status,
            str(block.get("reason") or ""),
            str(block.get("action") or ""),
            str(block.get("required_next_action") or ""),
        ]
    )

    cached = _RUN_DECISION_CACHE.get(cache_key) if cache_key else None
    if (
        cached
        and cached.get("blocked_signature") == blocked_signature
        and cached.get("recovery_attempted")
    ):
        terminal = dict(cached.get("terminal") or {})
        terminal["from_run_cache"] = True
        return terminal

    block_action = str(block.get("action") or "")
    if block_action == "REJECT_DUPLICATE":
        terminal_raw = stop_file_with_final_action(
            staging_path,
            action="DELETE_CONFIRMED_DUPLICATE",
            reason=f"acceptance_block:{block.get('reason') or 'duplicate'}",
            source_context="staging-ingest-failure",
            evidence={"acceptance_block": block},
        )
        forced_duplicate_unlink = False
        if staging_path.exists():
            # Metadata/DOI-collision duplicates may not have an owned hash match
            # for the lower-level duplicate helper. The acceptance block is
            # already terminal; do not leave the same file in staging.
            staging_path.unlink(missing_ok=True)
            forced_duplicate_unlink = True
        terminal = {
            "terminal": True,
            "deleted_duplicate": bool(
                terminal_raw.get("deleted")
                or forced_duplicate_unlink
                or terminal_raw.get("action") == "DELETE_CONFIRMED_DUPLICATE"
            ),
            "forced_duplicate_unlink": forced_duplicate_unlink,
            **terminal_raw,
        }
    else:
        if allow_paid_api:
            _log(
                f"  LLM final adjudication for blocked ingest "
                f"({cov_status or block.get('reason') or 'blocked'})..."
            )
            llm_client = client or anthropic.Anthropic(timeout=120.0)
            llm = _llm_final_adjudication_for_block(
                staging_path,
                block,
                client=llm_client,
                allow_opus=allow_opus and allow_paid_api,
            )
            terminal = _apply_llm_adjudication_decision(
                staging_path,
                llm,
                reason_prefix="llm_final_adjudication",
                evidence={"acceptance_block": block},
            )
        else:
            # Local-only mode: keep deterministic recovery path.
            terminal = _attempt_recovery_before_review(
                staging_path,
                f"coverage_block:{cov_status or block.get('reason')}",
                allow_paid_api=allow_paid_api,
                allow_opus=allow_opus,
                supplement_ctx=supplement_ctx,
                terminal_on_ingest=False,
            )
    if cache_key:
        _RUN_DECISION_CACHE[cache_key] = {
            "blocked_signature": blocked_signature,
            "recovery_attempted": True,
            "terminal": terminal,
        }
    return terminal


def _result(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": ok, "detail": detail}


def _log(msg: str, *, log_fp=None) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe = msg.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe, flush=True)
    if log_fp:
        log_fp.write(msg + "\n")
        log_fp.flush()


def list_root_staging_pdfs() -> list[Path]:
    """All staging candidate files (PDF and non-PDF). Legacy name retained."""
    return list_staging_candidates()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _release_lock() -> None:
    global _lock_held
    if not _lock_held or not LOCK_PATH.exists():
        return
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        if data.get("pid") == os.getpid():
            LOCK_PATH.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError):
        pass
    _lock_held = False


def acquire_ingest_lock(run_id: str) -> tuple[bool, str, dict[str, Any] | None]:
    """Single-instance lock. Stale locks (dead pid) are replaced."""
    global _lock_held
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stale_info = None
    if LOCK_PATH.exists():
        try:
            existing = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        old_pid = int(existing.get("pid") or 0)
        if old_pid and _pid_alive(old_pid):
            return (
                False,
                f"Another all-staging ingest is running (pid={old_pid}, run_id={existing.get('run_id')}). "
                "Stop that process or remove the stale lock only if no process is alive.",
                existing,
            )
        stale_info = {"recovered_stale_lock": True, "previous": existing}

    LOCK_PATH.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "run_id": run_id,
                "started_at": _now(),
                "stale_recovery": stale_info,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _lock_held = True
    atexit.register(_release_lock)
    return True, "", stale_info


def _paper_id_from_latest_insert(staging_filename: str) -> str | None:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, file_info FROM papers ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        raw = row["file_info"]
        if raw:
            try:
                fi = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(fi, dict) and fi.get("original_filename") == staging_filename:
                    return row["id"]
            except (TypeError, json.JSONDecodeError):
                pass
        return row["id"]
    finally:
        conn.close()


def _paper_has_tag(paper: dict, tag: str) -> bool:
    tags = paper.get("tags")
    if isinstance(tags, list):
        return tag in tags
    if isinstance(tags, str):
        return tag in tags
    return False


def cleanup_incomplete_staging_rows() -> list[dict[str, Any]]:
    """Remove incomplete all-staging-ingest DB rows and their source PDFs."""
    cleaned: list[dict[str, Any]] = []
    for paper in papers_db.get_all_metadata():
        if not _paper_has_tag(paper, INGEST_TAG):
            continue
        ok, reason = is_ingest_evaluation_complete(paper)
        if ok:
            continue
        pid = paper.get("id")
        if not pid:
            continue
        rb = _rollback_paper(pid)
        cleaned.append({"paper_id": pid, "reason": reason, "rollback": rb})
    return cleaned


def _rollback_paper(paper_id: str) -> dict[str, Any]:
    paper = papers_db.get_paper(paper_id)
    removed_pdf = None
    if paper:
        fi = paper.get("file_info") or {}
        rel = fi.get("filepath") or fi.get("managed_pdf_relative_path") or ""
        if rel:
            pdf_path = Path(rel) if Path(rel).is_absolute() else config.MASTER_PROJECT / rel
            if pdf_path.exists():
                pdf_path.unlink()
                removed_pdf = str(pdf_path)
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
        conn.commit()
    finally:
        conn.close()
    return {"paper_id": paper_id, "removed_source_pdf": removed_pdf}


def _reject_staging_file(
    staging_pdf: Path,
    reason: str,
    *,
    allow_paid_api: bool = False,
    allow_opus: bool = False,
    supplement_ctx: ChildDocumentRunContext | None = None,
) -> dict[str, Any]:
    """Terminal per-file rejection — never routes to review dust piles."""
    if not staging_pdf.exists():
        return {"terminal": True, "deleted": True, "reason": "file_missing"}
    result = stop_file_with_final_action(
        staging_pdf,
        action="DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
        reason=f"staging_reject:{reason}",
        source_context="staging-ingest-failure",
        evidence={"reject_reason": reason},
    )
    action = str(result.get("action") or "")
    if result.get("deleted") or action in {
        "DELETE_CONFIRMED_DUPLICATE",
        "DELETE_CONFIRMED_JUNK",
        "DELETE_TRUE_CORRUPT_UNUSABLE",
        "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
    }:
        key = (
            "deleted_duplicate"
            if action == "DELETE_CONFIRMED_DUPLICATE"
            else "deleted_corrupt"
            if action == "DELETE_TRUE_CORRUPT_UNUSABLE"
            else "deleted_unrecoverable"
            if action == "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION"
            else "deleted_junk"
        )
        return {"terminal": True, "deleted": True, key: True, **result}
    if result.get("system_integrity_failure"):
        return {"terminal": True, "system_integrity_failure": True, **result}
    return {"terminal": True, "deleted_unrecoverable": True, **result}


def _attempt_recovery_before_review(
    staging_pdf: Path,
    reason: str,
    *,
    allow_paid_api: bool = False,
    allow_opus: bool = False,
    supplement_ctx: ChildDocumentRunContext | None = None,
    terminal_on_ingest: bool = False,
) -> dict[str, Any]:
    """Run central disposition engine before any review-folder routing."""
    if not staging_pdf.exists():
        return {"routed": False, "reason": "file_missing"}

    result = disposition_file(
        staging_pdf,
        source_context="staging-ingest-failure",
        allow_paid_api=allow_paid_api,
        allow_opus_resolution=allow_opus,
        child_ctx=supplement_ctx,
    )
    action = str(result.get("action") or "")

    if result.get("needs_ingest") or action in {"INGEST_RESEARCH_PAPER", "INGEST_NON_RATABLE_REFERENCE"}:
        dest = Path(result.get("path") or staging_pdf)
        return {"routed": False, "recovered_to_staging": str(dest), "decision": action, **result}

    if action == "DELETE_CONFIRMED_DUPLICATE":
        return {"routed": False, "deleted_duplicate": True, **result}

    if action == "DELETE_CONFIRMED_JUNK":
        return {"routed": False, "deleted_junk": True, **result}
    if action == "DELETE_TRUE_CORRUPT_UNUSABLE":
        return {"routed": False, "deleted_corrupt": True, **result}

    if action == "HOLD_PENDING_PARENT":
        return {"routed": False, "child_pending": result.get("dest"), **result}

    if action == "LINK_CHILD_TO_PARENT":
        return {"terminal": True, "child_linked": result.get("linked_parent"), **result}

    if action == "SYSTEM_INTEGRITY_FAILURE":
        return {"terminal": True, "system_integrity_failure": True, **result}

    if action in {
        "DELETE_CONFIRMED_DUPLICATE",
        "DELETE_CONFIRMED_JUNK",
        "DELETE_TRUE_CORRUPT_UNUSABLE",
        "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
    }:
        key = (
            "deleted_duplicate"
            if action == "DELETE_CONFIRMED_DUPLICATE"
            else "deleted_corrupt"
            if action == "DELETE_TRUE_CORRUPT_UNUSABLE"
            else "deleted_unrecoverable"
            if action == "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION"
            else "deleted_junk"
        )
        return {"terminal": True, key: True, **result}

    return _reject_staging_file(
        staging_pdf,
        str(result.get("reason") or "unresolved_disposition"),
        allow_paid_api=allow_paid_api,
        allow_opus=allow_opus,
        supplement_ctx=supplement_ctx,
    )


def _ingest_extraction_snapshot() -> dict[str, Any]:
    try:
        from add_paper import LAST_INGEST_EXTRACTION
    except ImportError:
        return {}
    return dict(LAST_INGEST_EXTRACTION or {})


def _is_extraction_integrity_failure_for_terminalization(extraction: dict[str, Any]) -> bool:
    """Only true extraction/tool failures should become system integrity failures here."""
    if not extraction:
        return False
    status = str(extraction.get("extractor_status") or "")
    text_length = int(extraction.get("text_length") or 0)
    if status == "TEXT_OK" and text_length >= 200:
        return False
    from document_text_extraction import extraction_failure_terminal_action

    return extraction_failure_terminal_action(extraction) == "SYSTEM_INTEGRITY_FAILURE"


def _terminalize_failed_ingest_candidate(
    staging_pdf: Path,
    reason: str,
    *,
    allow_paid_api: bool,
    allow_opus: bool,
    supplement_ctx: ChildDocumentRunContext | None,
    first_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a terminal disposition when a file already failed ingest/evaluation."""
    from document_text_extraction import extraction_failure_terminal_action, format_extraction_diagnostic
    from recovery_ladder import run_recovery_ladder

    staging_pdf = Path(staging_pdf)
    if not staging_pdf.exists():
        return {"routed": False, "reason": "file_missing_after_recovery"}

    extraction = _ingest_extraction_snapshot()
    if extraction:
        final_action = extraction_failure_terminal_action(extraction)
        if final_action == "SYSTEM_INTEGRITY_FAILURE" and _is_extraction_integrity_failure_for_terminalization(extraction):
            decision = {
                "path": str(staging_pdf),
                "filename": staging_pdf.name,
                "source_context": "staging-ingest-failure",
                "action": final_action,
                "reason": f"ingest_extraction:{extraction.get('extractor_status')}:{reason}",
                "evidence": {
                    "ingest_failure_reason": reason,
                    "extraction": extraction,
                    "first_decision": first_decision or {},
                },
            }
            req = DispositionRequest(
                path=staging_pdf,
                source_context="staging-ingest-failure",
                allow_paid_api=allow_paid_api,
                allow_opus_resolution=allow_opus and allow_paid_api,
                allow_file_move=False,
            )
            executed = execute_file_disposition(decision, req, child_ctx=supplement_ctx)
            result = {**decision, **executed, "decision": final_action}
            return {"terminal": True, "system_integrity_failure": True, **result}

    ladder = run_recovery_ladder(
        staging_pdf,
        allow_paid_api=allow_paid_api,
        allow_opus=allow_opus and allow_paid_api,
        dry_run=False,
        force_model=True,
    )
    recovery_decision = str(ladder.get("decision") or "")
    final_action = map_recovery_decision(recovery_decision)
    evidence = {
        "ingest_failure_reason": reason,
        "first_decision": first_decision or {},
        "recovery_ladder": ladder,
        "extraction": extraction or _ingest_extraction_snapshot(),
    }
    terminal_reason = str((ladder.get("evidence") or {}).get("reason") or recovery_decision or reason)
    if extraction and not terminal_reason.startswith("ingest_extraction"):
        terminal_reason = f"{format_extraction_diagnostic(extraction)}; {terminal_reason}"

    if final_action in {"INGEST_RESEARCH_PAPER", "INGEST_NON_RATABLE_REFERENCE"}:
        if extraction and _is_extraction_integrity_failure_for_terminalization(extraction):
            final_action = "SYSTEM_INTEGRITY_FAILURE"
            terminal_reason = f"ingest_extraction_unresolved:{extraction.get('extractor_status')}:{reason}"
        else:
            scan_like = False
            if extraction:
                from first_pass_finalize import _scan_like_pdf_observations

                scan_like = _scan_like_pdf_observations(
                    {
                        "page_count": extraction.get("page_count"),
                        "text_length": extraction.get("text_length"),
                    }
                )
            return {
                "routed": False,
                "recovered_to_staging": str(staging_pdf),
                "decision": final_action,
                "llm_force_retry_ingest": True,
                "llm_force_acceptance": True,
                "llm_force_full_ocr": scan_like,
                "llm_force_non_ratable": final_action == "INGEST_NON_RATABLE_REFERENCE",
                "reason": terminal_reason,
                "recovery_ladder": ladder,
                "llm_adjudication": ladder.get("llm") if isinstance(ladder.get("llm"), dict) else {},
            }

    final_action = normalize_final_action(final_action)

    decision = {
        "path": str(staging_pdf),
        "filename": staging_pdf.name,
        "source_context": "staging-ingest-failure",
        "action": final_action,
        "reason": terminal_reason,
        "evidence": evidence,
        "child_type": (ladder.get("evidence") or {}).get("child_type"),
        "parent_id": None,
    }
    req = DispositionRequest(
        path=staging_pdf,
        source_context="staging-ingest-failure",
        allow_paid_api=allow_paid_api,
        allow_opus_resolution=allow_opus and allow_paid_api,
        allow_file_move=True,
    )
    executed = execute_file_disposition(decision, req, child_ctx=supplement_ctx)
    result = {**decision, **executed, "recovery_ladder": ladder, "decision": recovery_decision}

    if final_action == "DELETE_CONFIRMED_DUPLICATE":
        return {"terminal": True, "deleted_duplicate": True, **result}
    if final_action == "DELETE_CONFIRMED_JUNK":
        return {"terminal": True, "deleted_junk": True, **result}
    if final_action == "DELETE_TRUE_CORRUPT_UNUSABLE":
        return {"terminal": True, "deleted_corrupt": True, **result}
    if final_action == "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION":
        return {"terminal": True, "deleted_unrecoverable": True, **result}
    if final_action == "HOLD_PENDING_PARENT":
        return {"terminal": True, "child_pending": result.get("dest"), **result}
    if final_action == "LINK_CHILD_TO_PARENT":
        return {"terminal": True, "child_linked": result.get("linked_parent"), **result}
    if final_action == "SYSTEM_INTEGRITY_FAILURE":
        return {"terminal": True, "system_integrity_failure": True, **result}
    return {"terminal": True, "deleted_unrecoverable": True, **result}


def _terminal_recovery_outcome(recovery: dict[str, Any], *, default_reason: str, stage: str) -> dict[str, Any]:
    if recovery.get("llm_force_retry_ingest"):
        return {
            "status": "ingest_retry_required",
            "reason": recovery.get("reason") or default_reason,
            "stage": stage,
            "adjudication": recovery,
        }
    if recovery.get("recovered_to_staging"):
        return {
            "status": "moved_to_rejected",
            "reason": recovery.get("reason") or default_reason,
            "stage": stage,
        }
    if recovery.get("deleted"):
        action = str(recovery.get("action") or "")
        if action == "DELETE_CONFIRMED_DUPLICATE":
            return {"status": "confirmed_duplicate_deleted", "reason": default_reason, "stage": stage}
        if action == "DELETE_TRUE_CORRUPT_UNUSABLE":
            return {"status": "deleted_corrupt", "reason": default_reason, "stage": stage}
        if action == "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION":
            return {"status": "deleted_unrecoverable", "reason": default_reason, "stage": stage}
        if action == "DELETE_CONFIRMED_JUNK":
            return {"status": "deleted_junk", "reason": default_reason, "stage": stage}
    if recovery.get("system_integrity_failure"):
        return {
            "status": "system_integrity_failure",
            "reason": recovery.get("reason") or default_reason,
            "stage": stage,
        }
    if recovery.get("deleted_duplicate"):
        return {"status": "confirmed_duplicate_deleted", "reason": default_reason, "stage": stage}
    if recovery.get("deleted_corrupt"):
        return {"status": "deleted_corrupt", "reason": default_reason, "stage": stage}
    if recovery.get("deleted_unrecoverable"):
        return {"status": "deleted_unrecoverable", "reason": default_reason, "stage": stage}
    if recovery.get("deleted_junk"):
        return {"status": "deleted_junk", "reason": default_reason, "stage": stage}
    if recovery.get("child_pending"):
        return {"status": "child_document_pending_parent", "reason": default_reason, "stage": stage}
    if recovery.get("child_linked"):
        return {"status": "child_linked", "reason": default_reason, "stage": stage}
    return {"status": "deleted_unrecoverable", "reason": default_reason, "stage": stage}


def _apply_adjudication_to_process_args(
    process_args: dict[str, Any],
    adj: dict[str, Any],
) -> tuple[dict[str, Any], bool, bool]:
    """Merge LLM/recovery adjudication into ingest args; return (args, llm_forced, force_research_eval)."""
    args = dict(process_args)
    args["llm_force_acceptance"] = bool(adj.get("llm_force_acceptance"))
    if adj.get("llm_force_full_ocr"):
        args["llm_force_full_ocr"] = True
    if adj.get("llm_force_non_ratable"):
        args["llm_force_non_ratable"] = True
        args["non_ratable_preclassification"] = True
    llm_forced = bool(args.get("llm_force_acceptance"))
    force_research = str(adj.get("decision") or "") == "INGEST_RESEARCH_PAPER"
    return args, llm_forced, force_research


def _drive_ingest_with_escalation(
    staging_path: Path,
    process_args: dict[str, Any],
    *,
    max_tries: int = 6,
) -> bool:
    """Retry ingest with escalating OCR/acceptance until row inserted or give up."""
    args = dict(process_args)
    args["llm_force_acceptance"] = True
    for attempt in range(1, max_tries + 1):
        if attempt >= 2:
            args["llm_force_full_ocr"] = True
        print(
            f"  Ingest attempt {attempt}/{max_tries} (forced acceptance, OCR={bool(args.get('llm_force_full_ocr'))})...",
            flush=True,
        )
        if process_paper(str(staging_path), args):
            return True
    return False


def _quick_check_ok() -> tuple[bool, str]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        return (row and row[0] == "ok", row[0] if row else "no_result")
    finally:
        conn.close()


def _preflight_audit_log(run_dir: Path, record: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "all_staging_preflight_dedupe.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    audit_dir = config.LOGS_DIR / "disposition-audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    jsonl = audit_dir / f"preflight-dedupe-{_now()[:10]}.jsonl"
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _now(), **record}, ensure_ascii=False, default=str) + "\n")


def _delete_preflight_duplicate(path: Path, *, reason: str, evidence: dict[str, Any]) -> dict[str, Any]:
    decision = {
        "path": str(path),
        "filename": path.name,
        "source_context": "staging-preflight-dedupe",
        "action": "DELETE_CONFIRMED_DUPLICATE",
        "reason": reason,
        "evidence": evidence,
    }
    req = DispositionRequest(path=path, source_context="staging-preflight-dedupe", allow_file_move=True)
    return execute_file_disposition(decision, req)


def preflight_dedupe_staging_candidates(
    candidates: list[Path],
    *,
    run_dir: Path,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    log_fn: Callable[[str], None] | None = None,
    progress_every_n: int = 10,
    progress_min_interval_s: float = 1.5,
) -> dict[str, Any]:
    """Delete exact hash duplicates before any OCR/metadata/model/evaluation work."""
    from preflight_hash_cache import build_owned_source_hash_index, hash_staging_file

    loop_started = time.monotonic()
    source_hashes, index_stats = build_owned_source_hash_index(log_fn=log_fn)
    staging_loop_started = time.monotonic()
    seen_staging: dict[str, Path] = {}
    kept: list[Path] = []
    deleted_db_owned: list[dict[str, Any]] = []
    deleted_intra_staging: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    stopped = False
    total = len(candidates)
    staging_bytes_hashed = 0
    last_progress_at = 0.0

    def _maybe_progress(rec: dict[str, Any], *, force: bool = False) -> None:
        nonlocal last_progress_at
        if not on_progress:
            return
        now = time.monotonic()
        idx = int(rec.get("index") or 0)
        if force or idx >= total or idx % progress_every_n == 0 or (now - last_progress_at) >= progress_min_interval_s:
            on_progress(rec)
            last_progress_at = now

    for idx, path in enumerate(candidates, 1):
        if should_stop and should_stop():
            stopped = True
            _maybe_progress(
                {
                    "index": idx - 1 if idx > 0 else 0,
                    "total": total,
                    "file": path.name if idx <= total else None,
                    "deleted": len(deleted_db_owned) + len(deleted_intra_staging),
                    "unique": len(kept),
                },
                force=True,
            )
            break
        try:
            digest, nbytes = hash_staging_file(path)
            staging_bytes_hashed += nbytes
        except OSError as e:
            errors.append({"filename": path.name, "error": str(e)})
            kept.append(path)
            _maybe_progress(
                {
                    "index": idx,
                    "total": total,
                    "file": path.name,
                    "deleted": len(deleted_db_owned) + len(deleted_intra_staging),
                    "unique": len(kept),
                }
            )
            continue

        owners = source_hashes.get(digest) or []
        if owners:
            evidence = {
                "sha256": digest,
                "match": "db_owned_source_hash",
                "canonical": owners[0].get("canonical_name"),
                "paper_id": owners[0].get("paper_id"),
            }
            _delete_preflight_duplicate(path, reason="preflight_db_owned_hash_duplicate", evidence=evidence)
            if path.exists():
                errors.append({"filename": path.name, "error": "duplicate_delete_failed"})
                kept.append(path)
            else:
                deleted_db_owned.append({"filename": path.name, **evidence})
            _maybe_progress(
                {
                    "index": idx,
                    "total": total,
                    "file": path.name,
                    "deleted": len(deleted_db_owned) + len(deleted_intra_staging),
                    "unique": len(kept),
                }
            )
            continue

        first = seen_staging.get(digest)
        if first and first.exists():
            evidence = {
                "sha256": digest,
                "match": "intra_staging_hash_duplicate",
                "kept_filename": first.name,
            }
            _delete_preflight_duplicate(path, reason="preflight_intra_staging_hash_duplicate", evidence=evidence)
            if path.exists():
                errors.append({"filename": path.name, "error": "duplicate_delete_failed"})
                kept.append(path)
            else:
                deleted_intra_staging.append({"filename": path.name, **evidence})
            _maybe_progress(
                {
                    "index": idx,
                    "total": total,
                    "file": path.name,
                    "deleted": len(deleted_db_owned) + len(deleted_intra_staging),
                    "unique": len(kept),
                }
            )
            continue

        seen_staging[digest] = path
        kept.append(path)
        _maybe_progress(
            {
                "index": idx,
                "total": total,
                "file": path.name,
                "deleted": len(deleted_db_owned) + len(deleted_intra_staging),
                "unique": len(kept),
            }
        )

    loop_seconds = max(0.001, time.monotonic() - staging_loop_started)
    total_seconds = round(time.monotonic() - loop_started, 3)
    checked = len(deleted_db_owned) + len(deleted_intra_staging) + len(kept)
    duplicates_deleted = len(deleted_db_owned) + len(deleted_intra_staging)
    mb_hashed = staging_bytes_hashed / (1024 * 1024)
    summary = {
        "staging_candidates_before_dedupe": total,
        "checked": checked,
        "db_owned_hash_duplicates_deleted": len(deleted_db_owned),
        "intra_staging_duplicates_deleted": len(deleted_intra_staging),
        "unique_candidates_remaining": len([p for p in kept if p.exists()]),
        "stopped": stopped,
        "errors": errors,
        "deleted_db_owned_sample": deleted_db_owned[:50],
        "deleted_intra_staging_sample": deleted_intra_staging[:50],
        **index_stats,
        "staging_files_checked": checked,
        "staging_bytes_hashed": staging_bytes_hashed,
        "duplicate_files_deleted": duplicates_deleted,
        "unique_files_kept": len([p for p in kept if p.exists()]),
        "preflight_total_seconds": total_seconds,
        "preflight_staging_loop_seconds": round(loop_seconds, 3),
        "preflight_files_per_second": round(checked / loop_seconds, 2) if loop_seconds else 0.0,
        "preflight_mb_per_second": round(mb_hashed / loop_seconds, 2) if loop_seconds else 0.0,
    }
    _preflight_audit_log(run_dir, summary)
    if log_fn:
        log_fn(
            "Preflight dedupe: "
            f"checked {summary['checked']} / {total}, "
            f"deleted {summary['db_owned_hash_duplicates_deleted'] + summary['intra_staging_duplicates_deleted']} duplicate(s), "
            f"unique remaining {summary['unique_candidates_remaining']}"
        )
    return {**summary, "unique_candidates": [p for p in kept if p.exists()]}


def validate_live_gates(args) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    ctx: dict[str, Any] = {}
    config.METACHECK_MODE = "advanced"
    config.METACHECK_ENABLED = True
    checks.append(_result("metacheck_mode", True, "advanced_required"))

    try:
        mode = normalize_eval_mode(getattr(args, "mode", None) or "A")
    except ValueError as e:
        checks.append(_result("mode A (Sonnet-only)", False, str(e)))
        return False, checks, ctx
    checks.append(_result("mode A (Sonnet-only)", mode == "A", f"mode={mode!r}"))

    for gate, attr in (
        ("--execute", "execute"),
        ("--allow-db-write", "allow_db_write"),
        ("--allow-pdf-copy", "allow_pdf_copy"),
    ):
        ok = bool(getattr(args, attr, False))
        checks.append(_result(gate, ok, "required" if ok else "missing"))
        if not ok:
            return False, checks, ctx

    allow_paid_api = bool(getattr(args, "allow_paid_api", False))
    allow_local_ollama = bool(getattr(args, "allow_local_ollama", False))
    checks.append(
        _result(
            "--allow-paid-api OR --allow-local-ollama",
            allow_paid_api or allow_local_ollama,
            "required",
        )
    )
    if not (allow_paid_api or allow_local_ollama):
        return False, checks, ctx

    if allow_paid_api:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        from pipeline_llm import paid_api_ready

        ok_paid, paid_msg = paid_api_ready()
        checks.append(_result("paid_eval_api", ok_paid, paid_msg))
        checks.append(
            _result(
                "ANTHROPIC_API_KEY",
                bool(api_key),
                "optional: staging PDF vision (Haiku -> Sonnet -> Opus)",
            )
        )
        if not api_key:
            ctx["sonnet_unavailable"] = True
            return False, checks, ctx
    else:
        ollama_ok = False
        detail = config.OLLAMA_URL
        try:
            with urllib.request.urlopen(config.OLLAMA_URL.rstrip("/") + "/api/tags", timeout=3) as resp:
                ollama_ok = (resp.status == 200)
                detail = f"{config.OLLAMA_URL} status={resp.status}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            detail = f"{config.OLLAMA_URL} unreachable: {type(exc).__name__}"
        checks.append(_result("local_ollama_mode", ollama_ok, detail))
        if not ollama_ok:
            return False, checks, ctx

    qc_ok, qc_msg = _quick_check_ok()
    checks.append(_result("PRAGMA quick_check", qc_ok, qc_msg))
    if not qc_ok:
        return False, checks, ctx

    try:
        from metacheck_client import grobid_available, metacheck_available

        grobid_ok = grobid_available()
        metacheck_ok = metacheck_available()
    except Exception as exc:
        checks.append(_result("MetaCheck preflight", False, f"preflight_error:{type(exc).__name__}"))
        return False, checks, ctx
    checks.append(_result("GROBID service", grobid_ok, config.GROBID_API_URL))
    checks.append(_result("MetaCheck service", metacheck_ok, config.METACHECK_API_URL))
    if not (grobid_ok and metacheck_ok):
        ctx["metacheck_unavailable"] = True
        return False, checks, ctx

    staging_pdfs = list_root_staging_pdfs()
    ctx["staging_pdfs"] = staging_pdfs
    checks.append(_result("staging candidates", True, f"count={len(staging_pdfs)}"))
    return True, checks, ctx


def validate_plan_gates(args) -> tuple[bool, list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    try:
        mode = normalize_eval_mode(getattr(args, "mode", None) or "A")
        checks.append(_result("mode A (Sonnet-only)", mode == "A", f"mode={mode!r}"))
    except ValueError as e:
        checks.append(_result("mode A (Sonnet-only)", False, str(e)))
        return False, checks
    if getattr(args, "execute", False):
        checks.append(_result("plan-only (no --execute)", False, "--execute must not be set"))
        return False, checks
    checks.append(_result("plan-only (no --execute)", True, "read-only"))
    checks.append(_result("staging candidates", True, f"count={len(list_root_staging_pdfs())}"))
    return True, checks


def process_one_staging_pdf(
    staging_path: Path,
    client: Any,
    *,
    prescan_by_name: dict[str, dict[str, Any]],
    on_stage: Callable[[str], None] | None = None,
    tags: str = INGEST_TAG,
    supplement_ctx: ChildDocumentRunContext | None = None,
    log_fn=None,
    allow_paid_api: bool = False,
    allow_opus_resolution: bool = False,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    def stage(name: str) -> None:
        if on_stage:
            on_stage(name)

    name = staging_path.name
    outcome: dict[str, Any] = {
        "filename": name,
        "status": "unknown",
        "paper_id": None,
        "reason": "",
        "review_path": None,
        "evaluator": config.EVAL_MODEL,
        "stages_completed": [],
    }

    stage("start")
    outcome["stages_completed"].append("start")

    if not staging_path.exists():
        outcome.update(status="skipped_missing", reason="staging file not found", stage="start")
        return outcome

    stage("prescan_check")
    outcome["stages_completed"].append("prescan_check")

    db_before = papers_db.get_paper_count()
    process_args = {
        "extract_metadata": True,
        "no_claude": True,
        "no_ollama": (not getattr(config, "USE_LOCAL_OLLAMA_ONLY", False)),
        "tags": tags,
    }
    from review_retry_tracking import ingest_overrides_from_sidecar

    process_args.update(ingest_overrides_from_sidecar(staging_path))

    stage("ingest")
    outcome["stages_completed"].append("ingest")
    llm_retry_used = False
    force_research_eval = False
    llm_forced = False
    try:
        ingested = process_paper(str(staging_path), process_args)
    except Exception as e:
        if not staging_path.exists():
            outcome.update(
                status="confirmed_duplicate_deleted",
                reason=f"staging removed during ingest ({type(e).__name__}: {e})",
                traceback=traceback.format_exc(),
                stage="ingest",
            )
            return outcome
        recovery = _reject_staging_file(
            staging_path,
            str(e),
            allow_paid_api=allow_paid_api,
            allow_opus=allow_opus_resolution and allow_paid_api,
            supplement_ctx=supplement_ctx,
        )
        outcome["recovery"] = recovery
        outcome.update(_terminal_recovery_outcome(recovery, default_reason=str(e), stage="ingest"))
        outcome["traceback"] = traceback.format_exc()
        return outcome

    if ingested:
        pass  # Successful ingest (including LLM-forced retry); skip failure/recovery ladder.
    elif not ingested:
        from document_text_extraction import extraction_failure_terminal_action

        # A coverage/acceptance block is a trigger to adjudicate now — not a
        # failure to retry blindly. Route the same file through one terminal
        # adjudication (deterministic recovery + optional models) immediately.
        stage("llm_adjudication")
        block, adj = _adjudicate_acceptance_block_if_needed(
            staging_path,
            client=client,
            allow_paid_api=allow_paid_api,
            allow_opus=allow_opus_resolution and allow_paid_api,
            supplement_ctx=supplement_ctx,
        )
        if block and adj:
            outcome["acceptance_block"] = block
            outcome["adjudication"] = adj
            llm_usage = {}
            if isinstance(adj.get("llm_adjudication"), dict):
                son = (adj["llm_adjudication"].get("sonnet_usage") or {})
                opu = (adj["llm_adjudication"].get("opus_usage") or {})
                llm_usage = {
                    "sonnet_input_tokens": int(son.get("input_tokens") or 0),
                    "sonnet_output_tokens": int(son.get("output_tokens") or 0),
                    "opus_input_tokens": int(opu.get("input_tokens") or 0),
                    "opus_output_tokens": int(opu.get("output_tokens") or 0),
                }
            if llm_usage:
                outcome["llm_adjudication_usage"] = llm_usage
            local_recovery_decision = str(
                adj.get("decision")
                or adj.get("action")
                or adj.get("ingest_kind")
                or ""
            ).upper()
            if adj.get("llm_force_retry_ingest") and not llm_retry_used and staging_path.exists():
                llm_retry_used = True
                process_args["llm_force_acceptance"] = bool(adj.get("llm_force_acceptance"))
                llm_forced = llm_forced or bool(adj.get("llm_force_acceptance"))
                if adj.get("llm_force_full_ocr"):
                    process_args["llm_force_full_ocr"] = True
                if adj.get("llm_force_non_ratable"):
                    process_args["llm_force_non_ratable"] = True
                    process_args["non_ratable_preclassification"] = True
                outcome["llm_force_retry"] = {
                    "decision": adj.get("decision"),
                    "reason": adj.get("reason"),
                }
                force_research_eval = str(adj.get("decision") or "") == "INGEST_RESEARCH_PAPER"
                ingested = process_paper(str(staging_path), process_args)
            elif (
                adj.get("recovered_to_staging")
                and not allow_paid_api
                and not llm_retry_used
                and staging_path.exists()
                and local_recovery_decision in {"INGEST_RESEARCH_PAPER", "ACCEPT_RESEARCH"}
            ):
                llm_retry_used = True
                process_args["llm_force_acceptance"] = True
                process_args["llm_force_full_ocr"] = True
                outcome["local_recovery_retry"] = {
                    "decision": local_recovery_decision,
                    "reason": adj.get("reason"),
                }
                force_research_eval = True
                ingested = _drive_ingest_with_escalation(staging_path, process_args)
            else:
                outcome.update(
                    _terminal_recovery_outcome(
                        adj,
                        default_reason=f"coverage_block:{block.get('coverage_status') or block.get('reason')}",
                        stage="ingest",
                    )
                )
                return outcome

        extraction = _ingest_extraction_snapshot()
        if extraction:
            outcome["extraction"] = extraction
            extractor_status = str(extraction.get("extractor_status") or "")
            if extractor_status in {
                "EXTRACTOR_UNAVAILABLE",
                "EXTRACTOR_CRASHED",
                "WRONG_PATH_OR_MISSING_FILE",
            }:
                outcome.update(
                    status="system_integrity_failure",
                    reason=f"ingest_extraction:{extractor_status}",
                    stage="ingest",
                )
                return outcome
        # Ingest may succeed on LLM-forced retry while still inside this block; do not
        # run recovery (hash would match the row we just inserted) or treat missing
        # staging as a duplicate delete.
        if ingested:
            pass
        elif not staging_path.exists():
            if outcome.get("status") in {
                "supplement_linked",
                "supplement_pending_parent",
                "child_document_pending_parent",
                "addendum_linked",
                "correction_linked",
                "erratum_linked",
                "retraction_linked",
                "expression_of_concern_linked",
                "appendix_linked",
            } or str(outcome.get("status", "")).endswith("_linked"):
                return outcome
            outcome.update(
                status="confirmed_duplicate_deleted",
                reason="removed from staging (confirmed duplicate or supplement routing)",
                stage="ingest",
            )
            return outcome
        elif not ingested:
            try:
                recovery = _attempt_recovery_before_review(
                    staging_path,
                    "process_paper returned False",
                    allow_paid_api=allow_paid_api,
                    allow_opus=allow_opus_resolution and allow_paid_api,
                    supplement_ctx=supplement_ctx,
                )
                outcome["recovery"] = recovery
                if recovery.get("recovered_to_staging"):
                    staging_path = Path(recovery["recovered_to_staging"])
                    process_args.update(ingest_overrides_from_sidecar(staging_path))
                    ingested = process_paper(str(staging_path), process_args)
                elif recovery.get("terminal"):
                    outcome.update(_terminal_recovery_outcome(recovery, default_reason="ingest_failed", stage="ingest"))
                    return outcome
                elif recovery.get("deleted_duplicate"):
                    outcome.update(status="confirmed_duplicate_deleted", reason="recovery_ladder_duplicate", stage="ingest")
                    return outcome
                elif recovery.get("deleted_junk"):
                    outcome.update(status="deleted_junk", reason="recovery_ladder_junk", stage="ingest")
                    return outcome
                elif recovery.get("child_pending"):
                    outcome.update(status="child_document_pending_parent", reason="recovery_ladder_child", stage="ingest")
                    return outcome
            except OSError as e:
                outcome.update(status="failed", reason=f"ingest failed; recovery/review move failed: {e}", stage="ingest")
                return outcome

        if not ingested and llm_retry_used:
            if _drive_ingest_with_escalation(staging_path, process_args):
                ingested = True
            else:
                reject = _terminalize_failed_ingest_candidate(
                    staging_path,
                    "ingest_failed_after_llm_retry",
                    allow_paid_api=allow_paid_api,
                    allow_opus=allow_opus_resolution and allow_paid_api,
                    supplement_ctx=supplement_ctx,
                )
                outcome["recovery_final"] = reject
                outcome.update(
                    _terminal_recovery_outcome(
                        reject,
                        default_reason="ingest_failed_after_llm_retry",
                        stage="ingest",
                    )
                )
                return outcome

        if not ingested:
            block, adj = _adjudicate_acceptance_block_if_needed(
                staging_path,
                client=client,
                allow_paid_api=allow_paid_api,
                allow_opus=allow_opus_resolution and allow_paid_api,
                supplement_ctx=supplement_ctx,
            )
            if block and adj:
                outcome["acceptance_block"] = block
                outcome["adjudication"] = adj
                llm_usage = {}
                if isinstance(adj.get("llm_adjudication"), dict):
                    son = (adj["llm_adjudication"].get("sonnet_usage") or {})
                    opu = (adj["llm_adjudication"].get("opus_usage") or {})
                    llm_usage = {
                        "sonnet_input_tokens": int(son.get("input_tokens") or 0),
                        "sonnet_output_tokens": int(son.get("output_tokens") or 0),
                        "opus_input_tokens": int(opu.get("input_tokens") or 0),
                        "opus_output_tokens": int(opu.get("output_tokens") or 0),
                    }
                if llm_usage:
                    outcome["llm_adjudication_usage"] = llm_usage
                if adj.get("llm_force_retry_ingest") and not llm_retry_used and staging_path.exists():
                    llm_retry_used = True
                    process_args["llm_force_acceptance"] = bool(adj.get("llm_force_acceptance"))
                    llm_forced = llm_forced or bool(adj.get("llm_force_acceptance"))
                    if adj.get("llm_force_full_ocr"):
                        process_args["llm_force_full_ocr"] = True
                    if adj.get("llm_force_non_ratable"):
                        process_args["llm_force_non_ratable"] = True
                        process_args["non_ratable_preclassification"] = True
                    outcome["llm_force_retry"] = {
                        "decision": adj.get("decision"),
                        "reason": adj.get("reason"),
                    }
                    force_research_eval = str(adj.get("decision") or "") == "INGEST_RESEARCH_PAPER"
                    process_args, llm_forced, force_research_eval = _apply_adjudication_to_process_args(
                        process_args, adj
                    )
                    ingested = _drive_ingest_with_escalation(staging_path, process_args)
                    if not ingested:
                        reject = _terminalize_failed_ingest_candidate(
                            staging_path,
                            f"coverage_block_retry:{block.get('coverage_status') or block.get('reason')}",
                            allow_paid_api=allow_paid_api,
                            allow_opus=allow_opus_resolution and allow_paid_api,
                            supplement_ctx=supplement_ctx,
                            first_decision={"acceptance_block": block, "adjudication": adj},
                        )
                        outcome["recovery_final"] = reject
                        outcome.update(
                            _terminal_recovery_outcome(
                                reject,
                                default_reason=f"coverage_block_retry:{block.get('coverage_status') or block.get('reason')}",
                                stage="ingest",
                            )
                        )
                        return outcome
                else:
                    reject = _terminalize_failed_ingest_candidate(
                        staging_path,
                        f"coverage_block:{block.get('coverage_status') or block.get('reason')}",
                        allow_paid_api=allow_paid_api,
                        allow_opus=allow_opus_resolution and allow_paid_api,
                        supplement_ctx=supplement_ctx,
                        first_decision={"acceptance_block": block, "adjudication": adj},
                    )
                    outcome["recovery_final"] = reject
                    outcome.update(
                        _terminal_recovery_outcome(
                            reject,
                            default_reason=f"coverage_block:{block.get('coverage_status') or block.get('reason')}",
                            stage="ingest",
                        )
                    )
                    return outcome
            recovery = _attempt_recovery_before_review(
                staging_path,
                "process_paper returned False after recovery retry",
                allow_paid_api=allow_paid_api,
                allow_opus=allow_opus_resolution and allow_paid_api,
                supplement_ctx=supplement_ctx,
                terminal_on_ingest=True,
            )
            outcome["recovery_final"] = recovery
            if recovery.get("terminal"):
                outcome.update(_terminal_recovery_outcome(recovery, default_reason="ingest_failed_disposition_stop", stage="ingest"))
            elif recovery.get("deleted_duplicate"):
                outcome.update(status="confirmed_duplicate_deleted", reason="ingest_failed_duplicate", stage="ingest")
            elif recovery.get("deleted_junk"):
                outcome.update(status="deleted_junk", reason="ingest_failed_junk", stage="ingest")
            elif recovery.get("deleted_corrupt"):
                outcome.update(status="deleted_corrupt", reason="ingest_failed_corrupt", stage="ingest")
            elif recovery.get("deleted_unrecoverable"):
                outcome.update(status="deleted_unrecoverable", reason="ingest_failed_unrecoverable", stage="ingest")
            elif recovery.get("recovered_to_staging"):
                recovery_decision = str(
                    recovery.get("decision")
                    or recovery.get("action")
                    or recovery.get("ingest_kind")
                    or ""
                ).upper()
                if (
                    not allow_paid_api
                    and staging_path.exists()
                    and recovery_decision in {"INGEST_RESEARCH_PAPER", "ACCEPT_RESEARCH"}
                ):
                    local_args = dict(process_args)
                    local_args["llm_force_acceptance"] = True
                    local_args["llm_force_full_ocr"] = True
                    outcome["local_recovery_retry"] = {
                        "decision": recovery_decision,
                        "reason": recovery.get("reason"),
                    }
                    ingested = _drive_ingest_with_escalation(staging_path, local_args)
                    if ingested:
                        pass
                    else:
                        rec = intake_rejected.archive_staging_file(
                            staging_path,
                            status="moved_to_rejected",
                            reason="local_recovery_retry_failed",
                            user_reason=(
                                "Local model recovery confirmed this looked like a research paper, "
                                "but full OCR/forced ingest still could not add it."
                            ),
                        )
                        outcome.update(
                            status="moved_to_rejected",
                            reason="local_recovery_retry_failed",
                            stage="ingest",
                            **(rec or {}),
                        )
                elif allow_paid_api and staging_path.exists():
                    llm_client = client or anthropic.Anthropic()
                    llm = _llm_final_adjudication_for_block(
                        staging_path,
                        {
                            "action": "INGEST_RETRY_UNRESOLVED",
                            "reason": str(recovery.get("reason") or "ingest_failed_returned_to_staging"),
                            "coverage_status": "",
                            "required_next_action": "llm_final_arbitration",
                            "recovery_decision": recovery.get("decision"),
                            "classification": recovery.get("classification"),
                            "document_type": recovery.get("document_type"),
                        },
                        client=llm_client,
                        allow_opus=allow_opus_resolution and allow_paid_api,
                    )
                    llm_terminal = _apply_llm_adjudication_decision(
                        staging_path,
                        llm,
                        reason_prefix="llm_final_arbitration_after_recovery",
                        evidence={"recovery_final": recovery},
                    )
                    outcome["llm_final_arbitration"] = llm_terminal
                    llm_usage = {}
                    if isinstance(llm_terminal.get("llm_adjudication"), dict):
                        son = (llm_terminal["llm_adjudication"].get("sonnet_usage") or {})
                        opu = (llm_terminal["llm_adjudication"].get("opus_usage") or {})
                        llm_usage = {
                            "sonnet_input_tokens": int(son.get("input_tokens") or 0),
                            "sonnet_output_tokens": int(son.get("output_tokens") or 0),
                            "opus_input_tokens": int(opu.get("input_tokens") or 0),
                            "opus_output_tokens": int(opu.get("output_tokens") or 0),
                        }
                    if llm_usage:
                        outcome["llm_adjudication_usage"] = llm_usage
                    if llm_terminal.get("llm_force_retry_ingest"):
                        process_args["llm_force_acceptance"] = bool(llm_terminal.get("llm_force_acceptance"))
                        llm_forced = llm_forced or bool(llm_terminal.get("llm_force_acceptance"))
                        if llm_terminal.get("llm_force_full_ocr"):
                            process_args["llm_force_full_ocr"] = True
                        if llm_terminal.get("llm_force_non_ratable"):
                            process_args["llm_force_non_ratable"] = True
                            process_args["non_ratable_preclassification"] = True
                        force_research_eval = str(llm_terminal.get("decision") or "") == "INGEST_RESEARCH_PAPER"
                        ingested = process_paper(str(staging_path), process_args)
                        if ingested:
                            pass
                        else:
                            outcome.update(
                                _terminal_recovery_outcome(
                                    llm_terminal,
                                    default_reason="llm_forced_ingest_failed",
                                    stage="ingest",
                                )
                            )
                            return outcome
                    else:
                        outcome.update(
                            _terminal_recovery_outcome(
                                llm_terminal,
                                default_reason="llm_final_arbitration_after_recovery",
                                stage="ingest",
                            )
                        )
                        return outcome
                else:
                    rec = intake_rejected.archive_staging_file(
                        staging_path,
                        status="moved_to_rejected",
                        reason="ingest_failed_no_api_recovery",
                        user_reason=(
                            "Intake could not finish automatic recovery. "
                            "The file was moved to papers-rejected/ — not left in staging."
                        ),
                    )
                    outcome.update(
                        status="moved_to_rejected",
                        reason="ingest_failed_no_api_recovery",
                        stage="ingest",
                        **(rec or {}),
                    )
            elif recovery.get("child_pending"):
                outcome.update(status="child_document_pending_parent", reason="ingest_failed_child", stage="ingest")
            elif recovery.get("system_integrity_failure"):
                outcome.update(status="system_integrity_failure", reason="ingest_failed_integrity", stage="ingest")
            else:
                reject = _reject_staging_file(
                    staging_path,
                    "ingest_failed_unresolved",
                    allow_paid_api=allow_paid_api,
                    allow_opus=allow_opus_resolution and allow_paid_api,
                    supplement_ctx=supplement_ctx,
                )
                outcome["recovery_final"] = reject
                outcome.update(_terminal_recovery_outcome(reject, default_reason="ingest_failed_unresolved", stage="ingest"))
            return outcome

    db_after = papers_db.get_paper_count()
    if db_after != db_before + 1:
        if staging_path.exists():
            reject = _reject_staging_file(
                staging_path,
                f"unexpected DB row count before={db_before} after={db_after}",
                allow_paid_api=allow_paid_api,
                allow_opus=allow_opus_resolution and allow_paid_api,
                supplement_ctx=supplement_ctx,
            )
            outcome["recovery"] = reject
        outcome.update(
            status="system_integrity_failure",
            reason=f"unexpected DB row count before={db_before} after={db_after}",
            stage="ingest",
        )
        return outcome

    paper_id = _paper_id_from_latest_insert(name)
    if not paper_id:
        if staging_path.exists():
            reject = _reject_staging_file(
                staging_path,
                "ingested row not found after insert",
                allow_paid_api=allow_paid_api,
                allow_opus=allow_opus_resolution and allow_paid_api,
                supplement_ctx=supplement_ctx,
            )
            outcome["recovery"] = reject
        outcome.update(status="system_integrity_failure", reason="ingested row not found after insert", stage="ingest")
        return outcome
    outcome["paper_id"] = paper_id

    from document_corpus_classification import (
        apply_non_ratable_reference_classification,
        classify_corpus_document,
        detect_limited_publication_item,
        is_non_ratable_document_type,
    )
    from review_retry_tracking import ingest_overrides_from_sidecar, sidecar_indicates_non_ratable

    sidecar_flags = ingest_overrides_from_sidecar(staging_path)
    non_ratable = bool(sidecar_flags.get("non_ratable_preclassification")) or sidecar_indicates_non_ratable(staging_path)
    if not non_ratable:
        ingested_paper = papers_db.get_paper(paper_id) or {}
        non_ratable = is_non_ratable_document_type(ingested_paper.get("document_type"))
    if not non_ratable:
        bm = (papers_db.get_paper(paper_id) or {}).get("basic_metadata") or {}
        hint = classify_corpus_document(
            staging_path.name,
            title=str(bm.get("title") or ""),
        )
        non_ratable = bool(hint.get("is_non_ratable"))
        if non_ratable:
            sidecar_flags.setdefault("document_type", hint.get("document_type"))
            sidecar_flags.setdefault("classification_reasons", hint.get("classification_reasons"))

    if non_ratable:
        from document_evidence import check_article_type_guard, score_journal_article_signals

        ingested_paper = papers_db.get_paper(paper_id) or {}
        bm = ingested_paper.get("basic_metadata") or {}
        ft = ingested_paper.get("full_text") or {}
        fi = ingested_paper.get("file_info") or {}
        ids = ingested_paper.get("identifiers") or {}
        article_signals = score_journal_article_signals(
            text=str(ft.get("full") or ft.get("text") or ""),
            filename=str(fi.get("original_filename") or staging_path.name),
            identity={
                "title": bm.get("title"),
                "doi": ids.get("doi"),
                "pii": ids.get("pii"),
                "identity_status": bm.get("identity_status"),
            },
            page_count=None,
        )
        guard = check_article_type_guard(
            proposed_rating="not_ratable_reference_material",
            document_type=ingested_paper.get("document_type") or sidecar_flags.get("document_type"),
            article_signals=article_signals,
            identity={"title": bm.get("title"), "identity_status": bm.get("identity_status")},
        )
        if not guard.get("allowed"):
            non_ratable = False
            outcome["article_type_guard"] = guard
            outcome["evaluation_path"] = "guard_blocked_non_ratable"

    from evidence_coverage import assess_evidence_coverage_from_paper, format_coverage_diagnostic, resolve_raw_full_text

    ingested_for_coverage = papers_db.get_paper(paper_id) or {}
    coverage = assess_evidence_coverage_from_paper(ingested_for_coverage)
    outcome["coverage_status"] = coverage.get("coverage_status")
    outcome["coverage_gate"] = {
        "raw_text_length": coverage.get("raw_text_length"),
        "section_count": coverage.get("section_count"),
        "likely_journal_article": coverage.get("likely_journal_article"),
        "evidence_can_support_rating": coverage.get("evidence_can_support_rating"),
        "evidence_can_support_not_applicable": coverage.get("evidence_can_support_not_applicable"),
    }

    if not non_ratable and not coverage.get("evidence_can_support_rating"):
        bm = ingested_for_coverage.get("basic_metadata") or {}
        ft = ingested_for_coverage.get("full_text") or {}
        limited = detect_limited_publication_item(
            filename=staging_path.name,
            title=str(bm.get("title") or ""),
            text=resolve_raw_full_text(ft),
            page_count=coverage.get("page_count"),
            coverage=coverage,
        )
        if limited.get("is_limited_publication_item"):
            non_ratable = True
            sidecar_flags["document_type"] = limited.get("document_type") or "conference_abstract"
            sidecar_flags["classification_reasons"] = limited.get("classification_reasons") or []
            outcome["evaluation_path"] = "deterministic_limited_publication_non_ratable"
            outcome["limited_publication_detection"] = limited

    if non_ratable and coverage.get("likely_journal_article") and not coverage.get("evidence_can_support_not_applicable"):
        if outcome.get("evaluation_path") == "deterministic_limited_publication_non_ratable":
            pass
        else:
            non_ratable = False
            outcome["evaluation_path"] = "coverage_blocked_non_ratable"
            outcome["coverage_diagnostic"] = format_coverage_diagnostic(coverage)

    llm_forced = llm_forced or bool(process_args.get("llm_force_acceptance"))
    if not non_ratable and coverage.get("likely_journal_article"):
        force_research_eval = True

    stage("first_pass_finalize")
    outcome["stages_completed"].append("first_pass_finalize")
    print(f"  -> First-pass finalize {paper_id} (identity + text + eval + red team)...", flush=True)

    from first_pass_finalize import run_until_evaluation_complete

    try:
        fin = run_until_evaluation_complete(
            paper_id,
            staging_path,
            client,
            non_ratable=non_ratable,
            force_research_eval=force_research_eval,
            llm_forced=llm_forced,
            allow_paid_api=allow_paid_api,
            sidecar_flags=sidecar_flags,
            max_attempts=6,
            stop_requested=stop_requested,
            max_seconds=240,
        )
    except Exception as e:
        rb = _rollback_paper(paper_id)
        outcome["rollback"] = rb
        outcome.update(status="failed", reason=str(e), stage="first_pass_finalize", traceback=traceback.format_exc())
        return outcome

    outcome["first_pass_finalize"] = fin
    usage = fin.get("eval_usage") or {}
    if usage.get("input_tokens") or usage.get("output_tokens"):
        outcome["eval_usage"] = {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
        }

    stage("completion_check")
    outcome["stages_completed"].append("completion_check")
    paper = papers_db.get_paper(paper_id)
    complete = bool(fin.get("ok"))
    reason = str(fin.get("reason") or "")
    cl = (paper or {}).get("classification") or {}
    outcome["classification_rating"] = cl.get("rating")
    outcome["evaluation_complete"] = complete
    outcome["evaluation_complete_reason"] = reason
    rating_label = cl.get("rating") or "(none)"
    if complete:
        rt = "yes" if str(cl.get("red_team_notes") or "").strip() else "no"
        print(
            f"  -> First-pass complete: rating={rating_label} red_team={rt}",
            flush=True,
        )
    else:
        print(f"  -> First-pass failed (no next file): {reason}", flush=True)

    if not complete:
        if reason in {"STOP_REQUESTED", "first_pass_finalize_timeout"}:
            outcome.update(status="batch_stop", reason=reason, stage="first_pass_finalize")
            return outcome
        rb = _rollback_paper(paper_id)
        outcome["rollback"] = rb
        if staging_path.exists():
            recovery = _attempt_recovery_before_review(
                staging_path,
                reason,
                allow_paid_api=allow_paid_api,
                allow_opus=allow_opus_resolution and allow_paid_api,
                supplement_ctx=supplement_ctx,
                terminal_on_ingest=True,
            )
            outcome["recovery"] = recovery
            if recovery.get("terminal"):
                outcome.update(_terminal_recovery_outcome(recovery, default_reason=reason, stage="completion_check"))
                return outcome
        if staging_path.exists():
            reject = _reject_staging_file(
                staging_path,
                reason,
                allow_paid_api=allow_paid_api,
                allow_opus=allow_opus_resolution and allow_paid_api,
                supplement_ctx=supplement_ctx,
            )
            outcome["recovery"] = reject
            outcome.update(_terminal_recovery_outcome(reject, default_reason=reason, stage="completion_check"))
        else:
            outcome.update(status="deleted_unrecoverable", reason=reason, stage="completion_check")
        return outcome

    from eval_completion import is_research_rating
    from evaluate_paper import normalize_rating_label

    normalized_rating = normalize_rating_label(cl.get("rating"))
    if not is_research_rating(normalized_rating):
        outcome["metacheck"] = {
            "status": "not_applicable",
            "ok": True,
            "source": "not_applicable",
            "reason": f"rating_not_research:{normalized_rating or 'none'}",
        }
        print("  -> MetaCheck skipped: not a ratable research paper", flush=True)
    else:
        stage("metacheck")
        outcome["stages_completed"].append("metacheck")
        try:
            from metacheck_integration import run_metacheck_for_paper_id

            mc = run_metacheck_for_paper_id(
                paper_id,
                status_callback=lambda status: stage(f"metacheck_{status}"),
            )
            mc_summary = mc.get("summary") if isinstance(mc.get("summary"), dict) else {}
            outcome["metacheck"] = {
                "status": mc.get("status"),
                "ok": bool(mc.get("ok")),
                "source": mc_summary.get("source"),
                "mode": "advanced",
                "advanced_status": mc_summary.get("advanced_status"),
                "reason": (mc_summary.get("reason") if mc_summary else mc.get("reason")),
            }
            if mc.get("status") == "ok" and mc_summary.get("source") == "advanced_metacheck":
                print("  -> MetaCheck evidence stored (advanced GROBID + MetaCheck)", flush=True)
            elif mc.get("status") == "technical_unavailable":
                print("  -> MetaCheck unavailable for this PDF; corpus rating retained with DB note", flush=True)
            elif mc.get("status") == "not_applicable":
                print("  -> MetaCheck skipped: disabled", flush=True)
            else:
                raise RuntimeError(outcome["metacheck"].get("reason") or "metacheck_failed")
        except Exception as e:
            restored_to_staging = None
            paper_before_rollback = papers_db.get_paper(paper_id) or {}
            fi = paper_before_rollback.get("file_info") or {}
            rel = fi.get("filepath") or fi.get("managed_pdf_relative_path") or ""
            source_pdf = Path(rel) if rel and Path(rel).is_absolute() else (config.MASTER_PROJECT / rel if rel else None)
            if source_pdf and source_pdf.exists() and not staging_path.exists():
                try:
                    import shutil

                    config.PAPERS_STAGING.mkdir(parents=True, exist_ok=True)
                    restore_path = config.PAPERS_STAGING / staging_path.name
                    if not restore_path.exists():
                        shutil.copy2(source_pdf, restore_path)
                    restored_to_staging = str(restore_path)
                except OSError as restore_error:
                    outcome["metacheck_restore_error"] = str(restore_error)
            rb = _rollback_paper(paper_id)
            outcome["rollback"] = rb
            outcome["metacheck"] = {
                "status": "failed",
                "ok": False,
                "mode": "advanced",
                "reason": str(e),
            }
            if restored_to_staging:
                outcome["recovered_to_staging"] = restored_to_staging
                outcome.update(status="ingest_retry_required", reason=f"metacheck_retry_required:{e}", stage="metacheck")
                print(f"  -> MetaCheck failed; paper rolled back and returned to staging: {e}", flush=True)
            else:
                outcome.update(status="failed", reason=f"metacheck_failed:{e}", stage="metacheck")
                print(f"  -> MetaCheck failed; paper rolled back: {e}", flush=True)
            return outcome

    stage("staging_cleanup")
    outcome["stages_completed"].append("staging_cleanup")
    if staging_path.exists():
        try:
            staging_path.unlink()
        except OSError as e:
            outcome.update(status="failed", reason=f"ingest complete but staging removal failed: {e}", stage="staging_cleanup")
            return outcome

    outcome.update(status="success", reason="ingest_and_evaluation_complete", stage="done")
    if supplement_ctx and paper_id:
        supplement_ctx.register_successful_parent(paper_id)
    return outcome


def process_one_staging_file(
    staging_path: Path,
    client: Any,
    *,
    prescan_by_name: dict[str, dict[str, Any]],
    on_stage: Callable[[str], None] | None = None,
    tags: str = INGEST_TAG,
    supplement_ctx: ChildDocumentRunContext | None = None,
    log_fn=None,
    allow_paid_api: bool = False,
    allow_opus_resolution: bool = False,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Route one staging candidate through disposition before any ingest work."""
    staging_path = Path(staging_path)

    if on_stage:
        on_stage("disposition")
    result = disposition_transaction(
        staging_path,
        {
            "source_context": "staging",
            "allow_paid_api": allow_paid_api,
            "allow_opus_resolution": allow_opus_resolution and allow_paid_api,
            "allow_accept_to_db": True,
            "execute": True,
            "child_ctx": supplement_ctx,
        },
    )
    action = str(result.get("action") or "")
    outcome: dict[str, Any] = {
        "filename": staging_path.name,
        "status": "disposition",
        "disposition_action": action,
        "reason": result.get("reason"),
        "stage": "disposition",
    }
    if result.get("needs_ingest"):
        return process_one_staging_pdf(
            Path(result.get("path") or staging_path),
            client,
            prescan_by_name=prescan_by_name,
            on_stage=on_stage,
            tags=tags,
            supplement_ctx=supplement_ctx,
            log_fn=log_fn,
            allow_paid_api=allow_paid_api,
            allow_opus_resolution=allow_opus_resolution,
            stop_requested=stop_requested,
        )
    if action == "DELETE_CONFIRMED_DUPLICATE":
        outcome["status"] = "confirmed_duplicate_deleted"
    elif action == "DELETE_TRUE_CORRUPT_UNUSABLE":
        outcome["status"] = "deleted_corrupt"
    elif action == "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION":
        outcome["status"] = "deleted_unrecoverable"
    elif action in {"DELETE_CONFIRMED_JUNK"}:
        outcome["status"] = "deleted_junk"
    elif action == "HOLD_PENDING_PARENT":
        outcome["status"] = "child_document_pending_parent"
    elif action == "LINK_CHILD_TO_PARENT":
        outcome["status"] = "child_linked"
    elif action == "SYSTEM_INTEGRITY_FAILURE":
        outcome.update(status="system_integrity_failure", reason=result.get("reason"))
    return outcome


def _attach_rejected_paths_to_items(items: list[Any]) -> None:
    by_name: dict[str, dict[str, Any]] = {}
    for rec in intake_rejected.run_rejected_records():
        fn = str(rec.get("filename") or "")
        if fn:
            by_name[fn] = rec
    for item in items:
        if not isinstance(item, dict):
            continue
        fn = str(item.get("filename") or "")
        rec = by_name.get(fn)
        if not rec:
            continue
        for key in ("rejected_path", "rejected_filename", "purge_after", "user_reason"):
            if rec.get(key) is not None:
                item[key] = rec[key]


def _write_batch_artifacts(run_dir: Path, summary: dict[str, Any]) -> dict[str, str]:
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "all_staging_ingest_report.json"
    jsonl_path = run_dir / "all_staging_ingest_items.jsonl"
    live_path = run_dir / "all_staging_ingest_live.json"

    json_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8") as f:
        for item in summary.get("items", []):
            f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    live_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    return {
        "json": str(json_path),
        "jsonl": str(jsonl_path),
        "live": str(live_path),
        "intake_last_run": str(config.INTAKE_LAST_RUN_MD),
    }


def _collect_paths_from_item(obj: Any) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in {"path", "recovered_to_staging", "moved_to_staging", "review_path", "dest", "stop_path"} and isinstance(value, str):
                out.append(Path(value).name)
            else:
                out.extend(_collect_paths_from_item(value))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_collect_paths_from_item(item))
    return out


def _unresolved_staging_reason_counts(summary: dict[str, Any], remaining: list[Path]) -> dict[str, int]:
    by_name: dict[str, dict[str, Any]] = {}
    for item in summary.get("items", []):
        if isinstance(item, dict):
            for name in {str(item.get("filename") or ""), *_collect_paths_from_item(item)}:
                if name:
                    by_name[name] = item
    counts: Counter[str] = Counter()
    for path in remaining:
        item = by_name.get(path.name)
        if not item:
            counts["unresolved_error"] += 1
            continue
        reason = str(item.get("reason") or "")
        status = str(item.get("status") or "")
        recovery = item.get("recovery_final") or item.get("recovery") or {}
        action = str(recovery.get("action") or "")
        if action in {"DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION", "SYSTEM_INTEGRITY_FAILURE"}:
            counts["terminal_rejection"] += 1
        elif status in {"deleted_unrecoverable", "system_integrity_failure"}:
            counts["terminal_rejection"] += 1
        elif status in {"returned_to_staging", "moved_to_rejected", "child_document_pending_parent"}:
            counts["retriable_staging"] += 1
        elif reason in {"ingest_failed_unresolved", "evaluation incomplete or insufficient text"}:
            counts["unresolved_error"] += 1
        else:
            counts["unresolved_error"] += 1
    return dict(counts)


def _write_progress_line(run_dir: Path, record: dict[str, Any]) -> None:
    path = run_dir / "all_staging_ingest_progress.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        f.flush()


def _complete_paper_counts() -> dict[str, int]:
    from eval_completion import is_ingest_evaluation_complete, is_non_ratable_rating, is_research_rating
    from evaluate_paper import normalize_rating_label

    research_complete = non_rat_complete = 0
    for p in papers_db.get_all_metadata():
        cl = p.get("classification") or {}
        r = normalize_rating_label(cl.get("rating"))
        ok, _ = is_ingest_evaluation_complete(p)
        if is_research_rating(r) and ok:
            research_complete += 1
        if is_non_ratable_rating(r) and ok:
            non_rat_complete += 1
    total = research_complete + non_rat_complete
    return {
        "clean_complete_papers": total,
        "research_eval_complete": research_complete,
        "non_ratable_complete": non_rat_complete,
    }


def _safe_source_ownership_audit() -> tuple[dict[str, Any] | None, str | None]:
    try:
        from source_pdf_ownership import build_audit

        return build_audit(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


_TRANSIENT_RUN_STATUSES = {
    "matching_pending_children",
    "finalizing",
    "resolving_review",
    "preflight_dedupe",
    "processing_staging",
    "running",
    "estimating",
    "starting",
    "snapshotting_staging",
}

_SOFT_INTAKE_OUTCOMES = frozenset(
    {
        "success",
        "confirmed_duplicate_deleted",
        "deleted_junk",
        "deleted_corrupt",
        "deleted_unrecoverable",
        "moved_to_rejected",
        "child_document_pending_parent",
        "supplement_pending_parent",
        "skipped_missing",
    }
)

_TERMINAL_STAGING_OUTCOMES = _SOFT_INTAKE_OUTCOMES | frozenset({"failed"})


def _final_items_by_filename(items: list[Any]) -> dict[str, dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        fn = str(item.get("filename") or "")
        if fn:
            by_name[fn] = item
    return by_name


def _recompute_final_outcome_counts(summary: dict[str, Any]) -> None:
    """Use last outcome per filename so drain retries do not false-fail the run."""
    by_name = _final_items_by_filename(summary.get("items") or [])
    hard_fail = 0
    sys_fail = 0
    pending_parent = 0
    for item in by_name.values():
        st = str(item.get("status") or "")
        if st.endswith("_linked") or st in _SOFT_INTAKE_OUTCOMES:
            if st in {"child_document_pending_parent", "supplement_pending_parent"}:
                pending_parent += 1
            continue
        if st == "system_integrity_failure":
            hard_fail += 1
            sys_fail += 1
        elif st in {"failed", "batch_stop"}:
            hard_fail += 1

    summary["processed_unique_candidates"] = len(by_name)
    summary["processed_count"] = len(by_name)
    summary["failed_count"] = hard_fail
    summary["pipeline_failure_count"] = hard_fail
    summary["system_integrity_failure_count"] = sys_fail
    summary["supplement_pending_parent_count"] = pending_parent

    pending_names = intake_rejected.pending_child_display_names()
    summary["pending_child_documents_remaining"] = len(pending_names)
    summary["pending_supplements_remaining"] = len(pending_names)


def _resolve_final_run_status(summary: dict[str, Any]) -> str:
    """Map completed ingest to closed-loop terminal run status."""
    _recompute_final_outcome_counts(summary)
    current = str(summary.get("status") or "")
    if current == "stopped":
        return "stopped_user"
    if current in {"crashed", "failed_preconditions", "lock_busy"}:
        return "failed_integrity"
    if current == "batch_stopped":
        return "failed_integrity"

    ownership = summary.get("source_ownership_status")
    if ownership in {"ambiguous", "unowned", "audit_error"}:
        summary["error"] = summary.get("error") or f"source ownership {ownership} at end"
        return "failed_integrity"

    pipeline_failures = int(summary.get("failed_count") or 0)
    system_failures = int(summary.get("system_integrity_failure_count") or 0)
    unresolved = int(summary.get("unresolved_staging_candidates") or 0)
    unresolved_reasons = summary.get("unresolved_staging_reason_counts") or {}
    review_dust = int(summary.get("review_dust_remaining") or count_review_dust_pdfs())

    if pipeline_failures > 0 or system_failures > 0 or unresolved > 0 or review_dust > 0:
        if unresolved > 0 or review_dust > 0 or system_failures > 0 or pipeline_failures > 0:
            summary["error"] = summary.get("error") or (
                f"staging not empty ({unresolved} left) or failures "
                f"(pipeline={pipeline_failures}, system={system_failures})"
            )
            return "failed_integrity"
        return "failed_integrity"

    # Mid-run checkpoints can set this while other files are still in staging; clear when clean at end.
    err = str(summary.get("error") or "")
    if err.startswith("staging not empty") or "staging not empty" in err:
        summary["error"] = None

    pending_children = int(summary.get("pending_child_documents_remaining") or 0)
    file_rejections = int(summary.get("file_rejections_count") or 0)

    if pending_children > 0:
        return "done_with_pending_children"
    if file_rejections > 0:
        return "done_with_deletions"
    return "done_clean"


def _finalize_summary(
    summary: dict[str, Any],
    counts: dict[str, int],
    *,
    started_mono: float,
    include_audit: bool = True,
) -> None:
    summary["elapsed_seconds"] = round(time.monotonic() - started_mono, 1)
    summary["elapsed_time"] = summary["elapsed_seconds"]
    summary["processed_count"] = len(summary.get("items", []))
    summary["processed_unique_candidates"] = summary["processed_count"]
    summary["unique_candidates_remaining_unprocessed"] = max(
        0,
        int(summary.get("unique_candidates_to_process") or summary.get("starting_staging_count") or 0)
        - int(summary["processed_count"]),
    )
    summary.update(
        success_count=counts["success"],
        research_papers_added=counts["research_success"],
        non_ratable_reference_added=counts["non_ratable_success"],
        confirmed_duplicate_deleted_count=counts["confirmed_duplicate_deleted"],
        returned_to_staging_count=counts["returned_to_staging"],
        moved_to_rejected_count=counts["moved_to_rejected"],
        papers_rejected_count=len(intake_rejected.run_rejected_records()),
        deleted_junk_count=counts["deleted_junk"],
        deleted_corrupt_count=counts["deleted_corrupt"],
        deleted_unrecoverable_count=counts["deleted_unrecoverable"],
        system_integrity_failure_count=counts["system_integrity_failure"],
        technical_failure_count=0,
        skipped_duplicate_count=counts["confirmed_duplicate_deleted"],
        routed_review_count=0,
        failed_count=counts["failed"],
        pipeline_failure_count=counts["failed"],
        file_rejections_count=(
            counts["deleted_junk"]
            + counts["deleted_corrupt"]
            + counts["deleted_unrecoverable"]
        ),
        skipped_missing_count=counts["skipped_missing"],
        metacheck_advanced_count=counts["metacheck_advanced"],
        metacheck_not_applicable_count=counts["metacheck_not_applicable"],
        metacheck_technical_unavailable_count=counts["metacheck_technical_unavailable"],
        metacheck_failed_count=counts["metacheck_failed"],
        final_staging_count=len(list_root_staging_pdfs()),
        review_dust_remaining=count_review_dust_pdfs(),
        final_review_pdf_count=count_review_dust_pdfs(),
        db_rows_after=papers_db.get_paper_count(),
        source_pdfs_after=len(list(config.SOURCE_PDFS.glob("*.pdf"))),
    )
    summary["staging_candidate_count"] = summary["final_staging_count"]
    remaining_staging = list_root_staging_pdfs()
    summary["unresolved_staging_candidates"] = len(remaining_staging)
    summary["unresolved_staging_files"] = [p.name for p in remaining_staging[:50]]
    summary["unresolved_staging_reason_counts"] = _unresolved_staging_reason_counts(summary, remaining_staging)
    current_status = str(summary.get("status") or "")
    is_transient_status = current_status in _TRANSIENT_RUN_STATUSES
    summary["run_completed_with_unresolved_files"] = (
        int(summary.get("unresolved_staging_candidates") or 0) > 0
        and not summary.get("error")
        and not is_transient_status
    )
    summary["review_true_technical_model_failures_count"] = 0
    summary["disposition_counts"] = {
        "success": counts["success"],
        "ingest_research": counts["research_success"],
        "ingest_non_ratable": counts["non_ratable_success"],
        "confirmed_duplicate_deleted": counts["confirmed_duplicate_deleted"],
        "returned_to_staging": counts["returned_to_staging"],
        "deleted_junk": counts["deleted_junk"],
        "deleted_corrupt": counts["deleted_corrupt"],
        "deleted_unrecoverable": counts["deleted_unrecoverable"],
        "system_integrity_failure": counts["system_integrity_failure"],
        "failed": counts["failed"],
        "skipped_missing": counts["skipped_missing"],
        "supplement_pending_parent": counts["supplement_pending_parent"],
        "supplement_linked": counts["supplement_linked"],
        "metacheck_advanced": counts["metacheck_advanced"],
        "metacheck_not_applicable": counts["metacheck_not_applicable"],
        "metacheck_technical_unavailable": counts["metacheck_technical_unavailable"],
        "metacheck_failed": counts["metacheck_failed"],
    }
    after_counts = _complete_paper_counts()
    before_counts = summary.get("db_counts_before") or {}
    summary["clean_complete_papers_after"] = after_counts["clean_complete_papers"]
    summary["active_db_rows_after"] = summary["db_rows_after"]
    summary["source_pdfs_after"] = summary["source_pdfs_after"]
    summary["new_complete_papers_added"] = max(
        0,
        after_counts["clean_complete_papers"] - int(before_counts.get("clean_complete_papers") or 0),
    )
    summary["new_active_rows_added"] = max(
        0,
        summary["db_rows_after"] - int(summary.get("db_rows_before") or summary["db_rows_after"]),
    )
    if before_counts:
        summary["clean_complete_papers_before"] = before_counts.get("clean_complete_papers")
        summary["active_db_rows_before"] = before_counts.get("active_db_rows")
        summary["source_pdfs_before"] = before_counts.get("source_pdfs")
    qc_ok, qc_msg = _quick_check_ok()
    summary["pragma_quick_check"] = qc_msg
    summary["pragma_quick_check_ok"] = qc_ok
    audit = None
    if include_audit:
        audit, audit_error = _safe_source_ownership_audit()
        if audit_error:
            summary["audit_error"] = audit_error
            summary["source_ownership_status"] = "audit_error"
        else:
            audit = audit or {}
            unowned_total = int(audit.get("unowned_total") or 0)
            ambiguous_total = int(audit.get("ambiguous_total") or 0)
            if ambiguous_total:
                ownership_status = "ambiguous"
            elif unowned_total:
                ownership_status = "unowned"
            else:
                ownership_status = "ok"
            summary["source_ownership_status"] = ownership_status
            summary["source_ownership"] = {
                "source_pdfs_total": audit.get("source_pdfs_total"),
                "owned_total": audit.get("owned_total"),
                "unowned_total": unowned_total,
                "ambiguous_total": ambiguous_total,
                "unowned_source_pdfs": audit.get("unowned_source_pdfs", [])[:50],
                "ambiguous_source_pdfs": audit.get("ambiguous_source_pdfs", [])[:50],
            }
    terminal = {
        "batch_stopped",
        "crashed",
        "interrupted",
        "failed_sonnet_unavailable",
        "failed_preconditions",
        "lock_busy",
        "stopped",
        "stopped_user",
        "failed_integrity",
    }
    current = str(summary.get("status") or "")
    if current not in terminal and current not in _TRANSIENT_RUN_STATUSES:
        summary["status"] = _resolve_final_run_status(summary)
    update_summary_costs(summary)


def _finalize_run_status(summary: dict[str, Any]) -> None:
    """Recompute terminal status at end of run (ignores mid-run failed_integrity)."""
    summary["status"] = _resolve_final_run_status(summary)


def _should_stop(run_dir: Path, stopped: dict[str, bool], summary: dict[str, Any]) -> bool:
    if stopped["value"]:
        return True
    stop_file = run_dir / "STOP_REQUESTED"
    if stop_file.exists():
        stopped["value"] = True
        summary.update(status="stopped_user", error="STOP_REQUESTED", stop_requested=True, status_message="Stop requested")
        return True
    return False


def _run_review_dust_drain_phase(
    summary: dict[str, Any],
    *,
    run_dir: Path,
    log_fp,
    allow_paid_api: bool = False,
    allow_opus_resolution: bool = False,
) -> None:
    """Drain legacy review dust via central disposition transactions (Sonnet→Opus when enabled)."""
    from file_disposition_engine import drain_legacy_recovery_pending_files

    summary["phase"] = "draining_review_dust"
    summary["phase_label"] = "Draining review dust"
    summary["status_message"] = "Draining legacy review dust piles"
    legacy = drain_legacy_recovery_pending_files(
        run_id=str(summary.get("run_id") or "legacy-recovery-pending-drain"),
        allow_paid_api=allow_paid_api,
        allow_opus_resolution=allow_opus_resolution and allow_paid_api,
        allow_accept_to_db=False,
        log_fn=lambda m: _log(m, log_fp=log_fp),
    )
    summary["legacy_recovery_pending_drain"] = legacy
    drain = drain_review_dust_piles(
        execute=True,
        allow_paid_api=allow_paid_api,
        allow_opus_resolution=allow_opus_resolution and allow_paid_api,
        log_fn=lambda m: _log(m, log_fp=log_fp),
    )
    summary["review_dust_drain"] = drain
    summary["review_dust_remaining"] = int(drain.get("review_dust_pdfs_remaining") or 0)
    summary["review_dust_orphan_sidecars_removed"] = int(drain.get("orphan_sidecars_removed") or 0)


def run_all_staging_plan_only(
    *,
    prescan: dict[str, Any],
    standard: dict[str, Any],
    recommendation: dict[str, str],
    requested_mode: str | None,
    args,
) -> dict[str, Any]:
    ok, checks = validate_plan_gates(args)
    if not ok:
        return {"ok": False, "precondition_checks": checks, "error": "Plan preconditions failed"}

    staging_count = len(list_root_staging_pdfs())
    args.max_papers = staging_count
    run_plan = build_run_plan(
        prescan=prescan,
        standard=standard,
        recommendation=recommendation,
        requested_mode=requested_mode,
        max_papers=staging_count,
        args=args,
    )
    plan_path, report_path = write_run_plan(run_plan, runs_dir=getattr(args, "runs_dir", "") or "")
    run_dir = plan_path.parent

    already_in_db = sum(1 for p in prescan.get("pdfs", []) if p.get("already_in_db"))
    new_candidates = staging_count - already_in_db
    est_cost = round(new_candidates * 0.07, 2)

    workflow = {
        "created_at": _now(),
        "workflow": "ingest",
        "workflow_target": WORKFLOW_TARGET,
        "mode": "plan_only",
        "read_only": True,
        "run_id": run_dir.name,
        "selected_mode": run_plan.get("selected_mode", "A"),
        "staging_root_pdf_count": staging_count,
        "selected_pdf_count": staging_count,
        "already_in_db_count": already_in_db,
        "new_ingest_candidates": new_candidates,
        "evaluator": config.EVAL_MODEL,
        "eval_routing": "deepseek_first_escalation",
        "estimated_cost_usd_new_only": est_cost,
        "pdf_plans": run_plan.get("pdf_plans", []),
        "future_commands": {
            "plan_only_command": (
                "python pipeline/run_corpus_pipeline.py --workflow ingest --mode A "
                f"--workflow-target {WORKFLOW_TARGET} --plan-only"
            ),
            "live_command": (
                "python pipeline/run_corpus_pipeline.py --workflow ingest --mode A "
                f"--workflow-target {WORKFLOW_TARGET} --run-id all-staging-resume-YYYYMMDD-HHMMSS "
                "--execute --allow-db-write --allow-pdf-copy --allow-paid-api --allow-opus-resolution"
            ),
        },
        "artifact_paths": {
            "run_plan": str(plan_path),
            "run_report": str(report_path),
            "workflow_plan": str(run_dir / "workflow_plan.json"),
            "workflow_report": str(run_dir / "workflow_report.txt"),
        },
    }
    wf_json = run_dir / "workflow_plan.json"
    wf_txt = run_dir / "workflow_report.txt"
    wf_json.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
    wf_txt.write_text(
        "\n".join(
            [
                "All-staging ingest plan (read-only)",
                "=" * 40,
                f"Staging root PDFs: {staging_count}",
                f"Already in DB (will skip): {already_in_db}",
                f"New ingest candidates: {new_candidates}",
                f"Evaluator: {config.EVAL_MODEL_KEY} ({config.EVAL_MODEL})",
                f"Estimated cost (new only): ~${est_cost}",
                "",
                "Live command:",
                workflow["future_commands"]["live_command"],
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    return {
        "ok": True,
        "precondition_checks": checks,
        "staging_root_pdf_count": staging_count,
        "workflow": workflow,
        "run_plan": run_plan,
        "plan_path": plan_path,
        "report_path": report_path,
        "workflow_plan_path": wf_json,
        "workflow_report_path": wf_txt,
        "future_commands": workflow["future_commands"],
    }


def run_all_staging_live(args, *, prescan: dict[str, Any] | None = None) -> dict[str, Any]:
    run_id = getattr(args, "run_id", "") or f"all-staging-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir = Path(getattr(args, "runs_dir", "") or config.RUNS_DIR) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    console_log = run_dir / "console.log"
    log_fp = console_log.open("a", encoding="utf-8")

    summary: dict[str, Any] = {
        "created_at": _now(),
        "run_id": run_id,
        "workflow": WORKFLOW_TARGET,
        "evaluator": config.EVAL_MODEL,
        "eval_routing": "deepseek_first_escalation",
        "items": [],
        "current_pdf": None,
        "current_stage": None,
        "phase": "starting",
        "phase_label": "Starting",
        "phase_current": 0,
        "phase_total": 0,
        "current_file": None,
        "status_message": "Starting",
        "stop_requested": False,
    }
    counts = {
        "success": 0,
        "research_success": 0,
        "non_ratable_success": 0,
        "confirmed_duplicate_deleted": 0,
        "returned_to_staging": 0,
        "moved_to_rejected": 0,
        "deleted_junk": 0,
        "deleted_corrupt": 0,
        "deleted_unrecoverable": 0,
        "system_integrity_failure": 0,
        "technical_failure": 0,
        "routed_review": 0,
        "failed": 0,
        "skipped_missing": 0,
        "supplement_linked": 0,
        "supplement_pending_parent": 0,
        "metacheck_advanced": 0,
        "metacheck_not_applicable": 0,
        "metacheck_technical_unavailable": 0,
        "metacheck_failed": 0,
    }
    started_mono = time.monotonic()
    paths: dict[str, str] = {}
    stopped = {"value": False}
    supplement_ctx = ChildDocumentRunContext()
    summary.update(supplement_ctx.to_summary())
    stop_file = run_dir / "STOP_REQUESTED"
    stop_file.unlink(missing_ok=True)

    phase_labels = {
        "starting": "Starting",
        "snapshotting_staging": "Snapshotting staging",
        "preflight_dedupe": "Preflight dedupe",
        "processing_staging": "Processing staging",
        "resolving_review": "Resolving review/model failures",
        "matching_pending_children": "Matching pending child/support docs",
        "finalizing": "Finalizing",
        "done": "Done",
        "stopping": "Stopping",
        "stopped": "Stopped",
        "failed": "Failed",
    }

    def set_phase(
        phase: str,
        *,
        current: int = 0,
        total: int = 0,
        current_file: str | None = None,
        message: str | None = None,
        status: str | None = None,
    ) -> None:
        summary["phase"] = phase
        summary["phase_label"] = phase_labels.get(phase, phase.replace("_", " ").title())
        summary["phase_current"] = current
        summary["phase_total"] = total
        summary["current_file"] = current_file
        summary["current_pdf"] = current_file
        summary["current_stage"] = phase
        summary["status_message"] = message or summary["phase_label"]
        if status:
            summary["status"] = status

    def write_live_update(partial_status: str | None = None) -> None:
        if partial_status:
            summary["status"] = partial_status
        summary["elapsed_seconds"] = round(time.monotonic() - started_mono, 1)
        summary["elapsed_time"] = summary["elapsed_seconds"]
        summary["processed_count"] = len(summary.get("items", []))
        summary["stop_requested"] = bool(stopped["value"] or (run_dir / "STOP_REQUESTED").exists())
        live_path = run_dir / "all_staging_ingest_live.json"
        live_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        paths["live"] = str(live_path)

    def checkpoint(partial_status: str | None = None, *, include_audit: bool = False) -> None:
        if partial_status:
            summary["status"] = partial_status
        _finalize_summary(summary, counts, started_mono=started_mono, include_audit=include_audit)
        paths.update(_write_batch_artifacts(run_dir, summary))

    def _handle_stop(signum=None, frame=None) -> None:  # noqa: ARG001
        stopped["value"] = True
        try:
            (run_dir / "STOP_REQUESTED").write_text("1", encoding="utf-8")
        except OSError:
            pass
        summary.update(
            status="stopped",
            error=f"signal {signum}" if signum else "STOP_REQUESTED",
            stop_requested=True,
            status_message="Stop requested",
        )

    try:
        signal.signal(signal.SIGINT, _handle_stop)
    except (ValueError, OSError):
        pass
    try:
        signal.signal(signal.SIGTERM, _handle_stop)
    except (ValueError, OSError, AttributeError):
        pass

    try:
        summary.update(status="starting", phase="starting", current_stage="starting")
        write_live_update("starting")

        ok, checks, ctx = validate_live_gates(args)
        summary["precondition_checks"] = checks
        if not ok:
            if ctx.get("sonnet_unavailable"):
                summary.update(status="failed_sonnet_unavailable", error=SONNET_UNAVAILABLE_MSG)
            else:
                summary.update(status="failed_preconditions", error="Live gate preconditions failed")
            checkpoint(include_audit=True)
            return {"ok": False, "summary": summary, "artifacts": paths}

        lock_ok, lock_msg, stale = acquire_ingest_lock(run_id)
        summary["lock"] = {"acquired": lock_ok, "message": lock_msg, "stale_recovery": stale}
        if not lock_ok:
            summary.update(status="lock_busy", error=lock_msg)
            checkpoint(include_audit=True)
            return {"ok": False, "summary": summary, "artifacts": paths}

        _log(f"All-staging ingest starting run_id={run_id}", log_fp=log_fp)
        reset_run_decision_cache()
        intake_rejected.clear_run_rejected()
        intake_rejected.set_current_run_id(run_id)
        intake_rejected.ensure_rejected_layout()
        purged_rejected = intake_rejected.purge_expired_rejected()
        if purged_rejected:
            summary["rejected_purged_this_run"] = purged_rejected
            _log(f"Purged {len(purged_rejected)} expired file(s) from papers-rejected/", log_fp=log_fp)

        pending_count = (
            len([p for p in config.PENDING_PARENT_CHILD_DOCUMENTS.iterdir() if p.is_file() and not p.name.endswith(".meta.json")])
            if config.PENDING_PARENT_CHILD_DOCUMENTS.exists()
            else 0
        )
        summary["pending_child_documents_remaining"] = pending_count
        summary["pending_supplements_remaining"] = pending_count
        summary.update(supplement_ctx.to_summary())

        cleaned = cleanup_incomplete_staging_rows()
        if cleaned:
            summary["startup_cleanup_incomplete_rows"] = cleaned
            for row in cleaned:
                _log(
                    f"Startup cleanup: rolled back incomplete row {row.get('paper_id')} ({row.get('reason')})",
                    log_fp=log_fp,
                )
        deduped_pending = dedupe_pending_child_documents(log_fn=lambda m: _log(m, log_fp=log_fp))
        if deduped_pending:
            summary["pending_child_deduped"] = deduped_pending
            _log(f"Startup cleanup: removed {deduped_pending} duplicate pending child document(s)", log_fp=log_fp)
        if _should_stop(run_dir, stopped, summary):
            summary["staging_skipped"] = "stopped during startup cleanup"
            checkpoint("stopped", include_audit=True)
            return {"ok": False, "summary": summary, "artifacts": paths}

        set_phase("snapshotting_staging", status="snapshotting_staging", message="Snapshotting papers-staging candidates")
        write_live_update("snapshotting_staging")
        staging_snapshot = list_staging_candidates()
        summary["raw_staging_candidates_before_preflight"] = len(staging_snapshot)
        summary["staging_snapshot_count"] = len(staging_snapshot)
        if _should_stop(run_dir, stopped, summary):
            summary["staging_skipped"] = "stopped during staging snapshot"
            checkpoint("stopped", include_audit=True)
            return {"ok": False, "summary": summary, "artifacts": paths}

        def on_preflight_progress(rec: dict[str, Any]) -> None:
            summary["preflight_dedupe_current"] = int(rec.get("index") or 0)
            summary["preflight_dedupe_total"] = int(rec.get("total") or 0)
            summary["preflight_duplicates_deleted"] = int(rec.get("deleted") or 0)
            summary["preflight_unique_so_far"] = int(rec.get("unique") or 0)
            set_phase(
                "preflight_dedupe",
                current=summary["preflight_dedupe_current"],
                total=summary["preflight_dedupe_total"],
                current_file=rec.get("file") or None,
                message=(
                    f"Preflight dedupe: checked {summary['preflight_dedupe_current']} / {summary['preflight_dedupe_total']}, "
                    f"deleted {summary['preflight_duplicates_deleted']} duplicate(s), "
                    f"unique so far {summary['preflight_unique_so_far']}"
                ),
                status="preflight_dedupe",
            )
            write_live_update("preflight_dedupe")

        set_phase(
            "preflight_dedupe",
            current=0,
            total=len(staging_snapshot),
            message=f"Preflight dedupe: checked 0 / {len(staging_snapshot)}",
            status="preflight_dedupe",
        )
        write_live_update("preflight_dedupe")
        preflight = preflight_dedupe_staging_candidates(
            staging_snapshot,
            run_dir=run_dir,
            should_stop=lambda: _should_stop(run_dir, stopped, summary),
            on_progress=on_preflight_progress,
            log_fn=lambda m: _log(m, log_fp=log_fp),
        )
        staging_candidates = list(preflight.pop("unique_candidates", []))
        summary["preflight_dedupe"] = preflight
        summary["preflight_duplicates_deleted"] = int(preflight.get("db_owned_hash_duplicates_deleted") or 0) + int(
            preflight.get("intra_staging_duplicates_deleted") or 0
        )
        summary["preflight_db_owned_duplicates_deleted"] = int(preflight.get("db_owned_hash_duplicates_deleted") or 0)
        summary["preflight_intra_staging_duplicates_deleted"] = int(preflight.get("intra_staging_duplicates_deleted") or 0)
        summary["unique_candidates_to_process"] = len(staging_candidates)
        counts["confirmed_duplicate_deleted"] += int(summary["preflight_duplicates_deleted"])
        if stopped["value"] or preflight.get("stopped"):
            summary["staging_skipped"] = "stopped during preflight dedupe"
            checkpoint("stopped", include_audit=True)
            return {"ok": False, "summary": summary, "artifacts": paths}

        if prescan is None:
            set_phase("snapshotting_staging", message="Running lightweight prescan after preflight", status="snapshotting_staging")
            write_live_update("snapshotting_staging")
            from corpus_prescan import run_prescan

            prescan = run_prescan(
                staging_dir=getattr(args, "staging_dir", "") or "",
                db_path=getattr(args, "db_path", "") or "",
                blank_corpus=getattr(args, "blank_corpus", False),
            )
        if _should_stop(run_dir, stopped, summary):
            summary["staging_skipped"] = "stopped during prescan"
            checkpoint("stopped", include_audit=True)
            return {"ok": False, "summary": summary, "artifacts": paths}

        prescan_by_name = {p["name"]: p for p in prescan.get("pdfs", [])}
        already_in_db = 0
        new_candidates = len(staging_candidates)
        aud_rate, aud_source = fetch_usd_to_aud_rate()

        summary["starting_staging_count"] = len(staging_candidates)
        summary["selected_pdf_count"] = len(staging_candidates)
        summary["already_in_db_count"] = already_in_db
        summary["new_ingest_candidates"] = new_candidates
        summary["estimated_total_cost_usd"] = round(new_candidates * config.SONNET_ESTIMATE_USD_PER_PAPER, 4)
        summary["usd_to_aud_rate"] = aud_rate
        summary["usd_to_aud_rate_source"] = aud_source
        summary["db_rows_before"] = papers_db.get_paper_count()
        summary["source_pdfs_before"] = len(list(config.SOURCE_PDFS.glob("*.pdf")))
        db_before = _complete_paper_counts()
        summary["db_counts_before"] = {
            "clean_complete_papers": db_before["clean_complete_papers"],
            "active_db_rows": summary["db_rows_before"],
            "source_pdfs": summary["source_pdfs_before"],
        }
        summary["clean_complete_papers_before"] = db_before["clean_complete_papers"]
        summary["active_db_rows_before"] = summary["db_rows_before"]
        summary["existing_review_dust_count"] = count_review_dust_pdfs()

        backup_path = papers_db.backup(suffix="PRE-ALL-STAGING-INGEST")
        summary["backup_path"] = str(backup_path) if backup_path else None
        _log(f"Backup: {backup_path}", log_fp=log_fp)

        allow_paid_api = bool(getattr(args, "allow_paid_api", False))
        allow_local_ollama = bool(getattr(args, "allow_local_ollama", False))
        config.USE_LOCAL_OLLAMA_ONLY = bool(allow_local_ollama and not allow_paid_api)
        allow_opus_resolution = bool(getattr(args, "allow_opus_resolution", False)) and allow_paid_api
        summary["review_resolution_enabled"] = allow_opus_resolution
        summary["metacheck_mode"] = "advanced_required"

        update_summary_costs(summary)
        checkpoint("snapshotting_staging")

        client = anthropic.Anthropic() if allow_paid_api else None
        set_phase(
            "processing_staging",
            current=0,
            total=len(staging_candidates),
            message=f"Processing staging: 0 / {len(staging_candidates)}",
            status="running",
        )
        checkpoint("running")

        for idx, pdf_path in enumerate(staging_candidates, 1):
            if _should_stop(run_dir, stopped, summary):
                set_phase("stopped", current=idx - 1, total=len(staging_candidates), status="stopped", message="Stopped")
                summary.update(error=summary.get("error") or "STOP_REQUESTED")
                break

            summary["current_index"] = idx
            set_phase(
                "processing_staging",
                current=idx - 1,
                total=len(staging_candidates),
                current_file=pdf_path.name,
                message=f"Processing staging: {idx} / {len(staging_candidates)}",
                status="running",
            )
            write_live_update("running")
            _log(f"\n[{idx}/{len(staging_candidates)}] {pdf_path.name}", log_fp=log_fp)

            stage_holder = {"stage": "start"}

            def on_stage(stage_name: str, _holder=stage_holder) -> None:
                _holder["stage"] = stage_name
                summary["current_stage"] = stage_name
                summary["current_file"] = pdf_path.name
                summary["current_pdf"] = pdf_path.name
                summary["phase_current"] = idx - 1
                summary["phase_total"] = len(staging_candidates)
                summary["status_message"] = f"Processing {pdf_path.name}: {stage_name}"
                rec = {
                    "event": "stage",
                    "at": _now(),
                    "index": idx,
                    "filename": pdf_path.name,
                    "stage": stage_name,
                    "elapsed_seconds": round(time.monotonic() - started_mono, 1),
                }
                _write_progress_line(run_dir, rec)
                write_live_update("running")

            item = process_one_staging_file(
                pdf_path,
                client,
                prescan_by_name=prescan_by_name,
                on_stage=on_stage,
                tags=INGEST_TAG,
                supplement_ctx=supplement_ctx,
                log_fn=log_fp,
                allow_paid_api=allow_paid_api,
                allow_opus_resolution=bool(getattr(args, "allow_opus_resolution", False)),
                stop_requested=lambda: _should_stop(run_dir, stopped, summary),
            )
            item["index"] = idx
            item["elapsed_seconds"] = round(time.monotonic() - started_mono, 1)
            summary["items"].append(item)
            status = item.get("status", "failed")
            metacheck = item.get("metacheck") or {}
            if metacheck:
                if metacheck.get("source") == "advanced_metacheck":
                    counts["metacheck_advanced"] += 1
                elif metacheck.get("source") == "not_applicable":
                    counts["metacheck_not_applicable"] += 1
                elif metacheck.get("status") == "technical_unavailable":
                    counts["metacheck_technical_unavailable"] += 1
                elif metacheck.get("ok") is False:
                    counts["metacheck_failed"] += 1

            usage = item.get("eval_usage")
            if usage:
                accumulate_sonnet_usage(
                    summary,
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                )
            llm_usage = item.get("llm_adjudication_usage") or {}
            if llm_usage:
                son_in = int(llm_usage.get("sonnet_input_tokens") or 0)
                son_out = int(llm_usage.get("sonnet_output_tokens") or 0)
                op_in = int(llm_usage.get("opus_input_tokens") or 0)
                op_out = int(llm_usage.get("opus_output_tokens") or 0)
                if son_in or son_out:
                    adj_llm = ((item.get("adjudication") or {}).get("llm_adjudication") or {})
                    vision_model = str(
                        adj_llm.get("final_model")
                        or (adj_llm.get("sonnet_usage") or {}).get("model")
                        or ""
                    )
                    accumulate_vision_usage(
                        summary,
                        input_tokens=son_in,
                        output_tokens=son_out,
                        model_id=vision_model,
                    )
                if op_in or op_out:
                    accumulate_opus_usage(summary, input_tokens=op_in, output_tokens=op_out)

            _write_progress_line(run_dir, {"event": "paper_done", **item})
            summary["phase_current"] = idx
            summary["phase_total"] = len(staging_candidates)
            summary["status_message"] = f"Processed {idx} / {len(staging_candidates)} unique staging candidate(s)"
            checkpoint("running")

            if status == "batch_stop":
                summary.update(
                    status="batch_stopped",
                    error=item.get("reason"),
                    stopped_at_index=idx,
                    stopped_filename=pdf_path.name,
                    stopped_stage=item.get("stage"),
                )
                counts["failed"] += 1
                break
            if status == "success":
                counts["success"] += 1
                if item.get("corpus_class") == "non_ratable_reference_material" or item.get("evaluation_path") == "deterministic_non_ratable":
                    counts["non_ratable_success"] += 1
                else:
                    counts["research_success"] += 1
            elif status == "confirmed_duplicate_deleted":
                counts["confirmed_duplicate_deleted"] += 1
            elif status == "returned_to_staging":
                counts["returned_to_staging"] += 1
            elif status == "moved_to_rejected":
                counts["moved_to_rejected"] += 1
            elif status == "deleted_junk":
                counts["deleted_junk"] += 1
            elif status == "deleted_corrupt":
                counts["deleted_corrupt"] += 1
            elif status == "deleted_unrecoverable":
                counts["deleted_unrecoverable"] += 1
            elif status == "system_integrity_failure":
                counts["system_integrity_failure"] += 1
            elif status == "skipped_missing":
                counts["skipped_missing"] += 1
            elif status == "supplement_pending_parent" or status == "child_document_pending_parent":
                counts["supplement_pending_parent"] += 1
            elif str(status).endswith("_linked"):
                counts["supplement_linked"] += 1
            else:
                counts["failed"] += 1

            qc_ok, qc_msg = _quick_check_ok()
            if not qc_ok:
                summary.update(
                    status="batch_stopped",
                    error=f"PRAGMA quick_check failed: {qc_msg}",
                    stopped_at_index=idx,
                )
                break

        else:
            if not stopped["value"] and summary.get("status") not in {"batch_stopped", "stopped"}:
                summary["status"] = "completed"

        # Phase 2b: finish any staging PDFs left behind (no "returned_to_staging" exits).
        if not stopped["value"] and summary.get("status") not in {
            "batch_stopped",
            "crashed",
            "failed_sonnet_unavailable",
            "stopped",
        }:
            drain_round = 0
            final_outcomes = _final_items_by_filename(summary.get("items") or [])
            while list_root_staging_pdfs() and drain_round < 12:
                drain_round += 1
                remaining = list_root_staging_pdfs()
                if log_fp:
                    log_fp.write(
                        f"\nStaging completion drain round {drain_round}: "
                        f"{len(remaining)} file(s)\n"
                    )
                    log_fp.flush()
                summary["status_message"] = (
                    f"Finishing {len(remaining)} remaining staging file(s) (round {drain_round})"
                )
                checkpoint("running")
                for pdf_path in remaining:
                    if stopped["value"]:
                        break
                    prior = final_outcomes.get(pdf_path.name) or {}
                    prior_status = str(prior.get("status") or "")
                    if prior_status in _TERMINAL_STAGING_OUTCOMES:
                        continue
                    item = process_one_staging_file(
                        pdf_path,
                        client,
                        prescan_by_name=prescan_by_name,
                        on_stage=on_stage,
                        tags=INGEST_TAG,
                        supplement_ctx=supplement_ctx,
                        log_fn=log_fp,
                        allow_paid_api=allow_paid_api,
                        allow_opus_resolution=allow_opus_resolution,
                    )
                    summary["items"].append(item)
                    final_outcomes[pdf_path.name] = item
                    st = item.get("status", "failed")
                    if st == "success":
                        counts["success"] += 1
                    elif st == "deleted_junk":
                        counts["deleted_junk"] += 1
                    elif st == "deleted_corrupt":
                        counts["deleted_corrupt"] += 1
                    elif st == "deleted_unrecoverable":
                        counts["deleted_unrecoverable"] += 1
                    elif st == "confirmed_duplicate_deleted":
                        counts["confirmed_duplicate_deleted"] += 1
                    elif st == "returned_to_staging":
                        counts["returned_to_staging"] += 1
                    elif st == "moved_to_rejected":
                        counts["moved_to_rejected"] += 1
                    elif st in {"child_document_pending_parent", "supplement_pending_parent"}:
                        counts["supplement_pending_parent"] += 1
                    elif st == "system_integrity_failure":
                        counts["system_integrity_failure"] += 1
                    elif str(st).endswith("_linked"):
                        counts["supplement_linked"] += 1
                    else:
                        counts["failed"] += 1

        staging_failed = summary.get("status") in {
            "batch_stopped",
            "crashed",
            "failed_sonnet_unavailable",
            "stopped",
        }

        # Phase 3: drain legacy review dust (deterministic only; no model calls)
        if stopped["value"] or staging_failed:
            summary["review_dust_drain_skipped"] = "stopped_or_failed_before_drain"
        else:
            _run_review_dust_drain_phase(
                summary,
                run_dir=run_dir,
                log_fp=log_fp,
                allow_paid_api=bool(getattr(args, "allow_paid_api", False)),
                allow_opus_resolution=allow_opus_resolution,
            )

        summary["review_items_remaining_technical_failure"] = 0
        summary["review_items_remaining"] = count_review_dust_pdfs()

        # Legacy aggregate fields for launchers
        summary["review_items_start"] = summary.get("existing_review_dust_count")
        summary["review_items_resolved"] = int(summary.get("pre_review_review_items_resolved") or 0) + int(
            summary.get("post_review_review_items_resolved") or 0
        )
        summary["review_items_moved_to_staging"] = int(summary.get("pre_review_review_items_moved_to_staging") or 0) + int(
            summary.get("post_review_review_items_moved_to_staging") or 0
        )
        summary["review_items_deleted_duplicate"] = int(
            summary.get("pre_review_review_items_deleted_duplicate") or 0
        ) + int(summary.get("post_review_review_items_deleted_duplicate") or 0)
        summary["review_items_deleted_junk"] = int(summary.get("pre_review_review_items_deleted_junk") or 0) + int(
            summary.get("post_review_review_items_deleted_junk") or 0
        )
        summary["review_items_remaining"] = count_review_dust_pdfs()
        update_summary_costs(summary)

        run_completed_normally = (
            not stopped["value"]
            and summary.get("status") in {"completed", "done_clean", "done_with_deletions", "done_with_pending_children"}
            and not summary.get("staging_skipped")
        )
        pending_match_needed = bool(supplement_ctx.run_paper_ids) or int(
            supplement_ctx.counts.get("child_documents_moved_to_pending") or 0
        ) > 0
        if run_completed_normally and pending_match_needed:
            pending_total = (
                len([p for p in config.PENDING_PARENT_CHILD_DOCUMENTS.iterdir() if p.is_file() and not p.name.endswith(".meta.json")])
                if config.PENDING_PARENT_CHILD_DOCUMENTS.exists()
                else 0
            )

            def on_post_pending_progress(rec: dict[str, Any]) -> None:
                idx = int(rec.get("index") or 0)
                total = int(rec.get("total") or pending_total)
                set_phase(
                    "matching_pending_children",
                    current=idx,
                    total=total,
                    current_file=None,
                    message=f"Pending child/support match post-run: checked {idx} / {total}",
                    status="matching_pending_children",
                )
                write_live_update("matching_pending_children")

            set_phase(
                "matching_pending_children",
                current=0,
                total=pending_total,
                message=f"Pending child/support match post-run: checked 0 / {pending_total}",
                status="matching_pending_children",
            )
            write_live_update("matching_pending_children")
            linked = retry_pending_child_documents(
                supplement_ctx,
                log_fn=None,
                should_stop=lambda: _should_stop(run_dir, stopped, summary),
                on_progress=on_post_pending_progress,
                verbose=False,
            )
            summary["post_run_pending_child_match"] = {
                "checked": int(supplement_ctx.counts.get("pending_child_inspected") or 0),
                "linked": linked,
                "skipped": False,
            }
            _log(f"Pending child/support match post-run: checked {pending_total} sidecar(s), linked {linked}", log_fp=log_fp)
            summary.update(supplement_ctx.to_summary())
            if stopped["value"]:
                run_completed_normally = False
                summary.update(status="stopped_user", error=summary.get("error") or "STOP_REQUESTED")
        else:
            summary["post_run_pending_child_match"] = {
                "checked": 0,
                "linked": 0,
                "skipped": "no_new_parents_or_pending_children" if run_completed_normally else "run_not_completed",
            }
        summary.update(
            finalize_pending_child_documents(
                supplement_ctx,
                run_completed_normally=run_completed_normally,
                log_fn=lambda m: _log(m, log_fp=log_fp),
            )
        )
        if run_completed_normally:
            try:
                from translation_pair_linking import link_translation_pairs

                translation_links = link_translation_pairs()
                summary["translation_pair_linking"] = translation_links
                if int(translation_links.get("linked_count") or 0):
                    _log(
                        f"Translation/source-language linking: linked {translation_links.get('linked_count')} pair(s)",
                        log_fp=log_fp,
                    )
            except Exception as e:
                summary["translation_pair_linking"] = {
                    "ok": False,
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                }
                _log(f"Translation/source-language linking skipped after error: {e}", log_fp=log_fp)

    except KeyboardInterrupt:
        stopped["value"] = True
        summary.update(status="stopped_user", error="KeyboardInterrupt", phase=summary.get("phase") or "staging")
        summary.update(
            finalize_pending_child_documents(
                supplement_ctx,
                run_completed_normally=False,
                log_fn=lambda m: _log(m, log_fp=log_fp) if log_fp else None,
            )
        )
    except Exception as e:
        summary.update(status="crashed", error=str(e), traceback=traceback.format_exc())
        summary.update(
            finalize_pending_child_documents(
                supplement_ctx,
                run_completed_normally=False,
                log_fn=lambda m: _log(m, log_fp=log_fp) if log_fp else None,
            )
        )
    finally:
        if summary.get("status") not in {"stopped_user", "crashed", "failed_preconditions", "failed_sonnet_unavailable", "lock_busy", "failed_integrity"}:
            set_phase("finalizing", message="Finalizing", status="finalizing")
            write_live_update("finalizing")
        summary["sweep_rejected"] = intake_rejected.sweep_remaining_staging_to_rejected(run_id=run_id)
        _finalize_summary(summary, counts, started_mono=started_mono, include_audit=True)
        _finalize_run_status(summary)
        _attach_rejected_paths_to_items(summary.get("items") or [])
        status_before_final = summary.get("status")
        if status_before_final in {
            "done_clean",
            "done_with_deletions",
            "done_with_pending_children",
            "done_with_file_rejections",
            "done_with_unresolved_files",
        }:
            set_phase("done", status=status_before_final, message="Done")
        elif status_before_final == "stopped_user":
            set_phase("stopped", status="stopped_user", message="Stopped")
        elif status_before_final in {"crashed", "failed_preconditions", "failed_sonnet_unavailable", "lock_busy", "failed_integrity"}:
            set_phase("failed", status=status_before_final, message=str(summary.get("error") or "Failed"))
        intake_rejected.write_intake_last_run(summary)
        paths = _write_batch_artifacts(run_dir, summary)
        _release_lock()
        log_fp.close()

    batch_ok = summary.get("status") in {
        "done_clean",
        "done_with_deletions",
        "done_with_pending_children",
        "done_with_file_rejections",
        "done_with_unresolved_files",
    } and summary.get("pragma_quick_check_ok")
    return {"ok": batch_ok, "summary": summary, "artifacts": paths}


def review_resolution_gate_ok(args) -> bool:
    return bool(getattr(args, "allow_opus_resolution", False)) and bool(getattr(args, "allow_paid_api", False))
