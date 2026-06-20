"""Content-based inspection and classification for supplements and child documents."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import config

CLASSIFICATIONS = frozenset(
    {
        "VALID_PDF_CHILD_DOCUMENT",
        "VALID_PDF_STANDALONE_RESEARCH",
        "VALID_PDF_NON_RATABLE_REFERENCE",
        "VALID_NONPDF_CHILD_DOCUMENT",
        "VALID_NONPDF_REFERENCE",
        "VALID_NONPDF_DATASET_OR_MEDIA_SUPPLEMENT",
        "HTML_OR_TEXT_CHILD_DOCUMENT",
        "HTML_OR_TEXT_REFERENCE",
        "HTML_OR_TEXT_JUNK",
        "MIME_OR_WRAPPER_RECOVERABLE",
        "MIME_OR_WRAPPER_JUNK",
        "ARCHIVE_CHILD_DOCUMENT",
        "CONFIRMED_DUPLICATE",
        "TRUE_CORRUPT_UNUSABLE",
        "MODEL_RECOVERY_REQUIRED",
    }
)

DELETABLE_CLASSIFICATIONS = frozenset(
    {
        "CONFIRMED_DUPLICATE",
        "TRUE_CORRUPT_UNUSABLE",
        "HTML_OR_TEXT_JUNK",
        "MIME_OR_WRAPPER_JUNK",
    }
)

PENDING_CLASSIFICATIONS = frozenset(
    {
        "VALID_PDF_CHILD_DOCUMENT",
        "VALID_NONPDF_CHILD_DOCUMENT",
        "VALID_NONPDF_DATASET_OR_MEDIA_SUPPLEMENT",
        "HTML_OR_TEXT_CHILD_DOCUMENT",
        "MIME_OR_WRAPPER_RECOVERABLE",
        "ARCHIVE_CHILD_DOCUMENT",
        "MODEL_RECOVERY_REQUIRED",
    }
)

STAGING_RESEARCH = frozenset({"VALID_PDF_STANDALONE_RESEARCH"})
STAGING_NON_RATABLE = frozenset({"VALID_PDF_NON_RATABLE_REFERENCE", "VALID_NONPDF_REFERENCE", "HTML_OR_TEXT_REFERENCE"})

_OFFICE_ZIP_EXTENSIONS = frozenset({".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".odt"})

HTML_JUNK_PATTERNS = (
    r"there was a problem providing the content",
    r"access denied",
    r"403 forbidden",
    r"404 not found",
    r"page not found",
    r"cookie.?consent",
    r"enable cookies",
    r"refused to connect",
    r"service unavailable",
    r"temporarily unavailable",
    r"robot.?check",
    r"captcha",
    r"sign in to continue",
    r"purchase this article",
    r"institutional access",
)

HTML_USEFUL_PATTERNS = (
    r"doi\.org/",
    r"supplement",
    r"supplementary",
    r"mmc\d",
    r"supporting information",
    r"appendix",
    r"erratum",
    r"corrigendum",
    r"abstract",
    r"journal",
    r"sciencedirect\.com/science/article",
)

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.I)
TITLE_RE = re.compile(r"<title[^>]*>([^<]{4,300})</title>", re.I | re.S)


def sniff_content(path: Path) -> dict[str, Any]:
    path = Path(path)
    info: dict[str, Any] = {
        "path": str(path),
        "filename": path.name,
        "extension": path.suffix.lower(),
        "file_size": 0,
        "zero_byte": False,
        "header_hex": "",
        "detected_kind": "unknown",
        "extension_matches_content": True,
    }
    if not path.exists():
        info["detected_kind"] = "missing"
        return info
    size = path.stat().st_size
    info["file_size"] = size
    if size == 0:
        info["zero_byte"] = True
        info["detected_kind"] = "empty"
        return info
    try:
        with path.open("rb") as f:
            header = f.read(512)
    except OSError as e:
        info["detected_kind"] = "unreadable"
        info["read_error"] = str(e)
        return info
    info["header_hex"] = header[:16].hex()
    low = header[:200].lower()
    ext = path.suffix.lower()

    if header.startswith(b"%PDF"):
        info["detected_kind"] = "pdf"
    elif header.lstrip().startswith(b"<!") or b"<html" in low or b"<!doctype html" in low:
        info["detected_kind"] = "html"
        info["extension_matches_content"] = ext in {".html", ".htm", ".pdf", ".txt", ""}
    elif header.startswith(b"PK\x03\x04"):
        info["detected_kind"] = "zip"
        info["extension_matches_content"] = ext in {".zip", ".xlsx", ".docx", ".pptx", ".jar", ".odt", ""}
    elif header.startswith(b"Rar!"):
        info["detected_kind"] = "rar"
    elif header.startswith(b"7z\xbc\xaf\x27\x1c"):
        info["detected_kind"] = "7z"
    elif header.startswith(b"Content-Type:") or header.startswith(b"MIME-Version:") or b"Content-Type:" in header[:400]:
        info["detected_kind"] = "mime"
        info["extension_matches_content"] = ext in {".pdf", ".eml", ".mime", ".txt", ""}
    elif header.startswith(b"\xd0\xcf\x11\xe0"):
        info["detected_kind"] = "ole"
        info["extension_matches_content"] = ext in {".doc", ".xls", ".ppt", ""}
    elif header.startswith(b"{\\rtf"):
        info["detected_kind"] = "rtf"
    elif ext in {".csv", ".tsv"} or (b"," in header[:200] and b"\n" in header[:400]):
        info["detected_kind"] = "csv"
    elif ext in {".json", ".xml"} or header.lstrip().startswith(b"{") or header.lstrip().startswith(b"["):
        info["detected_kind"] = "text_structured"
    elif header.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"BM", b"RIFF", b"ID3", b"\x1a\x45\xdf\xa3")):
        info["detected_kind"] = "media"
    else:
        try:
            sample = header.decode("utf-8", errors="ignore")
            if sample.strip() and all(c.isprintable() or c in "\r\n\t" for c in sample[:120]):
                info["detected_kind"] = "text"
        except Exception:
            pass
    if ext == ".pdf" and info["detected_kind"] != "pdf":
        info["extension_matches_content"] = False
    return info


def _sha256(path: Path) -> str | None:
    from review_resolution import _sha256 as sha

    try:
        return sha(path)
    except OSError:
        return None


def _duplicate_check(path: Path, *, owned_canonical_only: bool = False) -> dict[str, Any]:
    from review_resolution import (
        _canonical_owner_for_hash,
        _other_staging_paths_for_hash,
        delete_confirmed_duplicate,
        is_staging_duplicate_of_other,
    )

    digest = _sha256(path)
    owner = _canonical_owner_for_hash(digest) if digest else None
    if owner:
        return {"is_duplicate": True, "evidence": {"match": "hash_identical_owned", "sha256": digest, "canonical": owner.get("canonical_name")}}

    if owned_canonical_only:
        return {"is_duplicate": False, "sha256": digest}

    dup = delete_confirmed_duplicate(path, dry_run=True)
    if dup.get("decision") == "DELETE_DUPLICATE":
        return {"is_duplicate": True, "evidence": dup.get("evidence") or {}}
    if digest and is_staging_duplicate_of_other(path, digest):
        return {
            "is_duplicate": True,
            "evidence": {
                "match": "staging_hash_identical",
                "sha256": digest,
                "staging_files": _other_staging_paths_for_hash(digest, exclude=path),
            },
        }
    return {"is_duplicate": False, "sha256": digest}


def _inspect_html(path: Path, sniff: dict[str, Any]) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:200_000]
    except OSError as e:
        return {"parse_error": str(e), "title": "", "dois": [], "junk_score": 0, "useful_score": 0}
    low = text.lower()
    title_m = TITLE_RE.search(text)
    title = (title_m.group(1).strip() if title_m else "")[:300]
    dois = list(dict.fromkeys(DOI_RE.findall(text)))[:5]
    junk_score = sum(1 for p in HTML_JUNK_PATTERNS if re.search(p, low))
    useful_score = sum(1 for p in HTML_USEFUL_PATTERNS if re.search(p, low))
    if title and len(title) >= 8 and not re.search(r"error|denied|cookie|sign in", title.lower()):
        useful_score += 1
    if dois:
        useful_score += 2
    return {
        "title": title,
        "dois": dois,
        "text_length": len(text),
        "junk_score": junk_score,
        "useful_score": useful_score,
    }


def _inspect_mime(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:100_000]
    except OSError as e:
        return {"parse_error": str(e), "recoverable": False}
    low = text.lower()
    has_pdf = "application/pdf" in low or "filename=\"*.pdf\"" in low or "filename=.pdf" in low
    has_attachment = "content-disposition: attachment" in low or "content-type: application/" in low
    has_doi = bool(DOI_RE.search(text))
    recoverable = has_pdf or has_attachment or has_doi or len(text.strip()) > 400
    return {
        "text_length": len(text),
        "has_pdf_attachment_hint": has_pdf,
        "has_attachment": has_attachment,
        "dois": list(dict.fromkeys(DOI_RE.findall(text)))[:3],
        "recoverable": recoverable,
    }


def _inspect_archive(path: Path, sniff: dict[str, Any]) -> dict[str, Any]:
    kind = sniff.get("detected_kind")
    if kind == "zip" and zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()[:50]
            return {"archive_type": "zip", "entries": names, "entry_count": len(names)}
        except (OSError, zipfile.BadZipFile) as e:
            return {"archive_type": "zip", "error": str(e)}
    return {"archive_type": kind or "unknown"}


def _inspect_csv(path: Path) -> dict[str, Any]:
    try:
        sample = path.read_text(encoding="utf-8", errors="replace")[:20_000]
        reader = csv.reader(io.StringIO(sample))
        rows = [row for _, row in zip(range(5), reader)]
        headers = rows[0] if rows else []
        return {"headers": headers[:20], "sample_rows": len(rows), "text_length": len(sample)}
    except OSError as e:
        return {"parse_error": str(e)}


def _inspect_pdf(path: Path) -> dict[str, Any]:
    from recovery_ladder import assess_pdf_usability, gather_evidence
    from document_corpus_classification import classify_corpus_document
    from child_document_matching import detect_child_document_type, is_child_document

    usability = assess_pdf_usability(path)
    evidence = gather_evidence(path, pages=3)
    recovered = evidence.get("recovered_metadata") or {}
    title = str(recovered.get("title") or "").strip()
    classification = classify_corpus_document(path.name, title=title, recovered=recovered)
    child_type = detect_child_document_type(path.name)
    return {
        "usability": usability,
        "page_count": evidence.get("page_count"),
        "text_length": evidence.get("text_length"),
        "recovered_metadata": recovered,
        "corpus_classification": classification,
        "is_child_filename": is_child_document(path.name),
        "child_type": child_type,
        "pdf_valid": evidence.get("pdf_valid"),
    }


def classify_supplement_file(
    path: Path,
    *,
    allow_paid_api: bool = False,
    allow_opus: bool = False,
    owned_canonical_only: bool = True,
) -> dict[str, Any]:
    """Inspect file content and return classification with evidence."""
    path = Path(path)
    sniff = sniff_content(path)
    result: dict[str, Any] = {
        "path": str(path),
        "filename": path.name,
        "sniff": sniff,
        "classification": "MODEL_RECOVERY_REQUIRED",
        "evidence": {},
        "parent_clues": {},
    }

    if sniff.get("detected_kind") == "missing":
        result["classification"] = "TRUE_CORRUPT_UNUSABLE"
        result["evidence"] = {"reason": "file_missing"}
        return result
    if sniff.get("zero_byte"):
        result["classification"] = "TRUE_CORRUPT_UNUSABLE"
        result["evidence"] = {"reason": "zero_byte"}
        return result

    dup = _duplicate_check(path, owned_canonical_only=owned_canonical_only)
    result["evidence"]["duplicate_check"] = dup
    if dup.get("is_duplicate"):
        result["classification"] = "CONFIRMED_DUPLICATE"
        return result

    kind = sniff.get("detected_kind")
    from child_document_matching import detect_child_document_type, is_child_document, extract_parent_hints

    hints = extract_parent_hints(path.name)
    result["parent_clues"] = hints
    child_filename = is_child_document(path.name)
    child_type = detect_child_document_type(path.name)

    if kind == "pdf":
        pdf_info = _inspect_pdf(path)
        result["evidence"]["pdf"] = pdf_info
        usability = pdf_info.get("usability") or {}
        classification = pdf_info.get("corpus_classification") or {}
        if usability.get("corrupt"):
            result["classification"] = "TRUE_CORRUPT_UNUSABLE"
            result["evidence"]["reason"] = usability.get("reason")
            return result
        if child_filename or pdf_info.get("is_child_filename"):
            result["classification"] = "VALID_PDF_CHILD_DOCUMENT"
            return result
        if classification.get("is_non_ratable"):
            result["classification"] = "VALID_PDF_NON_RATABLE_REFERENCE"
            return result
        if pdf_info.get("pdf_valid") and int(pdf_info.get("page_count") or 0) >= 1:
            result["classification"] = "VALID_PDF_STANDALONE_RESEARCH"
            return result
        result["classification"] = "MODEL_RECOVERY_REQUIRED"
        return result

    if kind == "html":
        html_info = _inspect_html(path, sniff)
        result["evidence"]["html"] = html_info
        if html_info.get("junk_score", 0) >= 2 and html_info.get("useful_score", 0) == 0:
            result["classification"] = "HTML_OR_TEXT_JUNK"
            return result
        if html_info.get("useful_score", 0) >= 1 or child_filename or hints:
            if child_filename:
                result["classification"] = "HTML_OR_TEXT_CHILD_DOCUMENT"
            else:
                result["classification"] = "HTML_OR_TEXT_REFERENCE"
            return result
        if html_info.get("text_length", 0) < 200:
            result["classification"] = "HTML_OR_TEXT_JUNK"
            return result
        result["classification"] = "MODEL_RECOVERY_REQUIRED" if not allow_paid_api else "HTML_OR_TEXT_CHILD_DOCUMENT"
        return result

    if kind == "mime":
        mime_info = _inspect_mime(path)
        result["evidence"]["mime"] = mime_info
        if mime_info.get("recoverable"):
            result["classification"] = "MIME_OR_WRAPPER_RECOVERABLE"
            return result
        result["classification"] = "MIME_OR_WRAPPER_JUNK"
        return result

    if kind in {"zip", "rar", "7z"}:
        ext = path.suffix.lower()
        if ext in _OFFICE_ZIP_EXTENSIONS:
            if child_filename:
                result["classification"] = "VALID_NONPDF_CHILD_DOCUMENT"
            else:
                result["classification"] = "VALID_NONPDF_REFERENCE"
            return result
        arch = _inspect_archive(path, sniff)
        result["evidence"]["archive"] = arch
        if arch.get("entries") or child_filename:
            result["classification"] = "ARCHIVE_CHILD_DOCUMENT"
            return result
        result["classification"] = "MODEL_RECOVERY_REQUIRED"
        return result

    if kind in {"csv", "text_structured", "text"} or path.suffix.lower() in {".csv", ".tsv", ".json", ".xml", ".txt", ".rtf"}:
        if path.suffix.lower() in {".csv", ".tsv"}:
            result["evidence"]["csv"] = _inspect_csv(path)
        if child_filename or re.search(r"supplement|mmc|data|dataset|supporting", path.name.lower()):
            result["classification"] = "VALID_NONPDF_DATASET_OR_MEDIA_SUPPLEMENT"
            return result
        if child_filename:
            result["classification"] = "VALID_NONPDF_CHILD_DOCUMENT"
            return result
        result["classification"] = "VALID_NONPDF_REFERENCE"
        return result

    if kind == "media" or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp3", ".mp4", ".wav", ".avi", ".mov"}:
        result["classification"] = (
            "VALID_NONPDF_DATASET_OR_MEDIA_SUPPLEMENT" if child_filename else "MODEL_RECOVERY_REQUIRED"
        )
        return result

    if kind == "ole" or path.suffix.lower() in {".doc", ".xls", ".docx", ".xlsx"}:
        if child_filename:
            result["classification"] = "VALID_NONPDF_CHILD_DOCUMENT"
        else:
            result["classification"] = "VALID_NONPDF_REFERENCE"
        return result

    if sniff.get("file_size", 0) < 64:
        result["classification"] = "TRUE_CORRUPT_UNUSABLE"
        result["evidence"]["reason"] = "too_small_unclassified"
        return result

    if child_filename:
        result["classification"] = "VALID_NONPDF_CHILD_DOCUMENT"
        return result

    result["classification"] = "MODEL_RECOVERY_REQUIRED"
    return result


def disposition_for_classification(classification: str) -> str:
    if classification in DELETABLE_CLASSIFICATIONS:
        return "DELETE"
    if classification in PENDING_CLASSIFICATIONS:
        return "PENDING"
    if classification in STAGING_RESEARCH or classification in STAGING_NON_RATABLE:
        return "STAGING"
    return "MODEL_RECOVERY"


def is_deletable(classification: str) -> bool:
    return classification in DELETABLE_CLASSIFICATIONS


def is_valid_pending(classification: str) -> bool:
    return classification in PENDING_CLASSIFICATIONS or classification.startswith("VALID_")
