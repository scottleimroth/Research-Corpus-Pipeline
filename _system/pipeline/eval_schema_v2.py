"""
Compact evaluation schema v2 — shorter outputs, same auditability core.
Validation mirrors evaluate_paper.py behaviour; no DB writes here.
"""
import re

from evaluate_paper import (  # noqa: E402
    RATING_ALIASES,
    VALID_RATINGS,
    normalize_rating_label,
)

# Bounded field limits (chars)
MAX_JUSTIFICATION_CHARS = 2_500
MAX_EVIDENCE_BULLET_CHARS = 400
MAX_EVIDENCE_BULLETS = 12
MAX_LIST_ITEMS = 10
MAX_REDTEAM_NOTES_CHARS = 2_000

REQUIRED_V2_TOP = {
    "classification",
    "tags",
    "notes_and_flags",
}
REQUIRED_V2_CLASSIFICATION = {
    "rating",
    "rating_justification",
    "applicable_standards_applied",
    "design_type_detected",
    "primary_measures_detected",
    "era_judgment",
    "standards_violated",
    "key_strengths",
    "key_weaknesses",
    "evidence_bullets",
    "confidence",
}

V2_SYSTEM_PROMPT_APPENDIX = """
## OUTPUT FORMAT (SCHEMA V2 COMPACT)
Return ONLY valid JSON. Use schema version "v2_compact".

Required compact shape:
{
  "schema_version": "v2_compact",
  "classification": {
    "rating": "landmark|strong|adequate|weak|flawed|not_applicable|not_ratable_reference_material",
    "rating_justification": "Concise multi-sentence justification (max ~2500 chars). Reference applicable standards.",
    "applicable_standards_applied": ["short names"],
    "design_type_detected": "RCT|observational|systematic_review|meta_analysis|...",
    "primary_measures_detected": ["EEG", "HRV", ...],
    "era_judgment": "brief era note",
    "standards_violated": ["specific violations or empty"],
    "key_strengths": ["max 10 bullets"],
    "key_weaknesses": ["max 10 bullets"],
    "evidence_bullets": [
      {"section": "methods", "claim": "...", "support": "supporting|contradicting|neutral"}
    ],
    "confidence": "high|medium|low"
  },
  "tags": {
    "domain": [], "design": [], "methods": [], "custom_tags": []
  },
  "notes_and_flags": {
    "general_notes": null,
    "revisit_flag": false,
    "revisit_reason": null
  }
}

Evidence bullets MUST cite section anchors (abstract, methods, results, etc.).
Do not omit methods/results/limitations review when present in the provided text.
"""


def _bound_str(value, max_len: int, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    s = value.strip()
    if not s:
        raise ValueError(f"{field} must be non-empty")
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


def _bound_list(value, max_items: int, field: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return value[:max_items]


def validate_eval_payload_v2(eval_result: dict):
    """Validate compact v2 payload. Returns (normalized, error)."""
    if not isinstance(eval_result, dict):
        return None, "Payload is not a JSON object"

    if eval_result.get("schema_version") != "v2_compact":
        return None, "schema_version must be 'v2_compact'"

    missing_top = sorted(k for k in REQUIRED_V2_TOP if k not in eval_result)
    if missing_top:
        return None, f"Missing required top-level fields: {missing_top}"

    cl = eval_result.get("classification")
    if not isinstance(cl, dict):
        return None, "classification must be an object"

    missing_cl = sorted(k for k in REQUIRED_V2_CLASSIFICATION if k not in cl)
    if missing_cl:
        return None, f"Missing required classification fields: {missing_cl}"

    normalized = dict(eval_result)
    normalized_cl = dict(cl)
    normalized_cl["rating"] = normalize_rating_label(normalized_cl.get("rating"))
    rating = normalized_cl["rating"]
    if rating not in VALID_RATINGS:
        return None, f"Invalid rating after normalization: {rating!r}"

    normalized_cl["rating_justification"] = _bound_str(
        normalized_cl.get("rating_justification"),
        MAX_JUSTIFICATION_CHARS,
        "classification.rating_justification",
    )

    conf = str(normalized_cl.get("confidence", "")).strip().lower()
    if conf not in ("high", "medium", "low"):
        return None, "classification.confidence must be high|medium|low"
    normalized_cl["confidence"] = conf

    for field in (
        "applicable_standards_applied",
        "primary_measures_detected",
        "standards_violated",
        "key_strengths",
        "key_weaknesses",
    ):
        normalized_cl[field] = _bound_list(
            normalized_cl.get(field), MAX_LIST_ITEMS, f"classification.{field}"
        )

    bullets = normalized_cl.get("evidence_bullets")
    if not isinstance(bullets, list) or not bullets:
        return None, "classification.evidence_bullets must be a non-empty list"
    norm_bullets = []
    for i, b in enumerate(bullets[:MAX_EVIDENCE_BULLETS]):
        if not isinstance(b, dict):
            return None, f"evidence_bullets[{i}] must be an object"
        section = _bound_str(b.get("section", ""), 80, f"evidence_bullets[{i}].section")
        claim = _bound_str(b.get("claim", ""), MAX_EVIDENCE_BULLET_CHARS, f"evidence_bullets[{i}].claim")
        support = str(b.get("support", "neutral")).strip().lower()
        if support not in ("supporting", "contradicting", "neutral"):
            support = "neutral"
        norm_bullets.append({"section": section, "claim": claim, "support": support})
    normalized_cl["evidence_bullets"] = norm_bullets

    if not isinstance(normalized.get("tags"), dict):
        return None, "tags must be an object"

    normalized["classification"] = normalized_cl
    return normalized, None


def expand_v2_to_v1_merge_fields(normalized_v2: dict) -> dict:
    """
    Map v2 compact result into v1-shaped dict for merge_evaluation compatibility
    when wiring is enabled later.
    """
    cl = normalized_v2["classification"]
    return {
        "classification": {
            "rating": cl["rating"],
            "rating_justification": cl["rating_justification"],
            "applicable_standards_applied": cl["applicable_standards_applied"],
            "design_type_detected": cl["design_type_detected"],
            "primary_measures_detected": cl["primary_measures_detected"],
            "era_judgment": cl["era_judgment"],
            "standards_violated": cl["standards_violated"],
            "key_strengths": cl["key_strengths"],
            "key_weaknesses": cl["key_weaknesses"],
        },
        "tags": normalized_v2.get("tags", {}),
        "notes_and_flags": normalized_v2.get("notes_and_flags", {}),
        "basic_metadata": {},
        "abstract_keywords": {},
        "methodology": {},
        "statistical_analysis": {},
    }
