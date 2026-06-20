#!/usr/bin/env python3
"""Corpus search helpers (structured SQLite + semantic vector)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from vector_index import semantic_search


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        out = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return out if isinstance(out, dict) else {}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _norm_result(row: sqlite3.Row) -> dict[str, Any]:
    basic = _json_obj(row["basic_metadata"])
    file_info = _json_obj(row["file_info"])
    rel = file_info.get("filepath") or file_info.get("managed_pdf_relative_path") or ""
    if isinstance(rel, str) and rel:
        abs_pdf = (config.CORPUS_STORE / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
    else:
        renamed = str(file_info.get("renamed_filename") or "")
        abs_pdf = (config.SOURCE_PDFS / renamed).resolve() if renamed else None
    return {
        "paper_id": row["id"],
        "title": basic.get("title") or "",
        "authors": basic.get("authors") or [],
        "year": basic.get("year"),
        "journal": basic.get("journal") or basic.get("venue") or "",
        "doi": basic.get("doi") or "",
        "rating": row["rating"] or "",
        "document_type": row["document_type"] or "",
        "pdf_path": str(abs_pdf) if abs_pdf else "",
    }


def structured_search(
    query: str,
    *,
    top_k: int = 25,
    min_rating: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {"ok": True, "mode": "structured", "query": query, "results": [], "result_count": 0}

    q = f"%{query.lower()}%"
    sql = (
        "SELECT id, document_type, basic_metadata, file_info, rating "
        "FROM papers WHERE ("
        "lower(id) LIKE ? OR lower(basic_metadata) LIKE ? OR lower(tags) LIKE ? OR lower(classification) LIKE ?"
        ")"
    )
    params: list[Any] = [q, q, q, q]
    if min_rating:
        sql += " AND lower(COALESCE(rating,'')) = ?"
        params.append(min_rating.lower())
    if year_from or year_to:
        sql += " AND lower(basic_metadata) LIKE ?"
        params.append("%\"year\"%")
    sql += " ORDER BY id LIMIT ?"
    params.append(int(max(1, min(top_k * 4, 250))))

    out: list[dict[str, Any]] = []
    conn = _conn()
    try:
        for row in conn.execute(sql, params).fetchall():
            rec = _norm_result(row)
            year = rec.get("year")
            try:
                yi = int(year)
            except Exception:
                yi = None
            if year_from and yi and yi < year_from:
                continue
            if year_to and yi and yi > year_to:
                continue
            out.append(rec)
            if len(out) >= top_k:
                break
    finally:
        conn.close()
    return {"ok": True, "mode": "structured", "query": query, "results": out, "result_count": len(out)}


def paper_detail(paper_id: str) -> dict[str, Any]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, timestamp_added, document_type, basic_metadata, classification, tags, file_info, rating "
            "FROM papers WHERE id = ?",
            (paper_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "paper not found"}
    base = _norm_result(row)
    base["timestamp_added"] = row["timestamp_added"]
    base["classification"] = _json_obj(row["classification"])
    base["tags"] = _json_obj(row["tags"])
    base["file_info"] = _json_obj(row["file_info"])
    return {"ok": True, "paper": base}


def run_cli() -> int:
    parser = argparse.ArgumentParser(description="Search papers corpus")
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--mode", choices=["structured", "semantic"], default="structured")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--min-rating", default="")
    parser.add_argument("--year-from", type=int, default=None)
    parser.add_argument("--year-to", type=int, default=None)
    parser.add_argument("--paper-id", default="", help="Fetch one paper detail by id")
    args = parser.parse_args()

    if args.paper_id:
        print(json.dumps(paper_detail(args.paper_id), indent=2, ensure_ascii=True))
        return 0

    if args.mode == "semantic":
        result = semantic_search(
            args.query,
            top_k=args.top,
            min_rating=args.min_rating,
            year_from=args.year_from,
            year_to=args.year_to,
        )
        if result.get("ok"):
            enriched: list[dict[str, Any]] = []
            for hit in result.get("results", []):
                detail = paper_detail(str(hit.get("paper_id")))
                paper = detail.get("paper") if detail.get("ok") else {}
                enriched.append({**hit, "pdf_path": paper.get("pdf_path", ""), "doi": paper.get("doi", "")})
            result["results"] = enriched
            result["result_count"] = len(enriched)
    else:
        result = structured_search(
            args.query,
            top_k=args.top,
            min_rating=args.min_rating,
            year_from=args.year_from,
            year_to=args.year_to,
        )
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
