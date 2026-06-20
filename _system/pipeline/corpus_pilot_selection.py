"""Curated pilot selection profiles for first orchestrated live workflow tests."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

PILOT_PROFILES: dict[str, dict[str, Any]] = {
    "none": {
        "label": "No pilot curation",
        "description": "Use selection profile only; no additional curation.",
    },
    "safe-live": {
        "label": "Curated first live workflow test batch",
        "description": (
            "Prefer 2–3 clean book/edited-volume parent candidates plus optional duplicate log-only control; "
            "exclude hash-named files, other_or_unknown, supplements, and low-confidence escalation-heavy items."
        ),
        "requires_selection_profile": "broader",
        "min_items": 3,
        "max_items": 5,
        "parent_candidate_target": (2, 3),
        "include_duplicate_control": True,
    },
}

DUPLICATE_LOG_CONTROL_CANDIDATES = [
    "Levitin-The Psychology of Music- Rhythm and Movement-2017-An.pdf",
]

PARENT_VALIDATION_ROUTE_TYPES = {
    "book_parent_record_candidate",
    "edited_volume_parent_record_candidate",
    "parent_plus_child_chapter_candidates",
}


def resolve_pilot_profile(name: str | None) -> str:
    profile = (name or "none").strip().lower()
    if profile in {"", "none", "standard"}:
        return "none"
    if profile not in PILOT_PROFILES:
        return "none"
    return profile


def is_hash_like_filename(name: str) -> bool:
    stem = Path(name).stem
    low = stem.lower()
    if re.fullmatch(r"-?\d{5,}", stem):
        return True
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", stem, re.I):
        return True
    compact = stem.replace("-", "")
    if len(compact) >= 16 and re.fullmatch(r"[0-9a-f]+", compact, re.I):
        return True
    if low in {"download", "download (1)", "untitled"}:
        return True
    return False


def safe_live_parent_score(pdf: dict[str, Any]) -> int:
    name = pdf.get("name", "").lower()
    score = 0
    doc = pdf.get("document_type_classification", "other_or_unknown")
    if doc == "edited_volume":
        score += 40
    elif doc == "book":
        score += 30
    for marker, weight in (
        ("handbooks in psychology", 20),
        ("oxford handbooks", 18),
        ("routledge companion", 18),
        ("cambridge handbooks", 18),
        ("handbook", 12),
        ("editor", 10),
        ("companion", 10),
        ("series", 8),
        ("formalized", 8),
        ("nonlinear science", 8),
    ):
        if marker in name:
            score += weight
    if len(pdf.get("name", "")) >= 35:
        score += 5
    if is_hash_like_filename(pdf.get("name", "")):
        score -= 200
    return score


def select_safe_live_pdfs(
    prescan: dict[str, Any],
    max_papers: int | None,
    *,
    pdf_eligible: Callable[[dict[str, Any], str], bool],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return curated PDF list and selection audit metadata."""
    cap = min(max(max_papers or 5, 3), 5)
    by_name = {p["name"]: p for p in prescan.get("pdfs", [])}
    audit: dict[str, Any] = {
        "excluded_hash_like": [],
        "excluded_other_or_unknown": [],
        "excluded_non_parent_types": [],
        "ranked_parent_candidates": [],
    }

    parent_pool: list[tuple[int, dict[str, Any]]] = []
    for pdf in prescan.get("pdfs", []):
        name = pdf["name"]
        if not pdf_eligible(pdf, "broader"):
            continue
        if is_hash_like_filename(name):
            audit["excluded_hash_like"].append(name)
            continue
        doc = pdf.get("document_type_classification", "other_or_unknown")
        if doc == "other_or_unknown":
            audit["excluded_other_or_unknown"].append(name)
            continue
        if doc not in {"book", "edited_volume"}:
            audit["excluded_non_parent_types"].append(f"{name} ({doc})")
            continue
        score = safe_live_parent_score(pdf)
        parent_pool.append((score, pdf))
        audit["ranked_parent_candidates"].append(
            {"name": name, "document_type": doc, "score": score}
        )

    parent_pool.sort(key=lambda x: (-x[0], x[1]["name"]))
    audit["ranked_parent_candidates"].sort(key=lambda x: (-x["score"], x["name"]))

    parent_target = min(3, max(2, cap - 1))
    selected: list[dict[str, Any]] = [p for _, p in parent_pool[:parent_target]]

    duplicate_added = False
    if cap >= 4:
        for dup_name in DUPLICATE_LOG_CONTROL_CANDIDATES:
            dup = by_name.get(dup_name)
            if dup and pdf_eligible(dup, "broader"):
                if not any(p["name"] == dup_name for p in selected):
                    selected.append(dup)
                    audit["duplicate_control_added"] = dup_name
                    duplicate_added = True
                break

    if not duplicate_added:
        idx = parent_target
        while len(selected) < cap and idx < len(parent_pool):
            candidate = parent_pool[idx][1]
            if not any(p["name"] == candidate["name"] for p in selected):
                selected.append(candidate)
            idx += 1

    audit["selected_count"] = len(selected[:cap])
    audit["parent_candidate_count"] = sum(
        1
        for p in selected[:cap]
        if p.get("document_type_classification") in {"book", "edited_volume"}
    )
    return selected[:cap], audit


def assess_live_pilot_suitability(
    *,
    name: str,
    prescan_doc_type: str,
    route: dict[str, Any],
    stage_e_item: dict[str, Any] | None,
) -> dict[str, Any]:
    """Post Stage D/E suitability notes for safe-live pilot items."""
    stage_e = stage_e_item or {}
    dup = stage_e.get("duplicate_version_resolution") or {}
    meta = stage_e.get("metadata_resolution") or {}
    stage_d_type = stage_e.get("stage_d_document_type") or prescan_doc_type
    refined_type = (stage_e.get("document_type_resolution") or {}).get("resolved_document_type") or stage_d_type
    confidence = dup.get("confidence") or meta.get("confidence") or route.get("stage_e_resolution", {}).get("confidence")
    route_type = route.get("route_type", "")

    parent_validation_required = route_type in PARENT_VALIDATION_ROUTE_TYPES
    if route.get("duplicate_log_only"):
        suitable = True
        reasons = ["Proven duplicate log-only control; no DB/PDF/eval writes expected."]
    elif parent_validation_required:
        suitable = confidence in {"medium", "high"} or safe_live_parent_score({"name": name, "document_type_classification": prescan_doc_type}) >= 50
        reasons = [
            "Clear non-hash filename with handbook/editor/series signals.",
            "Book or edited-volume parent path matches Koren/Harris pilot pattern.",
        ]
        if confidence == "low":
            reasons.append("Stage E confidence is low; parent validation (Stage G/J/O) required before any live write.")
            suitable = True  # still suitable for orchestrated test if validation gates enforced
        if is_hash_like_filename(name):
            suitable = False
            reasons.append("Hash-like filename excluded from safe-live pilot.")
    else:
        suitable = False
        reasons = [f"Route {route_type} is not targeted for first safe-live parent pilot."]

    return {
        "filename": name,
        "prescan_document_type": prescan_doc_type,
        "stage_d_document_type": stage_d_type,
        "refined_document_type": refined_type,
        "route_type": route_type,
        "confidence": confidence,
        "parent_validation_required_before_write": parent_validation_required and not route.get("duplicate_log_only"),
        "suitable_for_first_orchestrated_live_test": suitable,
        "suitability_reasons": reasons,
    }
