"""User-facing rejected intake: papers-rejected/ with per-file retention and INTAKE_LAST_RUN.md."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config

REJECTED_ACTIONS = frozenset(
    {
        "DELETE_CONFIRMED_JUNK",
        "DELETE_TRUE_CORRUPT_UNUSABLE",
        "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION",
    }
)

STATUS_TO_USER_REASON: dict[str, str] = {
    "deleted_junk": "Not a research paper (receipt, payslip, or similar non-research document).",
    "deleted_corrupt": "PDF is corrupt or could not be read reliably.",
    "deleted_unrecoverable": "Could not be added to the corpus after full recovery attempts.",
    "moved_to_rejected": "Could not be added to the corpus (see details in the run log).",
    "failed": "Processing failed before the paper could be added.",
    "system_integrity_failure": "A pipeline or database error blocked intake (not a judgment on the paper).",
    "rejected_not_added": "Could not be added to the corpus.",
}

_RUN_REJECTED: list[dict[str, Any]] = []
_CURRENT_RUN_ID: str = ""


def set_current_run_id(run_id: str) -> None:
    global _CURRENT_RUN_ID
    _CURRENT_RUN_ID = str(run_id or "")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def clear_run_rejected() -> None:
    _RUN_REJECTED.clear()


def run_rejected_records() -> list[dict[str, Any]]:
    return list(_RUN_REJECTED)


def rejected_readme_text() -> str:
    days = int(getattr(config, "REJECTED_RETENTION_DAYS", 14))
    folder = getattr(config, "PAPERS_REJECTED", config.PROJECT_ROOT / "papers-rejected")
    return (
        f"# papers-rejected\n\n"
        f"PDFs that were **not added** to the corpus are moved here (not left in `papers-staging/`).\n\n"
        f"- Each file is removed **{days} days after it was moved here** (per file, not as a batch).\n"
        f"- A `{folder.name}/<name>.why.md` file explains why in plain language.\n"
        f"- **Duplicates** of papers already in the corpus are deleted from staging only — "
        f"they do not appear here.\n\n"
        f"After each run, open **`INTAKE_LAST_RUN.md`** at the package root for a summary.\n"
    )


def ensure_rejected_layout() -> None:
    config.PAPERS_REJECTED.mkdir(parents=True, exist_ok=True)
    readme = config.PAPERS_REJECTED / "README.md"
    text = rejected_readme_text()
    if not readme.exists() or readme.read_text(encoding="utf-8") != text:
        readme.write_text(text, encoding="utf-8")


def purge_expired_rejected() -> list[str]:
    """Delete rejected PDFs whose per-file retention period has passed."""
    ensure_rejected_layout()
    removed: list[str] = []
    days = int(getattr(config, "REJECTED_RETENTION_DAYS", 14))
    cutoff_delta = timedelta(days=days)
    now = _utc_now()
    for meta_path in sorted(config.PAPERS_REJECTED.glob("*.rejected.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        moved_raw = str(meta.get("moved_at") or "")
        try:
            moved_at = datetime.fromisoformat(moved_raw.replace("Z", "+00:00"))
            if moved_at.tzinfo is None:
                moved_at = moved_at.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if now < moved_at + cutoff_delta:
            continue
        filename = str(meta.get("filename") or meta_path.name.replace(".rejected.json", ".pdf"))
        pdf_path = config.PAPERS_REJECTED / filename
        stem = Path(filename).stem
        why_path = config.PAPERS_REJECTED / f"{stem}.why.md"
        for p in (pdf_path, meta_path, why_path):
            try:
                if p.exists():
                    p.unlink()
                    removed.append(p.name)
            except OSError:
                pass
    return removed


def _unique_rejected_dest(original_name: str) -> Path:
    base = Path(original_name).name
    dest = config.PAPERS_REJECTED / base
    if not dest.exists():
        return dest
    stem = Path(base).stem
    suffix = Path(base).suffix or ".pdf"
    stamp = _utc_now().strftime("%Y%m%d_%H%M%S")
    return config.PAPERS_REJECTED / f"{stem}_{stamp}{suffix}"


def user_reason_from_item(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "")
    if item.get("user_reason"):
        return str(item["user_reason"])
    adj = item.get("adjudication") or {}
    llm = adj.get("llm_adjudication") if isinstance(adj, dict) else {}
    if isinstance(llm, dict):
        son = llm.get("sonnet") or {}
        if isinstance(son, dict) and son.get("reason"):
            return str(son["reason"])[:500]
    reason = str(item.get("reason") or "")
    if reason.startswith("coverage_block:"):
        return "Not enough readable text or metadata to treat as a research paper."
    if reason.startswith("identity:") or "insufficient_identity" in reason:
        return "Could not identify author/title/year reliably enough to add."
    if STATUS_TO_USER_REASON.get(status):
        return STATUS_TO_USER_REASON[status]
    if reason:
        return reason[:500]
    return STATUS_TO_USER_REASON["rejected_not_added"]


def user_reason_for_delete_action(action: str, reason: str, evidence: dict[str, Any] | None = None) -> str:
    action = str(action or "")
    reason = str(reason or "")
    evidence = evidence or {}
    llm = evidence.get("llm_adjudication") or {}
    if isinstance(llm, dict):
        son = llm.get("sonnet") or {}
        if isinstance(son, dict) and son.get("reason"):
            return str(son["reason"])[:500]
    if action == "DELETE_CONFIRMED_JUNK":
        return STATUS_TO_USER_REASON["deleted_junk"]
    if action == "DELETE_TRUE_CORRUPT_UNUSABLE":
        return STATUS_TO_USER_REASON["deleted_corrupt"]
    if action == "DELETE_UNRECOVERABLE_AFTER_FULL_ADJUDICATION":
        if reason.startswith("staging_reject:"):
            return f"Not added: {reason.split(':', 1)[-1].replace('_', ' ')}."
        return STATUS_TO_USER_REASON["deleted_unrecoverable"]
    if "junk" in reason.lower() or "receipt" in reason.lower():
        return STATUS_TO_USER_REASON["deleted_junk"]
    if "corrupt" in reason.lower():
        return STATUS_TO_USER_REASON["deleted_corrupt"]
    return STATUS_TO_USER_REASON["rejected_not_added"]


def _write_sidecars(
    dest_pdf: Path,
    *,
    original_name: str,
    status: str,
    user_reason: str,
    technical_reason: str,
    run_id: str = "",
    moved_at: datetime | None = None,
) -> dict[str, Any]:
    moved_at = moved_at or _utc_now()
    days = int(getattr(config, "REJECTED_RETENTION_DAYS", 14))
    purge_after = moved_at + timedelta(days=days)
    stem = dest_pdf.stem
    meta_path = config.PAPERS_REJECTED / f"{stem}.rejected.json"
    why_path = config.PAPERS_REJECTED / f"{stem}.why.md"
    meta = {
        "filename": dest_pdf.name,
        "original_filename": original_name,
        "moved_at": _iso(moved_at),
        "purge_after": _iso(purge_after),
        "retention_days": days,
        "status": status,
        "technical_reason": technical_reason,
        "user_reason": user_reason,
        "run_id": run_id,
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    why_path.write_text(
        "\n".join(
            [
                f"# {original_name} — not added to corpus",
                "",
                user_reason,
                "",
                f"**Folder:** `{config.PAPERS_REJECTED.name}/`",
                f"**File:** `{dest_pdf.name}`",
                f"**Moved here:** {meta['moved_at'][:10]}",
                f"**Auto-removed after:** {meta['purge_after'][:10]} "
                f"({days} days after this file was moved here — each file on its own schedule).",
                "",
                "If you believe this is a good research PDF, download a fresh copy from the publisher "
                "or re-scan, then put the new PDF in `papers-staging/` and run again.",
                "",
                f"*Technical: {technical_reason or status}*",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "rejected_path": str(dest_pdf),
        "rejected_filename": dest_pdf.name,
        "purge_after": meta["purge_after"],
        "user_reason": user_reason,
    }


def archive_before_delete(
    path: Path | str,
    *,
    action: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
    run_id: str = "",
) -> dict[str, Any] | None:
    """Move staging (or other) PDF to papers-rejected before disposition deletes it."""
    path = Path(path)
    run_id = run_id or _CURRENT_RUN_ID
    if str(action or "") == "DELETE_CONFIRMED_DUPLICATE":
        return None
    if action not in REJECTED_ACTIONS and action != "REJECT_ARCHIVE":
        return None
    if not path.exists() or not path.is_file():
        return None
    try:
        if path.resolve().parent != config.STAGING.resolve():
            return None
    except OSError:
        return None
    ensure_rejected_layout()
    original_name = path.name
    dest = _unique_rejected_dest(original_name)
    try:
        shutil.move(str(path), str(dest))
    except OSError:
        return None
    from staging_metadata import remove_internal_meta

    remove_internal_meta(path)
    user_reason = user_reason_for_delete_action(action, reason, evidence)
    status = (
        "deleted_junk"
        if action == "DELETE_CONFIRMED_JUNK"
        else "deleted_corrupt"
        if action == "DELETE_TRUE_CORRUPT_UNUSABLE"
        else "deleted_unrecoverable"
    )
    record = _write_sidecars(
        dest,
        original_name=original_name,
        status=status,
        user_reason=user_reason,
        technical_reason=f"{action}:{reason}",
        run_id=run_id,
    )
    record.update(status=status, filename=original_name)
    _RUN_REJECTED.append(record)
    return record


def move_file_to_rejected(
    path: Path | str,
    *,
    status: str,
    reason: str,
    user_reason: str | None = None,
    run_id: str = "",
    original_name: str | None = None,
) -> dict[str, Any] | None:
    """Move any file into papers-rejected/ (staging, pending-child, etc.)."""
    path = Path(path)
    if not path.exists():
        return None
    ensure_rejected_layout()
    original_name = original_name or path.name
    dest = _unique_rejected_dest(original_name)
    try:
        shutil.move(str(path), str(dest))
    except OSError:
        return None
    from staging_metadata import remove_internal_meta

    remove_internal_meta(path)
    meta_sidecar = path.parent / f"{path.name}.meta.json"
    if meta_sidecar.exists():
        try:
            meta_sidecar.unlink()
        except OSError:
            pass
    ur = user_reason or STATUS_TO_USER_REASON.get(status) or reason
    record = _write_sidecars(
        dest,
        original_name=original_name,
        status=status,
        user_reason=ur,
        technical_reason=reason,
        run_id=run_id,
    )
    record.update(status=status, filename=original_name)
    _RUN_REJECTED.append(record)
    return record


def archive_staging_file(
    path: Path | str,
    *,
    status: str,
    reason: str,
    user_reason: str | None = None,
    run_id: str = "",
) -> dict[str, Any] | None:
    """Move a file from papers-staging into papers-rejected (not added to corpus)."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        if path.resolve().parent != config.STAGING.resolve():
            return None
    except OSError:
        return None
    return move_file_to_rejected(
        path,
        status=status,
        reason=reason,
        user_reason=user_reason,
        run_id=run_id,
    )


