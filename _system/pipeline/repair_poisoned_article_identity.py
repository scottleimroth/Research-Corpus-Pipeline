#!/usr/bin/env python3
"""Deterministic audit and repair for legacy poisoned article identity rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
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
from add_paper import generate_filename, generate_id, is_unsafe_generated_id  # noqa: E402
from document_evidence import (  # noqa: E402
    check_article_type_guard,
    extract_pii_from_text,
    is_invalid_canonical_title,
    recover_title_from_filename,
    score_journal_article_signals,
    select_identity,
)
from path_governance import reserve_unique_path  # noqa: E402
from metadata_extractors import extract_first_page_heuristic  # noqa: E402
from source_pdf_ownership import _create_backup, _missing_db_row_ids, build_audit  # noqa: E402

AUDIT_DIR = config.CORPUS_STORE / "audit" / "identity-repairs"
POISONED_ID_RX = re.compile(r"(^|_)pii($|_)|unknown_pii|_pii_", re.I)
REFERENCE_DOC_TYPES = frozenset(
    {
        "handbook",
        "manual",
        "standard",
        "protocol",
        "guideline",
        "tutorial",
        "dictionary",
        "book",
        "book_chapter",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _title_from_paper_id(paper_id: str) -> str | None:
    pid = re.sub(r"_b$", "", str(paper_id or ""))
    if "_" not in pid:
        return None
    _author, rest = pid.split("_", 1)
    if rest.upper() == "PII" or rest.lower().endswith("_pii"):
        return None
    title = rest.replace("_", " ").strip()
    if len(title) >= 12 and not is_invalid_canonical_title(title)[0]:
        return title
    return None


def _text_length(paper: dict) -> int:
    ft = paper.get("full_text") or {}
    if isinstance(ft, dict):
        body = ft.get("full") or ft.get("text") or ""
        return len(str(body))
    return 0


def _first_text_sample(paper: dict, limit: int = 3000) -> str:
    ft = paper.get("full_text") or {}
    body = ""
    if isinstance(ft, dict):
        body = str(ft.get("full") or ft.get("text") or "")
    return body[:limit]


def _rating(paper: dict) -> str:
    cl = paper.get("classification") or {}
    return str(cl.get("rating") or paper.get("rating") or "").lower().replace("-", "_")


def _is_genuine_reference_material(paper: dict, article_signals: dict[str, Any]) -> bool:
    doc_type = str(paper.get("document_type") or "").lower()
    if doc_type in REFERENCE_DOC_TYPES:
        return True
    fn = str((paper.get("file_info") or {}).get("renamed_filename") or "").lower()
    if re.search(r"\b(handbook|manual|standard|as[_-]?nzs|checklist|guideline|protocol)\b", fn):
        return int(article_signals.get("reference_material_signals") or 0) >= int(
            article_signals.get("journal_article_signals") or 0
        )
    return int(article_signals.get("reference_material_signals") or 0) >= 2 and int(
        article_signals.get("journal_article_signals") or 0
    ) <= 1


def detect_poison_signals(paper: dict) -> list[str]:
    if (paper.get("notes_and_flags") or {}).get("db_readiness_resolved"):
        return []
    bm = paper.get("basic_metadata") or {}
    fi = paper.get("file_info") or {}
    ids = paper.get("identifiers") or {}
    title = str(bm.get("title") or "")
    pid = str(paper.get("id") or "")
    fn = str(fi.get("renamed_filename") or "")
    rating = _rating(paper)
    signals: list[str] = []

    if not title.strip():
        signals.append("empty_title")
    elif title.upper().startswith("PII:"):
        signals.append("pii_prefix_title")
    elif is_invalid_canonical_title(title)[0]:
        signals.append(f"invalid_title:{is_invalid_canonical_title(title)[1]}")

    if POISONED_ID_RX.search(pid):
        signals.append("poisoned_paper_id")
    if fn and ("pii" in fn.lower() or "unknown_pii" in fn.lower()):
        signals.append("poisoned_source_filename")

    stored_pii = str(ids.get("pii") or bm.get("pii") or "")
    if stored_pii and title.strip() and stored_pii.strip().lower() in title.strip().lower():
        signals.append("title_contains_stored_pii")

    orig = str(fi.get("original_filename") or "")
    if orig and fn:
        fn_rec = recover_title_from_filename(Path(orig))
        fn_title = fn_rec.get("title")
        if fn_title and not is_invalid_canonical_title(fn_title)[0]:
            if is_invalid_canonical_title(title)[0] or title.upper().startswith("PII:"):
                signals.append("filename_has_better_title")
            elif fn_title.lower()[:20] != title.lower()[:20] and POISONED_ID_RX.search(fn):
                signals.append("title_filename_identity_mismatch")

    if rating in {"not_ratable_reference_material", "not_applicable"}:
        signals.append(f"suspicious_rating:{rating}")

    notes = paper.get("notes_and_flags") or {}
    if notes.get("reevaluation_required") or (paper.get("classification") or {}).get("guard_blocked_rating"):
        signals.append("prior_guard_or_reeval_flag")

    cl = paper.get("classification") or {}
    if rating.startswith("not_") and not cl.get("red_team_survival") and cl.get("rating"):
        ft_len = _text_length(paper)
        if ft_len >= 2000:
            signals.append("red_team_skipped_with_substantial_text")

    return signals


def _source_path(paper: dict) -> Path | None:
    fi = paper.get("file_info") or {}
    rel = fi.get("filepath") or ""
    if rel:
        p = config.MASTER_PROJECT / str(rel).replace("/", "\\")
        if p.exists():
            return p
    name = fi.get("renamed_filename")
    if name:
        p = config.SOURCE_PDFS / name
        if p.exists():
            return p
    return None


def _row_ownership(paper_id: str, audit: dict[str, Any]) -> dict[str, Any]:
    owned_examples = []
    for cls in ("owned_canonical_path", "owned_filename_exact", "owned_hash_exact"):
        for ex in (audit.get("examples") or {}).get(cls, []):
            for m in ex.get("matches") or []:
                if m.get("id") == paper_id:
                    owned_examples.append(ex.get("source_pdf"))
    unowned = set(audit.get("unowned_source_pdfs") or [])
    ambiguous = set(audit.get("ambiguous_source_pdfs") or [])
    fi = papers_db.get_paper(paper_id)
    fi = (fi or {}).get("file_info") or {}
    rel = str(fi.get("filepath") or f"source-pdfs/{fi.get('renamed_filename') or ''}")
    rel = rel.replace("\\", "/")
    if not rel.startswith("source-pdfs/"):
        rel = f"source-pdfs/{Path(rel).name}" if fi.get("renamed_filename") else rel
    status = "owned"
    if rel in ambiguous:
        status = "ambiguous"
    elif rel in unowned:
        status = "unowned"
    elif not owned_examples and fi.get("renamed_filename"):
        src = _source_path({"file_info": fi})
        if not src or not src.exists():
            status = "missing_source_pdf"
        elif status == "owned":
            status = "owned_unverified"
    return {
        "status": status,
        "source_pdf": rel,
        "owned_paths": sorted(set(owned_examples)),
    }


def gather_deterministic_title_candidates(
    paper: dict,
    *,
    src: Path | None,
    text_sample: str,
) -> list[dict[str, Any]]:
    bm = paper.get("basic_metadata") or {}
    fi = paper.get("file_info") or {}
    current = str(bm.get("title") or "")
    candidates: list[dict[str, Any]] = []

    def add(title: Any, source: str, confidence: float) -> None:
        t = re.sub(r"\s+", " ", str(title or "").strip())
        if not t:
            return
        invalid, reason = is_invalid_canonical_title(t)
        candidates.append(
            {
                "title": t,
                "source": source,
                "confidence": confidence,
                "rejected": invalid,
                "rejection_reason": reason if invalid else None,
            }
        )

    if current and not is_invalid_canonical_title(current)[0]:
        add(current, "existing_valid_title", 1.0)

    doi = str((paper.get("identifiers") or {}).get("doi") or bm.get("doi") or "")
    if doi and bm.get("title") and str(bm.get("title_source") or "") == "crossref":
        add(bm.get("title"), "stored_crossref", 0.95)

    for label, hint in (
        ("original_filename", fi.get("original_filename")),
        ("renamed_filename", fi.get("renamed_filename")),
        ("paper_id", paper.get("id")),
    ):
        if not hint:
            continue
        if label == "paper_id":
            t = _title_from_paper_id(str(hint))
            if t:
                add(t, "paper_id_derived", 0.65)
            continue
        rec = recover_title_from_filename(Path(str(hint)))
        if rec.get("title"):
            add(rec["title"], f"filename_{label}", float(rec.get("confidence") or 0.75))

    if src and src.exists():
        rec = recover_title_from_filename(src)
        if rec.get("title"):
            add(rec["title"], "filename_source_path", float(rec.get("confidence") or 0.8))

    if text_sample:
        try:
            fp = extract_first_page_heuristic(src or Path("x.pdf"), text_sample) or {}
            if fp.get("title"):
                add(fp["title"], "first_page_heuristic", 0.7)
        except Exception:
            pass

    pii_vals = extract_pii_from_text(text_sample)
    tier_meta = {
        "authors": bm.get("authors") or [],
        "year": bm.get("year"),
        "doi": doi or None,
        "journal": bm.get("journal"),
    }
    picked = select_identity(
        candidates,
        path=src or Path(str(fi.get("original_filename") or "unknown.pdf")),
        tier_meta=tier_meta,
        first_pages=text_sample,
    )
    if picked.get("title") and not is_invalid_canonical_title(picked.get("title"))[0]:
        if not any(c.get("source") == "existing_valid_title" and not c.get("rejected") for c in candidates):
            add(
                picked["title"],
                str(picked.get("title_source") or "selected_identity"),
                float(picked.get("title_confidence") or 0.6),
            )

    best: dict[str, dict[str, Any]] = {}
    for c in candidates:
        key = c["title"].lower()
        prev = best.get(key)
        if not prev or (not c.get("rejected") and prev.get("rejected")) or c.get("confidence", 0) > prev.get("confidence", 0):
            best[key] = c
    return list(best.values())


def pick_repair_title(candidates: list[dict[str, Any]], current_title: str) -> dict[str, Any] | None:
    valid = [c for c in candidates if not c.get("rejected")]
    if not valid:
        return None
    cur = re.sub(r"\s+", " ", str(current_title or "").strip())
    cur_invalid = is_invalid_canonical_title(cur)[0]
    existing = next((c for c in valid if c.get("source") == "existing_valid_title"), None)
    if existing and not cur_invalid:
        return existing
    valid.sort(key=lambda c: float(c.get("confidence") or 0), reverse=True)
    best = valid[0]
    if cur_invalid:
        return best
    if cur and best["title"].strip().lower() == cur.lower():
        return best
    if float(best.get("confidence") or 0) <= 0.85:
        return None
    return None


def classify_bucket(
    paper: dict,
    *,
    poison_signals: list[str],
    ownership: dict[str, Any],
    repair_title: dict[str, Any] | None,
    article_signals: dict[str, Any],
) -> tuple[str, str, float]:
    bm = paper.get("basic_metadata") or {}
    title = str(bm.get("title") or "")
    ids = paper.get("identifiers") or {}
    rating = _rating(paper)

    if ownership.get("status") in {"ambiguous", "unowned", "missing_source_pdf"}:
        return "source_ownership_problem", ownership.get("status") or "ownership", 0.0

    has_identity_poison = any(
        s.startswith(("pii_", "invalid_title", "empty_title", "poisoned_", "title_filename"))
        for s in poison_signals
    )
    pii_in_identifiers_only = (
        bool(ids.get("pii"))
        and not has_identity_poison
        and not POISONED_ID_RX.search(str(paper.get("id") or ""))
        and "pii" not in str((paper.get("file_info") or {}).get("renamed_filename") or "").lower()
    )
    if pii_in_identifiers_only and not has_identity_poison:
        return "already_valid_identifier_only", "pii_stored_as_identifier", 1.0

    if not repair_title and has_identity_poison:
        return "uncertain_manual_review", "no_deterministic_title_recovery", 0.0

    if not repair_title and not has_identity_poison:
        if rating in {"not_ratable_reference_material", "not_applicable"} and any(
            s.startswith("suspicious_rating") for s in poison_signals
        ):
            if _is_genuine_reference_material(paper, article_signals):
                return "already_valid_identifier_only", "genuine_reference_material", 0.9
            ja = int(article_signals.get("journal_article_signals") or 0)
            if ja >= 2:
                return "mark_for_reevaluation", "journal_article_suspicious_rating", 0.85
        return "already_valid_identifier_only", "no_repair_needed", 1.0

    cur_invalid = is_invalid_canonical_title(title)[0]
    title_changes = bool(
        repair_title
        and repair_title.get("source") != "existing_valid_title"
        and repair_title["title"].strip().lower() != title.strip().lower()
    )
    id_poison = POISONED_ID_RX.search(str(paper.get("id") or ""))
    fn_poison = "poisoned_source_filename" in poison_signals

    needs_title_fix = cur_invalid or title_changes
    needs_id_fix = bool(id_poison)
    needs_fn_fix = bool(fn_poison)

    if repair_title and (needs_title_fix or needs_id_fix or needs_fn_fix):
        conf = float(repair_title.get("confidence") or 0)
        if needs_title_fix and conf < 0.65:
            return "uncertain_manual_review", "title_recovery_confidence_too_low", conf
        if needs_title_fix and is_invalid_canonical_title(repair_title.get("title"))[0]:
            return "uncertain_manual_review", "proposed_title_still_invalid", 0.0
        if conf >= 0.65 or (not needs_title_fix and (needs_id_fix or needs_fn_fix)):
            return "deterministic_repair_safe", "deterministic_identity_recovery", max(conf, 0.9 if not needs_title_fix else conf)

    if repair_title and rating.startswith("not_") and not _is_genuine_reference_material(paper, article_signals):
        ja = int(article_signals.get("journal_article_signals") or 0)
        if ja >= 2:
            return "mark_for_reevaluation", "poisoned_classification_journal_article", 0.8

    if has_identity_poison:
        return "uncertain_manual_review", "insufficient_confidence_for_auto_repair", 0.0
    return "already_valid_identifier_only", "stable", 1.0


def propose_changes(paper: dict, repair_title: dict[str, Any]) -> dict[str, Any]:
    bm = dict(paper.get("basic_metadata") or {})
    fi = dict(paper.get("file_info") or {})
    ids = dict(paper.get("identifiers") or {})
    current_title = str(bm.get("title") or "")
    if repair_title.get("source") == "existing_valid_title" or not is_invalid_canonical_title(current_title)[0]:
        new_title = current_title if not is_invalid_canonical_title(current_title)[0] else repair_title["title"]
    else:
        new_title = repair_title["title"]
    authors = list(bm.get("authors") or [])
    year = bm.get("year")
    orig = fi.get("original_filename") or fi.get("renamed_filename") or ""
    new_id = generate_id(authors, year, new_title, pdf_filename=str(orig))
    if new_id != paper.get("id") and papers_db.paper_exists(new_id):
        new_id = str(paper.get("id") or new_id)
    new_fn = generate_filename(authors, year, new_title, original_filename=str(orig))
    text_sample = _first_text_sample(paper)
    pii_list = extract_pii_from_text(text_sample)
    if pii_list:
        ids["pii"] = pii_list[0]
    return {
        "title": new_title,
        "title_source": repair_title.get("source"),
        "authors": authors,
        "year": year,
        "paper_id": new_id,
        "source_filename": new_fn,
        "filepath": f"source-pdfs/{new_fn}",
        "identifiers": ids,
    }


@dataclass
class RepairSession:
    execute: bool
    file_ops: list[tuple[str, str, str]] = field(default_factory=list)
    audit_records: list[dict[str, Any]] = field(default_factory=list)

    def rename_source(self, src: Path, dest: Path) -> None:
        if src.resolve() == dest.resolve():
            return
        if dest.exists():
            if _sha256(src) == _sha256(dest):
                return
            raise RuntimeError(f"destination exists with different content: {dest.name}")
        if self.execute:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        self.file_ops.append(("move", str(src), str(dest)))

    def rollback_files(self) -> None:
        for op, a, b in reversed(self.file_ops):
            if op == "move" and Path(b).exists() and not Path(a).exists():
                shutil.move(b, a)


def apply_repair(paper: dict, proposed: dict[str, Any], session: RepairSession) -> dict[str, Any]:
    pid = paper["id"]
    old_fn = (paper.get("file_info") or {}).get("renamed_filename")
    src = _source_path(paper)
    new_id = proposed["paper_id"]
    new_fn = proposed["source_filename"]

    if new_id != pid and papers_db.paper_exists(new_id):
        new_id = pid
    if is_unsafe_generated_id(new_id) and POISONED_ID_RX.search(pid):
        raise RuntimeError(f"refusing unsafe generated id: {new_id}")

    updated = papers_db.get_paper(pid) or paper
    bm = dict(updated.get("basic_metadata") or {})
    bm["title"] = proposed["title"]
    bm["authors"] = proposed.get("authors") or bm.get("authors") or []
    if proposed.get("year"):
        bm["year"] = proposed["year"]
    bm["title_source"] = proposed.get("title_source")
    updated["basic_metadata"] = bm
    updated["identifiers"] = proposed.get("identifiers") or updated.get("identifiers") or {}

    fi = dict(updated.get("file_info") or {})
    if src and new_fn and old_fn and str(old_fn) != str(new_fn):
        dest = reserve_unique_path(config.SOURCE_PDFS, new_fn, existing_ok=src)
        new_fn = dest.name
        proposed["source_filename"] = new_fn
        proposed["filepath"] = f"source-pdfs/{new_fn}"
        session.rename_source(src, dest)
    fi["renamed_filename"] = new_fn
    fi["filepath"] = proposed["filepath"]
    updated["file_info"] = fi

    if session.execute:
        if new_id != pid:
            conn = papers_db.get_connection()
            try:
                conn.execute("DELETE FROM papers WHERE id = ?", (pid,))
                conn.commit()
            finally:
                conn.close()
            updated["id"] = new_id
        papers_db.save_paper(updated)
    return updated


def mark_reevaluation(paper: dict, reason: str, session: RepairSession) -> dict[str, Any]:
    updated = papers_db.get_paper(paper["id"]) or paper
    cl = dict(updated.get("classification") or {})
    old_rating = cl.get("rating")
    if old_rating and str(old_rating).lower().replace("-", "_") in {
        "not_ratable_reference_material",
        "not_applicable",
    }:
        cl["guard_blocked_rating"] = old_rating
        cl["rating"] = None
    updated["classification"] = cl
    notes = dict(updated.get("notes_and_flags") or {})
    notes["reevaluation_required"] = reason
    notes["reevaluation_reason"] = "poisoned_identity_repair"
    updated["notes_and_flags"] = notes
    if session.execute:
        papers_db.save_paper(updated)
    return updated


def build_candidate(
    paper: dict,
    *,
    audit: dict[str, Any],
    full_paper: dict | None = None,
) -> dict[str, Any]:
    full = full_paper or papers_db.get_paper(paper["id"]) or paper
    poison = detect_poison_signals(full)
    if not poison:
        return {}

    bm = full.get("basic_metadata") or {}
    fi = full.get("file_info") or {}
    ids = full.get("identifiers") or {}
    src = _source_path(full)
    text_sample = _first_text_sample(full)
    ownership = _row_ownership(full["id"], audit)
    candidates = gather_deterministic_title_candidates(full, src=src, text_sample=text_sample)
    repair_title = pick_repair_title(candidates, str(bm.get("title") or ""))
    if bm.get("title") and not is_invalid_canonical_title(str(bm.get("title")))[0]:
        repair_title = {
            "title": str(bm.get("title")),
            "source": "existing_valid_title",
            "confidence": 1.0,
        }
    article_signals = score_journal_article_signals(
        text=text_sample,
        filename=str(fi.get("original_filename") or fi.get("renamed_filename") or ""),
        identity={
            "title": bm.get("title"),
            "doi": ids.get("doi"),
            "pii": ids.get("pii"),
            "identity_status": bm.get("identity_status"),
        },
        page_count=None,
    )
    bucket, reason, confidence = classify_bucket(
        full,
        poison_signals=poison,
        ownership=ownership,
        repair_title=repair_title,
        article_signals=article_signals,
    )
    guard = None
    rating = _rating(full)
    if rating.startswith("not_"):
        guard = check_article_type_guard(
            proposed_rating=rating,
            document_type=full.get("document_type"),
            article_signals=article_signals,
            identity={"title": bm.get("title")},
        )

    proposed = propose_changes(full, repair_title) if repair_title and bucket == "deterministic_repair_safe" else None
    if proposed and POISONED_ID_RX.search(str(paper.get("id") or "")) and proposed.get("paper_id") == paper.get("id"):
        bucket = "uncertain_manual_review"
        reason = "id_migration_blocked_by_collision"
        confidence = 0.0
        proposed = None
    action = bucket
    if bucket == "deterministic_repair_safe":
        action = "repair_identity"
    elif bucket == "mark_for_reevaluation":
        action = "mark_reevaluation"

    rejected = [c for c in candidates if c.get("rejected")]
    return {
        "paper_id": full["id"],
        "title": bm.get("title"),
        "authors": bm.get("authors"),
        "year": bm.get("year"),
        "journal": bm.get("journal"),
        "doi": ids.get("doi") or bm.get("doi"),
        "pii": ids.get("pii"),
        "source_pdf": ownership.get("source_pdf"),
        "source_ownership_status": ownership.get("status"),
        "full_text_length": _text_length(full),
        "full_text_sample": text_sample[:500],
        "filename_title_candidate": next(
            (c for c in candidates if str(c.get("source", "")).startswith("filename_") and not c.get("rejected")),
            None,
        ),
        "first_page_title_candidate": next(
            (c for c in candidates if c.get("source") == "first_page_heuristic" and not c.get("rejected")),
            None,
        ),
        "selected_repair_title": repair_title,
        "classification": (full.get("classification") or {}).get("rating"),
        "red_team_survival": (full.get("classification") or {}).get("red_team_survival"),
        "article_type_signals": article_signals,
        "reference_material_signals": article_signals.get("reference_material_signals"),
        "journal_article_signals": article_signals.get("journal_article_signals"),
        "repair_bucket": bucket,
        "repair_confidence": confidence,
        "proposed_action": action,
        "proposed_changes": proposed,
        "poison_signals": poison,
        "rejected_title_candidates": rejected[:10],
        "article_type_guard": guard,
        "bucket_reason": reason,
    }


def run_audit(
    *,
    execute: bool = False,
    write_report: bool = True,
    limit: int | None = None,
    paper_id: str | None = None,
) -> dict[str, Any]:
    mode = "execute" if execute else "plan_only"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    audit_before = build_audit()
    ownership_clean = audit_before.get("unowned_total", 1) == 0 and audit_before.get("ambiguous_total", 1) == 0

    if execute and not ownership_clean:
        return {
            "ok": False,
            "mode": mode,
            "aborted": True,
            "reason": "source_ownership_not_clean_before_execute",
            "ownership_before": {
                "unowned_total": audit_before.get("unowned_total"),
                "ambiguous_total": audit_before.get("ambiguous_total"),
            },
        }

    backup = None
    if execute:
        backup = _create_backup("PRE-IDENTITY-REPAIR")
        if not backup.get("ok"):
            return {"ok": False, "mode": mode, "aborted": True, "reason": "backup_failed", "backup": backup}

    session = RepairSession(execute=execute)
    candidates: list[dict[str, Any]] = []
    counts = {
        "deterministic_repair_safe": 0,
        "mark_for_reevaluation": 0,
        "already_valid_identifier_only": 0,
        "uncertain_manual_review": 0,
        "source_ownership_problem": 0,
    }
    repaired = 0
    reeval_marked = 0
    ids_repaired = 0
    filenames_repaired = 0
    records: list[dict[str, Any]] = []

    rows = papers_db.get_all_metadata()
    if paper_id:
        rows = [r for r in rows if r.get("id") == paper_id]
    for paper in rows:
        cand = build_candidate(paper, audit=audit_before)
        if not cand:
            continue
        candidates.append(cand)
        bucket = cand["repair_bucket"]
        counts[bucket] = counts.get(bucket, 0) + 1

    to_process = [c for c in candidates if c["repair_bucket"] == "deterministic_repair_safe"]
    if not execute:
        to_process.extend(c for c in candidates if c["repair_bucket"] == "mark_for_reevaluation")
    if limit is not None:
        to_process = to_process[:limit]

    for cand in to_process:
        pid = cand["paper_id"]
        paper = papers_db.get_paper(pid) or {"id": pid}
        before = {
            "paper_id": pid,
            "title": cand.get("title"),
            "source_pdf": cand.get("source_pdf"),
            "classification": cand.get("classification"),
            "reevaluation_flag": (paper.get("notes_and_flags") or {}).get("reevaluation_required"),
        }
        record = {
            "timestamp": _now(),
            "mode": mode,
            "paper_id_before": pid,
            "title_before": cand.get("title"),
            "source_pdf_before": cand.get("source_pdf"),
            "classification_before": cand.get("classification"),
            "reevaluation_flag_before": before["reevaluation_flag"],
            "repair_bucket": cand["repair_bucket"],
            "repair_confidence": cand.get("repair_confidence"),
            "evidence_sources": (cand.get("selected_repair_title") or {}).get("source"),
            "rejected_title_candidates": cand.get("rejected_title_candidates"),
            "reason": cand.get("bucket_reason"),
            "ownership_status_before": cand.get("source_ownership_status"),
            "warnings": [],
        }
        try:
            if cand["repair_bucket"] == "deterministic_repair_safe" and cand.get("proposed_changes"):
                proposed = cand["proposed_changes"]
                updated = apply_repair(paper, proposed, session)
                record["paper_id_after"] = updated.get("id")
                record["title_after"] = (updated.get("basic_metadata") or {}).get("title")
                record["source_pdf_after"] = (updated.get("file_info") or {}).get("filepath")
                record["classification_after"] = (updated.get("classification") or {}).get("rating")
                record["changed_fields"] = ["title", "file_info", "identifiers"]
                if proposed.get("paper_id") != pid:
                    record["changed_fields"].append("id")
                    ids_repaired += 1
                if proposed.get("source_filename") != Path(str(cand.get("source_pdf") or "")).name:
                    filenames_repaired += 1
                repaired += 1
                rating = _rating(paper)
                ja = int((cand.get("article_type_signals") or {}).get("journal_article_signals") or 0)
                if rating.startswith("not_") and ja >= 2 and not _is_genuine_reference_material(paper, cand.get("article_type_signals") or {}):
                    mark_reevaluation(updated, "poisoned_identity_after_title_repair", session)
                    reeval_marked += 1
                    record["reevaluation_flag_after"] = True
                    record["changed_fields"].append("classification_reeval")
            elif cand["repair_bucket"] == "mark_for_reevaluation":
                updated = mark_reevaluation(paper, cand.get("bucket_reason") or "poisoned_identity", session)
                record["paper_id_after"] = updated.get("id")
                record["title_after"] = (updated.get("basic_metadata") or {}).get("title")
                record["source_pdf_after"] = (updated.get("file_info") or {}).get("filepath")
                record["classification_after"] = (updated.get("classification") or {}).get("rating")
                record["reevaluation_flag_after"] = True
                record["changed_fields"] = ["classification", "notes_and_flags"]
                reeval_marked += 1
        except Exception as exc:
            session.rollback_files()
            record["warnings"].append(str(exc))
            record["status"] = "failed"
            records.append(record)
            if execute:
                return {
                    "ok": False,
                    "mode": mode,
                    "aborted": True,
                    "reason": f"repair_failed:{pid}",
                    "error": str(exc),
                    "partial_records": records,
                }
            continue
        record["status"] = "planned" if not execute else "applied"
        records.append(record)

    audit_after = build_audit()
    missing_paths = []
    for row in papers_db.get_all_metadata():
        src = _source_path(row)
        fi = row.get("file_info") or {}
        fn = fi.get("renamed_filename")
        if fn and not src:
            missing_paths.append({"paper_id": row["id"], "renamed_filename": fn})

    summary = {
        "ok": True,
        "mode": mode,
        "generated_at": _now(),
        "backup": backup,
        "rows_inspected": len(rows),
        "candidate_rows_found": len(candidates),
        "bucket_counts": counts,
        "repaired_rows": repaired if execute else sum(1 for c in candidates if c["repair_bucket"] == "deterministic_repair_safe"),
        "reevaluation_marked": reeval_marked if execute else sum(1 for c in candidates if c["repair_bucket"] == "mark_for_reevaluation"),
        "db_ids_repaired": ids_repaired,
        "source_filenames_repaired": filenames_repaired,
        "ownership_before": {
            "unowned_total": audit_before.get("unowned_total"),
            "ambiguous_total": audit_before.get("ambiguous_total"),
            "pass": ownership_clean,
        },
        "ownership_after": {
            "unowned_total": audit_after.get("unowned_total"),
            "ambiguous_total": audit_after.get("ambiguous_total"),
            "pass": audit_after.get("unowned_total", 1) == 0 and audit_after.get("ambiguous_total", 1) == 0,
        },
        "missing_source_paths_after": missing_paths[:20],
        "missing_db_row_ids_after": _missing_db_row_ids(audit_after),
        "candidates_sample": [{k: c.get(k) for k in ("paper_id", "title", "repair_bucket", "repair_confidence", "proposed_action", "poison_signals")} for c in candidates[:50]],
    }

    if write_report:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        jsonl_path = AUDIT_DIR / f"identity_repair_{ts}.jsonl"
        summary_path = AUDIT_DIR / f"identity_repair_summary_{ts}.json"
        with jsonl_path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            for cand in candidates:
                if cand["repair_bucket"] in {"uncertain_manual_review", "source_ownership_problem", "already_valid_identifier_only"}:
                    f.write(json.dumps({"timestamp": _now(), "mode": mode, "audit_only": True, **cand}, ensure_ascii=False) + "\n")
        summary["audit_jsonl"] = str(jsonl_path)
        summary["audit_summary"] = str(summary_path)
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    if execute and summary["ownership_after"]["pass"] is False:
        summary["ok"] = False
        summary["warnings"] = ["ownership_not_clean_after_execute"]

    return summary


def diagnose_known_cases() -> dict[str, Any]:
    """No-model diagnostics for known poison patterns."""
    out: dict[str, Any] = {}
    audit = build_audit()
    known_ids = [
        "Sokolov_2000_Perception",
        "NorthHolland_1987_Stimulus",
        "Barry_1991_Autonomic",
        "Barry_1993_Autonomic",
        "Barry_1998_PII",
    ]
    for pid in known_ids:
        paper = papers_db.get_paper(pid)
        if not paper:
            meta = next((r for r in papers_db.get_all_metadata() if r["id"] == pid), None)
            if not meta:
                out[pid] = {"status": "not_in_db"}
                continue
            paper = meta
        cand = build_candidate(paper, audit=audit, full_paper=papers_db.get_paper(pid))
        out[pid] = {
            "selected_title": (cand.get("selected_repair_title") or {}).get("title"),
            "title_source": (cand.get("selected_repair_title") or {}).get("source"),
            "rejected_pii": [r for r in (cand.get("rejected_title_candidates") or []) if "pii" in str(r.get("rejection_reason") or "")][:3],
            "identity_status": (cand.get("selected_repair_title") or {}).get("source"),
            "article_type_guard": cand.get("article_type_guard"),
            "repair_bucket": cand.get("repair_bucket"),
            "poison_signals": cand.get("poison_signals"),
        }
    for name in (
        "Barry-Automatic changes in a serial learning task-1991-Inter.pdf",
        "Barry-Autonomic patterning in elite and novice pistol shoote.pdf",
    ):
        rec = recover_title_from_filename(Path(name))
        out[name] = rec
    return out


def scan_active_poisoned_rows(*, uncertain_ids: set[str] | None = None) -> dict[str, Any]:
    uncertain_ids = uncertain_ids or set()
    audit = build_audit()
    blocking: list[dict[str, Any]] = []
    for paper in papers_db.get_all_metadata():
        pid = paper["id"]
        if pid in uncertain_ids:
            continue
        cand = build_candidate(paper, audit=audit)
        if not cand:
            continue
        if cand["repair_bucket"] == "deterministic_repair_safe":
            blocking.append({"paper_id": pid, "reason": "unrepaired_deterministic_safe", "title": cand.get("title")})
        elif cand["repair_bucket"] not in {"already_valid_identifier_only", "uncertain_manual_review", "source_ownership_problem", "mark_for_reevaluation"}:
            if any(s.startswith(("pii_", "invalid_title", "poisoned_")) for s in cand.get("poison_signals") or []):
                blocking.append({"paper_id": pid, "reason": "active_poisoned_identity", "title": cand.get("title")})
    return {"ok": not blocking, "blocking_count": len(blocking), "blocking_sample": blocking[:25]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit/repair poisoned article identity rows")
    parser.add_argument("--plan-only", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--paper-id", type=str, default=None)
    parser.add_argument("--diagnose-known", action="store_true")
    args = parser.parse_args()

    if args.diagnose_known:
        print(json.dumps(diagnose_known_cases(), indent=2, ensure_ascii=False))
        return 0

    execute = bool(args.execute)
    write_report = bool(args.write_report) or execute or args.plan_only
    report = run_audit(
        execute=execute,
        write_report=write_report,
        limit=args.limit,
        paper_id=args.paper_id,
    )
    print(json.dumps({k: v for k, v in report.items() if k not in ("candidates_sample", "partial_records")}, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
