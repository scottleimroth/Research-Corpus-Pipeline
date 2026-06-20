"""Persistent SHA256 cache for owned source PDFs used by staging preflight dedupe."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import config
import papers_db

CACHE_VERSION = 1
CACHE_PATH = config.CORPUS_STORE / "cache" / "source-pdf-sha256-v1.json"
HASH_CHUNK_BYTES = 8 * 1024 * 1024
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def sha256_stream(path: Path, *, chunk_size: int = HASH_CHUNK_BYTES) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel_source_key(path: Path) -> str:
    try:
        return path.relative_to(config.BASE).as_posix()
    except ValueError:
        return path.as_posix()


def _load_cache_file() -> dict[str, Any]:
    if not CACHE_PATH.exists():
        return {"version": CACHE_VERSION, "entries": {}}
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": CACHE_VERSION, "entries": {}}
    if int(data.get("version") or 0) != CACHE_VERSION:
        return {"version": CACHE_VERSION, "entries": {}}
    entries = data.get("entries")
    return {"version": CACHE_VERSION, "entries": entries if isinstance(entries, dict) else {}}


def _save_cache_file(data: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(CACHE_PATH)


def _owned_excluded_rel_set() -> set[str]:
    try:
        from source_pdf_ownership import build_audit

        audit = build_audit()
        return {
            str(p).replace("\\", "/").lower()
            for p in (audit.get("unowned_source_pdfs") or []) + (audit.get("ambiguous_source_pdfs") or [])
        }
    except Exception:
        return set()


def _metadata_hash_hints() -> dict[str, dict[str, str]]:
    """Map owned source-pdf rel path -> {paper_id, sha256} from DB file_info when present."""
    hints: dict[str, dict[str, str]] = {}
    for meta in papers_db.get_all_metadata():
        paper_id = str(meta.get("id") or "")
        fi = meta.get("file_info") or {}
        if not isinstance(fi, dict):
            continue
        digest = (
            str(fi.get("managed_pdf_sha256") or fi.get("sha256") or fi.get("source_sha256") or "").strip().lower()
        )
        if not _HEX64.match(digest):
            continue
        rel = str(fi.get("managed_pdf_relative_path") or fi.get("filepath") or "").replace("\\", "/")
        if not rel:
            renamed = str(fi.get("renamed_filename") or "")
            if renamed:
                rel = f"CorpusStore/source-pdfs/{renamed}"
        if not rel:
            continue
        rel_key = rel.lower()
        if "source-pdfs" not in rel_key:
            continue
        hints[rel_key] = {"paper_id": paper_id, "sha256": digest}
    return hints


def build_owned_source_hash_index(
    *,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[dict[str, list[dict[str, str]]], dict[str, Any]]:
    """Build digest -> owned source entries once; reuse persistent cache for source PDFs."""
    started = time.monotonic()
    excluded = _owned_excluded_rel_set()
    hints = _metadata_hash_hints()
    cache_doc = _load_cache_file()
    entries: dict[str, Any] = dict(cache_doc.get("entries") or {})
    index: dict[str, list[dict[str, str]]] = {}
    loaded_from_cache = 0
    loaded_from_db_hint = 0
    newly_computed = 0
    bytes_hashed = 0

    if not config.SOURCE_PDFS.exists():
        stats = {
            "source_hash_index_seconds": round(time.monotonic() - started, 3),
            "source_hashes_loaded_from_cache": 0,
            "source_hashes_loaded_from_db_hint": 0,
            "source_hashes_newly_computed": 0,
            "source_hash_bytes_read": 0,
            "owned_source_files_indexed": 0,
        }
        return index, stats

    dirty = False
    for path in sorted(config.SOURCE_PDFS.glob("*.pdf")):
        rel = _rel_source_key(path).lower()
        if rel in excluded:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        size = int(st.st_size)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
        key = _rel_source_key(path)
        cached = entries.get(key) if isinstance(entries.get(key), dict) else None
        digest: str | None = None

        if cached and int(cached.get("size") or -1) == size and int(cached.get("mtime_ns") or -1) == mtime_ns:
            cand = str(cached.get("sha256") or "").lower()
            if _HEX64.match(cand):
                digest = cand
                loaded_from_cache += 1

        if digest is None:
            hint = hints.get(rel) or hints.get(key.lower())
            if hint and _HEX64.match(hint.get("sha256", "")):
                digest = hint["sha256"]
                loaded_from_db_hint += 1
                entries[key] = {"size": size, "mtime_ns": mtime_ns, "sha256": digest}
                dirty = True
            else:
                try:
                    digest = sha256_stream(path)
                except OSError:
                    continue
                bytes_hashed += size
                newly_computed += 1
                entries[key] = {"size": size, "mtime_ns": mtime_ns, "sha256": digest}
                dirty = True

        paper_id = (hints.get(rel) or hints.get(key.lower()) or {}).get("paper_id", "")
        index.setdefault(digest, []).append(
            {
                "paper_id": paper_id,
                "path": str(path),
                "canonical_name": path.name,
            }
        )

    if dirty:
        _save_cache_file({"version": CACHE_VERSION, "entries": entries})

    elapsed = round(time.monotonic() - started, 3)
    stats = {
        "source_hash_index_seconds": elapsed,
        "source_hashes_loaded_from_cache": loaded_from_cache,
        "source_hashes_loaded_from_db_hint": loaded_from_db_hint,
        "source_hashes_newly_computed": newly_computed,
        "source_hash_bytes_read": bytes_hashed,
        "owned_source_files_indexed": sum(len(v) for v in index.values()),
        "source_hash_cache_path": str(CACHE_PATH),
    }
    if log_fn:
        log_fn(
            "Preflight source hash index: "
            f"{stats['owned_source_files_indexed']} owned file(s) in {elapsed}s "
            f"(cache={loaded_from_cache}, db_hint={loaded_from_db_hint}, computed={newly_computed})"
        )
    return index, stats


def hash_staging_file(path: Path) -> tuple[str, int]:
    """Return (sha256, bytes_read) for one staging candidate."""
    size = int(path.stat().st_size)
    return sha256_stream(path), size