def sweep_remaining_staging_to_rejected(*, run_id: str = "") -> list[dict[str, Any]]:
    """Any PDF still in papers-staging after a run was not successfully ingested."""
    from file_disposition_engine import list_staging_candidates

    moved: list[dict[str, Any]] = []
    for pdf_path in list_staging_candidates():
        rec = archive_staging_file(
            pdf_path,
            status="moved_to_rejected",
            reason="remaining_in_staging_after_run",
            user_reason=(
                "Still in the staging folder after the run finished — not added to the corpus. "
                "See the latest run log for details."
            ),
            run_id=run_id,
        )
        if rec:
            moved.append(rec)
    return moved


def _format_added_line(item: dict[str, Any]) -> str:
    name = str(item.get("filename") or "?")
    pid = str(item.get("paper_id") or "—")
    rating = item.get("classification_rating") or item.get("rating") or ""
    extra = f" ({rating})" if rating else ""
    return f"- `{name}` → **{pid}**{extra}"


def _format_duplicate_line(item: dict[str, Any]) -> str:
    name = str(item.get("filename") or "?")
    match = item.get("duplicate_of") or item.get("paper_id") or ""
    block = item.get("acceptance_block") or {}
    if not match and isinstance(block, dict):
        match = block.get("existing_id") or ""
    if match:
        return f"- `{name}` → already in corpus as **{match}** (staging copy removed)"
    return f"- `{name}` → already in corpus (staging copy removed)"


