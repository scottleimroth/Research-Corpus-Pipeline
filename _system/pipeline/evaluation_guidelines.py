"""Load PhD-level evaluation/red-team guidelines and normalize red-team audit objects."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

GUIDELINES_PATH = Path(__file__).resolve().parent.parent / "standards" / "evaluation_redteam_guidelines.md"

RATING_HIERARCHY = ["landmark", "strong", "adequate", "weak", "flawed"]
RATING_ALIASES = {"moderate": "adequate", "junk": "flawed"}
VALID_RATINGS = frozenset(
    {
        "landmark",
        "strong",
        "adequate",
        "weak",
        "flawed",
        "not_applicable",
        "not_ratable_reference_material",
    }
)

VALID_FRAMEWORK_VIOLATIONS = frozenset(
    {
        "methodology",
        "sample_size",
        "statistical_rigor",
        "measurement_validity",
        "reporting_quality",
        "reproducibility",
        "overclaiming",
        "article_type_mismatch",
        "domain_fit",
        "historical_context",
        "insufficient_evidence",
        "none",
    }
)

REQUIRED_RED_TEAM_AUDIT_FIELDS = frozenset(
    {
        "original_rating",
        "final_rating",
        "rating_changed",
        "change_direction",
        "change_reason",
        "framework_violation",
        "confidence",
        "red_team_summary",
        "survived_red_team",
        "key_attack_points",
        "historical_context_note",
        "article_type_consistency_note",
    }
)


def guidelines_path() -> Path:
    return GUIDELINES_PATH


def guidelines_exist() -> bool:
    return GUIDELINES_PATH.is_file() and GUIDELINES_PATH.stat().st_size > 200


def load_operational_guidelines() -> str:
    """Return compact operational standard text for prompt injection."""
    if not guidelines_exist():
        return "(evaluation_redteam_guidelines.md missing — regenerate standards/evaluation_redteam_guidelines.md)"
    return GUIDELINES_PATH.read_text(encoding="utf-8")


def _normalize_rating(raw: Any) -> str:
    if raw is None:
        return ""
    r = str(raw).strip().lower()
    r = re.sub(r"[\s\-]+", "_", r)
    return RATING_ALIASES.get(r, r)


def _rating_index(rating: str) -> int | None:
    r = _normalize_rating(rating)
    if r in RATING_HIERARCHY:
        return RATING_HIERARCHY.index(r)
    return None


def _change_direction(original: str, final: str) -> str:
    o, f = _rating_index(original), _rating_index(final)
    if o is None or f is None or o == f:
        return "unchanged"
    if f > o:
        return "downgrade"
    if f < o:
        return "upgrade"
    return "unchanged"


def _normalize_framework_violations(raw: Any) -> list[str]:
    if raw is None:
        return ["none"]
    if isinstance(raw, str):
        items = [raw.strip().lower()] if raw.strip() else ["none"]
    elif isinstance(raw, list):
        items = [str(x).strip().lower() for x in raw if str(x).strip()]
    else:
        items = ["none"]
    out = [v for v in items if v in VALID_FRAMEWORK_VIOLATIONS]
    return out or ["none"]


def normalize_red_team_audit(
    raw: dict[str, Any],
    *,
    original_rating: str,
    auditor: str | None = None,
) -> dict[str, Any]:
    """Merge model output (new or legacy schema) into canonical red_team_audit object."""
    original = _normalize_rating(original_rating)
    final = _normalize_rating(
        raw.get("final_rating") or raw.get("red_team_rating") or raw.get("recommended_rating") or original
    )
    survived = raw.get("survived_red_team")
    if survived is None:
        survived = raw.get("red_team_survival", True)
    survived = bool(survived)

    summary = str(
        raw.get("red_team_summary") or raw.get("red_team_notes") or raw.get("change_reason") or ""
    ).strip()
    change_reason = str(raw.get("change_reason") or raw.get("downgrade_reason") or "").strip()
    if not change_reason and not survived and final != original:
        change_reason = summary[:500] if summary else "Red-team downgrade"

    rating_changed = bool(raw.get("rating_changed")) if "rating_changed" in raw else (final != original)
    direction = str(raw.get("change_direction") or "").strip().lower()
    if direction not in {"upgrade", "downgrade", "unchanged"}:
        direction = _change_direction(original, final)

    attack_points = raw.get("key_attack_points")
    if not isinstance(attack_points, list):
        attack_points = []
    attack_points = [str(x).strip() for x in attack_points if str(x).strip()]

    confidence = str(raw.get("confidence") or "medium").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"

    audit = {
        "original_rating": original,
        "final_rating": final,
        "rating_changed": rating_changed,
        "change_direction": direction,
        "change_reason": change_reason or None,
        "framework_violation": _normalize_framework_violations(raw.get("framework_violation")),
        "confidence": confidence,
        "red_team_summary": summary,
        "survived_red_team": survived,
        "key_attack_points": attack_points,
        "historical_context_note": raw.get("historical_context_note"),
        "article_type_consistency_note": raw.get("article_type_consistency_note"),
        "auditor": str(raw.get("auditor") or auditor or config.EVAL_MODEL),
        "timestamp": str(raw.get("timestamp") or datetime.now(timezone.utc).isoformat()),
    }
    return audit


def validate_red_team_payload(
    raw: dict[str, Any] | None,
    *,
    original_rating: str,
    strict_attack_points: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and normalize red-team JSON. No API calls."""
    if not isinstance(raw, dict):
        return None, "red-team payload is not a JSON object"

    audit = normalize_red_team_audit(raw, original_rating=original_rating)
    final = audit["final_rating"]
    if final not in VALID_RATINGS:
        return None, f"invalid final_rating: {final!r}"

    if not audit["red_team_summary"]:
        return None, "red_team_summary / red_team_notes required"

    high_tiers = frozenset({"landmark", "strong"})
    if strict_attack_points and audit["original_rating"] in high_tiers and not audit["key_attack_points"]:
        return None, "key_attack_points required for LANDMARK/STRONG red-team"

    missing = sorted(REQUIRED_RED_TEAM_AUDIT_FIELDS - set(audit.keys()))
    if missing:
        return None, f"missing audit fields: {missing}"

    for fv in audit["framework_violation"]:
        if fv not in VALID_FRAMEWORK_VIOLATIONS:
            return None, f"invalid framework_violation: {fv!r}"

    return audit, None


