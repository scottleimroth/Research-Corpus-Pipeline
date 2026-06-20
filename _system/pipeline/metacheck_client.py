"""HTTP client for optional GROBID + MetaCheck sidecar services."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

import config


GROBID_TIMEOUT = 60
METACHECK_TIMEOUT = 180
REQUEST_ATTEMPTS = 1


def service_available(url: str, *, timeout: int = 3) -> bool:
    """Return True when a local sidecar responds to a basic HTTP request."""
    try:
        resp = requests.get(url.rstrip("/") + "/", timeout=timeout)
        return resp.status_code < 500
    except requests.RequestException:
        return False


def grobid_available() -> bool:
    return service_available(config.GROBID_API_URL.rstrip("/") + "/api/isalive")


def metacheck_available() -> bool:
    return service_available(config.METACHECK_API_URL.rstrip("/") + "/__docs__/")


def pdf_to_grobid_xml(pdf_path: Path, xml_path: Path) -> dict[str, Any]:
    """Convert PDF to GROBID TEI XML and cache it at xml_path."""
    pdf_path = Path(pdf_path)
    xml_path = Path(xml_path)
    if xml_path.exists() and xml_path.stat().st_size > 100:
        return {"ok": True, "cached": True, "xml_path": str(xml_path)}
    if not grobid_available():
        return {"ok": False, "reason": "grobid_unavailable", "xml_path": str(xml_path)}

    xml_path.parent.mkdir(parents=True, exist_ok=True)
    endpoint = config.GROBID_API_URL.rstrip("/") + "/api/processFulltextDocument"
    last_error: str | None = None
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        try:
            with pdf_path.open("rb") as fh:
                resp = requests.post(
                    endpoint,
                    files={"input": (pdf_path.name, fh, "application/pdf")},
                    data={"consolidateHeader": "0", "consolidateCitations": "0"},
                    timeout=GROBID_TIMEOUT,
                )
            if resp.status_code >= 500 and attempt < REQUEST_ATTEMPTS:
                last_error = f"grobid_http_{resp.status_code}"
                time.sleep(5)
                continue
            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "reason": f"grobid_http_{resp.status_code}",
                    "detail": resp.text[:500],
                    "attempts": attempt,
                    "xml_path": str(xml_path),
                }
            text = resp.text or ""
            if "<TEI" not in text and "<tei" not in text.lower():
                return {
                    "ok": False,
                    "reason": "grobid_non_tei_response",
                    "detail": text[:500],
                    "attempts": attempt,
                    "xml_path": str(xml_path),
                }
            xml_path.write_text(text, encoding="utf-8")
            return {"ok": True, "cached": False, "attempts": attempt, "xml_path": str(xml_path)}
        except requests.RequestException as exc:
            last_error = f"grobid_request_error:{type(exc).__name__}"
            if attempt < REQUEST_ATTEMPTS:
                time.sleep(5)
                continue
            return {"ok": False, "reason": last_error, "attempts": attempt, "xml_path": str(xml_path)}
        except OSError as exc:
            return {"ok": False, "reason": f"grobid_cache_write_error:{exc}", "attempts": attempt, "xml_path": str(xml_path)}
    return {"ok": False, "reason": last_error or "grobid_request_failed", "attempts": REQUEST_ATTEMPTS, "xml_path": str(xml_path)}


def run_metacheck_xml(xml_path: Path, modules: list[str], result_path: Path) -> dict[str, Any]:
    """Run MetaCheck /paper/check against a GROBID XML file."""
    xml_path = Path(xml_path)
    result_path = Path(result_path)
    if result_path.exists() and result_path.stat().st_size > 10:
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
            return {"ok": True, "cached": True, "result": data, "result_path": str(result_path)}
        except (OSError, json.JSONDecodeError):
            pass
    if not metacheck_available():
        return {"ok": False, "reason": "metacheck_unavailable", "result_path": str(result_path)}

    result_path.parent.mkdir(parents=True, exist_ok=True)
    endpoint = config.METACHECK_API_URL.rstrip("/") + "/paper/check"
    last_error: str | None = None
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        try:
            with xml_path.open("rb") as fh:
                resp = requests.post(
                    endpoint,
                    files={"file": (xml_path.name, fh, "application/xml")},
                    data={"modules": ",".join(modules)},
                    timeout=METACHECK_TIMEOUT,
                )
            if resp.status_code >= 500 and attempt < REQUEST_ATTEMPTS:
                last_error = f"metacheck_http_{resp.status_code}"
                time.sleep(5)
                continue
            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "reason": f"metacheck_http_{resp.status_code}",
                    "detail": resp.text[:500],
                    "attempts": attempt,
                    "result_path": str(result_path),
                }
            try:
                data = resp.json()
            except ValueError:
                data = {"raw": resp.text}
            result_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            return {"ok": True, "cached": False, "attempts": attempt, "result": data, "result_path": str(result_path)}
        except requests.RequestException as exc:
            last_error = f"metacheck_request_error:{type(exc).__name__}"
            if attempt < REQUEST_ATTEMPTS:
                time.sleep(5)
                continue
            return {"ok": False, "reason": last_error, "attempts": attempt, "result_path": str(result_path)}
        except OSError as exc:
            return {"ok": False, "reason": f"metacheck_cache_write_error:{exc}", "attempts": attempt, "result_path": str(result_path)}
    return {"ok": False, "reason": last_error or "metacheck_request_failed", "attempts": REQUEST_ATTEMPTS, "result_path": str(result_path)}
