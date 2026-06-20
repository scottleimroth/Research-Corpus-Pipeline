#!/usr/bin/env python3
"""Shared search helpers for vector and structured corpus queries."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import config  # noqa: E402
import papers_db  # noqa: E402


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _full_text_string(raw: Any) -> str:
    obj = _json_obj(raw)
    if obj:
        sections = obj.get("sections")
        if isinstance(sections, dict):
            parts = [str(v) for v in sections.values() if isinstance(v, str) and v.strip()]
            if parts:
                return "\n\n".join(parts)
        return str(obj.get("text") or obj.get("full_text") or obj.get("raw_text") or "")
    return str(raw or "")


def chunk_text(text: str, *, chunk_chars: int = 1600, overlap_chars: int = 200) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    out: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + chunk_chars)
        chunk = text[start:end].strip()
        if len(chunk) >= 120:
            out.append(chunk)
        if end >= n:
            break
        start = max(start + 1, end - overlap_chars)
    return out


def paper_metadata_row(meta: dict[str, Any]) -> dict[str, Any]:
    bm = meta.get("basic_metadata") if isinstance(meta.get("basic_metadata"), dict) else {}
    cl = meta.get("classification") if isinstance(meta.get("classification"), dict) else {}
    tags = meta.get("tags") if isinstance(meta.get("tags"), dict) else {}
    authors = bm.get("authors") if isinstance(bm.get("authors"), list) else []
    file_info = meta.get("file_info") if isinstance(meta.get("file_info"), dict) else {}
    return {
        "paper_id": str(meta.get("id") or ""),
        "title": str(bm.get("title") or ""),
        "authors": [str(a) for a in authors][:10],
        "year": str(bm.get("year") or ""),
        "journal": str(bm.get("journal") or bm.get("venue") or ""),
        "doi": str(bm.get("doi") or ""),
        "rating": str(cl.get("rating") or ""),
        "document_type": str(meta.get("document_type") or ""),
        "domains": tags.get("domain") if isinstance(tags.get("domain"), list) else [],
        "methods": tags.get("method") if isinstance(tags.get("method"), list) else [],
        "source_pdf_path": str(file_info.get("filepath") or file_info.get("managed_pdf_relative_path") or ""),
    }


def get_paper_details(paper_id: str) -> dict[str, Any] | None:
    paper = papers_db.get_paper(paper_id)
    if not paper:
        return None
    meta = paper_metadata_row(paper)
    bm = paper.get("basic_metadata") if isinstance(paper.get("basic_metadata"), dict) else {}
    ak = paper.get("abstract_keywords") if isinstance(paper.get("abstract_keywords"), dict) else {}
    fi = paper.get("file_info") if isinstance(paper.get("file_info"), dict) else {}
    rel = str(fi.get("filepath") or fi.get("managed_pdf_relative_path") or "")
    pdf_path = (config.CORPUS_STORE / rel).resolve() if rel else None
    if not pdf_path or not pdf_path.exists():
        renamed = fi.get("renamed_filename")
        if renamed:
            alt = (config.SOURCE_PDFS / str(renamed)).resolve()
            pdf_path = alt if alt.exists() else pdf_path
    return {
        **meta,
        "abstract": str(ak.get("abstract") or ""),
        "keywords": ak.get("keywords") if isinstance(ak.get("keywords"), list) else [],
        "classification": paper.get("classification") if isinstance(paper.get("classification"), dict) else {},
        "tags": paper.get("tags") if isinstance(paper.get("tags"), dict) else {},
        "pdf_path": str(pdf_path) if pdf_path else "",
    }


def semantic_index_rows() -> list[dict[str, Any]]:
    rows = papers_db.get_all_metadata()
    out: list[dict[str, Any]] = []
    for meta in rows:
        paper_id = str(meta.get("id") or "")
        if not paper_id:
            continue
        ft = papers_db.load_fulltext(paper_id)
        text = _full_text_string(ft)
        if not text.strip():
            continue
        out.append({"paper_id": paper_id, "meta": paper_metadata_row(meta), "text": text})
    return out


def structured_search(query: str, *, top: int = 20) -> list[dict[str, Any]]:
    needle = (query or "").strip().lower()
    if not needle:
        return []
    all_rows = papers_db.get_all_metadata()
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in all_rows:
        meta = paper_metadata_row(row)
        hay = " ".join(
            [
                meta["title"],
                " ".join(meta["authors"]),
                meta["year"],
                meta["journal"],
                meta["doi"],
                meta["rating"],
                " ".join(meta["domains"]),
                " ".join(meta["methods"]),
                str(row.get("id") or ""),
            ]
        ).lower()
        if needle not in hay:
            continue
        score = 3 if needle in meta["title"].lower() else 1
        if needle in " ".join(meta["authors"]).lower():
            score += 2
        scored.append((score, meta))
    scored.sort(key=lambda x: (-x[0], x[1]["title"]))
    return [m for _, m in scored[:top]]


def db_count() -> int:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
    finally:
        conn.close()