def _format_rejected_line(rec: dict[str, Any]) -> str:
    name = str(rec.get("filename") or rec.get("original_filename") or rec.get("rejected_filename") or "?")
    dest = str(rec.get("rejected_filename") or Path(str(rec.get("rejected_path") or "")).name)
    purge = str(rec.get("purge_after") or "")[:10]
    reason = str(rec.get("user_reason") or STATUS_TO_USER_REASON["rejected_not_added"])
    return (
        f"- `{name}` → `papers-rejected/{dest}`\n"
        f"  - {reason}\n"
        f"  - Auto-removed after: **{purge or '?'}** (14 days after moved to rejected)"
    )


def pending_child_display_names() -> list[str]:
    """User-facing filenames currently in pending-parent-child-documents/."""
    return _pending_child_display_names()


def _pending_child_display_names() -> list[str]:
    """User-facing filenames currently in pending-parent-child-documents/."""
    pending = config.PENDING_PARENT_CHILD_DOCUMENTS
    names: list[str] = []
    if not pending.exists():
        return names
    for data_path in sorted(pending.iterdir()):
        if not data_path.is_file() or data_path.name.endswith(".meta.json"):
            continue
        label = data_path.name
        meta_path = pending / f"{data_path.name}.meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                label = Path(str(meta.get("original_path") or label)).name
            except (OSError, json.JSONDecodeError):
                pass
        if label not in names:
            names.append(label)
    return names


