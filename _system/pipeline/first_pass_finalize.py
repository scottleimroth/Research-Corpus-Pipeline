"""First-pass only: enrich identity, recover text, prepare for eval — nothing deferred."""

from __future__ import annotations

import base64
import io
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import config
import papers_db


def _pdf_vision_inputs(path: Path, *, max_pages: int = 2) -> list[dict[str, str]]:
    inputs: list[dict[str, str]] = []
    try:
        import pypdfium2 as pdfium
    except Exception:
        return inputs
    doc = None
    try:
        doc = pdfium.PdfDocument(str(path))
        for idx in range(min(max_pages, len(doc))):
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


def _authors_look_poisoned(authors: list[Any] | None) -> bool:
    if not authors:
        return True
    cleaned = [str(a).strip() for a in authors if str(a).strip()]
    if not cleaned:
        return True
    if len(cleaned) == 1 and len(cleaned[0]) <= 2:
        return True
    if all(len(a) <= 2 for a in cleaned):
        return True
    return False


def _year_looks_poisoned(year: Any, doi: str | None, filename: str) -> bool:
    y = str(year or "").strip()
    if not y or not y.isdigit():
        return True
    yi = int(y)
    if yi < 1950 or yi > 2035:
        return True
    if doi and y in doi.replace("/", "").replace("-", ""):
        return True
    stem = Path(filename).stem.lower()
    if re.match(r"^s\d{4,6}", stem) and y.startswith("007"):
        return True
    return False


def identity_minimally_usable(
    *,
    title: str,
    authors: list[Any] | None,
    year: Any,
    doi: str | None = None,
    llm_forced: bool = False,
) -> bool:
    """Enough bibliographic signal to rate during first-pass (staging must finish)."""
    t = str(title or "").strip()
    if len(t) < 12:
        return False
    auth = [str(a).strip() for a in (authors or []) if len(str(a).strip()) > 2]
    if not auth:
        return False
    y = str(year or "").strip()
    if y.isdigit() and 1950 <= int(y) <= 2035:
        return True
    d = str(doi or "").strip()
    if d and len(d) >= 8:
        return True
    # Preprints/SSRN often lack DOI/year in metadata but have title + authors after vision.
    if llm_forced and len(t) >= 20 and len(auth) >= 1:
        return True
    return False


def identity_needs_enrichment(
    *,
    title: str,
    authors: list[Any],
    year: Any,
    doi: str | None,
    filename: str,
    identity_status: str | None,
    llm_forced: bool = False,
) -> bool:
    if llm_forced:
        return True
    if str(identity_status or "") in {"insufficient_identity", "recovered_from_filename"}:
        return True
    if doi and (_authors_look_poisoned(authors) or _year_looks_poisoned(year, doi, filename)):
        return True
    return False


