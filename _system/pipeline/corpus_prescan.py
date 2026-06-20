"""Pre-scan stage for one-command corpus pipeline (read-only)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import config

PDF_EXTENSIONS = {".pdf"}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _likely_text_available(path: Path) -> bool:
    return path.stat().st_size > 50_000


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def classify_document_type(filename: str) -> tuple[str, list[str]]:
    """Lightweight filename-based type classification for pilot selection."""
    low = filename.lower()
    reasons: list[str] = []
    supplement_markers = (
        "supplement",
        "supplementary",
        "_supp",
        "-supp",
        "appendix",
        "mmc",
        "supporting information",
    )
    if any(m in low for m in supplement_markers):
        reasons.append("supplement/addendum filename marker")
        return "supplement", reasons

    thesis_markers = ("thesis", "dissertation")
    if any(m in low for m in thesis_markers):
        reasons.append("thesis/dissertation filename marker")
        return "thesis", reasons

    book_markers = (
        "handbook",
        "cambridge",
        "oxford",
        "routledge",
        "nato asi",
        "series",
        "isbn",
        "press",
        "crc-press",
        "crc press",
        "rapid interpretation",
        "principles and practice",
        "current topics",
        "current top",
        "edited by",
        "(editor",
        "_editor",
    )
    if any(m in low for m in book_markers):
        reasons.append("book/handbook/series/editor filename marker")
        if "chapter" in low:
            return "book_chapter", reasons
        if "editor" in low or "(editor" in low:
            return "edited_volume", reasons
        return "book", reasons

    abstract_markers = ("poster", "conference abstract", "abstract book")
    if any(m in low for m in abstract_markers):
        reasons.append("poster/abstract filename marker")
        return "poster_or_abstract", reasons

    if "author)" in low or low.startswith("author"):
        reasons.append("placeholder/synthetic author filename marker")
        return "other_or_unknown", reasons

    protocol_markers = ("protocol", "registered report")
    if any(m in low for m in protocol_markers):
        reasons.append("protocol filename marker")
        return "protocol", reasons

    # "Author - Title (1998)" and publisher-like names are usually books.
    if re.search(r"\s-\s.+\((19|20)\d{2}\)", filename):
        reasons.append("author-title-(year) book-like filename structure")
        return "book", reasons

    if "review" in low and re.search(r"[-_](19|20)\d{2}[-_][a-z]", low):
        reasons.append("review + year filename marker")
        return "review_article", reasons

    journal_like = (
        bool(re.search(r"[-_](19|20)\d{2}[-_][a-z]", low))
        and (
            "-" in filename
            or "_" in filename
            or low.startswith("1-s2")
            or low.startswith("s0")
            or "doi" in low
        )
    )
    if journal_like:
        reasons.append("year + article-like filename structure")
        return "journal_article", reasons

    reasons.append("insufficient lightweight evidence")
    return "other_or_unknown", reasons


def _db_signal_index(db_path: Path) -> dict[str, Any]:
    """Read-only DB/source index for pilot duplicate exclusion."""
    index: dict[str, Any] = {
        "filenames": set(),
        "title_norms": [],
        "source_size_index": {},
    }
    if db_path.exists():
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = c.execute("SELECT basic_metadata, file_info FROM papers").fetchall()
            for bm_raw, fi_raw in rows:
                try:
                    fi = json.loads(fi_raw) if fi_raw else {}
                except json.JSONDecodeError:
                    fi = {}
                for key in ("original_filename", "renamed_filename"):
                    val = fi.get(key)
                    if val:
                        index["filenames"].add(val.lower())
                try:
                    bm = json.loads(bm_raw) if bm_raw else {}
                except json.JSONDecodeError:
                    bm = {}
                title = bm.get("title")
                year = bm.get("year")
                if title:
                    index["title_norms"].append((_norm(str(title)), str(year or "")))
        finally:
            c.close()

    if config.SOURCE_PDFS.exists():
        for p in config.SOURCE_PDFS.glob("*.pdf"):
            try:
                index["source_size_index"].setdefault(p.stat().st_size, []).append(p)
            except OSError:
                continue
    return index


def _already_in_db_signals(path: Path, sha256: str, doc_type: str, db_index: dict[str, Any]) -> list[str]:
    signals: list[str] = []
    low_name = path.name.lower()
    if low_name in db_index["filenames"]:
        signals.append("filename_in_db")

    # Hash compare only against managed PDFs with identical byte size (read-only, bounded).
    same_size = db_index["source_size_index"].get(path.stat().st_size, [])
    for candidate in same_size[:25]:
        try:
            if _sha256(candidate) == sha256:
                signals.append(f"hash_matches_managed_pdf:{candidate.name}")
                break
        except OSError:
            continue

    # Lightweight title/year signal from filename only; does not perform metadata extraction.
    stem_norm = _norm(path.stem)
    years = re.findall(r"(19|20)\d{2}", path.stem)
    year_hint = years[0] if years else ""
    if doc_type in {"journal_article", "review_article", "protocol"} and len(stem_norm) > 20:
        for title_norm, db_year in db_index["title_norms"]:
            if len(title_norm) < 20:
                continue
            if title_norm in stem_norm or stem_norm in title_norm:
                if not year_hint or not db_year or year_hint == db_year:
                    signals.append("title_year_metadata_match")
                    break
    return signals


def _scan_staging_pdfs(staging_dir: Path, db_path: Path) -> list[dict[str, Any]]:
    if not staging_dir.exists():
        return []
    db_index = _db_signal_index(db_path)
    files = [p for p in sorted(staging_dir.iterdir()) if p.is_file() and p.suffix.lower() in PDF_EXTENSIONS]
    rows = []
    for p in files:
        sha = _sha256(p)
        doc_type, doc_reasons = classify_document_type(p.name)
        db_signals = _already_in_db_signals(p, sha, doc_type, db_index)
        rows.append(
            {
                "path": str(p),
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "text_likely": _likely_text_available(p),
                "sha256": sha,
                "document_type_classification": doc_type,
                "document_type_reasons": doc_reasons,
                "already_in_db_signals": db_signals,
                "already_in_db": bool(db_signals),
                "duplicate_confidence": _duplicate_confidence(db_signals),
            }
        )
    return rows


def _duplicate_confidence(signals: list[str]) -> str:
    if any(s.startswith("hash_matches_managed_pdf:") for s in signals):
        return "confirmed"
    if signals:
        return "ambiguous"
    return "none"


def _likely_duplicate_groups(rows: list[dict[str, Any]]) -> tuple[int, list[list[str]]]:
    by_hash: dict[str, list[str]] = {}
    for r in rows:
        by_hash.setdefault(r["sha256"], []).append(r["name"])
    groups = [names for names in by_hash.values() if len(names) > 1]
    return len(groups), groups[:10]


def _detect_existing_db(db_path: Path) -> bool:
    return db_path.exists() and db_path.stat().st_size > 0


def _db_evaluated_count(db_path: Path) -> tuple[int, int]:
    if not _detect_existing_db(db_path):
        return 0, 0
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        total = c.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        rated = c.execute("SELECT COUNT(*) FROM papers WHERE rating IS NOT NULL AND rating != ''").fetchone()[0]
        return total, rated
    finally:
        c.close()


def run_prescan(*, staging_dir: str = "", db_path: str = "", blank_corpus: bool = False) -> dict[str, Any]:
    staging = Path(staging_dir).resolve() if staging_dir else config.STAGING.resolve()
    db = Path(db_path).resolve() if db_path else config.DB_PATH.resolve()

    pdf_rows = _scan_staging_pdfs(staging, db)
    dup_count, dup_examples = _likely_duplicate_groups(pdf_rows)
    total_db, rated_db = _db_evaluated_count(db)

    return {
        "staging_dir": str(staging),
        "db_path": str(db),
        "pdf_count": len(pdf_rows),
        "text_likely_count": sum(1 for r in pdf_rows if r["text_likely"]),
        "likely_duplicate_groups": dup_count,
        "likely_duplicate_examples": dup_examples,
        "existing_db_detected": _detect_existing_db(db),
        "blank_db_mode": bool(blank_corpus or not _detect_existing_db(db)),
        "db_total_rows": total_db,
        "db_rated_rows": rated_db,
        "pdfs": pdf_rows,
    }
