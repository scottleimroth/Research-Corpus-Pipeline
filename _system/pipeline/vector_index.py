#!/usr/bin/env python3
"""Portable sentence-transformers vector index for papers.db."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chromadb
from sentence_transformers import SentenceTransformer

import config

MANIFEST_VERSION = 2
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_CHUNK_CHARS = 1400
DEFAULT_OVERLAP_CHARS = 180
DEFAULT_BATCH_SIZE = 64
DEFAULT_PROGRESS_SECONDS = 30

VECTOR_ROOT = config.VECTOR_DB_DIR
MODEL_CACHE_DIR = config.SYSTEM_ROOT / "models" / "sentence-transformers"
MANIFEST_PATH = VECTOR_ROOT / "index_manifest.json"
CHROMA_DIR = VECTOR_ROOT / "chroma"


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


def _text_from_full_text(raw: Any) -> str:
    ft = _json_obj(raw)
    if not ft:
        return str(raw or "")
    sections = ft.get("sections")
    if isinstance(sections, dict):
        parts: list[str] = []
        for sec_name, sec_text in sections.items():
            if not isinstance(sec_text, str):
                continue
            sec_text = sec_text.strip()
            if not sec_text:
                continue
            parts.append(f"{sec_name}\n{sec_text}")
        if parts:
            return "\n\n".join(parts)
    raw_text = ft.get("raw_text")
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text
    for key in ("text", "full_text", "content"):
        val = ft.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _chunk_text(text: str, chunk_chars: int, overlap_chars: int) -> list[str]:
    text = (text or "").strip()
    if len(text) < 80:
        return []
    chunks: list[str] = []
    start = 0
    step = max(1, chunk_chars - overlap_chars)
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        chunk = text[start:end].strip()
        if len(chunk) >= 80:
            chunks.append(chunk)
        if end >= len(text):
            break
        start += step
    return chunks


def _safe_model_id(model_name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in model_name.lower()).strip("_")


def _collection_name(model_name: str) -> str:
    return f"papers_{_safe_model_id(model_name)}"


@dataclass
class VectorBuildConfig:
    model_name: str = DEFAULT_MODEL
    chunk_chars: int = DEFAULT_CHUNK_CHARS
    overlap_chars: int = DEFAULT_OVERLAP_CHARS
    batch_size: int = DEFAULT_BATCH_SIZE
    fresh: bool = False
    limit: int | None = None
    progress: bool = True
    progress_interval_seconds: int = DEFAULT_PROGRESS_SECONDS

    @property
    def collection_name(self) -> str:
        return _collection_name(self.model_name)


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def write_manifest(data: dict[str, Any]) -> None:
    VECTOR_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(MANIFEST_PATH)


def _manifest_compatible(manifest: dict[str, Any], cfg: VectorBuildConfig) -> bool:
    return (
        manifest.get("version") == MANIFEST_VERSION
        and manifest.get("backend") == "sentence-transformers"
        and manifest.get("model") == cfg.model_name
        and int(manifest.get("chunk_chars") or 0) == cfg.chunk_chars
        and int(manifest.get("overlap_chars") or 0) == cfg.overlap_chars
        and manifest.get("collection") == cfg.collection_name
    )


def _load_rows(limit: int | None = None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT id, basic_metadata, classification, tags, full_text, abstract_keywords, notes_and_flags "
        "FROM papers ORDER BY id"
    )
    params: tuple[Any, ...] = ()
    if limit:
        sql += " LIMIT ?"
        params = (int(limit),)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _paper_meta(row: sqlite3.Row) -> dict[str, Any]:
    basic = _json_obj(row["basic_metadata"])
    cl = _json_obj(row["classification"])
    tags = _json_obj(row["tags"])
    metacheck = _json_obj(_json_obj(row["notes_and_flags"]).get("metacheck"))
    domains = tags.get("domain") if isinstance(tags, dict) else None
    if not isinstance(domains, list):
        domains = []
    authors = basic.get("authors")
    if not isinstance(authors, list):
        authors = []
    modules = metacheck.get("modules")
    if not isinstance(modules, list):
        modules = []
    return {
        "paper_id": str(row["id"]),
        "title": str(basic.get("title") or "")[:500],
        "authors": "; ".join(str(a) for a in authors[:6])[:700],
        "year": str(basic.get("year") or ""),
        "journal": str(basic.get("journal") or basic.get("venue") or "")[:240],
        "rating": str(cl.get("rating") or ""),
        "domains": ", ".join(str(x) for x in domains[:8])[:500],
        "metacheck_status": str(metacheck.get("status") or ""),
        "metacheck_source": str(metacheck.get("source") or ""),
        "metacheck_mode": str(metacheck.get("mode") or ""),
        "metacheck_advanced_status": str(metacheck.get("advanced_status") or ""),
        "metacheck_reason": str(metacheck.get("reason") or "")[:500],
        "metacheck_modules": ", ".join(str(x) for x in modules[:20])[:800],
    }


def _metacheck_chunk(row: sqlite3.Row) -> str:
    metacheck = _json_obj(_json_obj(row["notes_and_flags"]).get("metacheck"))
    if not metacheck:
        return ""
    modules = metacheck.get("modules")
    if not isinstance(modules, list):
        modules = []
    parts = [
        "MetaCheck evidence",
        f"status: {metacheck.get('status') or 'unknown'}",
        f"source: {metacheck.get('source') or 'unknown'}",
        f"mode: {metacheck.get('mode') or 'unknown'}",
    ]
    if metacheck.get("advanced_status"):
        parts.append(f"advanced status: {metacheck.get('advanced_status')}")
    if metacheck.get("reason"):
        parts.append(f"reason: {metacheck.get('reason')}")
    if modules:
        parts.append("modules run: " + ", ".join(str(x) for x in modules[:20]))
    return "; ".join(parts)


def _paper_chunks(row: sqlite3.Row, cfg: VectorBuildConfig) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    metacheck_text = _metacheck_chunk(row)
    if len(metacheck_text) > 60:
        chunks.append(("metacheck", metacheck_text))
    abstract = _json_obj(row["abstract_keywords"]).get("abstract")
    if isinstance(abstract, str) and len(abstract.strip()) > 60:
        chunks.append(("abstract", abstract.strip()))
    full_text = _text_from_full_text(row["full_text"])
    for idx, chunk in enumerate(_chunk_text(full_text, cfg.chunk_chars, cfg.overlap_chars)):
        chunks.append((f"chunk_{idx}", chunk))
    return chunks


def _load_embedder(model_name: str) -> SentenceTransformer:
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return SentenceTransformer(model_name, cache_folder=str(MODEL_CACHE_DIR))


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def build_or_update(cfg: VectorBuildConfig) -> dict[str, Any]:
    started = time.time()
    VECTOR_ROOT.mkdir(parents=True, exist_ok=True)
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest()
    use_existing = (not cfg.fresh) and _manifest_compatible(manifest, cfg)
    indexed_ids = set(manifest.get("indexed_paper_ids") or []) if use_existing else set()

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    if cfg.fresh:
        try:
            client.delete_collection(cfg.collection_name)
        except Exception:
            pass
    collection = client.get_or_create_collection(
        name=cfg.collection_name,
        metadata={"backend": "sentence-transformers", "model": cfg.model_name},
    )
    if cfg.fresh:
        indexed_ids = set()

    rows = _load_rows(cfg.limit)
    pending_rows = [row for row in rows if str(row["id"]) not in indexed_ids]
    reused = len(rows) - len(pending_rows)
    estimated_chunk_total = sum(len(_paper_chunks(row, cfg)) for row in pending_rows)
    if cfg.progress:
        print(
            "Vector build starting: "
            f"{len(rows):,} DB paper(s); {reused:,} already indexed; "
            f"{len(pending_rows):,} to process; about {estimated_chunk_total:,} chunk(s).",
            flush=True,
        )
    embedder = _load_embedder(cfg.model_name)

    ids: list[str] = []
    docs: list[str] = []
    embs: list[list[float]] = []
    metas: list[dict[str, Any]] = []

    newly_indexed = 0
    skipped_no_text = 0
    chunk_count = 0
    processed_pending = 0
    last_progress = 0.0

    def flush() -> None:
        nonlocal chunk_count
        if not ids:
            return
        collection.add(ids=ids, documents=docs, embeddings=embs, metadatas=metas)
        chunk_count += len(ids)
        ids.clear()
        docs.clear()
        embs.clear()
        metas.clear()

    def progress_line(*, force: bool = False) -> None:
        nonlocal last_progress
        if not cfg.progress:
            return
        now = time.time()
        interval = max(5, int(cfg.progress_interval_seconds or DEFAULT_PROGRESS_SECONDS))
        if not force and now - last_progress < interval:
            return
        last_progress = now
        elapsed = max(0.001, now - started)
        chunks_done = chunk_count + len(ids)
        rate = chunks_done / elapsed
        remaining_chunks = max(0, estimated_chunk_total - chunks_done)
        eta = (remaining_chunks / rate) if rate > 0 else None
        paper_total = max(1, len(pending_rows))
        pct = min(100.0, (processed_pending / paper_total) * 100.0)
        print(
            "Vector progress: "
            f"{processed_pending:,}/{len(pending_rows):,} paper(s) ({pct:.1f}%); "
            f"{chunks_done:,}/{estimated_chunk_total:,} chunk(s); "
            f"{rate * 60:.0f} chunk(s)/min; "
            f"elapsed {_format_duration(elapsed)}; ETA {_format_duration(eta)}.",
            flush=True,
        )

    progress_line(force=True)

    for row in pending_rows:
        paper_id = str(row["id"])
        paper_meta = _paper_meta(row)
        paper_chunks = _paper_chunks(row, cfg)
        if not paper_chunks:
            skipped_no_text += 1
            indexed_ids.add(paper_id)
            processed_pending += 1
            progress_line()
            continue
        texts = [text for _, text in paper_chunks]
        vectors = embedder.encode(texts, batch_size=cfg.batch_size, show_progress_bar=False, normalize_embeddings=True)
        for i, ((section, text), vec) in enumerate(zip(paper_chunks, vectors)):
            ids.append(f"{paper_id}_c{i}")
            docs.append(text)
            metas.append({**paper_meta, "section": section, "chars": len(text)})
            embs.append(vec.tolist() if hasattr(vec, "tolist") else list(vec))
        indexed_ids.add(paper_id)
        newly_indexed += 1
        processed_pending += 1
        progress_line()
        if len(ids) >= cfg.batch_size * 6:
            flush()
    flush()
    progress_line(force=True)

    ended = time.time()
    new_manifest = {
        "version": MANIFEST_VERSION,
        "backend": "sentence-transformers",
        "model": cfg.model_name,
        "collection": cfg.collection_name,
        "chunk_chars": cfg.chunk_chars,
        "overlap_chars": cfg.overlap_chars,
        "indexed_paper_ids": sorted(indexed_ids),
        "stats": {
            "paper_count_db": len(rows),
            "paper_ids_indexed": len(indexed_ids),
            "newly_indexed": newly_indexed,
            "reused_existing": reused,
            "skipped_no_text": skipped_no_text,
            "new_chunks_added": chunk_count,
            "collection_count": collection.count(),
            "elapsed_seconds": round(ended - started, 2),
        },
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_manifest(new_manifest)
    return {"ok": True, "manifest_path": str(MANIFEST_PATH), **new_manifest["stats"]}


def semantic_search(
    query: str,
    *,
    top_k: int = 12,
    model_name: str = DEFAULT_MODEL,
    min_rating: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "empty query"}
    manifest = load_manifest()
    if not manifest:
        return {"ok": False, "error": "vector index not built yet"}
    if manifest.get("backend") != "sentence-transformers":
        return {"ok": False, "error": "unsupported vector backend in manifest"}
    active_model = str(manifest.get("model") or model_name or DEFAULT_MODEL)
    collection_name = str(manifest.get("collection") or _collection_name(active_model))

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = client.get_collection(collection_name)

    embedder = _load_embedder(active_model)
    vector = embedder.encode([query], show_progress_bar=False, normalize_embeddings=True)[0]

    where: dict[str, Any] | None = None
    if min_rating:
        where = {"rating": {"$eq": min_rating}}
    n = max(1, min(top_k * 5, 120))
    raw = collection.query(query_embeddings=[vector.tolist() if hasattr(vector, "tolist") else list(vector)], n_results=n, where=where, include=["metadatas", "documents", "distances"])

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    metas = raw.get("metadatas", [[]])[0]
    docs = raw.get("documents", [[]])[0]
    dists = raw.get("distances", [[]])[0]

    for meta, doc, dist in zip(metas, docs, dists):
        if not isinstance(meta, dict):
            continue
        paper_id = str(meta.get("paper_id") or "")
        if not paper_id:
            continue
        year = str(meta.get("year") or "")
        try:
            yi = int(year)
        except Exception:
            yi = None
        if year_from and yi and yi < year_from:
            continue
        if year_to and yi and yi > year_to:
            continue
        if paper_id in seen:
            continue
        seen.add(paper_id)
        out.append(
            {
                "paper_id": paper_id,
                "score": float(1.0 - float(dist)),
                "title": str(meta.get("title") or ""),
                "authors": str(meta.get("authors") or ""),
                "year": year,
                "journal": str(meta.get("journal") or ""),
                "rating": str(meta.get("rating") or ""),
                "domains": str(meta.get("domains") or ""),
                "metacheck_status": str(meta.get("metacheck_status") or ""),
                "metacheck_source": str(meta.get("metacheck_source") or ""),
                "metacheck_reason": str(meta.get("metacheck_reason") or ""),
                "snippet": (str(doc or "")[:800]).strip(),
                "section": str(meta.get("section") or ""),
            }
        )
        if len(out) >= top_k:
            break
    return {"ok": True, "mode": "semantic", "query": query, "results": out, "result_count": len(out)}
