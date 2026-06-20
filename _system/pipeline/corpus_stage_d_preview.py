"""Stage D: read-only text/metadata preview for a planned pilot run."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import config
from corpus_prescan import classify_document_type

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)\b", re.I)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
SYNTHETIC_AUTHOR_TOKENS = {
    "unknown",
    "author",
    "authors",
    "paper",
    "article",
    "review",
    "editor",
    "editors",
    "publisher",
    "book",
    "chapter",
    "orphan",
    "anonymous",
}


def _run_pdftotext(pdf_path: Path, *, first_pages_only: bool = False) -> tuple[str, str]:
    cmd = ["pdftotext"]
    if first_pages_only:
        cmd += ["-f", "1", "-l", "2"]
    cmd += [str(pdf_path), "-"]
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=90 if not first_pages_only else 30,
            check=False,
        )
        if p.returncode != 0:
            return p.stdout or "", (p.stderr or f"pdftotext_exit_{p.returncode}").strip()
        return p.stdout or "", ""
    except FileNotFoundError:
        return "", "pdftotext_not_found"
    except subprocess.TimeoutExpired:
        return "", "pdftotext_timeout"
    except Exception as e:  # pragma: no cover - defensive preview path
        return "", f"pdftotext_error:{e}"


def _read_pdf_metadata(pdf_path: Path) -> dict[str, Any]:
    from pdf_content_guard import read_pdf_metadata_safely

    return read_pdf_metadata_safely(pdf_path, tier="stage_d_pdf_metadata")


def _clean_doi(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw.rstrip(".,;)]}").lower()


def _extract_doi(text: str) -> str | None:
    m = DOI_RE.search(text or "")
    return _clean_doi(m.group(1)) if m else None


def _filename_metadata(name: str) -> dict[str, Any]:
    stem = Path(name).stem
    years = re.findall(r"(?:19|20)\d{2}", stem)
    year = years[-1] if years else None
    authors = []
    title = None
    source = None
    if "-" in stem:
        parts = [p.strip(" _") for p in stem.split("-") if p.strip(" _")]
        if parts:
            author_token = parts[0]
            if author_token and not author_token.lower().startswith("author"):
                authors = [author_token]
        if len(parts) >= 2:
            title = parts[1]
        if len(parts) >= 4:
            source = parts[3]
    return {"title": title, "authors": authors, "year": year, "source": source}


def _pdf_title_is_usable(title: str | None) -> bool:
    if not title:
        return False
    low = title.lower()
    if low.endswith((".indd", ".pdf")) or "print.indd" in low:
        return False
    if re.fullmatch(r"[\d_\- ]{4,}", title):
        return False
    return len(title.strip()) >= 8


def _title_from_first_pages(first_pages: str) -> str | None:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in first_pages.splitlines()]
    lines = [ln for ln in lines if len(ln) >= 12 and not DOI_RE.search(ln)]
    for ln in lines[:30]:
        low = ln.lower()
        if any(skip in low for skip in ("copyright", "downloaded", "http", "journal homepage")):
            continue
        if low in {"ebook", "abstract", "keywords"}:
            continue
        if len(ln) <= 180:
            return ln
    return None


def _year_from_text(text: str) -> str | None:
    years = re.findall(r"(?:19|20)\d{2}", text[:5000])
    return years[0] if years else None


def _authors_from_metadata(meta: dict[str, Any], filename_meta: dict[str, Any]) -> list[str]:
    author = meta.get("author")
    if author and len(author) < 200:
        parts = re.split(r";|\band\b|,", author)
        authors = [p.strip() for p in parts if p.strip()]
        if authors:
            return authors[:10]
    return filename_meta.get("authors", [])[:5]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _surname(author: str) -> str:
    parts = (author or "").split()
    return parts[-1] if parts else ""


def _ascii_token(s: str) -> str:
    normalized = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^A-Za-z0-9]", "", normalized.encode("ascii", "ignore").decode("ascii"))


def _candidate_id(metadata: dict[str, Any], conflicts: list[str]) -> dict[str, Any]:
    if metadata.get("document_type") not in {"journal_article", "review_article", "protocol"}:
        return {"candidate_id": None, "confidence": "low", "reason": "non-article document type"}
    if any("title differs" in c or "year" in c for c in conflicts):
        return {"candidate_id": None, "confidence": "low", "reason": "metadata conflict requires machine-first resolution"}
    authors = metadata.get("authors") or []
    year = metadata.get("year")
    title = metadata.get("title")
    if not authors or not year or not title:
        return {"candidate_id": None, "confidence": "low", "reason": "missing author/year/title"}
    surname = _ascii_token(_surname(authors[0]))
    if not surname or surname.lower() in SYNTHETIC_AUTHOR_TOKENS or len(surname) < 3:
        return {"candidate_id": None, "confidence": "low", "reason": "unsafe or synthetic author token"}
    words = [
        _ascii_token(w)
        for w in str(title).split()
        if len(w) > 3 and w.lower() not in {"with", "from", "that", "this", "their", "about", "between", "through"}
    ]
    keyword = next((w for w in words if w), "")
    if not keyword:
        return {"candidate_id": None, "confidence": "low", "reason": "no usable title keyword"}
    candidate = f"{surname}_{year}_{keyword}"
    if "unknown" in candidate.lower():
        return {"candidate_id": None, "confidence": "low", "reason": "unsafe generated ID"}
    return {"candidate_id": candidate, "confidence": "medium", "reason": "preview only; not committed"}


def _db_metadata_rows() -> list[dict[str, Any]]:
    if not config.DB_PATH.exists():
        return []
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, basic_metadata, file_info FROM papers").fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            bm = json.loads(row["basic_metadata"]) if row["basic_metadata"] else {}
        except json.JSONDecodeError:
            bm = {}
        try:
            fi = json.loads(row["file_info"]) if row["file_info"] else {}
        except json.JSONDecodeError:
            fi = {}
        out.append({"id": row["id"], "basic_metadata": bm, "file_info": fi})
    return out


def _duplicate_reassessment(
    pdf_plan: dict[str, Any],
    metadata: dict[str, Any],
    db_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    signals: list[str] = []
    doi = metadata.get("doi")
    title = metadata.get("title") or ""
    year = str(metadata.get("year") or "")
    authors = metadata.get("authors") or []
    surname = _surname(authors[0]).lower() if authors else ""
    title_norm = _norm(title)
    for row in db_rows:
        bm = row["basic_metadata"]
        fi = row["file_info"]
        if pdf_plan["name"].lower() in {
            str(fi.get("original_filename", "")).lower(),
            str(fi.get("renamed_filename", "")).lower(),
        }:
            signals.append(f"filename_match:{row['id']}")
            break
        if doi and doi == str(bm.get("doi", "")).lower().strip():
            signals.append(f"doi_match:{row['id']}")
            break
        db_title = str(bm.get("title") or "")
        db_year = str(bm.get("year") or "")
        if title_norm and len(title_norm) > 20 and db_title:
            ratio = SequenceMatcher(None, title_norm, _norm(db_title)).ratio()
            if ratio >= 0.90 and (not year or not db_year or year == db_year):
                signals.append(f"title_year_similarity_{int(ratio * 100)}:{row['id']}")
                break
        if surname and db_year and year and db_year == year:
            db_authors = bm.get("authors") or []
            db_surnames = {_surname(str(a)).lower() for a in db_authors if a}
            if surname in db_surnames and title_norm and db_title:
                ratio = SequenceMatcher(None, title_norm, _norm(db_title)).ratio()
                if ratio >= 0.80:
                    signals.append(f"author_year_title_similarity_{int(ratio * 100)}:{row['id']}")
                    break
    return {
        "status": "possible_duplicate" if signals else "no_duplicate_signal",
        "signals": signals,
        "route": "machine_first_duplicate_escalation" if signals else "normal_automated_processing",
    }


def _classify_with_evidence(filename: str, pdf_meta: dict[str, Any], first_pages: str, full_text: str) -> tuple[str, list[str]]:
    base_type, reasons = classify_document_type(filename)
    evidence = " ".join(
        str(x or "")
        for x in (
            filename,
            pdf_meta.get("title"),
            pdf_meta.get("subject"),
            first_pages[:5000],
        )
    ).lower()
    pages = int(pdf_meta.get("pages") or 0)
    book_signals = []
    if pages >= 300:
        book_signals.append(f"pdf has {pages} pages")
    if "ebook" in first_pages.lower()[:1000]:
        book_signals.append("first pages contain ebook marker")
    if "series editors" in evidence or " editors " in f" {evidence[:2000]} ":
        book_signals.append("first pages contain editor/series-editor markers")
    if any(x in evidence for x in ("guilford press", "springer nature", "isbn", "studies in neuroscience")):
        book_signals.append("publisher/book-series clue")
    if "10.1007/978-" in evidence or "978-" in evidence[:5000]:
        book_signals.append("book DOI/ISBN-like 978 clue")
    if book_signals:
        doc_type = "edited_volume" if any("editor" in s for s in book_signals) else "book"
        return doc_type, reasons + book_signals
    if any(x in evidence for x in ("book chapter", "chapter in", "in:")):
        return "book_chapter", reasons + ["text/metadata suggests chapter"]
    if any(x in evidence for x in ("edited by", "handbook", "isbn")):
        return "edited_volume" if "edited by" in evidence else "book", reasons + ["text/metadata suggests book/edited volume"]
    if "supplement" in evidence or "supplementary" in evidence:
        return "supplement", reasons + ["text/metadata suggests supplement"]
    if "protocol" in evidence[:3000]:
        return "protocol", reasons + ["text suggests protocol"]
    if "review" in evidence[:3000] or "best practices" in evidence[:3000]:
        return "review_article", reasons + ["text suggests review/guideline article"]
    if base_type in {"journal_article", "review_article", "protocol"}:
        return base_type, reasons
    return base_type, reasons


def _metadata_conflicts(filename_meta: dict[str, Any], pdf_meta: dict[str, Any], preview: dict[str, Any]) -> list[str]:
    conflicts = []
    if filename_meta.get("year") and preview.get("year") and filename_meta["year"] != preview["year"]:
        conflicts.append(f"filename_year={filename_meta['year']} differs from extracted_year={preview['year']}")
    if filename_meta.get("title") and preview.get("title"):
        ratio = SequenceMatcher(None, _norm(filename_meta["title"]), _norm(preview["title"])).ratio()
        if ratio < 0.45:
            conflicts.append("filename title differs from extracted/PDF title")
    pdf_subject_year = _year_from_text(str(pdf_meta.get("subject") or ""))
    if filename_meta.get("year") and pdf_subject_year and filename_meta["year"] != pdf_subject_year:
        conflicts.append(f"filename_year={filename_meta['year']} differs from pdf_subject_year={pdf_subject_year}")
    if not preview.get("authors"):
        conflicts.append("missing_or_ambiguous_authors")
    if not preview.get("doi"):
        conflicts.append("no_doi_found")
    if preview.get("document_type") not in {"journal_article", "review_article", "protocol"}:
        conflicts.append(f"source appears {preview.get('document_type')} rather than standalone journal article")
    if preview.get("ocr_needed"):
        conflicts.append("ocr_needed_or_text_extraction_poor")
    return conflicts


def run_text_metadata_preview(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    db_rows = _db_metadata_rows()
    items = []
    for pdf_plan in plan.get("pdf_plans", []):
        pdf_path = Path(pdf_plan["path"])
        first_pages, first_err = _run_pdftotext(pdf_path, first_pages_only=True)
        full_text, full_err = _run_pdftotext(pdf_path, first_pages_only=False)
        pdf_meta = _read_pdf_metadata(pdf_path)
        filename_meta = _filename_metadata(pdf_plan["name"])
        pdf_title = pdf_meta.get("title") if _pdf_title_is_usable(pdf_meta.get("title")) else None
        title = pdf_title or _title_from_first_pages(first_pages) or filename_meta.get("title")
        doi = _extract_doi(first_pages) or _extract_doi(full_text[:20_000])
        year = filename_meta.get("year") or _year_from_text(first_pages)
        authors = _authors_from_metadata(pdf_meta, filename_meta)
        doc_type, doc_reasons = _classify_with_evidence(pdf_plan["name"], pdf_meta, first_pages, full_text)
        ocr_needed = len(full_text.strip()) < 500
        metadata = {
            "title": title,
            "authors": authors,
            "year": year,
            "doi": doi,
            "journal_or_source": filename_meta.get("source"),
            "document_type": doc_type,
            "document_type_reasons": doc_reasons,
            "publisher_or_source_clues": {
                "pdf_creator": pdf_meta.get("creator"),
                "pdf_producer": pdf_meta.get("producer"),
                "filename_source": filename_meta.get("source"),
            },
            "confidence": "medium" if title and (authors or doi or year) and not ocr_needed else "low",
        }
        conflicts = _metadata_conflicts(filename_meta, pdf_meta, {**metadata, "ocr_needed": ocr_needed})
        duplicate = _duplicate_reassessment(pdf_plan, metadata, db_rows)
        if duplicate["status"] == "possible_duplicate":
            candidate_id = {
                "candidate_id": None,
                "confidence": "low",
                "reason": "possible duplicate requires machine-first duplicate resolution",
            }
        else:
            candidate_id = _candidate_id(metadata, conflicts)
        suitable = (
            metadata["document_type"] in {"journal_article", "review_article", "protocol"}
            and not ocr_needed
            and duplicate["status"] == "no_duplicate_signal"
            and not conflicts
            and not any(c.startswith("source appears") for c in conflicts)
        )
        items.append(
            {
                "name": pdf_plan["name"],
                "path": pdf_plan["path"],
                "text": {
                    "first_pages_chars": len(first_pages),
                    "full_text_chars": len(full_text),
                    "first_pages_error": first_err,
                    "full_text_error": full_err,
                    "first_page_snippet": first_pages[:1200],
                    "ocr_needed": ocr_needed,
                },
                "filename_metadata": filename_meta,
                "pdf_metadata": pdf_meta,
                "metadata_preview": metadata,
                "classification_changed_from_plan": metadata["document_type"] != pdf_plan.get("document_type_classification"),
                "metadata_conflicts": conflicts,
                "duplicate_reassessment": duplicate,
                "candidate_id_preview": candidate_id,
                "suitable_for_next_stage_ingest": suitable,
                "machine_first_next_step": "normal_ingest_preview_next" if suitable else "machine_first_metadata_or_duplicate_escalation",
            }
        )

    summary = {
        "run_id": run_dir.name,
        "stage": "text-metadata-preview",
        "items": items,
        "side_effects": {
            "api_calls": "none",
            "papers_db_writes": "none",
            "pdf_moves_copies_renames_deletes": "none",
            "id_changes": "none",
        },
    }
    (run_dir / "text_metadata_preview.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "stage_text_metadata_preview_report.txt").write_text(render_text_metadata_report(summary), encoding="utf-8")
    return summary


def render_text_metadata_report(summary: dict[str, Any]) -> str:
    lines = ["Stage D Text/Metadata Preview (READ-ONLY)", "=" * 48]
    for item in summary["items"]:
        meta = item["metadata_preview"]
        text = item["text"]
        lines.append(f"- {item['name']}")
        lines.append(f"  text: full={text['full_text_chars']} chars, first_pages={text['first_pages_chars']} chars, ocr_needed={text['ocr_needed']}")
        lines.append(f"  title: {meta.get('title')}")
        lines.append(f"  authors: {meta.get('authors')}")
        lines.append(f"  year/doi/source: {meta.get('year')} / {meta.get('doi')} / {meta.get('journal_or_source')}")
        lines.append(f"  document_type: {meta.get('document_type')} (changed={item['classification_changed_from_plan']})")
        lines.append(f"  duplicate: {item['duplicate_reassessment']['status']} {item['duplicate_reassessment']['signals']}")
        lines.append(f"  candidate_id: {item['candidate_id_preview']}")
        lines.append(f"  suitable_next: {item['suitable_for_next_stage_ingest']}")
        if item["metadata_conflicts"]:
            lines.append(f"  conflicts: {item['metadata_conflicts']}")
    lines.append("")
    lines.append("No API calls, DB writes, PDF operations, or ID changes were performed.")
    return "\n".join(lines) + "\n"
