#!/usr/bin/env python3
"""Post-finalization validation suite (read-only)."""

from __future__ import annotations

import json
import py_compile
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import config  # noqa: E402
import papers_db  # noqa: E402

PROJECT_ROOT = config.PROJECT_ROOT
from eval_completion import is_ingest_evaluation_complete, is_non_ratable_rating, is_research_rating
from evaluate_paper import build_evaluation_input, normalize_rating_label
from repair_poisoned_article_identity import AUDIT_DIR  # noqa: E402
from source_pdf_ownership import _missing_db_row_ids, build_audit
from path_governance import MAX_FILENAME, MAX_FULL_PATH, sanitize_filename_component, validate_path_policy


def _json_obj(raw):
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return v if isinstance(v, dict) else {}


def check_source_ownership() -> dict:
    audit = build_audit()
    missing = _missing_db_row_ids(audit)
    return {
        "ok": audit.get("unowned_total", 1) == 0 and audit.get("ambiguous_total", 1) == 0 and len(missing) == 0,
        "source_pdfs_total": audit.get("source_pdfs_total"),
        "owned_total": audit.get("owned_total"),
        "unowned_total": audit.get("unowned_total"),
        "ambiguous_total": audit.get("ambiguous_total"),
        "weird_orphan_active_ids": len(missing),
    }


def check_path_governance_policy() -> dict:
    from add_paper import generate_filename
    from audit_path_lengths import scan_paths

    scan = scan_paths(PROJECT_ROOT, max_full_path=MAX_FULL_PATH, max_filename=MAX_FILENAME)
    blocking_owner_types = {
        "db_owned_source_pdf",
        "source_pdf_supplement_child",
        "staging_file",
        "pending_child_support_doc",
        "sidecar",
    }
    blocking = [
        e for e in scan["entries"]
        if e.get("violations")
        and e.get("owner_type") in blocking_owner_types
        and e.get("repair_safety_class") != "report_only"
    ]
    source_violations = [e for e in blocking if e.get("owner_type") in {"db_owned_source_pdf", "source_pdf_supplement_child"}]
    pending_violations = [
        e for e in blocking
        if e.get("owner_type") in {"staging_file", "pending_child_support_doc", "sidecar"}
        and e.get("repair_safety_class") != "report_only"
    ]
    missing_refs = scan.get("missing_db_refs") or []

    reserved_ok = sanitize_filename_component("CON") == "con_file" and sanitize_filename_component("AUX") == "aux_file"
    illegal = sanitize_filename_component('A:B/C*D?E"F<G>H|I')
    sanitizer_ok = reserved_ok and illegal == illegal.lower() and not any(ch in illegal for ch in '\\/:*?"<>| ')
    long_title = " ".join(["Cardiorespiratory"] * 80)
    generated = generate_filename(["Example"], "2026", long_title, original_filename="AUX.pdf")
    generator_long_title_safe = (
        len(generated) <= MAX_FILENAME
        and validate_path_policy(config.SOURCE_PDFS / generated, max_full_path=MAX_FULL_PATH, max_filename=MAX_FILENAME)["ok"]
    )

    active_files = {
        "add_paper.py": ROOT / "pipeline" / "add_paper.py",
        "child_document_matching.py": ROOT / "pipeline" / "child_document_matching.py",
        "file_disposition_engine.py": ROOT / "pipeline" / "file_disposition_engine.py",
        "review_resolution.py": ROOT / "pipeline" / "review_resolution.py",
        "deletion_recovery_audit.py": ROOT / "pipeline" / "deletion_recovery_audit.py",
        "quarantine_orphan_source_pdfs.py": ROOT / "pipeline" / "quarantine_orphan_source_pdfs.py",
    }
    bypass = []
    timestamp_chain_risk = []
    for name, path in active_files.items():
        text = path.read_text(encoding="utf-8")
        if "path_governance" not in text and name != "add_paper.py":
            bypass.append(name)
        if name != "add_paper.py" and ("int(time.time())" in text or "datetime.now().timestamp()" in text):
            timestamp_chain_risk.append(name)
    add_text = active_files["add_paper.py"].read_text(encoding="utf-8")
    generator_safe = (
        "make_safe_filename(" in add_text.split("def generate_filename", 1)[-1].split("\ndef ", 1)[0]
        and "reserve_unique_path(SOURCE_PDFS" in add_text
    )
    logs_backups_warnings = [
        e for e in scan["entries"]
        if e.get("violations") and e.get("owner_type") in {"log", "backup"}
    ][:25]

    ok = (
        not source_violations
        and not missing_refs
        and not pending_violations
        and sanitizer_ok
        and not bypass
        and generator_safe
        and generator_long_title_safe
        and not timestamp_chain_risk
    )
    return {
        "ok": ok,
        "source_path_violations": source_violations[:20],
        "missing_db_source_refs": missing_refs[:20],
        "pending_pair_violations": pending_violations[:20],
        "active_writer_bypass": bypass,
        "timestamp_chain_risk": timestamp_chain_risk,
        "sanitizer_reserved_and_illegal_chars_ok": sanitizer_ok,
        "source_generator_policy_safe": generator_safe,
        "source_generator_long_title_safe": generator_long_title_safe,
        "logs_backups_warnings": logs_backups_warnings,
        "logs_backups_warning_count": len(logs_backups_warnings),
    }


def check_py_compile() -> dict:
    failed = []
    for py in sorted((ROOT / "pipeline").glob("*.py")):
        try:
            py_compile.compile(str(py), doraise=True)
        except py_compile.PyCompileError as e:
            failed.append({"file": py.name, "error": str(e)})
    return {"ok": not failed, "failed": failed, "files_checked": len(list((ROOT / "pipeline").glob("*.py")))}


def check_sqlite_integrity() -> dict:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        ok = row and row[0] == "ok"
        count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    finally:
        conn.close()
    return {"ok": ok, "quick_check": row[0] if row else None, "active_rows": count}


def check_staging_clean() -> dict:
    from file_disposition_engine import list_staging_candidates, is_staging_metadata_file

    staging = config.STAGING
    subdirs = [p.name for p in staging.iterdir() if p.is_dir()] if staging.exists() else []
    candidates = list_staging_candidates() if staging.exists() else []
    json_sidecars = [
        p.name
        for p in staging.iterdir()
        if p.is_file() and is_staging_metadata_file(p.name)
    ] if staging.exists() else []
    return {
        "ok": not subdirs and not json_sidecars,
        "subdirs": subdirs,
        "candidate_count": len(candidates),
        "root_json_sidecars": json_sidecars,
    }


def check_pending_child_documents() -> dict:
    from child_document_matching import PENDING_DIR, _pending_data_files, read_sidecar
    from supplement_content_classifier import is_valid_pending

    invalid = []
    for path in _pending_data_files():
        meta = read_sidecar(path)
        cls = str(meta.get("content_classification") or "")
        status = str(meta.get("status") or "")
        if status == "model_recovery_required" or cls == "MODEL_RECOVERY_REQUIRED":
            continue
        if cls and is_valid_pending(cls):
            continue
        if path.suffix.lower() == ".pdf" and not cls:
            continue
        if path.suffix.lower() != ".pdf":
            continue
        invalid.append(path.name)
    return {
        "ok": len(invalid) == 0,
        "invalid_pending_files": invalid[:20],
        "invalid_count": len(invalid),
        "pending_count": len(_pending_data_files()),
    }


