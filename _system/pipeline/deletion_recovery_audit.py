"""Restore deleted pending child files from git and re-audit by content."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from add_paper import log_error
from path_governance import MAX_FILENAME, safe_destination_for_file

RESTORE_COMMIT = "f5d6851"
DELETION_COMMIT = "c266e97"
PENDING_PREFIX = "pending-parent-child-documents/"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_deleted_pending_files() -> list[str]:
    cmd = [
        "git",
        "diff",
        "--name-only",
        "--diff-filter=D",
        RESTORE_COMMIT,
        DELETION_COMMIT,
        "--",
        PENDING_PREFIX,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(config.BASE))
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "git diff failed")
    names = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith(PENDING_PREFIX):
            continue
        if line.endswith(".meta.json"):
            continue
        names.append(line[len(PENDING_PREFIX) :])
    return sorted(set(names))


def restore_files_to_audit(names: list[str], *, dry_run: bool = False) -> list[Path]:
    audit_dir = config.RECOVERED_DELETION_AUDIT
    restored: list[Path] = []
    if not dry_run:
        audit_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        dest = audit_dir / name
        if dry_run:
            restored.append(dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        git_path = f"{RESTORE_COMMIT}:{PENDING_PREFIX}{name}"
        proc = subprocess.run(
            ["git", "show", git_path],
            capture_output=True,
            cwd=str(config.BASE),
        )
        if proc.returncode != 0:
            continue
        dest.write_bytes(proc.stdout)
        restored.append(dest)
    return restored


def _write_audit_log(entry: dict[str, Any]) -> None:
    config.DELETION_RECOVERY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.DELETION_RECOVERY_LOG_DIR / f"recovery-audit-{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _meta_path(file_path: Path) -> Path:
    return file_path.with_suffix(file_path.suffix + ".meta.json")


def _route_classified_file(
    path: Path,
    classification_result: dict[str, Any],
    *,
    dry_run: bool = False,
    ctx: Any | None = None,
) -> dict[str, Any]:
    from child_document_matching import (
        PENDING_DIR,
        detect_child_document_type,
        ensure_sidecar,
        extract_parent_hints,
        find_parent_for_child,
        link_child_to_parent,
        write_sidecar,
        _delete_corrupt_pending_child,
        _move_pending_to_staging,
    )
    from supplement_content_classifier import disposition_for_classification, is_deletable

    classification = str(classification_result.get("classification") or "")
    disposition = disposition_for_classification(classification)
    action = {"classification": classification, "disposition": disposition, "source": str(path)}

    if is_deletable(classification):
        reason = f"deletion_recovery_audit:{classification.lower()}"
        if dry_run:
            action["action"] = "delete"
            return action
        _delete_corrupt_pending_child(path, reason)
        action["action"] = "deleted"
        _write_audit_log({**action, "reason": reason, "timestamp": _now()})
        return action

    if disposition == "STAGING":
        recovered = (classification_result.get("evidence") or {}).get("pdf", {}).get("recovered_metadata") or {}
        if dry_run:
            action["action"] = "move_staging"
            return action
        dest = _move_pending_to_staging(path, recovered, reason=classification)
        action["action"] = "moved_staging" if dest else "staging_failed"
        action["dest"] = str(dest) if dest else None
        _write_audit_log({**action, "timestamp": _now()})
        return action

    if disposition == "PENDING" or classification == "MODEL_RECOVERY_REQUIRED":
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        dest = safe_destination_for_file(
            path,
            PENDING_DIR,
            reason=f"deletion_recovery:{classification}",
            max_filename=MAX_FILENAME - len(".meta.json"),
        )
        child_type = detect_child_document_type(path.name) or "supplement"
        hints = {**extract_parent_hints(path.name), **(classification_result.get("parent_clues") or {})}
        sniff = classification_result.get("sniff") or {}
        if dry_run:
            action["action"] = "move_pending"
            action["dest"] = str(dest)
            return action
        shutil.move(str(path), str(dest))
        meta = ensure_sidecar(
            dest,
            child_type=child_type,
            original_path=str(path),
            hints=hints,
        )
        meta.update(
            {
                "status": "model_recovery_required" if classification == "MODEL_RECOVERY_REQUIRED" else "pending_parent",
                "recovered_from_git": True,
                "recovered_from_commit": RESTORE_COMMIT,
                "reason": "restored_valid_pdf_child_document_deleted_without_audit"
                if classification == "VALID_PDF_CHILD_DOCUMENT"
                else f"restored_{classification.lower()}",
                "content_classification": classification,
                "file_type": path.suffix.lstrip(".") or sniff.get("detected_kind"),
                "actual_content_type": sniff.get("detected_kind"),
                "classification_evidence": classification_result.get("evidence"),
            }
        )
        write_sidecar(dest, meta)
        if ctx and classification != "MODEL_RECOVERY_REQUIRED":
            parent = find_parent_for_child(dest.name, ctx)
            if parent and link_child_to_parent(dest, parent, child_type=child_type):
                action["linked_parent"] = parent.get("id")
        action["action"] = "moved_pending"
        action["dest"] = str(dest)
        _write_audit_log({**action, "timestamp": _now()})
        return action

    action["action"] = "kept_audit"
    return action


def run_recovery_audit(
    *,
    restore: bool = True,
    execute: bool = False,
    allow_paid_api: bool = False,
    allow_opus: bool = False,
) -> dict[str, Any]:
    from child_document_matching import ChildDocumentRunContext
    from supplement_content_classifier import classify_supplement_file

    dry_run = not execute
    summary: dict[str, Any] = {
        "restore_commit": RESTORE_COMMIT,
        "deletion_commit": DELETION_COMMIT,
        "dry_run": dry_run,
        "restored_from_git_count": 0,
        "valid_pdf_child_restored": 0,
        "valid_pdf_moved_pending": 0,
        "valid_pdf_moved_staging": 0,
        "nonpdf_inspected": 0,
        "nonpdf_kept": 0,
        "nonpdf_deleted_junk": 0,
        "model_recovery_required": 0,
        "confirmed_duplicates_deleted": 0,
        "corrupt_deleted": 0,
        "paid_api_calls": 0,
        "sonnet_calls": 0,
        "opus_calls": 0,
        "actions": [],
    }

    names = list_deleted_pending_files()
    if restore:
        restored = restore_files_to_audit(names, dry_run=dry_run)
        summary["restored_from_git_count"] = len(restored)
        audit_files = restored
    else:
        audit_dir = config.RECOVERED_DELETION_AUDIT
        audit_files = [p for p in audit_dir.rglob("*") if p.is_file() and not p.name.endswith(".meta.json")]

    ctx = ChildDocumentRunContext()
    for path in sorted(audit_files, key=lambda p: p.name):
        if not path.exists():
            continue
        result = classify_supplement_file(
            path,
            allow_paid_api=allow_paid_api,
            allow_opus=allow_opus,
            owned_canonical_only=True,
        )
        classification = result.get("classification")
        sniff = result.get("sniff") or {}
        if sniff.get("detected_kind") != "pdf":
            summary["nonpdf_inspected"] += 1

        action = _route_classified_file(path, result, dry_run=dry_run, ctx=ctx)
        summary["actions"].append(action)

        if classification == "VALID_PDF_CHILD_DOCUMENT":
            summary["valid_pdf_child_restored"] += 1
            if action.get("action") == "moved_pending":
                summary["valid_pdf_moved_pending"] += 1
        if classification in {"VALID_PDF_STANDALONE_RESEARCH", "VALID_PDF_NON_RATABLE_REFERENCE"}:
            if action.get("action") == "moved_staging":
                summary["valid_pdf_moved_staging"] += 1
        if classification == "MODEL_RECOVERY_REQUIRED":
            summary["model_recovery_required"] += 1
        if classification in {
            "VALID_NONPDF_CHILD_DOCUMENT",
            "VALID_NONPDF_DATASET_OR_MEDIA_SUPPLEMENT",
            "HTML_OR_TEXT_CHILD_DOCUMENT",
            "MIME_OR_WRAPPER_RECOVERABLE",
            "ARCHIVE_CHILD_DOCUMENT",
        }:
            summary["nonpdf_kept"] += 1
        if classification in {"HTML_OR_TEXT_JUNK", "MIME_OR_WRAPPER_JUNK"}:
            summary["nonpdf_deleted_junk"] += 1
        if classification == "CONFIRMED_DUPLICATE":
            summary["confirmed_duplicates_deleted"] += 1
        if classification == "TRUE_CORRUPT_UNUSABLE":
            summary["corrupt_deleted"] += 1

    from child_document_matching import PENDING_DIR

    pending_files = [
        p for p in PENDING_DIR.glob("*") if p.is_file() and not p.name.endswith(".meta.json")
    ] if PENDING_DIR.exists() else []
    summary["pending_parent_final_count"] = len(pending_files)
    summary["nonpdf_child_retained"] = sum(
        1
        for p in pending_files
        if p.suffix.lower() != ".pdf"
        or (read_sidecar := __import__("child_document_matching", fromlist=["read_sidecar"]).read_sidecar(p)).get("actual_content_type") not in {None, "pdf"}
    )

    report_path = config.DELETION_RECOVERY_LOG_DIR / "recovery-summary.json"
    if execute:
        config.DELETION_RECOVERY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore and re-audit deleted pending child documents")
    parser.add_argument("--restore-only", action="store_true", help="Only restore files to recovered-deletion-audit/")
    parser.add_argument("--execute", action="store_true", help="Route classified files (default is dry-run)")
    parser.add_argument("--no-restore", action="store_true", help="Audit files already in recovered-deletion-audit/")
    parser.add_argument("--allow-paid-api", action="store_true")
    parser.add_argument("--allow-opus-resolution", action="store_true")
    args = parser.parse_args(argv)
    config.ensure_dirs()

    if args.restore_only:
        names = list_deleted_pending_files()
        restored = restore_files_to_audit(names, dry_run=False)
        print(json.dumps({"restored_count": len(restored), "names": [p.name for p in restored]}, indent=2))
        return 0

    summary = run_recovery_audit(
        restore=not args.no_restore,
        execute=args.execute,
        allow_paid_api=args.allow_paid_api,
        allow_opus=args.allow_opus_resolution and args.allow_paid_api,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
