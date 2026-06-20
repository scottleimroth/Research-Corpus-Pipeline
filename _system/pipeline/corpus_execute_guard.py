"""Guarded execute preflight for corpus pipeline live pilot.

This module performs guarded validation of staged run plans. Preview stages stay
read-only; explicitly named live pilot stages remain separately gated and narrow.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from corpus_stage_d_preview import run_text_metadata_preview
from corpus_stage_e_preview import run_metadata_duplicate_resolution_preview
from corpus_stage_f_preview import run_final_route_plan_preview
from corpus_stage_g_preview import run_book_toc_chapter_preview
from corpus_stage_h_preview import run_final_pilot_execution_plan
from corpus_stage_i_duplicate_skip_log import run_live_duplicate_skip_log_only
from corpus_stage_j_koren_validation import run_koren_parent_validation_preview, run_koren_titlepage_editor_preview
from corpus_stage_k_koren_live_parent import run_live_koren_parent_record_only
from corpus_stage_l_koren_pdf_copy import run_live_koren_pdf_copy_only
from corpus_stage_m_koren_fileinfo_attach import run_live_koren_fileinfo_attach_only
from corpus_stage_n_koren_staging_archive import run_live_koren_staging_archive_only
from corpus_stage_o_harris_validation import run_harris_edited_volume_validation_preview
from corpus_stage_p_harris_live_parent import run_live_harris_parent_record_only
from corpus_stage_q_harris_pdf_copy import run_live_harris_pdf_copy_only
from corpus_stage_r_harris_fileinfo_attach import run_live_harris_fileinfo_attach_only
from corpus_stage_s_harris_staging_archive import run_live_harris_staging_archive_only
from corpus_stage_t_final_pilot_report import run_final_pilot_report_preview

EXECUTE_STAGES = {
    "preflight",
    "preflight-backup",
    "preflight-backup-plancheck",
    "text-metadata-preview",
    "metadata-duplicate-resolution-preview",
    "final-route-plan-preview",
    "book-toc-chapter-preview",
    "final-pilot-execution-plan",
    "live-duplicate-skip-log-only",
    "koren-titlepage-editor-preview",
    "koren-parent-validation-preview",
    "live-koren-parent-record-only",
    "live-koren-pdf-copy-only",
    "live-koren-fileinfo-attach-only",
    "live-koren-staging-archive-only",
    "harris-edited-volume-validation-preview",
    "live-harris-parent-record-only",
    "live-harris-pdf-copy-only",
    "live-harris-fileinfo-attach-only",
    "live-harris-staging-archive-only",
    "final-pilot-report-preview",
}

REQUIRED_STAGES = [
    "A. preflight",
    "B. backup",
    "C. ingest plan validation",
    "D. text/metadata extraction",
    "E. duplicate resolution",
    "F. final route/action plan preview",
    "G. book/TOC/chapter escalation preview",
    "H. final pilot execution plan preview",
    "I. live duplicate-skip log only",
    "J. Koren title-page/editor preview",
    "K. Koren parent validation preview",
    "L. live Koren parent-record only",
    "M. live Koren managed PDF copy only",
    "N. live Koren file-info attachment only",
    "O. live Koren staging archive only",
    "P. Harris edited-volume validation preview",
    "Q. live Harris parent-record only",
    "R. live Harris managed PDF copy only",
    "S. live Harris file-info attachment only",
    "T. live Harris staging archive only",
    "U. final pilot report preview",
    "V. evaluation planning",
    "W. paid evaluation (separately gated)",
    "X. validated DB save",
    "Y. production final report",
]


def resolve_plan_path(*, run_id: str = "", plan_path: str = "", runs_dir: str = "") -> Path | None:
    if plan_path:
        return Path(plan_path).resolve()
    if not run_id:
        return None
    base = Path(runs_dir) if runs_dir else config.RUNS_DIR
    return (base / run_id / "run_plan.json").resolve()


def load_run_plan(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _db_has_original_filename(filename: str, db_path: Path) -> bool:
    if not db_path.exists():
        return False
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM papers WHERE file_info LIKE ? LIMIT 1",
            (f"%{filename}%",),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _result(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": ok, "detail": detail}


def _looks_book_like(filename: str) -> bool:
    low = filename.lower()
    signals = (
        "handbook",
        "series",
        "editor",
        "(editor",
        "book",
        "oxford",
        "cambridge",
        "routledge",
        "nato asi",
    )
    return any(s in low for s in signals)


def _run_dir_from_plan_path(plan_path: Path) -> Path:
    return plan_path.parent


def _backup_db_for_pilot(run_dir: Path, db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"ok": False, "path": "", "bytes": 0, "detail": f"DB not found: {db_path}"}
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = config.BACKUP_DIR / f"papers-{ts}-PRE-CORPUS-PILOT.db"
    shutil.copy2(db_path, dest)
    size = dest.stat().st_size if dest.exists() else 0
    ok = dest.exists() and size > 1024 * 1024
    info = {
        "ok": ok,
        "path": str(dest),
        "bytes": size,
        "detail": "backup created and size verified" if ok else "backup missing or too small",
    }
    (run_dir / "stage_backup_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


def _existing_pilot_backup_info(run_dir: Path) -> dict[str, Any]:
    info_path = run_dir / "stage_backup_info.json"
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            p = Path(info.get("path", ""))
            size = p.stat().st_size if p.exists() else 0
            return {
                "ok": p.exists() and size > 1024 * 1024,
                "path": str(p),
                "bytes": size,
                "detail": "existing run backup verified" if p.exists() else "existing backup missing",
            }
        except Exception as e:
            return {"ok": False, "path": "", "bytes": 0, "detail": f"backup info parse failed: {e}"}

    backups = sorted(config.BACKUP_DIR.glob("*PRE-CORPUS-PILOT.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    if backups:
        p = backups[0]
        size = p.stat().st_size
        return {
            "ok": size > 1024 * 1024,
            "path": str(p),
            "bytes": size,
            "detail": "latest PRE-CORPUS-PILOT backup verified",
        }
    return {"ok": False, "path": "", "bytes": 0, "detail": "no PRE-CORPUS-PILOT backup found"}


def _existing_stage_d_preview_info(run_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    artifact = run_dir / "text_metadata_preview.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage D preview artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage D preview parse failed: {e}"}
    expected = {p.get("name") for p in plan.get("pdf_plans", [])}
    actual = {i.get("name") for i in data.get("items", [])}
    ok = data.get("stage") == "text-metadata-preview" and expected == actual and len(actual) == len(expected)
    return {
        "ok": ok,
        "path": str(artifact),
        "detail": "Stage D preview artifact verified" if ok else f"Stage D item mismatch expected={len(expected)} actual={len(actual)}",
    }


def _existing_stage_e_preview_info(run_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    artifact = run_dir / "metadata_duplicate_resolution_preview.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage E preview artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage E preview parse failed: {e}"}
    expected = {p.get("name") for p in plan.get("pdf_plans", [])}
    actual = {i.get("name") for i in data.get("items", [])}
    ok = data.get("stage") == "metadata-duplicate-resolution-preview" and expected == actual and len(actual) == len(expected)
    return {
        "ok": ok,
        "path": str(artifact),
        "detail": "Stage E preview artifact verified" if ok else f"Stage E item mismatch expected={len(expected)} actual={len(actual)}",
    }


def _existing_stage_f_preview_info(run_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    artifact = run_dir / "final_route_plan_preview.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage F preview artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage F preview parse failed: {e}"}
    expected = {p.get("name") for p in plan.get("pdf_plans", [])}
    actual = {i.get("name") for i in data.get("routes", [])}
    target_count = len([n for n in actual if str(n).startswith(("Harris-", "Koren-"))])
    ok = (
        data.get("stage") == "final-route-plan-preview"
        and expected == actual
        and len(actual) == len(expected)
        and target_count == 2
    )
    detail = "Stage F preview artifact verified" if ok else f"Stage F mismatch expected={len(expected)} actual={len(actual)} target_count={target_count}"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_stage_g_preview_info(run_dir: Path) -> dict[str, Any]:
    artifact = run_dir / "book_toc_chapter_preview.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage G preview artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage G preview parse failed: {e}"}
    processed = set(data.get("processed_only", []))
    expected = {
        "Harris-Effect of pegylation on pharmaceuticals-2003-Nature R.pdf",
        "Koren-A kinetic study of protein-protein interactions-1976-B.pdf",
    }
    ok = data.get("stage") == "book-toc-chapter-preview" and processed == expected
    detail = "Stage G preview artifact verified" if ok else f"Stage G target mismatch processed={sorted(processed)}"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_stage_h_preview_info(run_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    artifact = run_dir / "final_pilot_execution_plan.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage H final plan artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage H final plan parse failed: {e}"}
    expected = {p.get("name") for p in plan.get("pdf_plans", [])}
    actions = data.get("consolidated_actions", [])
    actual = {a.get("name") for a in actions}
    duplicate_count = len([a for a in actions if a.get("action_class") == "live_duplicate_skip_log_candidate"])
    non_duplicate_live_actions = [
        a.get("name")
        for a in actions
        if a.get("action_class") not in {"live_duplicate_skip_log_candidate", "live_machine_escalation_required_before_write", "live_parent_record_candidate"}
    ]
    ok = (
        data.get("stage") == "final-pilot-execution-plan"
        and expected == actual
        and duplicate_count == 3
        and not non_duplicate_live_actions
    )
    detail = (
        "Stage H final plan verified"
        if ok
        else f"Stage H mismatch expected={len(expected)} actual={len(actual)} duplicate_count={duplicate_count}"
    )
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_stage_i_duplicate_log_info(run_dir: Path) -> dict[str, Any]:
    artifact = run_dir / "live_duplicate_skip_log.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage I duplicate-skip log artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage I duplicate-skip log parse failed: {e}"}
    accepted = data.get("accepted_duplicate_skip_decisions", [])
    matched = {i.get("matched_existing_db_id") for i in accepted}
    ok = data.get("stage") == "live-duplicate-skip-log-only" and matched == {"Levitin2018", "SuarezRoca2021", "Yucel2021"}
    detail = "Stage I duplicate-skip log verified" if ok else f"Stage I mismatch matched={sorted(matched)}"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_stage_j_koren_validation_info(run_dir: Path) -> dict[str, Any]:
    artifact = run_dir / "koren_parent_validation_preview.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage J Koren parent validation artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage J Koren validation parse failed: {e}"}
    processed = data.get("processed_only", [])
    ok = data.get("stage") == "koren-parent-validation-preview" and processed == ["Koren-A kinetic study of protein-protein interactions-1976-B.pdf"]
    detail = "Stage J Koren parent validation artifact verified" if ok else f"Stage J mismatch processed={processed}"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_stage_k_live_koren_parent_info(run_dir: Path) -> dict[str, Any]:
    artifact = run_dir / "live_koren_parent_record_only.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage K live Koren parent artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage K artifact parse failed: {e}"}
    ok = (
        data.get("stage") == "live-koren-parent-record-only"
        and data.get("row_created") is True
        and data.get("row_id") == "Barrett_2016_Handbook_book"
        and data.get("processed_only") == ["Koren-A kinetic study of protein-protein interactions-1976-B.pdf"]
    )
    detail = "Stage K live Koren parent artifact verified" if ok else "Stage K artifact does not show created Koren row"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_stage_l_live_koren_pdf_copy_info(run_dir: Path) -> dict[str, Any]:
    artifact = run_dir / "live_koren_pdf_copy_only.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage L live Koren PDF copy artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage L artifact parse failed: {e}"}
    ok = (
        data.get("stage") == "live-koren-pdf-copy-only"
        and data.get("row_id") == "Barrett_2016_Handbook_book"
        and data.get("post_copy_validation", {}).get("ok") is True
        and data.get("managed_copied_pdf_hash") == "7fcfd2b7a7da136de6d2a053dbf060f455351d8048dd2fca231e7902a1e948c1"
    )
    detail = "Stage L live Koren PDF copy artifact verified" if ok else "Stage L artifact does not show valid Koren PDF copy"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_stage_m_live_koren_fileinfo_info(run_dir: Path) -> dict[str, Any]:
    artifact = run_dir / "live_koren_fileinfo_attach_only.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage M live Koren file-info artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage M artifact parse failed: {e}"}
    ok = (
        data.get("stage") == "live-koren-fileinfo-attach-only"
        and data.get("row_id") == "Barrett_2016_Handbook_book"
        and data.get("post_write_validation", {}).get("ok") is True
        and data.get("pdf_validation", {}).get("managed_pdf_hash_matches") is True
        and data.get("pdf_validation", {}).get("staged_pdf_exists") is True
    )
    detail = "Stage M live Koren file-info artifact verified" if ok else "Stage M artifact does not show valid file-info attachment"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_koren_titlepage_editor_info(run_dir: Path) -> dict[str, Any]:
    artifact = run_dir / "koren_titlepage_editor_preview.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Koren title-page/editor preview artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Koren title-page/editor preview parse failed: {e}"}
    preview = data.get("titlepage_editor_preview", {})
    ok = (
        data.get("stage") == "koren-titlepage-editor-preview"
        and data.get("processed_only") == ["Koren-A kinetic study of protein-protein interactions-1976-B.pdf"]
        and preview.get("editor_validation_status") == "validated"
        and preview.get("editors_or_authors") == ["Lisa Feldman Barrett", "Michael Lewis", "Jeannette M. Haviland-Jones"]
    )
    detail = "Koren title-page/editor artifact verified" if ok else "Koren title-page/editor artifact not validated"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_stage_o_harris_validation_info(run_dir: Path) -> dict[str, Any]:
    artifact = run_dir / "harris_edited_volume_validation_preview.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage O Harris validation artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage O Harris validation parse failed: {e}"}
    id_preview = data.get("safe_parent_id_preview") or {}
    row = data.get("proposed_parent_row_preview") or {}
    duplicate = data.get("duplicate_reassessment") or {}
    ok = (
        data.get("stage") == "harris-edited-volume-validation-preview"
        and data.get("processed_only") == ["Harris-Effect of pegylation on pharmaceuticals-2003-Nature R.pdf"]
        and data.get("live_readiness_decision") == "ready_for_parent_record_live_pilot"
        and duplicate.get("status") == "no_duplicate_signal"
        and id_preview.get("candidate_id") == "Reuter_2016_Neuroeconomics_edited"
        and row.get("id_preview") == "Reuter_2016_Neuroeconomics_edited"
    )
    detail = "Stage O Harris validation artifact verified" if ok else "Stage O Harris validation artifact is not live-ready"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_stage_p_live_harris_parent_info(run_dir: Path) -> dict[str, Any]:
    artifact = run_dir / "live_harris_parent_record_only.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage P live Harris parent artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage P artifact parse failed: {e}"}
    ok = (
        data.get("stage") == "live-harris-parent-record-only"
        and data.get("row_created") is True
        and data.get("row_id") == "Reuter_2016_Neuroeconomics_edited"
        and data.get("processed_only") == ["Harris-Effect of pegylation on pharmaceuticals-2003-Nature R.pdf"]
        and data.get("db_validation", {}).get("row_read_back_cleanly") is True
    )
    detail = "Stage P live Harris parent artifact verified" if ok else "Stage P artifact does not show created Harris row"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_stage_q_live_harris_pdf_copy_info(run_dir: Path) -> dict[str, Any]:
    artifact = run_dir / "live_harris_pdf_copy_only.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage Q live Harris PDF copy artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage Q artifact parse failed: {e}"}
    ok = (
        data.get("stage") == "live-harris-pdf-copy-only"
        and data.get("row_id") == "Reuter_2016_Neuroeconomics_edited"
        and data.get("post_copy_validation", {}).get("ok") is True
        and data.get("managed_copied_pdf_hash") == "e99b0e4977097f3aac1f6d91326702cce58673ff4f2e6dbe9a7e7f9c7fe2d305"
        and data.get("db_update", {}).get("unchanged") is True
    )
    detail = "Stage Q live Harris PDF copy artifact verified" if ok else "Stage Q artifact does not show valid Harris PDF copy"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_stage_r_live_harris_fileinfo_info(run_dir: Path) -> dict[str, Any]:
    artifact = run_dir / "live_harris_fileinfo_attach_only.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage R live Harris file-info artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage R artifact parse failed: {e}"}
    ok = (
        data.get("stage") == "live-harris-fileinfo-attach-only"
        and data.get("row_id") == "Reuter_2016_Neuroeconomics_edited"
        and data.get("post_write_validation", {}).get("ok") is True
        and data.get("pdf_validation", {}).get("managed_pdf_hash_matches") is True
        and data.get("pdf_validation", {}).get("staged_pdf_exists") is True
        and data.get("db_validation", {}).get("no_non_target_rows_changed") is True
    )
    detail = "Stage R live Harris file-info artifact verified" if ok else "Stage R artifact does not show valid file-info attachment"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_stage_n_live_koren_archive_info(run_dir: Path) -> dict[str, Any]:
    artifact = run_dir / "live_koren_staging_archive_only.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage N live Koren archive artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage N artifact parse failed: {e}"}
    ok = (
        data.get("stage") == "live-koren-staging-archive-only"
        and data.get("archived") is True
        and data.get("post_archive_validation", {}).get("ok") is True
        and data.get("original_staged_pdf_exists_after") is False
    )
    detail = "Stage N live Koren archive artifact verified" if ok else "Stage N artifact does not show valid Koren archive"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _existing_stage_s_live_harris_archive_info(run_dir: Path) -> dict[str, Any]:
    artifact = run_dir / "live_harris_staging_archive_only.json"
    if not artifact.exists():
        return {"ok": False, "path": str(artifact), "detail": "Stage S live Harris archive artifact missing"}
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(artifact), "detail": f"Stage S artifact parse failed: {e}"}
    ok = (
        data.get("stage") == "live-harris-staging-archive-only"
        and data.get("archived") is True
        and data.get("post_archive_validation", {}).get("ok") is True
        and data.get("original_staged_pdf_exists_after") is False
    )
    detail = "Stage S live Harris archive artifact verified" if ok else "Stage S artifact does not show valid Harris archive"
    return {"ok": ok, "path": str(artifact), "detail": detail}


def _validate_ingest_plan_shape(plan: dict[str, Any]) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    doc_risks: list[dict[str, Any]] = []
    required_pdf_fields = [
        "sha256",
        "metadata_route",
        "text_extraction_check",
        "duplicate_check_status",
        "safe_id_route",
        "proposed_evaluation_action",
        "review_escalation_route",
        "proposed_db_action",
        "proposed_ingest_action",
    ]
    for pdf in plan.get("pdf_plans", []):
        missing = [f for f in required_pdf_fields if f not in pdf or pdf.get(f) in (None, "")]
        checks.append(_result(f"plan fields: {pdf.get('name')}", not missing, f"missing={missing}"))

        db_action = str(pdf.get("proposed_db_action", "")).lower()
        checks.append(
            _result(
                f"DB save gated: {pdf.get('name')}",
                "schema" in db_action and ("approval" in db_action or "backup" in db_action),
                pdf.get("proposed_db_action", ""),
            )
        )

        dest = str(pdf.get("proposed_managed_pdf_destination", "")).lower()
        checks.append(
            _result(
                f"PDF move/copy gated: {pdf.get('name')}",
                "canonical_filename_after_metadata_validation" in dest,
                pdf.get("proposed_managed_pdf_destination", ""),
            )
        )

        eval_action = pdf.get("proposed_evaluation_action", {})
        checks.append(
            _result(
                f"paid API gated: {pdf.get('name')}",
                eval_action.get("batch_api") == "planned_not_submitted",
                str(eval_action),
            )
        )

        route = [str(x).lower() for x in pdf.get("review_escalation_route", [])]
        checks.append(
            _result(
                f"human review last fallback: {pdf.get('name')}",
                route and route[0] == "normal_automated_processing",
                str(pdf.get("review_escalation_route", [])),
            )
        )

        if _looks_book_like(pdf.get("name", "")):
            doc_risks.append(
                {
                    "name": pdf.get("name", ""),
                    "risk": "document_type_check_required",
                    "reason": "filename looks like a book/handbook/edited volume; do not assume journal_article",
                }
            )
        if pdf.get("document_type_classification") in {"book", "book_chapter", "edited_volume", "supplement", "other_or_unknown"}:
            doc_risks.append(
                {
                    "name": pdf.get("name", ""),
                    "risk": "document_type_check_required",
                    "reason": f"planned document type is {pdf.get('document_type_classification')}",
                }
            )
        checks.append(
            _result(
                f"already-in-DB check: {pdf.get('name')}",
                not pdf.get("already_in_db"),
                ",".join(pdf.get("already_in_db_signals", [])) or "no DB representation signals",
            )
        )

    return all(c["ok"] for c in checks), checks, doc_risks


def run_limited_execute_stage(args) -> tuple[int, str]:
    """Run limited A-C execute-stage checks. Side effects: optional backup + run report only."""
    if args.execute_stage not in EXECUTE_STAGES:
        return (
            1,
            "Unsupported or missing --execute-stage. "
            "Allowed: preflight, preflight-backup, preflight-backup-plancheck, "
            "text-metadata-preview, metadata-duplicate-resolution-preview, "
            "final-route-plan-preview, book-toc-chapter-preview, final-pilot-execution-plan, "
            "live-duplicate-skip-log-only, koren-titlepage-editor-preview, koren-parent-validation-preview, "
            "live-koren-parent-record-only, live-koren-pdf-copy-only, live-koren-fileinfo-attach-only, "
            "live-koren-staging-archive-only, harris-edited-volume-validation-preview, "
            "live-harris-parent-record-only, live-harris-pdf-copy-only, "
            "live-harris-fileinfo-attach-only, live-harris-staging-archive-only, "
            "final-pilot-report-preview.",
        )

    ok, checks, plan = validate_execute_preconditions(args)
    lines = ["Corpus Pipeline Limited Execute Stage", "=" * 44, f"Stage: {args.execute_stage}", ""]
    lines.append(render_execute_preflight(checks, plan))

    if plan is None:
        return 1, "\n".join(lines)

    plan_path = resolve_plan_path(run_id=args.run_id, plan_path=args.plan_path, runs_dir=args.runs_dir)
    run_dir = _run_dir_from_plan_path(plan_path)
    db_path = Path(args.db_path).resolve() if args.db_path else config.DB_PATH.resolve()

    # For limited stages, paid/db/pdf gates are intentionally not required because
    # later stages are not executed. Base safety checks must pass.
    hard_fail_checks = {
        "--execute present",
        "--mode explicit",
        "--max-papers <= 5",
        "run plan selected",
        "run_plan.json exists",
        "plan mode matches request",
        "plan paper count matches max",
        "DB path exists or blank mode",
        "staging path exists",
        "no existing run lock",
        "selected PDFs still exist",
        "selected PDF hashes unchanged",
        "already processed handling",
    }
    base_ok = all(c["ok"] for c in checks if c["check"] in hard_fail_checks)
    backup_info = None
    plancheck_ok = True
    plancheck_rows: list[dict[str, Any]] = []
    doc_risks: list[dict[str, Any]] = []

    if not base_ok:
        lines.append("\nBase preflight failed; backup not run and later live progression is blocked.")
    else:
        lines.append("\nStage A preflight: PASS")

        if args.execute_stage in {"preflight-backup", "preflight-backup-plancheck"}:
            backup_info = _backup_db_for_pilot(run_dir, db_path)
            lines.append(
                f"Stage B backup: {'PASS' if backup_info['ok'] else 'FAIL'} — "
                f"{backup_info['path']} ({backup_info['bytes']} bytes)"
            )

    if args.execute_stage in {
        "preflight-backup-plancheck",
        "text-metadata-preview",
        "metadata-duplicate-resolution-preview",
        "final-route-plan-preview",
        "book-toc-chapter-preview",
        "final-pilot-execution-plan",
        "live-duplicate-skip-log-only",
        "koren-titlepage-editor-preview",
        "koren-parent-validation-preview",
        "live-koren-parent-record-only",
        "live-koren-pdf-copy-only",
        "live-koren-fileinfo-attach-only",
        "live-koren-staging-archive-only",
        "harris-edited-volume-validation-preview",
        "live-harris-parent-record-only",
        "live-harris-pdf-copy-only",
        "live-harris-fileinfo-attach-only",
        "live-harris-staging-archive-only",
        "final-pilot-report-preview",
    }:
        plancheck_ok, plancheck_rows, doc_risks = _validate_ingest_plan_shape(plan)
        lines.append(f"Stage C ingest plan validation: {'PASS' if plancheck_ok else 'FAIL'}")
        if doc_risks:
            lines.append("Document-type risk flags:")
            for risk in doc_risks:
                lines.append(f"- {risk['name']}: {risk['risk']} ({risk['reason']})")

    stage_d_summary = None
    stage_e_summary = None
    stage_f_summary = None
    stage_g_summary = None
    stage_h_summary = None
    stage_i_summary = None
    stage_koren_titlepage_summary = None
    stage_j_summary = None
    stage_k_live_summary = None
    stage_l_copy_summary = None
    stage_m_fileinfo_summary = None
    stage_n_archive_summary = None
    stage_o_harris_summary = None
    stage_p_harris_live_summary = None
    stage_q_harris_copy_summary = None
    stage_r_harris_fileinfo_summary = None
    stage_s_harris_archive_summary = None
    stage_t_final_report_summary = None
    if args.execute_stage == "text-metadata-preview":
        existing_backup = _existing_pilot_backup_info(run_dir)
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        if base_ok and plancheck_ok and existing_backup["ok"]:
            stage_d_summary = run_text_metadata_preview(plan, run_dir)
            lines.append("Stage D text/metadata preview: PASS")
            lines.append(f"Preview artifact: {run_dir / 'text_metadata_preview.json'}")
        else:
            lines.append("Stage D text/metadata preview: NOT RUN (preconditions failed)")

    if args.execute_stage == "metadata-duplicate-resolution-preview":
        existing_backup = _existing_pilot_backup_info(run_dir)
        stage_d_info = _existing_stage_d_preview_info(run_dir, plan)
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        lines.append(
            f"Existing Stage D artifact: {'PASS' if stage_d_info['ok'] else 'FAIL'} — "
            f"{stage_d_info['path']} ({stage_d_info['detail']})"
        )
        if base_ok and plancheck_ok and existing_backup["ok"] and stage_d_info["ok"]:
            stage_e_summary = run_metadata_duplicate_resolution_preview(plan, run_dir)
            lines.append("Stage E metadata/duplicate resolution preview: PASS")
            lines.append(f"Preview artifact: {run_dir / 'metadata_duplicate_resolution_preview.json'}")
        else:
            lines.append("Stage E metadata/duplicate resolution preview: NOT RUN (preconditions failed)")

    if args.execute_stage == "final-route-plan-preview":
        existing_backup = _existing_pilot_backup_info(run_dir)
        stage_d_info = _existing_stage_d_preview_info(run_dir, plan)
        stage_e_info = _existing_stage_e_preview_info(run_dir, plan)
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        lines.append(
            f"Existing Stage D artifact: {'PASS' if stage_d_info['ok'] else 'FAIL'} — "
            f"{stage_d_info['path']} ({stage_d_info['detail']})"
        )
        lines.append(
            f"Existing Stage E artifact: {'PASS' if stage_e_info['ok'] else 'FAIL'} — "
            f"{stage_e_info['path']} ({stage_e_info['detail']})"
        )
        if base_ok and plancheck_ok and existing_backup["ok"] and stage_d_info["ok"] and stage_e_info["ok"]:
            stage_f_summary = run_final_route_plan_preview(plan, run_dir)
            lines.append("Stage F final route/action plan preview: PASS")
            lines.append(f"Preview artifact: {run_dir / 'final_route_plan_preview.json'}")
        else:
            lines.append("Stage F final route/action plan preview: NOT RUN (preconditions failed)")

    if args.execute_stage == "book-toc-chapter-preview":
        existing_backup = _existing_pilot_backup_info(run_dir)
        stage_d_info = _existing_stage_d_preview_info(run_dir, plan)
        stage_e_info = _existing_stage_e_preview_info(run_dir, plan)
        stage_f_info = _existing_stage_f_preview_info(run_dir, plan)
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        lines.append(
            f"Existing Stage D artifact: {'PASS' if stage_d_info['ok'] else 'FAIL'} — "
            f"{stage_d_info['path']} ({stage_d_info['detail']})"
        )
        lines.append(
            f"Existing Stage E artifact: {'PASS' if stage_e_info['ok'] else 'FAIL'} — "
            f"{stage_e_info['path']} ({stage_e_info['detail']})"
        )
        lines.append(
            f"Existing Stage F artifact: {'PASS' if stage_f_info['ok'] else 'FAIL'} — "
            f"{stage_f_info['path']} ({stage_f_info['detail']})"
        )
        if base_ok and plancheck_ok and existing_backup["ok"] and stage_d_info["ok"] and stage_e_info["ok"] and stage_f_info["ok"]:
            stage_g_summary = run_book_toc_chapter_preview(plan, run_dir)
            lines.append("Stage G book/TOC/chapter preview: PASS")
            lines.append(f"Preview artifact: {run_dir / 'book_toc_chapter_preview.json'}")
        else:
            lines.append("Stage G book/TOC/chapter preview: NOT RUN (preconditions failed)")

    if args.execute_stage == "final-pilot-execution-plan":
        existing_backup = _existing_pilot_backup_info(run_dir)
        stage_d_info = _existing_stage_d_preview_info(run_dir, plan)
        stage_e_info = _existing_stage_e_preview_info(run_dir, plan)
        stage_f_info = _existing_stage_f_preview_info(run_dir, plan)
        stage_g_info = _existing_stage_g_preview_info(run_dir)
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        for label, info in (
            ("Stage D", stage_d_info),
            ("Stage E", stage_e_info),
            ("Stage F", stage_f_info),
            ("Stage G", stage_g_info),
        ):
            lines.append(f"Existing {label} artifact: {'PASS' if info['ok'] else 'FAIL'} — {info['path']} ({info['detail']})")
        if (
            base_ok
            and plancheck_ok
            and existing_backup["ok"]
            and stage_d_info["ok"]
            and stage_e_info["ok"]
            and stage_f_info["ok"]
            and stage_g_info["ok"]
        ):
            stage_h_summary = run_final_pilot_execution_plan(plan, run_dir)
            lines.append("Stage H final pilot execution plan: PASS")
            lines.append(f"Preview artifact: {run_dir / 'final_pilot_execution_plan.json'}")
        else:
            lines.append("Stage H final pilot execution plan: NOT RUN (preconditions failed)")

    if args.execute_stage == "live-duplicate-skip-log-only":
        existing_backup = _existing_pilot_backup_info(run_dir)
        stage_h_info = _existing_stage_h_preview_info(run_dir, plan)
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        lines.append(
            f"Existing Stage H artifact: {'PASS' if stage_h_info['ok'] else 'FAIL'} — "
            f"{stage_h_info['path']} ({stage_h_info['detail']})"
        )
        if base_ok and plancheck_ok and existing_backup["ok"] and stage_h_info["ok"]:
            stage_i_summary = run_live_duplicate_skip_log_only(plan, run_dir)
            lines.append("Stage I live duplicate-skip log only: PASS")
            lines.append(f"Audit artifact: {run_dir / 'live_duplicate_skip_log.json'}")
        else:
            lines.append("Stage I live duplicate-skip log only: NOT RUN (preconditions failed)")

    if args.execute_stage == "koren-titlepage-editor-preview":
        existing_backup = _existing_pilot_backup_info(run_dir)
        prior_infos = [
            ("Stage D", _existing_stage_d_preview_info(run_dir, plan)),
            ("Stage E", _existing_stage_e_preview_info(run_dir, plan)),
            ("Stage F", _existing_stage_f_preview_info(run_dir, plan)),
            ("Stage G", _existing_stage_g_preview_info(run_dir)),
            ("Stage H", _existing_stage_h_preview_info(run_dir, plan)),
            ("Stage I", _existing_stage_i_duplicate_log_info(run_dir)),
            ("Stage J", _existing_stage_j_koren_validation_info(run_dir)),
        ]
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        for label, info in prior_infos:
            lines.append(f"Existing {label} artifact: {'PASS' if info['ok'] else 'FAIL'} — {info['path']} ({info['detail']})")
        if base_ok and plancheck_ok and existing_backup["ok"] and all(info["ok"] for _, info in prior_infos):
            stage_koren_titlepage_summary = run_koren_titlepage_editor_preview(plan, run_dir)
            lines.append("Koren title-page/editor preview: PASS")
            lines.append(f"Preview artifact: {run_dir / 'koren_titlepage_editor_preview.json'}")
        else:
            lines.append("Koren title-page/editor preview: NOT RUN (preconditions failed)")

    if args.execute_stage == "koren-parent-validation-preview":
        existing_backup = _existing_pilot_backup_info(run_dir)
        prior_infos = [
            ("Stage D", _existing_stage_d_preview_info(run_dir, plan)),
            ("Stage E", _existing_stage_e_preview_info(run_dir, plan)),
            ("Stage F", _existing_stage_f_preview_info(run_dir, plan)),
            ("Stage G", _existing_stage_g_preview_info(run_dir)),
            ("Stage H", _existing_stage_h_preview_info(run_dir, plan)),
            ("Stage I", _existing_stage_i_duplicate_log_info(run_dir)),
        ]
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        for label, info in prior_infos:
            lines.append(f"Existing {label} artifact: {'PASS' if info['ok'] else 'FAIL'} — {info['path']} ({info['detail']})")
        if base_ok and plancheck_ok and existing_backup["ok"] and all(info["ok"] for _, info in prior_infos):
            stage_j_summary = run_koren_parent_validation_preview(plan, run_dir)
            lines.append("Stage J Koren parent validation preview: PASS")
            lines.append(f"Preview artifact: {run_dir / 'koren_parent_validation_preview.json'}")
        else:
            lines.append("Stage J Koren parent validation preview: NOT RUN (preconditions failed)")

    if args.execute_stage == "live-koren-parent-record-only":
        existing_backup = _existing_pilot_backup_info(run_dir)
        prior_infos = [
            ("Stage D", _existing_stage_d_preview_info(run_dir, plan)),
            ("Stage E", _existing_stage_e_preview_info(run_dir, plan)),
            ("Stage F", _existing_stage_f_preview_info(run_dir, plan)),
            ("Stage G", _existing_stage_g_preview_info(run_dir)),
            ("Stage H", _existing_stage_h_preview_info(run_dir, plan)),
            ("Stage I", _existing_stage_i_duplicate_log_info(run_dir)),
            ("Koren title-page/editor", _existing_koren_titlepage_editor_info(run_dir)),
            ("Stage J", _existing_stage_j_koren_validation_info(run_dir)),
        ]
        gate_ok = bool(args.allow_db_write) and not bool(args.allow_paid_api) and not bool(args.allow_pdf_copy)
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        for label, info in prior_infos:
            lines.append(f"Existing {label} artifact: {'PASS' if info['ok'] else 'FAIL'} — {info['path']} ({info['detail']})")
        lines.append(f"Live Koren DB-write gate: {'PASS' if args.allow_db_write else 'FAIL'} — --allow-db-write is required")
        lines.append(f"Paid API absent: {'PASS' if not args.allow_paid_api else 'FAIL'} — --allow-paid-api must not be used")
        lines.append(f"PDF copy gate absent: {'PASS' if not args.allow_pdf_copy else 'FAIL'} — --allow-pdf-copy must not be used")
        if base_ok and plancheck_ok and existing_backup["ok"] and gate_ok and all(info["ok"] for _, info in prior_infos):
            stage_k_live_summary = run_live_koren_parent_record_only(plan, run_dir)
            if stage_k_live_summary.get("row_created"):
                lines.append("Stage K live Koren parent-record-only: PASS")
            else:
                lines.append("Stage K live Koren parent-record-only: FAIL")
            lines.append(f"Live artifact: {run_dir / 'live_koren_parent_record_only.json'}")
        else:
            lines.append("Stage K live Koren parent-record-only: NOT RUN (preconditions failed)")

    if args.execute_stage == "live-koren-pdf-copy-only":
        existing_backup = _existing_pilot_backup_info(run_dir)
        prior_infos = [
            ("Stage D", _existing_stage_d_preview_info(run_dir, plan)),
            ("Stage E", _existing_stage_e_preview_info(run_dir, plan)),
            ("Stage F", _existing_stage_f_preview_info(run_dir, plan)),
            ("Stage G", _existing_stage_g_preview_info(run_dir)),
            ("Stage H", _existing_stage_h_preview_info(run_dir, plan)),
            ("Stage I", _existing_stage_i_duplicate_log_info(run_dir)),
            ("Koren title-page/editor", _existing_koren_titlepage_editor_info(run_dir)),
            ("Stage J", _existing_stage_j_koren_validation_info(run_dir)),
            ("Stage K", _existing_stage_k_live_koren_parent_info(run_dir)),
        ]
        gate_ok = bool(args.allow_pdf_copy) and not bool(args.allow_paid_api) and not bool(args.allow_db_write)
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        for label, info in prior_infos:
            lines.append(f"Existing {label} artifact: {'PASS' if info['ok'] else 'FAIL'} — {info['path']} ({info['detail']})")
        lines.append(f"Live Koren PDF-copy gate: {'PASS' if args.allow_pdf_copy else 'FAIL'} — --allow-pdf-copy is required")
        lines.append(f"Paid API absent: {'PASS' if not args.allow_paid_api else 'FAIL'} — --allow-paid-api must not be used")
        lines.append(f"DB write gate absent: {'PASS' if not args.allow_db_write else 'FAIL'} — copy-only pilot must not use --allow-db-write")
        if base_ok and plancheck_ok and existing_backup["ok"] and gate_ok and all(info["ok"] for _, info in prior_infos):
            stage_l_copy_summary = run_live_koren_pdf_copy_only(plan, run_dir)
            if stage_l_copy_summary.get("post_copy_validation", {}).get("ok"):
                lines.append("Stage L live Koren PDF-copy-only: PASS")
            else:
                lines.append("Stage L live Koren PDF-copy-only: FAIL")
            lines.append(f"Copy artifact: {run_dir / 'live_koren_pdf_copy_only.json'}")
        else:
            lines.append("Stage L live Koren PDF-copy-only: NOT RUN (preconditions failed)")

    if args.execute_stage == "live-koren-fileinfo-attach-only":
        existing_backup = _existing_pilot_backup_info(run_dir)
        prior_infos = [
            ("Stage D", _existing_stage_d_preview_info(run_dir, plan)),
            ("Stage E", _existing_stage_e_preview_info(run_dir, plan)),
            ("Stage F", _existing_stage_f_preview_info(run_dir, plan)),
            ("Stage G", _existing_stage_g_preview_info(run_dir)),
            ("Stage H", _existing_stage_h_preview_info(run_dir, plan)),
            ("Stage I", _existing_stage_i_duplicate_log_info(run_dir)),
            ("Koren title-page/editor", _existing_koren_titlepage_editor_info(run_dir)),
            ("Stage J", _existing_stage_j_koren_validation_info(run_dir)),
            ("Stage K", _existing_stage_k_live_koren_parent_info(run_dir)),
            ("Stage L", _existing_stage_l_live_koren_pdf_copy_info(run_dir)),
        ]
        gate_ok = bool(args.allow_db_write) and not bool(args.allow_paid_api) and not bool(args.allow_pdf_copy)
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        for label, info in prior_infos:
            lines.append(f"Existing {label} artifact: {'PASS' if info['ok'] else 'FAIL'} — {info['path']} ({info['detail']})")
        lines.append(f"Live Koren DB-write gate: {'PASS' if args.allow_db_write else 'FAIL'} — --allow-db-write is required")
        lines.append(f"Paid API absent: {'PASS' if not args.allow_paid_api else 'FAIL'} — --allow-paid-api must not be used")
        lines.append(f"PDF copy gate absent: {'PASS' if not args.allow_pdf_copy else 'FAIL'} — file-info attach must not use --allow-pdf-copy")
        if base_ok and plancheck_ok and existing_backup["ok"] and gate_ok and all(info["ok"] for _, info in prior_infos):
            stage_m_fileinfo_summary = run_live_koren_fileinfo_attach_only(plan, run_dir)
            if stage_m_fileinfo_summary.get("post_write_validation", {}).get("ok"):
                lines.append("Stage M live Koren file-info attach only: PASS")
            else:
                lines.append("Stage M live Koren file-info attach only: FAIL")
            lines.append(f"Attach artifact: {run_dir / 'live_koren_fileinfo_attach_only.json'}")
        else:
            lines.append("Stage M live Koren file-info attach only: NOT RUN (preconditions failed)")

    if args.execute_stage == "live-koren-staging-archive-only":
        existing_backup = _existing_pilot_backup_info(run_dir)
        prior_infos = [
            ("Stage D", _existing_stage_d_preview_info(run_dir, plan)),
            ("Stage E", _existing_stage_e_preview_info(run_dir, plan)),
            ("Stage F", _existing_stage_f_preview_info(run_dir, plan)),
            ("Stage G", _existing_stage_g_preview_info(run_dir)),
            ("Stage H", _existing_stage_h_preview_info(run_dir, plan)),
            ("Stage I", _existing_stage_i_duplicate_log_info(run_dir)),
            ("Koren title-page/editor", _existing_koren_titlepage_editor_info(run_dir)),
            ("Stage J", _existing_stage_j_koren_validation_info(run_dir)),
            ("Stage K", _existing_stage_k_live_koren_parent_info(run_dir)),
            ("Stage L", _existing_stage_l_live_koren_pdf_copy_info(run_dir)),
            ("Stage M", _existing_stage_m_live_koren_fileinfo_info(run_dir)),
        ]
        archive_gate = bool(getattr(args, "allow_staging_archive", False))
        gate_ok = archive_gate and not bool(args.allow_paid_api) and not bool(args.allow_db_write) and not bool(args.allow_pdf_copy)
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        for label, info in prior_infos:
            lines.append(f"Existing {label} artifact: {'PASS' if info['ok'] else 'FAIL'} — {info['path']} ({info['detail']})")
        lines.append(f"Live Koren staging archive gate: {'PASS' if archive_gate else 'FAIL'} — --allow-staging-archive is required")
        lines.append(f"Paid API absent: {'PASS' if not args.allow_paid_api else 'FAIL'} — --allow-paid-api must not be used")
        lines.append(f"DB write gate absent: {'PASS' if not args.allow_db_write else 'FAIL'} — staging archive must not use --allow-db-write")
        lines.append(f"PDF copy gate absent: {'PASS' if not args.allow_pdf_copy else 'FAIL'} — staging archive must not use --allow-pdf-copy")
        if base_ok and plancheck_ok and existing_backup["ok"] and gate_ok and all(info["ok"] for _, info in prior_infos):
            stage_n_archive_summary = run_live_koren_staging_archive_only(plan, run_dir)
            if stage_n_archive_summary.get("post_archive_validation", {}).get("ok"):
                lines.append("Stage N live Koren staging archive only: PASS")
            else:
                lines.append("Stage N live Koren staging archive only: FAIL")
            lines.append(f"Archive artifact: {run_dir / 'live_koren_staging_archive_only.json'}")
        else:
            lines.append("Stage N live Koren staging archive only: NOT RUN (preconditions failed)")

    if args.execute_stage == "harris-edited-volume-validation-preview":
        existing_backup = _existing_pilot_backup_info(run_dir)
        prior_infos = [
            ("Stage D", _existing_stage_d_preview_info(run_dir, plan)),
            ("Stage E", _existing_stage_e_preview_info(run_dir, plan)),
            ("Stage F", _existing_stage_f_preview_info(run_dir, plan)),
            ("Stage G", _existing_stage_g_preview_info(run_dir)),
        ]
        gate_ok = (
            not bool(args.allow_paid_api)
            and not bool(args.allow_db_write)
            and not bool(args.allow_pdf_copy)
            and not bool(getattr(args, "allow_staging_archive", False))
        )
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        for label, info in prior_infos:
            lines.append(f"Existing {label} artifact: {'PASS' if info['ok'] else 'FAIL'} — {info['path']} ({info['detail']})")
        lines.append(f"Paid API absent: {'PASS' if not args.allow_paid_api else 'FAIL'} — --allow-paid-api must not be used")
        lines.append(f"DB write gate absent: {'PASS' if not args.allow_db_write else 'FAIL'} — --allow-db-write must not be used")
        lines.append(f"PDF copy gate absent: {'PASS' if not args.allow_pdf_copy else 'FAIL'} — --allow-pdf-copy must not be used")
        lines.append(
            f"Staging archive gate absent: {'PASS' if not getattr(args, 'allow_staging_archive', False) else 'FAIL'} — "
            "--allow-staging-archive must not be used"
        )
        if base_ok and plancheck_ok and existing_backup["ok"] and gate_ok and all(info["ok"] for _, info in prior_infos):
            stage_o_harris_summary = run_harris_edited_volume_validation_preview(plan, run_dir)
            lines.append("Stage O Harris edited-volume validation preview: PASS")
            lines.append(f"Preview artifact: {run_dir / 'harris_edited_volume_validation_preview.json'}")
        else:
            lines.append("Stage O Harris edited-volume validation preview: NOT RUN (preconditions failed)")

    if args.execute_stage == "live-harris-parent-record-only":
        existing_backup = _existing_pilot_backup_info(run_dir)
        prior_infos = [
            ("Stage D", _existing_stage_d_preview_info(run_dir, plan)),
            ("Stage E", _existing_stage_e_preview_info(run_dir, plan)),
            ("Stage F", _existing_stage_f_preview_info(run_dir, plan)),
            ("Stage G", _existing_stage_g_preview_info(run_dir)),
            ("Stage O", _existing_stage_o_harris_validation_info(run_dir)),
        ]
        gate_ok = bool(args.allow_db_write) and not bool(args.allow_paid_api) and not bool(args.allow_pdf_copy) and not bool(getattr(args, "allow_staging_archive", False))
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        for label, info in prior_infos:
            lines.append(f"Existing {label} artifact: {'PASS' if info['ok'] else 'FAIL'} — {info['path']} ({info['detail']})")
        lines.append(f"Live Harris DB-write gate: {'PASS' if args.allow_db_write else 'FAIL'} — --allow-db-write is required")
        lines.append(f"Paid API absent: {'PASS' if not args.allow_paid_api else 'FAIL'} — --allow-paid-api must not be used")
        lines.append(f"PDF copy gate absent: {'PASS' if not args.allow_pdf_copy else 'FAIL'} — --allow-pdf-copy must not be used")
        lines.append(
            f"Staging archive gate absent: {'PASS' if not getattr(args, 'allow_staging_archive', False) else 'FAIL'} — "
            "--allow-staging-archive must not be used"
        )
        if base_ok and plancheck_ok and existing_backup["ok"] and gate_ok and all(info["ok"] for _, info in prior_infos):
            stage_p_harris_live_summary = run_live_harris_parent_record_only(plan, run_dir)
            if stage_p_harris_live_summary.get("row_created"):
                lines.append("Stage P live Harris parent-record-only: PASS")
            else:
                lines.append("Stage P live Harris parent-record-only: FAIL")
            lines.append(f"Live artifact: {run_dir / 'live_harris_parent_record_only.json'}")
        else:
            lines.append("Stage P live Harris parent-record-only: NOT RUN (preconditions failed)")

    if args.execute_stage == "live-harris-pdf-copy-only":
        existing_backup = _existing_pilot_backup_info(run_dir)
        prior_infos = [
            ("Stage D", _existing_stage_d_preview_info(run_dir, plan)),
            ("Stage E", _existing_stage_e_preview_info(run_dir, plan)),
            ("Stage F", _existing_stage_f_preview_info(run_dir, plan)),
            ("Stage G", _existing_stage_g_preview_info(run_dir)),
            ("Stage O", _existing_stage_o_harris_validation_info(run_dir)),
            ("Stage P", _existing_stage_p_live_harris_parent_info(run_dir)),
        ]
        gate_ok = bool(args.allow_pdf_copy) and not bool(args.allow_paid_api) and not bool(args.allow_db_write) and not bool(getattr(args, "allow_staging_archive", False))
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        for label, info in prior_infos:
            lines.append(f"Existing {label} artifact: {'PASS' if info['ok'] else 'FAIL'} — {info['path']} ({info['detail']})")
        lines.append(f"Live Harris PDF-copy gate: {'PASS' if args.allow_pdf_copy else 'FAIL'} — --allow-pdf-copy is required")
        lines.append(f"Paid API absent: {'PASS' if not args.allow_paid_api else 'FAIL'} — --allow-paid-api must not be used")
        lines.append(f"DB write gate absent: {'PASS' if not args.allow_db_write else 'FAIL'} — copy-only pilot must not use --allow-db-write")
        lines.append(
            f"Staging archive gate absent: {'PASS' if not getattr(args, 'allow_staging_archive', False) else 'FAIL'} — "
            "--allow-staging-archive must not be used"
        )
        if base_ok and plancheck_ok and existing_backup["ok"] and gate_ok and all(info["ok"] for _, info in prior_infos):
            stage_q_harris_copy_summary = run_live_harris_pdf_copy_only(plan, run_dir)
            if stage_q_harris_copy_summary.get("post_copy_validation", {}).get("ok"):
                lines.append("Stage Q live Harris PDF-copy-only: PASS")
            else:
                lines.append("Stage Q live Harris PDF-copy-only: FAIL")
            lines.append(f"Copy artifact: {run_dir / 'live_harris_pdf_copy_only.json'}")
        else:
            lines.append("Stage Q live Harris PDF-copy-only: NOT RUN (preconditions failed)")

    if args.execute_stage == "live-harris-fileinfo-attach-only":
        existing_backup = _existing_pilot_backup_info(run_dir)
        prior_infos = [
            ("Stage D", _existing_stage_d_preview_info(run_dir, plan)),
            ("Stage E", _existing_stage_e_preview_info(run_dir, plan)),
            ("Stage F", _existing_stage_f_preview_info(run_dir, plan)),
            ("Stage G", _existing_stage_g_preview_info(run_dir)),
            ("Stage O", _existing_stage_o_harris_validation_info(run_dir)),
            ("Stage P", _existing_stage_p_live_harris_parent_info(run_dir)),
            ("Stage Q", _existing_stage_q_live_harris_pdf_copy_info(run_dir)),
        ]
        gate_ok = bool(args.allow_db_write) and not bool(args.allow_paid_api) and not bool(args.allow_pdf_copy) and not bool(getattr(args, "allow_staging_archive", False))
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        for label, info in prior_infos:
            lines.append(f"Existing {label} artifact: {'PASS' if info['ok'] else 'FAIL'} — {info['path']} ({info['detail']})")
        lines.append(f"Live Harris DB-write gate: {'PASS' if args.allow_db_write else 'FAIL'} — --allow-db-write is required")
        lines.append(f"Paid API absent: {'PASS' if not args.allow_paid_api else 'FAIL'} — --allow-paid-api must not be used")
        lines.append(f"PDF copy gate absent: {'PASS' if not args.allow_pdf_copy else 'FAIL'} — file-info attach must not use --allow-pdf-copy")
        lines.append(
            f"Staging archive gate absent: {'PASS' if not getattr(args, 'allow_staging_archive', False) else 'FAIL'} — "
            "--allow-staging-archive must not be used"
        )
        if base_ok and plancheck_ok and existing_backup["ok"] and gate_ok and all(info["ok"] for _, info in prior_infos):
            stage_r_harris_fileinfo_summary = run_live_harris_fileinfo_attach_only(plan, run_dir)
            if stage_r_harris_fileinfo_summary.get("post_write_validation", {}).get("ok"):
                lines.append("Stage R live Harris file-info attach only: PASS")
            else:
                lines.append("Stage R live Harris file-info attach only: FAIL")
            lines.append(f"Attach artifact: {run_dir / 'live_harris_fileinfo_attach_only.json'}")
        else:
            lines.append("Stage R live Harris file-info attach only: NOT RUN (preconditions failed)")

    if args.execute_stage == "live-harris-staging-archive-only":
        existing_backup = _existing_pilot_backup_info(run_dir)
        prior_infos = [
            ("Stage D", _existing_stage_d_preview_info(run_dir, plan)),
            ("Stage E", _existing_stage_e_preview_info(run_dir, plan)),
            ("Stage F", _existing_stage_f_preview_info(run_dir, plan)),
            ("Stage G", _existing_stage_g_preview_info(run_dir)),
            ("Stage O", _existing_stage_o_harris_validation_info(run_dir)),
            ("Stage P", _existing_stage_p_live_harris_parent_info(run_dir)),
            ("Stage Q", _existing_stage_q_live_harris_pdf_copy_info(run_dir)),
            ("Stage R", _existing_stage_r_live_harris_fileinfo_info(run_dir)),
        ]
        archive_gate = bool(getattr(args, "allow_staging_archive", False))
        gate_ok = archive_gate and not bool(args.allow_paid_api) and not bool(args.allow_db_write) and not bool(args.allow_pdf_copy)
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        for label, info in prior_infos:
            lines.append(f"Existing {label} artifact: {'PASS' if info['ok'] else 'FAIL'} — {info['path']} ({info['detail']})")
        lines.append(f"Live Harris staging archive gate: {'PASS' if archive_gate else 'FAIL'} — --allow-staging-archive is required")
        lines.append(f"Paid API absent: {'PASS' if not args.allow_paid_api else 'FAIL'} — --allow-paid-api must not be used")
        lines.append(f"DB write gate absent: {'PASS' if not args.allow_db_write else 'FAIL'} — staging archive must not use --allow-db-write")
        lines.append(f"PDF copy gate absent: {'PASS' if not args.allow_pdf_copy else 'FAIL'} — staging archive must not use --allow-pdf-copy")
        if base_ok and plancheck_ok and existing_backup["ok"] and gate_ok and all(info["ok"] for _, info in prior_infos):
            stage_s_harris_archive_summary = run_live_harris_staging_archive_only(plan, run_dir)
            if stage_s_harris_archive_summary.get("post_archive_validation", {}).get("ok"):
                lines.append("Stage S live Harris staging archive only: PASS")
            else:
                lines.append("Stage S live Harris staging archive only: FAIL")
            lines.append(f"Archive artifact: {run_dir / 'live_harris_staging_archive_only.json'}")
        else:
            lines.append("Stage S live Harris staging archive only: NOT RUN (preconditions failed)")

    if args.execute_stage == "final-pilot-report-preview":
        existing_backup = _existing_pilot_backup_info(run_dir)
        prior_infos = [
            ("Stage D", _existing_stage_d_preview_info(run_dir, plan)),
            ("Stage E", _existing_stage_e_preview_info(run_dir, plan)),
            ("Stage F", _existing_stage_f_preview_info(run_dir, plan)),
            ("Stage G", _existing_stage_g_preview_info(run_dir)),
            ("Stage H", _existing_stage_h_preview_info(run_dir, plan)),
            ("Stage I", _existing_stage_i_duplicate_log_info(run_dir)),
            ("Stage J", _existing_stage_j_koren_validation_info(run_dir)),
            ("Stage K", _existing_stage_k_live_koren_parent_info(run_dir)),
            ("Stage L", _existing_stage_l_live_koren_pdf_copy_info(run_dir)),
            ("Stage M", _existing_stage_m_live_koren_fileinfo_info(run_dir)),
            ("Stage N", _existing_stage_n_live_koren_archive_info(run_dir)),
            ("Stage O", _existing_stage_o_harris_validation_info(run_dir)),
            ("Stage P", _existing_stage_p_live_harris_parent_info(run_dir)),
            ("Stage Q", _existing_stage_q_live_harris_pdf_copy_info(run_dir)),
            ("Stage R", _existing_stage_r_live_harris_fileinfo_info(run_dir)),
            ("Stage S", _existing_stage_s_live_harris_archive_info(run_dir)),
        ]
        gate_ok = (
            not bool(args.allow_paid_api)
            and not bool(args.allow_db_write)
            and not bool(args.allow_pdf_copy)
            and not getattr(args, "allow_staging_archive", False)
        )
        lines.append(
            f"Existing pilot backup: {'PASS' if existing_backup['ok'] else 'FAIL'} — "
            f"{existing_backup['path']} ({existing_backup['bytes']} bytes)"
        )
        for label, info in prior_infos:
            lines.append(f"Existing {label} artifact: {'PASS' if info['ok'] else 'FAIL'} — {info['path']} ({info['detail']})")
        lines.append(f"Read-only gates: {'PASS' if gate_ok else 'FAIL'} — no --allow-paid-api/db-write/pdf-copy/staging-archive")
        if base_ok and plancheck_ok and existing_backup["ok"] and gate_ok and all(info["ok"] for _, info in prior_infos):
            stage_t_final_report_summary = run_final_pilot_report_preview(plan, run_dir, db_path)
            if stage_t_final_report_summary.get("verification_ok"):
                lines.append("Stage T final pilot report preview: PASS")
            else:
                lines.append("Stage T final pilot report preview: FAIL (verification checks did not all pass)")
            lines.append(f"Report artifact: {run_dir / 'final_pilot_report.json'}")
            lines.append(f"Report text: {run_dir / 'final_pilot_report.txt'}")
        else:
            lines.append("Stage T final pilot report preview: NOT RUN (preconditions failed)")

    stage_report = {
        "ts": datetime.now().isoformat(),
        "stage": args.execute_stage,
        "base_preflight_ok": base_ok,
        "backup": backup_info,
        "plancheck_ok": plancheck_ok,
        "plancheck_rows": plancheck_rows,
        "document_type_risks": doc_risks,
        "stage_d_preview": {
            "ran": stage_d_summary is not None,
            "artifact": str(run_dir / "text_metadata_preview.json") if stage_d_summary else "",
        },
        "stage_e_preview": {
            "ran": stage_e_summary is not None,
            "artifact": str(run_dir / "metadata_duplicate_resolution_preview.json") if stage_e_summary else "",
        },
        "stage_f_preview": {
            "ran": stage_f_summary is not None,
            "artifact": str(run_dir / "final_route_plan_preview.json") if stage_f_summary else "",
        },
        "stage_g_preview": {
            "ran": stage_g_summary is not None,
            "artifact": str(run_dir / "book_toc_chapter_preview.json") if stage_g_summary else "",
        },
        "stage_h_preview": {
            "ran": stage_h_summary is not None,
            "artifact": str(run_dir / "final_pilot_execution_plan.json") if stage_h_summary else "",
        },
        "stage_i_duplicate_skip_log": {
            "ran": stage_i_summary is not None,
            "artifact": str(run_dir / "live_duplicate_skip_log.json") if stage_i_summary else "",
        },
        "stage_koren_titlepage_editor_preview": {
            "ran": stage_koren_titlepage_summary is not None,
            "artifact": str(run_dir / "koren_titlepage_editor_preview.json") if stage_koren_titlepage_summary else "",
        },
        "stage_j_koren_parent_validation": {
            "ran": stage_j_summary is not None,
            "artifact": str(run_dir / "koren_parent_validation_preview.json") if stage_j_summary else "",
        },
        "stage_k_live_koren_parent_record_only": {
            "ran": stage_k_live_summary is not None,
            "artifact": str(run_dir / "live_koren_parent_record_only.json") if stage_k_live_summary else "",
            "row_created": bool(stage_k_live_summary and stage_k_live_summary.get("row_created")),
        },
        "stage_l_live_koren_pdf_copy_only": {
            "ran": stage_l_copy_summary is not None,
            "artifact": str(run_dir / "live_koren_pdf_copy_only.json") if stage_l_copy_summary else "",
            "copy_ok": bool(stage_l_copy_summary and stage_l_copy_summary.get("post_copy_validation", {}).get("ok")),
        },
        "stage_m_live_koren_fileinfo_attach_only": {
            "ran": stage_m_fileinfo_summary is not None,
            "artifact": str(run_dir / "live_koren_fileinfo_attach_only.json") if stage_m_fileinfo_summary else "",
            "attach_ok": bool(stage_m_fileinfo_summary and stage_m_fileinfo_summary.get("post_write_validation", {}).get("ok")),
        },
        "stage_n_live_koren_staging_archive_only": {
            "ran": stage_n_archive_summary is not None,
            "artifact": str(run_dir / "live_koren_staging_archive_only.json") if stage_n_archive_summary else "",
            "archive_ok": bool(stage_n_archive_summary and stage_n_archive_summary.get("post_archive_validation", {}).get("ok")),
        },
        "stage_p_live_harris_parent_record_only": {
            "ran": stage_p_harris_live_summary is not None,
            "artifact": str(run_dir / "live_harris_parent_record_only.json") if stage_p_harris_live_summary else "",
            "row_created": bool(stage_p_harris_live_summary and stage_p_harris_live_summary.get("row_created")),
        },
        "stage_q_live_harris_pdf_copy_only": {
            "ran": stage_q_harris_copy_summary is not None,
            "artifact": str(run_dir / "live_harris_pdf_copy_only.json") if stage_q_harris_copy_summary else "",
            "copy_ok": bool(stage_q_harris_copy_summary and stage_q_harris_copy_summary.get("post_copy_validation", {}).get("ok")),
        },
        "stage_r_live_harris_fileinfo_attach_only": {
            "ran": stage_r_harris_fileinfo_summary is not None,
            "artifact": str(run_dir / "live_harris_fileinfo_attach_only.json") if stage_r_harris_fileinfo_summary else "",
            "attach_ok": bool(stage_r_harris_fileinfo_summary and stage_r_harris_fileinfo_summary.get("post_write_validation", {}).get("ok")),
        },
        "stage_s_live_harris_staging_archive_only": {
            "ran": stage_s_harris_archive_summary is not None,
            "artifact": str(run_dir / "live_harris_staging_archive_only.json") if stage_s_harris_archive_summary else "",
            "archive_ok": bool(stage_s_harris_archive_summary and stage_s_harris_archive_summary.get("post_archive_validation", {}).get("ok")),
        },
        "stage_t_final_pilot_report_preview": {
            "ran": stage_t_final_report_summary is not None,
            "artifact": str(run_dir / "final_pilot_report.json") if stage_t_final_report_summary else "",
            "verification_ok": bool(stage_t_final_report_summary and stage_t_final_report_summary.get("verification_ok")),
        },
        "later_stages_disabled": [
            "live text extraction",
            "live metadata extraction",
            "duplicate resolution writes",
            "safe ID commit outside explicitly gated Harris/Koren parent live stages",
            "DB row creation/update outside explicitly gated Harris/Koren live stages",
            "PDF copy/move outside explicitly gated PDF live stages",
            "paid evaluation",
            "batch submit",
            "batch collect/save",
            "duplicate merge",
            "PDF replacement",
            "book/chapter splitting writes",
            "human review folder creation outside the run folder",
            "parent record creation outside explicitly gated Harris/Koren parent live stages",
            "mixed pilot execution",
        ],
        "side_effects": {
            "api_calls": "none",
            "papers_db_writes": (
                "one file_info update on Barrett_2016_Handbook_book"
                if stage_m_fileinfo_summary and stage_m_fileinfo_summary.get("post_write_validation", {}).get("ok")
                else
                "one file_info update on Reuter_2016_Neuroeconomics_edited"
                if stage_r_harris_fileinfo_summary and stage_r_harris_fileinfo_summary.get("post_write_validation", {}).get("ok")
                else
                "one new Harris parent row inserted"
                if stage_p_harris_live_summary and stage_p_harris_live_summary.get("row_created")
                else
                "one new Koren parent row inserted"
                if stage_k_live_summary and stage_k_live_summary.get("row_created")
                else "none"
            ),
            "pdf_moves_copies_renames_deletes": (
                f"one Koren staged PDF archived to {stage_n_archive_summary.get('archive_destination')}"
                if stage_n_archive_summary and stage_n_archive_summary.get("post_archive_validation", {}).get("ok")
                else
                f"one Harris staged PDF archived to {stage_s_harris_archive_summary.get('archive_destination')}"
                if stage_s_harris_archive_summary and stage_s_harris_archive_summary.get("post_archive_validation", {}).get("ok")
                else
                f"one Harris PDF copied to {stage_q_harris_copy_summary.get('managed_pdf_destination')}"
                if stage_q_harris_copy_summary and stage_q_harris_copy_summary.get("copied")
                else
                f"one Koren PDF copied to {stage_l_copy_summary.get('managed_pdf_destination')}"
                if stage_l_copy_summary and stage_l_copy_summary.get("copied")
                else "none"
            ),
            "id_changes": (
                "assigned single new Harris parent row ID only"
                if stage_p_harris_live_summary and stage_p_harris_live_summary.get("row_created")
                else
                "assigned single new Koren parent row ID only"
                if stage_k_live_summary and stage_k_live_summary.get("row_created")
                else "none"
            ),
                "backup_created": bool(backup_info and backup_info.get("ok")),
                "live_stage_backup": (
                    stage_m_fileinfo_summary.get("backup")
                    if stage_m_fileinfo_summary and stage_m_fileinfo_summary.get("backup")
                    else
                    stage_r_harris_fileinfo_summary.get("backup")
                    if stage_r_harris_fileinfo_summary and stage_r_harris_fileinfo_summary.get("backup")
                    else
                    stage_p_harris_live_summary.get("backup")
                    if stage_p_harris_live_summary and stage_p_harris_live_summary.get("backup")
                    else
                    stage_k_live_summary.get("backup")
                    if stage_k_live_summary and stage_k_live_summary.get("backup")
                    else None
                ),
        },
    }
    (run_dir / f"stage_{args.execute_stage}_report.json").write_text(
        json.dumps(stage_report, indent=2),
        encoding="utf-8",
    )
    (run_dir / f"stage_{args.execute_stage}_report.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    lines.append("\nLater stages remain disabled.")
    lines.extend(f"- {s}" for s in stage_report["later_stages_disabled"])
    if stage_n_archive_summary and stage_n_archive_summary.get("post_archive_validation", {}).get("ok"):
        lines.append("\nNo API calls, DB writes, managed PDF changes, copies, deletes, live ingest extraction, evaluations, ID changes, or child/chapter records were performed.")
    elif stage_s_harris_archive_summary and stage_s_harris_archive_summary.get("post_archive_validation", {}).get("ok"):
        lines.append("\nNo API calls, DB writes, managed PDF changes, copies, deletes, live ingest extraction, evaluations, ID changes, or child/chapter records were performed.")
    elif stage_t_final_report_summary:
        lines.append("\nNo API calls, DB writes, PDF moves/copies/renames/deletes/archive, evaluations, ID changes, or child/chapter records were performed in this final report stage.")
    elif stage_m_fileinfo_summary and stage_m_fileinfo_summary.get("post_write_validation", {}).get("ok"):
        lines.append("\nNo API calls, PDF operations, live ingest extraction, evaluations, ID changes, new rows, or child/chapter records were performed.")
    elif stage_r_harris_fileinfo_summary and stage_r_harris_fileinfo_summary.get("post_write_validation", {}).get("ok"):
        lines.append("\nNo API calls, PDF operations, live ingest extraction, evaluations, ID changes, new rows, or child/chapter records were performed.")
    elif stage_l_copy_summary and stage_l_copy_summary.get("copied"):
        lines.append("\nNo API calls, DB writes, PDF moves/renames/deletes, live ingest extraction, evaluations, or ID changes were performed.")
    elif stage_q_harris_copy_summary and stage_q_harris_copy_summary.get("copied"):
        lines.append("\nNo API calls, DB writes, PDF moves/renames/deletes, live ingest extraction, evaluations, ID changes, or child/chapter records were performed.")
    elif stage_k_live_summary and stage_k_live_summary.get("row_created"):
        lines.append("\nNo API calls, PDF operations, live ingest extraction, evaluations, or non-target ID changes were performed.")
    elif stage_p_harris_live_summary and stage_p_harris_live_summary.get("row_created"):
        lines.append("\nNo API calls, PDF operations, live ingest extraction, evaluations, existing-row updates, or child/chapter records were performed.")
    else:
        lines.append("\nNo API calls, DB writes, PDF operations, live ingest extraction, or ID changes were performed.")

    exit_code = (
        0
        if base_ok
        and (not backup_info or backup_info["ok"])
        and plancheck_ok
        and (args.execute_stage != "text-metadata-preview" or stage_d_summary is not None)
        and (args.execute_stage != "metadata-duplicate-resolution-preview" or stage_e_summary is not None)
        and (args.execute_stage != "final-route-plan-preview" or stage_f_summary is not None)
        and (args.execute_stage != "book-toc-chapter-preview" or stage_g_summary is not None)
        and (args.execute_stage != "final-pilot-execution-plan" or stage_h_summary is not None)
        and (args.execute_stage != "live-duplicate-skip-log-only" or stage_i_summary is not None)
        and (args.execute_stage != "koren-titlepage-editor-preview" or stage_koren_titlepage_summary is not None)
        and (args.execute_stage != "koren-parent-validation-preview" or stage_j_summary is not None)
        and (args.execute_stage != "live-koren-parent-record-only" or bool(stage_k_live_summary and stage_k_live_summary.get("row_created")))
        and (args.execute_stage != "live-koren-pdf-copy-only" or bool(stage_l_copy_summary and stage_l_copy_summary.get("post_copy_validation", {}).get("ok")))
        and (args.execute_stage != "live-koren-fileinfo-attach-only" or bool(stage_m_fileinfo_summary and stage_m_fileinfo_summary.get("post_write_validation", {}).get("ok")))
        and (args.execute_stage != "live-koren-staging-archive-only" or bool(stage_n_archive_summary and stage_n_archive_summary.get("post_archive_validation", {}).get("ok")))
        and (args.execute_stage != "harris-edited-volume-validation-preview" or stage_o_harris_summary is not None)
        and (args.execute_stage != "live-harris-parent-record-only" or bool(stage_p_harris_live_summary and stage_p_harris_live_summary.get("row_created")))
        and (args.execute_stage != "live-harris-pdf-copy-only" or bool(stage_q_harris_copy_summary and stage_q_harris_copy_summary.get("post_copy_validation", {}).get("ok")))
        and (args.execute_stage != "live-harris-fileinfo-attach-only" or bool(stage_r_harris_fileinfo_summary and stage_r_harris_fileinfo_summary.get("post_write_validation", {}).get("ok")))
        and (args.execute_stage != "live-harris-staging-archive-only" or bool(stage_s_harris_archive_summary and stage_s_harris_archive_summary.get("post_archive_validation", {}).get("ok")))
        and (args.execute_stage != "final-pilot-report-preview" or bool(stage_t_final_report_summary and stage_t_final_report_summary.get("verification_ok")))
        else 1
    )
    return exit_code, "\n".join(lines)


def validate_execute_preconditions(args) -> tuple[bool, list[dict[str, Any]], dict[str, Any] | None]:
    """Validate all future-live pilot preconditions in read-only mode."""
    checks: list[dict[str, Any]] = []

    checks.append(_result("--execute present", bool(args.execute), "required for live pilot"))
    explicit_mode = args.mode in {"A", "B", "C"}
    checks.append(_result("--mode explicit", explicit_mode, "must be A, B, or C (not recommended)"))
    max_ok = args.max_papers is not None and 0 < args.max_papers <= 5
    checks.append(_result("--max-papers <= 5", max_ok, f"value={args.max_papers!r}"))

    plan_path = resolve_plan_path(run_id=args.run_id, plan_path=args.plan_path, runs_dir=args.runs_dir)
    checks.append(_result("run plan selected", plan_path is not None, str(plan_path) if plan_path else "missing --run-id/--plan-path"))
    if plan_path is None or not plan_path.exists():
        checks.append(_result("run_plan.json exists", False, str(plan_path) if plan_path else "no path"))
        return False, checks, None

    checks.append(_result("run_plan.json exists", True, str(plan_path)))
    plan = load_run_plan(plan_path)

    checks.append(
        _result(
            "plan mode matches request",
            plan.get("selected_mode") == args.mode,
            f"plan={plan.get('selected_mode')} request={args.mode}",
        )
    )
    checks.append(
        _result(
            "plan paper count matches max",
            plan.get("selected_pdf_count") == args.max_papers,
            f"plan={plan.get('selected_pdf_count')} request={args.max_papers}",
        )
    )

    db_path = Path(args.db_path).resolve() if args.db_path else config.DB_PATH.resolve()
    checks.append(_result("DB path exists or blank mode", db_path.exists() or args.blank_corpus, str(db_path)))
    checks.append(_result("staging path exists", Path(plan["pdf_plans"][0]["path"]).parent.exists(), "from selected PDFs"))

    lock_dir = Path(args.runs_dir) if args.runs_dir else config.RUNS_DIR
    lock_path = lock_dir / ".corpus_pipeline.lock"
    checks.append(_result("no existing run lock", not lock_path.exists(), str(lock_path)))

    harris_preview_stage = args.execute_stage == "harris-edited-volume-validation-preview"
    harris_live_parent_stage = args.execute_stage == "live-harris-parent-record-only"
    harris_pdf_copy_stage = args.execute_stage == "live-harris-pdf-copy-only"
    harris_fileinfo_stage = args.execute_stage == "live-harris-fileinfo-attach-only"
    harris_archive_stage = args.execute_stage == "live-harris-staging-archive-only"
    final_report_stage = args.execute_stage == "final-pilot-report-preview"
    harris_scoped_stage = harris_preview_stage or harris_live_parent_stage or harris_pdf_copy_stage or harris_fileinfo_stage or harris_archive_stage
    if final_report_stage:
        paid_absent = not bool(args.allow_paid_api) and os.environ.get("CORPUS_PIPELINE_ALLOW_PAID_API") != "1"
        checks.append(_result("paid API gate", paid_absent, "--allow-paid-api must be absent for final report preview"))
        checks.append(_result("Anthropic API key present", True, "not required for final report preview"))
        checks.append(_result("DB write gate", not bool(args.allow_db_write), "--allow-db-write must be absent for final report preview"))
        checks.append(_result("PDF copy/move gate", not bool(args.allow_pdf_copy), "--allow-pdf-copy must be absent for final report preview"))
    elif harris_preview_stage:
        paid_absent = not bool(args.allow_paid_api) and os.environ.get("CORPUS_PIPELINE_ALLOW_PAID_API") != "1"
        checks.append(_result("paid API gate", paid_absent, "--allow-paid-api must be absent for Harris read-only preview"))
        checks.append(_result("Anthropic API key present", True, "not required for Harris read-only preview"))
        checks.append(_result("DB write gate", not bool(args.allow_db_write), "--allow-db-write must be absent for Harris read-only preview"))
        checks.append(_result("PDF copy/move gate", not bool(args.allow_pdf_copy), "--allow-pdf-copy must be absent for Harris read-only preview"))
    elif harris_live_parent_stage:
        paid_absent = not bool(args.allow_paid_api) and os.environ.get("CORPUS_PIPELINE_ALLOW_PAID_API") != "1"
        checks.append(_result("paid API gate", paid_absent, "--allow-paid-api must be absent for Harris live parent pilot"))
        checks.append(_result("Anthropic API key present", True, "not required; no API call is allowed in Harris live parent pilot"))
        checks.append(_result("DB write gate", bool(args.allow_db_write), "--allow-db-write required only for Harris parent row insert"))
        checks.append(_result("PDF copy/move gate", not bool(args.allow_pdf_copy), "--allow-pdf-copy must be absent for Harris live parent pilot"))
    elif harris_pdf_copy_stage:
        paid_absent = not bool(args.allow_paid_api) and os.environ.get("CORPUS_PIPELINE_ALLOW_PAID_API") != "1"
        checks.append(_result("paid API gate", paid_absent, "--allow-paid-api must be absent for Harris PDF copy pilot"))
        checks.append(_result("Anthropic API key present", True, "not required; no API call is allowed in Harris PDF copy pilot"))
        checks.append(_result("DB write gate", not bool(args.allow_db_write), "--allow-db-write must be absent for Harris PDF copy pilot"))
        checks.append(_result("PDF copy/move gate", bool(args.allow_pdf_copy), "--allow-pdf-copy required only for Harris managed copy"))
    elif harris_fileinfo_stage:
        paid_absent = not bool(args.allow_paid_api) and os.environ.get("CORPUS_PIPELINE_ALLOW_PAID_API") != "1"
        checks.append(_result("paid API gate", paid_absent, "--allow-paid-api must be absent for Harris file-info attach pilot"))
        checks.append(_result("Anthropic API key present", True, "not required; no API call is allowed in Harris file-info attach pilot"))
        checks.append(_result("DB write gate", bool(args.allow_db_write), "--allow-db-write required only for Harris file_info update"))
        checks.append(_result("PDF copy/move gate", not bool(args.allow_pdf_copy), "--allow-pdf-copy must be absent for Harris file-info attach pilot"))
    elif harris_archive_stage:
        paid_absent = not bool(args.allow_paid_api) and os.environ.get("CORPUS_PIPELINE_ALLOW_PAID_API") != "1"
        checks.append(_result("paid API gate", paid_absent, "--allow-paid-api must be absent for Harris staging archive pilot"))
        checks.append(_result("Anthropic API key present", True, "not required; no API call is allowed in Harris staging archive pilot"))
        checks.append(_result("DB write gate", not bool(args.allow_db_write), "--allow-db-write must be absent for Harris staging archive pilot"))
        checks.append(_result("PDF copy/move gate", not bool(args.allow_pdf_copy) and bool(getattr(args, "allow_staging_archive", False)), "--allow-staging-archive required and --allow-pdf-copy must be absent"))
    else:
        paid_gate = bool(args.allow_paid_api) or os.environ.get("CORPUS_PIPELINE_ALLOW_PAID_API") == "1"
        checks.append(_result("paid API gate", paid_gate, "--allow-paid-api or CORPUS_PIPELINE_ALLOW_PAID_API=1 required"))
        checks.append(_result("Anthropic API key present", bool(os.environ.get("ANTHROPIC_API_KEY")), "checked only because paid gate is required"))

        db_gate = bool(args.allow_db_write)
        checks.append(_result("DB write gate", db_gate, "--allow-db-write required"))

        pdf_gate = bool(args.allow_pdf_copy)
        checks.append(_result("PDF copy/move gate", pdf_gate, "--allow-pdf-copy required"))

    managed_dir = Path(args.managed_pdf_dir) if args.managed_pdf_dir else config.SOURCE_PDFS
    checks.append(_result("managed PDF folder exists", managed_dir.exists(), str(managed_dir)))
    checks.append(_result("review folder exists", Path(args.review_dir or config.REVIEW_DIR).exists(), args.review_dir or str(config.REVIEW_DIR)))

    required_pdf_names = (
        {"Harris-Effect of pegylation on pharmaceuticals-2003-Nature R.pdf"}
        if harris_scoped_stage
        else {pdf.get("name") for pdf in plan.get("pdf_plans", [])}
    )
    all_pdfs_ok = True
    all_hashes_ok = True
    missing_required = []
    already_processed = []
    if final_report_stage:
        duplicate_names = {
            "Levitin-The Psychology of Music- Rhythm and Movement-2017-An.pdf",
            "Suarez‐Roca-Comprehensive Physiology-2022-Comprehensive Phys.pdf",
            "Yücel-Best practices for fNIRS publications-2021-Neurophot_1.pdf",
        }
        archived_names = {
            "Koren-A kinetic study of protein-protein interactions-1976-B.pdf",
            "Harris-Effect of pegylation on pharmaceuticals-2003-Nature R.pdf",
        }
        for pdf in plan.get("pdf_plans", []):
            name = pdf.get("name")
            pdf_path = Path(pdf["path"])
            if name in duplicate_names:
                if not pdf_path.exists():
                    all_pdfs_ok = False
                    missing_required.append(name)
                elif _sha256(pdf_path) != pdf.get("sha256"):
                    all_hashes_ok = False
            elif name in archived_names:
                if pdf_path.exists():
                    all_pdfs_ok = False
                    missing_required.append(f"{name} still in staging (expected archived)")
                archive_path = config.STAGING / "processed" / (
                    "Barrett_2016_Handbook_book" if "Koren" in name else "Reuter_2016_Neuroeconomics_edited"
                ) / name
                if not archive_path.exists():
                    all_pdfs_ok = False
                    missing_required.append(f"{name} archive missing")
                elif _sha256(archive_path) != pdf.get("sha256"):
                    all_hashes_ok = False
            if _db_has_original_filename(name, db_path):
                already_processed.append(name)
    else:
        for pdf in plan.get("pdf_plans", []):
            if pdf.get("name") not in required_pdf_names:
                continue
            pdf_path = Path(pdf["path"])
            if not pdf_path.exists():
                all_pdfs_ok = False
                missing_required.append(pdf["name"])
                continue
            if _sha256(pdf_path) != pdf.get("sha256"):
                all_hashes_ok = False
            if _db_has_original_filename(pdf["name"], db_path):
                already_processed.append(pdf["name"])
    expected_existing_for_stage = {
        "live-koren-pdf-copy-only": ["Koren-A kinetic study of protein-protein interactions-1976-B.pdf"],
        "live-koren-fileinfo-attach-only": ["Koren-A kinetic study of protein-protein interactions-1976-B.pdf"],
        "live-koren-staging-archive-only": ["Koren-A kinetic study of protein-protein interactions-1976-B.pdf"],
        "harris-edited-volume-validation-preview": [],
        "live-harris-pdf-copy-only": ["Harris-Effect of pegylation on pharmaceuticals-2003-Nature R.pdf"],
        "live-harris-fileinfo-attach-only": ["Harris-Effect of pegylation on pharmaceuticals-2003-Nature R.pdf"],
        "live-harris-staging-archive-only": ["Harris-Effect of pegylation on pharmaceuticals-2003-Nature R.pdf"],
        "final-pilot-report-preview": [
            "Koren-A kinetic study of protein-protein interactions-1976-B.pdf",
            "Harris-Effect of pegylation on pharmaceuticals-2003-Nature R.pdf",
        ],
    }.get(args.execute_stage, [])
    already_ok = (
        len(already_processed) == 0
        or bool(args.resume)
        or sorted(already_processed) == sorted(expected_existing_for_stage)
    )

    checks.append(
        _result(
            "selected PDFs still exist",
            all_pdfs_ok,
            "final report: duplicate PDFs present and parent sources archived"
            if final_report_stage and all_pdfs_ok
            else "Harris-only staged PDF check; Koren is expected to have been archived"
            if harris_scoped_stage and all_pdfs_ok
            else f"missing_required={missing_required}",
        )
    )
    checks.append(
        _result(
            "selected PDF hashes unchanged",
            all_hashes_ok,
            "final report: duplicate and archive hashes match run_plan.json"
            if final_report_stage
            else "Harris sha256 matches run_plan.json" if harris_scoped_stage else "sha256 matches run_plan.json",
        )
    )
    checks.append(
        _result(
            "already processed handling",
            already_ok,
            (
                "resume handles skips"
                if args.resume
                else "expected existing parent row for follow-up live stage"
                if expected_existing_for_stage and sorted(already_processed) == sorted(expected_existing_for_stage)
                else f"already_processed={already_processed[:3]}"
            ),
        )
    )

    if final_report_stage:
        checks.append(_result("backup before write", True, "no write is performed in final report preview"))
        checks.append(_result("ledger/idempotency", True, "no paid call is performed in final report preview"))
        checks.append(_result("schema-before-save", True, "final report is read-only consolidation only"))
        checks.append(_result("safe destination", True, "no managed PDF or staging paths are modified"))
    elif harris_preview_stage:
        checks.append(_result("backup before write", True, "no write is performed in Harris read-only preview"))
        checks.append(_result("ledger/idempotency", True, "no paid call is performed in Harris read-only preview"))
        checks.append(_result("schema-before-save", True, "parent row is preview-only and not saved"))
        checks.append(_result("safe destination", True, "managed PDF destination is preview-only and not touched"))
    elif harris_live_parent_stage:
        checks.append(_result("backup before write", True, "live stage creates and verifies fresh PRE-HARRIS-PARENT backup"))
        checks.append(_result("ledger/idempotency", True, "no paid call is performed in Harris live parent pilot"))
        checks.append(_result("schema-before-save", True, "live stage validates the single Harris parent row before INSERT"))
        checks.append(_result("safe destination", True, "managed PDF destination remains preview-only and is not touched"))
    elif harris_pdf_copy_stage:
        checks.append(_result("backup before write", True, "no DB write is performed in Harris PDF copy pilot"))
        checks.append(_result("ledger/idempotency", True, "no paid call is performed in Harris PDF copy pilot"))
        checks.append(_result("schema-before-save", True, "no DB row is saved in Harris PDF copy pilot"))
        checks.append(_result("safe destination", True, "live stage validates managed destination before copy"))
    elif harris_fileinfo_stage:
        checks.append(_result("backup before write", True, "live stage creates and verifies fresh PRE-HARRIS-FILEINFO backup"))
        checks.append(_result("ledger/idempotency", True, "no paid call is performed in Harris file-info attach pilot"))
        checks.append(_result("schema-before-save", True, "live stage updates only the Harris file_info column"))
        checks.append(_result("safe destination", True, "managed PDF destination was validated in Stage Q and rechecked before DB update"))
    elif harris_archive_stage:
        checks.append(_result("backup before write", True, "no DB write is performed in Harris staging archive pilot"))
        checks.append(_result("ledger/idempotency", True, "no paid call is performed in Harris staging archive pilot"))
        checks.append(_result("schema-before-save", True, "no DB row is saved in Harris staging archive pilot"))
        checks.append(_result("safe destination", True, "live stage validates processed archive destination before move"))
    else:
        checks.append(_result("backup before write", False, "future live stage must create and verify backup before writes"))
        checks.append(_result("ledger/idempotency", False, "future live stage must check ledger before every paid call"))
        checks.append(_result("schema-before-save", False, "future live stage must validate row/eval payload before DB save"))
        checks.append(_result("safe destination", False, "future live stage must validate managed destination and prevent overwrite"))
    checks.append(_result("machine-first escalation", True, "planned routes present in run_plan.json"))

    ok = all(c["ok"] for c in checks)
    return ok, checks, plan


def render_execute_preflight(checks: list[dict[str, Any]], plan: dict[str, Any] | None = None) -> str:
    lines = ["Corpus Pipeline Execute Preflight (NO SIDE EFFECTS)", "=" * 50]
    for c in checks:
        mark = "PASS" if c["ok"] else "FAIL"
        lines.append(f"{mark}: {c['check']} — {c['detail']}")
    if plan:
        lines.extend(["", "Stage boundaries (fail-closed until implemented):"])
        lines.extend(f"- {s}" for s in REQUIRED_STAGES)
    lines.extend(
        [
            "",
            "This preflight section performed no API calls, DB writes, PDF copies/moves, or ID changes.",
            "Any later live action must still pass its separately gated stage checks.",
        ]
    )
    return "\n".join(lines)