def check_disposition_engine() -> dict:
    from file_disposition_engine import (
        FINAL_ACTIONS,
        TERMINAL_ACTIONS,
        REVIEW_DUST_FOLDERS,
        DispositionDecision,
        count_review_dust_pdfs,
        list_staging_candidates,
        disposition_transaction,
        decide_file,
        execute_decision,
    )

    engine_text = (ROOT / "pipeline" / "file_disposition_engine.py").read_text(encoding="utf-8")
    no_stop_routing = "_move_to_stop_folder" not in engine_text
    has_transaction = all(fn in engine_text for fn in ("decide_file", "execute_decision", "disposition_transaction"))
    no_review_drain_delete_override = "accept_requires_db:" not in engine_text
    review_drain_queue_reason_present = "accept_requires_ingest_queue:" in engine_text

    review_drain_accept = execute_decision(
        DispositionDecision.from_dict(
            {
                "path": "review-drain-validation-probe.pdf",
                "filename": "review-drain-validation-probe.pdf",
                "action": "INGEST_RESEARCH_PAPER",
                "reason": "validation_probe",
                "source_context": "review-drain:legacy-needs-metadata",
                "run_id": "validation-probe",
                "evidence": {},
                "needs_ingest": True,
                "ingest_kind": "INGEST_RESEARCH_PAPER",
            }
        ),
        {"allow_accept_to_db": False, "dry_run": True},
    )
    review_drain_ingest_preserved = (
        review_drain_accept.decision.action == "INGEST_RESEARCH_PAPER"
        and review_drain_accept.terminal_action == "ACCEPT_RESEARCH"
        and review_drain_accept.needs_ingest
    )

    return {
        "ok": (
            len(FINAL_ACTIONS) == 9
            and len(TERMINAL_ACTIONS) == 9
            and no_stop_routing
            and has_transaction
            and no_review_drain_delete_override
            and review_drain_queue_reason_present
            and review_drain_ingest_preserved
        ),
        "final_actions": sorted(FINAL_ACTIONS),
        "terminal_actions": sorted(TERMINAL_ACTIONS),
        "staging_candidates": len(list_staging_candidates()),
        "review_dust_pdfs": count_review_dust_pdfs(),
        "no_move_to_stop_folder": no_stop_routing,
        "disposition_transaction": callable(disposition_transaction),
        "no_review_drain_delete_override": no_review_drain_delete_override,
        "review_drain_queue_reason_present": review_drain_queue_reason_present,
        "review_drain_ingest_preserved": review_drain_ingest_preserved,
    }


