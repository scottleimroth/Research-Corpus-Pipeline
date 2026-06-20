"""Shared pre-run cleanup for corpus pipeline launchers (logs + DB backups)."""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

LAUNCHER_DIR = Path(__file__).resolve().parent
SYSTEM_ROOT = LAUNCHER_DIR.parent
PROJECT_ROOT = SYSTEM_ROOT.parent

RUNS_DIR = SYSTEM_ROOT / "logs" / "runs"
LOGS_ARCHIVE = SYSTEM_ROOT / "logs" / "archive"
BACKUP_DIR = SYSTEM_ROOT / "CorpusStore" / "backups"
BACKUP_ARCHIVE = BACKUP_DIR / "archive"
LOCK_PATH = SYSTEM_ROOT / "logs" / "all_staging.ingest.lock"
REVIEW_DIR = SYSTEM_ROOT / "review"

KEEP_RUN_FOLDERS = 2
KEEP_DB_BACKUPS = 1


def ensure_project_cwd() -> Path:
    os.chdir(SYSTEM_ROOT)
    return SYSTEM_ROOT


def _warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def active_run_id_from_lock() -> str | None:
    if not LOCK_PATH.exists():
        return None
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = int(data.get("pid") or 0)
    if pid and _pid_alive(pid):
        return str(data.get("run_id") or "") or None
    return None


def _folder_in_use(path: Path) -> bool:
    """Best-effort check: try exclusive rename of a probe file."""
    probe = path / ".launcher_cleanup_probe"
    try:
        probe.write_text("x", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return False
    except OSError:
        return True


def cleanup_old_run_logs(*, exclude_run_ids: set[str] | None = None) -> dict:
    """Keep latest KEEP_RUN_FOLDERS under logs/runs; archive older folders."""
    result = {"archived": [], "kept": [], "skipped": [], "warnings": []}
    exclude = set(exclude_run_ids or ())
    active = active_run_id_from_lock()
    if active:
        exclude.add(active)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_ARCHIVE.mkdir(parents=True, exist_ok=True)

    folders = [p for p in RUNS_DIR.iterdir() if p.is_dir()]
    folders.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for folder in folders[:KEEP_RUN_FOLDERS]:
        result["kept"].append(folder.name)

    for folder in folders[KEEP_RUN_FOLDERS:]:
        name = folder.name
        if name in exclude:
            result["skipped"].append({"name": name, "reason": "active or excluded run"})
            continue
        if _folder_in_use(folder):
            result["skipped"].append({"name": name, "reason": "folder in use"})
            continue
        dest = LOGS_ARCHIVE / name
        if dest.exists():
            dest = LOGS_ARCHIVE / f"{name}-archived-{datetime.now():%Y%m%d-%H%M%S}"
        try:
            shutil.move(str(folder), str(dest))
            result["archived"].append({"from": name, "to": str(dest.relative_to(SYSTEM_ROOT))})
        except OSError as e:
            msg = f"Could not archive run log folder {name}: {e}"
            result["warnings"].append(msg)
            _warn(msg)

    return result


def cleanup_old_db_backups() -> dict:
    """Keep latest KEEP_DB_BACKUPS .db file in CorpusStore/backups; archive older."""
    result = {"archived": [], "kept": [], "skipped": [], "warnings": []}
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_ARCHIVE.mkdir(parents=True, exist_ok=True)

    backups = sorted(
        (p for p in BACKUP_DIR.glob("*.db") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    for path in backups[:KEEP_DB_BACKUPS]:
        result["kept"].append(path.name)

    for path in backups[KEEP_DB_BACKUPS:]:
        dest = BACKUP_ARCHIVE / path.name
        if dest.exists():
            stem = path.stem
            dest = BACKUP_ARCHIVE / f"{stem}-archived-{datetime.now():%Y%m%d-%H%M%S}.db"
        try:
            shutil.move(str(path), str(dest))
            result["archived"].append({"from": path.name, "to": str(dest.relative_to(SYSTEM_ROOT))})
        except OSError as e:
            msg = f"Could not archive DB backup {path.name}: {e}"
            result["warnings"].append(msg)
            _warn(msg)

    return result


def run_pre_launch_cleanup(*, exclude_run_ids: set[str] | None = None) -> dict:
    logs = cleanup_old_run_logs(exclude_run_ids=exclude_run_ids)
    backups = cleanup_old_db_backups()
    return {"logs": logs, "backups": backups}


def count_review_pdfs() -> int:
    if not REVIEW_DIR.exists():
        return 0
    return sum(1 for _ in REVIEW_DIR.rglob("*.pdf"))


def count_technical_failure_pdfs() -> int:
    tech = REVIEW_DIR / "technical-failure"
    if not tech.exists():
        return 0
    return sum(1 for _ in tech.rglob("*.pdf"))


def count_review_recovery_pending() -> int:
    folder = REVIEW_DIR / "recovery-pending"
    if not folder.exists():
        return 0
    return sum(1 for p in folder.iterdir() if p.is_file() and not p.name.endswith(".json"))


def count_review_subfolder(name: str) -> int:
    folder = REVIEW_DIR / name
    if not folder.exists():
        return 0
    return sum(1 for _ in folder.glob("*.pdf"))


def count_staging_candidates() -> int:
    staging = PROJECT_ROOT / "papers-staging"
    if not staging.exists():
        return 0
    return sum(
        1
        for p in staging.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() != ".json"
    )


def count_staging_pdfs() -> int:
    return count_staging_candidates()


def count_pending_child_documents() -> int:
    pending = SYSTEM_ROOT / "pending-parent-child-documents"
    if not pending.exists():
        return 0
    return sum(
        1 for p in pending.iterdir() if p.is_file() and not p.name.endswith(".meta.json")
    )


def count_pending_supplements() -> int:
    return count_pending_child_documents()


def pending_child_document_stats() -> dict[str, int]:
    pending = SYSTEM_ROOT / "pending-parent-child-documents"
    if not pending.exists():
        return {"count": 0, "oldest_days": 0}
    import json
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    count = 0
    oldest = 0
    for pdf in pending.iterdir():
        if not pdf.is_file() or pdf.name.endswith(".meta.json"):
            continue
        count += 1
        meta_path = pdf.with_suffix(pdf.suffix + ".meta.json")
        first_seen = None
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                first_seen = meta.get("first_seen")
            except (OSError, json.JSONDecodeError):
                pass
        if first_seen:
            try:
                seen = datetime.fromisoformat(str(first_seen).replace("Z", "+00:00"))
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                oldest = max(oldest, max(0, (now - seen).days))
            except ValueError:
                pass
    return {"count": count, "oldest_days": oldest}


STOP_REQUESTED_FILENAME = "STOP_REQUESTED"


def stop_requested_path(run_dir: Path) -> Path:
    return run_dir / STOP_REQUESTED_FILENAME


def request_stop(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    stop_requested_path(run_dir).write_text("1", encoding="utf-8")


def clear_stop_request(run_dir: Path) -> None:
    stop_requested_path(run_dir).unlink(missing_ok=True)


def is_stop_requested(run_dir: Path) -> bool:
    return stop_requested_path(run_dir).exists()


def fetch_usd_to_aud_rate() -> tuple[float, str]:
    import sys

    system_root = str(SYSTEM_ROOT)
    pipeline_dir = str(SYSTEM_ROOT / "pipeline")
    if system_root not in sys.path:
        sys.path.insert(0, system_root)
    if pipeline_dir not in sys.path:
        sys.path.insert(0, pipeline_dir)
    from cost_tracking import fetch_usd_to_aud_rate as _fetch

    return _fetch()


def make_run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}"
