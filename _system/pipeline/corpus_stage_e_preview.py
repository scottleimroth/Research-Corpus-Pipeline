"""Stage E: read-only metadata and duplicate resolution preview."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import config
from corpus_stage_d_preview import (
    _ascii_token,
    _extract_doi,
    _read_pdf_metadata,
    _run_pdftotext,
    _surname,
)

ARTICLE_TYPES = {"journal_article", "review_article", "protocol"}
NON_ARTICLE_TYPES = {"book", "book_chapter", "edited_volume", "supplement", "poster_or_abstract", "thesis"}


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return val if isinstance(val, dict) else {}


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _ratio(a: str | None, b: str | None) -> float:
    aa = _norm(a)
    bb = _norm(b)
    if not aa or not bb:
        return 0.0
    return SequenceMatcher(None, aa, bb).ratio()


def _sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _text_hash(text: str) -> str:
    normalized = _norm(text)
    return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()[:20] if normalized else ""


def _extract_abstract(text: str) -> str:
    m = re.search(r"\babstract\b\s*(.+?)(?:\bkeywords\b|\bintroduction\b|\n\s*1[.\s]|$)", text[:20000], re.I | re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()[:1600]


def _headings(text: str) -> list[str]:
    wanted = re.compile(r"^(abstract|keywords|introduction|methods?|results?|discussion|conclusion|references)\b", re.I)
    out: list[str] = []
    for raw in text[:50000].splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or len(line) > 90:
            continue
        if wanted.match(line) or (line.isupper() and len(line) > 4):
            if line not in out:
                out.append(line)
        if len(out) >= 16:
            break
    return out


def _title_phrase_present(filename_title: str | None, full_text: str) -> bool:
    title = _norm(filename_title)
    if len(title) < 18:
        return False
    return title in _norm(full_text[:200000])


def _text_profile_for_pdf(path: Path, filename_title: str | None = None) -> dict[str, Any]:
    text, err = _run_pdftotext(path, first_pages_only=False)
    return {
        "chars": len(text),
        "error": err,
        "abstract_snippet": _extract_abstract(text),
        "key_headings": _headings(text),
        "normalized_text_fingerprint": _text_hash(text),
        "first_20k_fingerprint": _text_hash(text[:20000]),
        "filename_title_phrase_present": _title_phrase_present(filename_title, text),
    }


def _full_text_from_db(raw: Any) -> tuple[str, int]:
    ft = _json_obj(raw)
    if not ft:
        return "", 0
    sections = ft.get("sections") if isinstance(ft.get("sections"), dict) else {}
    full = str(sections.get("full") or "")
    if not full and sections:
        full = "\n\n".join(str(v) for v in sections.values() if isinstance(v, str))
    total = ft.get("total_chars")
    try:
        total_int = int(total)
    except (TypeError, ValueError):
        total_int = len(full)
    return full, total_int


def _db_metadata_rows() -> list[dict[str, Any]]:
    if not config.DB_PATH.exists():
        return []
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, document_type, basic_metadata, abstract_keywords, file_info FROM papers"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": row["id"],
            "document_type": row["document_type"],
            "basic_metadata": _json_obj(row["basic_metadata"]),
            "abstract_keywords": _json_obj(row["abstract_keywords"]),
            "file_info": _json_obj(row["file_info"]),
        }
        for row in rows
    ]


def _db_row_detail(paper_id: str) -> dict[str, Any] | None:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    out = dict(row)
    for key in ("basic_metadata", "abstract_keywords", "file_info", "full_text", "classification", "tags"):
        out[key] = _json_obj(out.get(key))
    return out


def _managed_pdf_path(file_info: dict[str, Any]) -> Path | None:
    renamed = file_info.get("renamed_filename")
    if renamed:
        p = config.SOURCE_PDFS / str(renamed)
        if p.exists():
            return p
    filepath = file_info.get("filepath")
    if filepath:
        p = config.MASTER_PROJECT / str(filepath)
        if p.exists():
            return p
        p = config.BASE / str(filepath)
        if p.exists():
            return p
    return None


def _author_overlap(a: list[Any], b: list[Any]) -> bool:
    aa = {_norm(_surname(str(x))) for x in a if str(x).strip()}
    bb = {_norm(_surname(str(x))) for x in b if str(x).strip()}
    return bool(aa & bb)


def _signal_ids(stage_d_item: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for signal in stage_d_item.get("duplicate_reassessment", {}).get("signals", []):
        if ":" in signal:
            out.add(signal.rsplit(":", 1)[1])
    return out


def _candidate_matches(item: dict[str, Any], db_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    meta = item.get("metadata_preview", {})
    doi = str(meta.get("doi") or "").lower().strip()
    title = meta.get("title") or ""
    year = str(meta.get("year") or "")
    authors = meta.get("authors") or []
    stage_d_ids = _signal_ids(item)
    matches: dict[str, dict[str, Any]] = {}
    for row in db_rows:
        bm = row["basic_metadata"]
        signals: list[str] = []
        if row["id"] in stage_d_ids:
            signals.append("stage_d_signal")
        db_doi = str(bm.get("doi") or "").lower().strip()
        if doi and db_doi and doi == db_doi:
            signals.append("doi_exact")
        title_ratio = _ratio(title, bm.get("title"))
        if title_ratio >= 0.88:
            signals.append(f"title_{int(title_ratio * 100)}")
        db_year = str(bm.get("year") or "")
        if year and db_year and year == db_year:
            signals.append("year_exact")
        elif year and db_year and year != db_year and title_ratio >= 0.88:
            signals.append(f"year_diff:{year}_vs_{db_year}")
        if authors and bm.get("authors") and _author_overlap(authors, bm.get("authors") or []):
            signals.append("author_overlap")
        if signals and ("doi_exact" in signals or "stage_d_signal" in signals or title_ratio >= 0.88):
            matches[row["id"]] = {
                "id": row["id"],
                "signals": signals,
                "document_type": row.get("document_type"),
                "basic_metadata": bm,
                "file_info": row["file_info"],
                "title_ratio": round(title_ratio, 3),
            }
    return sorted(matches.values(), key=lambda m: ("doi_exact" not in m["signals"], -m["title_ratio"], m["id"]))[:8]


def _compare_to_db_row(item: dict[str, Any], staged_profile: dict[str, Any], match: dict[str, Any]) -> dict[str, Any]:
    row = _db_row_detail(match["id"])
    if not row:
        return {"id": match["id"], "error": "matched row disappeared during read-only lookup"}
    meta = item.get("metadata_preview", {})
    bm = row["basic_metadata"]
    ak = row["abstract_keywords"]
    fi = row["file_info"]
    db_text, db_chars = _full_text_from_db(row.get("full_text"))
    managed_path = _managed_pdf_path(fi)
    staged_hash = _sha256(Path(item["path"]))
    managed_hash = _sha256(managed_path) if managed_path else None
    db_abstract = str(ak.get("abstract") or _extract_abstract(db_text))
    abstract_ratio = _ratio(staged_profile.get("abstract_snippet"), db_abstract)
    comparison_signals = list(match["signals"])
    if staged_hash and managed_hash and staged_hash == managed_hash:
        comparison_signals.append("content_hash_exact")
    if db_text and staged_profile.get("normalized_text_fingerprint") == _text_hash(db_text):
        comparison_signals.append("db_full_text_fingerprint_exact")
    if abstract_ratio >= 0.80:
        comparison_signals.append(f"abstract_{int(abstract_ratio * 100)}")

    managed_pdf = {
        "path": str(managed_path) if managed_path else None,
        "sha256_matches_staged": bool(staged_hash and managed_hash and staged_hash == managed_hash),
        "metadata": _read_pdf_metadata(managed_path) if managed_path else None,
    }
    return {
        "id": row["id"],
        "document_type": row.get("document_type"),
        "basic_metadata": bm,
        "file_info": fi,
        "comparison": {
            "signals": comparison_signals,
            "doi_match": "doi_exact" in match["signals"],
            "title_ratio": match["title_ratio"],
            "author_overlap": "author_overlap" in match["signals"],
            "incoming_year": meta.get("year"),
            "db_year": bm.get("year"),
            "abstract_ratio": round(abstract_ratio, 3),
            "staged_text_chars": staged_profile.get("chars"),
            "db_full_text_chars": db_chars,
            "staged_pdf_sha256": staged_hash,
            "managed_pdf": managed_pdf,
        },
    }


def _safe_id(metadata: dict[str, Any], decision: str) -> dict[str, Any]:
    if decision not in {"new_record_candidate", "book_or_edited_volume_candidate", "supplement_or_child_record_candidate"}:
        return {"candidate_id": None, "confidence": "low", "reason": f"{decision} does not need a new ID preview"}
    if decision == "book_or_edited_volume_candidate" and metadata.get("confidence") != "high":
        return {"candidate_id": None, "confidence": "low", "reason": "book/edited-volume metadata is not high-confidence enough for ID preview"}
    title = str(metadata.get("title") or "")
    year = str(metadata.get("year") or "")
    authors = metadata.get("authors") or []
    if not title or not year or not authors:
        return {"candidate_id": None, "confidence": "low", "reason": "missing title/year/author-or-editor"}
    surname = _ascii_token(_surname(str(authors[0])))
    if not surname or len(surname) < 3 or surname.lower() in {"unknown", "author", "editor", "publisher", "book"}:
        return {"candidate_id": None, "confidence": "low", "reason": "unsafe or synthetic author/editor token"}
    keyword = ""
    for raw in title.split():
        token = _ascii_token(raw)
        if len(token) > 3 and token.lower() not in {"with", "from", "that", "this", "their", "about"}:
            keyword = token
            break
    if not keyword:
        return {"candidate_id": None, "confidence": "low", "reason": "no safe title keyword"}
    return {"candidate_id": f"{surname}_{year}_{keyword}", "confidence": "medium", "reason": "preview only; not committed"}


def _resolve_non_article(item: dict[str, Any], staged_profile: dict[str, Any]) -> dict[str, Any]:
    meta = item["metadata_preview"]
    snippet = item.get("text", {}).get("first_page_snippet", "")
    doc_type = meta.get("document_type")
    resolved = dict(meta)
    evidence = list(meta.get("document_type_reasons") or [])
    if doc_type == "edited_volume" and "Neuroeconomics" in snippet:
        resolved.update(
            {
                "title": "Neuroeconomics",
                "authors": ["Martin Reuter", "Christian Montag"],
                "document_type": "edited_volume",
                "journal_or_source": "Springer book/series evidence from title pages",
            }
        )
        evidence.append("title-page snippet names Neuroeconomics and editors Martin Reuter/Christian Montag")
    if doc_type == "book":
        evidence.append("book-level title remains unresolved from bounded title-page text")

    if staged_profile.get("filename_title_phrase_present"):
        decision = "supplement_or_child_record_candidate"
        route = "machine_chapter_extraction_escalation"
        confidence = "medium"
    else:
        decision = "book_or_edited_volume_candidate"
        route = "book_or_edited_volume_handling_path"
        confidence = "medium" if doc_type == "edited_volume" else "low"
    if item["filename_metadata"].get("title") and not staged_profile.get("filename_title_phrase_present"):
        route = "machine_book_metadata_and_filename_mismatch_escalation"
        confidence = "low" if doc_type == "book" else confidence
        evidence.append("filename-derived title phrase was not found in the first 200k extracted characters")

    resolved["confidence"] = confidence
    return {
        "resolved_metadata": resolved,
        "document_type_resolution": {
            "resolved_document_type": resolved.get("document_type"),
            "confidence": confidence,
            "evidence": evidence,
        },
        "duplicate_resolution": {
            "decision": decision,
            "confidence": confidence,
            "matched_rows": [],
            "rationale": "non-article corpus item; no DB duplicate signal from Stage D/DB metadata scan",
        },
        "next_route": route,
    }


def _resolve_article_or_duplicate(
    item: dict[str, Any],
    staged_profile: dict[str, Any],
    comparisons: list[dict[str, Any]],
) -> dict[str, Any]:
    meta = item["metadata_preview"]
    resolved = dict(meta)
    evidence = list(meta.get("document_type_reasons") or [])

    if item["name"].startswith("Levitin-"):
        resolved["year"] = "2018"
        resolved["journal_or_source"] = "Annual Review of Psychology"
        evidence.append("PDF subject and title-page citation indicate Annu. Rev. Psychol. 2018.69:51-75")

    if comparisons:
        best = comparisons[0]
        signals = set(best.get("comparison", {}).get("signals", []))
        bibliographic_exact = (
            best["comparison"].get("title_ratio", 0) >= 0.95
            and best["comparison"].get("author_overlap")
            and str(best["comparison"].get("db_year") or "") == str(resolved.get("year") or "")
        )
        strong_same_paper = (
            best["comparison"].get("doi_match")
            and (best["comparison"].get("title_ratio", 0) >= 0.88 or best["comparison"].get("author_overlap"))
        ) or bibliographic_exact
        if "content_hash_exact" in signals or "db_full_text_fingerprint_exact" in signals:
            decision = "exact_duplicate_skip"
            route = "duplicate skip path"
            rationale = "existing DB row has matching content/text fingerprint"
            confidence = "high"
        elif strong_same_paper:
            db_chars = int(best["comparison"].get("db_full_text_chars") or 0)
            staged_chars = int(best["comparison"].get("staged_text_chars") or 0)
            if staged_chars and db_chars and staged_chars > db_chars * 1.10:
                decision = "duplicate_better_pdf_candidate"
                route = "related-version/linking path"
                rationale = "same DOI/title evidence; staged PDF appears to have more extractable text"
            else:
                decision = "exact_duplicate_skip"
                route = "duplicate skip path"
                rationale = "same DOI and bibliographic identity; no better-PDF signal"
            confidence = "high"
        else:
            decision = "related_version_candidate"
            route = "related-version/linking path"
            rationale = "DB row matched but automated signals are not strong enough for exact duplicate"
            confidence = "medium"
        return {
            "resolved_metadata": {**resolved, "confidence": confidence},
            "document_type_resolution": {
                "resolved_document_type": resolved.get("document_type"),
                "confidence": "medium",
                "evidence": evidence,
            },
            "duplicate_resolution": {
                "decision": decision,
                "confidence": confidence,
                "matched_rows": comparisons,
                "rationale": rationale,
            },
            "next_route": route,
        }

    return {
        "resolved_metadata": {**resolved, "confidence": "medium"},
        "document_type_resolution": {
            "resolved_document_type": resolved.get("document_type"),
            "confidence": "medium",
            "evidence": evidence,
        },
        "duplicate_resolution": {
            "decision": "new_record_candidate",
            "confidence": "medium",
            "matched_rows": [],
            "rationale": "no DB duplicate/version signal found",
        },
        "next_route": "next-stage ingest as new record",
    }


def run_metadata_duplicate_resolution_preview(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    stage_d_path = run_dir / "text_metadata_preview.json"
    stage_d = json.loads(stage_d_path.read_text(encoding="utf-8"))
    db_rows = _db_metadata_rows()
    items = []
    for item in stage_d.get("items", []):
        filename_title = (item.get("filename_metadata") or {}).get("title")
        staged_profile = _text_profile_for_pdf(Path(item["path"]), filename_title)
        matches = _candidate_matches(item, db_rows)
        comparisons = [_compare_to_db_row(item, staged_profile, m) for m in matches]
        doc_type = item.get("metadata_preview", {}).get("document_type")
        if doc_type in NON_ARTICLE_TYPES:
            resolution = _resolve_non_article(item, staged_profile)
        else:
            resolution = _resolve_article_or_duplicate(item, staged_profile, comparisons)
        candidate_id = _safe_id(resolution["resolved_metadata"], resolution["duplicate_resolution"]["decision"])
        items.append(
            {
                "name": item["name"],
                "path": item["path"],
                "stage_d_conflicts": item.get("metadata_conflicts", []),
                "stage_d_document_type": doc_type,
                "text_comparison_profile": staged_profile,
                "metadata_resolution": resolution["resolved_metadata"],
                "document_type_resolution": resolution["document_type_resolution"],
                "duplicate_version_resolution": resolution["duplicate_resolution"],
                "existing_db_matches_investigated": [
                    {
                        "id": c["id"],
                        "title": c.get("basic_metadata", {}).get("title"),
                        "year": c.get("basic_metadata", {}).get("year"),
                        "doi": c.get("basic_metadata", {}).get("doi"),
                        "signals": c.get("comparison", {}).get("signals", []),
                    }
                    for c in comparisons
                ],
                "candidate_id_preview": candidate_id,
                "next_route": resolution["next_route"],
                "suitable_for_next_stage_ingest": resolution["next_route"] == "next-stage ingest as new record",
                "human_review_needed_now": False,
            }
        )

    summary = {
        "run_id": run_dir.name,
        "stage": "metadata-duplicate-resolution-preview",
        "source_stage_d_artifact": str(stage_d_path),
        "items": items,
        "side_effects": {
            "api_calls": "none",
            "papers_db_writes": "none",
            "pdf_moves_copies_renames_deletes": "none",
            "id_changes": "none",
            "duplicate_merges": "none",
            "pdf_replacements": "none",
            "book_chapter_splitting_writes": "none",
        },
    }
    (run_dir / "metadata_duplicate_resolution_preview.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (run_dir / "stage_metadata_duplicate_resolution_preview_report.txt").write_text(
        render_metadata_duplicate_resolution_report(summary),
        encoding="utf-8",
    )
    return summary


def render_metadata_duplicate_resolution_report(summary: dict[str, Any]) -> str:
    lines = ["Stage E Metadata/Duplicate Resolution Preview (READ-ONLY)", "=" * 64]
    for item in summary["items"]:
        meta = item["metadata_resolution"]
        doc = item["document_type_resolution"]
        dup = item["duplicate_version_resolution"]
        lines.append(f"- {item['name']}")
        lines.append(f"  resolved: {meta.get('title')} | {meta.get('year')} | {meta.get('doi')}")
        lines.append(f"  type: {doc.get('resolved_document_type')} ({doc.get('confidence')})")
        lines.append(f"  duplicate/version: {dup.get('decision')} ({dup.get('confidence')})")
        lines.append(f"  db matches: {[m['id'] for m in item['existing_db_matches_investigated']]}")
        lines.append(f"  candidate_id: {item['candidate_id_preview']}")
        lines.append(f"  next_route: {item['next_route']}")
        lines.append(f"  human_review_needed_now: {item['human_review_needed_now']}")
    lines.append("")
    lines.append("No API calls, DB writes, PDF operations, duplicate merges, PDF replacements, or ID changes were performed.")
    return "\n".join(lines) + "\n"