def _format_startup_cleanup_lines(summary: dict[str, Any]) -> list[str]:
    cleaned = summary.get("startup_cleanup_incomplete_rows") or []
    if not cleaned:
        return []
    lines = [
        "",
        "## Startup repair (before this intake)",
        "",
        "These half-finished rows from an earlier run were removed so intake could start clean:",
        "",
    ]
    for row in cleaned:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("paper_id") or "?")
        reason = str(row.get("reason") or "incomplete")
        lines.append(f"- `{pid}` — {reason}")
    return lines


def build_intake_summary_markdown(summary: dict[str, Any]) -> str:
    items = [i for i in (summary.get("items") or []) if isinstance(i, dict)]
    added: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    pending_child: list[dict[str, Any]] = []
    seen_dup: set[str] = set()
    for item in items:
        st = str(item.get("status") or "")
        fn = str(item.get("filename") or "")
        if st == "success":
            added.append(item)
        elif st == "confirmed_duplicate_deleted" and fn not in seen_dup:
            duplicates.append(item)
            seen_dup.add(fn)
        elif st in {"child_document_pending_parent", "supplement_pending_parent"}:
            pending_child.append(item)

    rejected_records = run_rejected_records()
    if summary.get("sweep_rejected"):
        for rec in summary["sweep_rejected"]:
            if isinstance(rec, dict) and rec not in rejected_records:
                rejected_records.append(rec)

    run_id = str(summary.get("run_id") or "")
    raw_status = str(summary.get("status") or "")
    if raw_status in {"done_clean", "done_with_deletions", "done_with_pending_children"}:
        status_note = "finished OK"
    elif raw_status == "failed_integrity":
        status_note = "finished with a warning (check below)"
    else:
        status_note = raw_status or "unknown"
    finished = _iso(_utc_now())[:19].replace("T", " ") + " UTC"
    pending_names = pending_child_display_names()
    pending_total = len(pending_names)
    lines = [
        "# Last staging intake",
        "",
        f"**Run:** `{run_id}`  ",
        f"**Finished:** {finished}  ",
        f"**Result:** {status_note}",
    ]
    lines.extend(_format_startup_cleanup_lines(summary))
    lines.extend(
        [
        "",
        "Drop new PDFs only in `papers-staging/`. Anything **not added** as a main paper is either in "
        f"**`papers-rejected/`** (junk/couldn't add) or **waiting for a parent paper** (supplements, "
        "spreadsheets, etc.). Rejected files are deleted **14 days after they were moved there** "
        "(each file on its own schedule).",
        "",
        f"## Added to corpus ({len(added)})",
        "",
    ])
    if added:
        lines.extend(_format_added_line(i) for i in added)
    else:
        lines.append("- *(none this run)*")
    lines.extend(["", f"## Already in corpus — staging copy removed ({len(duplicates)})", ""])
    if duplicates:
        lines.extend(_format_duplicate_line(i) for i in duplicates)
    else:
        lines.append("- *(none this run)*")
    lines.extend(["", f"## Not added — see `papers-rejected/` ({len(rejected_records)})", ""])
    if rejected_records:
        lines.extend(_format_rejected_line(r) for r in rejected_records)
    else:
        lines.append("- *(none this run)*")
    lines.extend(
        [
            "",
            f"## Waiting for parent paper ({pending_total})",
            "",
            "These are **not** main research papers (e.g. supplement `.docx`, image, spreadsheet). "
            "They were moved out of staging to the pending-child area until the main paper is in the corpus.",
            "",
        ]
    )
    if pending_names:
        for name in pending_names:
            lines.append(f"- `{name}`")
    else:
        lines.append("- *(none)*")
    staging_left = int(summary.get("final_staging_count") or 0)
    lines.extend(
        [
            "",
            "## Staging folder",
            "",
            f"`papers-staging/` PDFs remaining: **{staging_left}** (should be **0** after a clean run).",
            "",
            "Machine-readable detail: `_system/logs/runs/"
            f"{run_id}/all_staging_ingest_items.jsonl`",
            "",
        ]
    )
    return "\n".join(lines)


