#!/usr/bin/env python3
"""
evaluate_paper.py -- Evaluate papers via Anthropic API (Sonnet).

Two-pass evaluation:
  Pass 1: Full schema evaluation (all sections + classification)
  Pass 2: Red team (separate call, adversarial critique)
  If red team downgrades: save downgraded rating.

Usage:
  python3 evaluate_paper.py                         # Count unevaluated
  python3 evaluate_paper.py --list                  # List unevaluated papers
  python3 evaluate_paper.py --next [N]              # Show next N unevaluated IDs
  python3 evaluate_paper.py --evaluate              # Evaluate next unevaluated paper
  python3 evaluate_paper.py --evaluate --count 5    # Evaluate next 5
  python3 evaluate_paper.py --paper-id Smith2024    # Evaluate specific paper
  python3 evaluate_paper.py --force --paper-id X    # Re-evaluate even if already rated
  python3 evaluate_paper.py --dry-run               # Show what would be evaluated
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic

# --- Portable path bootstrap (Phase 2 repath) ---
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from config import EVAL_LOG, EVAL_MODEL, EVAL_MODEL_KEY  # noqa: E402
from eval_model_config import assert_eval_api_key, build_eval_escalation_ladder  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import papers_db  # noqa: E402
from evaluation_guidelines import (  # noqa: E402
    apply_red_team_audit_to_classification,
    load_operational_guidelines,
    normalize_red_team_audit,
    resolve_final_rating_after_red_team,
    validate_red_team_payload,
)

EVAL_LOG = Path(EVAL_LOG)

MODEL = EVAL_MODEL
MODEL_KEY = EVAL_MODEL_KEY
MAX_TOKENS = 8192
FAST_MAX_TOKENS = 4096
DELAY_BETWEEN_PAPERS = 5
FAST_DELAY_BETWEEN_PAPERS = 0
MAX_RETRIES = 3
RETRY_BASE_DELAY = 30

# Evaluation input budget (quality-first; no blind front truncation).
# Claude Sonnet ~200k input tokens; reserve headroom for system prompt + rubric + header.
CHARS_PER_TOKEN_ESTIMATE = 3.5
MODEL_CONTEXT_INPUT_TOKENS = 200_000
RESERVED_INPUT_TOKENS = 45_000
MAX_EVAL_INPUT_CHARS = int(
    (MODEL_CONTEXT_INPUT_TOKENS - RESERVED_INPUT_TOKENS) * CHARS_PER_TOKEN_ESTIMATE
)
REFERENCE_MAX_CHARS = 15_000
SUPPLEMENT_APPENDIX_MAX_CHARS = 30_000
# Fair trim for large monolithic materialized text when evidence-pack would starve input
FAIR_TRIM_MAX_CHARS = 80_000
MIN_USABLE_EVAL_INPUT_CHARS = 5_000

VALID_RATINGS = {
    "landmark", "strong", "adequate", "weak", "flawed",
    "not_applicable", "not_ratable_reference_material"
}
RATING_ALIASES = {
    "moderate": "adequate",
    "junk": "flawed",
}
REQUIRED_TOP_LEVEL_FIELDS = {
    "basic_metadata",
    "abstract_keywords",
    "methodology",
    "statistical_analysis",
    "classification",
    "tags",
    "notes_and_flags",
}
REQUIRED_CLASSIFICATION_FIELDS = {
    "rating",
    "rating_justification",
    "applicable_standards_applied",
    "design_type_detected",
    "primary_measures_detected",
    "era_judgment",
    "standards_violated",
    "key_strengths",
    "key_weaknesses",
}

# Fields to PRESERVE from existing data (never overwrite)
PRESERVE_FIELDS = {"file_info", "identifiers", "full_text"}

# Path to the methodological standards rubric.
# Primary = distilled hierarchical rubric (Tier U/D/M/C/E, built 2026-05-27).
# Fallback = original auto-compiled rubric from extract_guideline_rubrics.py.
STANDARDS_DIR = (
    Path(__file__).resolve().parent.parent /
    "Methods Paper Project" / "standards"
)
STANDARDS_RUBRIC_PATH = STANDARDS_DIR / "standards-rubric-distilled.md"
STANDARDS_RUBRIC_FALLBACK = STANDARDS_DIR / "standards-rubric.md"


def load_standards_rubric():
    """Load the methodological standards rubric. Returns string or fallback note."""
    if STANDARDS_RUBRIC_PATH.exists():
        return STANDARDS_RUBRIC_PATH.read_text(encoding="utf-8")
    if STANDARDS_RUBRIC_FALLBACK.exists():
        return STANDARDS_RUBRIC_FALLBACK.read_text(encoding="utf-8")
    return ("(standards-rubric not yet generated - run "
            "extract_guideline_rubrics.py --execute to build it)")


def build_eval_system_prompt():
    """Compose eval system prompt: core task + operational guidelines + rubric."""
    guidelines = load_operational_guidelines()
    rubric = load_standards_rubric()
    return (
        EVAL_SYSTEM_PROMPT.rstrip()
        + "\n\n## PHD EVALUATION & RED-TEAM OPERATIONAL STANDARD\n\n"
        + guidelines
        + "\n\n## METHODOLOGICAL STANDARDS RUBRIC\n\n"
        + rubric
        + "\n"
    )


def build_red_team_system_prompt():
    """Compose red-team system prompt: adversarial checklist + guidelines + rubric."""
    guidelines = load_operational_guidelines()
    rubric = load_standards_rubric()
    return (
        RED_TEAM_SYSTEM_PROMPT.rstrip()
        + "\n\n## PHD EVALUATION & RED-TEAM OPERATIONAL STANDARD\n\n"
        + guidelines
        + "\n\n## METHODOLOGICAL STANDARDS RUBRIC (for comparison)\n\n"
        + rubric
        + "\n"
    )

# ============================================================
# EVALUATION SYSTEM PROMPT (from methods paper pipeline)
# ============================================================
EVAL_SYSTEM_PROMPT = """You are a PhD-level academic paper evaluator performing a systematic quality audit of a research library. Follow the PHD EVALUATION & RED-TEAM OPERATIONAL STANDARD section below (loaded after this block).

## YOUR TASK
Read the full paper text / evidence pack provided and produce a complete JSON evaluation following the exact schema below.

## ARTICLE TYPE (decide first)
Explicitly classify before rating:
- empirical research paper
- review / meta-analysis / systematic review
- theoretical / commentary
- book / manual / handbook / reference material
- supplement / erratum / child document (not a standalone research rating)

Do NOT assign landmark/strong/adequate/weak/flawed to reference material — use not_ratable_reference_material.

## EVIDENCE
- Use the FULL text/evidence pack — no abstract-only rating unless input is clearly insufficient (note in era_judgment / justification).
- Verify methodology matches claims.
- Rate METHODS on the page, not reputation or citations.

## APPLICABLE-STANDARDS PROTOCOL (before rating)
1. Identify design, primary measures, publication year.
2. From the METHODOLOGICAL STANDARDS RUBRIC, list ONLY applicable standards (design + measures + era).
3. Score compliance; do not penalise for non-applicable guidelines.
4. Apply rating definitions from the operational standard.

