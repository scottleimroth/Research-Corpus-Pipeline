"""Central document text extraction contract for ingest and adjudication."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import config

MIN_RESEARCH_TEXT_CHARS = 200
MIN_PREVIEW_TEXT_CHARS = 20

EXTRACTOR_STATUSES = frozenset(
    {
        "TEXT_OK",
        "TRUE_EMPTY_TEXT",
        "EXTRACTOR_UNAVAILABLE",
        "EXTRACTOR_CRASHED",
        "WRONG_PATH_OR_MISSING_FILE",
        "UNSUPPORTED_CONTENT_TYPE",
        "CORRUPT_OR_UNREADABLE",
        "OCR_REQUIRED",
        "OCR_FAILED",
    }
)

_FORMFEED_RE = re.compile(r"[\x0c\x00-\x08\x0b-\x1f]+")


def meaningful_text_length(text: str | None) -> int:
    """Count usable characters; form-feed-only pdftotext output counts as zero."""
    if not text:
        return 0
    cleaned = _FORMFEED_RE.sub(" ", text)
    return len(cleaned.strip())


def normalize_extracted_text(text: str | None) -> str:
    if not text:
        return ""
    cleaned = _FORMFEED_RE.sub("\n", text)
    return cleaned.strip()


@dataclass
class ExtractionResult:
    file_path_used: str
    original_path: str
    content_type: str
    page_count: int | None
    text: str
    text_length: int
    extraction_source: str
    extractor_status: str
    ocr_status: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    was_copy_path_used: bool = False
    source_equals_staging_hash: bool | None = None
    pdftotext_length: int = 0
    pypdf_length: int = 0
    ocr_attempted: bool = False
    ocr_text_length: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_usable_for_research(self) -> bool:
        return self.extractor_status == "TEXT_OK" and self.text_length >= MIN_RESEARCH_TEXT_CHARS

    @property
    def is_pipeline_tool_failure(self) -> bool:
        return self.extractor_status in {
            "EXTRACTOR_UNAVAILABLE",
            "EXTRACTOR_CRASHED",
            "WRONG_PATH_OR_MISSING_FILE",
            "OCR_FAILED",
        }


def _tool_available(name: str) -> bool:
    return shutil.which(name) is not None


EXTRACTION_CACHE_DIR = config.CORPUS_STORE / "cache" / "extraction"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_extraction_cache(path: Path) -> dict[str, Any] | None:
    try:
        digest = _file_sha256(path)
        cache_path = EXTRACTION_CACHE_DIR / f"{digest}.json"
        if not cache_path.exists():
            return None
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if int(payload.get("size_bytes") or 0) != path.stat().st_size:
            return None
        return payload
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _save_extraction_cache(path: Path, result: ExtractionResult) -> None:
    try:
        EXTRACTION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        digest = _file_sha256(path)
        payload = result.to_dict()
        payload["size_bytes"] = path.stat().st_size
        payload["sha256"] = digest
        (EXTRACTION_CACHE_DIR / f"{digest}.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _finalize_extraction_result(
    base: dict[str, Any],
    path: Path,
    *,
    use_cache: bool,
    started: float,
) -> ExtractionResult:
    base["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    result = ExtractionResult(**base)
    if use_cache and path.exists() and result.extractor_status == "TEXT_OK":
        _save_extraction_cache(path, result)
    return result


def _pdf_page_count(path: Path) -> int | None:
    if not _tool_available("pdfinfo"):
        return None
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            return None
        for line in (result.stdout or "").splitlines():
            if line.lower().startswith("pages:"):
                return int(line.split(":", 1)[1].strip())
    except (ValueError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _run_pdftotext(path: Path, *, pages: int | None = None, timeout: int = 90) -> tuple[str, str | None]:
    if not _tool_available("pdftotext"):
        return "", "pdftotext_not_found"
    cmd = ["pdftotext"]
    if pages:
        cmd.extend(["-l", str(pages)])
    cmd.extend([str(path), "-"])
    try:
        result = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout
        )
        text = normalize_extracted_text(result.stdout or "")
        if result.returncode != 0 and not text:
            return "", f"pdftotext_exit_{result.returncode}"
        err = None if result.returncode == 0 else f"pdftotext_exit_{result.returncode}"
        return text, err
    except subprocess.TimeoutExpired:
        return "", "pdftotext_timeout"
    except OSError as e:
        return "", f"pdftotext_error:{e}"


def _run_pypdf(path: Path, *, pages: int | None = None) -> tuple[str, str | None]:
    from pdf_content_guard import extract_pypdf_text_safely

    return extract_pypdf_text_safely(path, pages=pages or 9999)


def _run_ocr_pdftotext(path: Path, *, timeout: int = 300) -> tuple[str, str, list[str]]:
    """Return (text, ocr_status, errors)."""
    errors: list[str] = []
    if not _tool_available("ocrmypdf"):
        return "", "unavailable", ["ocrmypdf_not_found"]
    if not _tool_available("pdftotext"):
        return "", "unavailable", ["pdftotext_not_found"]

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
        proc = subprocess.run(
            ["ocrmypdf", "--force-ocr", str(path), tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            errors.append(f"ocrmypdf_exit_{proc.returncode}:{tail}")
            return "", "failed", errors
        text, err = _run_pdftotext(Path(tmp_path), timeout=timeout)
        if err:
            errors.append(err)
        if meaningful_text_length(text) >= MIN_PREVIEW_TEXT_CHARS:
            return text, "ok", errors
        errors.append("ocr_produced_insufficient_text")
        return text, "failed", errors
    except subprocess.TimeoutExpired:
        return "", "failed", ["ocrmypdf_timeout"]
    except OSError as e:
        return "", "failed", [f"ocrmypdf_error:{e}"]
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass


def extract_document_text(
    path: Path | str,
    *,
    pages: int | None = None,
    attempt_ocr: bool = True,
    original_path: Path | str | None = None,
    was_copy_path_used: bool = False,
    use_cache: bool = True,
) -> ExtractionResult:
    """Extract text with explicit status — never treat tool failure as empty file."""
    started = time.perf_counter()
    path = Path(path)
    original = Path(original_path) if original_path else path

    if path.exists() and use_cache and pages is None:
        cached = _load_extraction_cache(path)
        if cached and cached.get("extractor_status"):
            cached["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
            return ExtractionResult(**{k: cached[k] for k in ExtractionResult.__dataclass_fields__ if k in cached})

    base: dict[str, Any] = {
        "file_path_used": str(path),
        "original_path": str(original),
        "content_type": "unknown",
        "page_count": None,
        "text": "",
        "text_length": 0,
        "extraction_source": "none",
        "extractor_status": "WRONG_PATH_OR_MISSING_FILE",
        "ocr_status": "not_attempted",
        "errors": [],
        "warnings": [],
        "was_copy_path_used": was_copy_path_used,
        "pdftotext_length": 0,
        "pypdf_length": 0,
        "ocr_attempted": False,
        "ocr_text_length": 0,
    }

    if not path.exists():
        base["errors"].append("file_missing")
        base["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return _finalize_extraction_result(base, path, use_cache=use_cache, started=started)

    from pdf_content_guard import content_kind, extract_text_preview, is_actual_pdf
    from supplement_content_classifier import sniff_content

    sniff = sniff_content(path)
    kind = content_kind(path)
    base["content_type"] = kind

    if kind != "pdf":
        text, preview_kind, _ = extract_text_preview(path, pages=pages or 3)
        text = normalize_extracted_text(text)
        length = meaningful_text_length(text)
        base.update(
            page_count=None,
            text=text,
            text_length=length,
            extraction_source=preview_kind or "non_pdf_preview",
            extractor_status="TEXT_OK" if length >= MIN_PREVIEW_TEXT_CHARS else "TRUE_EMPTY_TEXT",
            ocr_status="not_applicable",
        )
        if length < MIN_PREVIEW_TEXT_CHARS:
            base["warnings"].append(f"non_pdf_low_text:{preview_kind}")
        base["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return _finalize_extraction_result(base, path, use_cache=use_cache, started=started)

    if not is_actual_pdf(path):
        base["extractor_status"] = "CORRUPT_OR_UNREADABLE"
        base["errors"].append("invalid_pdf_header")
        base["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return _finalize_extraction_result(base, path, use_cache=use_cache, started=started)

    page_count = _pdf_page_count(path)
    base["page_count"] = page_count

    pdftotext_text, pdftotext_err = _run_pdftotext(path, pages=pages)
    base["pdftotext_length"] = meaningful_text_length(pdftotext_text)
    if pdftotext_err == "pdftotext_not_found":
        base["extractor_status"] = "EXTRACTOR_UNAVAILABLE"
        base["errors"].append(pdftotext_err)
        base["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return _finalize_extraction_result(base, path, use_cache=use_cache, started=started)
    if pdftotext_err and pdftotext_err.startswith("pdftotext_error:"):
        base["extractor_status"] = "EXTRACTOR_CRASHED"
        base["errors"].append(pdftotext_err)
        base["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return _finalize_extraction_result(base, path, use_cache=use_cache, started=started)

    best_text = pdftotext_text
    best_source = "pdftotext"
    # Full-document ingest must reach research-grade length before skipping OCR.
    min_early_ok = MIN_RESEARCH_TEXT_CHARS if pages is None else MIN_PREVIEW_TEXT_CHARS
    if meaningful_text_length(best_text) >= min_early_ok:
        base.update(
            text=best_text,
            text_length=meaningful_text_length(best_text),
            extraction_source=best_source,
            extractor_status="TEXT_OK",
            ocr_status="not_needed",
        )
        base["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return _finalize_extraction_result(base, path, use_cache=use_cache, started=started)

    pypdf_text, pypdf_err = _run_pypdf(path, pages=pages)
    base["pypdf_length"] = meaningful_text_length(pypdf_text)
    if pypdf_err and pypdf_err != "not_actual_pdf":
        base["warnings"].append(pypdf_err)
    if meaningful_text_length(pypdf_text) > meaningful_text_length(best_text):
        best_text = pypdf_text
        best_source = "pypdf"

    if meaningful_text_length(best_text) >= min_early_ok:
        base.update(
            text=best_text,
            text_length=meaningful_text_length(best_text),
            extraction_source=best_source,
            extractor_status="TEXT_OK",
            ocr_status="not_needed",
        )
        base["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return _finalize_extraction_result(base, path, use_cache=use_cache, started=started)

    if not attempt_ocr:
        status = "OCR_REQUIRED" if (page_count or 0) >= 1 else "TRUE_EMPTY_TEXT"
        base.update(
            text=best_text,
            text_length=meaningful_text_length(best_text),
            extraction_source=best_source,
            extractor_status=status,
            ocr_status="not_attempted",
            warnings=[*(base.get("warnings") or []), "ocr_not_attempted"],
        )
        base["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return _finalize_extraction_result(base, path, use_cache=use_cache, started=started)

    base["ocr_attempted"] = True
    ocr_text, ocr_status, ocr_errors = _run_ocr_pdftotext(path)
    base["ocr_text_length"] = meaningful_text_length(ocr_text)
    base["errors"].extend(ocr_errors)
    base["ocr_status"] = ocr_status

    if ocr_status == "unavailable":
        base.update(
            text=best_text,
            text_length=meaningful_text_length(best_text),
            extraction_source=best_source,
            extractor_status="EXTRACTOR_UNAVAILABLE",
        )
        base["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return _finalize_extraction_result(base, path, use_cache=use_cache, started=started)

    if meaningful_text_length(ocr_text) >= MIN_PREVIEW_TEXT_CHARS:
        base.update(
            text=ocr_text,
            text_length=meaningful_text_length(ocr_text),
            extraction_source="ocr",
            extractor_status="TEXT_OK",
        )
        base["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return _finalize_extraction_result(base, path, use_cache=use_cache, started=started)

    if ocr_status == "failed":
        base.update(
            text=best_text,
            text_length=meaningful_text_length(best_text),
            extraction_source=best_source,
            extractor_status="OCR_FAILED",
        )
        base["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return _finalize_extraction_result(base, path, use_cache=use_cache, started=started)

    # OCR ran but produced no usable text on a readable PDF shell.
    if (page_count or 0) >= 1 and (path.stat().st_size if path.exists() else 0) >= 500:
        base.update(
            text=best_text,
            text_length=meaningful_text_length(best_text),
            extraction_source=best_source,
            extractor_status="TRUE_EMPTY_TEXT",
        )
    else:
        base.update(
            text=best_text,
            text_length=meaningful_text_length(best_text),
            extraction_source=best_source,
            extractor_status="CORRUPT_OR_UNREADABLE",
        )
    base["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return ExtractionResult(**base)


def extraction_failure_terminal_action(result: ExtractionResult | dict[str, Any]) -> str:
    """Map extraction outcome to disposition action; never delete on tool/path failure alone."""
    data = result if isinstance(result, dict) else result.to_dict()
    status = str(data.get("extractor_status") or "")
    if status in {"EXTRACTOR_UNAVAILABLE", "EXTRACTOR_CRASHED", "WRONG_PATH_OR_MISSING_FILE", "OCR_FAILED"}:
        return "SYSTEM_INTEGRITY_FAILURE"
    if status == "OCR_REQUIRED":
        return "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION"
    if status == "TRUE_EMPTY_TEXT":
        return "DELETE_TRUE_CORRUPT_UNUSABLE"
    if status == "CORRUPT_OR_UNREADABLE":
        return "DELETE_TRUE_CORRUPT_UNUSABLE"
    if status == "UNSUPPORTED_CONTENT_TYPE":
        return "DELETE_CONFIRMED_JUNK"
    if status == "TEXT_OK" and int(data.get("text_length") or 0) < MIN_RESEARCH_TEXT_CHARS:
        return "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION"
    return "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION"


def format_extraction_diagnostic(result: ExtractionResult | dict[str, Any]) -> str:
    data = result if isinstance(result, dict) else result.to_dict()
    name = Path(str(data.get("file_path_used") or "")).name
    return (
        f"extraction status={data.get('extractor_status')} "
        f"source={data.get('extraction_source')} "
        f"text_len={data.get('text_length')} "
        f"pages={data.get('page_count')} "
        f"ocr={data.get('ocr_status')} "
        f"path={name}"
    )


def diagnose_file_no_write(path: Path | str, *, attempt_ocr: bool = False) -> dict[str, Any]:
    """No-write diagnostic helper for validation and manual inspection."""
    result = extract_document_text(path, attempt_ocr=attempt_ocr)
    out = result.to_dict()
    out["meaningful_text"] = result.text_length >= MIN_PREVIEW_TEXT_CHARS
    out["diagnostic_line"] = format_extraction_diagnostic(result)
    try:
        raw = Path(path).read_bytes()[:8]
        out["file_signature"] = raw.decode("latin-1", errors="replace")
    except OSError as e:
        out["file_signature"] = f"read_error:{e}"
    try:
        out["size_bytes"] = Path(path).stat().st_size
    except OSError:
        out["size_bytes"] = None
    return out
