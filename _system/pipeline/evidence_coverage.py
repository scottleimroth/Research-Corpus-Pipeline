"""Evidence coverage assessment — gate evaluation and classification acceptance."""

from __future__ import annotations

import re
from typing import Any

from document_text_extraction import MIN_RESEARCH_TEXT_CHARS, meaningful_text_length

MIN_JOURNAL_EVAL_CHARS = 8000
MIN_PARTIAL_USABLE_CHARS = 2000

COVERAGE_COMPLETE = "COMPLETE_ENOUGH"
COVERAGE_PARTIAL_USABLE = "PARTIAL_BUT_USABLE"
COVERAGE_PARTIAL_RECOVERY = "PARTIAL_NEEDS_RECOVERY"
COVERAGE_EXTRACTION_FAILED = "EXTRACTION_FAILED"
COVERAGE_OCR_REQUIRED = "OCR_REQUIRED"
COVERAGE_OCR_FAILED = "OCR_FAILED"
COVERAGE_SECTION_PARSE_FAILED = "SECTION_PARSE_FAILED"
COVERAGE_INSUFFICIENT = "INSUFFICIENT_FOR_EVALUATION"

_SECTION_SIGNAL_RX = {
    "abstract": re.compile(r"\babstract\b", re.I),
    "introduction": re.compile(r"\bintroduction\b", re.I),
    "methods": re.compile(r"\b(?:materials?\s+and\s+)?methods?\b", re.I),
    "results": re.compile(r"\bresults?\b", re.I),
    "discussion": re.compile(r"\bdiscussion\b", re.I),
    "references": re.compile(r"\breferences\b", re.I),
}

_REFERENCE_DOC_TYPES = frozenset(
    {
        "reference_material",
        "manual",
        "handbook",
        "standard",
        "book",
        "book_chapter",
        "conference_abstract",
        "conference_proceedings",
        "poster_or_abstract",
        "thesis",
        "protocol",
    }
)


def resolve_raw_full_text(full_text: dict[str, Any] | None) -> str:
    """Return preserved raw body; never collapse to a single parsed section."""
    if not isinstance(full_text, dict):
        return ""
    for key in ("raw_full_text", "raw_text", "text", "full"):
        val = str(full_text.get(key) or "").strip()
        if len(val) >= MIN_RESEARCH_TEXT_CHARS:
            return val
    sections = full_text.get("sections") or {}
    full_block = str(sections.get("full") or sections.get("Full") or "").strip()
    if len(full_block) >= MIN_RESEARCH_TEXT_CHARS:
        return full_block
    structured = {
        k: v
        for k, v in sections.items()
        if str(k).lower() != "full" and v and str(v).strip()
    }
    if len(structured) == 1 and full_block:
        return full_block
    if structured:
        joined = "\n\n".join(str(v).strip() for v in structured.values() if str(v).strip())
        if len(joined) >= MIN_RESEARCH_TEXT_CHARS:
            return joined
    return full_block or val if (val := str(full_text.get("text") or "")).strip() else ""


def _section_names(sections: dict[str, Any]) -> list[str]:
    return sorted(k for k in sections if str(k).lower() != "full" and sections.get(k))


def _text_signals(text: str) -> dict[str, bool]:
    sample = text or ""
    return {name: bool(rx.search(sample)) for name, rx in _SECTION_SIGNAL_RX.items()}


def _is_likely_journal_article(
    *,
    article_signals: dict[str, Any],
    identity: dict[str, Any],
    document_type: str | None,
    filename: str = "",
) -> bool:
    ja = int(article_signals.get("journal_article_signals") or 0)
    ref = int(article_signals.get("reference_material_signals") or 0)
    if ja >= 2 and ref <= 1:
        return True
    doc = str(document_type or "").lower()
    if doc in {"journal_article", "empirical", "review", "meta_analysis"} and ja >= 1:
        return True
    fn = filename.lower()
    if re.search(r"\b(journal|psychophysiology|neuroscience|biological psychology)\b", fn):
        return ja >= 1
    title = str(identity.get("title") or "")
    if title and ja >= 1 and ref == 0:
        return True
    return False