## OUTPUT FORMAT
Respond with ONLY valid JSON matching the schema below. No markdown, no explanation, no preamble. Just the JSON object.

{
  "basic_metadata": {
    "title": "Full paper title",
    "authors": ["Author 1 Full Name", "Author 2 Full Name"],
    "corresponding_author": "Name or null",
    "corresponding_author_email": "email or null",
    "year": 2024,
    "journal": "Journal Name",
    "volume": "string or null",
    "issue": "string or null",
    "pages": "string or null",
    "doi": "DOI string or null",
    "publisher": "Publisher or null",
    "language": "English",
    "article_type": "empirical|review|meta-analysis|systematic-review|theoretical|case-study|protocol|commentary|letter|erratum"
  },

  "abstract_keywords": {
    "abstract": "Full abstract text",
    "keywords": ["keyword1", "keyword2"]
  },

  "ethics_and_consent": {
    "informed_consent": "yes|not_mentioned|na",
    "ethics_approval": "description or not_mentioned or na",
    "declaration_of_helsinki": true,
    "data_privacy_anonymization": "yes|no|partial|not_mentioned",
    "vulnerable_population_considerations": "yes|no|not_applicable"
  },

  "sample_characteristics": {
    "total_sample_size": 0,
    "age_range_or_mean": "string description",
    "sex_breakdown": "string description",
    "population_type": "healthy_adults|musicians|children|clinical_group|mixed|other",
    "clinical_group_description": "string or null",
    "weird_bias_assessment": "high|moderate|low|not_applicable",
    "recruitment_method": "string or null"
  },

  "replication_and_preregistration": {
    "replication_status": "original|direct_replication|conceptual_replication|replicated|failed_replication",
    "preregistration_status": "osf|aspredict|journal_registered|none|not_applicable",
    "preregistration_link": "URL or null"
  },

  "methodology": {
    "research_design": "RCT|quasi_experimental|observational|longitudinal|cross_sectional|case_control|cohort|single_case|qualitative|mixed_methods",
    "exclusion_criteria": "clearly_stated|vague|not_stated",
    "attrition_reporting": "thorough|partial|absent",
    "missing_data_handling": "method_described|not_described|na",
    "assumptions_testing_reported": true,
    "blinding": "double|single|none|na",
    "randomization": "method_described|not_described|na",
    "randomization_method": "string or null",
    "control_conditions": "active|passive|waitlist|none|na",
    "counterbalancing": "yes|no|na",
    "order_effects_controlled": true
  },

  "statistical_analysis": {
    "statistical_approach": "frequentist|bayesian|both|descriptive_only",
    "exact_tests_used": ["list of tests"],
    "multiple_comparison_corrections": "method_used|none|not_applicable",
    "correction_method": "bonferroni|fdr|holm|none|na",
    "effect_sizes_reported": true,
    "effect_size_types": ["list or empty"],
    "power_analysis": "a_priori|post_hoc|not_reported",
    "sample_size_justification": "string or null"
  },

  "quality_indicators": {
    "open_data_availability": "full|partial|none|na",
    "open_data_link": "URL or null",
    "open_code_availability": "full|partial|none|na",
    "open_code_link": "URL or null",
    "funding_source": "declared|undeclared|not_applicable",
    "funding_details": "string or null",
    "conflicts_of_interest_statement": true,
    "conflicts_details": "string or null"
  },

  "measurement_quality": {
    "inter_rater_reliability_reported": true,
    "inter_rater_reliability_value": "number or null",
    "test_retest_reliability": "number or null",
    "internal_consistency": "number or null",
    "validity_measures_discussed": true,
    "validity_types": ["list or empty"],
    "pilot_testing": true,
    "manipulation_checks": "yes|no|na"
  },

  "stimulus_and_procedures": {
    "stimulus_type": "music|speech|white_noise|visual|other|na",
    "stimulus_description": "detailed description",
    "stimulus_naturalistic": true,
    "stimulus_materials_described": "fully_replicable|adequate|insufficient|na",
    "stimulus_materials_available": "publicly_shared|upon_request|not_available|na",
    "stimulus_materials_link": "URL or null",
    "procedure_replicability": "fully|partially|insufficient",
    "exact_procedure_description": "assessment string",
    "equipment_software_specifications": "description",
    "equipment_details": "list with versions or null"
  },

  "analysis_pipeline": {
    "analysis_approach": "univariate|multivariate|machine_learning|qualitative|mixed",
    "specific_techniques": ["list of techniques"],
    "pipeline_replicable": true,
    "calibration_procedures_described": true,
    "code_availability": "yes|partial|no"
  },

  "reporting_standards": {
    "guidelines_followed": ["list or empty"],
    "credit_author_contributions_listed": true,
    "data_availability_statement": true,
    "competing_interests_statement": true,
    "limitations_section": "thorough|superficial|absent",
    "generalizability_discussed": true
  },

  "meta_analysis_specific": {
    "prospero_registration": "yes|no|na",
    "prospero_number": "string or null",
    "risk_of_bias_assessment": "yes|no|na",
    "risk_of_bias_method": "string or null",
    "heterogeneity_reported": true,
    "i_squared": "number or null",
    "q_statistic": "number or null",
    "publication_bias_assessed": "funnel_plot|eggers_test|other|none|na",
    "sensitivity_analyses": true
  },

  "supplements": {
    "has_supplements": true,
    "supplement_type": "string or null",
    "supplement_link": "URL or null",
    "supplement_notes": "string or null"
  },

  "classification": {
    "rating": "landmark|strong|adequate|weak|flawed|not_applicable|not_ratable_reference_material",
    "rating_justification": "Multi-paragraph justification (minimum 200 chars). MUST reference applicable_standards_applied. For WEAK but possibly influential papers, include hypothesis for why.",
    "applicable_standards_applied": ["list of short standard names you applied, e.g. 'Quigley 2024 HRV', 'CONSORT 2010', 'Keil 2022 ERP'. Empty list = universal standards only."],
    "design_type_detected": "RCT|observational|systematic_review|meta_analysis|case_study|theoretical|qualitative|protocol|commentary|other",
    "primary_measures_detected": ["list of primary measurement modalities, e.g. 'HRV', 'EEG', 'EDA', 'self_report'"],
    "era_judgment": "Brief note on era-appropriate evaluation (e.g. '1985 paper - judged against Jasper 1958, Cobb 1958; not penalised for absent preregistration')",
    "standards_violated": ["list of specific standard violations from applicable_standards_applied, empty if none"],
    "key_strengths": ["list of key strengths"],
    "key_weaknesses": ["list of key weaknesses"]
  },

  "tags": {
    "domain": ["list of domain tags"],
    "design": ["list"],
    "data_type": ["list"],
    "methods": ["list"],
    "constructs": ["list"],
    "population": ["list"],
    "paradigm": ["list"],
    "stimulus": ["list"],
    "analysis": ["list"],
    "custom_tags": []
  },

  "notes_and_flags": {
    "general_notes": "string or null",
    "duplicate_check": "no|preprint_kept|published_kept",
    "duplicate_doi": null,
    "retraction_status": "not_retracted|retracted_reason",
    "reading_difficulty": "easy|moderate|challenging",
    "revisit_flag": false,
    "revisit_reason": null
  }
}"""

# ============================================================
# RED TEAM SYSTEM PROMPT
# ============================================================
RED_TEAM_SYSTEM_PROMPT = """You are an adversarial PhD-level red-team reviewer. Your SOLE job is to stress-test the initial rating using the destructive checklist in the OPERATIONAL STANDARD below.