def _merge_meta(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key in ("title", "year", "journal", "doi", "abstract", "volume", "issue", "pages"):
        val = patch.get(key)
        if val is not None and str(val).strip():
            out[key] = val
    if patch.get("authors"):
        out["authors"] = list(patch["authors"])
    src = patch.get("source") or patch.get("metadata_source")
    if src:
        out["metadata_source"] = src
        out["source"] = src
    return out


def enrich_identity_from_crossref(
    path: Path,
    *,
    doi: str | None,
    first_pages: str,
) -> dict[str, Any]:
    if not doi:
        return {}
    from metadata_extractors import extract_crossref

    data = extract_crossref(path, first_pages, doi=doi) or {}
    if data:
        data["identity_status"] = "recovered_from_crossref"
    return data


def enrich_identity_from_vision(path: Path) -> dict[str, Any]:
    from pipeline_llm import public_vision_available

    if not public_vision_available():
        return {}
    vision_inputs = _pdf_vision_inputs(path, max_pages=2)
    if not vision_inputs:
        return {}

    from pipeline_llm import call_anthropic_vision_json

    system = (
        "Extract bibliographic metadata from document page images. "
        "Return JSON only with keys: title, authors (array of full names), year (4-digit string), "
        "journal, doi, document_kind (journal_article|book|book_chapter|conference_abstract|"
        "supplement|junk_other), confidence (0-1), reason (short)."
    )
    user = json.dumps(
        {
            "task": "first_pass_identity_enrichment",
            "filename": path.name,
            "required_json_schema": {
                "title": "string",
                "authors": ["string"],
                "year": "string",
                "journal": "string",
                "doi": "string or null",
                "document_kind": "string",
                "confidence": "number",
                "reason": "string",
            },
        },
        ensure_ascii=False,
    )
    blocks: list[dict[str, Any]] = [{"type": "text", "text": user}]
    for img in vision_inputs:
        data = str(img.get("data") or "")
        if not data:
            continue
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": str(img.get("media_type") or "image/png"),
                    "data": data,
                },
            }
        )
    parsed, usage, model = call_anthropic_vision_json(
        system=system,
        content_blocks=blocks,
        max_tokens=900,
    )
    if (
        parsed
        and float(parsed.get("confidence") or 0) < 0.90
        and config.ANTHROPIC_API_KEY
        and str(config.CORPUS_API_MODE or "").lower() != "openrouter"
    ):
        opus_parsed, opus_usage, opus_model = call_anthropic_vision_json(
            system=system,
            content_blocks=blocks,
            max_tokens=900,
            models=(config.OPUS_RESOLUTION_MODEL,),
        )
        if opus_parsed and float(opus_parsed.get("confidence") or 0) >= float(
            parsed.get("confidence") or 0
        ):
            parsed, usage, model = opus_parsed, opus_usage, opus_model
    if not parsed:
        return {"vision_error": usage}
    authors = parsed.get("authors") or []
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(";") if a.strip()]
    return {
        "title": parsed.get("title"),
        "authors": authors,
        "year": parsed.get("year"),
        "journal": parsed.get("journal"),
        "doi": parsed.get("doi"),
        "document_kind": parsed.get("document_kind"),
        "confidence": float(parsed.get("confidence") or 0.0),
        "source": f"vision:{model or 'anthropic'}",
        "identity_status": "recovered_from_vision",
        "vision_usage": usage,
    }