def resolve_final_rating_after_red_team(original_rating: str, audit: dict[str, Any]) -> str:
    """Apply downgrade-only policy (matches legacy evaluate_one behaviour)."""
    original = _normalize_rating(original_rating)
    final = _normalize_rating(audit.get("final_rating") or original)
    survived = bool(audit.get("survived_red_team", True))
    if survived:
        return original
    o_idx, f_idx = _rating_index(original), _rating_index(final)
    if o_idx is not None and f_idx is not None and f_idx > o_idx:
        return final
    return original


def apply_red_team_audit_to_classification(cl: dict[str, Any], audit: dict[str, Any]) -> None:
    """Write audit object and legacy compatibility fields into classification dict."""
    cl["red_team_audit"] = dict(audit)
    cl["red_team_notes"] = audit.get("red_team_summary") or ""
    cl["red_team_survival"] = audit.get("survived_red_team")
    if audit.get("change_direction") == "downgrade" and audit.get("change_reason"):
        cl["red_team_downgrade_reason"] = audit["change_reason"]
    elif "red_team_downgrade_reason" in cl and audit.get("change_direction") != "downgrade":
        cl.pop("red_team_downgrade_reason", None)


def legacy_red_team_payload_compatible() -> dict[str, Any]:
    """Sample legacy-shaped payload for regression tests."""
    return {
        "red_team_notes": "Legacy format still supported.",
        "red_team_survival": True,
        "recommended_rating": "strong",
        "downgrade_reason": None,
    }