## APPROACH
- Assume the initial rating may be too generous unless evidence is overwhelming.
- Attack LANDMARK and STRONG hardest; scrutinize ADEQUATE for hidden weaknesses; WEAK may become FLAWED if fatal flaws exist.
- Do NOT downgrade solely for age — separate historical limitations from fatal flaws.
- Do NOT treat books/manuals/reference works as empirical studies.
- Use the full text/evidence pack — not abstract-only critique unless evidence is insufficient.
- Cite specific attack points (sample, stats, measurement, overclaiming, article-type mismatch, etc.).

## OUTPUT FORMAT
Respond with ONLY valid JSON. No markdown, no preamble.

{
  "original_rating": "same as initial pass",
  "final_rating": "your rating after red-team (landmark|strong|adequate|weak|flawed)",
  "rating_changed": true or false,
  "change_direction": "upgrade|downgrade|unchanged",
  "change_reason": "If changed, why. null if unchanged.",
  "framework_violation": ["methodology|sample_size|statistical_rigor|measurement_validity|reporting_quality|reproducibility|overclaiming|article_type_mismatch|domain_fit|historical_context|insufficient_evidence|none"],
  "confidence": "high|medium|low",
  "red_team_summary": "Detailed adversarial critique (required).",
  "survived_red_team": true or false,
  "key_attack_points": ["specific flaw 1", "specific flaw 2"],
  "historical_context_note": "Era-appropriate context or null",
  "article_type_consistency_note": "Whether article type matches the rating or null",
  "red_team_notes": "same as red_team_summary (legacy compatibility)",
  "red_team_survival": "same as survived_red_team (legacy compatibility)",
  "recommended_rating": "same as final_rating (legacy compatibility)",
  "downgrade_reason": "same as change_reason when downgraded, else null"
}"""


def log_eval(message):
    EVAL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(EVAL_LOG, "a") as f:
        f.write(f"[{datetime.utcnow().isoformat()}] {message}\n")


def _load_fulltext(paper):
    """Load full_text dict from paper or DB (read-only)."""
    ft = paper.get("full_text", {})
    if not ft and paper.get("id"):
        ft = papers_db.load_fulltext(paper["id"])
    return ft if isinstance(ft, dict) else {}


def _normalize_section_key(key):
    return re.sub(r"[\s\-]+", "_", str(key).strip().lower())


def _section_priority(key):
    """Lower sort key = earlier in evidence pack."""
    k = _normalize_section_key(key)
    priority = [
        ("abstract", 0),
        ("introduction", 10),
        ("background", 15),
        ("method", 20),
        ("material", 25),
        ("procedure", 30),
        ("result", 40),
        ("finding", 45),
        ("table", 50),
        ("figure", 55),
        ("caption", 56),
        ("discussion", 60),
        ("limitation", 70),
        ("conclusion", 80),
        ("appendix", 90),
        ("supplement", 95),
        ("acknowledg", 100),
        ("reference", 110),
        ("bibliograph", 115),
    ]
    for needle, rank in priority:
        if needle in k:
            return rank
    return 50


def _section_bucket(key):
    k = _normalize_section_key(key)
    if any(x in k for x in ("reference", "bibliograph")):
        return "references"
    if any(x in k for x in ("acknowledg", "funding", "conflict", "author_contribution")):
        return "acknowledgements"
    if any(x in k for x in ("supplement", "appendix")):
        return "supplement"
    if any(x in k for x in ("abstract", "introduction", "background", "method", "result",
                            "discussion", "limitation", "conclusion", "table", "figure", "caption")):
        return "critical"
    return "standard"


def _materialized_top_level_text(ft):
    """Read materialized body from top-level text fields."""
    for key in ("text", "full_text", "content"):
        val = str(ft.get(key) or "").strip()
        if len(val) >= 200:
            return val, f"top_level_{key}"
    return "", "none"


def _head_mid_tail_trim(text: str, max_chars: int = FAIR_TRIM_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    head = text[: int(max_chars * 0.45)]
    tail = text[-int(max_chars * 0.35) :]
    mid_start = max(0, len(text) // 2 - 2000)
    mid = text[mid_start : mid_start + 4000]
    merged = (
        head
        + "\n\n[... middle excerpt for context budget ...]\n\n"
        + mid
        + "\n\n[... document continues ...]\n\n"
        + tail
    )
    return merged[:max_chars]


def _select_canonical_text(ft):
    """
    Choose one canonical body for evaluation (no duplicate full + structured concat).
    Returns (text, source_label).
    Priority: raw_full_text, then sections.full, then balanced structured sections.
    """
    from evidence_coverage import resolve_raw_full_text

    raw = resolve_raw_full_text(ft)
    if raw and len(raw) >= 200:
        return raw, "raw_full_text"

    top, top_src = _materialized_top_level_text(ft)
    sections = ft.get("sections") or {}
    full_block = sections.get("full") or sections.get("Full") or ""
    full_text = str(full_block).strip()
    structured = {
        k: v for k, v in sections.items()
        if _normalize_section_key(k) != "full" and v and str(v).strip()
    }

    if full_text and len(full_text) >= 200:
        return full_text, "full_section"
    if structured and sum(len(str(v)) for v in structured.values()) >= 500:
        return _join_sections_ordered(structured), "structured_sections"
    if top:
        return top, top_src
    return "", "none"


def _join_sections_ordered(sections):
    parts = []
    for key in sorted(sections.keys(), key=lambda k: (_section_priority(k), k)):
        value = str(sections[key]).strip()
        if value:
            parts.append(f"=== {key.upper()} ===\n{value}")
    return "\n\n".join(parts)


def _trim_low_priority_block(key, body, omissions):
    bucket = _section_bucket(key)
    if bucket == "references" and len(body) > REFERENCE_MAX_CHARS:
        omitted = len(body) - REFERENCE_MAX_CHARS
        omissions.append(
            f"references:tail_{omitted}_chars_omitted (bibliography trim, not blind front truncation)"
        )
        body = (
            body[:REFERENCE_MAX_CHARS]
            + f"\n\n[REFERENCES TRUNCATED: {omitted:,} chars omitted from bibliography tail only]"
        )
    elif bucket == "supplement" and len(body) > SUPPLEMENT_APPENDIX_MAX_CHARS:
        omitted = len(body) - SUPPLEMENT_APPENDIX_MAX_CHARS
        omissions.append(
            f"{key}:tail_{omitted}_chars_omitted (supplement/appendix tail trim only)"
        )
        body = (
            body[:SUPPLEMENT_APPENDIX_MAX_CHARS]
            + f"\n\n[{key.upper()} TRUNCATED: {omitted:,} chars omitted from section tail only]"
        )
    return body


def _parse_full_text_by_headings(text):
    """Best-effort split of monolithic full text into pseudo-sections."""
    heading_rx = re.compile(
        r"(?m)^\s*((?:ABSTRACT|INTRODUCTION|(?:MATERIALS?\s+AND\s+)?METHODS?|"
        r"RESULTS?|DISCUSSION|LIMITATIONS?|CONCLUSIONS?|REFERENCES|"
        r"APPENDIX(?:ES)?|SUPPLEMENTARY)(?:\s+[A-Z][A-Z\s]{0,40})?)\s*$"
    )
    matches = list(heading_rx.finditer(text))
    if len(matches) < 3:
        return {}
    out = {}
    for i, m in enumerate(matches):
        name = re.sub(r"\s+", "_", m.group(1).strip().lower())[:40]
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            out[name] = chunk
    return out


def _build_evidence_pack(ft, canonical_text, canonical_source):
    """
    Quality-preserving reduction when canonical text exceeds context budget.
    Never uses blind [:N] front truncation of the whole document.
    """
    omissions = []
    sections = {
        k: v for k, v in (ft.get("sections") or {}).items()
        if _normalize_section_key(k) != "full" and v and str(v).strip()
    }
    struct_chars = sum(len(str(v)) for v in sections.values())

    parsed = _parse_full_text_by_headings(canonical_text)
    if len(parsed) >= 3:
        pack_sections = parsed
    elif sections and struct_chars >= max(5000, int(0.35 * len(canonical_text))):
        pack_sections = dict(sections)
    else:
        pack_sections = {"document_body": canonical_text}

    parts = []
    used = 0
    budget = MAX_EVAL_INPUT_CHARS - 1500  # banner / omission notes

    for key in sorted(pack_sections.keys(), key=lambda k: (_section_priority(k), k)):
        body = _trim_low_priority_block(key, str(pack_sections[key]).strip(), omissions)
        block = f"=== {key.upper()} ===\n{body}"
        bucket = _section_bucket(key)

        if used + len(block) <= budget:
            parts.append(block)
            used += len(block) + 2
            continue

        remaining = budget - used
        if bucket == "critical" and remaining > 8000:
            parts.append(
                block[:remaining]
                + f"\n\n[{key.upper()} PARTIAL: end truncated at context budget; "
                f"start of section preserved — not blind whole-document truncation]"
            )
            omissions.append(f"{key}:partial_end_at_context_budget")
            used = budget
            break
        omissions.append(f"{key}:excluded_at_context_budget ({len(body):,} chars)")

    banner = (
        "=== EVALUATION EVIDENCE PACK ===\n"
        "Structured evidence pack (priority-ordered). "
        "No blind first-N character truncation of the full document.\n"
    )
    if omissions:
        banner += "Omissions: " + "; ".join(omissions) + "\n"

    return banner + "\n\n" + "\n\n".join(parts), omissions


def build_evaluation_input(paper, *, ignore_coverage_gate: bool = False):
    """
    Build evaluation input text and a deterministic plan dict.
    Does not modify paper or database.
    """
    from evidence_coverage import assess_evidence_coverage_from_paper, format_coverage_diagnostic

    ft = _load_fulltext(paper)
    coverage = assess_evidence_coverage_from_paper(paper)
    db_chars = ft.get("total_chars") or coverage.get("raw_text_length") or 0
    if not db_chars:
        canonical_probe, _ = _select_canonical_text(ft)
        db_chars = len(canonical_probe)

    canonical, source = _select_canonical_text(ft)
    plan = {
        "db_full_text_chars": db_chars,
        "canonical_source": source,
        "canonical_chars": len(canonical),
        "strategy": None,
        "input_chars": 0,
        "input_tokens_est": 0,
        "omitted": [],
        "blind_truncation": False,
        "coverage_status": coverage.get("coverage_status"),
        "coverage": coverage,
    }

    if (
        not ignore_coverage_gate
        and coverage.get("likely_journal_article")
        and not coverage.get("evidence_can_support_rating")
    ):
        plan["strategy"] = "insufficient_evidence_coverage"
        plan["coverage_diagnostic"] = format_coverage_diagnostic(coverage)
        return "", plan

    if not canonical or len(canonical) < 200:
        plan["strategy"] = "insufficient_text"
        return "", plan

    if len(canonical) <= MAX_EVAL_INPUT_CHARS:
        plan["strategy"] = "full_text_within_context"
        plan["input_chars"] = len(canonical)
    else:
        text, omissions = _build_evidence_pack(ft, canonical, source)
        if len(text) < MIN_USABLE_EVAL_INPUT_CHARS and len(canonical) >= MIN_USABLE_EVAL_INPUT_CHARS:
            text = _head_mid_tail_trim(canonical)
            plan["strategy"] = "head_mid_tail_trim"
            plan["input_chars"] = len(text)
            plan["omitted"] = omissions + ["evidence_pack_starved; used head_mid_tail_trim"]
            plan["blind_truncation"] = False
            canonical = text
        else:
            plan["strategy"] = "evidence_pack"
            plan["input_chars"] = len(text)
            plan["omitted"] = omissions
            canonical = text

    plan["input_tokens_est"] = max(1, int(plan["input_chars"] / CHARS_PER_TOKEN_ESTIMATE))
    return canonical, plan


def log_evaluation_input(paper_id, plan):
    """Log input strategy to evaluation.log and compact console."""
    omitted = "; ".join(plan["omitted"]) if plan["omitted"] else "none"
    cov = plan.get("coverage") or {}
    msg = (
        f"INPUT | {paper_id} | strategy={plan['strategy']} | "
        f"coverage={plan.get('coverage_status')} | "
        f"raw_len={cov.get('raw_text_length')} | sections={cov.get('section_count')} | "
        f"db_chars={plan['db_full_text_chars']} | input_chars={plan['input_chars']} | "
        f"est_tokens={plan['input_tokens_est']} | source={plan['canonical_source']} | "
        f"blind_trunc={plan['blind_truncation']} | omitted={omitted}"
    )
    log_eval(msg)
    print(
        f"  Input: {plan['strategy']} | coverage={plan.get('coverage_status')} | "
        f"raw={cov.get('raw_text_length', 0):,}c sections={cov.get('section_count', 0)} | "
        f"db={plan['db_full_text_chars']:,}c -> eval={plan['input_chars']:,}c "
        f"(~{plan['input_tokens_est']:,} tok) | source={plan['canonical_source']}"
    )
    if plan.get("coverage_diagnostic"):
        print(f"    Coverage gate: {plan['coverage_diagnostic']}")
    if plan["omitted"]:
        print(f"    Omitted: {omitted}")


def get_paper_text(paper):
    """Extract canonical paper text (no duplicate full+structured concatenation)."""
    ft = _load_fulltext(paper)
    text, _source = _select_canonical_text(ft)
    return text


def is_already_evaluated(paper):
    """Check if paper has complete evaluation for ingest purposes."""
    from eval_completion import is_ingest_evaluation_complete

    ok, _reason = is_ingest_evaluation_complete(paper)
    return ok


def get_unevaluated_papers(force=False):
    """Return papers needing evaluation. Prioritize those with text."""
    all_papers = papers_db.get_all_metadata()
    result = []
    for p in all_papers:
        if not force and is_already_evaluated(p):
            continue
        doc_type = p.get("document_type", "")
        if isinstance(doc_type, dict):
            doc_type = doc_type.get("type", "")
        if doc_type and doc_type.lower() in {
            "reference_material", "manual", "supplement", "book",
            "thesis", "conference_proceedings", "book_chapter"
        }:
            continue
        # Get text char count from full_text in DB
        ft = papers_db.load_fulltext(p.get("id", ""))
        text_chars = ft.get("total_chars", 0) if isinstance(ft, dict) else 0
        if text_chars == 0:
            # Fallback: check sections
            sections = ft.get("sections", {}) if isinstance(ft, dict) else {}
            text_chars = sum(len(str(v)) for v in sections.values()) if sections else 0
        bm = p.get("basic_metadata", {})
        result.append({
            "id": p.get("id", ""),
            "title": (bm.get("title") or "")[:100],
            "year": bm.get("year") or "",
            "journal": bm.get("journal") or "",
            "text_chars": text_chars,
            "has_old_rating": bool(p.get("classification", {}).get("rating") if isinstance(p.get("classification"), dict) else False),
        })

    def sort_key(x):
        chars = x["text_chars"]
        yr = int(x["year"]) if str(x["year"]).isdigit() else 0
        if 5000 <= chars <= 150000:
            bucket = 0
        elif chars < 5000:
            bucket = 1
        else:
            bucket = 2
        return (bucket, -yr, -chars)
    result.sort(key=sort_key)
    return result


def api_call_with_retry(client, **kwargs):
    """Make an API call with retry logic for rate limits and server errors."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            if attempt < MAX_RETRIES:
                wait = RETRY_BASE_DELAY * (2 ** attempt)
                print(f"    Rate limited, waiting {wait}s (retry {attempt + 1}/{MAX_RETRIES})...")
                time.sleep(wait)
                continue
            raise
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 and attempt < MAX_RETRIES:
                wait = RETRY_BASE_DELAY * (2 ** attempt)
                print(f"    Server error {e.status_code}, waiting {wait}s (retry {attempt + 1}/{MAX_RETRIES})...")
                time.sleep(wait)
                continue
            raise
        except anthropic.APIError as e:
            if attempt < MAX_RETRIES:
                wait = RETRY_BASE_DELAY * (2 ** attempt)
                print(f"    API error, waiting {wait}s (retry {attempt + 1}/{MAX_RETRIES})...")
                time.sleep(wait)
                continue
            raise