def enrich_identity_first_pass(
    path: Path,
    *,
    title: str,
    authors: list[Any],
    year: Any,
    journal: str,
    doi: str | None,
    first_pages_text: str,
    identity_status: str | None = None,
    llm_forced: bool = False,
) -> dict[str, Any]:
    """Return updated title, authors, year, journal, doi, identity_status, enrichment_log."""
    filename = path.name
    if not identity_needs_enrichment(
        title=title,
        authors=authors,
        year=year,
        doi=doi,
        filename=filename,
        identity_status=identity_status,
        llm_forced=llm_forced,
    ):
        return {
            "title": title,
            "authors": authors,
            "year": year,
            "journal": journal,
            "doi": doi,
            "identity_status": identity_status or "high_confidence",
            "enrichment_log": [],
            "changed": False,
        }

    log: list[str] = []
    meta = {
        "title": title,
        "authors": list(authors or []),
        "year": year,
        "journal": journal,
        "doi": doi,
    }
    doi_use = doi
    if not doi_use and first_pages_text:
        from add_paper import doi_from_pdf_text

        doi_use = doi_from_pdf_text(first_pages_text[:12000])
        if doi_use:
            meta["doi"] = doi_use
            log.append(f"doi_from_text:{doi_use}")

    cr = enrich_identity_from_crossref(path, doi=doi_use, first_pages=first_pages_text)
    if cr:
        meta = _merge_meta(meta, cr)
        log.append("crossref")

    if identity_needs_enrichment(
        title=str(meta.get("title") or ""),
        authors=meta.get("authors") or [],
        year=meta.get("year"),
        doi=meta.get("doi"),
        filename=filename,
        identity_status=meta.get("identity_status"),
        llm_forced=False,
    ):
        vis = enrich_identity_from_vision(path)
        if vis.get("title") or vis.get("authors"):
            meta = _merge_meta(meta, vis)
            log.append(str(vis.get("source") or "vision"))
        elif vis.get("vision_error"):
            log.append("vision_failed")

    status = str(meta.get("identity_status") or "high_confidence")
    if _authors_look_poisoned(meta.get("authors")) or _year_looks_poisoned(
        meta.get("year"), meta.get("doi"), filename
    ):
        status = "insufficient_identity"
    elif status == "insufficient_identity":
        status = "recovered_from_crossref" if "crossref" in log else "recovered_from_vision"

    # First-pass ingest must not strand rows: if enrichment produced usable metadata, allow eval.
    if llm_forced and identity_minimally_usable(
        title=str(meta.get("title") or ""),
        authors=meta.get("authors") or [],
        year=meta.get("year"),
        doi=meta.get("doi"),
        llm_forced=True,
    ):
        if status == "insufficient_identity":
            if "crossref" in log:
                status = "recovered_from_crossref"
            elif any(str(x).startswith("vision") for x in log):
                status = "recovered_from_vision"
            else:
                status = "recovered_first_pass"
        elif not status or status in {"recovered_from_filename"}:
            status = "high_confidence"

    changed = (
        str(meta.get("title") or "") != str(title or "")
        or list(meta.get("authors") or []) != list(authors or [])
        or str(meta.get("year") or "") != str(year or "")
        or str(meta.get("doi") or "") != str(doi or "")
    )
    if log:
        auth = meta.get("authors") or []
        lead = auth[0] if auth else "?"
        print(
            f"  Identity enriched ({', '.join(log)}): {lead} | {meta.get('year')} | {str(meta.get('title') or '')[:60]}",
            flush=True,
        )
    return {
        "title": str(meta.get("title") or title or ""),
        "authors": meta.get("authors") or authors or [],
        "year": meta.get("year") or year or "",
        "journal": meta.get("journal") or journal or "",
        "doi": meta.get("doi") or doi,
        "abstract": meta.get("abstract") or "",
        "identity_status": status,
        "enrichment_log": log,
        "changed": changed,
    }


def _reextract_full_text(pdf_path: Path) -> tuple[str, dict[str, Any]]:
    from add_paper import extract_full_text
    from document_text_extraction import meaningful_text_length

    text = extract_full_text(str(pdf_path), attempt_ocr=True, use_cache=False)
    from add_paper import LAST_INGEST_EXTRACTION

    extraction = dict(LAST_INGEST_EXTRACTION or {})
    return text, extraction


def apply_enriched_fields_to_paper(paper: dict[str, Any], enriched: dict[str, Any]) -> None:
    bm = paper.setdefault("basic_metadata", {})
    if enriched.get("title"):
        bm["title"] = enriched["title"]
    if enriched.get("authors"):
        bm["authors"] = enriched["authors"]
    if enriched.get("year"):
        bm["year"] = enriched["year"]
    if enriched.get("journal"):
        bm["journal"] = enriched["journal"]
    ids = paper.setdefault("identifiers", {})
    if enriched.get("doi"):
        ids["doi"] = enriched["doi"]
        bm["doi"] = enriched["doi"]
    if enriched.get("abstract"):
        ak = paper.setdefault("abstract_keywords", {})
        ak["abstract"] = enriched["abstract"]
    bm["identity_status"] = enriched.get("identity_status") or bm.get("identity_status")
    nf = paper.setdefault("notes_and_flags", {})
    nf["first_pass_identity_enriched"] = True
    if enriched.get("enrichment_log"):
        nf["identity_enrichment_log"] = ",".join(enriched["enrichment_log"])


