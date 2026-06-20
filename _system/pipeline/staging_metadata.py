"""Internal staging metadata storage — papers-staging root is PDF-only."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from path_governance import MAX_FILENAME, shorten_existing_filename, validate_path_policy

RETRY_SUFFIX = ".review-retry.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def internal_sidecar_path(staging_pdf: Path) -> Path:
    """Path for internal metadata sidecar (not in papers-staging root)."""
    config.STAGING_METADATA.mkdir(parents=True, exist_ok=True)
    name = Path(staging_pdf).name
    candidate = config.STAGING_METADATA / f"{name}{RETRY_SUFFIX}"
    if validate_path_policy(candidate)["ok"]:
        return candidate
    safe_name = shorten_existing_filename(
        name,
        title=Path(name).stem,
        hash_source=name,
        max_filename=MAX_FILENAME - len(RETRY_SUFFIX),
    )
    return config.STAGING_METADATA / f"{safe_name}{RETRY_SUFFIX}"


def read_internal_meta(staging_pdf: Path) -> dict[str, Any]:
    path = internal_sidecar_path(staging_pdf)
    if not path.exists():
        raw_internal = config.STAGING_METADATA / f"{Path(staging_pdf).name}{RETRY_SUFFIX}"
        if raw_internal.exists():
            return _load_json(raw_internal)
        legacy = Path(staging_pdf).with_suffix(Path(staging_pdf).suffix + RETRY_SUFFIX)
        if legacy.exists():
            return _load_json(legacy)
        return {}
    return _load_json(path)


def write_internal_meta(staging_pdf: Path, meta: dict[str, Any]) -> Path:
    path = internal_sidecar_path(staging_pdf)
    meta = dict(meta)
    meta["updated_at"] = _now()
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def migrate_root_sidecars_to_internal(*, dry_run: bool = False) -> dict[str, int]:
    """Move *.review-retry.json from papers-staging root to CorpusStore/staging-metadata/."""
    counts = {"migrated": 0, "removed_legacy_root": 0, "already_internal": 0}
    staging = config.STAGING
    if not staging.exists():
        return counts
    for sidecar in staging.glob(f"*{RETRY_SUFFIX}"):
        pdf_name = sidecar.name[: -len(RETRY_SUFFIX)]
        pdf_path = staging / pdf_name
        dest = internal_sidecar_path(pdf_path)
        if dest.exists():
            counts["already_internal"] += 1
            if not dry_run:
                sidecar.unlink(missing_ok=True)
            counts["removed_legacy_root"] += 1
            continue
        if dry_run:
            counts["migrated"] += 1
            continue
        config.STAGING_METADATA.mkdir(parents=True, exist_ok=True)
        shutil.move(str(sidecar), str(dest))
        counts["migrated"] += 1
        counts["removed_legacy_root"] += 1
    for other in staging.iterdir():
        if other.is_file() and other.suffix.lower() == ".json" and not other.name.endswith(RETRY_SUFFIX):
            if dry_run:
                counts["removed_legacy_root"] += 1
            else:
                archive = config.STAGING_METADATA / other.name
                shutil.move(str(other), str(archive))
                counts["removed_legacy_root"] += 1
    return counts


def remove_internal_meta(staging_pdf: Path) -> None:
    path = internal_sidecar_path(staging_pdf)
    path.unlink(missing_ok=True)
    legacy = Path(staging_pdf).with_suffix(Path(staging_pdf).suffix + RETRY_SUFFIX)
    legacy.unlink(missing_ok=True)
