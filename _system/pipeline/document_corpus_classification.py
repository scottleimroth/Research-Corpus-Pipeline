"""Classify staged/review documents as research papers vs valid non-ratable corpus material."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from corpus_prescan import classify_document_type

NON_RATABLE_DOCUMENT_TYPES = frozenset({
    "book",
    "edited_volume",
    "book_chapter",
    "manual",
    "product_manual",
    "dictionary",
    "reference_document",
    "report",
    "thesis",
    "poster_or_abstract",
    "conference_abstract",
    "conference_proceedings",
    "handbook",
    "non_research_reference",
    "supplement",
    "supplementary_material",
    "corrigendum",
    "erratum",
    "correction",
})

RESEARCH_DOCUMENT_TYPES = frozenset({
    "journal_article",
    "review_article",
    "protocol",
})

REFERENCE_FILENAME_MARKERS = (
    "for dummies",
    "dictionary",
    "handbook",
    "manual",
    "product manual",
    "user guide",
    "reference",
    "encyclopedia",
    "companion to",
    "introduction to",
    "principles and practice",
    "conditioned reflexes",
    "mismeasure of man",
    "motivation and action",
    "handbook of emotions",
    "dream dictionary",
    "wiley",
    "norton",
    "springer",
    "routledge",
    "cambridge",
    "oxford",
    "isbn",
    "nato asi",
    "current topics",
    "current top",
    "edited by",
    "(editor",
    "lecture",
    "course",
    "slides",
    "technical report",
    "white paper",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def classify_corpus_document(
    filename: str,
    *,
    title: str = "",
    recovered: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return corpus disposition hints for resolver/ingest."""
    recovered = recovered or {}
    title = title or str(recovered.get("title") or "")
    combined = _norm(f"{filename} {title}")

    prescan_type, prescan_reasons = classify_document_type(filename)
    document_type = prescan_type
    reasons = list(prescan_reasons)

    if any(m in combined for m in ("dictionary", "for dummies", "encyclopedia")):
        document_type = "dictionary"
        reasons.append("dictionary/reference title or filename marker")
    elif any(m in combined for m in ("manual", "user guide", "product manual")):
        document_type = "manual"
        reasons.append("manual/product-manual marker")
    elif "handbook" in combined:
        document_type = "handbook"
        reasons.append("handbook marker")
    elif any(m in combined for m in ("report", "technical report", "white paper")):
        document_type = "report"
        reasons.append("report marker")
    elif document_type in {"book", "edited_volume", "book_chapter"}:
        reasons.append("book/edited-volume prescan classification")
    elif document_type == "thesis":
        reasons.append("thesis/dissertation marker")
    elif document_type == "poster_or_abstract":
        document_type = "reference_document"
        reasons.append("poster/abstract treated as non-ratable reference")

    if document_type in RESEARCH_DOCUMENT_TYPES:
        corpus_class = "research_paper"
        is_non_ratable = False
    elif document_type in NON_RATABLE_DOCUMENT_TYPES or any(m in combined for m in REFERENCE_FILENAME_MARKERS):
        corpus_class = "non_ratable_reference_material"
        is_non_ratable = True
        if document_type in {"other_or_unknown", "journal_article"} and is_non_ratable:
            if any(m in combined for m in REFERENCE_FILENAME_MARKERS):
                document_type = "reference_document"
    else:
        corpus_class = "research_paper"
        is_non_ratable = False

    rating = "not_ratable_reference_material" if is_non_ratable else None
    justification = ""
    if is_non_ratable:
        label = document_type.replace("_", " ")
        justification = (
            f"This item is classified as {label} / non-research reference corpus material. "
            f"It is retained for background and reference use but does not receive a research-paper "
            f"quality rating (landmark/strong/adequate/weak/flawed). "
            f"Classification reasons: {'; '.join(reasons[:5])}."
        )

    return {
        "corpus_class": corpus_class,
        "document_type": document_type,
        "is_non_ratable": is_non_ratable,
        "expected_rating": rating,
        "classification_reasons": reasons,
        "rating_justification": justification,
    }


