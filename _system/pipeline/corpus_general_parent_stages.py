"""Generalized read-only book/edited-volume parent stages (G + validation).

Not Koren/Harris-specific. Safe for simulation and future live parent pilots.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

import config
from corpus_stage_d_preview import _read_pdf_metadata, _run_pdftotext
from corpus_stage_g_preview import (
    _bounded_unique,
    _clean_lines,
    _detect_chapter_boundaries,
    _detect_toc,
    _extract_identifiers,
    _extract_year_candidates,
    _guideline_assessment,
    _norm,
    _safe_id_preview,
    _text_fingerprint,
)

EDITOR_RE = re.compile(r"\b(edited by|editors?|contributors?)\b", re.I)
HANDBOOK_RE = re.compile(r"\b(handbook|companion|oxford handbooks|cambridge handbooks)\b", re.I)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
TITLE_STOPWORDS = {"the", "of", "and", "a", "an", "to", "in", "for"}
BAD_TITLE_FRAGMENTS = {"theory research", "applications", "references", "introduction"}
HONORIFICS = {"Professor", "Prof", "Dr"}


def _ascii_token(s: str) -> str:
    normalized = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^A-Za-z0-9]", "", normalized.encode("ascii", "ignore").decode("ascii"))


def _compact_ocr_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _norm(s))


def _title_tokens(title: str | None) -> list[str]:
    if not title:
        return []
    return [
        token
        for token in re.findall(r"[a-z0-9]+", _norm(title))
        if len(token) >= 4 and token not in TITLE_STOPWORDS
    ]


def _filename_editor_names(name: str) -> list[str]:
    stem = Path(name).stem
    stem = re.sub(r"^\([^)]*\)\s*", "", stem)
    stem = re.sub(r"\((?:editor|contributor)[^)]*\)", "", stem, flags=re.I)
    stem = re.sub(r"\([^)]*$", "", stem).strip()
    parts = [p.strip(" ,._-") for p in re.split(r",| and |&", stem) if p.strip(" ,._-")]
    names: list[str] = []
    pending_first: str | None = None
    for part in parts:
        words = [
            w
            for w in re.findall(r"[A-Z][a-z]+", part)
            if w.lower() not in {"editor", "contributor", "professor", "prof", "dr"}
        ]
        if len(words) >= 2:
            names.append(" ".join(words[:3]))
            pending_first = None
        elif len(words) == 1:
            if pending_first:
                names.append(f"{pending_first} {words[0]}")
                pending_first = None
            else:
                pending_first = words[0]
    return _bounded_unique(names, 6)


def _front_matter_profile(name: str, lines: list[str]) -> dict[str, Any]:
    front = lines[:500]
    compact_lines = [_compact_ocr_text(line) for line in front]
    joined = "\n".join(front)
    compact_joined = _compact_ocr_text(joined)
    title_candidates: list[str] = []
    if "musicandemotiontheoryresearchapplications" in compact_joined:
        title_candidates.append("Music and Emotion: Theory, Research, Applications")
    if "oxfordhandbookofmusicpsychology" in compact_joined or (
        "oxfordhandbook" in compact_joined and "musicpsychology" in compact_joined
    ):
        title_candidates.append("The Oxford Handbook of Music Psychology")
    for i, line in enumerate(front[:160]):
        compact = compact_lines[i]
        if len(compact) < 8:
            continue
        low = _norm(line)
        if any(skip in low for skip in ("isbn", "copyright", "contents", "references")):
            continue
        if any(marker in compact for marker in ("handbook", "companion", "synchronization")):
            title_candidates.append(line)
    publisher = None
    series = None
    for line in front[:220]:
        norm = _norm(line)
        if "oxford university press" in norm:
            publisher = "Oxford University Press"
        elif "cambridge university press" in norm:
            publisher = "Cambridge University Press"
        elif "routledge" in norm:
            publisher = "Routledge"
        if "cambridge nonlinear science series" in norm:
            series = "Cambridge Nonlinear Science Series"
        elif "oxford handbook" in norm:
            series = "Oxford Handbooks"
    return {
        "title_candidates": _bounded_unique(title_candidates, 6),
        "filename_editors_or_authors": _filename_editor_names(name),
        "publisher": publisher,
        "series": series,
        "front_matter_compact_fingerprint": hashlib.sha256(compact_joined[:5000].encode("utf-8")).hexdigest()[:20],
    }


def _is_bad_title_fragment(title: str | None) -> bool:
    norm = _norm(title)
    return not norm or norm in BAD_TITLE_FRAGMENTS or norm.endswith(" research")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _infer_general_parent_metadata(
    *,
    name: str,
    stage_e_item: dict[str, Any] | None,
    text: str,
    lines: list[str],
    ids: dict[str, Any],
) -> dict[str, Any]:
    stage_e = stage_e_item or {}
    meta = stage_e.get("metadata_resolution") or {}
    doc_res = stage_e.get("document_type_resolution") or {}
    doc_type = doc_res.get("resolved_document_type") or meta.get("document_type") or "book"
    front_profile = _front_matter_profile(name, lines)

    title = meta.get("title")
    if _is_bad_title_fragment(title) and front_profile["title_candidates"]:
        title = front_profile["title_candidates"][0]
    if not title:
        for line in lines[:400]:
            if len(line) < 12 or len(line) > 160:
                continue
            low = line.lower()
            if any(skip in low for skip in ("isbn", "doi", "copyright", "contents", "www.")):
                continue
            if HANDBOOK_RE.search(line) or "companion" in low:
                title = line
                break
        if not title and lines:
            title = lines[0][:160]

    editors: list[str] = []
    head = "\n".join(lines[:120])
    if meta.get("authors"):
        editors.extend(str(a) for a in meta.get("authors", [])[:6])
    editors.extend(front_profile["filename_editors_or_authors"])
    for marker in ("edited by", "editors", "editor"):
        if marker in head.lower() and not editors:
            editors.append("editor_signal_detected")
            break

    years = _extract_year_candidates(text)
    year = meta.get("year") or (years[0] if years else None)

    confidence = "low"
    if title and (editors or HANDBOOK_RE.search(name.lower()) or "editor" in name.lower()):
        confidence = "medium"
    if title and year and ids.get("isbn_candidates"):
        confidence = "medium"

    return {
        "title": title,
        "authors_or_editors": _bounded_unique(editors, 8) if editors else [],
        "year": year,
        "publisher_or_source": meta.get("journal_or_source") or meta.get("publisher_or_source_clues"),
        "publisher": front_profile.get("publisher"),
        "series": front_profile.get("series"),
        "doi": ids.get("doi") or meta.get("doi"),
        "isbn_candidates": ids.get("isbn_candidates", []),
        "document_type": doc_type if doc_type in {"book", "edited_volume"} else "book",
        "confidence": confidence,
        "front_matter_evidence": front_profile,
    }


def run_general_book_toc_chapter_preview_item(
    *,
    pdf_plan: dict[str, Any],
    stage_e_item: dict[str, Any] | None,
) -> dict[str, Any]:
    """Stage G-style read-only escalation for one parent candidate."""
    pdf_path = Path(pdf_plan["path"])
    full_text, text_error = _run_pdftotext(pdf_path, first_pages_only=False)
    pdf_meta = _read_pdf_metadata(pdf_path)
    lines = _clean_lines(full_text)
    ids = _extract_identifiers(full_text, pdf_meta)
    toc = _detect_toc(lines)
    chapters = _detect_chapter_boundaries(lines)
    metadata = _infer_general_parent_metadata(
        name=pdf_plan["name"],
        stage_e_item=stage_e_item,
        text=full_text,
        lines=lines,
        ids=ids,
    )

    if metadata.get("document_type") == "edited_volume" and (toc["toc_found"] or chapters):
        route = "edited_volume_parent_record_candidate"
        if len(chapters) >= 3:
            route = "parent_plus_child_chapter_candidates"
    elif toc["toc_found"] or chapters:
        route = "book_parent_record_candidate"
    elif metadata.get("confidence") == "low":
        route = "machine_escalation_still_needed"
    else:
        route = "book_parent_record_candidate"

    return {
        "stage": "general-book-toc-chapter-preview",
        "name": pdf_plan["name"],
        "path": str(pdf_path),
        "generalized": True,
        "koren_harris_specific_code_bypassed": True,
        "text_profile": {
            "chars": len(full_text),
            "fingerprint": _text_fingerprint(full_text),
            "pdf_metadata": pdf_meta,
            "extraction_error": text_error,
        },
        "candidate_parent_metadata": metadata,
        "toc_preview": toc,
        "chapter_boundary_preview": chapters[:20],
        "final_route_recommendation": route,
        "candidate_parent_id_preview": _safe_id_preview(metadata, route),
        "guideline_assessment": _guideline_assessment(metadata),
        "explicit_non_actions_now": [
            "no DB row creation/update",
            "no managed PDF copy/move",
            "no safe ID commit",
            "no paid API call",
            "no evaluation",
        ],
    }


def _titlepage_validation_score(lines: list[str], metadata: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    score = 0
    reasons: list[str] = []
    blockers: list[str] = []
    low = "\n".join(lines[:200]).lower()
    title_page = "\n".join(lines[:250])
    compact_title_page = _compact_ocr_text(title_page)
    title_norm = _norm(metadata.get("title"))

    if title_norm:
        title_compact = _compact_ocr_text(title_norm)
        tokens = _title_tokens(title_norm)
        token_hits = [token for token in tokens if token in compact_title_page]
        enough_fragmented_hits = bool(tokens) and len(token_hits) >= max(2, int(len(tokens) * 0.6))
        if any(title_norm in _norm(line) or _norm(line) in title_norm for line in lines[:250]):
            score += 12
            reasons.append("title line found in bounded title-page scan")
        elif title_compact and title_compact in compact_title_page:
            score += 12
            reasons.append("canonical title found after OCR-space normalization")
        elif enough_fragmented_hits:
            score += 12
            reasons.append(f"fragmented OCR title tokens confirmed: {token_hits}")
        else:
            blockers.append("canonical title not confirmed on title page")
    else:
        blockers.append("missing resolved title")

    if EDITOR_RE.search(low) or "editor" in low:
        score += 8
        reasons.append("editor marker on title page")
    if HANDBOOK_RE.search(low) or any(marker in compact_title_page for marker in ("handbook", "companion", "oxfordhandbook")):
        score += 6
        reasons.append("handbook/companion marker")
    if "isbn" in low or metadata.get("isbn_candidates"):
        score += 4
        reasons.append("ISBN signal")
    if metadata.get("year") and YEAR_RE.search(low):
        score += 4
        reasons.append("year signal")
    if metadata.get("doi"):
        score += 2
        reasons.append("DOI present in metadata resolution")

    return score, reasons, blockers


def _title_confirmed_in_text(text: str, title: str | None) -> tuple[bool, list[str]]:
    if not title:
        return False, []
    compact_text = _compact_ocr_text(text)
    title_compact = _compact_ocr_text(title)
    tokens = _title_tokens(title)
    token_hits = [token for token in tokens if token in compact_text]
    confirmed = bool(title_compact and title_compact in compact_text) or (
        bool(tokens) and len(token_hits) >= max(2, int(len(tokens) * 0.75))
    )
    return confirmed, token_hits


def _db_id_exists(candidate_id: str | None, db_path: Path) -> bool:
    if not candidate_id or not db_path.exists():
        return False
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT 1 FROM papers WHERE id = ? LIMIT 1", (candidate_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def run_general_parent_validation_preview(
    *,
    pdf_plan: dict[str, Any],
    stage_g_item: dict[str, Any],
    stage_e_item: dict[str, Any] | None,
    workflow_route: dict[str, Any],
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Generalized read-only parent validation; refuses live write in this module."""
    db_path = db_path or config.DB_PATH.resolve()
    pdf_path = Path(pdf_plan["path"])
    on_disk_hash = _sha256(pdf_path)
    plan_hash = pdf_plan.get("sha256", "")
    hash_ok = on_disk_hash == plan_hash

    full_text, text_error = _run_pdftotext(pdf_path, first_pages_only=True)
    lines = _clean_lines(full_text, limit_chars=80_000)
    metadata = stage_g_item.get("candidate_parent_metadata") or {}
    id_preview = stage_g_item.get("candidate_parent_id_preview") or {}
    score, reasons, blockers = _titlepage_validation_score(lines, metadata)
    if "canonical title not confirmed on title page" in blockers:
        full_text, full_text_error = _run_pdftotext(pdf_path, first_pages_only=False)
        confirmed, token_hits = _title_confirmed_in_text(full_text, metadata.get("title"))
        if confirmed and not full_text_error:
            blockers = [b for b in blockers if b != "canonical title not confirmed on title page"]
            score += 12
            reasons.append(f"canonical title confirmed in full-text scan after nonstandard page-order fallback: {token_hits}")

    if not hash_ok:
        blockers.append("sha256 mismatch vs run plan")
    if text_error:
        blockers.append(f"text extraction error: {text_error}")
    if workflow_route.get("duplicate_log_only"):
        blockers.append("duplicate log-only item must not enter parent validation")

    candidate_id = id_preview.get("candidate_id")
    if candidate_id and _db_id_exists(candidate_id, db_path):
        blockers.append(f"proposed candidate_id already exists in DB: {candidate_id}")

    validation_pass = hash_ok and not text_error and score >= 14 and not blockers
    ready_for_future_parent_record_pilot = validation_pass and id_preview.get("confidence") in {"medium", "high"}

    return {
        "stage": "general-parent-validation-preview",
        "name": pdf_plan["name"],
        "path": str(pdf_path),
        "generalized": True,
        "koren_harris_specific_validator_bypassed": True,
        "sha256_check": {"plan": plan_hash, "on_disk": on_disk_hash, "ok": hash_ok},
        "validation_score": score,
        "validation_reasons": reasons,
        "validation_blockers": blockers,
        "validation_pass": validation_pass,
        "ready_for_future_parent_record_pilot": ready_for_future_parent_record_pilot,
        "live_db_write_refused": True,
        "live_write_requires": [
            "validation_pass true in live mode",
            "--allow-db-write",
            "DB backup",
            "item-specific parent record stage (not Koren/Harris hardcoded)",
        ],
        "candidate_parent_id_preview": id_preview,
        "candidate_parent_metadata": metadata,
        "workflow_route_type": workflow_route.get("route_type"),
        "stage_e_confidence": (stage_e_item or {}).get("duplicate_version_resolution", {}).get("confidence")
        or (stage_e_item or {}).get("metadata_resolution", {}).get("confidence"),
        "explicit_non_actions_now": [
            "no DB row creation/update",
            "no managed PDF copy/move",
            "no staging archive",
            "no ID commit",
        ],
    }