def assess_evidence_coverage(
    *,
    raw_text: str,
    sections: dict[str, Any] | None = None,
    extraction: dict[str, Any] | None = None,
    article_signals: dict[str, Any] | None = None,
    identity: dict[str, Any] | None = None,
    document_type: str | None = None,
    filename: str = "",
) -> dict[str, Any]:
    """Assess whether stored evidence can support rating or non-ratable classification."""
    sections = sections or {}
    extraction = extraction or {}
    article_signals = article_signals or {}
    identity = identity or {}

    raw_len = meaningful_text_length(raw_text)
    normalized_len = len(re.sub(r"\s+", " ", (raw_text or "").strip()))
    page_count = extraction.get("page_count")
    if page_count is None:
        page_count = (extraction.get("file_identity") or {}).get("page_count")
    extraction_source = extraction.get("extraction_source") or extraction.get("method") or "unknown"
    extractor_status = str(extraction.get("extractor_status") or "TEXT_OK")

    section_names = _section_names(sections)
    section_count = len(section_names)
    only_single_section = section_count == 1
    signals = _text_signals(raw_text)

    likely_journal = _is_likely_journal_article(
        article_signals=article_signals,
        identity=identity,
        document_type=document_type,
        filename=filename,
    )
    is_reference_doc = str(document_type or "").lower() in _REFERENCE_DOC_TYPES or int(
        article_signals.get("reference_material_signals") or 0
    ) >= 2

    warnings: list[str] = []
    required_recovery_action: str | None = None

    if extractor_status in {"OCR_REQUIRED"}:
        status = COVERAGE_OCR_REQUIRED
        required_recovery_action = "ocr_extraction"
    elif extractor_status in {"OCR_FAILED"}:
        status = COVERAGE_OCR_FAILED
        required_recovery_action = "ocr_retry_or_manual"
    elif extractor_status not in {"TEXT_OK", ""} and raw_len < MIN_RESEARCH_TEXT_CHARS:
        status = COVERAGE_EXTRACTION_FAILED
        required_recovery_action = "re_extract"
    elif raw_len < MIN_RESEARCH_TEXT_CHARS:
        status = COVERAGE_INSUFFICIENT
        required_recovery_action = "re_extract_or_ocr"
    elif likely_journal and only_single_section and raw_len < MIN_JOURNAL_EVAL_CHARS:
        status = COVERAGE_INSUFFICIENT
        warnings.append("only_single_parsed_section_for_journal_article")
        required_recovery_action = "use_raw_full_text_or_re_extract"
    elif likely_journal and raw_len < MIN_JOURNAL_EVAL_CHARS:
        if page_count and int(page_count) > 1:
            status = COVERAGE_PARTIAL_RECOVERY
            warnings.append("multi_page_low_text_yield")
            required_recovery_action = "ocr_or_full_re_extract"
        else:
            status = COVERAGE_INSUFFICIENT
            warnings.append("journal_article_below_eval_char_threshold")
            required_recovery_action = "re_extract_or_verify_pdf"
    elif likely_journal and raw_len >= MIN_JOURNAL_EVAL_CHARS and section_count >= 2:
        status = COVERAGE_COMPLETE
    elif likely_journal and raw_len >= MIN_PARTIAL_USABLE_CHARS:
        status = COVERAGE_PARTIAL_USABLE
        if section_count < 2:
            warnings.append("sparse_section_parse_journal_article")
    elif raw_len >= MIN_JOURNAL_EVAL_CHARS:
        status = COVERAGE_COMPLETE
    elif raw_len >= MIN_PARTIAL_USABLE_CHARS:
        status = COVERAGE_PARTIAL_USABLE
    else:
        status = COVERAGE_INSUFFICIENT

    if section_count == 0 and raw_len >= MIN_RESEARCH_TEXT_CHARS and likely_journal:
        warnings.append("section_parse_found_no_headings")
        if status == COVERAGE_COMPLETE:
            status = COVERAGE_PARTIAL_USABLE

    evidence_can_support_rating = status in {COVERAGE_COMPLETE, COVERAGE_PARTIAL_USABLE} and (
        not likely_journal or raw_len >= MIN_PARTIAL_USABLE_CHARS
    )
    evidence_can_support_not_applicable = (
        not likely_journal or is_reference_doc
    ) and status not in {COVERAGE_EXTRACTION_FAILED, COVERAGE_OCR_REQUIRED, COVERAGE_OCR_FAILED}
    evidence_can_support_reference_material = is_reference_doc or int(
        article_signals.get("reference_material_signals") or 0
    ) >= 2

    if likely_journal and raw_len < MIN_JOURNAL_EVAL_CHARS and not is_reference_doc:
        evidence_can_support_not_applicable = False
        evidence_can_support_rating = False

    return {
        "coverage_status": status,
        "raw_text_length": raw_len,
        "normalized_text_length": normalized_len,
        "page_count": page_count,
        "extraction_source": extraction_source,
        "extractor_status": extractor_status,
        "section_count": section_count,
        "section_names": section_names,
        "has_title_signal": bool(identity.get("title")),
        "has_abstract_signal": signals.get("abstract", False),
        "has_intro_signal": signals.get("introduction", False),
        "has_methods_signal": signals.get("methods", False),
        "has_results_signal": signals.get("results", False),
        "has_discussion_signal": signals.get("discussion", False),
        "has_references_signal": signals.get("references", False),
        "only_single_section": only_single_section,
        "likely_journal_article": likely_journal,
        "evidence_can_support_rating": evidence_can_support_rating,
        "evidence_can_support_not_applicable": evidence_can_support_not_applicable,
        "evidence_can_support_reference_material": evidence_can_support_reference_material,
        "required_recovery_action": required_recovery_action,
        "warnings": warnings,
    }