def merge_evaluation(paper, eval_result):
    """Merge API evaluation into paper, preserving PRESERVE_FIELDS."""
    for key, value in eval_result.items():
        if key in PRESERVE_FIELDS:
            continue
        if key == "classification":
            if "classification" not in paper:
                paper["classification"] = {}
            for ck, cv in value.items():
                paper["classification"][ck] = cv
        elif key == "tags":
            paper["tags"] = value
        elif isinstance(value, dict):
            if key not in paper or not isinstance(paper[key], dict):
                paper[key] = {}
            paper[key].update(value)
        else:
            paper[key] = value


def normalize_rating_label(raw_rating):
    """Normalize model rating labels to canonical DB labels."""
    if raw_rating is None:
        return ""
    r = str(raw_rating).strip().lower()
    r = re.sub(r"[\s\-]+", "_", r)
    return RATING_ALIASES.get(r, r)


def normalize_fast_eval_payload(eval_result: dict) -> dict:
    """Fill minimal schema gaps for bulk backfill models (DeepSeek/Haiku)."""
    out = dict(eval_result) if isinstance(eval_result, dict) else {}
    cl = out.get("classification")
    if not isinstance(cl, dict):
        cl = {k: v for k, v in out.items() if k in REQUIRED_CLASSIFICATION_FIELDS}
        out["classification"] = cl
    cl = dict(out.get("classification") or {})
    defaults: dict[str, object] = {
        "rating": "adequate",
        "rating_justification": "Fast backfill: model returned incomplete schema.",
        "design_type_detected": "unknown",
        "era_judgment": "not_assessed",
        "applicable_standards_applied": [],
        "primary_measures_detected": [],
        "standards_violated": [],
        "key_strengths": [],
        "key_weaknesses": [],
    }
    for key, val in defaults.items():
        if key not in cl or cl[key] in (None, ""):
            cl[key] = val
    for field in (
        "applicable_standards_applied",
        "primary_measures_detected",
        "standards_violated",
        "key_strengths",
        "key_weaknesses",
    ):
        if not isinstance(cl.get(field), list):
            cl[field] = []
    out["classification"] = cl
    if "tags" not in out or not isinstance(out.get("tags"), dict):
        out["tags"] = {}
    for top in REQUIRED_TOP_LEVEL_FIELDS:
        if top not in out:
            if top == "tags":
                out[top] = {}
            elif top == "classification":
                out[top] = cl
            else:
                out[top] = out.get(top) or {}
    return out


