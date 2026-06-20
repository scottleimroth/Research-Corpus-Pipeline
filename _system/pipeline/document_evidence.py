"""Central document evidence package for intake, identity, and adjudication readiness."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from document_text_extraction import (
    MIN_RESEARCH_TEXT_CHARS,
    extract_document_text,
    meaningful_text_length,
)

# ---------------------------------------------------------------------------
# Identifier / invalid-title patterns
# ---------------------------------------------------------------------------

PII_PREFIX_RX = re.compile(r"^\s*PII\s*:\s*", re.I)
PII_VALUE_RX = re.compile(r"\bS?\d{4}[-–]\d{4}\(\d{2}\)\d{5}[-–]?\d*\b", re.I)
DOI_RX = re.compile(r"\b10\.\d{4,9}/[^\s\])>\"']+", re.I)
ISSN_RX = re.compile(r"\bISSN[:\s-]*\d{4}[-–]?\d{3,4}[Xx0-9]?\b", re.I)
COPYRIGHT_RX = re.compile(r"©|copyright|\(c\)\s*\d{4}", re.I)
JUNK_TITLE_LITERALS = frozenset(
    {
        "untitled",
        "unknown",
        "no title",
        "document",
        "microsoft word",
        "download",
        "exact paper title",
    }
)
YEAR_JOURNAL_TAIL_RX = re.compile(r"-\d{4}-[A-Za-z]{2,12}$")
TIMESTAMP_SUFFIX_RX = re.compile(r"(?:_\d{7,})+$")


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def extract_pii_from_text(text: str) -> list[str]:
    found: list[str] = []
    for m in PII_VALUE_RX.finditer(text or ""):
        found.append(m.group(0).strip())
    for ln in (text or "").splitlines()[:40]:
        if PII_PREFIX_RX.match(ln):
            val = PII_PREFIX_RX.sub("", ln).strip()
            if val:
                found.append(val)
    return list(dict.fromkeys(found))


def is_invalid_canonical_title(title: str | None) -> tuple[bool, str]:
    """Return (invalid, reason). PII/DOI/ISSN/copyright must not be canonical titles."""
    t = normalize_whitespace(title or "")
    if not t:
        return True, "empty"
    low = t.lower()
    if low in JUNK_TITLE_LITERALS:
        return True, "junk_literal"
    if len(t) < 8:
        # Historical single-word article titles (e.g. Bolton 1894 "Rhythm").
        if len(t) >= 5 and re.fullmatch(r"[A-Za-z][A-Za-z'-]{3,}", t):
            return False, ""
        return True, "too_short"
    if PII_PREFIX_RX.match(t):
        return True, "pii_prefix"
    if PII_VALUE_RX.fullmatch(t.replace(" ", "")) or PII_VALUE_RX.search(t) and len(t) < 40:
        return True, "pii_value"
    if DOI_RX.fullmatch(t) or (t.lower().startswith("doi:") and DOI_RX.search(t)):
        return True, "doi_only"
    if ISSN_RX.search(t) and len(t) < 30:
        return True, "issn_only"
    if COPYRIGHT_RX.search(t) and len(t) < 80:
        return True, "copyright_line"
    if re.match(r"^(vol\.|volume|issue|page|pp\.)\b", low):
        return True, "issue_metadata"
    if re.match(r"^(psychophysiology|biological psychology|neuroscience letters)\b", low) and len(t) < 40:
        if " in " not in low and " of " not in low and ":" not in t:
            return True, "journal_header_only"
    if re.match(r"^[A-Z0-9\s\-–().,/]+$", t) and PII_VALUE_RX.search(t):
        return True, "pii_caps_header"
    if re.search(r"\bunknown\s+pii\b", low):
        return True, "unknown_pii_title"
    if re.search(r"\bpii\s*[s]?\d*$", low):
        return True, "pii_derived_title"
    if re.search(r"\bpii\b", low) and len(t) < 48 and not re.search(r"polygraph|symposium", low):
        return True, "pii_token_in_title"
    if re.match(r"^author\s*\(", low):
        return True, "author_line_not_title"
    if re.match(r"^(international|the)\s+journal\s+of\b", low):
        return True, "journal_header_not_title"
    if re.match(r"^the\s+(?:[a-z]+\s+){0,3}journal\s+of\b", low):
        return True, "journal_header_not_title"
    if re.search(r"\.(pdf|docx?|pptx?)\b", low):
        return True, "filename_extension_artifact"
    if re.search(r"\b\d{1,3}$", low) and len(re.findall(r"[a-zA-Z]{2,}", t)) <= 3 and len(t) < 45:
        return True, "trailing_numeric_fragment"
    last_token = re.findall(r"[A-Za-z]+", t)[-1:] or [""]
    if last_token[0].lower() in {"de", "da", "di", "du", "fo"} and len(t) < 80:
        return True, "dangling_title_fragment"
    return False, ""


def _substantive_title_word_count(title: str) -> int:
    return len(re.findall(r"[A-Za-z][A-Za-z'-]{2,}", title or ""))


def identity_acceptance_problem(identity: dict[str, Any]) -> str | None:
    """Return a rejection reason when bibliographic identity is too weak for corpus insert."""
    title = normalize_whitespace(str(identity.get("title") or ""))
    invalid, reason = is_invalid_canonical_title(title)
    if invalid:
        return reason
    if identity.get("identity_status") == "insufficient_identity":
        from first_pass_finalize import identity_minimally_usable

        if identity_minimally_usable(
            title=title,
            authors=identity.get("authors") or [],
            year=identity.get("year"),
            doi=identity.get("doi") or identity.get("pii"),
            llm_forced=bool(identity.get("llm_forced_first_pass")),
        ):
            return None
        return "insufficient_identity"

    source = str(identity.get("title_source") or "")
    metadata_source = str(identity.get("metadata_source") or "")
    status = str(identity.get("identity_status") or "")
    weak_sources = {
        "filename",
        "filename_human",
        "filename_plain_title",
        "filename_plain_recovery",
        "pdf_embed",
        "first_page_heuristic",
    }
    weak_identity = (
        source.startswith("filename")
        or metadata_source in weak_sources
        or status in {"recovered_from_filename", "recovered_from_pdf_embed", "recovered_from_first_page"}
    )
    has_external_id = bool(identity.get("doi") or identity.get("pii"))
    has_bibliographic_anchor = bool(identity.get("year") or identity.get("journal"))
    word_count = _substantive_title_word_count(title)

    if weak_identity and not has_external_id:
        if word_count < 4:
            return "weak_identity_title_fragment"
        if not has_bibliographic_anchor and word_count < 5:
            return "weak_identity_lacks_bibliographic_anchor"
    return None


def recover_title_from_filename(path: Path | str) -> dict[str, Any]:
    """General filename title recovery with confidence and explanation."""
    from metadata_extractors import extract_filename_human

    path = Path(path)
    stem = TIMESTAMP_SUFFIX_RX.sub("", path.stem).strip()
    stem = re.sub(r"(?:_\d{7,})+", " ", stem).strip()

    data = extract_filename_human(path)
    title = normalize_whitespace(str(data.get("title") or ""))
    year = data.get("year")
    authors = list(data.get("authors") or [])

    if title:
        title = YEAR_JOURNAL_TAIL_RX.sub("", title).strip()
        title = re.sub(r"-\d{4}$", "", title).strip()

    invalid, reason = is_invalid_canonical_title(title)
    if invalid or not title:
        plain = normalize_whitespace(re.sub(r"[_\s]+", " ", stem))
        if " - " in plain:
            plain = plain.split(" - ", 1)[-1].strip()
        elif "-" in plain and re.match(r"^[A-Za-z]+-", plain):
            plain = plain.split("-", 1)[-1].strip()
        plain = YEAR_JOURNAL_TAIL_RX.sub("", plain).strip()
        if plain and not is_invalid_canonical_title(plain)[0] and len(plain) >= 12:
            title = plain
            invalid = False
            reason = ""
            data["source"] = "filename_plain_recovery"

    if not title or is_invalid_canonical_title(title)[0]:
        return {
            "title": None,
            "authors": authors,
            "year": year,
            "source": "filename_recovery_failed",
            "confidence": 0.0,
            "explanation": reason or "no_valid_filename_title",
            "rejected": True,
        }

    conf = float(data.get("confidence") or 0.55)
    return {
        "title": title,
        "authors": authors,
        "year": year,
        "source": str(data.get("source") or "filename_human"),
        "confidence": conf,
        "explanation": f"recovered_from_{path.name}",
        "rejected": False,
    }


def collect_title_candidates(
    path: Path,
    *,
    first_pages: str,
    tier_meta: dict[str, Any],
    tiers_log: list[tuple],
) -> list[dict[str, Any]]:
    """Gather title candidates from tiers and filename recovery."""
    candidates: list[dict[str, Any]] = []

    def _add(title: Any, source: str, confidence: float, extra: dict | None = None) -> None:
        t = normalize_whitespace(str(title or ""))
        if not t:
            return
        invalid, reason = is_invalid_canonical_title(t)
        candidates.append(
            {
                "title": t,
                "source": source,
                "confidence": confidence,
                "rejected": invalid,
                "rejection_reason": reason if invalid else None,
                **(extra or {}),
            }
        )

    _add(
        tier_meta.get("title"),
        "metadata_chain",
        0.7,
        {"tiers_log": tiers_log, "metadata_source": tier_meta.get("source")},
    )
    for tier_name, _, fired in tiers_log:
        if not fired:
            continue
    fn_rec = recover_title_from_filename(path)
    if fn_rec.get("title"):
        _add(fn_rec["title"], fn_rec.get("source") or "filename_human", float(fn_rec.get("confidence") or 0.55))

    from metadata_extractors import extract_first_page_heuristic, extract_crossref, extract_pdf_embed

    for source, fn, conf in (
        ("crossref", lambda: extract_crossref(path, first_pages, tier_meta.get("doi")), 0.95),
        ("pdf_embed", lambda: extract_pdf_embed(path, first_pages, tier_meta.get("doi")), 0.6),
        ("first_page_heuristic", lambda: extract_first_page_heuristic(path, first_pages, tier_meta.get("doi")), 0.45),
    ):
        try:
            data = fn() or {}
            _add(data.get("title"), source, conf)
        except Exception:
            pass

    # dedupe by normalized title keeping highest confidence non-rejected
    best: dict[str, dict] = {}
    for c in candidates:
        key = c["title"].lower()
        prev = best.get(key)
        if not prev or (not c.get("rejected") and prev.get("rejected")) or c.get("confidence", 0) > prev.get("confidence", 0):
            best[key] = c
    return list(best.values())


def select_identity(
    candidates: list[dict[str, Any]],
    *,
    path: Path,
    tier_meta: dict[str, Any],
    first_pages: str,
) -> dict[str, Any]:
    """Choose canonical identity; never accept invalid title if alternative exists."""
    valid = [c for c in candidates if not c.get("rejected")]
    rejected = [c for c in candidates if c.get("rejected")]

    chosen = None
    if valid:
        valid.sort(key=lambda c: float(c.get("confidence") or 0), reverse=True)
        chosen = valid[0]

    fn_rec = recover_title_from_filename(path)
    authors = list(tier_meta.get("authors") or fn_rec.get("authors") or [])
    year = tier_meta.get("year") or fn_rec.get("year")
    doi = tier_meta.get("doi")
    pii_list = extract_pii_from_text(first_pages)

    if not chosen and fn_rec.get("title") and not fn_rec.get("rejected"):
        chosen = {
            "title": fn_rec["title"],
            "source": fn_rec.get("source") or "filename_human",
            "confidence": fn_rec.get("confidence") or 0.55,
        }

    if not chosen:
        return {
            "title": None,
            "authors": authors,
            "year": year,
            "journal": tier_meta.get("journal"),
            "doi": doi,
            "pii": pii_list[0] if pii_list else None,
            "title_source": None,
            "title_confidence": 0.0,
            "identity_status": "insufficient_identity",
            "rejected_title_candidates": rejected,
        }

    if not authors and fn_rec.get("authors"):
        authors = fn_rec["authors"]
    if not year and fn_rec.get("year"):
        year = fn_rec["year"]

    status = "high_confidence"
    src = str(chosen.get("source") or "")
    metadata_source = str(chosen.get("metadata_source") or tier_meta.get("source") or "")
    effective_source = metadata_source if src == "metadata_chain" and metadata_source else src
    if effective_source.startswith("filename"):
        status = "recovered_from_filename"
    elif effective_source == "first_page_heuristic":
        status = "recovered_from_first_page"
    elif effective_source == "pdf_embed":
        status = "recovered_from_pdf_embed"
    elif effective_source == "crossref":
        status = "recovered_from_crossref"
    elif effective_source in {"claude", "ollama"}:
        status = f"recovered_from_{effective_source}"
    elif float(chosen.get("confidence") or 0) < 0.5:
        status = "insufficient_identity"

    return {
        "title": chosen["title"],
        "authors": authors,
        "year": year,
        "journal": tier_meta.get("journal"),
        "doi": doi,
        "pii": pii_list[0] if pii_list else None,
        "title_source": src,
        "metadata_source": metadata_source,
        "title_confidence": float(chosen.get("confidence") or 0),
        "identity_status": status,
        "rejected_title_candidates": rejected,
    }


def score_journal_article_signals(
    *,
    text: str,
    filename: str,
    identity: dict[str, Any],
    page_count: int | None,
) -> dict[str, Any]:
    low = (text or "").lower()
    fn_low = filename.lower()
    signals: dict[str, Any] = {
        "journal_article_signals": 0,
        "empirical_signals": 0,
        "reference_material_signals": 0,
        "child_support_signals": 0,
        "insufficient_evidence_signals": 0,
    }

    section_hits = sum(1 for s in ("abstract", "introduction", "method", "result", "discussion", "reference") if s in low)
    if section_hits >= 2:
        signals["journal_article_signals"] += 2
    if section_hits >= 4:
        signals["empirical_signals"] += 2
    if identity.get("doi") or identity.get("pii"):
        signals["journal_article_signals"] += 1
    if page_count and page_count >= 3:
        signals["journal_article_signals"] += 1
    if re.search(r"\b(participants|subjects|n\s*=\s*\d+|experiment|study)\b", low):
        signals["empirical_signals"] += 1
    if re.search(r"\b(handbook|manual|dictionary|standard|protocol|guideline|tutorial)\b", fn_low):
        signals["reference_material_signals"] += 2
    if re.search(r"\b(book|chapter|atlas)\b", fn_low) and "review paper" not in fn_low:
        signals["reference_material_signals"] += 1
    if meaningful_text_length(text) < MIN_RESEARCH_TEXT_CHARS:
        signals["insufficient_evidence_signals"] += 1
    if identity.get("identity_status") == "insufficient_identity":
        signals["insufficient_evidence_signals"] += 2
    return signals


def check_article_type_guard(
    *,
    proposed_rating: str | None,
    document_type: str | None,
    article_signals: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, Any]:
    """Block NOT_RATABLE / NOT_APPLICABLE when journal article signals dominate."""
    rating = (proposed_rating or "").lower().replace("-", "_")
    ja = int(article_signals.get("journal_article_signals") or 0)
    ref = int(article_signals.get("reference_material_signals") or 0)
    insuf = int(article_signals.get("insufficient_evidence_signals") or 0)

    likely_journal = ja >= 2 and ref <= 1
    bad_rating = rating in {
        "not_ratable_reference_material",
        "not_applicable",
        "not applicable",
    }

    if likely_journal and bad_rating:
        return {
            "allowed": False,
            "action": "insufficient_evidence",
            "reason": "journal_article_signals_conflict_with_non_ratable",
            "model_classification": rating,
            "article_signals": article_signals,
            "corrected_action": "requires_reevaluation",
        }
    if rating == "not_applicable" and insuf >= 1 and ja >= 1:
        return {
            "allowed": False,
            "action": "identity_or_extraction_problem",
            "reason": "not_applicable_used_as_metadata_fallback",
            "model_classification": rating,
            "article_signals": article_signals,
            "corrected_action": "requires_reevaluation",
        }
    return {"allowed": True, "action": "accept", "reason": "ok"}


def check_evaluation_input_health(
    evidence: dict[str, Any],
) -> dict[str, Any]:
    from evidence_coverage import assess_evidence_coverage

    ext = evidence.get("extraction") or {}
    ident = evidence.get("selected_identity") or {}
    sig = evidence.get("article_type_evidence") or {}
    cov = evidence.get("coverage") or {}
    title = ident.get("title") or ""
    invalid, treason = is_invalid_canonical_title(title)

    if not cov:
        cov = assess_evidence_coverage(
            raw_text=str(ext.get("text") or ""),
            sections={},
            extraction=ext,
            article_signals=sig,
            identity=ident,
            document_type=evidence.get("document_type"),
            filename=str((evidence.get("file_identity") or {}).get("filename") or ""),
        )

    health = {
        "identity_status": ident.get("identity_status"),
        "title_quality": "invalid" if invalid else "ok",
        "title_reject_reason": treason if invalid else None,
        "article_type_confidence": "low" if sig.get("insufficient_evidence_signals") else "ok",
        "full_text_length": ext.get("text_length") or cov.get("raw_text_length") or 0,
        "extraction_source": ext.get("extraction_source"),
        "extraction_status": ext.get("extractor_status"),
        "coverage_status": cov.get("coverage_status"),
        "journal_article_signals": sig.get("journal_article_signals") or 0,
        "reference_material_signals": sig.get("reference_material_signals") or 0,
        "can_evaluate": True,
        "can_red_team_skip": False,
        "reasons": [],
    }

    if invalid:
        health["can_evaluate"] = False
        health["reasons"].append("invalid_title")
    if ext.get("extractor_status") not in ("TEXT_OK",) and meaningful_text_length(ext.get("text") or "") < MIN_RESEARCH_TEXT_CHARS:
        health["can_evaluate"] = False
        health["reasons"].append("insufficient_text")
    if ident.get("identity_status") == "insufficient_identity":
        first_pass = bool(evidence.get("first_pass_finalize"))
        if first_pass:
            from first_pass_finalize import identity_minimally_usable

            if identity_minimally_usable(
                title=str(ident.get("title") or ""),
                authors=ident.get("authors") or [],
                year=ident.get("year"),
                doi=ident.get("doi"),
                llm_forced=True,
            ):
                health["reasons"].append("first_pass_identity_recovered")
            else:
                health["can_evaluate"] = False
                health["reasons"].append("insufficient_identity")
        else:
            health["can_evaluate"] = False
            health["reasons"].append("insufficient_identity")
    if cov.get("likely_journal_article") and not cov.get("evidence_can_support_rating"):
        first_pass = bool(evidence.get("first_pass_finalize"))
        raw_len = int(cov.get("raw_text_length") or ext.get("text_length") or 0)
        ident_ok = str(ident.get("identity_status") or "") not in {
            "",
            "insufficient_identity",
        }
        if first_pass and raw_len >= 2000 and ident_ok:
            health["reasons"].append(
                f"first_pass_partial_coverage:{cov.get('coverage_status')}"
            )
        else:
            health["can_evaluate"] = False
            health["reasons"].append(f"insufficient_coverage:{cov.get('coverage_status')}")
    if (sig.get("journal_article_signals") or 0) >= 2:
        health["can_red_team_skip"] = False
    if cov.get("likely_journal_article"):
        health["can_red_team_skip"] = False
    return health


# ---------------------------------------------------------------------------
# Central acceptance gate — single source of truth for DB/source/eval permissions
# ---------------------------------------------------------------------------

ACCEPT_RESEARCH = "ACCEPT_RESEARCH"
ACCEPT_REFERENCE = "ACCEPT_REFERENCE"
LINK_CHILD = "LINK_CHILD"
HOLD_PENDING_CHILD = "HOLD_PENDING_CHILD"
REJECT_INSUFFICIENT_EVIDENCE = "REJECT_INSUFFICIENT_EVIDENCE"
REJECT_NEEDS_RECOVERY = "REJECT_NEEDS_RECOVERY"
REJECT_CORRUPT_OR_UNUSABLE = "REJECT_CORRUPT_OR_UNUSABLE"
REJECT_JUNK = "REJECT_JUNK"
REJECT_DUPLICATE = "REJECT_DUPLICATE"
SYSTEM_INTEGRITY_FAILURE = "SYSTEM_INTEGRITY_FAILURE"


@dataclass
class AcceptanceDecision:
    action: str
    can_insert_db: bool
    can_copy_to_source: bool
    can_evaluate: bool
    can_save_not_applicable: bool
    can_skip_red_team: bool
    reason: str
    required_next_action: str | None = None
    audit_fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _reject_decision(
    action: str,
    reason: str,
    *,
    required_next_action: str | None = None,
    audit_fields: dict[str, Any] | None = None,
) -> AcceptanceDecision:
    return AcceptanceDecision(
        action=action,
        can_insert_db=False,
        can_copy_to_source=False,
        can_evaluate=False,
        can_save_not_applicable=False,
        can_skip_red_team=False,
        reason=reason,
        required_next_action=required_next_action,
        audit_fields=audit_fields or {},
    )


def decide_acceptance_from_evidence(
    evidence: EvidencePackage | dict[str, Any],
    *,
    document_type: str | None = None,
    non_ratable_preclassification: bool = False,
    is_confirmed_duplicate: bool = False,
    child_link_context: dict[str, Any] | None = None,
) -> AcceptanceDecision:
    """Single acceptance gate from EvidencePackage fields — must pass before DB/source commit."""
    from evidence_coverage import (
        COVERAGE_COMPLETE,
        COVERAGE_EXTRACTION_FAILED,
        COVERAGE_INSUFFICIENT,
        COVERAGE_OCR_FAILED,
        COVERAGE_OCR_REQUIRED,
        COVERAGE_PARTIAL_RECOVERY,
        COVERAGE_PARTIAL_USABLE,
        COVERAGE_SECTION_PARSE_FAILED,
    )

    data = evidence.to_dict() if isinstance(evidence, EvidencePackage) else evidence
    cov = data.get("coverage") or {}
    ident = data.get("selected_identity") or {}
    ext = data.get("extraction") or {}
    child_link_context = child_link_context or {}

    audit = {
        "coverage_status": cov.get("coverage_status"),
        "likely_journal_article": cov.get("likely_journal_article"),
        "evidence_can_support_rating": cov.get("evidence_can_support_rating"),
        "evidence_can_support_not_applicable": cov.get("evidence_can_support_not_applicable"),
        "identity_status": ident.get("identity_status"),
        "extractor_status": ext.get("extractor_status"),
    }

    if is_confirmed_duplicate:
        return _reject_decision(REJECT_DUPLICATE, "confirmed_duplicate", audit_fields=audit)

    if child_link_context.get("is_child"):
        if child_link_context.get("parent_exists"):
            return AcceptanceDecision(
                action=LINK_CHILD,
                can_insert_db=True,
                can_copy_to_source=True,
                can_evaluate=False,
                can_save_not_applicable=False,
                can_skip_red_team=True,
                reason="child_link_to_parent",
                audit_fields=audit,
            )
        return AcceptanceDecision(
            action=HOLD_PENDING_CHILD,
            can_insert_db=False,
            can_copy_to_source=True,
            can_evaluate=False,
            can_save_not_applicable=False,
            can_skip_red_team=True,
            reason="child_pending_parent",
            audit_fields=audit,
        )

    title = ident.get("title") or ""
    identity_problem = identity_acceptance_problem(ident)
    if identity_problem:
        return _reject_decision(
            REJECT_INSUFFICIENT_EVIDENCE,
            f"identity:{identity_problem}",
            audit_fields=audit,
        )

    extractor_status = str(ext.get("extractor_status") or "TEXT_OK")
    raw_len = meaningful_text_length(ext.get("text") or "") or int(cov.get("raw_text_length") or 0)
    if extractor_status == "OCR_FAILED":
        return _reject_decision(
            REJECT_NEEDS_RECOVERY,
            "extraction:OCR_FAILED",
            required_next_action=cov.get("required_recovery_action") or "ocr_retry_or_manual",
            audit_fields=audit,
        )
    if extractor_status in {"OCR_REQUIRED"}:
        return _reject_decision(
            REJECT_NEEDS_RECOVERY,
            "extraction:OCR_REQUIRED",
            required_next_action="ocr_extraction",
            audit_fields=audit,
        )
    if extractor_status not in {"TEXT_OK", ""} and raw_len < MIN_RESEARCH_TEXT_CHARS:
        return _reject_decision(
            REJECT_CORRUPT_OR_UNUSABLE,
            f"extraction:{extractor_status}",
            audit_fields=audit,
        )
    if raw_len < MIN_RESEARCH_TEXT_CHARS and not non_ratable_preclassification:
        return _reject_decision(
            REJECT_INSUFFICIENT_EVIDENCE,
            f"text_below_minimum:{raw_len}",
            audit_fields=audit,
        )

    likely_journal = bool(cov.get("likely_journal_article"))
    can_rate = bool(cov.get("evidence_can_support_rating"))
    can_na = bool(cov.get("evidence_can_support_not_applicable"))
    can_ref = bool(cov.get("evidence_can_support_reference_material"))
    status = str(cov.get("coverage_status") or "")
    doc_type = str(document_type or data.get("document_type") or "").lower()
    ref_doc = doc_type in {
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

    if likely_journal and not can_rate and not can_na and not (non_ratable_preclassification and ref_doc):
        recovery_statuses = {
            COVERAGE_PARTIAL_RECOVERY,
            COVERAGE_OCR_REQUIRED,
            COVERAGE_OCR_FAILED,
            COVERAGE_SECTION_PARSE_FAILED,
        }
        action = REJECT_NEEDS_RECOVERY if status in recovery_statuses else REJECT_INSUFFICIENT_EVIDENCE
        return _reject_decision(
            action,
            f"coverage:{status}",
            required_next_action=cov.get("required_recovery_action"),
            audit_fields=audit,
        )

    if status in {COVERAGE_PARTIAL_RECOVERY, COVERAGE_INSUFFICIENT, COVERAGE_EXTRACTION_FAILED}:
        if (likely_journal and not (non_ratable_preclassification and ref_doc)) or not non_ratable_preclassification:
            action = REJECT_NEEDS_RECOVERY if status == COVERAGE_PARTIAL_RECOVERY else REJECT_INSUFFICIENT_EVIDENCE
            return _reject_decision(
                action,
                f"coverage:{status}",
                required_next_action=cov.get("required_recovery_action"),
                audit_fields=audit,
            )

    if non_ratable_preclassification or ref_doc:
        if can_ref and (not likely_journal or can_na):
            return AcceptanceDecision(
                action=ACCEPT_REFERENCE,
                can_insert_db=True,
                can_copy_to_source=True,
                can_evaluate=False,
                can_save_not_applicable=True,
                can_skip_red_team=True,
                reason="reference_material_evidence",
                audit_fields=audit,
            )
        if likely_journal and not can_ref:
            return _reject_decision(
                REJECT_INSUFFICIENT_EVIDENCE,
                "journal_article_lacks_reference_evidence",
                audit_fields=audit,
            )

    if can_rate and status in {COVERAGE_COMPLETE, COVERAGE_PARTIAL_USABLE}:
        return AcceptanceDecision(
            action=ACCEPT_RESEARCH,
            can_insert_db=True,
            can_copy_to_source=True,
            can_evaluate=True,
            can_save_not_applicable=can_na and not likely_journal,
            can_skip_red_team=False,
            reason="research_evidence_sufficient",
            audit_fields=audit,
        )

    return _reject_decision(
        REJECT_INSUFFICIENT_EVIDENCE,
        f"coverage:{status or 'unknown'}",
        required_next_action=cov.get("required_recovery_action"),
        audit_fields=audit,
    )


def decide_acceptance_from_paper(
    paper: dict[str, Any],
    *,
    coverage: dict[str, Any] | None = None,
) -> AcceptanceDecision:
    """Acceptance gate from a DB paper row.

    Pass a precomputed ``coverage`` to avoid re-assessing (and re-loading full
    text) when the caller already has it — this is a per-row hot path.
    """
    from document_corpus_classification import is_non_ratable_document_type
    from evidence_coverage import assess_evidence_coverage_from_paper

    cov = coverage if coverage is not None else assess_evidence_coverage_from_paper(paper)
    bm = paper.get("basic_metadata") or {}
    fi = paper.get("file_info") or {}
    doc_type = paper.get("document_type")
    if isinstance(doc_type, dict):
        doc_type = doc_type.get("type")

    ids = paper.get("identifiers") or {}
    return decide_acceptance_from_evidence(
        {
            "coverage": cov,
            "selected_identity": {
                "title": bm.get("title"),
                "authors": bm.get("authors") or [],
                "year": bm.get("year"),
                "doi": ids.get("doi") or bm.get("doi"),
                "identity_status": bm.get("identity_status") or "high_confidence",
            },
            "extraction": {
                "extractor_status": cov.get("extractor_status") or "TEXT_OK",
                "text_length": cov.get("raw_text_length") or 0,
            },
            "article_type_evidence": cov.get("article_type_evidence") or {},
            "document_type": doc_type,
            "file_identity": {"filename": fi.get("renamed_filename") or fi.get("original_filename")},
        },
        document_type=str(doc_type or ""),
        non_ratable_preclassification=is_non_ratable_document_type(doc_type),
    )


def format_acceptance_diagnostic(decision: AcceptanceDecision | dict[str, Any]) -> str:
    d = decision if isinstance(decision, dict) else decision.to_dict()
    return (
        f"acceptance={d.get('action')} "
        f"can_insert={d.get('can_insert_db')} "
        f"can_copy={d.get('can_copy_to_source')} "
        f"can_evaluate={d.get('can_evaluate')} "
        f"reason={d.get('reason')}"
    )


@dataclass
class EvidencePackage:
    file_identity: dict[str, Any] = field(default_factory=dict)
    extraction: dict[str, Any] = field(default_factory=dict)
    metadata_candidates: dict[str, Any] = field(default_factory=dict)
    selected_identity: dict[str, Any] = field(default_factory=dict)
    article_type_evidence: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    adjudication_readiness: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_evidence_package(
    path: Path | str,
    *,
    args: dict[str, Any] | None = None,
    attempt_ocr: bool = True,
) -> EvidencePackage:
    """Build one evidence package before DB insert."""
    from metadata_extractors import extract_metadata

    path = Path(path)
    args = args or {}
    digest = None
    size = None
    if path.exists():
        try:
            h = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            digest = h.hexdigest()
            size = path.stat().st_size
        except OSError:
            pass

    ext_result = extract_document_text(path, attempt_ocr=attempt_ocr)
    first_pages = (ext_result.text or "")[:12000]

    tier_order = ["crossref", "pdf_embed", "filename_human", "first_page_heuristic", "filename"]
    if args.get("no_claude"):
        pass
    tier_meta = extract_metadata(str(path), first_pages, doi=args.get("doi"), tier_order=tier_order, verbose=False)

    candidates = collect_title_candidates(path, first_pages=first_pages, tier_meta=tier_meta, tiers_log=tier_meta.get("tiers_log") or [])
    identity = select_identity(candidates, path=path, tier_meta=tier_meta, first_pages=first_pages)

    if args.get("title"):
        identity["title"] = args["title"]
        identity["title_source"] = "cli_override"
        identity["identity_status"] = "high_confidence"

    article_signals = score_journal_article_signals(
        text=ext_result.text,
        filename=path.name,
        identity=identity,
        page_count=ext_result.page_count,
    )

    from evidence_coverage import assess_evidence_coverage

    coverage = assess_evidence_coverage(
        raw_text=ext_result.text,
        sections={"full": ext_result.text},
        extraction=ext_result.to_dict(),
        article_signals=article_signals,
        identity=identity,
        document_type=args.get("document_type"),
        filename=path.name,
    )

    acceptance = decide_acceptance_from_evidence(
        {
            "coverage": coverage,
            "selected_identity": identity,
            "extraction": ext_result.to_dict(),
            "article_type_evidence": article_signals,
            "document_type": args.get("document_type"),
            "file_identity": {"filename": path.name},
        },
        document_type=args.get("document_type"),
        non_ratable_preclassification=bool(args.get("non_ratable_preclassification")),
    )

    readiness = {
        "can_insert_db": acceptance.can_insert_db,
        "can_copy_to_source": acceptance.can_copy_to_source,
        "can_evaluate": acceptance.can_evaluate,
        "can_save_not_applicable": acceptance.can_save_not_applicable,
        "can_skip_red_team": acceptance.can_skip_red_team,
        "acceptance_action": acceptance.action,
        "acceptance_reason": acceptance.reason,
        "requires_recovery": acceptance.action == REJECT_NEEDS_RECOVERY,
        "requires_model_adjudication": identity.get("identity_status") == "insufficient_identity",
        "reason": acceptance.reason if acceptance.can_insert_db else acceptance.reason,
    }

    pkg = EvidencePackage(
        file_identity={
            "original_path": str(path),
            "current_path": str(path),
            "filename": path.name,
            "sha256": digest,
            "size_bytes": size,
            "detected_content_type": ext_result.content_type,
            "is_actual_pdf": ext_result.content_type == "pdf",
            "page_count": ext_result.page_count,
        },
        extraction=ext_result.to_dict(),
        metadata_candidates={"titles": candidates, "tier_meta": tier_meta},
        selected_identity=identity,
        article_type_evidence=article_signals,
        coverage=coverage,
        adjudication_readiness=readiness,
    )
    eval_health = check_evaluation_input_health({**pkg.to_dict(), "coverage": coverage})
    pkg.adjudication_readiness["can_evaluate"] = acceptance.can_evaluate and eval_health["can_evaluate"]
    pkg.adjudication_readiness["evaluation_health"] = eval_health
    pkg.adjudication_readiness["coverage_status"] = coverage.get("coverage_status")
    pkg.adjudication_readiness["acceptance"] = acceptance.to_dict()
    return pkg


def format_identity_diagnostic(pkg: EvidencePackage | dict[str, Any]) -> str:
    data = pkg.to_dict() if isinstance(pkg, EvidencePackage) else pkg
    ident = data.get("selected_identity") or {}
    rej = ident.get("rejected_title_candidates") or []
    rej_s = ", ".join(f"{r.get('title','')[:30]}({r.get('rejection_reason')})" for r in rej[:3])
    return (
        f"identity_status={ident.get('identity_status')} "
        f"title={str(ident.get('title') or '')[:60]!r} "
        f"source={ident.get('title_source')} "
        f"rejected=[{rej_s}] "
        f"can_insert={data.get('adjudication_readiness', {}).get('can_insert_db')}"
    )


def diagnose_file_no_write(path: Path | str, *, attempt_ocr: bool = False) -> dict[str, Any]:
    pkg = build_evidence_package(path, attempt_ocr=attempt_ocr)
    out = pkg.to_dict()
    out["diagnostic_line"] = format_identity_diagnostic(pkg)
    return out
