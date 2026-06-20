"""Stage T: read-only final pilot report for completed 5-PDF pilot runs.

Consolidates outcomes from Stages D–S without DB writes, PDF operations, or API calls.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

KOREN_NAME = "Koren-A kinetic study of protein-protein interactions-1976-B.pdf"
HARRIS_NAME = "Harris-Effect of pegylation on pharmaceuticals-2003-Nature R.pdf"
LEVITIN_NAME = "Levitin-The Psychology of Music- Rhythm and Movement-2017-An.pdf"
SUAREZ_NAME = "Suarez‐Roca-Comprehensive Physiology-2022-Comprehensive Phys.pdf"
YUCEL_NAME = "Yücel-Best practices for fNIRS publications-2021-Neurophot_1.pdf"

KOREN_ID = "Barrett_2016_Handbook_book"
HARRIS_ID = "Reuter_2016_Neuroeconomics_edited"
DUPLICATE_IDS = ("Levitin2018", "SuarezRoca2021", "Yucel2021")

EXPECTED_ROW_COUNT = 3374

REQUIRED_ARTIFACTS: list[tuple[str, str]] = [
    ("Stage D", "text_metadata_preview.json"),
    ("Stage E", "metadata_duplicate_resolution_preview.json"),
    ("Stage F", "final_route_plan_preview.json"),
    ("Stage G", "book_toc_chapter_preview.json"),
    ("Stage H", "final_pilot_execution_plan.json"),
    ("Stage I", "live_duplicate_skip_log.json"),
    ("Stage J", "koren_parent_validation_preview.json"),
    ("Stage K", "live_koren_parent_record_only.json"),
    ("Stage L", "live_koren_pdf_copy_only.json"),
    ("Stage M", "live_koren_fileinfo_attach_only.json"),
    ("Stage N", "live_koren_staging_archive_only.json"),
    ("Stage O", "harris_edited_volume_validation_preview.json"),
    ("Stage P", "live_harris_parent_record_only.json"),
    ("Stage Q", "live_harris_pdf_copy_only.json"),
    ("Stage R", "live_harris_fileinfo_attach_only.json"),
    ("Stage S", "live_harris_staging_archive_only.json"),
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return val if isinstance(val, dict) else {}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": ok, "detail": detail}


def _verify_prior_artifacts(run_dir: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for label, filename in REQUIRED_ARTIFACTS:
        path = run_dir / filename
        ok = path.exists()
        items.append(_check(label, ok, str(path) if ok else f"missing: {path}"))
    return {
        "ok": all(i["ok"] for i in items),
        "items": items,
    }


def _read_row(conn: sqlite3.Connection, paper_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if not row:
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM papers LIMIT 0").description]
    return dict(zip(cols, row))


def _verify_db(db_path: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    if not db_path.exists():
        return {"ok": False, "checks": [_check("db_exists", False, str(db_path))], "summary": {}}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        checks.append(_check("row_count_3374", count == EXPECTED_ROW_COUNT, f"count={count}"))

        for paper_id in (KOREN_ID, HARRIS_ID):
            row = _read_row(conn, paper_id)
            exists = row is not None
            checks.append(_check(f"row_exists:{paper_id}", exists, "present" if exists else "missing"))
            if not row:
                continue
            file_info = _json_obj(row.get("file_info"))
            classification = _json_obj(row.get("classification"))
            basic = _json_obj(row.get("basic_metadata"))
            checks.append(
                _check(
                    f"file_info_managed_pdf:{paper_id}",
                    bool(file_info.get("managed_pdf_sha256") and file_info.get("managed_pdf_absolute_path")),
                    "managed PDF fields present" if file_info.get("managed_pdf_sha256") else "managed PDF fields missing",
                )
            )
            eval_status = classification.get("evaluation_status", "")
            deferred = "deferred_not_evaluated" in str(eval_status)
            checks.append(
                _check(
                    f"evaluation_deferred:{paper_id}",
                    deferred,
                    eval_status or "missing evaluation_status",
                )
            )
            rating = classification.get("rating")
            checks.append(
                _check(
                    f"no_evaluation_rating:{paper_id}",
                    rating is None,
                    f"rating={rating!r}",
                )
            )
            summary[paper_id] = {
                "document_type": row.get("document_type"),
                "title": basic.get("title"),
                "year": basic.get("year"),
                "evaluation_status": eval_status,
                "managed_pdf_sha256": file_info.get("managed_pdf_sha256"),
                "managed_pdf_path": file_info.get("managed_pdf_absolute_path"),
            }

        child_like = conn.execute(
            """
            SELECT id FROM papers
            WHERE id LIKE ? OR id LIKE ?
            LIMIT 5
            """,
            (f"%{KOREN_ID}%chapter%", f"%{HARRIS_ID}%chapter%"),
        ).fetchall()
        checks.append(
            _check(
                "no_child_chapter_records",
                len(child_like) == 0,
                "none found" if not child_like else f"found={child_like}",
            )
        )

        for dup_id in DUPLICATE_IDS:
            dup_row = _read_row(conn, dup_id)
            checks.append(
                _check(
                    f"duplicate_match_row_exists:{dup_id}",
                    dup_row is not None,
                    "present" if dup_row else "missing",
                )
            )
            if dup_row:
                summary[dup_id] = {"exists": True, "document_type": dup_row.get("document_type")}
    finally:
        conn.close()

    return {"ok": all(c["ok"] for c in checks), "checks": checks, "summary": summary, "row_count": count}


def _verify_pdfs(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    by_name = {p["name"]: p for p in plan.get("pdf_plans", [])}

    koren_archive = config.STAGING / "processed" / KOREN_ID / KOREN_NAME
    harris_archive = config.STAGING / "processed" / HARRIS_ID / HARRIS_NAME
    koren_managed = config.SOURCE_PDFS / f"{KOREN_ID}.pdf"
    harris_managed = config.SOURCE_PDFS / f"{HARRIS_ID}.pdf"

    koren_live = _load_json(run_dir / "live_koren_staging_archive_only.json")
    harris_live = _load_json(run_dir / "live_harris_staging_archive_only.json")
    koren_fileinfo = _load_json(run_dir / "live_koren_fileinfo_attach_only.json")
    harris_fileinfo = _load_json(run_dir / "live_harris_fileinfo_attach_only.json")

    expected_koren_hash = koren_fileinfo.get("file_info_after", {}).get("managed_pdf_sha256") or koren_live.get("managed_pdf_hash")
    expected_harris_hash = harris_fileinfo.get("file_info_after", {}).get("managed_pdf_sha256") or harris_live.get("managed_pdf_hash")

    for name in (LEVITIN_NAME, SUAREZ_NAME, YUCEL_NAME):
        plan_pdf = by_name.get(name, {})
        path = Path(plan_pdf.get("path", config.STAGING / name))
        exists = path.exists()
        checks.append(_check(f"duplicate_staged_exists:{name}", exists, str(path)))
        if exists and plan_pdf.get("sha256"):
            actual = _sha256(path)
            checks.append(
                _check(
                    f"duplicate_staged_hash:{name}",
                    actual == plan_pdf["sha256"],
                    f"expected={plan_pdf['sha256'][:12]}… actual={actual[:12]}…",
                )
            )

    koren_staged = Path(by_name.get(KOREN_NAME, {}).get("path", config.STAGING / KOREN_NAME))
    harris_staged = Path(by_name.get(HARRIS_NAME, {}).get("path", config.STAGING / HARRIS_NAME))
    checks.append(_check("koren_staging_path_cleared", not koren_staged.exists(), str(koren_staged)))
    checks.append(_check("harris_staging_path_cleared", not harris_staged.exists(), str(harris_staged)))

    checks.append(_check("koren_archive_exists", koren_archive.exists(), str(koren_archive)))
    checks.append(_check("harris_archive_exists", harris_archive.exists(), str(harris_archive)))

    if koren_archive.exists() and expected_koren_hash:
        actual = _sha256(koren_archive)
        checks.append(
            _check(
                "koren_archive_hash_matches_file_info",
                actual == expected_koren_hash,
                f"hash={actual[:12]}…",
            )
        )
    if harris_archive.exists() and expected_harris_hash:
        actual = _sha256(harris_archive)
        checks.append(
            _check(
                "harris_archive_hash_matches_file_info",
                actual == expected_harris_hash,
                f"hash={actual[:12]}…",
            )
        )

    checks.append(_check("koren_managed_pdf_exists", koren_managed.exists(), str(koren_managed)))
    checks.append(_check("harris_managed_pdf_exists", harris_managed.exists(), str(harris_managed)))

    if koren_managed.exists() and expected_koren_hash:
        actual = _sha256(koren_managed)
        checks.append(
            _check(
                "koren_managed_hash_matches_file_info",
                actual == expected_koren_hash,
                f"hash={actual[:12]}…",
            )
        )
    if harris_managed.exists() and expected_harris_hash:
        actual = _sha256(harris_managed)
        checks.append(
            _check(
                "harris_managed_hash_matches_file_info",
                actual == expected_harris_hash,
                f"hash={actual[:12]}…",
            )
        )

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "paths": {
            "koren_managed": str(koren_managed),
            "harris_managed": str(harris_managed),
            "koren_archive": str(koren_archive),
            "harris_archive": str(harris_archive),
        },
    }


def _backup_entry(label: str, path_str: str) -> dict[str, Any]:
    path = Path(path_str) if path_str else Path()
    size = path.stat().st_size if path.exists() else 0
    return {
        "label": label,
        "path": path_str,
        "bytes": size,
        "exists": path.exists() and size > 0,
    }


def _summarize_backups(run_dir: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []

    backup_info = run_dir / "stage_backup_info.json"
    if backup_info.exists():
        data = _load_json(backup_info)
        entries.append(_backup_entry("PRE-CORPUS-PILOT", data.get("path", "")))

    artifact_backups = [
        ("PRE-KOREN-PARENT", run_dir / "live_koren_parent_record_only.json", "backup"),
        ("PRE-KOREN-FILEINFO", run_dir / "live_koren_fileinfo_attach_only.json", "backup"),
        ("PRE-HARRIS-PARENT", run_dir / "live_harris_parent_record_only.json", "backup"),
        ("PRE-HARRIS-FILEINFO", run_dir / "live_harris_fileinfo_attach_only.json", "backup"),
    ]
    for label, artifact, key in artifact_backups:
        if artifact.exists():
            data = _load_json(artifact)
            backup = data.get(key) or {}
            entries.append(_backup_entry(label, backup.get("path", "")))

    for label, pattern in (
        ("PRE-CORPUS-PILOT", "*PRE-CORPUS-PILOT.db"),
        ("PRE-KOREN-PARENT", "*PRE-KOREN-PARENT.db"),
        ("PRE-KOREN-FILEINFO", "*PRE-KOREN-FILEINFO.db"),
        ("PRE-HARRIS-PARENT", "*PRE-HARRIS-PARENT.db"),
        ("PRE-HARRIS-FILEINFO", "*PRE-HARRIS-FILEINFO.db"),
    ):
        if any(e["label"] == label and e["exists"] for e in entries):
            continue
        matches = sorted(config.BACKUP_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if matches:
            entries.append(_backup_entry(label, str(matches[0])))

    return {"ok": all(e["exists"] for e in entries), "entries": entries}


def _pdf_outcomes(run_dir: Path) -> list[dict[str, Any]]:
    dup_log = _load_json(run_dir / "live_duplicate_skip_log.json")
    dup_by_name = {d["staged_filename"]: d for d in dup_log.get("accepted_duplicate_skip_decisions", [])}

    return [
        {
            "original_staged_filename": KOREN_NAME,
            "final_route": "book_parent_record_created_managed_pdf_copied_attached_staged_source_archived",
            "db_action": f"insert parent row {KOREN_ID}",
            "pdf_action": "managed copy + staging archive",
            "evaluation_action": "deferred_not_evaluated_for_book_parent",
            "id_action": f"committed {KOREN_ID}",
            "final_status": "completed",
            "artifact_references": [
                "koren_parent_validation_preview.json",
                "live_koren_parent_record_only.json",
                "live_koren_pdf_copy_only.json",
                "live_koren_fileinfo_attach_only.json",
                "live_koren_staging_archive_only.json",
            ],
        },
        {
            "original_staged_filename": HARRIS_NAME,
            "final_route": "edited_volume_parent_record_created_managed_pdf_copied_attached_staged_source_archived",
            "db_action": f"insert parent row {HARRIS_ID}",
            "pdf_action": "managed copy + staging archive",
            "evaluation_action": "deferred_not_evaluated_for_edited_volume_parent",
            "id_action": f"committed {HARRIS_ID}",
            "final_status": "completed",
            "artifact_references": [
                "harris_edited_volume_validation_preview.json",
                "live_harris_parent_record_only.json",
                "live_harris_pdf_copy_only.json",
                "live_harris_fileinfo_attach_only.json",
                "live_harris_staging_archive_only.json",
            ],
        },
        {
            "original_staged_filename": LEVITIN_NAME,
            "final_route": "exact_duplicate_skip_audit_accepted",
            "db_action": "none",
            "pdf_action": "none (staged file retained)",
            "evaluation_action": "none",
            "id_action": "none",
            "final_status": "completed_log_only",
            "matched_existing_db_id": dup_by_name.get(LEVITIN_NAME, {}).get("matched_existing_db_id", "Levitin2018"),
            "artifact_references": [
                "metadata_duplicate_resolution_preview.json",
                "live_duplicate_skip_log.json",
            ],
        },
        {
            "original_staged_filename": SUAREZ_NAME,
            "final_route": "exact_duplicate_skip_audit_accepted",
            "db_action": "none",
            "pdf_action": "none (staged file retained)",
            "evaluation_action": "none",
            "id_action": "none",
            "final_status": "completed_log_only",
            "matched_existing_db_id": dup_by_name.get(SUAREZ_NAME, {}).get("matched_existing_db_id", "SuarezRoca2021"),
            "artifact_references": [
                "metadata_duplicate_resolution_preview.json",
                "live_duplicate_skip_log.json",
            ],
        },
        {
            "original_staged_filename": YUCEL_NAME,
            "final_route": "exact_duplicate_skip_audit_accepted",
            "db_action": "none",
            "pdf_action": "none (staged file retained)",
            "evaluation_action": "none",
            "id_action": "none",
            "final_status": "completed_log_only",
            "matched_existing_db_id": dup_by_name.get(YUCEL_NAME, {}).get("matched_existing_db_id", "Yucel2021"),
            "artifact_references": [
                "metadata_duplicate_resolution_preview.json",
                "live_duplicate_skip_log.json",
            ],
        },
    ]


def _pilot_proved() -> list[str]:
    return [
        "one-command pipeline planning works (prescan, mode selection, run plan)",
        "A/B/C pre-scan and staged run planning exist",
        "duplicate-skip audit path works (Stage I log-only acceptance)",
        "book parent-record creation works (Koren / Barrett_2016_Handbook_book)",
        "edited-volume parent-record creation works (Harris / Reuter_2016_Neuroeconomics_edited)",
        "managed PDF copy works (Stages L and Q)",
        "file_info attachment works (Stages M and R)",
        "staging archive works (Stages N and S)",
        "target-only DB writes and digest checks work",
        "paid API/evaluation remained disabled throughout pilot",
    ]


def _remaining_gaps() -> list[str]:
    return [
        "child/chapter record creation still deferred for Koren and Harris",
        "bulk/broader ingest pilot not yet run",
        "automatic selection of next clean staged PDFs may need testing",
        "evaluation integration for new parent records is still deferred",
        "duplicate archive/move path for exact duplicates may still be pending",
        "general from-scratch adaptive-guideline system is not part of this pipeline yet",
    ]


def _recommended_next_step() -> dict[str, str]:
    return {
        "recommendation": "consolidate_pilot_stages_into_internal_workflow",
        "rationale": (
            "The 5-PDF pilot proved each atomic step, but future users should not run stages A–S manually. "
            "Consolidating into a clean internal orchestration path yields the desired UX: add PDFs to staging, "
            "run pipeline, choose A/B/C, and let the pipeline handle stages internally."
        ),
        "alternative": (
            "After consolidation, run a broader ingest pilot on 5–10 additional staged PDFs using the proven workflow."
        ),
    }


def _render_text(report: dict[str, Any]) -> str:
    lines = [
        "Final Pilot Report Preview",
        "=" * 44,
        f"Run ID: {report.get('run_id')}",
        f"Stage: {report.get('stage')}",
        f"Generated: {report.get('timestamp')}",
        "",
        f"Overall verification: {'PASS' if report.get('verification_ok') else 'FAIL'}",
        "",
        "Per-PDF outcomes",
        "-" * 44,
    ]
    for pdf in report.get("pdf_outcomes", []):
        lines.extend(
            [
                f"\n{pdf['original_staged_filename']}",
                f"  Route:   {pdf['final_route']}",
                f"  DB:      {pdf['db_action']}",
                f"  PDF:     {pdf['pdf_action']}",
                f"  Eval:    {pdf['evaluation_action']}",
                f"  ID:      {pdf['id_action']}",
                f"  Status:  {pdf['final_status']}",
            ]
        )

    lines.extend(["", "Database verification", "-" * 44])
    db = report.get("db_verification", {})
    lines.append(f"Row count: {db.get('row_count', 'unknown')} (expected {EXPECTED_ROW_COUNT})")
    for check in db.get("checks", []):
        mark = "PASS" if check["ok"] else "FAIL"
        lines.append(f"{mark}: {check['check']} — {check['detail']}")

    lines.extend(["", "PDF/file verification", "-" * 44])
    for check in report.get("pdf_verification", {}).get("checks", []):
        mark = "PASS" if check["ok"] else "FAIL"
        lines.append(f"{mark}: {check['check']} — {check['detail']}")

    lines.extend(["", "Backups", "-" * 44])
    for entry in report.get("backup_summary", {}).get("entries", []):
        mark = "OK" if entry.get("exists") else "MISSING"
        lines.append(f"{mark}: {entry['label']} — {entry['path']} ({entry.get('bytes', 0)} bytes)")

    lines.extend(["", "What this pilot proved", "-" * 44])
    for item in report.get("pilot_proved", []):
        lines.append(f"- {item}")

    lines.extend(["", "Remaining gaps", "-" * 44])
    for item in report.get("remaining_gaps", []):
        lines.append(f"- {item}")

    rec = report.get("recommended_next_step", {})
    lines.extend(
        [
            "",
            "Recommended next step",
            "-" * 44,
            rec.get("recommendation", ""),
            rec.get("rationale", ""),
            f"Alternative: {rec.get('alternative', '')}",
            "",
            "Stage T side effects: none",
            "- no API calls",
            "- no papers.db writes",
            "- no PDF moves/copies/renames/deletes/archive",
            "- no ID changes",
            "- no evaluations or batch jobs",
        ]
    )
    return "\n".join(lines) + "\n"


def run_final_pilot_report_preview(plan: dict[str, Any], run_dir: Path, db_path: Path | None = None) -> dict[str, Any]:
    """Build read-only final pilot report; writes artifacts under run_dir only."""
    db = db_path or config.DB_PATH
    prior = _verify_prior_artifacts(run_dir)
    db_verification = _verify_db(db)
    pdf_verification = _verify_pdfs(plan, run_dir)
    backup_summary = _summarize_backups(run_dir)

    verification_ok = (
        prior["ok"]
        and db_verification["ok"]
        and pdf_verification["ok"]
        and backup_summary["ok"]
    )

    report: dict[str, Any] = {
        "run_id": run_dir.name,
        "stage": "final-pilot-report-preview",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "processed_pdf_count": 5,
        "verification_ok": verification_ok,
        "prior_artifacts": prior,
        "pdf_outcomes": _pdf_outcomes(run_dir),
        "db_verification": db_verification,
        "pdf_verification": pdf_verification,
        "backup_summary": backup_summary,
        "pilot_proved": _pilot_proved(),
        "remaining_gaps": _remaining_gaps(),
        "recommended_next_step": _recommended_next_step(),
        "fail_closed_guards": {
            "db_writes_allowed": False,
            "pdf_actions_allowed": False,
            "api_calls_allowed": False,
            "evaluations_allowed": False,
            "id_changes_allowed": False,
        },
        "side_effects": {
            "api_calls": "none",
            "papers_db_writes": "none",
            "pdf_moves_copies_renames_deletes": "none",
            "id_changes": "none",
            "evaluations": "none",
            "batch_jobs": "none",
            "artifacts_written": [
                str(run_dir / "final_pilot_report.json"),
                str(run_dir / "final_pilot_report.txt"),
            ],
        },
    }

    json_path = run_dir / "final_pilot_report.json"
    txt_path = run_dir / "final_pilot_report.txt"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    txt_path.write_text(_render_text(report), encoding="utf-8")
    report["artifact_paths"] = {"json": str(json_path), "txt": str(txt_path)}
    return report