def enrich_recovered_metadata(recovered: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
    """Merge classification hints into sidecar/recovered metadata for staging ingest."""
    out = dict(recovered)
    out["document_type"] = classification.get("document_type")
    out["corpus_class"] = classification.get("corpus_class")
    out["is_non_ratable"] = bool(classification.get("is_non_ratable"))
    if classification.get("expected_rating"):
        out["expected_rating"] = classification["expected_rating"]
    if classification.get("rating_justification"):
        out["non_ratable_justification"] = classification["rating_justification"]
    out["classification_reasons"] = classification.get("classification_reasons") or []
    return out


def apply_non_ratable_reference_classification(
    paper_id: str,
    *,
    document_type: str | None = None,
    reasons: list[str] | None = None,
    title: str = "",
) -> dict[str, Any]:
    """Apply complete non-ratable classification without Sonnet (deterministic path)."""
    import papers_db

    paper = papers_db.get_paper(paper_id)
    if not paper:
        return {"ok": False, "error": "paper_not_found"}

    doc_type = document_type or paper.get("document_type") or "reference_document"
    reason_list = reasons or []
    bm = paper.get("basic_metadata") or {}
    display_title = title or bm.get("title") or paper_id
    justification = (
        f"Non-ratable reference material ({doc_type.replace('_', ' ')}): '{display_title}'. "
        "Retained in corpus for background/reference use; research-paper quality rating not applicable. "
    )
    if reason_list:
        justification += "Reasons: " + "; ".join(reason_list[:6]) + "."
    else:
        justification += "Classified from document type and filename/metadata heuristics."

    cl = paper.get("classification") or {}
    if not isinstance(cl, dict):
        cl = {}
    cl.update(
        {
            "rating": "not_ratable_reference_material",
            "rating_justification": justification,
            "red_team_notes": "Skipped — non-ratable reference document (deterministic classification).",
            "red_team_survival": None,
            "rating_date": _now(),
            "key_strengths": cl.get("key_strengths") or [],
            "key_weaknesses": cl.get("key_weaknesses") or [],
            "standards_violated": [],
            "non_ratable": True,
            "corpus_class": "non_ratable_reference_material",
        }
    )
    paper["document_type"] = doc_type
    paper["classification"] = cl
    tags = paper.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    for tag in ("non-ratable-reference", "reference-material"):
        if tag not in tags:
            tags.append(tag)
    paper["tags"] = tags

    if not paper.get("full_text"):
        existing = papers_db.get_paper(paper_id)
        if existing and existing.get("full_text"):
            paper["full_text"] = existing["full_text"]

    papers_db.save_paper(paper)
    return {"ok": True, "paper_id": paper_id, "rating": "not_ratable_reference_material", "document_type": doc_type}


def is_non_ratable_document_type(document_type: str | None) -> bool:
    if not document_type:
        return False
    normalized = str(document_type).strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in NON_RATABLE_DOCUMENT_TYPES


def detect_limited_publication_item(
    *,
    filename: str = "",
    title: str = "",
    text: str = "",
    page_count: int | None = None,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Detect abstracts/proceedings snippets that should be retained but not rated."""
    coverage = coverage or {}
    sample = _norm(f"{filename} {title} {text[:12000]}")
    doi_count = len(re.findall(r"\bdoi\s*:\s*10\.\d{4,9}/\S+", text or "", flags=re.I))
    reasons: list[str] = []

    if "abstracts /" in sample or "conference abstracts" in sample:
        reasons.append("abstracts/proceedings page marker")
    if doi_count >= 2:
        reasons.append(f"multiple DOI-marked abstracts on one page ({doi_count})")
    if page_count is not None:
        try:
            if int(page_count) <= 2:
                reasons.append(f"short document/page count ({int(page_count)})")
        except (TypeError, ValueError):
            pass
    if coverage.get("coverage_status") == "INSUFFICIENT_FOR_EVALUATION":
        reasons.append("coverage insufficient for research-paper rating")

    is_limited = (
        bool(reasons)
        and (
            "abstracts/proceedings page marker" in reasons
            or doi_count >= 2
            or "conference abstract" in sample
        )
        and coverage.get("evidence_can_support_rating") is False
    )
    return {
        "is_limited_publication_item": is_limited,
        "document_type": "conference_abstract" if is_limited else "",
        "corpus_class": "non_ratable_reference_material" if is_limited else "",
        "classification_reasons": reasons,
    }
