"""Duplicate disposition for ingest: delete confirmed dupes, resolve ambiguous via review_resolution."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from review_resolution import (
    ResolutionAudit,
    delete_confirmed_duplicate,
    resolve_pdf,
)

CONFIRMED_HASH_PREFIX = "hash_matches_managed_pdf:"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def classify_prescan_duplicate_confidence(signals: list[str]) -> str:
    if any(s.startswith(CONFIRMED_HASH_PREFIX) for s in signals):
        return "confirmed"
    if signals:
        return "ambiguous"
    return "none"


def _log_disposition(**fields: Any) -> None:
    config.DUPE_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(fields, ensure_ascii=False) + "\n"
    with config.DUPE_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def delete_confirmed_duplicate_file(
    pdf_path: Path,
    reason: str,
    *,
    signals: list[str] | None = None,
    audit: ResolutionAudit | None = None,
) -> dict[str, Any]:
    """Delete extra copy when canonical owned PDF exists (hash-identical)."""
    audit = audit or ResolutionAudit()
    result = delete_confirmed_duplicate(pdf_path, audit=audit)
    _log_disposition(
        action="confirmed_duplicate_deleted",
        source=str(pdf_path),
        reason=reason,
        signals=signals or [],
        result=result,
    )
    return result


def resolve_ambiguous_duplicate_file(
    pdf_path: Path,
    reason: str,
    *,
    allow_opus: bool = False,
    audit: ResolutionAudit | None = None,
) -> dict[str, Any]:
    """Resolve ambiguous duplicate via deterministic checks (+ Opus if allowed)."""
    audit = audit or ResolutionAudit()
    result = resolve_pdf(pdf_path, audit=audit, allow_opus=allow_opus)
    _log_disposition(action="ambiguous_resolved", source=str(pdf_path), reason=reason, result=result)
    return result


# Backward-compatible aliases used by older imports
archive_confirmed_duplicate = delete_confirmed_duplicate_file
route_ambiguous_duplicate = resolve_ambiguous_duplicate_file


def _source_hash_index() -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    if not config.SOURCE_PDFS.exists():
        return index
    for path in config.SOURCE_PDFS.glob("*.pdf"):
        try:
            index.setdefault(_sha256(path), []).append(path.name)
        except OSError:
            continue
    return index


def _db_filename_index() -> set[str]:
    import sqlite3

    names: set[str] = set()
    if not config.DB_PATH.exists():
        return names
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT file_info FROM papers").fetchall()
    finally:
        conn.close()
    for (fi_raw,) in rows:
        try:
            fi = json.loads(fi_raw) if fi_raw else {}
        except json.JSONDecodeError:
            fi = {}
        for key in ("original_filename", "renamed_filename"):
            val = fi.get(key)
            if val:
                names.add(str(val).lower())
    return names


def assess_pdf_duplicate_confidence(
    pdf_path: Path,
    *,
    hash_index: dict[str, list[str]] | None = None,
    filename_index: set[str] | None = None,
) -> tuple[str, list[str]]:
    signals: list[str] = []
    try:
        digest = _sha256(pdf_path)
    except OSError:
        return "none", []

    index = hash_index if hash_index is not None else _source_hash_index()
    for owner in index.get(digest, []):
        signals.append(f"{CONFIRMED_HASH_PREFIX}{owner}")
        return "confirmed", signals

    names = filename_index if filename_index is not None else _db_filename_index()
    if pdf_path.name.lower() in names:
        signals.append("filename_in_db")
        return "ambiguous", signals

    return "none", signals


def reclassify_review_duplicates(*, dry_run: bool = False, allow_opus: bool = False) -> dict[str, Any]:
    from review_resolution import run_bloat_cleanup

    return run_bloat_cleanup(dry_run=dry_run, allow_opus=allow_opus)


def main() -> None:
    from review_resolution import main as resolution_main

    resolution_main()


if __name__ == "__main__":
    main()
