"""Preview a rebuildable local vector DB pipeline from papers.db full_text.

This scaffold is intentionally dry-run by default. It treats papers.db as the
source of truth and `CorpusStore/vector-db` as rebuildable derived data.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

DEFAULT_VECTOR_DB_DIR = config.MASTER_PROJECT / "vector-db"
DEFAULT_CHUNK_CHARS = 1800
DEFAULT_OVERLAP_CHARS = 250


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


def _text(raw: Any) -> str:
    obj = _json_obj(raw)
    if obj:
        return str(obj.get("text") or obj.get("full_text") or obj.get("content") or "")
    return str(raw or "")


def _estimate_chunks(text_len: int, chunk_chars: int, overlap_chars: int) -> int:
    if text_len <= 0:
        return 0
    step = max(1, chunk_chars - overlap_chars)
    return max(1, (max(0, text_len - overlap_chars) + step - 1) // step)


def inspect_db(limit: int | None, chunk_chars: int, overlap_chars: int) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = "SELECT id, document_type, basic_metadata, full_text FROM papers ORDER BY id"
    if limit:
        sql += " LIMIT ?"
    try:
        rows = conn.execute(sql, (limit,)).fetchall() if limit else conn.execute(sql).fetchall()
    finally:
        conn.close()

    materialized = 0
    estimated_chunks = 0
    examples: list[dict[str, Any]] = []
    for row in rows:
        text_len = len(_text(row["full_text"]))
        if text_len <= 0:
            continue
        materialized += 1
        chunks = _estimate_chunks(text_len, chunk_chars, overlap_chars)
        estimated_chunks += chunks
        if len(examples) < 5:
            basic = _json_obj(row["basic_metadata"])
            examples.append({"id": row["id"], "title": basic.get("title"), "text_chars": text_len, "estimated_chunks": chunks})
    return {
        "selected_rows": len(rows),
        "materialized_rows": materialized,
        "estimated_chunks": estimated_chunks,
        "examples": examples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview local vector DB rebuild from papers.db full_text")
    parser.add_argument("--vector-dir", default=str(DEFAULT_VECTOR_DB_DIR))
    parser.add_argument("--embedding-backend", default="sentence-transformers", choices=["sentence-transformers", "ollama"])
    parser.add_argument("--embedding-model", default="all-MiniLM-L6-v2")
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    parser.add_argument("--overlap-chars", type=int, default=DEFAULT_OVERLAP_CHARS)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Preview only; no vector DB files are written")
    args = parser.parse_args()

    if not args.dry_run:
        raise SystemExit("Vector DB build is scaffolded only. Re-run with --dry-run for the safe command preview.")

    inspection = inspect_db(args.limit, args.chunk_chars, args.overlap_chars)
    result = {
        "ok": True,
        "mode": "dry_run",
        "source": str(config.DB_PATH),
        "vector_dir": args.vector_dir,
        "vector_db_is_rebuildable": True,
        "source_of_truth": "papers.db full_text",
        "embedding_backend": args.embedding_backend,
        "embedding_model": args.embedding_model,
        "chunk_chars": args.chunk_chars,
        "overlap_chars": args.overlap_chars,
        "inspection": inspection,
        "future_build_command": (
            "DO NOT RUN YET: python pipeline\\build_vector_db.py "
            "--source papers.db --vector-dir CorpusStore\\vector-db "
            "--embedding-backend local --rebuild"
        ),
        "side_effects": {"db_writes": "none", "pdf_operations": "none", "api_calls": "none", "evaluation": "none"},
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