def run_first_pass_finalize(
    paper_id: str,
    pdf_path: Path,
    *,
    llm_forced: bool = False,
    force_ocr: bool = False,
) -> dict[str, Any]:
    """
    On first meeting: fix identity, OCR-recover text if needed, persist before eval.
    Never leaves enrichment for a later batch.
    """
    result: dict[str, Any] = {"paper_id": paper_id, "steps": []}
    paper = papers_db.get_paper(paper_id)
    if not paper:
        result["ok"] = False
        result["reason"] = "paper_not_found"
        return result

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        fi = paper.get("file_info") or {}
        rel = str(fi.get("filepath") or "")
        if rel:
            from config import CORPUS_STORE, SOURCE_PDFS

            candidate = CORPUS_STORE / rel.replace("\\", "/").lstrip("/")
            if not candidate.exists():
                candidate = SOURCE_PDFS / str(fi.get("renamed_filename") or "")
            if candidate.exists():
                pdf_path = candidate

    bm = paper.get("basic_metadata") or {}
    ft = paper.get("full_text") or {}
    from evidence_coverage import assess_evidence_coverage_from_paper, resolve_raw_full_text
    from document_text_extraction import MIN_RESEARCH_TEXT_CHARS, meaningful_text_length

    raw = resolve_raw_full_text(ft)
    text_len = meaningful_text_length(raw)
    coverage = assess_evidence_coverage_from_paper(paper)
    cov_status = str(coverage.get("coverage_status") or "")

    needs_ocr = force_ocr or text_len < MIN_RESEARCH_TEXT_CHARS or cov_status in {
        "PARTIAL_NEEDS_RECOVERY",
        "INSUFFICIENT_FOR_EVALUATION",
    }
    if needs_ocr and pdf_path.exists():
        print(f"  First-pass text recovery (OCR) for {paper_id}...", flush=True)
        full_text, extraction = _reextract_full_text(pdf_path)
        from add_paper import parse_sections

        sections = parse_sections(full_text) if full_text else {"full": full_text or ""}
        paper["full_text"] = {
            **(ft if isinstance(ft, dict) else {}),
            "raw_full_text": full_text,
            "sections": sections,
            "extraction_method": extraction.get("extraction_source") or "ocr",
            "total_chars": len(full_text or ""),
            "extraction": extraction,
        }
        papers_db.save_paper(paper)
        result["steps"].append(f"ocr:{meaningful_text_length(full_text)}")
        coverage = assess_evidence_coverage_from_paper(paper)

    first_pages = raw[:12000] if raw else ""
    if paper.get("full_text"):
        from evidence_coverage import resolve_raw_full_text as _rr

        first_pages = (_rr(paper.get("full_text")) or "")[:12000]

    ids = paper.get("identifiers") or {}
    enriched = enrich_identity_first_pass(
        pdf_path,
        title=str(bm.get("title") or ""),
        authors=bm.get("authors") or [],
        year=bm.get("year"),
        journal=str(bm.get("journal") or ""),
        doi=ids.get("doi") or bm.get("doi"),
        first_pages_text=first_pages,
        identity_status=bm.get("identity_status"),
        llm_forced=llm_forced,
    )
    apply_enriched_fields_to_paper(paper, enriched)
    if llm_forced and identity_minimally_usable(
        title=str((paper.get("basic_metadata") or {}).get("title") or ""),
        authors=(paper.get("basic_metadata") or {}).get("authors") or [],
        year=(paper.get("basic_metadata") or {}).get("year"),
        doi=(paper.get("identifiers") or {}).get("doi"),
        llm_forced=True,
    ):
        bm2 = paper.setdefault("basic_metadata", {})
        if str(bm2.get("identity_status") or "") == "insufficient_identity":
            bm2["identity_status"] = enriched.get("identity_status") or "recovered_first_pass"
    papers_db.save_paper(paper)
    if enriched.get("changed"):
        result["steps"].append("identity")
    result["coverage"] = assess_evidence_coverage_from_paper(paper)
    result["ok"] = True
    return result