def validate_eval_payload(eval_result):
    """Validate and normalize eval JSON before it can be merged/saved."""
    if not isinstance(eval_result, dict):
        return None, "Payload is not a JSON object"

    missing_top = sorted(k for k in REQUIRED_TOP_LEVEL_FIELDS if k not in eval_result)
    if missing_top:
        return None, f"Missing required top-level fields: {missing_top}"

    cl = eval_result.get("classification")
    if not isinstance(cl, dict):
        return None, "classification must be an object"

    missing_cl = sorted(k for k in REQUIRED_CLASSIFICATION_FIELDS if k not in cl)
    if missing_cl:
        return None, f"Missing required classification fields: {missing_cl}"

    normalized = dict(eval_result)
    normalized_cl = dict(cl)
    normalized_cl["rating"] = normalize_rating_label(normalized_cl.get("rating"))
    normalized["classification"] = normalized_cl

    rating = normalized_cl["rating"]
    if rating not in VALID_RATINGS:
        return None, f"Invalid rating after normalization: {rating!r}"

    if not isinstance(normalized_cl.get("rating_justification"), str) or not normalized_cl.get("rating_justification").strip():
        return None, "classification.rating_justification must be a non-empty string"

    list_fields = (
        "applicable_standards_applied",
        "primary_measures_detected",
        "standards_violated",
        "key_strengths",
        "key_weaknesses",
    )
    for field in list_fields:
        if not isinstance(normalized_cl.get(field), list):
            return None, f"classification.{field} must be a list"

    if not isinstance(normalized.get("tags"), dict):
        return None, "tags must be an object"

    return normalized, None


def _eval_result(ok: bool, input_tokens: int = 0, output_tokens: int = 0) -> dict:
    return {"ok": ok, "input_tokens": int(input_tokens or 0), "output_tokens": int(output_tokens or 0)}


def _fast_backfill_red_team_notes(cl: dict, *, model_label: str = "fast") -> str:
    weaknesses = cl.get("key_weaknesses") or []
    if not isinstance(weaknesses, list):
        weaknesses = [str(weaknesses)]
    parts = [str(w).strip() for w in weaknesses if str(w).strip()][:6]
    summary = "; ".join(parts) if parts else "No major weaknesses listed in fast pass."
    return (
        f"{model_label} backfill (single-pass): separate red-team pass deferred. "
        f"Initial pass concerns: {summary}"
    )


