"""Retry metadata sidecars for review ↔ staging anti-loop policy."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RETRY_SIDECAR_SUFFIX = ".review-retry.json"
FAILURE_MISSING_TITLE = "missing_basic_metadata.title"
FAILURE_INGEST_FAILED = "ingest_failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sidecar_path(pdf_path: Path) -> Path:
    from staging_metadata import internal_sidecar_path

    return internal_sidecar_path(pdf_path)


def read_retry_meta(pdf_path: Path) -> dict[str, Any]:
    path = sidecar_path(pdf_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_retry_meta(pdf_path: Path, meta: dict[str, Any]) -> Path:
    pdf_path = Path(pdf_path)
    path = sidecar_path(pdf_path)
    meta = dict(meta)
    meta["updated_at"] = _now()
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def move_sidecar(src_pdf: Path, dest_pdf: Path) -> None:
    src = sidecar_path(src_pdf)
    if not src.exists():
        return
    dest = sidecar_path(dest_pdf)
    try:
        meta = read_retry_meta(src_pdf)
        write_retry_meta(dest_pdf, meta)
        src.unlink(missing_ok=True)
    except OSError:
        pass


def metadata_from_sidecar(pdf_path: Path) -> dict[str, Any]:
    meta = read_retry_meta(pdf_path)
    recovered = meta.get("recovered_metadata") or {}
    return recovered if isinstance(recovered, dict) else {}


def has_recovered_title(pdf_path: Path) -> bool:
    md = metadata_from_sidecar(pdf_path)
    title = str(md.get("title") or "").strip()
    return len(title) >= 3


def should_block_restaging(meta: dict[str, Any], failure_reason: str | None = None) -> bool:
    count = int(meta.get("review_retry_count") or 0)
    if count < 1:
        return False
    last = str(meta.get("last_failure_reason") or "")
    reason = failure_reason or FAILURE_MISSING_TITLE
    if count >= 1 and last and reason and last == reason:
        return True
    return count >= 2


def record_staging_move(
    pdf_path: Path,
    *,
    source_review_folder: str,
    original_review_reason: str = "",
    recovered_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prev = read_retry_meta(pdf_path)
    count = int(prev.get("review_retry_count") or 0) + 1
    meta = {
        **prev,
        "source_review_folder": source_review_folder,
        "original_review_reason": original_review_reason or prev.get("original_review_reason") or "",
        "moved_to_staging_at": _now(),
        "review_retry_count": count,
        "last_failure_reason": prev.get("last_failure_reason"),
    }
    if recovered_metadata:
        meta["recovered_metadata"] = recovered_metadata
    write_retry_meta(pdf_path, meta)
    return meta


def record_review_failure(
    pdf_path: Path,
    *,
    failure_reason: str,
    source_folder: str = "needs-metadata",
) -> dict[str, Any]:
    prev = read_retry_meta(pdf_path)
    meta = {
        **prev,
        "source_review_folder": source_folder,
        "last_failure_reason": failure_reason,
        "last_review_failure_at": _now(),
    }
    if not meta.get("original_review_reason"):
        meta["original_review_reason"] = failure_reason
    write_retry_meta(pdf_path, meta)
    return meta


def ingest_overrides_from_sidecar(pdf_path: Path) -> dict[str, Any]:
    """Build process_paper arg overrides from recovered sidecar metadata."""
    meta = read_retry_meta(pdf_path)
    md = metadata_from_sidecar(pdf_path)
    if not md and not meta:
        return {}
    out: dict[str, Any] = {}
    title = str(md.get("title") or "").strip()
    if title:
        out["title"] = title
    authors = md.get("authors") or []
    if isinstance(authors, list) and authors:
        out["authors"] = "; ".join(str(a) for a in authors if str(a).strip())
    elif md.get("is_non_ratable") or meta.get("recovered_metadata", {}).get("is_non_ratable"):
        out["authors"] = "Unknown"
    year = md.get("year")
    if year:
        out["year"] = str(year)
    for key in ("journal", "doi", "volume", "issue", "pages"):
        val = md.get(key)
        if val:
            out[key] = str(val)
    if md.get("document_type"):
        out["document_type"] = md["document_type"]
    if md.get("is_non_ratable") or meta.get("recovered_metadata", {}).get("is_non_ratable"):
        out["non_ratable_preclassification"] = True
        out["corpus_class"] = md.get("corpus_class") or "non_ratable_reference_material"
        if md.get("expected_rating"):
            out["expected_rating"] = md["expected_rating"]
        if md.get("non_ratable_justification"):
            out["non_ratable_justification"] = md["non_ratable_justification"]
        if md.get("classification_reasons"):
            out["classification_reasons"] = md["classification_reasons"]
    return out


def sidecar_indicates_non_ratable(pdf_path: Path) -> bool:
    md = metadata_from_sidecar(pdf_path)
    meta = read_retry_meta(pdf_path)
    recovered = meta.get("recovered_metadata") or {}
    return bool(md.get("is_non_ratable") or recovered.get("is_non_ratable"))


def cleanup_orphan_ingest_artifacts(pdf_path: Path, source_pdfs_dir: Path) -> int:
    """Remove unowned source-pdf copies left by failed ingest (no DB row)."""
    stem = re.sub(r"[^a-zA-Z0-9_.-]", "_", Path(pdf_path).stem)[:80]
    removed = 0
    for p in list(source_pdfs_dir.glob(f"unidentified_{stem}*.pdf")):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def can_move_to_staging(
    meta: dict[str, Any],
    recovered: dict[str, Any],
    *,
    failure_reason: str = FAILURE_MISSING_TITLE,
) -> bool:
    title = str(recovered.get("title") or "").strip()
    if len(title) < 3:
        return False
    if not should_block_restaging(meta, failure_reason):
        return True
    prev = meta.get("recovered_metadata") or {}
    prev_title = str(prev.get("title") or "").strip()
    return not prev_title and bool(title)


def infer_prior_staging_attempt(pdf_path: Path, *, source_pdfs_dir: Path) -> bool:
    stem = re.sub(r"[^a-zA-Z0-9_.-]", "_", pdf_path.stem)[:80]
    candidate = source_pdfs_dir / f"unidentified_{stem}.pdf"
    if candidate.exists():
        return True
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", pdf_path.stem)[:80]
    for p in source_pdfs_dir.glob(f"unidentified_{safe}*.pdf"):
        return True
    return False