def run_until_evaluation_complete(
    paper_id: str,
    pdf_path: Path,
    client: Any,
    *,
    non_ratable: bool,
    force_research_eval: bool,
    llm_forced: bool,
    allow_paid_api: bool,
    sidecar_flags: dict[str, Any] | None = None,
    max_attempts: int = 6,
    stop_requested: Callable[[], bool] | None = None,
    max_seconds: int | None = None,
) -> dict[str, Any]:
    """Block until rating+red_team (or non-ratable classification) is complete."""
    from eval_completion import is_ingest_evaluation_complete
    from eval_model_config import build_eval_escalation_ladder
    from evaluate_paper import evaluate_one
    from document_corpus_classification import apply_non_ratable_reference_classification

    sidecar_flags = sidecar_flags or {}
    last_reason = ""
    started = time.monotonic()
    eval_ladder = build_eval_escalation_ladder(profile=config.CORPUS_PROFILE)

    if non_ratable:
        apply_non_ratable_reference_classification(
            paper_id,
            document_type=sidecar_flags.get("document_type"),
            reasons=sidecar_flags.get("classification_reasons") or [],
            title=str(sidecar_flags.get("title") or ""),
        )
        ok, reason = is_ingest_evaluation_complete(papers_db.get_paper(paper_id) or {})
        return {
            "ok": ok,
            "reason": reason,
            "attempts": 0,
            "prep": {"skipped": "non_ratable_fast_path"},
            "non_ratable": True,
        }

    for attempt in range(1, max_attempts + 1):
        if stop_requested and stop_requested():
            return {"ok": False, "reason": "STOP_REQUESTED", "attempts": attempt - 1}
        if max_seconds is not None and time.monotonic() - started > max_seconds:
            return {"ok": False, "reason": "first_pass_finalize_timeout", "attempts": attempt - 1}

        prep = run_first_pass_finalize(
            paper_id,
            pdf_path,
            llm_forced=llm_forced,
            force_ocr=(attempt > 1),
        )
        paper = papers_db.get_paper(paper_id) or {}
        coverage = prep.get("coverage") or {}

        if not eval_ladder:
            return {
                "ok": False,
                "reason": "no_eval_model_available",
                "attempts": attempt,
                "prep": prep,
            }

        if coverage and coverage.get("evidence_can_support_rating") is False:
            cov_status = str(coverage.get("coverage_status") or "")
            if cov_status == "INSUFFICIENT_FOR_EVALUATION":
                return {
                    "ok": False,
                    "reason": f"coverage_insufficient_after_recovery:{cov_status}",
                    "attempts": attempt,
                    "prep": prep,
                }

        eval_kwargs: dict[str, Any] = {
            "force": attempt > 1,
            "require_research_rating": force_research_eval or llm_forced,
            "allow_blocked_eval": True,
            "first_pass_finalize": True,
            "min_text_chars": 200,
        }
        if attempt >= max_attempts - 1:
            eval_kwargs["start_key"] = "opus-4"
        eval_result = evaluate_one(client, paper_id, **eval_kwargs)
        if stop_requested and stop_requested():
            return {"ok": False, "reason": "STOP_REQUESTED", "attempts": attempt, "prep": prep}
        eval_ok = bool(eval_result.get("ok")) if isinstance(eval_result, dict) else bool(eval_result)
        paper = papers_db.get_paper(paper_id) or {}
        complete, reason = is_ingest_evaluation_complete(paper)
        last_reason = reason
        if complete and eval_ok:
            return {
                "ok": True,
                "reason": reason,
                "attempts": attempt,
                "prep": prep,
                "eval_usage": eval_result if isinstance(eval_result, dict) else {},
            }
        print(
            f"  First-pass eval attempt {attempt}/{max_attempts} incomplete: {reason}",
            flush=True,
        )

    return {
        "ok": False,
        "reason": last_reason or "evaluation_incomplete",
        "attempts": max_attempts,
    }
