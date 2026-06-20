"""Mandatory recovery ladder before routing PDFs to review folders."""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from path_governance import reserve_unique_path, shorten_existing_filename

RECOVERY_DECISIONS = frozenset(
    {
        "RECOVERED_RESEARCH_PAPER_MOVE_TO_STAGING",
        "RECOVERED_NON_RATABLE_REFERENCE_MOVE_TO_STAGING",
        "CHILD_DOCUMENT_TO_PENDING_PARENT",
        "DELETE_DUPLICATE",
        "DELETE_JUNK",
        "TECHNICAL_FAILURE_UNRECOVERABLE",
        "API_OR_MODEL_UNAVAILABLE",
        "MODEL_RECOVERY_REQUIRED",
    }
)

LEGACY_DECISION_MAP = {
    "RECOVERED_RESEARCH_PAPER_MOVE_TO_STAGING": "MOVE_TO_STAGING_FOR_PROCESSING",
    "RECOVERED_NON_RATABLE_REFERENCE_MOVE_TO_STAGING": "MOVE_TO_STAGING_FOR_PROCESSING",
    "CHILD_DOCUMENT_TO_PENDING_PARENT": "CHILD_TO_PENDING",
    "DELETE_DUPLICATE": "DELETE_DUPLICATE",
    "DELETE_JUNK": "DELETE_JUNK",
    "TECHNICAL_FAILURE_UNRECOVERABLE": "TECHNICAL_FAILURE_KEEP_IN_REVIEW",
    "API_OR_MODEL_UNAVAILABLE": "MODEL_RECOVERY_REQUIRED",
    "MODEL_RECOVERY_REQUIRED": "MODEL_RECOVERY_REQUIRED",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_legacy_decision(decision: str) -> str:
    return LEGACY_DECISION_MAP.get(decision, decision)


def gather_evidence(pdf_path: Path, *, pages: int = 5) -> dict[str, Any]:
    from pdf_content_guard import content_kind, extract_text_preview, is_actual_pdf
    from review_resolution import (
        _find_candidate_matches,
        _is_valid_pdf,
        _pdf_page_count,
        _sha256,
    )

    pdf_path = Path(pdf_path)
    kind = content_kind(pdf_path)
    if not is_actual_pdf(pdf_path):
        text_pages, _, _ = extract_text_preview(pdf_path, pages=pages)
        from pdf_content_guard import recover_metadata_for_path

        recovered = recover_metadata_for_path(pdf_path, pages=pages)
        return {
            "path": str(pdf_path),
            "filename": pdf_path.name,
            "pdf_valid": False,
            "pdf_reason": f"non_pdf_content:{kind}",
            "actual_content_type": kind,
            "file_size": pdf_path.stat().st_size if pdf_path.exists() else 0,
            "page_count": None,
            "text_length": len(text_pages),
            "text_pages_length": len(text_pages),
            "text_front_length": len(text_pages[:2000]),
            "extract_error": None,
            "pdf_embed": {},
            "recovered_metadata": recovered,
            "candidate_matches": [
                {"id": m["paper"]["id"], "score": m["score"], "reasons": m["reasons"]}
                for m in _find_candidate_matches(pdf_path)
            ],
            "visual_recovery_attempted": False,
            "visual_recovery_error": None,
            "rendered_page_images": [],
        }

    ok, pdf_reason = _is_valid_pdf(pdf_path)
    from review_resolution import _extract_text

    text_full, err_full = _extract_text(pdf_path)
    text_pages, err_pages = _extract_text(pdf_path, pages=pages)
    text_front, _ = _extract_text(pdf_path, pages=1)
    from document_text_extraction import meaningful_text_length

    text_len = meaningful_text_length(text_full)

    from metadata_extractors import extract_pdf_embed, recover_metadata_deterministic

    embed = extract_pdf_embed(pdf_path)
    recovered = recover_metadata_deterministic(pdf_path, text_pages, pages=pages)

    evidence: dict[str, Any] = {
        "path": str(pdf_path),
        "filename": pdf_path.name,
        "pdf_valid": ok,
        "pdf_reason": pdf_reason,
        "file_size": pdf_path.stat().st_size if pdf_path.exists() else 0,
        "page_count": _pdf_page_count(pdf_path),
        "text_length": text_len,
        "text_pages_length": meaningful_text_length(text_pages),
        "text_front_length": meaningful_text_length(text_front),
        "extract_error": err_full or err_pages,
        "pdf_embed": embed,
        "recovered_metadata": recovered,
        "candidate_matches": [
            {"id": m["paper"]["id"], "score": m["score"], "reasons": m["reasons"]}
            for m in _find_candidate_matches(pdf_path)
        ],
        "visual_recovery_attempted": False,
        "visual_recovery_error": None,
        "rendered_page_images": [],
    }

    if ok and len(text_pages) < 120 and evidence.get("page_count", 0) >= 1:
        images, verr = _render_pdf_pages(pdf_path, max_pages=min(3, pages))
        evidence["visual_recovery_attempted"] = bool(images) or verr is not None
        evidence["visual_recovery_error"] = verr
        evidence["rendered_page_images"] = images

    if ok:
        try:
            evidence["sha256"] = _sha256(pdf_path)
        except OSError:
            pass

    return evidence


def _render_pdf_pages(pdf_path: Path, *, max_pages: int = 3) -> tuple[list[str], str | None]:
    """Render first N pages to PNG via pdftoppm when available."""
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        return [], "pdftoppm_unavailable"
    render_root = config.LOGS_DIR / "recovery-renders"
    render_name = shorten_existing_filename(
        f"{pdf_path.stem}.dir",
        title=pdf_path.stem,
        hash_source=f"render:{pdf_path}",
        max_filename=96,
    ).removesuffix(".dir")
    out_dir = reserve_unique_path(render_root, render_name, content_token=None)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "page"
    try:
        result = subprocess.run(
            [pdftoppm, "-png", "-f", "1", "-l", str(max_pages), str(pdf_path), str(prefix)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode != 0:
            return [], f"pdftoppm_exit_{result.returncode}"
        images = sorted(str(p) for p in out_dir.glob("*.png"))
        return images[:max_pages], None
    except subprocess.TimeoutExpired:
        return [], "pdftoppm_timeout"
    except OSError as e:
        return [], str(e)


def _enrich_recovered_metadata(pdf_path: Path, recovered: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    from document_corpus_classification import classify_corpus_document, enrich_recovered_metadata

    title = str(recovered.get("title") or "").strip()
    if not title:
        from metadata_extractors import extract_clean_title_from_filename

        title = extract_clean_title_from_filename(pdf_path)
        if title:
            recovered = {**recovered, "title": title, "source": recovered.get("source") or "filename_plain_title"}

    classification = classify_corpus_document(pdf_path.name, title=title, recovered=recovered)
    enriched = enrich_recovered_metadata(recovered, classification)
    if not enriched.get("authors") and classification.get("is_non_ratable"):
        enriched["authors"] = ["Unknown"]
    elif not enriched.get("authors") and title:
        enriched.setdefault("authors", ["Unknown"])
    return enriched, classification


def assess_pdf_usability(pdf_path: Path, *, strict_pdf_only: bool = True) -> dict[str, Any]:
    """Determine whether a PDF is readable or genuinely corrupt.

    HTML/MIME/non-PDF content masquerading as .pdf is NOT marked corrupt — callers must
    classify content before deletion (see supplement_content_classifier).
    """
    from review_resolution import _extract_text, _is_valid_pdf, _pdf_page_count

    pdf_path = Path(pdf_path)
    result: dict[str, Any] = {
        "path": str(pdf_path),
        "readable": False,
        "corrupt": False,
        "reason": "",
        "page_count": 0,
        "text_length": 0,
        "file_size": 0,
        "pdf_valid": False,
        "is_html": False,
        "content_mismatch": False,
        "visual_render_ok": False,
    }
    if not pdf_path.exists():
        result.update(corrupt=True, reason="file_missing")
        return result

    file_size = pdf_path.stat().st_size
    result["file_size"] = file_size
    if file_size == 0:
        result.update(corrupt=True, reason="zero_byte")
        return result

    try:
        with pdf_path.open("rb") as f:
            header = f.read(512)
    except OSError as e:
        result.update(corrupt=True, reason=f"os_error:{e}")
        return result

    is_html = header.lstrip().startswith(b"<!") or b"<html" in header[:200].lower()
    is_mime = header.startswith(b"MIME-Version:") or header.startswith(b"Content-Type:") or b"Content-Type:" in header[:400]
    result["is_html"] = is_html
    ok, pdf_reason = _is_valid_pdf(pdf_path)
    result["pdf_valid"] = ok

    if is_html or is_mime or (not ok and pdf_reason == "invalid_pdf_header"):
        result.update(content_mismatch=True, reason="non_pdf_content_requires_classification")
        return result

    page_count = _pdf_page_count(pdf_path) or 0
    result["page_count"] = page_count
    text, _ = _extract_text(pdf_path, pages=1)
    result["text_length"] = len(text)

    if file_size < 500 and page_count == 0 and len(text) < 5:
        result.update(corrupt=True, reason="file_too_small")
        return result
    if not ok:
        result.update(corrupt=True, reason=pdf_reason or "invalid_pdf")
        return result
    if page_count == 0 and len(text) < 5 and file_size < 8000:
        result.update(corrupt=True, reason="zero_pages_no_extractable_content")
        return result

    if page_count > 0 or len(text) >= 5:
        result["readable"] = True
        return result

    images, verr = _render_pdf_pages(pdf_path, max_pages=1)
    result["visual_render_ok"] = bool(images)
    if page_count > 0 or len(text) >= 5 or images:
        result["readable"] = True
        return result

    result.update(corrupt=True, reason=verr or "unrenderable_pdf")
    return result


def _deterministic_recovery_decision(
    pdf_path: Path,
    evidence: dict[str, Any],
    *,
    hash_index: dict | None = None,
) -> tuple[str | None, dict[str, Any]]:
    from child_document_matching import detect_child_document_type, is_child_document
    from review_resolution import (
        _canonical_owner_for_hash,
        _deterministic_decision,
        _other_staging_paths_for_hash,
        _staging_hash_index,
        delete_confirmed_duplicate,
        is_staging_duplicate_of_other,
    )

    dup = delete_confirmed_duplicate(pdf_path, dry_run=True, hash_index=hash_index)
    if dup.get("decision") == "DELETE_DUPLICATE":
        return "DELETE_DUPLICATE", {**evidence, **(dup.get("evidence") or {}), "ladder_step": "duplicate_check"}

    digest = evidence.get("sha256")
    if digest and _canonical_owner_for_hash(digest, hash_index):
        return "DELETE_DUPLICATE", {**evidence, "match": "hash_identical_owned", "ladder_step": "ownership_repair"}

    usability = assess_pdf_usability(pdf_path)
    evidence = {**evidence, "usability": usability}
    if usability.get("content_mismatch"):
        from supplement_content_classifier import classify_supplement_file, disposition_for_classification

        classified = classify_supplement_file(pdf_path)
        evidence = {**evidence, "content_classification": classified}
        cls = str(classified.get("classification") or "")
        disp = disposition_for_classification(cls)
        if disp == "DELETE":
            return "DELETE_JUNK", {**evidence, "reason": cls.lower(), "ladder_step": "content_classification"}
        if disp == "PENDING":
            child_type = detect_child_document_type(pdf_path.name) or "supplement"
            return "CHILD_DOCUMENT_TO_PENDING_PARENT", {**evidence, "child_type": child_type, "ladder_step": "non_pdf_child_content"}
        if cls == "MODEL_RECOVERY_REQUIRED":
            return "MODEL_RECOVERY_REQUIRED", {**evidence, "ladder_step": "content_classification_uncertain"}
        if disp == "STAGING":
            if cls == "VALID_PDF_NON_RATABLE_REFERENCE":
                return "RECOVERED_NON_RATABLE_REFERENCE_MOVE_TO_STAGING", {**evidence, "ladder_step": "content_classification_staging"}
            return "RECOVERED_RESEARCH_PAPER_MOVE_TO_STAGING", {**evidence, "ladder_step": "content_classification_staging"}
    if usability.get("corrupt"):
        return (
            "DELETE_JUNK",
            {
                **evidence,
                "reason": usability.get("reason") or "corrupt_unreadable_pdf",
                "ladder_step": "pdf_usability_check",
            },
        )

    if is_child_document(pdf_path.name):
        recovered = dict(evidence.get("recovered_metadata") or {})
        enriched, classification = _enrich_recovered_metadata(pdf_path, recovered)
        evidence = {**evidence, "recovered_metadata": enriched, "classification": classification}
        title = str(enriched.get("title") or "").strip().lower()
        # Filename says child, but recovered metadata looks like standalone corpus material.
        if title and len(title) >= 12 and not any(
            m in title for m in ("supplement", "supplementary", "erratum", "corrigendum", "addendum", "appendix", "retraction")
        ):
            if classification.get("is_non_ratable"):
                return (
                    "RECOVERED_NON_RATABLE_REFERENCE_MOVE_TO_STAGING",
                    {**evidence, "ladder_step": "misclassified_child_non_ratable", "reason": "standalone_reference_not_child"},
                )
            return (
                "RECOVERED_RESEARCH_PAPER_MOVE_TO_STAGING",
                {**evidence, "ladder_step": "misclassified_child_research", "reason": "standalone_paper_not_child"},
            )
        if usability.get("readable"):
            child_type = detect_child_document_type(pdf_path.name) or "supplement"
            return "CHILD_DOCUMENT_TO_PENDING_PARENT", {**evidence, "child_type": child_type, "ladder_step": "valid_child_document"}

    ok = bool(evidence.get("pdf_valid"))
    pdf_reason = str(evidence.get("pdf_reason") or "")
    if not ok and pdf_reason in {"file_too_small", "invalid_pdf_header"}:
        return "DELETE_JUNK", {**evidence, "reason": pdf_reason, "ladder_step": "corrupt_pdf"}

    recovered = dict(evidence.get("recovered_metadata") or {})
    enriched, classification = _enrich_recovered_metadata(pdf_path, recovered)
    evidence = {**evidence, "recovered_metadata": enriched, "classification": classification}

    title = str(enriched.get("title") or "").strip()
    text_len = int(evidence.get("text_length") or 0)
    page_count = int(evidence.get("page_count") or 0)
    file_size = int(evidence.get("file_size") or 0)

    if title and len(title) >= 8:
        if classification.get("is_non_ratable"):
            return (
                "RECOVERED_NON_RATABLE_REFERENCE_MOVE_TO_STAGING",
                {**evidence, "ladder_step": "deterministic_non_ratable", "reason": "metadata_recovered"},
            )
        return (
            "RECOVERED_RESEARCH_PAPER_MOVE_TO_STAGING",
            {**evidence, "ladder_step": "deterministic_research", "reason": "metadata_recovered"},
        )

    legacy_decision, legacy_evidence = _deterministic_decision(pdf_path, hash_index=hash_index)
    if legacy_decision == "DELETE_DUPLICATE":
        return "DELETE_DUPLICATE", {**evidence, **legacy_evidence, "ladder_step": "legacy_deterministic"}
    if legacy_decision == "DELETE_JUNK":
        return "DELETE_JUNK", {**evidence, **legacy_evidence, "ladder_step": "legacy_deterministic"}
    if legacy_decision == "MOVE_TO_STAGING_FOR_PROCESSING":
        return (
            "RECOVERED_RESEARCH_PAPER_MOVE_TO_STAGING",
            {**evidence, **legacy_evidence, "ladder_step": "legacy_deterministic_text"},
        )

    if digest and is_staging_duplicate_of_other(pdf_path, digest):
        return "DELETE_DUPLICATE", {
            **evidence,
            "match": "staging_hash_identical",
            "staging_files": _other_staging_paths_for_hash(digest, exclude=pdf_path),
            "ladder_step": "staging_dupe",
        }

    if file_size >= 5000 and page_count >= 1 and ok:
        return None, {**evidence, "reason": "readable_pdf_metadata_unrecovered", "ladder_step": "needs_model"}

    if text_len < 80 and file_size < 5000:
        return "DELETE_JUNK", {**evidence, "reason": "insufficient_content", "ladder_step": "junk"}

    if not ok:
        return "TECHNICAL_FAILURE_UNRECOVERABLE", {**evidence, "reason": pdf_reason or "invalid_pdf", "ladder_step": "corrupt"}

    return None, {**evidence, "reason": "deterministic_inconclusive", "ladder_step": "needs_model"}


def _build_recovery_prompt(pdf_path: Path, evidence: dict[str, Any]) -> str:
    recovered = evidence.get("recovered_metadata") or {}
    text = (evidence.get("sample_text") or "")[:12000]
    return f"""You are recovering metadata and disposition for a PDF in a research corpus pipeline.

Candidate file: {pdf_path.name}
Evidence JSON (truncated): {json.dumps({k: evidence[k] for k in evidence if k != "rendered_page_images"}, ensure_ascii=False)[:5000]}
Recovered metadata so far: {json.dumps(recovered, ensure_ascii=False)[:2000]}

Extracted text sample:
{text[:10000]}

Classify and decide. For books/manuals/dictionaries/guides use RECOVERED_NON_RATABLE_REFERENCE_MOVE_TO_STAGING.
For journal research articles use RECOVERED_RESEARCH_PAPER_MOVE_TO_STAGING.

Respond JSON only:
{{
  "decision": "<one of RECOVERED_RESEARCH_PAPER_MOVE_TO_STAGING|RECOVERED_NON_RATABLE_REFERENCE_MOVE_TO_STAGING|CHILD_DOCUMENT_TO_PENDING_PARENT|DELETE_DUPLICATE|DELETE_JUNK|TECHNICAL_FAILURE_UNRECOVERABLE>",
  "reason": "<short reason>",
  "recovered_metadata": {{
    "title": "<string or null>",
    "authors": ["..."],
    "year": "<string or null>",
    "journal": "<string or null>",
    "document_type": "<journal_article|book|manual|reference_document|report|thesis|...>",
    "is_non_ratable": <true|false>
  }}
}}
"""


def _parse_model_recovery_response(raw: str, evidence: dict[str, Any], *, model: str) -> tuple[str, dict[str, Any]]:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return "TECHNICAL_FAILURE_UNRECOVERABLE", {**evidence, "reason": "model_unparseable_response", "model": model}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return "TECHNICAL_FAILURE_UNRECOVERABLE", {**evidence, "reason": "model_invalid_json", "model": model}

    decision = str(data.get("decision") or "").strip()
    if decision not in RECOVERY_DECISIONS - {"API_OR_MODEL_UNAVAILABLE", "MODEL_RECOVERY_REQUIRED"}:
        return "TECHNICAL_FAILURE_UNRECOVERABLE", {**evidence, "reason": "model_invalid_decision", "model": model}

    md = data.get("recovered_metadata") or {}
    if isinstance(md, dict) and md.get("title"):
        enriched, classification = _enrich_recovered_metadata(
            Path(evidence.get("path") or ""),
            {**dict(evidence.get("recovered_metadata") or {}), **md},
        )
        evidence = {**evidence, "recovered_metadata": enriched, "classification": classification}

    return decision, {**evidence, "model_reason": data.get("reason"), "model": model, "ladder_step": "model_recovery"}


def _sonnet_recovery(
    pdf_path: Path,
    evidence: dict[str, Any],
    *,
    audit: Any | None = None,
) -> tuple[str, dict[str, Any]]:
    from review_resolution import _extract_text

    text, _ = _extract_text(pdf_path, pages=3)
    evidence = {**evidence, "sample_text": text[:15000]}
    prompt = _build_recovery_prompt(pdf_path, evidence)

    if audit is not None:
        audit.bump("sonnet_calls")
        audit.bump("paid_model_calls")

    try:
        from pipeline_llm import call_anthropic_vision_json, call_llm_escalation, public_vision_available

        raw, usage, model_key = call_llm_escalation(
            "Return JSON only for document recovery classification.",
            prompt,
            max_tokens=800,
        )
        if audit is not None:
            audit.summary["sonnet_input_tokens"] = int(audit.summary.get("sonnet_input_tokens", 0)) + int(
                usage.get("input_tokens", 0) or 0
            )
            audit.summary["sonnet_output_tokens"] = int(audit.summary.get("sonnet_output_tokens", 0)) + int(
                usage.get("output_tokens", 0) or 0
            )
        return _parse_model_recovery_response(raw, evidence, model=model_key)
    except Exception:
        pass

    if not public_vision_available():
        return "API_OR_MODEL_UNAVAILABLE", {**evidence, "reason": "recovery_llm_unavailable"}

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img_path in (evidence.get("rendered_page_images") or [])[:3]:
        try:
            raw_bytes = Path(img_path).read_bytes()
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.standard_b64encode(raw_bytes).decode("ascii"),
                    },
                }
            )
        except OSError:
            continue

    data, usage, model_id = call_anthropic_vision_json(
        system="Return JSON only for document recovery classification.",
        content_blocks=content,
        max_tokens=800,
    )
    if not data:
        return "TECHNICAL_FAILURE_UNRECOVERABLE", {**evidence, "reason": "recovery_vision_failed", **usage}
    raw = json.dumps(data)
    return _parse_model_recovery_response(raw, evidence, model=model_id or config.STAGING_LLM_MODEL)


def _opus_recovery(
    pdf_path: Path,
    evidence: dict[str, Any],
    *,
    audit: Any | None = None,
) -> tuple[str, dict[str, Any]]:
    if not config.ANTHROPIC_API_KEY:
        return "API_OR_MODEL_UNAVAILABLE", {**evidence, "reason": "opus_unavailable_no_api_key"}

    try:
        import anthropic
    except ImportError:
        return "API_OR_MODEL_UNAVAILABLE", {**evidence, "reason": "opus_unavailable_no_sdk"}

    from review_resolution import _extract_text

    text, _ = _extract_text(pdf_path, pages=3)
    evidence = {**evidence, "sample_text": text[:15000]}
    prompt = _build_recovery_prompt(pdf_path, evidence)

    client = anthropic.Anthropic()
    if audit is not None:
        audit.bump("opus_calls")
        audit.bump("paid_model_calls")

    try:
        msg = client.messages.create(
            model=config.OPUS_RESOLUTION_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        usage = getattr(msg, "usage", None)
        if usage and audit is not None:
            audit.summary["opus_input_tokens"] = int(audit.summary.get("opus_input_tokens", 0)) + int(
                getattr(usage, "input_tokens", 0) or 0
            )
            audit.summary["opus_output_tokens"] = int(audit.summary.get("opus_output_tokens", 0)) + int(
                getattr(usage, "output_tokens", 0) or 0
            )
        raw = msg.content[0].text if msg.content else ""
        return _parse_model_recovery_response(raw, evidence, model=config.OPUS_RESOLUTION_MODEL)
    except Exception as e:
        return "TECHNICAL_FAILURE_UNRECOVERABLE", {**evidence, "reason": f"opus_api_error:{e}", "model": config.OPUS_RESOLUTION_MODEL}


def run_recovery_ladder(
    pdf_path: Path,
    *,
    allow_paid_api: bool = False,
    allow_opus: bool = False,
    dry_run: bool = False,
    hash_index: dict | None = None,
    audit: Any | None = None,
    force_model: bool = False,
) -> dict[str, Any]:
    """Run duplicate → deterministic → Sonnet → Opus ladder. Never calls paid APIs unless allow_paid_api."""
    pdf_path = Path(pdf_path)
    evidence = gather_evidence(pdf_path)
    counters = {
        "visual_recovery_attempted": 1 if evidence.get("visual_recovery_attempted") else 0,
        "sonnet_recovery_calls": 0,
        "opus_recovery_calls": 0,
    }

    decision, evidence = _deterministic_recovery_decision(pdf_path, evidence, hash_index=hash_index)

    should_call_model = (
        decision is None
        or decision == "MODEL_RECOVERY_REQUIRED"
        or (
            force_model
            and decision in {"RECOVERED_RESEARCH_PAPER_MOVE_TO_STAGING", "RECOVERED_NON_RATABLE_REFERENCE_MOVE_TO_STAGING"}
        )
    )

    if should_call_model:
        if allow_paid_api and not dry_run:
            decision, evidence = _sonnet_recovery(pdf_path, evidence, audit=audit)
            counters["sonnet_recovery_calls"] = 1
            if decision == "TECHNICAL_FAILURE_UNRECOVERABLE" or evidence.get("model_reason", "").lower().find("uncertain") >= 0:
                if allow_opus:
                    decision, evidence = _opus_recovery(pdf_path, evidence, audit=audit)
                    counters["opus_recovery_calls"] = 1
        else:
            reason = "paid_recovery_not_allowed" if not allow_paid_api else "dry_run_no_paid_calls"
            if evidence.get("pdf_valid") and int(evidence.get("file_size") or 0) >= 3000:
                decision = "MODEL_RECOVERY_REQUIRED"
                evidence = {**evidence, "reason": reason, "ladder_step": "model_gate_blocked"}
            else:
                decision = "TECHNICAL_FAILURE_UNRECOVERABLE"
                evidence = {**evidence, "reason": evidence.get("reason") or reason}

    if decision in {"API_OR_MODEL_UNAVAILABLE"}:
        decision = "MODEL_RECOVERY_REQUIRED"

    evidence["sonnet_attempted"] = counters["sonnet_recovery_calls"] > 0
    evidence["opus_attempted"] = counters["opus_recovery_calls"] > 0
    classification = evidence.get("classification") or {}
    result = {
        "decision": decision,
        "legacy_decision": to_legacy_decision(decision),
        "evidence": evidence,
        "sonnet_attempted": evidence["sonnet_attempted"],
        "opus_attempted": evidence["opus_attempted"],
        "recovered_metadata": evidence.get("recovered_metadata") or {},
        "corpus_class": classification.get("corpus_class"),
        "is_non_ratable": bool(classification.get("is_non_ratable")),
        "dry_run": dry_run,
        **counters,
    }
    return result


def write_recovery_sidecar(pdf_path: Path, result: dict[str, Any]) -> None:
    from review_retry_tracking import write_retry_meta

    meta = {
        "recovery_ladder_at": _now(),
        "recovery_decision": result.get("decision"),
        "model_recovery_required": result.get("decision") == "MODEL_RECOVERY_REQUIRED",
        "recovered_metadata": result.get("recovered_metadata") or {},
        "recovery_evidence": {
            k: result.get("evidence", {}).get(k)
            for k in (
                "reason",
                "ladder_step",
                "visual_recovery_attempted",
                "text_length",
                "page_count",
            )
        },
    }
    write_retry_meta(pdf_path, meta)


def write_reason_sidecar(pdf_path: Path, reason: str, *, status: str = "technical_failure") -> None:
    sidecar = pdf_path.with_suffix(pdf_path.suffix + ".reason.json")
    sidecar.write_text(
        json.dumps({"reason": reason, "updated_at": _now(), "status": status}, indent=2),
        encoding="utf-8",
    )
