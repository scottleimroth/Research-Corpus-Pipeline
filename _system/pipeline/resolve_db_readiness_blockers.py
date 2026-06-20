#!/usr/bin/env python3
"""Resolve DB readiness blockers before live ingest (targeted, audited)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import config  # noqa: E402
import papers_db  # noqa: E402
from document_corpus_classification import (  # noqa: E402
    apply_non_ratable_reference_classification,
    classify_corpus_document,
)
from document_evidence import (  # noqa: E402
    build_evidence_package,
    check_article_type_guard,
    is_invalid_canonical_title,
    normalize_whitespace,
    recover_title_from_filename,
    score_journal_article_signals,
)
from document_text_extraction import extract_document_text, meaningful_text_length  # noqa: E402
from metadata_extractors import extract_first_page_heuristic  # noqa: E402
from repair_poisoned_article_identity import (  # noqa: E402
    POISONED_ID_RX,
    _first_text_sample,
    _is_genuine_reference_material,
    _rating,
    _source_path,
    _text_length,
    build_candidate,
    detect_poison_signals,
    scan_active_poisoned_rows,
)
from source_pdf_ownership import _create_backup, _missing_db_row_ids, build_audit  # noqa: E402

AUDIT_DIR = config.CORPUS_STORE / "audit" / "db-readiness"
BARRY_KEEP_CANDIDATE = "Barry_1998_PII"
BARRY_DROP_CANDIDATE = "Barry_1998_Psychophysiology"
UNCERTAIN_IDS = ("Bolton_1894_Rhythm", "Bolton_1894_Rhythm_b", "Krumhansl1997b", "22")
BOLTON_KEEP_ID = "Bolton_1894_Rhythm"
BOLTON_DROP_ID = "Bolton_1894_Rhythm_b"

NOT_APPLICABLE_TITLE_RX = re.compile(
    r"(last word from|supplementary material|supplement for|reply to|committee report|"
    r"osf prereg|panel discussion|copyediting specification|abstracts \d{4}|"
    r"^erratum to |^corrigendum to |^reply to )",
    re.I,
)
NON_RATABLE_TITLE_RX = re.compile(
    r"(springerbriefs|media effects:|treating attachment disorders|eye movement research: an introduction|"
    r"designing experiments and analyzing data|for dummies|user manual|guideline thirteen|"
    r"product manual|handbook of |edited volume|second edition|model comparison perspective|"
    r"physiology of the autonomic nervous system|complete reference guide to the elsevier)",
    re.I,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    d = str(doi).strip().lower()
    d = d.replace("https://doi.org/", "").replace("http://doi.org/", "")
    return d


def _doi_match(a: str | None, b: str | None) -> bool:
    da, db = _normalize_doi(a), _normalize_doi(b)
    if not da or not db:
        return False
    return da == db or da.endswith(db) or db.endswith(da)


@dataclass
class ModelUsage:
    sonnet_calls: int = 0
    opus_calls: int = 0
    sonnet_input_tokens: int = 0
    sonnet_output_tokens: int = 0
    opus_input_tokens: int = 0
    opus_output_tokens: int = 0


@dataclass
class Session:
    execute: bool
    allow_paid_api: bool
    allow_opus: bool
    usage: ModelUsage = field(default_factory=ModelUsage)
    records: list[dict[str, Any]] = field(default_factory=list)

    def audit(self, rec: dict[str, Any]) -> None:
        rec.setdefault("timestamp", _now())
        self.records.append(rec)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _export_row_audit(paper_id: str, reason: str) -> dict:
    paper = papers_db.get_paper(paper_id)
    return {"deleted_at": _now(), "reason": reason, "row": paper}


def apply_not_applicable_classification(
    paper_id: str,
    *,
    reasons: list[str],
    old_rating: str | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    paper = papers_db.get_paper(paper_id)
    if not paper:
        return {"ok": False, "error": "paper_not_found"}
    bm = paper.get("basic_metadata") or {}
    title = str(bm.get("title") or paper_id)
    justification = (
        f"Not applicable for research-paper quality rating: '{title[:120]}'. "
        + ("Reasons: " + "; ".join(reasons[:8]) + "." if reasons else "Deterministic corpus classification.")
    )
    cl = dict(paper.get("classification") or {})
    cl.update(
        {
            "rating": "not_applicable",
            "rating_justification": justification,
            "red_team_notes": "Skipped — not applicable document type (deterministic db-readiness classification).",
            "red_team_survival": None,
            "rating_date": _now(),
            "guard_blocked_rating": old_rating or cl.get("guard_blocked_rating"),
            "article_type_consistency_note": "Deterministic not_applicable confirmed during db-readiness pass.",
        }
    )
    paper["classification"] = cl
    notes = dict(paper.get("notes_and_flags") or {})
    notes.pop("reevaluation_required", None)
    notes.pop("reevaluation_reason", None)
    notes["db_readiness_resolved"] = _now()
    paper["notes_and_flags"] = notes
    if execute:
        papers_db.save_paper(paper)
    return {"ok": True, "paper_id": paper_id, "rating": "not_applicable", "justification": justification}


def _article_context(paper: dict) -> dict[str, Any]:
    ft_sample = _first_text_sample(paper, 8000)
    fi = paper.get("file_info") or {}
    ids = paper.get("identifiers") or {}
    bm = paper.get("basic_metadata") or {}
    src = _source_path(paper)
    signals = score_journal_article_signals(
        text=ft_sample,
        filename=str(fi.get("original_filename") or fi.get("renamed_filename") or ""),
        identity={"title": bm.get("title"), "doi": ids.get("doi"), "pii": ids.get("pii")},
        page_count=None,
    )
    return {"signals": signals, "text_length": _text_length(paper), "src": src, "sample": ft_sample}


def classify_row_action(paper: dict) -> tuple[str, list[str]]:
    """Return action bucket and reasons."""
    bm = paper.get("basic_metadata") or {}
    title = str(bm.get("title") or "")
    fn = str((paper.get("file_info") or {}).get("renamed_filename") or "")
    rating = _rating(paper)
    ctx = _article_context(paper)
    signals = ctx["signals"]
    ja = int(signals.get("journal_article_signals") or 0)
    ref = int(signals.get("reference_material_signals") or 0)

    if NOT_APPLICABLE_TITLE_RX.search(title) or NOT_APPLICABLE_TITLE_RX.search(fn):
        return "confirm_not_applicable", ["title_or_filename_editorial_or_supplement"]
    if NON_RATABLE_TITLE_RX.search(title) or NON_RATABLE_TITLE_RX.search(fn):
        return "confirm_non_ratable", ["title_or_filename_reference_material"]
    if _is_genuine_reference_material(paper, signals):
        return "confirm_non_ratable", ["reference_material_signals"]
    corpus = classify_corpus_document(fn, title=title)
    if corpus.get("is_non_ratable"):
        return "confirm_non_ratable", list(corpus.get("classification_reasons") or ["corpus_classifier"])

    if rating in {"not_ratable_reference_material", "not_applicable"} and ja >= 2 and ref <= 1:
        if ctx["text_length"] >= 200:
            return "sonnet_evaluate", ["journal_article_signals_with_suspicious_rating"]
        return "confirm_not_applicable", ["insufficient_text_for_research_eval"]

    if rating in {"not_ratable_reference_material", "not_applicable"}:
        return "confirm_not_applicable", ["retain_non_research_classification"]

    if (paper.get("notes_and_flags") or {}).get("reevaluation_required"):
        if ctx["text_length"] >= 200:
            return "sonnet_evaluate", ["explicit_reevaluation_flag"]
        return "confirm_not_applicable", ["reevaluation_flag_but_insufficient_text"]

    return "already_complete", ["no_action_needed"]


def ensure_full_text(paper_id: str, *, execute: bool) -> dict[str, Any]:
    paper = papers_db.get_paper(paper_id)
    if not paper:
        return {"ok": False, "error": "missing"}
    if _text_length(paper) >= 200:
        return {"ok": True, "status": "has_text", "chars": _text_length(paper)}
    src = _source_path(paper)
    if not src or not src.exists():
        return {"ok": False, "error": "source_missing"}
    result = extract_document_text(src, attempt_ocr=False)
    if meaningful_text_length(result.text) < 200:
        return {"ok": False, "error": f"extract_failed:{result.extractor_status}", "chars": result.text_length}
    ft = dict(paper.get("full_text") or {})
    ft["full"] = result.text
    ft["total_chars"] = result.text_length
    paper["full_text"] = ft
    if execute:
        papers_db.save_paper(paper)
    return {"ok": True, "status": "extracted", "chars": result.text_length}


def extract_leading_article_title(text_sample: str) -> str | None:
    """Recover title from JSTOR-style first lines (e.g. Bolton 1894 'Rhythm')."""
    lines = [ln.strip() for ln in (text_sample or "").splitlines()[:24] if ln.strip()]
    if not lines:
        return None
    first = normalize_whitespace(lines[0])
    if not first or first.lower().startswith("author"):
        return None
    if len(lines) >= 2 and re.match(r"^author\s*\(", lines[1], re.I):
        invalid, _ = is_invalid_canonical_title(first)
        if not invalid:
            return first
    return None


def recover_title_deterministic(paper: dict) -> tuple[str | None, str | None]:
    bm = paper.get("basic_metadata") or {}
    current = str(bm.get("title") or "")
    if current and not is_invalid_canonical_title(current)[0]:
        return current, "existing_valid_title"

    sample = _first_text_sample(paper, 12000)
    lead = extract_leading_article_title(sample)
    if lead:
        return lead, "jstor_leading_line"

    if sample:
        fp = extract_first_page_heuristic(Path("x.pdf"), sample) or {}
        t = fp.get("title")
        if t and not is_invalid_canonical_title(t)[0]:
            return str(t), "first_page_heuristic"

    fi = paper.get("file_info") or {}
    for hint in (fi.get("original_filename"), fi.get("renamed_filename")):
        if hint:
            rec = recover_title_from_filename(Path(str(hint)))
            title = rec.get("title")
            if title and not is_invalid_canonical_title(title)[0]:
                return title, "filename"
    return None, None


def get_mark_for_reevaluation_rows() -> list[str]:
    audit = build_audit()
    ids: list[str] = []
    for meta in papers_db.get_all_metadata():
        cand = build_candidate(meta, audit=audit)
        if cand.get("repair_bucket") == "mark_for_reevaluation":
            ids.append(meta["id"])
    return sorted(set(ids))


def get_incomplete_guard_blocked_rows() -> list[str]:
    ids: list[str] = []
    for meta in papers_db.get_all_metadata():
        paper = papers_db.get_paper(meta["id"]) or meta
        cl = paper.get("classification") or {}
        rating = str(cl.get("rating") or "").strip()
        if rating:
            continue
        if cl.get("guard_blocked_rating") or (paper.get("notes_and_flags") or {}).get("reevaluation_required"):
            ids.append(meta["id"])
    return sorted(set(ids))


def cleanup_audited_unowned_source_pdfs(session: Session) -> list[dict[str, Any]]:
    """Remove unowned duplicate scans with prior db-readiness audit records."""
    audit = build_audit()
    cleaned: list[dict[str, Any]] = []
    for rel in audit.get("unowned_source_pdfs") or []:
        name = Path(str(rel)).name
        dup_audit_path = AUDIT_DIR / f"duplicate_source_{name}.json"
        bolton_audit_path = AUDIT_DIR / f"bolton_duplicate_source_{name}.json"
        audit_path = dup_audit_path if dup_audit_path.exists() else bolton_audit_path
        if not audit_path.exists():
            continue
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
        path = config.MASTER_PROJECT / str(rel).replace("/", "\\")
        rec = {
            "task": "cleanup_unowned_source_pdf",
            "relative_path": rel,
            "audit_path": str(audit_path),
            "reason": payload.get("reason"),
            "final_status": "planned",
        }
        if path.exists() and session.execute:
            payload["deleted_at"] = _now()
            payload["action"] = "deleted_unowned_duplicate_scan"
            audit_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            path.unlink(missing_ok=True)
            rec["final_status"] = "deleted"
        elif not path.exists():
            rec["final_status"] = "already_absent"
        session.audit(rec)
        cleaned.append(rec)
    return cleaned


def _text_fingerprint(paper: dict, *, limit: int = 8000) -> str:
    sample = _first_text_sample(paper, limit).lower()
    sample = re.sub(r"\s+", " ", sample)
    return hashlib.sha256(sample.encode("utf-8")).hexdigest()[:16]


def resolve_bolton_duplicate_pair(session: Session) -> dict[str, Any]:
    keep_id, drop_id = BOLTON_KEEP_ID, BOLTON_DROP_ID
    keep = papers_db.get_paper(keep_id)
    drop = papers_db.get_paper(drop_id)
    rec: dict[str, Any] = {
        "task": "bolton_1894_duplicate",
        "keep_id": keep_id,
        "drop_id": drop_id,
        "final_status": "unresolved",
    }
    if not keep:
        rec["final_status"] = "keep_missing"
        session.audit(rec)
        return rec

    new_title, source = recover_title_deterministic(keep)
    if not new_title:
        new_title = "Rhythm"
        source = "historical_default"
    old_title = (keep.get("basic_metadata") or {}).get("title")
    rec.update({"old_title": old_title, "new_title": new_title, "title_source": source})

    if drop:
        rec["text_fp_keep"] = _text_fingerprint(keep)
        rec["text_fp_drop"] = _text_fingerprint(drop)
        rec["same_article"] = rec["text_fp_keep"] == rec["text_fp_drop"]
        if not rec["same_article"]:
            kb = keep.get("basic_metadata") or {}
            db = drop.get("basic_metadata") or {}
            rec["same_article"] = (
                str(kb.get("year")) == str(db.get("year"))
                and str(kb.get("journal") or "").lower() == str(db.get("journal") or "").lower()
                and _text_length(keep) > 100000
                and _text_length(drop) > 100000
            )
    else:
        rec["same_article"] = True

    if session.execute:
        bm = dict(keep.get("basic_metadata") or {})
        bm["title"] = new_title
        bm["title_source"] = source
        keep["basic_metadata"] = bm
        notes = dict(keep.get("notes_and_flags") or {})
        notes.pop("reevaluation_required", None)
        notes["db_readiness_resolved"] = _now()
        notes["bolton_title_note"] = "Historical single-word article title confirmed from JSTOR full text."
        keep["notes_and_flags"] = notes
        papers_db.save_paper(keep)

        if drop and rec.get("same_article"):
            export = _export_row_audit(drop_id, "duplicate_bolton_1894_rhythm_scan")
            _write_jsonl(AUDIT_DIR / f"deleted_row_{drop_id}.jsonl", [export])
            conn = papers_db.get_connection()
            try:
                conn.execute("DELETE FROM papers WHERE id = ?", (drop_id,))
                conn.commit()
            finally:
                conn.close()
            drop_src = _source_path({"file_info": drop.get("file_info") or {}})
            if drop_src and drop_src.exists():
                dup_audit = {
                    "timestamp": _now(),
                    "reason": "bolton_1894_duplicate_scan_deleted",
                    "path": str(drop_src),
                    "sha256": _sha256(drop_src),
                    "canonical_id": keep_id,
                }
                keep_src = _source_path(keep)
                if keep_src and keep_src.exists():
                    dup_audit["keep_sha256"] = _sha256(keep_src)
                audit_name = AUDIT_DIR / f"bolton_duplicate_source_{drop_src.name}.json"
                audit_name.write_text(json.dumps(dup_audit, indent=2), encoding="utf-8")
                drop_src.unlink(missing_ok=True)

    rec["final_status"] = "merged_duplicate_same_article" if drop and rec.get("same_article") else "title_repaired"
    session.audit(rec)
    return rec


def resolve_incomplete_evaluations(session: Session, paper_ids: list[str] | None = None) -> list[dict[str, Any]]:
    import anthropic
    from evaluate_paper import evaluate_one

    targets = paper_ids or get_incomplete_guard_blocked_rows()
    results: list[dict[str, Any]] = []
    if not targets:
        return results
    client = anthropic.Anthropic() if session.allow_paid_api else None
    for paper_id in targets:
        paper = papers_db.get_paper(paper_id)
        rec = {
            "paper_id": paper_id,
            "task": "incomplete_guard_blocked_eval",
            "old_rating": _rating(paper),
            "guard_blocked_rating": (paper.get("classification") or {}).get("guard_blocked_rating"),
            "sonnet_called": False,
            "final_status": "skipped",
        }
        if not paper or not session.allow_paid_api or not client:
            rec["final_status"] = "skipped_no_api"
            session.audit(rec)
            results.append(rec)
            continue
        ensure_full_text(paper_id, execute=session.execute)
        session.usage.sonnet_calls += 1
        rec["sonnet_called"] = True
        result = evaluate_one(client, paper_id, force=True, require_research_rating=True)
        session.usage.sonnet_input_tokens += int(result.get("input_tokens") or 0)
        session.usage.sonnet_output_tokens += int(result.get("output_tokens") or 0)
        paper = papers_db.get_paper(paper_id) or {}
        new_rating = _rating(paper)
        rec["new_rating"] = new_rating
        if result.get("ok") and new_rating:
            notes = dict(paper.get("notes_and_flags") or {})
            notes.pop("reevaluation_required", None)
            notes["db_readiness_resolved"] = _now()
            if session.execute:
                paper["notes_and_flags"] = notes
                papers_db.save_paper(paper)
            rec["final_status"] = "evaluated"
        elif session.allow_opus and session.execute:
            rec.update(_opus_article_adjudication(session, paper_id, rec.get("guard_blocked_rating") or ""))
            if rec.get("final_status") == "opus_requires_retry_eval":
                session.usage.sonnet_calls += 1
                retry = evaluate_one(client, paper_id, force=True, require_research_rating=True)
                session.usage.sonnet_input_tokens += int(retry.get("input_tokens") or 0)
                session.usage.sonnet_output_tokens += int(retry.get("output_tokens") or 0)
                paper = papers_db.get_paper(paper_id) or {}
                rec["new_rating"] = _rating(paper)
                rec["final_status"] = "evaluated" if retry.get("ok") and _rating(paper) else "eval_failed"
        else:
            rec["final_status"] = "eval_failed"
        session.audit(rec)
        results.append(rec)
    return results


def resolve_barry_collision(session: Session) -> dict[str, Any]:
    keep_id, drop_id = BARRY_KEEP_CANDIDATE, BARRY_DROP_CANDIDATE
    keep = papers_db.get_paper(keep_id)
    drop = papers_db.get_paper(drop_id)
    rec = {
        "task": "barry_1998_collision",
        "keep_id": keep_id,
        "drop_id": drop_id,
        "final_status": "unresolved",
    }
    if not keep or not drop:
        rec["final_status"] = "missing_row"
        session.audit(rec)
        return rec

    kb = keep.get("basic_metadata") or {}
    db = drop.get("basic_metadata") or {}
    kdoi = (keep.get("identifiers") or {}).get("doi") or kb.get("doi")
    ddoi = (drop.get("identifiers") or {}).get("doi") or db.get("doi")
    rec["doi_match"] = _doi_match(kdoi, ddoi)
    rec["keep_title"] = kb.get("title")
    rec["drop_title"] = db.get("title")

    if not rec["doi_match"]:
        rec["final_status"] = "distinct_documents_requires_adjudication"
        session.audit(rec)
        return rec

    # Same DOI — merge drop full_text into keep, delete duplicate row.
    if _text_length(keep) < 200 and _text_length(drop) >= 200:
        keep["full_text"] = drop.get("full_text") or keep.get("full_text")
    elif _text_length(keep) < 200:
        ensure_full_text(keep_id, execute=False)

    bm = dict(kb)
    if is_invalid_canonical_title(str(bm.get("title") or ""))[0] and not is_invalid_canonical_title(str(db.get("title") or ""))[0]:
        bm["title"] = db.get("title")
    bm["doi"] = kdoi or ddoi
    keep["basic_metadata"] = bm
    ids = dict(keep.get("identifiers") or {})
    if kdoi or ddoi:
        ids["doi"] = _normalize_doi(kdoi or ddoi)
    keep["identifiers"] = ids

    notes = dict(keep.get("notes_and_flags") or {})
    notes.pop("reevaluation_required", None)
    notes["db_readiness_resolved"] = _now()
    notes["merged_from"] = drop_id
    keep["notes_and_flags"] = notes
    keep["classification"] = keep.get("classification") or {}
    if not keep["classification"].get("rating"):
        keep["classification"]["rating"] = None

    new_id = "Barry_1998_Eysenck"
    if session.execute:
        export = _export_row_audit(drop_id, "duplicate_same_doi_barry_1998")
        _write_jsonl(AUDIT_DIR / f"deleted_row_{drop_id}.jsonl", [export])

        conn = papers_db.get_connection()
        try:
            conn.execute("DELETE FROM papers WHERE id = ?", (drop_id,))
            conn.commit()
        finally:
            conn.close()

        final_id = new_id if not papers_db.paper_exists(new_id) else keep_id
        keep["id"] = final_id
        papers_db.save_paper(keep)
        if final_id != keep_id:
            conn = papers_db.get_connection()
            try:
                conn.execute("DELETE FROM papers WHERE id = ?", (keep_id,))
                conn.commit()
            finally:
                conn.close()

        drop_src = _source_path({"file_info": drop.get("file_info") or {}})
        keep_src = _source_path(keep)
        if drop_src and drop_src.exists():
            dup_audit = {
                "timestamp": _now(),
                "reason": "duplicate_doi_row_deleted",
                "path": str(drop_src),
                "sha256": _sha256(drop_src),
                "canonical_id": final_id,
            }
            if keep_src and keep_src.exists():
                dup_audit["keep_sha256"] = _sha256(keep_src)
                dup_audit["identical_to_keep"] = dup_audit["sha256"] == dup_audit["keep_sha256"]
            (AUDIT_DIR / f"duplicate_source_{drop_src.name}.json").write_text(
                json.dumps(dup_audit, indent=2),
                encoding="utf-8",
            )
            if dup_audit.get("identical_to_keep"):
                drop_src.unlink(missing_ok=True)
            elif drop_src.exists():
                # Same DOI duplicate scan retained on disk; canonical row uses keep_src.
                pass

    rec.update(
        {
            "final_status": "merged_duplicate_same_doi",
            "canonical_id": new_id,
            "action": "deleted_duplicate_row",
            "sonnet_called": False,
            "opus_called": False,
        }
    )
    session.audit(rec)
    return rec


def resolve_uncertain_identity(session: Session, paper_id: str) -> dict[str, Any]:
    paper = papers_db.get_paper(paper_id)
    rec = {"paper_id": paper_id, "task": "uncertain_identity", "final_status": "unresolved"}
    if not paper:
        rec["final_status"] = "missing"
        session.audit(rec)
        return rec

    new_title, source = recover_title_deterministic(paper)
    old_title = (paper.get("basic_metadata") or {}).get("title")
    rec.update({"old_title": old_title, "new_title": new_title, "title_source": source})

    if not new_title:
        rec["final_status"] = "no_deterministic_title"
        session.audit(rec)
        return rec

    if session.execute and new_title != old_title:
        bm = dict(paper.get("basic_metadata") or {})
        bm["title"] = new_title
        bm["title_source"] = source
        paper["basic_metadata"] = bm
        notes = dict(paper.get("notes_and_flags") or {})
        notes.pop("reevaluation_required", None)
        notes["db_readiness_resolved"] = _now()
        paper["notes_and_flags"] = notes
        papers_db.save_paper(paper)

    rating = _rating(paper)
    ctx = _article_context(paper)
    if rating in {"not_applicable", "not_ratable_reference_material"} and int(ctx["signals"].get("journal_article_signals") or 0) >= 2:
        action = "sonnet_evaluate"
    elif rating in {"weak", "adequate", "strong", "landmark", "flawed"}:
        action = "already_evaluated"
    elif ctx["text_length"] >= 200:
        action = "sonnet_evaluate"
    else:
        action = "title_repaired_only"

    rec["recommended_action"] = action
    if action == "sonnet_evaluate" and session.allow_paid_api and session.execute:
        eval_rec = run_targeted_evaluation(session, paper_id, force=True)
        rec.update(eval_rec)
    else:
        rec["final_status"] = "title_repaired" if new_title != old_title else "already_ok"

    if session.execute and rec.get("final_status") in {"title_repaired", "already_ok", "evaluated"}:
        p = papers_db.get_paper(paper_id) or {}
        notes = dict(p.get("notes_and_flags") or {})
        notes.pop("reevaluation_required", None)
        notes["db_readiness_resolved"] = _now()
        p["notes_and_flags"] = notes
        papers_db.save_paper(p)

    session.audit(rec)
    return rec


def run_targeted_evaluation(session: Session, paper_id: str, *, force: bool = True) -> dict[str, Any]:
    import anthropic
    from evaluate_paper import evaluate_one

    rec = {
        "paper_id": paper_id,
        "sonnet_called": False,
        "opus_called": False,
        "red_team_run": False,
        "red_team_audit_present": False,
        "final_status": "eval_failed",
    }
    if not session.allow_paid_api:
        rec["final_status"] = "skipped_no_paid_api"
        return rec

    ensure_full_text(paper_id, execute=session.execute)
    paper = papers_db.get_paper(paper_id) or {}
    old_rating = _rating(paper)
    rec["old_rating"] = old_rating

    client = anthropic.Anthropic()
    session.usage.sonnet_calls += 1
    rec["sonnet_called"] = True
    result = evaluate_one(client, paper_id, force=force)
    session.usage.sonnet_input_tokens += int(result.get("input_tokens") or 0)
    session.usage.sonnet_output_tokens += int(result.get("output_tokens") or 0)

    paper = papers_db.get_paper(paper_id) or {}
    new_rating = _rating(paper)
    cl = paper.get("classification") or {}
    rec.update(
        {
            "new_rating": new_rating,
            "red_team_run": bool(cl.get("red_team_notes") and "Skipped" not in str(cl.get("red_team_notes"))),
            "red_team_audit_present": bool(cl.get("red_team_survival") is not None or cl.get("red_team_notes")),
            "final_status": "evaluated" if result.get("ok") else "eval_failed",
        }
    )

    if not result.get("ok") and session.allow_opus:
        rec.update(_opus_article_adjudication(session, paper_id, old_rating))
        if rec.get("final_status") == "opus_requires_retry_eval" and session.allow_paid_api and session.execute:
            session.usage.sonnet_calls += 1
            retry = evaluate_one(client, paper_id, force=True, require_research_rating=True)
            session.usage.sonnet_input_tokens += int(retry.get("input_tokens") or 0)
            session.usage.sonnet_output_tokens += int(retry.get("output_tokens") or 0)
            paper = papers_db.get_paper(paper_id) or {}
            new_rating = _rating(paper)
            rec["new_rating"] = new_rating
            if retry.get("ok") and new_rating:
                rec["final_status"] = "evaluated"
                rec["opus_resolved"] = True

    if result.get("ok") or rec.get("opus_resolved") or (rec.get("final_status") == "evaluated" and _rating(papers_db.get_paper(paper_id))):
        notes = dict(paper.get("notes_and_flags") or {})
        notes.pop("reevaluation_required", None)
        notes["db_readiness_resolved"] = _now()
        if session.execute:
            p = papers_db.get_paper(paper_id) or {}
            p["notes_and_flags"] = notes
            papers_db.save_paper(p)

    return rec


def _opus_article_adjudication(session: Session, paper_id: str, old_rating: str) -> dict[str, Any]:
    """Lightweight Opus adjudication when Sonnet eval fails or guard blocks."""
    import anthropic

    paper = papers_db.get_paper(paper_id) or {}
    bm = paper.get("basic_metadata") or {}
    sample = _first_text_sample(paper, 6000)
    ctx = _article_context(paper)
    prompt = (
        "Adjudicate this corpus row. Return JSON only with keys: "
        "final_rating (research rating or not_applicable or not_ratable_reference_material), "
        "article_type, justification, requires_full_eval (bool).\n\n"
        f"Title: {bm.get('title')}\nOld rating: {old_rating}\n"
        f"Signals: {json.dumps(ctx['signals'])}\nText sample:\n{sample[:4000]}"
    )
    client = anthropic.Anthropic()
    try:
        session.usage.opus_calls += 1
        resp = client.messages.create(
            model=config.OPUS_RESOLUTION_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        session.usage.opus_input_tokens += int(resp.usage.input_tokens)
        session.usage.opus_output_tokens += int(resp.usage.output_tokens)
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        rating = str(data.get("final_rating") or "").lower().replace("-", "_")
        if rating in {"not_applicable", "not_ratable_reference_material"}:
            if rating == "not_applicable":
                apply_not_applicable_classification(
                    paper_id,
                    reasons=[str(data.get("justification") or "opus_adjudication")],
                    old_rating=old_rating,
                    execute=session.execute,
                )
            else:
                apply_non_ratable_reference_classification(
                    paper_id,
                    reasons=[str(data.get("justification") or "opus_adjudication")],
                    title=str(bm.get("title") or ""),
                )
            return {"opus_called": True, "opus_resolved": True, "new_rating": rating, "final_status": "opus_adjudicated"}
        if data.get("requires_full_eval"):
            if session.execute and session.allow_paid_api:
                from evaluate_paper import evaluate_one

                client = anthropic.Anthropic()
                session.usage.sonnet_calls += 1
                ev = evaluate_one(client, paper_id, force=True, require_research_rating=True)
                session.usage.sonnet_input_tokens += int(ev.get("input_tokens") or 0)
                session.usage.sonnet_output_tokens += int(ev.get("output_tokens") or 0)
                new_rating = _rating(papers_db.get_paper(paper_id))
                ok = bool(ev.get("ok") and new_rating)
                return {
                    "opus_called": True,
                    "opus_resolved": ok,
                    "new_rating": new_rating,
                    "final_status": "research_eval_completed" if ok else "opus_requires_retry_eval",
                }
            return {"opus_called": True, "opus_resolved": False, "final_status": "opus_requires_retry_eval"}
    except Exception as exc:
        return {"opus_called": True, "opus_resolved": False, "final_status": f"opus_failed:{exc}"}
    return {"opus_called": True, "opus_resolved": False}


def resolve_mark_row(session: Session, paper_id: str) -> dict[str, Any]:
    paper = papers_db.get_paper(paper_id)
    if not paper:
        return {"paper_id": paper_id, "final_status": "missing"}

    old_rating = _rating(paper)
    action, reasons = classify_row_action(paper)
    bm = paper.get("basic_metadata") or {}
    fi = paper.get("file_info") or {}
    rec = {
        "paper_id": paper_id,
        "title": bm.get("title"),
        "source_pdf": fi.get("filepath") or fi.get("renamed_filename"),
        "old_rating": old_rating,
        "action": action,
        "reasons": reasons,
        "sonnet_called": False,
        "opus_called": False,
        "red_team_run": False,
        "red_team_audit_present": False,
        "final_status": "pending",
    }

    if action == "confirm_not_applicable":
        out = apply_not_applicable_classification(
            paper_id, reasons=reasons, old_rating=old_rating, execute=session.execute
        )
        rec.update(new_rating="not_applicable", final_status="confirmed_not_applicable", new_reason=out.get("justification"))
    elif action == "confirm_non_ratable":
        if session.execute:
            apply_non_ratable_reference_classification(paper_id, reasons=reasons, title=str(bm.get("title") or ""))
            p = papers_db.get_paper(paper_id) or {}
            notes = dict(p.get("notes_and_flags") or {})
            notes.pop("reevaluation_required", None)
            notes["db_readiness_resolved"] = _now()
            p["notes_and_flags"] = notes
            papers_db.save_paper(p)
        rec.update(new_rating="not_ratable_reference_material", final_status="confirmed_non_ratable")
    elif action == "sonnet_evaluate":
        eval_rec = run_targeted_evaluation(session, paper_id, force=True)
        rec.update(eval_rec)
    else:
        notes = dict(paper.get("notes_and_flags") or {})
        notes["db_readiness_resolved"] = _now()
        if session.execute:
            paper["notes_and_flags"] = notes
            papers_db.save_paper(paper)
        rec.update(final_status="already_complete", new_rating=old_rating)

    paper = papers_db.get_paper(paper_id) or {}
    rec["new_rating"] = _rating(paper)
    cl = paper.get("classification") or {}
    rec["red_team_audit_present"] = bool(cl.get("red_team_notes"))
    session.audit(rec)
    return rec


def whole_db_readiness_audit() -> dict[str, Any]:
    from file_disposition_engine import count_review_dust_pdfs
    from final_validation_suite import check_pending_child_documents

    ownership = build_audit()
    poison = scan_active_poisoned_rows()
    audit = build_audit()
    suspicious: list[dict] = []
    incomplete_eval: list[dict] = []
    for meta in papers_db.get_all_metadata():
        paper = papers_db.get_paper(meta["id"]) or meta
        cand = build_candidate(meta, audit=audit)
        if cand.get("repair_bucket") == "mark_for_reevaluation":
            suspicious.append({"paper_id": meta["id"], "title": cand.get("title"), "rating": cand.get("classification")})
        elif cand.get("repair_bucket") == "deterministic_repair_safe":
            suspicious.append({"paper_id": meta["id"], "reason": "unrepaired_deterministic", "title": cand.get("title")})
        elif cand.get("repair_bucket") == "uncertain_manual_review":
            suspicious.append({"paper_id": meta["id"], "reason": "uncertain_manual_review", "title": cand.get("title")})

        cl = paper.get("classification") or {}
        rating = str(cl.get("rating") or "").strip()
        if not rating and (cl.get("guard_blocked_rating") or (paper.get("notes_and_flags") or {}).get("reevaluation_required")):
            from evidence_coverage import assess_evidence_coverage_from_paper

            cov = assess_evidence_coverage_from_paper(paper)
            notes = paper.get("notes_and_flags") or {}
            if notes.get("coverage_terminal") or (
                not cov.get("evidence_can_support_rating")
                and cov.get("coverage_status") in {
                    "INSUFFICIENT_FOR_EVALUATION",
                    "PARTIAL_NEEDS_RECOVERY",
                    "OCR_REQUIRED",
                    "OCR_FAILED",
                    "EXTRACTION_FAILED",
                }
            ):
                continue
            incomplete_eval.append(
                {
                    "paper_id": meta["id"],
                    "guard_blocked_rating": cl.get("guard_blocked_rating"),
                    "title": (paper.get("basic_metadata") or {}).get("title"),
                }
            )

    pending = check_pending_child_documents()
    blockers: list[str] = []
    if ownership.get("unowned_total", 0) > 0:
        blockers.append(f"unowned_source_pdfs:{ownership.get('unowned_total')}")
    if ownership.get("ambiguous_total", 0) > 0:
        blockers.append(f"ambiguous_source_pdfs:{ownership.get('ambiguous_total')}")
    if _missing_db_row_ids(ownership):
        blockers.append(f"missing_db_source_paths:{len(_missing_db_row_ids(ownership))}")
    if poison.get("blocking_count", 0) > 0:
        blockers.append(f"poisoned_identity:{poison.get('blocking_count')}")
    if suspicious:
        blockers.append(f"suspicious_rows:{len(suspicious)}")
    if incomplete_eval:
        blockers.append(f"incomplete_evaluations:{len(incomplete_eval)}")
    if count_review_dust_pdfs() > 0:
        blockers.append(f"review_dust:{count_review_dust_pdfs()}")
    if not pending.get("ok"):
        blockers.append("pending_child_invalid")

    return {
        "timestamp": _now(),
        "active_rows": papers_db.get_paper_count(),
        "ownership": ownership,
        "poison_scan": poison,
        "suspicious_rows": suspicious,
        "incomplete_evaluations": incomplete_eval,
        "review_dust": count_review_dust_pdfs(),
        "pending_child": pending,
        "blockers": blockers,
        "ready": not blockers,
    }


def run_readiness(
    *,
    execute: bool = False,
    write_report: bool = True,
    limit: int | None = None,
    paper_id: str | None = None,
    allow_paid_api: bool = False,
    allow_opus: bool = False,
) -> dict[str, Any]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    ownership_before = build_audit()

    backup = None
    session = Session(
        execute=execute,
        allow_paid_api=allow_paid_api,
        allow_opus=allow_opus and allow_paid_api,
    )

    if execute:
        backup = _create_backup("PRE-DB-READINESS")
        cleanup_audited_unowned_source_pdfs(session)
        ownership_before = build_audit()

    if ownership_before.get("ambiguous_total", 0) and execute:
        return {"ok": False, "aborted": True, "reason": "ambiguous_source_ownership", "ownership_before": ownership_before}
    if ownership_before.get("unowned_total", 0) and execute:
        return {
            "ok": False,
            "aborted": True,
            "reason": "unowned_source_pdfs_remain_after_cleanup",
            "ownership_before": ownership_before,
        }

    before_audit = whole_db_readiness_audit()
    mark_before = get_mark_for_reevaluation_rows()

    results: dict[str, Any] = {
        "bolton": resolve_bolton_duplicate_pair(session) if not paper_id or paper_id in {BOLTON_KEEP_ID, BOLTON_DROP_ID} else None,
    }

    if papers_db.get_paper(BARRY_DROP_CANDIDATE) and (not paper_id or paper_id in {BARRY_KEEP_CANDIDATE, BARRY_DROP_CANDIDATE}):
        results["barry"] = resolve_barry_collision(session)
    elif not paper_id:
        results["barry"] = {"final_status": "already_resolved", "canonical_id": "Barry_1998_Eysenck"}

    results["uncertain"] = [
        resolve_uncertain_identity(session, pid)
        for pid in UNCERTAIN_IDS
        if (not paper_id or paper_id == pid) and pid not in {BOLTON_DROP_ID}
    ]
    results["reeval_rows"] = []

    targets = [paper_id] if paper_id else mark_before
    if limit is not None:
        targets = targets[:limit]
    for pid in targets:
        if pid in {BARRY_KEEP_CANDIDATE, BARRY_DROP_CANDIDATE}:
            continue
        results["reeval_rows"].append(resolve_mark_row(session, pid))

    if not paper_id and (not mark_before or get_incomplete_guard_blocked_rows()):
        results["incomplete_eval"] = resolve_incomplete_evaluations(session)

    if execute:
        results["cleanup_unowned"] = cleanup_audited_unowned_source_pdfs(session)

    after_audit = whole_db_readiness_audit()
    ownership_after = after_audit["ownership"]

    summary = {
        "ok": after_audit["ready"],
        "mode": "execute" if execute else "plan_only",
        "generated_at": _now(),
        "backup": backup,
        "ownership_before": {
            "pass": ownership_before.get("unowned_total", 0) == 0 and ownership_before.get("ambiguous_total", 0) == 0,
            "unowned_total": ownership_before.get("unowned_total"),
            "ambiguous_total": ownership_before.get("ambiguous_total"),
        },
        "ownership_after": {
            "pass": ownership_after.get("unowned_total", 0) == 0 and ownership_after.get("ambiguous_total", 0) == 0,
            "unowned_total": ownership_after.get("unowned_total"),
            "ambiguous_total": ownership_after.get("ambiguous_total"),
        },
        "mark_for_reevaluation_before": len(mark_before),
        "mark_for_reevaluation_after": len(get_mark_for_reevaluation_rows()),
        "suspicious_rows_remaining": after_audit.get("suspicious_rows"),
        "blockers": after_audit.get("blockers"),
        "model_usage": {
            "sonnet_calls": session.usage.sonnet_calls,
            "opus_calls": session.usage.opus_calls,
            "sonnet_input_tokens": session.usage.sonnet_input_tokens,
            "sonnet_output_tokens": session.usage.sonnet_output_tokens,
            "opus_input_tokens": session.usage.opus_input_tokens,
            "opus_output_tokens": session.usage.opus_output_tokens,
        },
        "results": results,
    }

    if write_report:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        jsonl = AUDIT_DIR / f"db_readiness_{ts}.jsonl"
        summary_path = AUDIT_DIR / f"final_readiness_report_{ts}.json"
        _write_jsonl(jsonl, session.records)
        summary["audit_jsonl"] = str(jsonl)
        summary["final_readiness_report"] = str(summary_path)
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve DB readiness blockers")
    parser.add_argument("--plan-only", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--paper-id", type=str, default=None)
    parser.add_argument("--allow-paid-api", action="store_true")
    parser.add_argument("--allow-opus-resolution", action="store_true")
    args = parser.parse_args()

    report = run_readiness(
        execute=bool(args.execute),
        write_report=bool(args.write_report) or bool(args.execute),
        limit=args.limit,
        paper_id=args.paper_id,
        allow_paid_api=bool(args.allow_paid_api),
        allow_opus=bool(args.allow_opus_resolution),
    )
    print(json.dumps({k: v for k, v in report.items() if k not in ("results", "suspicious_rows_remaining")}, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