def write_intake_last_run(summary: dict[str, Any]) -> Path:
    path = config.INTAKE_LAST_RUN_MD
    text = build_intake_summary_markdown(summary)
    path.write_text(text, encoding="utf-8")
    return path


def print_intake_summary_console(summary: dict[str, Any], *, artifacts: dict[str, str] | None = None) -> None:
    items = [i for i in (summary.get("items") or []) if isinstance(i, dict)]
    added = sum(1 for i in items if i.get("status") == "success")
    dups = sum(1 for i in items if i.get("status") == "confirmed_duplicate_deleted")
    rej = len(run_rejected_records())
    pending_names = pending_child_display_names()
    cleaned = summary.get("startup_cleanup_incomplete_rows") or []
    print("\n=== Intake summary (for you) ===", flush=True)
    if cleaned:
        print(f"  Startup repair: removed {len(cleaned)} incomplete row(s) from an earlier run", flush=True)
    print(f"  Added to corpus: {added}", flush=True)
    print(f"  Already in corpus (duplicate removed from staging): {dups}", flush=True)
    print(f"  Not added (in papers-rejected/): {rej}", flush=True)
    if pending_names:
        print(f"  Waiting for parent paper (not main ingest): {len(pending_names)}", flush=True)
    print(f"  Staging PDFs left: {int(summary.get('final_staging_count') or 0)}", flush=True)
    print(f"  Full summary: {config.INTAKE_LAST_RUN_MD}", flush=True)
    if rej:
        print(f"  Rejected folder: {config.PAPERS_REJECTED}", flush=True)
    if summary.get("error"):
        print(f"  Pipeline note: {summary['error']}", flush=True)
    arts = artifacts or {}
    if arts.get("json"):
        print(f"  Machine log: {arts['json']}", flush=True)