def _parse_eval_json(eval_text: str) -> dict:
    text = eval_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return json.loads(text)


def _pass1_with_escalation(
    *,
    paper_id: str,
    header: str,
    text: str,
    research_only_note: str,
    force_research_rating: bool,
    fast_mode: bool,
    max_tokens: int,
    start_key: str,
) -> tuple[dict | None, str | None, int, int]:
    """Try Pass 1 on DeepSeek Pro → Reasoner → Sonnet → Opus until JSON validates."""
    ladder = build_eval_escalation_ladder(profile=config.CORPUS_PROFILE, start_key=start_key)
    if not ladder:
        ladder = [start_key]

    in_tok = out_tok = 0
    from llm_providers import LOCAL_MODEL_KEY, call_llm

    for mk in ladder:
        if mk == LOCAL_MODEL_KEY and mk != ladder[0]:
            print("    LLM local fallback firing for Pass 1 after cloud ladder failure.", flush=True)
            log_eval(f"EVAL_LOCAL_FALLBACK | {paper_id} | trigger=cloud_ladder_failure | model={mk}")
        print(f"  Pass 1: Evaluating {paper_id} ({mk})...")
        model_failed = False
        for attempt in range(2 if force_research_rating else 1):
            try:
                retry_note = ""
                if attempt == 1:
                    retry_note = (
                        "\n\nRETRY: Your prior response used a non-research rating. "
                        "Assign landmark/strong/adequate/weak/flawed only.\n"
                    )
                user_content = (
                    f"Evaluate this paper. Return the COMPLETE JSON schema.\n\n"
                    f"{header}{text}{research_only_note}{retry_note}\n\n"
                    f"Return ONLY the JSON evaluation. No markdown fences."
                )
                eval_text, usage = call_llm(
                    mk,
                    build_eval_system_prompt(),
                    user_content,
                    max_tokens=max_tokens,
                )
                in_tok += int(usage.get("input_tokens") or 0)
                out_tok += int(usage.get("output_tokens") or 0)
                print(
                    f"    Tokens: {usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out "
                    f"(${usage.get('cost_usd', 0):.4f})"
                )
                raw_eval_result = _parse_eval_json(eval_text)
                rating_probe = str((raw_eval_result.get("classification") or {}).get("rating") or "")
                if force_research_rating and rating_probe.startswith("not_"):
                    if attempt == 0:
                        print(f"    Research-only retry on {mk} (got {rating_probe})")
                        continue
                    print(f"    Escalating: {mk} returned non-research rating twice")
                    model_failed = True
                    break

                payload = normalize_fast_eval_payload(raw_eval_result) if fast_mode else raw_eval_result
                eval_result, validation_error = validate_eval_payload(payload)
                if validation_error and fast_mode:
                    payload = normalize_fast_eval_payload(payload)
                    eval_result, validation_error = validate_eval_payload(payload)
                if validation_error:
                    print(f"    Schema failed on {mk}: {validation_error}")
                    log_eval(f"EVAL_SCHEMA_FAILED | {paper_id} | {mk} | {validation_error}")
                    model_failed = True
                    break

                if mk != ladder[0]:
                    log_eval(f"EVAL_ESCALATED | {paper_id} | success_on={mk}")
                return eval_result, mk, in_tok, out_tok
            except json.JSONDecodeError as e:
                if attempt == 0 and force_research_rating:
                    continue
                print(f"    JSON parse failed on {mk}: {e}")
                log_eval(f"EVAL_JSON_FAILED | {paper_id} | {mk} | {e}")
                model_failed = True
                break
            except Exception as e:
                print(f"    API failed on {mk}: {e}")
                log_eval(f"EVAL_API_FAILED | {paper_id} | {mk} | {e}")
                model_failed = True
                break

    print(f"  Pass 1 FAILED for {paper_id} after ladder: {', '.join(ladder)}")
    log_eval(f"EVAL_LADDER_EXHAUSTED | {paper_id} | ladder={ladder}")
    return None, None, in_tok, out_tok


