"""Stage G: read-only book/TOC/chapter escalation preview.

This preview is scoped to the current DB/staging workflow. It never excludes a
valid staged PDF for topical reasons; topic/guideline fit is metadata only.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from corpus_stage_d_preview import _extract_doi, _read_pdf_metadata, _run_pdftotext

TARGET_PREFIXES = ("Harris-", "Koren-")
ISBN_RE = re.compile(r"\b(?:97[89][-\s]?)?\d[-\s]?\d{2,5}[-\s]?\d{2,7}[-\s]?\d{1,7}[-\s]?[\dX]\b")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _ascii_token(s: str) -> str:
    normalized = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^A-Za-z0-9]", "", normalized.encode("ascii", "ignore").decode("ascii"))


def _text_fingerprint(text: str) -> str:
    norm = _norm(text)
    return hashlib.sha256(norm.encode("utf-8", errors="ignore")).hexdigest()[:20] if norm else ""


def _clean_lines(text: str, *, limit_chars: int = 120_000) -> list[str]:
    lines = []
    for raw in text[:limit_chars].splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return lines


def _bounded_unique(items: list[str], limit: int = 30) -> list[str]:
    out = []
    seen = set()
    for item in items:
        clean = re.sub(r"\s+", " ", item).strip()
        if not clean or clean.lower() in seen:
            continue
        seen.add(clean.lower())
        out.append(clean[:220])
        if len(out) >= limit:
            break
    return out


def _extract_year_candidates(text: str) -> list[str]:
    years = YEAR_RE.findall(text[:80_000])
    scored: list[tuple[int, str]] = []
    for year in sorted(set(years)):
        score = 0
        for m in re.finditer(year, text[:80_000]):
            window = text[max(0, m.start() - 80) : m.end() + 80].lower()
            if any(k in window for k in ("copyright", "©", "published", "springer", "guilford", "isbn", "doi")):
                score += 3
            else:
                score += 1
        scored.append((score, year))
    return [year for _, year in sorted(scored, reverse=True)[:8]]


def _extract_identifiers(text: str, pdf_meta: dict[str, Any]) -> dict[str, Any]:
    first = text[:120_000]
    doi = _extract_doi(first)
    isbn_candidates = _bounded_unique(ISBN_RE.findall(first), 8)
    return {
        "doi": doi,
        "isbn_candidates": isbn_candidates,
        "pdf_metadata_title": pdf_meta.get("title"),
        "pdf_metadata_author": pdf_meta.get("author"),
        "pdf_pages": pdf_meta.get("pages"),
    }


def _detect_toc(lines: list[str]) -> dict[str, Any]:
    toc_indices = [
        i
        for i, line in enumerate(lines[:2500])
        if line.lower() in {"contents", "table of contents"} or line.lower().startswith("contents ")
    ]
    if not toc_indices:
        return {"toc_found": False, "toc_start_line": None, "toc_excerpt": [], "candidate_entries": []}
    start = toc_indices[0]
    excerpt = lines[start : min(start + 90, len(lines))]
    entries = []
    for line in excerpt[1:]:
        low = line.lower()
        if low in {"references", "index"}:
            break
        if len(line) < 4 or len(line) > 180:
            continue
        if re.search(r"\.{2,}\s*\d+$", line) or re.match(r"^(chapter\s+)?\d+[\.:]?\s+\S", line, re.I):
            entries.append(line)
        elif any(k in low for k in ("introduction", "references", "acknowledg", "contributors")):
            entries.append(line)
    return {
        "toc_found": True,
        "toc_start_line": start,
        "toc_excerpt": excerpt[:35],
        "candidate_entries": _bounded_unique(entries, 25),
    }


def _detect_chapter_boundaries(lines: list[str]) -> list[dict[str, Any]]:
    candidates = []
    for i, line in enumerate(lines[:9000]):
        if len(line) > 160:
            continue
        low = line.lower()
        is_boundary = bool(re.match(r"^(chapter|part)\s+[0-9ivxlcdm]+[:.\s-]+\S", line, re.I))
        is_numbered_title = bool(re.match(r"^[0-9]{1,2}\s+[A-Z][A-Za-z].{8,}$", line))
        if not (is_boundary or is_numbered_title):
            continue
        context = lines[i : min(i + 4, len(lines))]
        candidates.append({"line_index": i, "heading": line, "context": context})
        if len(candidates) >= 30:
            break
    return candidates


def _infer_harris_metadata(stage_item: dict[str, Any], text: str, lines: list[str], ids: dict[str, Any]) -> dict[str, Any]:
    title = "Neuroeconomics" if "Neuroeconomics" in text[:20_000] else stage_item["metadata_snapshot"].get("title")
    editors = []
    title_page = "\n".join(lines[:80])
    if "Martin Reuter" in title_page:
        editors.append("Martin Reuter")
    if "Christian Montag" in title_page:
        editors.append("Christian Montag")
    years = _extract_year_candidates(text)
    # Prefer a publisher/copyright year over the filename-derived year when available.
    year = next((y for y in years if y != "2003"), years[0] if years else stage_item["metadata_snapshot"].get("year"))
    return {
        "title": title,
        "authors_or_editors": editors or stage_item["metadata_snapshot"].get("authors", []),
        "year": year,
        "publisher_or_source": "Springer / Studies in Neuroscience, Psychology and Behavioral Economics",
        "doi": ids.get("doi") or stage_item["metadata_snapshot"].get("doi"),
        "isbn_candidates": ids.get("isbn_candidates", []),
        "document_type": "edited_volume",
        "confidence": "medium" if title and editors else "low",
    }


def _infer_koren_metadata(stage_item: dict[str, Any], text: str, lines: list[str], ids: dict[str, Any]) -> dict[str, Any]:
    noisy = {"ebook", "the guilford press", "contents", "index", "references"}
    title_candidates = []
    for i, line in enumerate(lines[:500]):
        low = line.lower()
        if low in noisy or len(line) < 8 or len(line) > 120:
            continue
        if any(skip in low for skip in ("download", "copyright", "isbn", "doi", "http", "guilford")):
            continue
        if line.isupper() or i < 120:
            title_candidates.append(line)
    title = title_candidates[0] if title_candidates else None
    years = _extract_year_candidates(text)
    return {
        "title": title,
        "authors_or_editors": [stage_item["metadata_snapshot"].get("authors", ["Koren"])[0]]
        if stage_item["metadata_snapshot"].get("authors")
        else [],
        "year": years[0] if years else stage_item["metadata_snapshot"].get("year"),
        "publisher_or_source": "The Guilford Press",
        "doi": ids.get("doi"),
        "isbn_candidates": ids.get("isbn_candidates", []),
        "document_type": "book",
        "confidence": "medium" if title and years else "low",
        "title_candidates": _bounded_unique(title_candidates, 8),
    }


def _safe_id_preview(metadata: dict[str, Any], route: str) -> dict[str, Any]:
    if metadata.get("confidence") != "medium" and metadata.get("confidence") != "high":
        return {"candidate_id": None, "confidence": "low", "reason": "metadata confidence is too low for ID preview"}
    title = metadata.get("title")
    year = metadata.get("year")
    names = metadata.get("authors_or_editors") or []
    if not title or not year or not names:
        return {"candidate_id": None, "confidence": "low", "reason": "missing title/year/author-or-editor"}
    surname = _ascii_token(str(names[0]).split()[-1])
    keyword = next((_ascii_token(w) for w in str(title).split() if len(_ascii_token(w)) > 3), "")
    if not surname or not keyword or surname.lower() in {"unknown", "publisher", "editor", "author"}:
        return {"candidate_id": None, "confidence": "low", "reason": "unsafe or synthetic ID token"}
    suffix = "book" if route == "book_parent_record_candidate" else "edited"
    return {"candidate_id": f"{surname}_{year}_{keyword}_{suffix}", "confidence": "medium", "reason": "preview only; not committed"}


def _guideline_assessment(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "default_guidelines": "current DB methodological standards/guidelines remain default where applicable",
        "topical_relevance_is_exclusion": False,
        "recommended_tag_or_route": "guideline_mismatch_or_new_guideline_needed_candidate"
        if metadata.get("document_type") in {"book", "edited_volume"}
        else "current_guidelines_probably_applicable",
        "assessment": (
            "Book/edited-volume handling may need additional book/chapter metadata rules, "
            "but the PDF should be kept and typed correctly rather than excluded."
        ),
    }


def _analysis_for_item(stage_item: dict[str, Any], full_text: str, pdf_meta: dict[str, Any]) -> dict[str, Any]:
    lines = _clean_lines(full_text)
    ids = _extract_identifiers(full_text, pdf_meta)
    toc = _detect_toc(lines)
    chapters = _detect_chapter_boundaries(lines)
    if stage_item["name"].startswith("Harris-"):
        metadata = _infer_harris_metadata(stage_item, full_text, lines, ids)
        if toc["toc_found"] or chapters:
            route = "parent_plus_child_chapter_candidates"
            representation = "Create an edited-volume parent candidate and chapter child candidates if chapter metadata validates."
        else:
            route = "edited_volume_parent_record_candidate"
            representation = "Create an edited-volume parent candidate first; rerun chapter detection if needed."
    else:
        metadata = _infer_koren_metadata(stage_item, full_text, lines, ids)
        if metadata.get("confidence") == "low":
            route = "machine_escalation_still_needed"
            representation = "Keep as a valid book candidate, but canonical title/identifier needs more machine parsing."
        elif toc["toc_found"] or chapters:
            route = "book_parent_record_candidate"
            representation = "Create a book parent candidate; child records only if chapter-level metadata becomes relevant."
        else:
            route = "book_parent_record_candidate"
            representation = "Create a book parent candidate."

    candidate_children = []
    for c in chapters[:12]:
        candidate_children.append(
            {
                "title": c["heading"],
                "line_index": c["line_index"],
                "metadata_confidence": "low",
                "id_preview": {"candidate_id": None, "confidence": "low", "reason": "chapter metadata is not validated"},
            }
        )

    return {
        "name": stage_item["name"],
        "path": stage_item["path"],
        "processed_in_stage_g": True,
        "text_profile": {
            "chars": len(full_text),
            "fingerprint": _text_fingerprint(full_text),
            "pdf_metadata": pdf_meta,
        },
        "candidate_parent_metadata": metadata,
        "toc_preview": toc,
        "chapter_boundary_preview": chapters[:20],
        "candidate_child_metadata": candidate_children,
        "recommended_representation": representation,
        "candidate_parent_id_preview": _safe_id_preview(metadata, route),
        "guideline_assessment": _guideline_assessment(metadata),
        "final_route_recommendation": route,
        "human_review_needed_now": False,
        "human_review_last_resort_conditions": [
            "machine title-page/TOC parsing cannot identify canonical metadata",
            "multiple candidate parent or child records remain equally plausible after machine comparison",
            "a future destructive operation would be ambiguous and require user approval",
        ],
        "explicit_non_actions_now": [
            "no DB row creation/update",
            "no managed PDF copy/move",
            "no safe ID commit",
            "no paid API call",
            "no evaluation",
            "no book/chapter splitting write",
        ],
    }


def run_book_toc_chapter_preview(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    stage_f = json.loads((run_dir / "final_route_plan_preview.json").read_text(encoding="utf-8"))
    target_routes = [
        route
        for route in stage_f.get("routes", [])
        if route.get("name", "").startswith(TARGET_PREFIXES)
        and route.get("final_route_type") == "book_or_edited_volume_machine_escalation"
    ]
    items = []
    for route in target_routes:
        pdf_path = Path(route["path"])
        full_text, text_error = _run_pdftotext(pdf_path, first_pages_only=False)
        pdf_meta = _read_pdf_metadata(pdf_path)
        analysis = _analysis_for_item(route, full_text, pdf_meta)
        analysis["text_profile"]["extraction_error"] = text_error
        items.append(analysis)

    skipped = [
        route.get("name")
        for route in stage_f.get("routes", [])
        if not route.get("name", "").startswith(TARGET_PREFIXES)
    ]
    summary = {
        "run_id": run_dir.name,
        "stage": "book-toc-chapter-preview",
        "source_artifact": str(run_dir / "final_route_plan_preview.json"),
        "processed_only": [item["name"] for item in items],
        "not_processed_by_design": skipped,
        "items": items,
        "safety": {
            "processed_only_harris_and_koren": {item["name"] for item in items}
            <= {route.get("name") for route in stage_f.get("routes", []) if route.get("name", "").startswith(TARGET_PREFIXES)},
            "no_topical_exclusion": True,
            "report_only": True,
        },
        "later_stages_still_disabled": [
            "DB row creation/update",
            "managed PDF copy/move",
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
            "pdf_moves_copies_renames_deletes": "none",
            "id_changes": "none",
            "book_chapter_splitting_writes": "none",
        },
    }
    (run_dir / "book_toc_chapter_preview.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "stage_book_toc_chapter_preview_report.txt").write_text(render_book_toc_chapter_report(summary), encoding="utf-8")
    return summary


def render_book_toc_chapter_report(summary: dict[str, Any]) -> str:
    lines = ["Stage G Book/TOC/Chapter Preview (READ-ONLY)", "=" * 52]
    lines.append("Policy: valid staged PDFs are kept/typed; topical relevance is not an exclusion criterion.")
    for item in summary["items"]:
        meta = item["candidate_parent_metadata"]
        lines.append(f"- {item['name']}")
        lines.append(f"  route: {item['final_route_recommendation']}")
        lines.append(f"  parent: {meta.get('title')} | {meta.get('year')} | {meta.get('document_type')} | confidence={meta.get('confidence')}")
        lines.append(f"  identifiers: DOI={meta.get('doi')} ISBNs={meta.get('isbn_candidates')}")
        lines.append(f"  TOC found: {item['toc_preview']['toc_found']} entries={len(item['toc_preview']['candidate_entries'])}")
        lines.append(f"  chapter boundaries: {len(item['chapter_boundary_preview'])}")
        lines.append(f"  parent_id_preview: {item['candidate_parent_id_preview']}")
        lines.append(f"  guideline: {item['guideline_assessment']['recommended_tag_or_route']}")
        lines.append(f"  human_review_now: {item['human_review_needed_now']}")
    lines.append("")
    lines.append("No API calls, DB writes, PDF operations, book/chapter writes, or ID changes were performed.")
    return "\n".join(lines) + "\n"