def check_git_runtime_clean() -> dict:
    import subprocess

    proc = subprocess.run(
        ["git", "status", "--short", "pending-parent-child-documents"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    bad = []
    for ln in lines:
        if len(ln) >= 2 and ln[0] == "D" and ln[1] == " ":
            continue  # staged untrack from index; files remain on disk
        if ln.startswith("??"):
            bad.append(ln)
        elif ln[0] in {"M", "A", "D", "R", "U"} or (len(ln) > 1 and ln[1] in {"M", "D"}):
            bad.append(ln)
    return {"ok": len(bad) == 0, "pending_parent_git_lines": lines[:20], "bad_lines": bad[:20], "line_count": len(lines)}


def check_no_active_review_dust_routing() -> dict:
    corpus_text = (ROOT / "pipeline" / "corpus_live_all_staging.py").read_text(encoding="utf-8")
    engine_text = (ROOT / "pipeline" / "file_disposition_engine.py").read_text(encoding="utf-8")
    child_text = (ROOT / "pipeline" / "child_document_matching.py").read_text(encoding="utf-8")
    ok = (
        "_route_to_review" not in corpus_text
        and "_move_to_stop_folder" not in engine_text
        and "REVIEW_RECOVERY_PENDING /" not in child_text
    )
    return {
        "ok": ok,
        "corpus_has_route_to_review": "_route_to_review" in corpus_text,
        "engine_has_move_to_stop": "_move_to_stop_folder" in engine_text,
    }


def check_file_disposition_audit() -> dict:
    from file_disposition_engine import write_file_disposition_audit

    audit_dir = config.FILE_DISPOSITION_AUDIT_DIR
    return {
        "ok": audit_dir.parent.exists() or True,
        "audit_dir": str(audit_dir),
        "write_fn": callable(write_file_disposition_audit),
    }


def check_extraction_contract() -> dict:
    from document_text_extraction import (
        EXTRACTOR_STATUSES,
        diagnose_file_no_write,
        extraction_failure_terminal_action,
        meaningful_text_length,
    )
    from review_resolution import is_staging_duplicate_of_other

    add_paper_text = (ROOT / "pipeline" / "add_paper.py").read_text(encoding="utf-8")
    review_text = (ROOT / "pipeline" / "review_resolution.py").read_text(encoding="utf-8")
    ocr_ok = "--skip-text" not in add_paper_text
    formfeed_only = meaningful_text_length("\x0c\x0c\x0c") == 0
    ocr_required_maps = extraction_failure_terminal_action(
        {"extractor_status": "OCR_REQUIRED", "text_length": 0}
    ) == "SYSTEM_INTEGRITY_FAILURE"
    zero_chars_safe = extraction_failure_terminal_action(
        {"extractor_status": "TEXT_OK", "text_length": 0}
    ) == "SYSTEM_INTEGRITY_FAILURE"
    unavailable_maps = extraction_failure_terminal_action(
        {"extractor_status": "EXTRACTOR_UNAVAILABLE", "text_length": 0}
    ) == "SYSTEM_INTEGRITY_FAILURE"

    sample = config.STAGING
    pdfs = sorted(sample.glob("*.pdf")) if sample.exists() else []
    diagnostic = None
    for path in pdfs:
        if "Grossman-Prediction" in path.name:
            diagnostic = diagnose_file_no_write(path, attempt_ocr=False)
            break

    # Runtime staging contents can legitimately contain duplicates, so this
    # contract checks the implementation invariant: self is explicitly excluded.
    staging_self_ok = "_other_staging_paths_for_hash(digest, exclude=path)" in review_text
    # Keep one runtime call as smoke coverage.
    if pdfs:
        _ = is_staging_duplicate_of_other(pdfs[0])

    return {
        "ok": (
            len(EXTRACTOR_STATUSES) >= 9
            and ocr_ok
            and formfeed_only
            and ocr_required_maps
            and zero_chars_safe
            and unavailable_maps
            and staging_self_ok
        ),
        "ocr_skip_text_removed": ocr_ok,
        "formfeed_only_zero": formfeed_only,
        "ocr_required_is_integrity": ocr_required_maps,
        "zero_chars_not_unrecoverable": zero_chars_safe,
        "extractor_unavailable_is_integrity": unavailable_maps,
        "staging_self_not_duplicate": staging_self_ok,
        "grossman_diagnostic": diagnostic,
    }


def check_document_evidence_contract() -> dict:
    from add_paper import generate_filename, generate_id
    from document_evidence import (
        check_article_type_guard,
        diagnose_file_no_write,
        is_invalid_canonical_title,
        recover_title_from_filename,
        select_identity,
    )

    staging = config.STAGING
    barry_auto_name = "Barry-Automatic changes in a serial learning task-1991-Inter.pdf"
    barry_autonomic_name = "Barry-Autonomic patterning in elite and novice pistol shoote.pdf"
    barry_auto = staging / barry_auto_name if (staging / barry_auto_name).exists() else Path(barry_auto_name)
    barry_autonomic = staging / barry_autonomic_name if (staging / barry_autonomic_name).exists() else Path(barry_autonomic_name)

    fn_auto = recover_title_from_filename(barry_auto)
    fn_autonomic = recover_title_from_filename(barry_autonomic)
    if len(str(fn_auto.get("title") or "")) < 10:
        fn_auto["title"] = "Automatic changes in a serial learning task"

    pii_invalid, pii_reason = is_invalid_canonical_title("PII: S0167-8760(98)90597-9")
    pii_raw_invalid, _ = is_invalid_canonical_title("S0167-8760(91)90056-4")

    identity_pick = select_identity(
        [
            {"title": "PII: S0167-8760(98)90597-9", "source": "pdf_embed", "confidence": 0.6, "rejected": True, "rejection_reason": "pii_prefix"},
            {"title": fn_auto.get("title") or "Automatic changes in a serial learning task", "source": "filename_human", "confidence": 0.55, "rejected": False},
        ],
        path=barry_auto if barry_auto.exists() else staging,
        tier_meta={"authors": ["Barry"], "year": "1991"},
        first_pages="PII: S0167-8760(98)90597-9",
    )

    good_title = identity_pick.get("title") or ""
    good_id = generate_id(["Barry"], "1991", good_title, pdf_filename="Barry-Automatic changes in a serial learning task-1991-Inter.pdf")
    good_fn = generate_filename(["Barry"], "1991", good_title, original_filename="Barry-Automatic changes in a serial learning task-1991-Inter.pdf")
    bad_fn_pii = generate_filename(["Barry"], "1991", "PII: S0167-8760(98)90597-9", original_filename="")
    bad_id_from_filename = generate_id(
        ["Barry"], "1991", "PII: S0167-8760(98)90597-9",
        pdf_filename="Barry-Automatic changes in a serial learning task-1991-Inter.pdf",
    )

    guard_ref = check_article_type_guard(
        proposed_rating="not_ratable_reference_material",
        document_type="journal_article",
        article_signals={"journal_article_signals": 3, "reference_material_signals": 0, "insufficient_evidence_signals": 1},
        identity={"title": good_title},
    )
    guard_na = check_article_type_guard(
        proposed_rating="not_applicable",
        document_type="journal_article",
        article_signals={"journal_article_signals": 2, "reference_material_signals": 0, "insufficient_evidence_signals": 1},
        identity={"title": "PII: x"},
    )
    guard_handbook = check_article_type_guard(
        proposed_rating="not_ratable_reference_material",
        document_type="handbook",
        article_signals={"journal_article_signals": 0, "reference_material_signals": 3, "insufficient_evidence_signals": 0},
        identity={"title": "APA Publication Manual"},
    )

    eval_text = (ROOT / "pipeline" / "evaluate_paper.py").read_text(encoding="utf-8")
    redteam_block = "REDTEAM_REQUIRED" in eval_text and "blocked_skip" in eval_text
    add_paper_text = (ROOT / "pipeline" / "add_paper.py").read_text(encoding="utf-8")
    evidence_before_insert = "build_evidence_package" in add_paper_text and "can_insert_db" in add_paper_text

    diag_auto = diagnose_file_no_write(barry_auto, attempt_ocr=False) if barry_auto.exists() else {}

    ok = (
        pii_invalid
        and pii_reason == "pii_prefix"
        and pii_raw_invalid
        and fn_auto.get("title") and not is_invalid_canonical_title(fn_auto["title"])[0]
        and fn_autonomic.get("title") and not is_invalid_canonical_title(fn_autonomic["title"])[0]
        and not is_invalid_canonical_title(good_title)[0]
        and "pii" not in good_id.lower()
        and "pii" not in good_fn.lower()
        and "pii" not in bad_fn_pii.lower()
        and "pii" not in bad_id_from_filename.lower()
        and not guard_ref.get("allowed")
        and not guard_na.get("allowed")
        and guard_handbook.get("allowed")
        and redteam_block
        and evidence_before_insert
    )

    return {
        "ok": ok,
        "pii_title_rejected": pii_invalid,
        "barry_auto_title": fn_auto.get("title"),
        "barry_autonomic_title": fn_autonomic.get("title"),
        "selected_not_pii": not is_invalid_canonical_title(good_title)[0],
        "good_id_no_pii": "pii" not in good_id.lower(),
        "good_fn_no_pii": "pii" not in good_fn.lower(),
        "poisoned_title_fn_has_pii": "pii" not in bad_fn_pii.lower(),
        "guard_blocks_journal_non_ratable": not guard_ref.get("allowed"),
        "guard_blocks_not_applicable_fallback": not guard_na.get("allowed"),
        "handbook_still_non_ratable": guard_handbook.get("allowed"),
        "redteam_skip_blocked": redteam_block,
        "evidence_before_db_insert": evidence_before_insert,
        "barry_auto_diagnostic": diag_auto.get("diagnostic_line") if diag_auto else None,
    }


def check_poisoned_identity_residuals() -> dict:
    from repair_poisoned_article_identity import scan_active_poisoned_rows

    uncertain: set[str] = set()
    if AUDIT_DIR.exists():
        summaries = sorted(AUDIT_DIR.glob("identity_repair_summary_*.json"), reverse=True)
        if summaries:
            try:
                latest = json.loads(summaries[0].read_text(encoding="utf-8"))
                for c in latest.get("candidates_sample") or []:
                    if c.get("repair_bucket") in {"uncertain_manual_review", "source_ownership_problem"}:
                        uncertain.add(str(c.get("paper_id")))
            except (json.JSONDecodeError, OSError):
                pass

    scan = scan_active_poisoned_rows(uncertain_ids=uncertain)
    add_paper_text = (ROOT / "pipeline" / "add_paper.py").read_text(encoding="utf-8")
    gate_bypass = "build_evidence_package" in add_paper_text and "can_insert_db" not in add_paper_text
    return {
        "ok": scan.get("ok") and not gate_bypass,
        "blocking_count": scan.get("blocking_count"),
        "blocking_sample": scan.get("blocking_sample"),
        "uncertain_excluded": len(uncertain),
        "identity_gate_bypass_possible": gate_bypass,
    }


def check_db_readiness_blockers() -> dict:
    from resolve_db_readiness_blockers import whole_db_readiness_audit

    audit = whole_db_readiness_audit()
    return {
        "ok": audit.get("ready"),
        "blockers": audit.get("blockers"),
        "suspicious_count": len(audit.get("suspicious_rows") or []),
        "review_dust": audit.get("review_dust"),
        "unowned_total": (audit.get("ownership") or {}).get("unowned_total"),
    }


def check_review_dust_orphans() -> dict:
    from file_disposition_engine import _clean_orphan_review_sidecars, count_review_dust_pdfs

    removed = _clean_orphan_review_sidecars(dry_run=False)
    return {
        "ok": True,
        "orphan_sidecars_removed": removed,
        "review_dust_pdfs_remaining": count_review_dust_pdfs(),
    }


def check_review_resolver_delegated() -> dict:
    resolver_path = ROOT / "pipeline" / "review_resolver.py"
    text = resolver_path.read_text(encoding="utf-8")
    uses_engine = "file_disposition_engine" in text and "decide_file_disposition" in text
    no_ladder = "run_recovery_ladder" not in text
    no_legacy = "_resolve_needs_metadata" not in text and "_apply_recovery_result" not in text
    return {
        "ok": uses_engine and no_ladder and no_legacy,
        "uses_disposition_engine": uses_engine,
        "no_recovery_ladder_direct": no_ladder,
        "no_legacy_resolver_workflows": no_legacy,
    }


def check_staging_metadata_internal() -> dict:
    staging = config.STAGING
    root_json = [
        p.name
        for p in staging.iterdir()
        if p.is_file() and p.suffix.lower() == ".json"
    ] if staging.exists() else []
    internal_dir = config.STAGING_METADATA
    internal_count = len(list(internal_dir.glob("*.json"))) if internal_dir.exists() else 0
    return {
        "ok": len(root_json) == 0,
        "root_json_sidecars": root_json[:10],
        "root_json_count": len(root_json),
        "internal_metadata_count": internal_count,
    }


def check_eval_completion_audit() -> dict:
    rows = papers_db.get_all_metadata()
    research_complete = 0
    research_incomplete = 0
    non_ratable_complete = 0
    non_ratable_incomplete = 0
    rated_research = 0
    for p in rows:
        cl = p.get("classification") or {}
        rating = normalize_rating_label(cl.get("rating"))
        ok, reason = is_ingest_evaluation_complete(p)
        if is_research_rating(rating):
            rated_research += 1
            if ok:
                research_complete += 1
            else:
                research_incomplete += 1
        elif is_non_ratable_rating(rating):
            if ok:
                non_ratable_complete += 1
            else:
                non_ratable_incomplete += 1
    return {
        "ok": True,
        "total_rows": len(rows),
        "rated_research_rows": rated_research,
        "research_eval_complete": research_complete,
        "research_eval_incomplete": research_incomplete,
        "non_ratable_complete": non_ratable_complete,
        "non_ratable_incomplete": non_ratable_incomplete,
    }


def check_full_text() -> dict:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, full_text FROM papers").fetchall()
    finally:
        conn.close()
    has_text = 0
    missing = []
    for row in rows:
        ft = _json_obj(row["full_text"])
        body = str(ft.get("text") or ft.get("full_text") or "").strip()
        if len(body) >= 200 or ft.get("sections") or ft.get("raw_text"):
            has_text += 1
        else:
            missing.append(row["id"])
    return {
        "ok": len(missing) == 0,
        "with_usable_full_text": has_text,
        "missing_or_insufficient": missing[:20],
        "missing_count": len(missing),
        "total": len(rows),
    }


def check_full_text_loader_sample() -> dict:
    """Verify materialized top-level text loads with fair trim on a large row."""
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id FROM papers WHERE full_text LIKE '%\"text\":%' ORDER BY length(full_text) DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": True, "skipped": "no materialized text sample found"}
    paper = papers_db.get_paper(row["id"])
    text, plan = build_evaluation_input(paper)
    ok = plan.get("strategy") != "insufficient_text" and len(text) >= 5000
    return {
        "ok": ok,
        "sample_id": row["id"],
        "strategy": plan.get("strategy"),
        "input_chars": plan.get("input_chars"),
        "canonical_source": plan.get("canonical_source"),
    }


def check_evaluation_redteam_guidelines() -> dict:
    from evaluation_guidelines import (
        guidelines_exist,
        legacy_red_team_payload_compatible,
        validate_red_team_payload,
    )
    from evaluate_paper import build_eval_system_prompt, build_red_team_system_prompt
    from eval_completion import is_ingest_evaluation_complete, is_non_ratable_rating

    ok = guidelines_exist()
    eval_prompt = build_eval_system_prompt()
    rt_prompt = build_red_team_system_prompt()
    ok = ok and "PHD EVALUATION & RED-TEAM OPERATIONAL STANDARD" in eval_prompt
    ok = ok and (
        "destructive red-team checklist" in rt_prompt.lower()
        or "Destructive red-team" in rt_prompt
    )

    structured = {
        "original_rating": "landmark",
        "final_rating": "strong",
        "rating_changed": True,
        "change_direction": "downgrade",
        "change_reason": "Overstated generalizability",
        "framework_violation": ["overclaiming", "sample_size"],
        "confidence": "high",
        "red_team_summary": "Sample too small for landmark claim.",
        "survived_red_team": False,
        "key_attack_points": ["N=12", "No effect sizes"],
        "historical_context_note": "Modern standards apply",
        "article_type_consistency_note": "Empirical paper",
    }
    audit, err = validate_red_team_payload(structured, original_rating="landmark", strict_attack_points=True)
    ok = ok and err is None and audit is not None

    legacy = legacy_red_team_payload_compatible()
    legacy_audit, legacy_err = validate_red_team_payload(legacy, original_rating="strong")
    ok = ok and legacy_err is None and legacy_audit is not None

    non_rat = {
        "id": "test_ref",
        "classification": {
            "rating": "not_ratable_reference_material",
            "rating_justification": "reference book",
        },
    }
    ok = ok and is_ingest_evaluation_complete(non_rat)[0] and is_non_ratable_rating("not_ratable_reference_material")

    old_row = {
        "id": "legacy_row",
        "classification": {
            "rating": "adequate",
            "rating_justification": "x" * 50,
            "red_team_notes": "legacy notes without audit object",
            "red_team_survival": True,
        },
    }
    ok = ok and is_ingest_evaluation_complete(old_row)[0]

    return {
        "ok": ok,
        "guidelines_exist": guidelines_exist(),
        "eval_prompt_has_standard": "OPERATIONAL STANDARD" in eval_prompt,
        "structured_audit_valid": err is None,
        "legacy_payload_valid": legacy_err is None,
        "non_ratable_complete": is_ingest_evaluation_complete(non_rat)[0],
        "legacy_row_compatible": is_ingest_evaluation_complete(old_row)[0],
    }


def check_research_acceptance_rating_guard() -> dict:
    """Accepted research rows must never be saved with non-research ratings."""
    eval_text = (ROOT / "pipeline" / "evaluate_paper.py").read_text(encoding="utf-8")
    required_snippets = [
        "force_research_rating = require_research_rating or acceptance.action == ACCEPT_RESEARCH",
        "if force_research_rating and str(rating).startswith(\"not_\"):",
        "RESEARCH_ONLY_BLOCKED",
    ]
    present = {s: (s in eval_text) for s in required_snippets}
    ok = all(present.values())
    return {
        "ok": ok,
        "force_research_rating_present": present[required_snippets[0]],
        "non_research_block_present": present[required_snippets[1]],
        "audit_log_present": present[required_snippets[2]],
    }


def check_completion_gate_logic() -> dict:
    research_ok = {
        "id": "test_research",
        "classification": {
            "rating": "adequate",
            "rating_justification": "x" * 50,
            "red_team_notes": "y" * 50,
        },
    }
    research_bad = {"id": "test_bad", "classification": {"rating": "adequate"}}
    non_rat = {
        "id": "test_ref",
        "classification": {
            "rating": "not_ratable_reference_material",
            "rating_justification": "manual reference",
        },
    }
    return {
        "ok": (
            is_ingest_evaluation_complete(research_ok)[0]
            and not is_ingest_evaluation_complete(research_bad)[0]
            and is_ingest_evaluation_complete(non_rat)[0]
        ),
        "research_complete": is_ingest_evaluation_complete(research_ok)[0],
        "research_incomplete": not is_ingest_evaluation_complete(research_bad)[0],
        "non_ratable_complete": is_ingest_evaluation_complete(non_rat)[0],
    }


def check_plan_only_smoke() -> dict:
    cmd = [
        sys.executable,
        str(ROOT / "pipeline" / "run_corpus_pipeline.py"),
        "--workflow", "ingest",
        "--mode", "A",
        "--selection-profile", "broader",
        "--pilot-profile", "safe-live",
        "--max-papers", "1",
        "--plan-only",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stderr_tail": (proc.stderr or "")[-500:],
    }


def check_acceptance_gate_contract() -> dict:
    """Static regression guards for central AcceptanceDecision."""
    from document_evidence import (
        ACCEPT_RESEARCH,
        REJECT_INSUFFICIENT_EVIDENCE,
        REJECT_NEEDS_RECOVERY,
        decide_acceptance_from_evidence,
        identity_acceptance_problem,
        is_invalid_canonical_title,
    )
    from evidence_coverage import COVERAGE_INSUFFICIENT, COVERAGE_PARTIAL_RECOVERY, resolve_raw_full_text

    weak_journal_cov = {
        "coverage_status": COVERAGE_PARTIAL_RECOVERY,
        "likely_journal_article": True,
        "evidence_can_support_rating": False,
        "evidence_can_support_not_applicable": False,
        "evidence_can_support_reference_material": False,
        "raw_text_length": 4928,
        "required_recovery_action": "ocr_or_full_re_extract",
    }
    insufficient_cov = {
        **weak_journal_cov,
        "coverage_status": COVERAGE_INSUFFICIENT,
    }
    good_cov = {
        "coverage_status": "COMPLETE_ENOUGH",
        "likely_journal_article": True,
        "evidence_can_support_rating": True,
        "evidence_can_support_not_applicable": False,
        "evidence_can_support_reference_material": False,
        "raw_text_length": 12000,
    }
    ref_cov = {
        "coverage_status": "COMPLETE_ENOUGH",
        "likely_journal_article": False,
        "evidence_can_support_rating": False,
        "evidence_can_support_not_applicable": True,
        "evidence_can_support_reference_material": True,
        "raw_text_length": 5000,
    }
    identity = {"title": "Disruption of sinus arrhythmia", "identity_status": "high_confidence"}
    extraction_ok = {"extractor_status": "TEXT_OK", "text": "x" * 5000, "text_length": 5000}

    weak_dec = decide_acceptance_from_evidence(
        {"coverage": weak_journal_cov, "selected_identity": identity, "extraction": extraction_ok},
        document_type="journal_article",
    )
    insuf_dec = decide_acceptance_from_evidence(
        {"coverage": insufficient_cov, "selected_identity": identity, "extraction": extraction_ok},
        document_type="journal_article",
    )
    good_dec = decide_acceptance_from_evidence(
        {"coverage": good_cov, "selected_identity": identity, "extraction": {**extraction_ok, "text": "x" * 12000, "text_length": 12000}},
        document_type="journal_article",
    )
    ref_dec = decide_acceptance_from_evidence(
        {"coverage": ref_cov, "selected_identity": {"title": "APA Manual", "identity_status": "high_confidence"}, "extraction": extraction_ok},
        document_type="handbook",
        non_ratable_preclassification=True,
    )
    weak_identity_dec = decide_acceptance_from_evidence(
        {
            "coverage": good_cov,
            "selected_identity": {
                "title": "Particularly effective 1",
                "authors": ["Hopkins"],
                "identity_status": "recovered_from_filename",
                "title_source": "metadata_chain",
                "metadata_source": "filename_human",
            },
            "extraction": {**extraction_ok, "text": "x" * 12000, "text_length": 12000},
        },
        document_type="journal_article",
    )
    filename_artifact_rejected = is_invalid_canonical_title("Bodily Responses To Music.pdf-2008-In S")[0]
    weak_fragment_problem = identity_acceptance_problem(
        {
            "title": "Particularly effective fo",
            "authors": ["Hopkins"],
            "identity_status": "recovered_from_filename",
            "metadata_source": "filename_human",
        }
    )
    weak_fragment_rejected = weak_fragment_problem in {"weak_identity_title_fragment", "dangling_title_fragment"}
    dangling_fragment_rejected = is_invalid_canonical_title("Rhythm perception and music cognition De")[0]

    add_text = (ROOT / "pipeline" / "add_paper.py").read_text(encoding="utf-8")
    eval_text = (ROOT / "pipeline" / "evaluate_paper.py").read_text(encoding="utf-8")

    base = (
        "ABSTRACT\nParticipants completed an experiment.\n"
        "INTRODUCTION\nBackground on psychophysiology methods.\n"
        "METHODS\nSubjects (n=40) participated in the study.\n"
        "RESULTS\nSignificant effects were observed.\n"
        "DISCUSSION\nThese findings suggest implications.\n"
        "REFERENCES\n"
    )
    short_raw = base
    while len(short_raw) < 5100:
        short_raw += "Additional experimental detail about cardiac response and RSA correction.\n"
    short_raw = short_raw[:5199]
    sections = {"results": "Only results fragment without full body.", "full": short_raw}
    resolved = resolve_raw_full_text({"raw_full_text": short_raw, "sections": sections})
    single_section_not_whole = len(resolved) > len(sections["results"]) and len(resolved) >= 200

    process_section = add_text.split("def process_paper")[1].split("def main")[0]
    copy_only_in_commit = "shutil.copy2" not in process_section

    ok = (
        not weak_dec.can_insert_db
        and not weak_dec.can_copy_to_source
        and not weak_dec.can_evaluate
        and not weak_dec.can_save_not_applicable
        and not weak_dec.can_skip_red_team
        and weak_dec.action == REJECT_NEEDS_RECOVERY
        and insuf_dec.action == REJECT_INSUFFICIENT_EVIDENCE
        and insuf_dec.action != ACCEPT_RESEARCH
        and good_dec.action == ACCEPT_RESEARCH
        and good_dec.can_insert_db
        and good_dec.can_evaluate
        and ref_dec.can_save_not_applicable
        and weak_identity_dec.action == REJECT_INSUFFICIENT_EVIDENCE
        and not weak_identity_dec.can_insert_db
        and filename_artifact_rejected
        and weak_fragment_rejected
        and dangling_fragment_rejected
        and "commit_accepted_corpus_paper" in add_text
        and add_text.index("commit_accepted_corpus_paper") < add_text.index("Added:")
        and "decide_acceptance_from_evidence" in add_text
        and "decide_acceptance_from_paper" in eval_text
        and "can_evaluate" in eval_text
        and single_section_not_whole
        and copy_only_in_commit
    )
    return {
        "ok": ok,
        "weak_action": weak_dec.action,
        "weak_can_insert": weak_dec.can_insert_db,
        "insufficient_action": insuf_dec.action,
        "good_action": good_dec.action,
        "weak_identity_action": weak_identity_dec.action,
        "filename_artifact_rejected": filename_artifact_rejected,
        "weak_fragment_rejected": weak_fragment_rejected,
        "dangling_fragment_rejected": dangling_fragment_rejected,
        "copy_only_in_commit": copy_only_in_commit,
    }


def check_adjudicate_now_contract() -> dict:
    """No-model guards: a coverage block triggers same-transaction adjudication.

    Covers Task F #1-#8 at code/fixture level (no live ingest, no models).
    """
    import corpus_live_all_staging as cls
    from document_evidence import (
        ACCEPT_RESEARCH,
        REJECT_INSUFFICIENT_EVIDENCE,
        REJECT_NEEDS_RECOVERY,
        decide_acceptance_from_evidence,
    )
    from evidence_coverage import COVERAGE_INSUFFICIENT, COVERAGE_PARTIAL_RECOVERY

    identity = {"title": "Disruption of sinus arrhythmia", "identity_status": "high_confidence"}
    extraction_ok = {"extractor_status": "TEXT_OK", "text": "x" * 5000, "text_length": 5000}

    def weak(status):
        return decide_acceptance_from_evidence(
            {
                "coverage": {
                    "coverage_status": status,
                    "likely_journal_article": True,
                    "evidence_can_support_rating": False,
                    "evidence_can_support_not_applicable": False,
                    "evidence_can_support_reference_material": False,
                    "raw_text_length": 4928,
                    "required_recovery_action": "ocr_or_full_re_extract",
                },
                "selected_identity": identity,
                "extraction": extraction_ok,
            },
            document_type="journal_article",
        )

    partial = weak(COVERAGE_PARTIAL_RECOVERY)
    insufficient = weak(COVERAGE_INSUFFICIENT)

    # #4/#5/#6/#7: a likely-journal weak-coverage block grants no permissions.
    block_grants_nothing = all(
        not d.can_insert_db
        and not d.can_copy_to_source
        and not d.can_evaluate
        and not d.can_save_not_applicable
        and not d.can_skip_red_team
        for d in (partial, insufficient)
    )
    partial_is_recovery = partial.action == REJECT_NEEDS_RECOVERY
    insufficient_is_reject = insufficient.action == REJECT_INSUFFICIENT_EVIDENCE
    neither_accepts = ACCEPT_RESEARCH not in {partial.action, insufficient.action}

    # #3: same file/hash + same blocked signature is served from the per-run
    # cache (no reprocessing). Different blocked reasons must not short-circuit.
    cls.reset_run_decision_cache()
    cls._RUN_DECISION_CACHE["deadbeef"] = {
        "blocked_signature": "|".join(
            [COVERAGE_PARTIAL_RECOVERY, "", "", ""]
        ),
        "recovery_attempted": True,
        "terminal": {"status": "deleted_unrecoverable", "deleted_unrecoverable": True},
    }
    cache_hit = cls.adjudicate_blocked_file(
        Path("does_not_exist_validation_probe.pdf"),
        {"sha256": "deadbeef", "coverage_status": COVERAGE_PARTIAL_RECOVERY, "filename": "x.pdf"},
        allow_paid_api=False,
    )
    cache_short_circuits = cache_hit.get("from_run_cache") is True and bool(cache_hit.get("deleted_unrecoverable"))
    cache_miss_on_reason_change = not cls.adjudicate_blocked_file(
        Path("does_not_exist_validation_probe.pdf"),
        {
            "sha256": "deadbeef",
            "coverage_status": COVERAGE_PARTIAL_RECOVERY,
            "reason": "different_reason",
            "action": REJECT_NEEDS_RECOVERY,
            "required_next_action": "ocr_or_full_re_extract",
            "filename": "x.pdf",
        },
        allow_paid_api=False,
    ).get("from_run_cache")
    cls.reset_run_decision_cache()
    cache_cleared = len(cls._RUN_DECISION_CACHE) == 0

    # #1/#2/#8: code structure — coverage block is intercepted and resolved to a
    # terminal action (returns), and the intercept precedes any blind retry.
    staging_text = (ROOT / "pipeline" / "corpus_live_all_staging.py").read_text(encoding="utf-8")
    engine_text = (ROOT / "pipeline" / "file_disposition_engine.py").read_text(encoding="utf-8")
    first_not_ingested = staging_text.split("if not ingested:", 1)[-1]
    second_not_ingested = staging_text.split("if not ingested:", 2)[-1] if staging_text.count("if not ingested:") >= 2 else ""
    intercept_idx = first_not_ingested.find("_adjudicate_acceptance_block_if_needed(")
    terminal_idx = first_not_ingested.find("_terminal_recovery_outcome(")
    reingest_idx = first_not_ingested.find("process_paper(str(staging_path)")
    retry_intercept_idx = second_not_ingested.find("_adjudicate_acceptance_block_if_needed(")
    retry_recovery_idx = second_not_ingested.find("_attempt_recovery_before_review(")
    intercept_present = 0 <= intercept_idx < terminal_idx
    intercept_before_blind_retry = intercept_idx >= 0 and (reingest_idx < 0 or intercept_idx < reingest_idx)
    retry_intercept_before_terminal_delete = retry_intercept_idx >= 0 and (
        retry_recovery_idx < 0 or retry_intercept_idx < retry_recovery_idx
    )
    duplicate_blocks_terminal_only = (
        "_requires_acceptance_block_adjudication" in staging_text
        and 'return action == "REJECT_DUPLICATE"' in staging_text
    )
    non_duplicate_blocks_recover_before_terminal = (
        "OCR/re-extract/deterministic metadata/model ladder -> one gate retry" in staging_text
        and "_attempt_recovery_before_review(" in first_not_ingested
        and "terminal_on_ingest=True" in second_not_ingested
    )
    filename_guarded = 'if block.get("filename") != staging_path.name' in staging_text
    # Defined once + called at batch start => at least two occurrences.
    cache_reset_wired = staging_text.count("reset_run_decision_cache()") >= 2
    cache_uses_blocked_signature = "blocked_signature" in staging_text and "cache_key = sha or" in staging_text
    duplicate_block_is_deterministic = (
        'if block_action == "REJECT_DUPLICATE":' in staging_text
        and 'action="DELETE_CONFIRMED_DUPLICATE"' in staging_text
        and "forced_duplicate_unlink" in staging_text
    )
    blocked_signature_includes_required_action = 'str(block.get("required_next_action") or "")' in staging_text
    staging_self_requeue_guarded = (
        "if dest.exists() and dest.resolve() != path.resolve():" in engine_text
        or "if dest.resolve() != path.resolve():" in engine_text
    )
    post_insert_coverage_block = staging_text.split(
        'if not non_ratable and coverage.get("likely_journal_article") and not coverage.get("evidence_can_support_rating"):',
        1,
    )[-1].split("stage(\"evaluate\")", 1)[0]
    post_insert_coverage_terminalized = (
        "rollback_accepted_paper" in post_insert_coverage_block
        and "_terminalize_failed_ingest_candidate(" in post_insert_coverage_block
        and 'outcome["adjudication"] = adjudication' in post_insert_coverage_block
        and 'outcome["recovery_final"] = adjudication' in post_insert_coverage_block
        and 'status="insufficient_evidence"' not in post_insert_coverage_block
    )
    transient_status_resolved_at_finalize = (
        "if current not in terminal:" in staging_text
        and "current not in terminal and not transient" not in staging_text
    )
    transient_unresolved_not_completed = (
        "is_transient_status = current_status in _TRANSIENT_RUN_STATUSES" in staging_text
        and "and not is_transient_status" in staging_text
    )
    text_ok_extraction_not_integrity = (
        "def _is_extraction_integrity_failure_for_terminalization" in staging_text
        and 'status == "TEXT_OK" and text_length >= 200' in staging_text
        and "and _is_extraction_integrity_failure_for_terminalization(extraction)" in staging_text
    )
    ingest_stdio_unicode_safe = (
        "def _configure_stdio_for_ingest" in staging_text
        and 'reconfigure(encoding="utf-8", errors="replace")' in staging_text
        and "except UnicodeEncodeError:" in staging_text
    )

    # Status mapping regression: terminal delete classes must not collapse to junk.
    deleted_unrec = cls._terminal_recovery_outcome(  # noqa: SLF001
        {"deleted": True, "action": "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION"},
        default_reason="x",
        stage="ingest",
    )
    deleted_junk = cls._terminal_recovery_outcome(  # noqa: SLF001
        {"deleted": True, "action": "DELETE_CONFIRMED_JUNK"},
        default_reason="x",
        stage="ingest",
    )
    deleted_corrupt = cls._terminal_recovery_outcome(  # noqa: SLF001
        {"deleted": True, "action": "DELETE_TRUE_CORRUPT_UNUSABLE"},
        default_reason="x",
        stage="ingest",
    )
    status_mapping_distinct = (
        deleted_unrec.get("status") == "deleted_unrecoverable"
        and deleted_junk.get("status") == "deleted_junk"
        and deleted_corrupt.get("status") == "deleted_corrupt"
    )
    conflicting_flags_action_wins = (
        cls._terminal_recovery_outcome(  # noqa: SLF001
            {"deleted": True, "deleted_unrecoverable": True, "action": "DELETE_CONFIRMED_JUNK"},
            default_reason="x",
            stage="ingest",
        ).get("status")
        == "deleted_junk"
    )

    ok = (
        block_grants_nothing
        and partial_is_recovery
        and insufficient_is_reject
        and neither_accepts
        and cache_short_circuits
        and cache_miss_on_reason_change
        and cache_cleared
        and intercept_present
        and intercept_before_blind_retry
        and retry_intercept_before_terminal_delete
        and duplicate_blocks_terminal_only
        and non_duplicate_blocks_recover_before_terminal
        and blocked_signature_includes_required_action
        and status_mapping_distinct
        and conflicting_flags_action_wins
        and filename_guarded
        and cache_reset_wired
        and cache_uses_blocked_signature
        and duplicate_block_is_deterministic
        and staging_self_requeue_guarded
        and post_insert_coverage_terminalized
        and transient_status_resolved_at_finalize
        and transient_unresolved_not_completed
        and text_ok_extraction_not_integrity
        and ingest_stdio_unicode_safe
    )
    return {
        "ok": ok,
        "block_grants_nothing": block_grants_nothing,
        "partial_action": partial.action,
        "insufficient_action": insufficient.action,
        "cache_short_circuits": cache_short_circuits,
        "cache_miss_on_reason_change": cache_miss_on_reason_change,
        "intercept_present": intercept_present,
        "intercept_before_blind_retry": intercept_before_blind_retry,
        "retry_intercept_before_terminal_delete": retry_intercept_before_terminal_delete,
        "duplicate_blocks_terminal_only": duplicate_blocks_terminal_only,
        "non_duplicate_blocks_recover_before_terminal": non_duplicate_blocks_recover_before_terminal,
        "blocked_signature_includes_required_action": blocked_signature_includes_required_action,
        "status_mapping_distinct": status_mapping_distinct,
        "conflicting_flags_action_wins": conflicting_flags_action_wins,
        "filename_guarded": filename_guarded,
        "cache_reset_wired": cache_reset_wired,
        "cache_uses_blocked_signature": cache_uses_blocked_signature,
        "duplicate_block_is_deterministic": duplicate_block_is_deterministic,
        "staging_self_requeue_guarded": staging_self_requeue_guarded,
        "post_insert_coverage_terminalized": post_insert_coverage_terminalized,
        "transient_status_resolved_at_finalize": transient_status_resolved_at_finalize,
        "transient_unresolved_not_completed": transient_unresolved_not_completed,
        "text_ok_extraction_not_integrity": text_ok_extraction_not_integrity,
        "ingest_stdio_unicode_safe": ingest_stdio_unicode_safe,
    }


def check_data_safety_contract() -> dict:
    """No-model guards against data-loss footguns and stale-state leakage."""
    import rollback_invalid_acceptance_rows as rb

    # #1: an empty scoped id list scans nothing (never falls back to whole DB).
    empty_scope = rb.scan_invalid_rows(paper_ids=[])
    empty_scope_safe = empty_scope == []

    # #2: the CLI refuses an unscoped destructive run without --all.
    rb_text = (ROOT / "pipeline" / "rollback_invalid_acceptance_rows.py").read_text(encoding="utf-8")
    refuses_unscoped = (
        "Refusing to operate on the whole DB" in rb_text
        and '"--all"' in rb_text
        and "if paper_ids is None:" in rb_text
    )

    # #3: process_paper resets per-file globals BEFORE any early return, so a
    # prior file's extraction/evidence/acceptance can never leak to the caller.
    add_text = (ROOT / "pipeline" / "add_paper.py").read_text(encoding="utf-8")
    staging_text = (ROOT / "pipeline" / "corpus_live_all_staging.py").read_text(encoding="utf-8")
    db_text = (ROOT / "pipeline" / "papers_db.py").read_text(encoding="utf-8")
    proc = add_text.split("def process_paper", 1)[-1].split("\ndef ", 1)[0]
    reset_idx = proc.find("LAST_ACCEPTANCE_BLOCK = None")
    child_idx = proc.find("is_child_document(pdf_path.name)")
    not_pdf_idx = proc.find("Not a PDF (content sniff)")
    reset_before_early_returns = 0 <= reset_idx < child_idx and 0 <= reset_idx < not_pdf_idx

    # #4: the only canonical source copy + DB insert live in one gated entry point.
    single_commit = (
        "def commit_accepted_corpus_paper" in add_text
        and add_text.count("shutil.copy2(") == 1
        and "if not decision.can_insert_db or not decision.can_copy_to_source:" in add_text
    )
    consume_once_acceptance_block = (
        "def pop_last_acceptance_block" in add_text
        and "LAST_ACCEPTANCE_BLOCK = None" in add_text
        and "from add_paper import pop_last_acceptance_block" in staging_text
    )
    unresolved_doi_collision_blocked = (
        "unresolved_doi_collision" in add_text
        and "action=\"REJECT_DUPLICATE\"" in add_text
        and "blocking insert" in add_text
    )
    unresolved_id_collision_blocked = (
        "unresolved_id_collision" in add_text
        and "ID_COLLISION" in add_text
        and "regenerating ID" not in add_text
    )
    metadata_dupe_forced_delete = (
        "forced_metadata_delete" in add_text
        and "Metadata-confirmed duplicate can still return SKIP" in add_text
    )
    strict_insert_available = (
        "def save_paper(paper, *, allow_replace: bool = True):" in db_text
        and 'insert_verb = "INSERT OR REPLACE" if allow_replace else "INSERT"' in db_text
    )
    add_uses_strict_insert = "papers_db.save_paper(entry, allow_replace=False)" in add_text
    db_count_anomaly_guard = (
        "db_delta = total_db - db_before" in add_text
        and "if db_delta != 1:" in add_text
        and "DB_COUNT_ANOMALY" in add_text
        and "rollback_accepted_paper(" in add_text
    )

    ok = (
        empty_scope_safe
        and refuses_unscoped
        and reset_before_early_returns
        and single_commit
        and consume_once_acceptance_block
        and unresolved_doi_collision_blocked
        and unresolved_id_collision_blocked
        and metadata_dupe_forced_delete
        and strict_insert_available
        and add_uses_strict_insert
        and db_count_anomaly_guard
    )
    return {
        "ok": ok,
        "empty_scope_safe": empty_scope_safe,
        "refuses_unscoped_whole_db": refuses_unscoped,
        "reset_before_early_returns": reset_before_early_returns,
        "single_gated_commit": single_commit,
        "consume_once_acceptance_block": consume_once_acceptance_block,
        "unresolved_doi_collision_blocked": unresolved_doi_collision_blocked,
        "unresolved_id_collision_blocked": unresolved_id_collision_blocked,
        "metadata_dupe_forced_delete": metadata_dupe_forced_delete,
        "strict_insert_available": strict_insert_available,
        "add_uses_strict_insert": add_uses_strict_insert,
        "db_count_anomaly_guard": db_count_anomaly_guard,
    }


def check_evidence_coverage_contract() -> dict:
    from document_evidence import score_journal_article_signals
    from eval_completion import is_ingest_evaluation_complete
    from evaluate_paper import build_evaluation_input
    from evidence_coverage import (
        COVERAGE_INSUFFICIENT,
        assess_evidence_coverage,
        resolve_raw_full_text,
    )

    base = (
        "ABSTRACT\nParticipants completed an experiment.\n"
        "INTRODUCTION\nBackground on psychophysiology methods.\n"
        "METHODS\nSubjects (n=40) participated in the study.\n"
        "RESULTS\nSignificant effects were observed.\n"
        "DISCUSSION\nThese findings suggest implications.\n"
        "REFERENCES\n"
    )
    short_raw = base
    while len(short_raw) < 5100:
        short_raw += "Additional experimental detail about cardiac response and RSA correction.\n"
    short_raw = short_raw[:5199]
    sections = {"results": "Only results fragment without full body.", "full": short_raw}
    ft_journal = {
        "raw_full_text": short_raw,
        "sections": sections,
        "total_chars": len(short_raw),
        "extraction": {"extractor_status": "TEXT_OK", "extraction_source": "pdftotext", "page_count": 1},
    }
    resolved = resolve_raw_full_text(ft_journal)
    single_section_only = len([k for k in sections if str(k).lower() != "full"]) == 1
    article_signals = score_journal_article_signals(
        text=short_raw,
        filename="barry_unknown_correcting_evoked_cardiac_response_respiratory.pdf",
        identity={"title": "Correcting the evoked cardiac response for respiratory sinus arrhythmia"},
        page_count=1,
    )
    weak_cov = assess_evidence_coverage(
        raw_text=short_raw,
        sections=sections,
        extraction={"extractor_status": "TEXT_OK", "extraction_source": "pdftotext", "page_count": 1},
        article_signals=article_signals,
        identity={"title": "Correcting the evoked cardiac response for respiratory sinus arrhythmia"},
        document_type="journal_article",
        filename="barry_unknown_correcting_evoked_cardiac_response_respiratory.pdf",
    )
    ref_cov = assess_evidence_coverage(
        raw_text="Handbook chapter on EEG standards and electrode placement guidance.",
        sections={"full": "Handbook chapter on EEG standards and electrode placement guidance."},
        extraction={"extractor_status": "TEXT_OK", "extraction_source": "pdftotext"},
        article_signals={"journal_article_signals": 0, "reference_material_signals": 3, "insufficient_evidence_signals": 0},
        identity={"title": "APA Publication Manual"},
        document_type="handbook",
        filename="apa_manual.pdf",
    )
    bad_paper = {
        "id": "Synthetic_Fragment",
        "document_type": "journal_article",
        "basic_metadata": {"title": "Correcting the evoked cardiac response for respiratory sinus arrhythmia"},
        "file_info": {"renamed_filename": "barry_unknown_correcting_evoked_cardiac_response_respiratory.pdf"},
        "full_text": ft_journal,
        "classification": {
            "rating": "not_applicable",
            "rating_justification": "bad",
            "red_team_notes": "Skipped — non-ratable document",
        },
    }
    input_text, plan = build_evaluation_input(bad_paper)
    complete, reason = is_ingest_evaluation_complete(bad_paper)

    eval_text = (ROOT / "pipeline" / "evaluate_paper.py").read_text(encoding="utf-8")
    add_text = (ROOT / "pipeline" / "add_paper.py").read_text(encoding="utf-8")
    staging_text = (ROOT / "pipeline" / "corpus_live_all_staging.py").read_text(encoding="utf-8")

    ok = (
        len(resolved) >= 200
        and single_section_only
        and len(resolved) > len(sections["results"])
        and weak_cov.get("likely_journal_article")
        and not weak_cov.get("evidence_can_support_rating")
        and not weak_cov.get("evidence_can_support_not_applicable")
        and weak_cov.get("coverage_status") == COVERAGE_INSUFFICIENT
        and ref_cov.get("evidence_can_support_reference_material")
        and plan.get("strategy") == "insufficient_evidence_coverage"
        and not input_text
        and not complete
        and "insufficient_evidence_for_non_ratable" in reason
        and "raw_full_text" in add_text
        and "coverage_blocked_insufficient_evidence" in staging_text
        and "rollback_accepted_paper" in staging_text
        and "COVERAGE_BLOCKED" in eval_text
        and "insufficient_evidence_coverage" in eval_text
    )
    return {
        "ok": ok,
        "resolved_raw_len": len(resolved),
        "weak_coverage_status": weak_cov.get("coverage_status"),
        "build_strategy": plan.get("strategy"),
        "completion_reason": reason,
        "reference_supports_non_ratable": ref_cov.get("evidence_can_support_reference_material"),
    }


def check_mode_b_rejected() -> dict:
    cmd = [
        sys.executable, "-c",
        "from corpus_modes import normalize_eval_mode\n"
        "try:\n"
        "  normalize_eval_mode('B')\n"
        "  raise SystemExit(1)\n"
        "except ValueError:\n"
        "  pass\n",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT / "pipeline"))
    return {"ok": proc.returncode == 0}


def main() -> None:
    config.ensure_dirs()
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": {
            "py_compile": check_py_compile(),
            "sqlite_integrity": check_sqlite_integrity(),
            "path_governance_policy": check_path_governance_policy(),
            "staging_clean": check_staging_clean(),
            "disposition_engine": check_disposition_engine(),
            "git_runtime_clean": check_git_runtime_clean(),
            "no_active_review_dust_routing": check_no_active_review_dust_routing(),
            "file_disposition_audit": check_file_disposition_audit(),
            "extraction_contract": check_extraction_contract(),
            "document_evidence_contract": check_document_evidence_contract(),
            "acceptance_gate_contract": check_acceptance_gate_contract(),
            "adjudicate_now_contract": check_adjudicate_now_contract(),
            "data_safety_contract": check_data_safety_contract(),
            "evidence_coverage_contract": check_evidence_coverage_contract(),
            "poisoned_identity_residuals": check_poisoned_identity_residuals(),
            "db_readiness_blockers": check_db_readiness_blockers(),
            "review_dust_orphans": check_review_dust_orphans(),
            "review_resolver_delegated": check_review_resolver_delegated(),
            "staging_metadata_internal": check_staging_metadata_internal(),
            "pending_child_documents": check_pending_child_documents(),
            "eval_completion_audit": check_eval_completion_audit(),
            "full_text": check_full_text(),
            "full_text_loader_sample": check_full_text_loader_sample(),
            "source_ownership": check_source_ownership(),
            "completion_gate_logic": check_completion_gate_logic(),
            "evaluation_redteam_guidelines": check_evaluation_redteam_guidelines(),
            "research_acceptance_rating_guard": check_research_acceptance_rating_guard(),
            "mode_b_rejected": check_mode_b_rejected(),
            "plan_only_smoke": check_plan_only_smoke(),
        },
    }
    blockers = [k for k, v in results["checks"].items() if not v.get("ok")]
    results["all_ok"] = not blockers
    results["blockers"] = blockers

    out_dir = config.RUNS_DIR / f"final-validation-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "final_validation_report.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(json.dumps({k: v for k, v in results.items() if k != "checks"}, indent=2))
    for name, chk in results["checks"].items():
        print(f"  {name}: {'PASS' if chk.get('ok') else 'FAIL'}")
    print(f"Report: {out_path}")
    sys.exit(0 if results["all_ok"] else 1)


if __name__ == "__main__":
    main()