def evaluate_one(
    client,
    paper_id,
    force=False,
    require_research_rating=False,
    allow_blocked_eval: bool = False,
    first_pass_finalize: bool = False,
    min_text_chars: int = 200,
    fast_mode: bool = False,
    fast_model_key: str | None = None,
    start_key: str | None = None,
):
    """Evaluate one paper: DeepSeek Pro default, escalate Reasoner → Sonnet → Opus when stuck."""
    max_tokens = FAST_MAX_TOKENS if fast_mode else MAX_TOKENS
    inter_pass_delay = FAST_DELAY_BETWEEN_PAPERS if fast_mode else DELAY_BETWEEN_PAPERS
    start_key = start_key or fast_model_key or MODEL_KEY
    ladder = build_eval_escalation_ladder(profile=config.CORPUS_PROFILE, start_key=start_key)
    if not ladder:
        assert_eval_api_key(start_key)
        ladder = [start_key]
    model_key = ladder[0]

    paper = papers_db.get_paper(paper_id)
    if not paper:
        print(f"Paper not found: {paper_id}")
        return _eval_result(False)

    if not force and is_already_evaluated(paper):
        print(f"Already evaluated: {paper_id} (use --force to re-evaluate)")
        return _eval_result(False)

    from document_evidence import decide_acceptance_from_paper

    from document_evidence import ACCEPT_RESEARCH

    acceptance = decide_acceptance_from_paper(paper)
    force_research_rating = require_research_rating or acceptance.action == ACCEPT_RESEARCH
    text, input_plan = build_evaluation_input(paper, ignore_coverage_gate=allow_blocked_eval)
    bm = paper.get("basic_metadata", {})
    title = bm.get("title", "Unknown")
    authors = bm.get("authors", [])
    year = bm.get("year", "")
    journal = bm.get("journal", "")

    blocked = (
        ((not allow_blocked_eval) and (not acceptance.can_evaluate))
        or ((not allow_blocked_eval) and input_plan["strategy"] in {"insufficient_text", "insufficient_evidence_coverage"})
        or not text
        or len(text) < int(min_text_chars)
    )
    if blocked:
        reason = input_plan["strategy"] or acceptance.reason or "insufficient_text"
        print(f"  Evaluation blocked for {paper_id}: {reason} ({len(text)} chars).")
        log_eval(
            f"BLOCKED | {paper_id} | acceptance={acceptance.action} | {reason} | "
            f"{input_plan.get('coverage_diagnostic', '')}"
        )
        return _eval_result(False)
    if allow_blocked_eval and not acceptance.can_evaluate:
        print(
            f"  Forcing evaluation for {paper_id} despite acceptance gate "
            f"({acceptance.action}: {acceptance.reason}); text={len(text)} chars."
        )

    log_evaluation_input(paper_id, input_plan)

    author_str = ", ".join(authors) if isinstance(authors, list) else str(authors)
    header = f"Title: {title}\nAuthors: {author_str}\nYear: {year}\nJournal: {journal}\n\n"
    try:
        from metacheck_integration import format_metacheck_for_prompt

        header += format_metacheck_for_prompt(paper)
    except Exception as e:
        log_eval(f"METACHECK_PROMPT_BLOCK_FAILED | {paper_id} | {e}")

    research_only_note = ""
    if force_research_rating:
        research_only_note = (
            "\n\nIMPORTANT: This is an empirical journal article. "
            "You MUST assign a research quality rating "
            "(landmark, strong, adequate, weak, or flawed). "
            "Do NOT use not_applicable or not_ratable_reference_material.\n"
        )

    eval_result, model_key, in_tok, out_tok = _pass1_with_escalation(
        paper_id=paper_id,
        header=header,
        text=text,
        research_only_note=research_only_note,
        force_research_rating=force_research_rating,
        fast_mode=fast_mode,
        max_tokens=max_tokens,
        start_key=start_key,
    )
    if not eval_result:
        return _eval_result(False, in_tok, out_tok)

    mode_label = model_key
    cl = eval_result["classification"]
    rating = cl["rating"]

    if force_research_rating and str(rating).startswith("not_"):
        print(f"    Research-only guard blocked non-research rating: {rating}")
        log_eval(f"RESEARCH_ONLY_BLOCKED | {paper_id} | model={rating} | acceptance={acceptance.action}")
        paper.setdefault("notes_and_flags", {})["reevaluation_required"] = "research_acceptance_requires_research_rating"
        paper["classification"] = paper.get("classification") or {}
        paper["classification"]["rating"] = None
        paper["classification"]["guard_blocked_rating"] = rating
        papers_db.save_paper(paper)
        return _eval_result(False, in_tok, out_tok)

    justification = cl.get("rating_justification", "")
    print(f"    Initial rating: {rating.upper()} ({len(justification)} char justification)")

    from document_evidence import (
        check_article_type_guard,
        check_evaluation_input_health,
        score_journal_article_signals,
    )
    from evidence_coverage import assess_evidence_coverage_from_paper, resolve_raw_full_text

    bm = paper.get("basic_metadata") or {}
    fi = paper.get("file_info") or {}
    ft = paper.get("full_text") or papers_db.load_fulltext(paper_id) or {}
    text_body = resolve_raw_full_text(ft)
    coverage = assess_evidence_coverage_from_paper(paper)
    article_signals = score_journal_article_signals(
        text=text_body,
        filename=str(fi.get("original_filename") or fi.get("renamed_filename") or ""),
        identity={
            "title": bm.get("title"),
            "doi": (paper.get("identifiers") or {}).get("doi"),
            "identity_status": bm.get("identity_status") or "high_confidence",
        },
        page_count=ft.get("page_count") or (ft.get("extraction") or {}).get("page_count"),
    )
    guard = {"allowed": True}
    if not fast_mode:
        guard = check_article_type_guard(
            proposed_rating=rating,
            document_type=paper.get("document_type"),
            article_signals=article_signals,
            identity={"title": bm.get("title")},
        )
    if not guard.get("allowed"):
        print(f"    Article-type guard blocked rating {rating}: {guard.get('reason')}")
        log_eval(f"GUARD_BLOCKED | {paper_id} | {guard.get('reason')} | model={rating}")
        paper.setdefault("notes_and_flags", {})["reevaluation_required"] = guard.get("reason")
        paper["classification"] = paper.get("classification") or {}
        paper["classification"]["rating"] = None
        paper["classification"]["guard_blocked_rating"] = rating
        papers_db.save_paper(paper)
        return _eval_result(False, in_tok, out_tok)

    eval_health = {"can_evaluate": True}
    if not fast_mode:
        eval_health = check_evaluation_input_health(
            {
                "extraction": {
                    "text": text_body,
                    "text_length": len(text_body),
                    "extractor_status": (ft.get("extraction") or {}).get("extractor_status") or "TEXT_OK",
                    "extraction_source": (ft.get("extraction") or {}).get("extraction_source") or ft.get("extraction_method"),
                },
                "selected_identity": {
                    "title": bm.get("title"),
                    "authors": bm.get("authors") or [],
                    "year": bm.get("year"),
                    "doi": (paper.get("identifiers") or {}).get("doi") or bm.get("doi"),
                    "identity_status": bm.get("identity_status") or "high_confidence",
                    "llm_forced_first_pass": first_pass_finalize,
                },
                "article_type_evidence": article_signals,
                "coverage": coverage,
                "document_type": paper.get("document_type"),
                "file_identity": {"filename": fi.get("renamed_filename")},
                "first_pass_finalize": first_pass_finalize,
            }
        )
    if not eval_health.get("can_evaluate"):
        print(f"    Evaluation input health blocked save: {eval_health.get('reasons')}")
        log_eval(f"EVAL_HEALTH_BLOCKED | {paper_id} | {eval_health.get('reasons')}")
        paper.setdefault("notes_and_flags", {})["reevaluation_required"] = ";".join(eval_health.get("reasons") or [])
        paper["notes_and_flags"]["coverage_status"] = coverage.get("coverage_status")
        paper["classification"] = paper.get("classification") or {}
        paper["classification"]["rating"] = None
        papers_db.save_paper(paper)
        return _eval_result(False, in_tok, out_tok)

    # Skip red team for non-ratable documents (only when guard allows)
    if rating.startswith("not_"):
        if coverage.get("likely_journal_article") and not coverage.get("evidence_can_support_not_applicable"):
            print(f"    Coverage blocked non-ratable save for journal article: {coverage.get('coverage_status')}")
            log_eval(f"COVERAGE_BLOCKED | {paper_id} | not_ratable_rejected | coverage={coverage.get('coverage_status')}")
            paper.setdefault("notes_and_flags", {})["reevaluation_required"] = f"coverage:{coverage.get('coverage_status')}"
            paper["notes_and_flags"]["coverage_status"] = coverage.get("coverage_status")
            paper["classification"] = paper.get("classification") or {}
            paper["classification"]["rating"] = None
            paper["classification"]["guard_blocked_rating"] = rating
            papers_db.save_paper(paper)
            return _eval_result(False, in_tok, out_tok)
        if (article_signals.get("journal_article_signals") or 0) >= 2:
            print(f"    Red team required — journal article signals despite {rating}")
            log_eval(f"REDTEAM_REQUIRED | {paper_id} | blocked_skip | model={rating}")
            return _eval_result(False, in_tok, out_tok)
        print(f"    Skipping red team — non-ratable document ({rating})")
        merge_evaluation(paper, eval_result)
        paper["classification"]["rating"] = rating
        paper["classification"]["rating_date"] = datetime.utcnow().isoformat()
        paper["classification"]["red_team_notes"] = "Skipped — non-ratable document"
        paper["classification"]["red_team_survival"] = None
        papers_db.save_paper(paper)
        log_eval(f"OK | {paper_id} | {rating} | red_team=skipped | {len(text)} chars")
        print(f"  Saved: {paper_id} = {rating.upper()} (no red team)")
        return _eval_result(True, in_tok, out_tok)

    if fast_mode:
        merge_evaluation(paper, eval_result)
        paper["classification"]["rating"] = rating
        paper["classification"]["rating_date"] = datetime.utcnow().isoformat()
        paper["classification"]["evaluator"] = eval_model
        paper["classification"]["red_team_notes"] = _fast_backfill_red_team_notes(cl, model_label=mode_label)
        paper["classification"]["red_team_survival"] = None
        paper.setdefault("notes_and_flags", {})["evaluation_tier"] = f"fast_backfill:{mode_label}"
        papers_db.save_paper(paper)
        log_eval(f"OK_FAST | {paper_id} | {rating} | model={mode_label} | red_team=synthetic | {len(text)} chars")
        print(f"  Saved: {paper_id} = {rating.upper()} ({mode_label} fast, no Pass 2)")
        return _eval_result(True, in_tok, out_tok)

    time.sleep(inter_pass_delay)

    # === PASS 2: RED TEAM ===
    print(f"  Pass 2: Red teaming {paper_id}...")
    rt_in = rt_out = 0
    try:
        from llm_providers import call_llm

        rt_user = (
            f"Paper:\n{header}{text}\n\n"
            f"Initial evaluation:\n"
            f"Rating: {rating}\n"
            f"Design: {cl.get('design_type_detected', '')}\n"
            f"Justification: {justification}\n"
            f"Applicable standards: {json.dumps(cl.get('applicable_standards_applied', []))}\n"
            f"Strengths: {json.dumps(cl.get('key_strengths', []))}\n"
            f"Weaknesses: {json.dumps(cl.get('key_weaknesses', []))}\n\n"
            f"Apply the destructive red-team checklist. Return ONLY the JSON red-team audit."
        )
        rt_text, rt_usage = call_llm(
            model_key,
            build_red_team_system_prompt(),
            rt_user,
            max_tokens=max_tokens,
        )
        if rt_text.startswith("```"):
            rt_text = rt_text.split("\n", 1)[1]
            if rt_text.endswith("```"):
                rt_text = rt_text[:-3]
            rt_text = rt_text.strip()
        rt_result = json.loads(rt_text)

        rt_in = int(rt_usage.get("input_tokens") or 0)
        rt_out = int(rt_usage.get("output_tokens") or 0)
        print(f"    Tokens: {rt_in:,} in / {rt_out:,} out (${rt_usage.get('cost_usd', 0):.4f})")

    except (json.JSONDecodeError, anthropic.APIError) as e:
        print(f"  Pass 2 FAILED for {paper_id}: {e}")
        log_eval(f"RED_TEAM_FAILED | {paper_id} | {e}")
        rt_result = {
            "red_team_notes": f"Red team API call failed: {e}",
            "red_team_survival": None,
            "recommended_rating": rating,
            "downgrade_reason": None
        }

    except (json.JSONDecodeError, anthropic.APIError) as e:
        print(f"  Pass 2 FAILED for {paper_id}: {e}")
        log_eval(f"RED_TEAM_FAILED | {paper_id} | {e}")
        rt_result = {
            "red_team_summary": f"Red team API call failed: {e}",
            "survived_red_team": None,
            "final_rating": rating,
            "recommended_rating": rating,
            "downgrade_reason": None,
        }

    audit, audit_err = validate_red_team_payload(rt_result, original_rating=rating)
    if audit_err:
        audit = normalize_red_team_audit(rt_result, original_rating=rating, auditor=MODEL)
        log_eval(f"RED_TEAM_SCHEMA_WARN | {paper_id} | {audit_err}")

    final_rating = resolve_final_rating_after_red_team(rating, audit)
    survived = audit.get("survived_red_team", True)

    if final_rating != rating:
        print(f"    RED TEAM DOWNGRADE: {rating.upper()} -> {final_rating.upper()}")
        log_eval(
            f"DOWNGRADE | {paper_id} | {rating} -> {final_rating} | "
            f"{(audit.get('change_reason') or '')[:100]}"
        )

    print(f"    Final rating: {final_rating.upper()} (survived={survived})")

    # === MERGE INTO PAPER AND SAVE ===
    merge_evaluation(paper, eval_result)

    paper["classification"]["rating"] = final_rating
    paper["classification"]["rating_date"] = datetime.utcnow().isoformat()
    apply_red_team_audit_to_classification(paper["classification"], audit)

    papers_db.save_paper(paper)
    log_eval(f"OK | {paper_id} | {final_rating} | survived={survived} | {len(text)} chars")
    print(f"  Saved: {paper_id} = {final_rating.upper()}")
    return _eval_result(True, in_tok + rt_in, out_tok + rt_out)


