"""MetaCheck evidence-layer integration for the corpus pilot copy."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import config
import papers_db
from metacheck_client import pdf_to_grobid_xml, run_metacheck_xml


ERA_WEIGHTS = {
    "retraction": {"pre_1990": "high", "1990_2009": "high", "2010_2019": "high", "2020_plus": "high", "unknown_year": "high"},
    "pubpeer": {"pre_1990": "high", "1990_2009": "high", "2010_2019": "high", "2020_plus": "high", "unknown_year": "high"},
    "statistical": {"pre_1990": "high", "1990_2009": "high", "2010_2019": "high", "2020_plus": "high", "unknown_year": "high"},
    "unsupported_claim": {"pre_1990": "high", "1990_2009": "high", "2010_2019": "high", "2020_plus": "high", "unknown_year": "high"},
    "effect_size": {"pre_1990": "low", "1990_2009": "medium", "2010_2019": "high", "2020_plus": "high", "unknown_year": "medium"},
    "power": {"pre_1990": "low", "1990_2009": "medium", "2010_2019": "medium", "2020_plus": "high", "unknown_year": "medium"},
    "preregistration": {"pre_1990": "none", "1990_2009": "low", "2010_2019": "medium", "2020_plus": "high", "unknown_year": "low"},
    "open_practices": {"pre_1990": "none", "1990_2009": "low", "2010_2019": "medium", "2020_plus": "high", "unknown_year": "low"},
    "funding_coi": {"pre_1990": "low", "1990_2009": "medium", "2010_2019": "high", "2020_plus": "high", "unknown_year": "medium"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def era_bucket(year: Any) -> str:
    try:
        y = int(year)
    except (TypeError, ValueError):
        return "unknown_year"
    if y < 1990:
        return "pre_1990"
    if y < 2010:
        return "1990_2009"
    if y < 2020:
        return "2010_2019"
    return "2020_plus"


def _finding_family(module: str, finding_text: str = "") -> str:
    m = module.lower()
    t = finding_text.lower()
    if "retraction" in m or "retracted" in t:
        return "retraction"
    if "pubpeer" in m or "pubpeer" in t:
        return "pubpeer"
    if "effect" in m:
        return "effect_size"
    if "power" in m:
        return "power"
    if "stat" in m or "p_" in m or "p-value" in t or "p value" in t:
        return "statistical"
    if "prereg" in m or "registration" in t:
        return "preregistration"
    if "open" in m or "data" in t or "code" in t:
        return "open_practices"
    if "funding" in m or "coi" in m or "conflict" in t:
        return "funding_coi"
    return "unsupported_claim"


def era_context(year: Any) -> dict[str, Any]:
    bucket = era_bucket(year)
    return {
        "bucket": bucket,
        "instruction": (
            "Interpret MetaCheck findings in publication-era context. Do not penalize older papers "
            "for absent modern preregistration, open-code, open-data, CRediT, or checklist norms unless "
            "the absence prevents interpretation, replication, or supports overclaiming. Retractions, "
            "serious statistical inconsistency, citation problems, and unsupported claims remain relevant in all eras."
        ),
        "weights": {family: weights[bucket] for family, weights in ERA_WEIGHTS.items()},
    }


def _source_pdf_for_paper(paper: dict[str, Any]) -> Path | None:
    fi = paper.get("file_info") or {}
    rel = fi.get("filepath") or fi.get("managed_pdf_relative_path") or ""
    if rel:
        candidate = Path(rel) if Path(rel).is_absolute() else config.CORPUS_STORE / rel
        if candidate.exists():
            return candidate
    renamed = fi.get("renamed_filename")
    if renamed:
        candidate = config.SOURCE_PDFS / str(renamed)
        if candidate.exists():
            return candidate
    return None


def _text_for_paper(paper: dict[str, Any]) -> str:
    ft = paper.get("full_text") or {}
    try:
        from evidence_coverage import resolve_raw_full_text

        text = resolve_raw_full_text(ft)
        if text:
            return text
    except Exception:
        pass
    if isinstance(ft, dict):
        for key in ("raw_full_text", "text", "full_text", "content"):
            val = ft.get(key)
            if isinstance(val, str) and val.strip():
                return val
        sections = ft.get("sections")
        if isinstance(sections, dict):
            return "\n\n".join(str(v) for v in sections.values() if isinstance(v, str))
    return ""


def _traffic_light(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip().lower()
    aliases = {
        "green": "green",
        "yellow": "yellow",
        "red": "red",
        "info": "info",
        "na": "na",
        "fail": "fail",
        "no problems detected": "green",
        "something to check": "yellow",
        "possible problems detected": "red",
        "informational only": "info",
    }
    return aliases.get(s, s if s in {"green", "yellow", "red", "info", "na", "fail"} else "")


def _iter_module_results(result: Any) -> list[tuple[str, Any]]:
    if not isinstance(result, dict):
        return []
    modules = result.get("modules") or result.get("results") or result.get("checks")
    if isinstance(modules, dict):
        return [(str(k), v) for k, v in modules.items()]
    if isinstance(modules, list):
        out = []
        for idx, item in enumerate(modules):
            if isinstance(item, dict):
                name = item.get("module") or item.get("name") or item.get("id") or f"module_{idx}"
                out.append((str(name), item))
        return out
    return [(str(k), v) for k, v in result.items() if isinstance(v, (dict, list))]


def _compact_text(value: Any, max_len: int = 260) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text[:max_len]


def summarize_metacheck_result(result: Any, *, year: Any, modules_requested: list[str]) -> dict[str, Any]:
    era = era_context(year)
    traffic: dict[str, str] = {}
    counts = {"red": 0, "yellow": 0, "green": 0, "info": 0, "na": 0, "fail": 0}
    high_priority: list[dict[str, Any]] = []

    for module, payload in _iter_module_results(result):
        tl = ""
        summary = ""
        rows = 0
        if isinstance(payload, dict):
            for key in ("traffic_light", "traffic", "status", "light"):
                tl = _traffic_light(payload.get(key))
                if tl:
                    break
            summary = _compact_text(payload.get("summary_text") or payload.get("summary") or payload.get("message") or payload.get("report"))
            table = payload.get("table") or payload.get("findings") or payload.get("data")
            if isinstance(table, list):
                rows = len(table)
            elif isinstance(table, dict):
                rows = len(table)
        elif isinstance(payload, list):
            rows = len(payload)
            summary = _compact_text(payload[:3])
        if not tl:
            tl = "info" if rows else "na"
        traffic[module] = tl
        if tl in counts:
            counts[tl] += 1

        family = _finding_family(module, summary)
        relevance = era["weights"].get(family, "medium")
        if tl in {"red", "yellow", "fail"} or (tl == "info" and relevance == "high" and rows):
            high_priority.append(
                {
                    "module": module,
                    "traffic_light": tl,
                    "finding_family": family,
                    "era_adjusted_relevance": relevance,
                    "summary": summary or f"{rows} finding(s)",
                    "finding_count": rows,
                }
            )

    era_note = era["instruction"]
    return {
        "modules_requested": modules_requested,
        "traffic_lights": traffic,
        "finding_counts": counts,
        "era_context": era,
        "era_adjusted_summary": era_note,
        "high_priority_findings": high_priority[:12],
    }


def _technical_unavailable_summary(
    *,
    reason: str,
    stage: str,
    digest: str,
    modules: list[str],
    year: Any,
    xml_path: Path | None = None,
    result_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "status": "technical_unavailable",
        "source": "corpus_pipeline_only",
        "mode": "advanced_required_but_unavailable",
        "advanced_status": "technical_unavailable",
        "ran_at": _now(),
        "reason": reason,
        "stage": stage,
        "modules": modules,
        "source_pdf_sha256": digest,
        "grobid_xml_path": str(xml_path) if xml_path else "",
        "result_path": str(result_path) if result_path else "",
        "era_context": era_context(year),
        "methodology_note": (
            "Paper was rated by the existing corpus pipeline, but Real MetaCheck could not be run "
            "because GROBID did not produce usable XML for this PDF. This is expected for some scanned, "
            "image-based, photographed, malformed, or otherwise GROBID-hostile PDFs. Do not treat this "
            "as a MetaCheck pass or fail; treat it as missing automated MetaCheck evidence."
        ),
    }


def run_metacheck_for_paper_id(
    paper_id: str,
    *,
    modules: list[str] | None = None,
    force: bool = False,
    status_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run hybrid MetaCheck evidence for one paper and persist compact DB evidence."""
    def report(status: str) -> None:
        if status_callback:
            status_callback(status)

    paper = papers_db.get_paper(paper_id)
    if not paper:
        return {"ok": False, "status": "failed", "reason": "paper_not_found", "paper_id": paper_id}
    modules = modules or list(config.METACHECK_MODULES)
    notes = paper.setdefault("notes_and_flags", {})
    existing = notes.get("metacheck") if isinstance(notes, dict) else None
    if existing and existing.get("status") == "ok" and not force:
        if existing.get("source") == "advanced_metacheck":
            report("cached")
            return {"ok": True, "status": "cached", "paper_id": paper_id, "summary": existing}

    bm = paper.get("basic_metadata") or {}

    pdf_path = _source_pdf_for_paper(paper)
    if not pdf_path:
        return {"ok": False, "status": "failed", "reason": "source_pdf_missing", "paper_id": paper_id}

    digest = sha256_file(pdf_path)

    xml_path = config.GROBID_CACHE_DIR / f"{digest}.tei.xml"
    result_path = config.METACHECK_CACHE_DIR / f"{paper_id}.json"

    report("advanced_grobid_pdf_to_xml")
    grobid = pdf_to_grobid_xml(pdf_path, xml_path)
    if not grobid.get("ok"):
        reason = grobid.get("reason") or "grobid_failed"
        summary = _technical_unavailable_summary(
            reason=reason,
            stage="grobid_pdf_to_xml",
            digest=digest,
            modules=modules,
            year=bm.get("year"),
            xml_path=xml_path,
            result_path=result_path,
        )
        notes["metacheck"] = summary
        papers_db.save_paper(paper)
        return {"ok": True, "status": "technical_unavailable", "paper_id": paper_id, "summary": summary}

    report("advanced_metacheck_xml_checks")
    mc = run_metacheck_xml(Path(grobid["xml_path"]), modules, result_path)
    if not mc.get("ok"):
        reason = mc.get("reason") or "metacheck_failed"
        summary = _technical_unavailable_summary(
            reason=reason,
            stage="metacheck_xml_checks",
            digest=digest,
            modules=modules,
            year=bm.get("year"),
            xml_path=Path(grobid["xml_path"]),
            result_path=result_path,
        )
        notes["metacheck"] = summary
        papers_db.save_paper(paper)
        return {"ok": True, "status": "technical_unavailable", "paper_id": paper_id, "summary": summary}

    report("advanced_metacheck_done")
    compact = summarize_metacheck_result(mc.get("result"), year=bm.get("year"), modules_requested=modules)
    summary = {
        "status": "ok",
        "source": "advanced_metacheck",
        "mode": "advanced",
        "advanced_status": "ok",
        "ran_at": _now(),
        "modules": modules,
        "source_pdf_sha256": digest,
        "grobid_xml_path": str(xml_path),
        "result_path": str(result_path),
        **compact,
    }
    notes["metacheck"] = summary
    papers_db.save_paper(paper)
    return {"ok": True, "status": "ok", "paper_id": paper_id, "summary": summary}


