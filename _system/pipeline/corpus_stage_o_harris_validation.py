"""Stage O: read-only Harris edited-volume validation preview."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import config
from corpus_stage_d_preview import _read_pdf_metadata, _run_pdftotext

HARRIS_NAME = "Harris-Effect of pegylation on pharmaceuticals-2003-Nature R.pdf"
HARRIS_ID_PREVIEW = "Reuter_2016_Neuroeconomics_edited"
REJECTED_PRIOR_ID_PREVIEW = "Reuter_2009_Neuroeconomics_edited"
EXPECTED_DOI = "10.1007/978-3-642-35923-1"
EXPECTED_EDITORS = ["Martin Reuter", "Christian Montag"]
EXPECTED_ISBNS = ["978-3-642-35922-4", "978-3-642-35923-1"]
EXPECTED_YEAR = "2016"
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
ISBN_RE = re.compile(r"\b(?:97[89][-\s]?)?\d[-\s]?\d{2,5}[-\s]?\d{2,7}[-\s]?\d{1,7}[-\s]?[\dX]\b")


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _ascii_token(s: str) -> str:
    normalized = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^A-Za-z0-9]", "", normalized.encode("ascii", "ignore").decode("ascii"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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


def _clean_lines(text: str, limit_chars: int | None = 220_000) -> list[str]:
    source = text[:limit_chars] if limit_chars is not None else text
    lines = []
    for raw in source.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return lines


def _pages(text: str) -> list[list[str]]:
    return [_clean_lines(page, limit_chars=None) for page in text.split("\f") if page.strip()]


def _unique(items: list[str], limit: int = 20) -> list[str]:
    out = []
    seen = set()
    for item in items:
        clean = re.sub(r"\s+", " ", item).strip(" ,;")
        if not clean or clean.lower() in seen:
            continue
        seen.add(clean.lower())
        out.append(clean)
        if len(out) >= limit:
            break
    return out


def _extract_doi(text: str) -> str | None:
    m = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", text[:180_000])
    return m.group(0).rstrip(".,;)") if m else None


def _extract_isbns(text: str) -> list[str]:
    candidates = []
    for raw in ISBN_RE.findall(text[:180_000]):
        digits = re.sub(r"[^0-9X]", "", raw.upper())
        if len(digits) in {10, 13} and digits.startswith(("97", "0", "1")):
            candidates.append(raw)
    return _unique(candidates, 12)


def _score_parent_page(lines: list[str], page: int) -> tuple[int, list[str]]:
    low = "\n".join(lines).lower()
    score = 0
    reasons = []
    if any(_norm(line) == "neuroeconomics" for line in lines):
        score += 12
        reasons.append("exact parent title line")
    if "martin reuter" in low and "christian montag" in low:
        score += 8
        reasons.append("both volume editor names")
    if "springer" in low:
        score += 5
        reasons.append("Springer publisher marker")
    if "studies in neuroscience" in low or "psychology and behavioral economics" in low:
        score += 4
        reasons.append("series marker")
    if "isbn" in low or "doi" in low:
        score += 3
        reasons.append("identifier marker")
    if "chapter" in low or "abstract" in low:
        score -= 4
        reasons.append("chapter-content penalty")
    if page > 25:
        score -= 8
        reasons.append("late-page penalty")
    return score, reasons


def _title_page_candidates(pages: list[list[str]]) -> list[dict[str, Any]]:
    candidates = []
    for i, lines in enumerate(pages[:80], start=1):
        low = "\n".join(lines).lower()
        if "neuroeconomics" not in low:
            continue
        score, reasons = _score_parent_page(lines, i)
        if score >= 8:
            candidates.append({"page": i, "score": score, "reasons": reasons, "lines": lines[:80]})
    return sorted(candidates, key=lambda c: (c["score"], -c["page"]), reverse=True)


def _extract_year(lines: list[str], text: str) -> tuple[str | None, list[str]]:
    evidence = []
    priority_evidence = []
    scored: dict[str, int] = {}
    for line in lines[:1200]:
        years = YEAR_RE.findall(line)
        if not years:
            continue
        low = line.lower()
        priority = any(k in low for k in ("copyright", "©", "springer-verlag", "springer nature", "isbn", "doi"))
        weight = 10 if priority else 1
        for year in years:
            scored[year] = scored.get(year, 0) + weight
            if priority:
                priority_evidence.append(line)
            elif weight > 1:
                evidence.append(line)
    if not scored:
        return None, []
    priority_years: dict[str, int] = {}
    for line in priority_evidence:
        for year in YEAR_RE.findall(line):
            priority_years[year] = priority_years.get(year, 0) + 1
    if priority_years:
        preferred = sorted(priority_years.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)[0][0]
        return preferred, _unique(priority_evidence, 12)
    ranked = sorted(scored.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    preferred = next((year for year, _ in ranked if year != "2003"), ranked[0][0])
    return preferred, _unique(evidence, 12)


def _metadata_from_text(text: str, pdf_meta: dict[str, Any]) -> dict[str, Any]:
    lines = _clean_lines(text)
    page_list = _pages(text)
    title_candidates = _title_page_candidates(page_list)
    selected = title_candidates[0] if title_candidates else None
    selected_lines = selected.get("lines", []) if selected else []
    selected_text = "\n".join(selected_lines)
    full_head = "\n".join(lines[:1200])
    title = "Neuroeconomics" if "Neuroeconomics" in (selected_text or full_head[:20_000]) else None
    editors = [name for name in EXPECTED_EDITORS if name in (selected_text or full_head[:60_000])]
    year, year_evidence = _extract_year(lines, text)
    doi = _extract_doi(text)
    isbns = _extract_isbns(text)
    publisher = "Springer" if "springer" in full_head.lower() else None
    series = "Studies in Neuroscience, Psychology and Behavioral Economics" if "Studies in Neuroscience" in full_head else None
    confidence_parts = [
        bool(title),
        editors == EXPECTED_EDITORS,
        year == EXPECTED_YEAR,
        publisher == "Springer",
        doi == EXPECTED_DOI,
        any(x in isbns for x in EXPECTED_ISBNS),
    ]
    confidence = "high" if sum(confidence_parts) >= 5 else "medium" if sum(confidence_parts) >= 4 else "low"
    return {
        "title": title,
        "subtitle": None,
        "editors": editors,
        "year": year,
        "publisher": publisher,
        "source_or_series": series,
        "doi": doi,
        "isbn_candidates": isbns,
        "edition": None,
        "document_type": "edited_volume",
        "whole_volume_or_other": "full_edited_volume",
        "confidence": confidence,
        "evidence": {
            "title_page_candidates": title_candidates[:6],
            "selected_title_page": selected,
            "editor_evidence": [line for line in selected_lines if any(name in line for name in EXPECTED_EDITORS)] or selected_lines[:12],
            "year_evidence": year_evidence,
            "identifier_evidence": [line for line in lines[:1200] if "ISBN" in line or "DOI" in line][:20],
            "publisher_evidence": [line for line in lines[:1200] if "Springer" in line][:10],
            "pdf_metadata": pdf_meta,
        },
    }


def _chapter_candidates(stage_g_item: dict[str, Any]) -> dict[str, Any]:
    toc_entries = stage_g_item.get("toc_preview", {}).get("candidate_entries", [])
    boundaries = stage_g_item.get("chapter_boundary_preview", [])
    bounded = []
    for idx, entry in enumerate(toc_entries[:12], start=1):
        author = None
        toc_excerpt = stage_g_item.get("toc_preview", {}).get("toc_excerpt", [])
        for i, line in enumerate(toc_excerpt):
            if line == entry and i + 1 < len(toc_excerpt):
                nxt = toc_excerpt[i + 1]
                if not re.fullmatch(r"\d+", nxt.strip()):
                    author = nxt
                elif i + 2 < len(toc_excerpt):
                    author = toc_excerpt[i + 2]
                break
        bounded.append(
            {
                "ordinal": idx,
                "title": re.sub(r"\s*\.{2,}\s*\d+\s*$", "", entry).strip(),
                "authors": [author] if author and not author.lower().startswith("part ") else [],
                "metadata_confidence": "medium" if author else "low",
                "id_preview": {"candidate_id": None, "confidence": "low", "reason": "child IDs deferred; chapter metadata needs dedicated validation"},
            }
        )
    return {
        "toc_found": stage_g_item.get("toc_preview", {}).get("toc_found") is True,
        "chapter_boundary_count": len(boundaries),
        "candidate_chapters": bounded,
        "chapter_metadata_reliable_for_future_child_records": False,
        "recommendation": "parent-only first; child/chapter records deferred until dedicated chapter metadata validation",
    }


def _db_rows() -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, document_type, basic_metadata, identifiers, file_info FROM papers").fetchall()
    finally:
        conn.close()
    return [
        {
            "id": row["id"],
            "document_type": row["document_type"],
            "basic_metadata": _json_obj(row["basic_metadata"]),
            "identifiers": _json_obj(row["identifiers"]),
            "file_info": _json_obj(row["file_info"]),
        }
        for row in rows
    ]


def _duplicate_reassessment(metadata: dict[str, Any], staged_path: Path, staged_hash: str) -> dict[str, Any]:
    matches = []
    title = metadata.get("title") or ""
    year = str(metadata.get("year") or "")
    doi = (metadata.get("doi") or "").lower()
    isbns = {re.sub(r"[^0-9X]", "", x.upper()) for x in metadata.get("isbn_candidates", [])}
    editors = {_norm(e.split()[-1]) for e in metadata.get("editors", []) if e.split()}
    for row in _db_rows():
        bm = row["basic_metadata"]
        ids = row["identifiers"]
        fi = row["file_info"]
        signals = []
        raw_json = json.dumps({"basic_metadata": bm, "identifiers": ids, "file_info": fi}, ensure_ascii=False)
        db_title = str(bm.get("title") or "")
        title_ratio = SequenceMatcher(None, _norm(title), _norm(db_title)).ratio() if title and db_title else 0.0
        if doi and doi in raw_json.lower():
            signals.append("doi_match")
        if any(isbn and isbn in re.sub(r"[^0-9X]", "", raw_json.upper()) for isbn in isbns):
            signals.append("isbn_match")
        if title_ratio >= 0.92:
            signals.append(f"title_{int(title_ratio * 100)}")
        if year and str(bm.get("year") or "") == year:
            signals.append("year_exact")
        people = bm.get("authors") or []
        people_surnames = {_norm(str(p).split()[-1]) for p in people if str(p).split()}
        if editors and people_surnames and editors & people_surnames:
            signals.append("editor_or_author_overlap")
        renamed = fi.get("renamed_filename")
        if renamed:
            candidate = config.SOURCE_PDFS / str(renamed)
            try:
                if candidate.exists() and candidate.stat().st_size == staged_path.stat().st_size and _sha256(candidate) == staged_hash:
                    signals.append("managed_pdf_hash_match")
            except OSError:
                pass
        strong = "doi_match" in signals or "isbn_match" in signals or "managed_pdf_hash_match" in signals or any(s.startswith("title_") for s in signals)
        if strong:
            matches.append(
                {
                    "id": row["id"],
                    "document_type": row["document_type"],
                    "title": bm.get("title"),
                    "year": bm.get("year"),
                    "doi": bm.get("doi") or ids.get("doi"),
                    "signals": signals,
                    "title_ratio": round(title_ratio, 3),
                }
            )
    if any("managed_pdf_hash_match" in m["signals"] or ("doi_match" in m["signals"] and any(s.startswith("title_") for s in m["signals"])) for m in matches):
        status = "exact_duplicate_skip_candidate"
    elif any("doi_match" in m["signals"] or "isbn_match" in m["signals"] for m in matches):
        status = "possible_duplicate_needs_machine_comparison"
    elif matches:
        status = "possible_duplicate_needs_machine_comparison"
    else:
        status = "no_duplicate_signal"
    return {"status": status, "matches": matches[:12]}


def _safe_id_preview(metadata: dict[str, Any], duplicate_status: str) -> dict[str, Any]:
    if duplicate_status != "no_duplicate_signal":
        return {"candidate_id": None, "confidence": "low", "reason": "duplicate reassessment is not clear"}
    if metadata.get("confidence") not in {"high", "medium"}:
        return {"candidate_id": None, "confidence": "low", "reason": "metadata confidence is too low"}
    editors = metadata.get("editors") or []
    if editors != EXPECTED_EDITORS:
        return {"candidate_id": None, "confidence": "low", "reason": "editor evidence is not validated"}
    surname = _ascii_token(editors[0].split()[-1])
    year = metadata.get("year")
    title = metadata.get("title")
    keyword = _ascii_token(str(title or ""))
    candidate_id = f"{surname}_{year}_{keyword}_edited"
    if candidate_id != HARRIS_ID_PREVIEW:
        return {"candidate_id": None, "confidence": "low", "reason": f"generated ID mismatch: {candidate_id}"}
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        collision = conn.execute("SELECT 1 FROM papers WHERE id = ?", (candidate_id,)).fetchone() is not None
    finally:
        conn.close()
    if collision:
        return {"candidate_id": None, "confidence": "low", "reason": "candidate ID already exists in DB"}
    return {
        "candidate_id": candidate_id,
        "confidence": "medium",
        "reason": "preview only; based on validated editor/title/year evidence; acceptable edited-volume ID convention",
    }


def _row_preview(metadata: dict[str, Any], staged_path: Path, staged_hash: str, id_preview: dict[str, Any], chapter_policy: dict[str, Any]) -> dict[str, Any] | None:
    candidate_id = id_preview.get("candidate_id")
    if not candidate_id:
        return None
    return {
        "id_preview": candidate_id,
        "document_type": "edited_volume",
        "basic_metadata": {
            "title": metadata.get("title"),
            "subtitle": metadata.get("subtitle"),
            "authors": metadata.get("editors"),
            "year": metadata.get("year"),
            "publisher": metadata.get("publisher"),
            "source_or_series": metadata.get("source_or_series"),
            "doi": metadata.get("doi") or "",
            "isbn": metadata.get("isbn_candidates") or [],
            "edition": metadata.get("edition"),
            "language": "English",
            "article_type": "edited_volume_parent",
        },
        "file_info": {
            "original_filename": staged_path.name,
            "staged_sha256": staged_hash,
            "managed_pdf_destination_preview": str(config.SOURCE_PDFS / f"{candidate_id}.pdf"),
            "file_format": "pdf",
            "readability_status": "readable",
        },
        "tags": [
            "edited_volume_parent",
            "guideline_mismatch_or_new_guideline_needed_candidate",
            "not_journal_article_evaluation",
        ],
        "evaluation_status": "deferred_not_evaluated_for_edited_volume_parent",
        "child_chapter_policy": chapter_policy.get("recommendation"),
    }


def run_harris_edited_volume_validation_preview(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    matches = [p for p in plan.get("pdf_plans", []) if p.get("name") == HARRIS_NAME]
    if len(matches) != 1:
        raise RuntimeError("Harris PDF plan not found exactly once")
    pdf_plan = matches[0]
    staged_path = Path(pdf_plan["path"])
    text, text_error = _run_pdftotext(staged_path, first_pages_only=False)
    pdf_meta = _read_pdf_metadata(staged_path)
    staged_hash = _sha256(staged_path)
    stage_g = json.loads((run_dir / "book_toc_chapter_preview.json").read_text(encoding="utf-8"))
    stage_g_item = next(i for i in stage_g.get("items", []) if i.get("name") == HARRIS_NAME)
    metadata = _metadata_from_text(text, pdf_meta)
    duplicate = _duplicate_reassessment(metadata, staged_path, staged_hash)
    chapter_policy = _chapter_candidates(stage_g_item)
    if duplicate["status"] == "exact_duplicate_skip_candidate":
        route = "exact_duplicate_skip_candidate"
        readiness = "exact_duplicate_skip_candidate"
    elif metadata.get("confidence") == "high" and duplicate["status"] == "no_duplicate_signal":
        if chapter_policy["toc_found"] and not chapter_policy["chapter_metadata_reliable_for_future_child_records"]:
            route = "edited_volume_parent_record_candidate_only"
        else:
            route = "parent_plus_child_chapter_candidates" if chapter_policy["toc_found"] else "edited_volume_parent_record_candidate"
        readiness = "ready_for_parent_record_live_pilot"
    else:
        route = "machine_escalation_still_needed"
        readiness = "not_ready_needs_more_machine_escalation"
    id_preview = _safe_id_preview(metadata, duplicate["status"])
    row_preview = _row_preview(metadata, staged_path, staged_hash, id_preview, chapter_policy)
    summary = {
        "run_id": run_dir.name,
        "stage": "harris-edited-volume-validation-preview",
        "processed_only": [HARRIS_NAME],
        "source_artifacts": {
            "stage_d": str(run_dir / "text_metadata_preview.json"),
            "stage_e": str(run_dir / "metadata_duplicate_resolution_preview.json"),
            "stage_f": str(run_dir / "final_route_plan_preview.json"),
            "stage_g": str(run_dir / "book_toc_chapter_preview.json"),
        },
        "text_extraction": {"chars": len(text), "error": text_error},
        "canonical_metadata_validation": metadata,
        "duplicate_reassessment": duplicate,
        "parent_chapter_policy": {
            "recommended_representation": route,
            **chapter_policy,
        },
        "safe_parent_id_preview": id_preview,
        "rejected_prior_id_previews": [
            {
                "candidate_id": REJECTED_PRIOR_ID_PREVIEW,
                "reason": "rejected because publication/copyright evidence supports 2016, not 2009",
            }
        ],
        "proposed_parent_row_preview": row_preview,
        "live_readiness_decision": readiness,
        "human_review_needed_now": False,
        "later_stages_still_disabled": [
            "DB row creation/update",
            "managed PDF copy/move/archive",
            "safe ID commit",
            "paid evaluation",
            "batch submit",
            "batch collect/save",
            "duplicate skip execution",
            "PDF replacement",
            "book/chapter splitting writes",
            "human review folder creation outside the run folder",
        ],
        "side_effects": {
            "api_calls": "none",
            "papers_db_writes": "none",
            "pdf_moves_copies_renames_deletes_archive": "none",
            "id_changes": "none",
            "evaluations": "none",
            "child_chapter_records_created": "none",
        },
    }
    (run_dir / "harris_edited_volume_validation_preview.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "stage_harris_edited_volume_validation_preview_report.txt").write_text(render_harris_edited_volume_validation_report(summary), encoding="utf-8")
    return summary


def render_harris_edited_volume_validation_report(summary: dict[str, Any]) -> str:
    meta = summary["canonical_metadata_validation"]
    dup = summary["duplicate_reassessment"]
    policy = summary["parent_chapter_policy"]
    lines = ["Stage O Harris Edited-Volume Validation Preview (READ-ONLY)", "=" * 68]
    lines.append(f"Title: {meta.get('title')}")
    lines.append(f"Editors: {meta.get('editors')}")
    lines.append(f"Year: {meta.get('year')} | Publisher: {meta.get('publisher')} | Series: {meta.get('source_or_series')}")
    lines.append(f"DOI: {meta.get('doi')} | ISBNs: {meta.get('isbn_candidates')}")
    lines.append(f"Document type: {meta.get('document_type')} ({meta.get('confidence')})")
    lines.append(f"Duplicate reassessment: {dup.get('status')} matches={len(dup.get('matches', []))}")
    lines.append(f"Representation: {policy.get('recommended_representation')}")
    lines.append(f"Chapter candidates listed: {len(policy.get('candidate_chapters', []))}; child metadata reliable now: {policy.get('chapter_metadata_reliable_for_future_child_records')}")
    lines.append(f"ID preview: {summary['safe_parent_id_preview']}")
    lines.append(f"Parent row preview available: {summary['proposed_parent_row_preview'] is not None}")
    lines.append(f"Live readiness: {summary['live_readiness_decision']}")
    lines.append("Human review needed now: False")
    lines.append("")
    lines.append("No API calls, papers.db writes, PDF operations/archive, evaluations, child/chapter writes, or ID changes were performed.")
    return "\n".join(lines) + "\n"