def main():
    parser = argparse.ArgumentParser(description="Evaluate papers via Anthropic API")
    parser.add_argument("--list", action="store_true", help="List unevaluated papers")
    parser.add_argument("--next", nargs="?", const=1, type=int, help="Show next N unevaluated IDs")
    parser.add_argument("--evaluate", action="store_true", help="Run evaluation")
    parser.add_argument("--count", type=int, default=1, help="Number of papers to evaluate")
    parser.add_argument("--paper-id", type=str, help="Evaluate a specific paper")
    parser.add_argument("--force", action="store_true", help="Re-evaluate even if already rated")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without API calls")

    args = parser.parse_args()

    if args.list:
        unevaluated = get_unevaluated_papers(args.force)
        print(f"Unevaluated papers: {len(unevaluated)}\n")
        for i, p in enumerate(unevaluated[:50], 1):
            old = " [has old rating]" if p["has_old_rating"] else ""
            print(f"  {i}. [{p['id']}] {p['title']} ({p['year']}) -- {p['text_chars']}c{old}")
        if len(unevaluated) > 50:
            print(f"\n  ... and {len(unevaluated) - 50} more")

    elif args.next is not None:
        unevaluated = get_unevaluated_papers(args.force)
        for p in unevaluated[:args.next]:
            print(p["id"])

    elif args.evaluate or args.paper_id:
        client = anthropic.Anthropic()

        if args.paper_id:
            evaluate_one(client, args.paper_id, args.force)
        else:
            unevaluated = get_unevaluated_papers(args.force)
            to_eval = unevaluated[:args.count]
            print(f"Evaluating {len(to_eval)} papers...\n")
            for i, p in enumerate(to_eval, 1):
                print(f"[{i}/{len(to_eval)}] {p['id']} ({p['text_chars']} chars)")
                result = evaluate_one(client, p["id"], args.force)
                if isinstance(result, dict) and result.get("ok"):
                    pass
                if i < len(to_eval):
                    time.sleep(DELAY_BETWEEN_PAPERS)

    elif args.dry_run:
        unevaluated = get_unevaluated_papers(args.force)
        count = args.count if args.count else 5
        to_eval = unevaluated[:count]
        print(f"DRY RUN -- would evaluate {len(to_eval)} papers:\n")
        for i, p in enumerate(to_eval, 1):
            old = f" [old: {p['has_old_rating']}]" if p["has_old_rating"] else ""
            print(f"  {i}. [{p['id']}] {p['title']} ({p['year']}) -- {p['text_chars']} chars{old}")
        total_chars = sum(p["text_chars"] for p in to_eval)
        print(f"\n  Total text: {total_chars:,} chars")
        print(f"  Est API calls: {len(to_eval) * 2} (eval + red team each)")

    else:
        unevaluated = get_unevaluated_papers(args.force)
        all_papers = papers_db.get_all_metadata()
        already = sum(1 for p in all_papers if is_already_evaluated(p))
        print(f"{len(unevaluated)} papers need evaluation ({already} already done)")


if __name__ == "__main__":
    main()