def assess_evidence_coverage_from_paper(paper: dict[str, Any]) -> dict[str, Any]:
    """Assess coverage from a DB paper row."""
    from document_evidence import score_journal_article_signals

    ft = paper.get("full_text") or {}
    if not ft and paper.get("id"):
        import papers_db

        ft = papers_db.load_fulltext(paper["id"]) or {}

    extraction = ft.get("extraction") or {}
    if not extraction:
        extraction = {
            "extraction_source": ft.get("extraction_method"),
            "extractor_status": "TEXT_OK" if (ft.get("total_chars") or 0) >= MIN_RESEARCH_TEXT_CHARS else "UNKNOWN",
            "page_count": ft.get("page_count"),
        }

    raw = resolve_raw_full_text(ft)
    bm = paper.get("basic_metadata") or {}
    fi = paper.get("file_info") or {}
    ids = paper.get("identifiers") or {}
    doc_type = paper.get("document_type")
    if isinstance(doc_type, dict):
        doc_type = doc_type.get("type")

    article_signals = score_journal_article_signals(
        text=raw,
        filename=str(fi.get("original_filename") or fi.get("renamed_filename") or ""),
        identity={
            "title": bm.get("title"),
            "doi": ids.get("doi") or bm.get("doi"),
            "pii": ids.get("pii"),
            "identity_status": bm.get("identity_status"),
        },
        page_count=extraction.get("page_count") or ft.get("page_count"),
    )

    cov = assess_evidence_coverage(
        raw_text=raw,
        sections=ft.get("sections") or {},
        extraction=extraction,
        article_signals=article_signals,
        identity={"title": bm.get("title"), "identity_status": bm.get("identity_status")},
        document_type=str(doc_type or ""),
        filename=str(fi.get("renamed_filename") or ""),
    )
    cov["article_type_evidence"] = article_signals
    return cov


def format_coverage_diagnostic(coverage: dict[str, Any]) -> str:
    return (
        f"coverage={coverage.get('coverage_status')} "
        f"raw_len={coverage.get('raw_text_length')} "
        f"sections={coverage.get('section_count')} "
        f"only_single={coverage.get('only_single_section')} "
        f"likely_journal={coverage.get('likely_journal_article')} "
        f"can_rate={coverage.get('evidence_can_support_rating')} "
        f"can_na={coverage.get('evidence_can_support_not_applicable')}"
    )
