"""Hard guards: non-PDF files must never enter PDF-only extraction tiers."""

from __future__ import annotations

import re
import subprocess
import contextlib
import io
import logging
from pathlib import Path
from typing import Any

from supplement_content_classifier import sniff_content

_logged_skips: set[str] = set()
_logged_pdf_warnings: set[str] = set()

TITLE_RE = re.compile(r"<title[^>]*>([^<]{4,300})</title>", re.I | re.S)
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.I)


def is_actual_pdf(path: Path) -> bool:
    """True only when file content begins with %PDF."""
    path = Path(path)
    if not path.exists():
        return False
    sniff = sniff_content(path)
    return sniff.get("detected_kind") == "pdf"


def content_kind(path: Path) -> str:
    return str(sniff_content(path).get("detected_kind") or "unknown")


def log_skip_non_pdf_once(path: Path, tier: str, *, kind: str | None = None) -> None:
    key = f"{Path(path).resolve()}:{tier}"
    if key in _logged_skips:
        return
    _logged_skips.add(key)
    kind = kind or content_kind(path)
    print(f"  [skip {tier}] non-PDF content ({kind}): {Path(path).name}")


def guard_pdf_only(path: Path, *, tier: str) -> bool:
    """Return True if caller must NOT run PDF-only logic."""
    path = Path(path)
    if is_actual_pdf(path):
        return False
    log_skip_non_pdf_once(path, tier, kind=content_kind(path))
    return True


def log_pdf_warning_once(path: Path, tier: str, reason: str) -> None:
    key = f"{Path(path).resolve()}:{tier}:{reason[:120]}"
    if key in _logged_pdf_warnings:
        return
    _logged_pdf_warnings.add(key)
    compact = " ".join(str(reason).split())[:240]
    print(f"  [pdf_warning] tier={tier} file={Path(path).name} reason={compact}")


@contextlib.contextmanager
def suppress_pypdf_noise():
    """Prevent recoverable pypdf parser chatter from spamming stderr/console."""
    logger_names = ("pypdf", "PyPDF2")
    saved = []
    for name in logger_names:
        logger = logging.getLogger(name)
        saved.append((logger, logger.disabled, logger.level, logger.propagate))
        logger.disabled = True
        logger.propagate = False
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            yield
        finally:
            for logger, disabled, level, propagate in saved:
                logger.disabled = disabled
                logger.level = level
                logger.propagate = propagate


def read_pdf_metadata_safely(path: Path, *, tier: str = "pdf_metadata") -> dict[str, Any]:
    """Best-effort embedded metadata read with malformed-PDF noise suppression."""
    path = Path(path)
    if guard_pdf_only(path, tier=tier):
        return {"error": "not_actual_pdf"}
    try:
        from pypdf import PdfReader  # type: ignore

        with suppress_pypdf_noise():
            reader = PdfReader(str(path), strict=False)
            meta = reader.metadata or {}
            pages = len(reader.pages)
        return {
            "title": (getattr(meta, "title", None) or "").strip() or None,
            "author": (getattr(meta, "author", None) or "").strip() or None,
            "subject": (getattr(meta, "subject", None) or "").strip() or None,
            "creator": (getattr(meta, "creator", None) or "").strip() or None,
            "producer": (getattr(meta, "producer", None) or "").strip() or None,
            "pages": pages,
            "error": None,
        }
    except Exception as e:
        log_pdf_warning_once(path, tier, f"{type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


def extract_pypdf_text_safely(path: Path, *, pages: int = 1, tier: str = "pypdf_text") -> tuple[str, str | None]:
    """Best-effort pypdf text extraction for fallback tiers."""
    path = Path(path)
    if guard_pdf_only(path, tier=tier):
        return "", "not_actual_pdf"
    try:
        from pypdf import PdfReader  # type: ignore

        with suppress_pypdf_noise():
            reader = PdfReader(str(path), strict=False)
            chunks = []
            for page in list(reader.pages)[:pages]:
                chunks.append(page.extract_text() or "")
        return "\n".join(chunks).strip(), None
    except Exception as e:
        log_pdf_warning_once(path, tier, f"{type(e).__name__}: {e}")
        return "", f"{type(e).__name__}: {e}"


def _pdftotext(path: Path, *, pages: int | None = None, timeout: int = 90) -> tuple[str, str | None]:
    cmd = ["pdftotext"]
    if pages:
        cmd.extend(["-l", str(pages)])
    cmd.extend([str(path), "-"])
    try:
        result = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout
        )
        text = (result.stdout or "").strip()
        if result.returncode != 0 and not text:
            return "", f"pdftotext_exit_{result.returncode}"
        return text, None
    except subprocess.TimeoutExpired:
        return "", "pdftotext_timeout"
    except OSError as e:
        return "", str(e)


def extract_text_preview(path: Path, *, pages: int = 3, max_chars: int = 80_000) -> tuple[str, str, dict[str, Any]]:
    """Content-aware text preview. Never invokes PDF tools on non-PDF files."""
    path = Path(path)
    sniff = sniff_content(path)
    kind = str(sniff.get("detected_kind") or "unknown")

    if kind == "pdf":
        text, err = _pdftotext(path, pages=pages)
        if err and not text:
            return "", kind, sniff
        return text, kind, sniff

    if kind in {"html", "text", "mime", "text_structured", "xml", "csv"}:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
        except OSError:
            text = ""
        return text, kind, sniff

    if kind == "zip":
        return "", kind, sniff

    try:
        raw = path.read_bytes()[:max_chars]
        text = raw.decode("utf-8", errors="replace")
        if text.strip():
            return text, kind, sniff
    except OSError:
        pass
    return "", kind, sniff


def recover_metadata_for_path(
    path: Path,
    *,
    pages: int = 5,
    doi: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Metadata recovery that respects actual content type."""
    from metadata_extractors import extract_metadata, extract_clean_title_from_filename

    path = Path(path)
    text, kind, _ = extract_text_preview(path, pages=pages)

    if not doi and text:
        from add_paper import doi_from_pdf_text

        doi = doi_from_pdf_text(text)

    if is_actual_pdf(path):
        tier_order = ["crossref", "pdf_embed", "filename_human", "first_page_heuristic", "filename"]
    else:
        tier_order = ["crossref", "filename_human", "first_page_heuristic", "filename"]

    merged = extract_metadata(
        str(path),
        text,
        doi=doi,
        tier_order=tier_order,
        verbose=verbose,
    )
    merged.pop("needs_review", None)

    if kind in {"html", "mime", "text", "xml"} and text and not merged.get("title"):
        title_m = TITLE_RE.search(text)
        if title_m:
            merged["title"] = title_m.group(1).strip()[:300]
            merged["source"] = merged.get("source") or f"{kind}_title_extract"
        dois = DOI_RE.findall(text)
        if dois and not merged.get("doi"):
            merged["doi"] = dois[0]

    if not merged.get("title"):
        plain = extract_clean_title_from_filename(path)
        if plain:
            merged["title"] = plain
            merged.setdefault("source", "filename_plain_title")

    merged["actual_content_type"] = kind
    return merged