def format_metacheck_for_prompt(paper: dict[str, Any]) -> str:
    """Return concise MetaCheck evidence block for evaluator prompts."""
    mc = ((paper.get("notes_and_flags") or {}).get("metacheck") or {})
    if not isinstance(mc, dict) or mc.get("status") != "ok":
        return ""
    era = mc.get("era_context") or {}
    lines = [
        "=== METACHECK EVIDENCE LAYER ===",
        "Use these automated checks as evidence requiring interpretation, not as an automatic rating decision.",
        str(mc.get("era_adjusted_summary") or era.get("instruction") or "").strip(),
    ]
    traffic = mc.get("traffic_lights") or {}
    for module, tl in list(traffic.items())[:12]:
        lines.append(f"- {module}: {tl}")
    findings = mc.get("high_priority_findings") or []
    if findings:
        lines.append("High-priority findings:")
        for item in findings[:8]:
            lines.append(
                "- {module}: {traffic}; era relevance={rel}; {summary}".format(
                    module=item.get("module"),
                    traffic=item.get("traffic_light"),
                    rel=item.get("era_adjusted_relevance"),
                    summary=_compact_text(item.get("summary"), 220),
                )
            )
    lines.append("Do not penalize older papers for absent modern preregistration/open-data/open-code norms unless this affects interpretation, replication, or overclaiming.")
    return "\n".join(x for x in lines if x).strip() + "\n\n"
