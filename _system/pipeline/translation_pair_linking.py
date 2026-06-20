#!/usr/bin/env python3
"""Link rare source-language/original PDFs to translated book entries."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config


STOP_TOKENS = {
    "a",
    "ai",
    "and",
    "book",
    "english",
    "file",
    "image",
    "information",
    "language",
    "of",
    "original",
    "pdf",
    "proper",
    "russian",
    "source",
    "text",
    "the",
    "translated",
    "translation",
    "version",
}


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _tokens(*parts: Any) -> set[str]:
    text = " ".join(str(p or "") for p in parts).lower()
    raw = re.findall(r"[a-z0-9]{3,}", text)
    return {tok for tok in raw if tok not in STOP_TOKENS}


def _is_translation_row(filename: str, metadata: dict[str, Any]) -> bool:
    text = f"{filename} {metadata.get('title') or ''} {metadata.get('language') or ''}".lower()
    return "translated" in text or "translation" in text or "english version" in text


def _is_original_source_row(filename: str, metadata: dict[str, Any]) -> bool:
    text = f"{filename} {metadata.get('title') or ''} {metadata.get('language') or ''}".lower()
    return (
        ("original" in text and ("russian" in text or "source" in text or "scan" in text or "image" in text))
        or "original russian" in text
        or "source language" in text
    )


def _mark_book_not_ratable(row: sqlite3.Row, *, role: str, now: str) -> tuple[str, str, str, str]:
    bm = _json_obj(row["basic_metadata"])
    cl = _json_obj(row["classification"])
    nf = _json_obj(row["notes_and_flags"])
    cl["document_type"] = "book"
    cl["rating"] = None
    cl["rating_justification"] = "Not rated: book/reference material rather than a peer-reviewed research paper."
    nf["acceptance_action"] = "ACCEPT_REFERENCE_MATERIAL"
    nf["acceptance_reason"] = "document_type:book_translation_pair_not_ratable"
    nf["methodology_note"] = (
        "Retained as book/reference material and excluded from paper ratings and MetaCheck because "
        "it is not a peer-reviewed research paper."
    )
    nf["translation_pair_role"] = role
    nf["metacheck"] = {
        "status": "not_applicable",
        "source": "not_applicable",
        "mode": "not_applicable",
        "ran_at": now,
        "reason": "document_type:book_translation_pair_not_ratable",
    }
    return (
        json.dumps(bm, ensure_ascii=False),
        json.dumps(cl, ensure_ascii=False),
        json.dumps(nf, ensure_ascii=False),
        "book",
    )


def link_translation_pairs() -> dict[str, Any]:
    """Make translated book PDFs the main row and attach original/source scans.

    This is deliberately conservative. It only acts when filenames/metadata clearly
    indicate a translation row and an original/source-language row with overlapping
    identifying tokens.
    """
    db_path = config.DB_PATH
    now = datetime.now(timezone.utc).isoformat()
    linked: list[dict[str, Any]] = []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, document_type, rating, basic_metadata, classification, notes_and_flags, file_info, supplements "
            "FROM papers"
        ).fetchall()
        enriched: list[dict[str, Any]] = []
        for row in rows:
            bm = _json_obj(row["basic_metadata"])
            fi = _json_obj(row["file_info"])
            filename = str(fi.get("original_filename") or fi.get("renamed_filename") or fi.get("filepath") or "")
            enriched.append(
                {
                    "row": row,
                    "bm": bm,
                    "fi": fi,
                    "filename": filename,
                    "tokens": _tokens(filename, bm.get("title"), bm.get("authors"), bm.get("year")),
                }
            )

        translations = [x for x in enriched if _is_translation_row(x["filename"], x["bm"])]
        originals = [x for x in enriched if _is_original_source_row(x["filename"], x["bm"])]

        for main in translations:
            main_id = str(main["row"]["id"])
            for orig in originals:
                orig_id = str(orig["row"]["id"])
                if main_id == orig_id:
                    continue
                overlap = main["tokens"] & orig["tokens"]
                if len(overlap) < 2:
                    continue
                main_fi = main["fi"]
                orig_fi = orig["fi"]
                orig_rel = str(orig_fi.get("filepath") or orig_fi.get("managed_pdf_relative_path") or "")
                orig_filename = str(orig_fi.get("renamed_filename") or Path(orig_rel).name)
                orig_path = config.CORPUS_STORE / orig_rel if orig_rel else config.SOURCE_PDFS / orig_filename
                size_mb = round(orig_path.stat().st_size / (1024 * 1024), 3) if orig_path.exists() else None

                supplements = _json_obj(main["row"]["supplements"])
                items = supplements.get("items")
                if not isinstance(items, list):
                    items = []
                items = [
                    item
                    for item in items
                    if not (
                        isinstance(item, dict)
                        and (item.get("filename") == orig_filename or item.get("filepath") == orig_rel)
                    )
                ]
                items.append(
                    {
                        "filename": orig_filename,
                        "filepath": orig_rel,
                        "original_filename": orig_fi.get("original_filename") or orig_filename,
                        "format": "pdf",
                        "size_mb": size_mb,
                        "type": "source_language_original",
                        "description": "Original/source-language PDF linked to translated main book entry.",
                        "language": orig["bm"].get("language") or "",
                        "linked_from_prior_row_id": orig_id,
                        "added": now,
                        "child_document": True,
                    }
                )
                supplements.update(
                    {
                        "has_supplements": True,
                        "supplement_type": "linked_source_language_original",
                        "supplement_notes": "Translated PDF is the primary book entry; original/source-language PDF is linked.",
                        "items": items,
                    }
                )

                main_bm = dict(main["bm"])
                if orig["bm"].get("authors") and main_bm.get("authors") in (None, [], ["Unknown"]):
                    main_bm["authors"] = orig["bm"].get("authors")
                if orig["bm"].get("year") and not main_bm.get("year"):
                    main_bm["year"] = orig["bm"].get("year")
                main_bm["article_type"] = "book"
                main_bm["translation_status"] = "translated_from_source_language_pdf"
                main_bm["source_language"] = orig["bm"].get("language") or "source_language"

                _, main_cl, main_nf, main_doc_type = _mark_book_not_ratable(
                    main["row"], role="main_translation", now=now
                )
                main_nf_obj = _json_obj(main_nf)
                main_nf_obj["translation_linkage"] = {
                    "role": "main_translation",
                    "original_row_id_removed": orig_id,
                    "original_pdf_filename": orig_filename,
                    "original_pdf_filepath": orig_rel,
                    "linked_at": now,
                }
                conn.execute(
                    "UPDATE papers SET document_type=?, rating=?, basic_metadata=?, classification=?, notes_and_flags=?, supplements=? WHERE id=?",
                    (
                        main_doc_type,
                        None,
                        json.dumps(main_bm, ensure_ascii=False),
                        main_cl,
                        json.dumps(main_nf_obj, ensure_ascii=False),
                        json.dumps(supplements, ensure_ascii=False),
                        main_id,
                    ),
                )
                conn.execute("DELETE FROM papers WHERE id=?", (orig_id,))
                linked.append(
                    {
                        "main_id": main_id,
                        "linked_original_id": orig_id,
                        "linked_original_file": orig_rel,
                        "token_overlap": sorted(overlap),
                    }
                )
                break

        conn.commit()
        return {"ok": True, "linked_count": len(linked), "linked": linked}
    finally:
        conn.close()

