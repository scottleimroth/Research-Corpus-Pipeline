"""Parent-dependent child document matching, pending-parent holding, and expiry policy."""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import config
import papers_db
from add_paper import SOURCE_PDFS, _find_parent_paper_from_name, log_error
from path_governance import MAX_FILENAME, reserve_unique_path, safe_destination_for_file

PENDING_DIR = config.PENDING_PARENT_CHILD_DOCUMENTS
EXPIRY_DAYS = int(getattr(config, "PENDING_CHILD_EXPIRY_DAYS", 180))
TECHNICAL_FAILURE_DIR = config.REVIEW_TECHNICAL_FAILURE

LEGACY_MIGRATION_DIRS = (
    config.REVIEW_SUPPLEMENTS,
    config.REVIEW_SUPPLEMENTS_PENDING,
    config.REVIEW_DIR / "child-documents-pending-parent",
)

CHILD_TYPE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("expression_of_concern", (r"expression[\s_-]?of[\s_-]?concern",)),
    ("retraction", (r"retraction", r"retracted")),
    ("erratum", (r"erratum", r"errata")),
    ("corrigendum", (r"corrigendum",)),
    ("correction", (r"correction", r"corrected")),
    ("addendum", (r"addendum",)),
    ("appendix", (r"appendix", r"appendices")),
    ("supplement", (r"supplement", r"supplementary", r"_supp", r"-supp", r"mmc\d", r"supporting")),
)

PENDING_REASON = (
    "pending because no parent matched yet; will retry on every pipeline run"
)

LINK_COUNT_KEYS = (
    "supplement_linked",
    "addendum_linked",
    "correction_linked",
    "erratum_linked",
    "retraction_linked",
    "expression_of_concern_linked",
    "appendix_linked",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty_child_counts() -> dict[str, int]:
    counts = {
        "child_document_pending_parent": 0,
        "child_documents_linked_this_run": 0,
        "child_documents_moved_to_pending": 0,
        "child_document_deleted_expired_no_parent": 0,
        "child_document_linked_from_pending": 0,
        "pending_child_documents_remaining": 0,
        "pending_child_documents_expiring_soon": 0,
        "oldest_pending_child_document_days": 0,
        "pending_child_start": 0,
        "pending_child_inspected": 0,
        "pending_child_corrupt_deleted": 0,
        "pending_child_sidecars_repaired": 0,
        "pending_child_linked": 0,
        "pending_child_moved_to_staging": 0,
        "pending_child_expired_deleted": 0,
        "pending_child_startup_checked": 0,
    }
    for key in LINK_COUNT_KEYS:
        counts[key] = 0
    # Legacy aliases for reports/GUI
    counts.update(
        {
            "supplement_pending_parent": 0,
            "supplement_linked_from_pending": 0,
            "supplement_deleted_no_parent": 0,
            "pending_supplements_remaining": 0,
        }
    )
    return counts


@dataclass
class ChildDocumentRunContext:
    counts: dict[str, int] = field(default_factory=empty_child_counts)
    run_paper_ids: set[str] = field(default_factory=set)

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = int(self.counts.get(key, 0)) + n

    def register_successful_parent(self, paper_id: str) -> None:
        if paper_id:
            self.run_paper_ids.add(paper_id)

    def to_summary(self) -> dict[str, Any]:
        stats = pending_folder_stats()
        self.counts["pending_child_documents_remaining"] = stats["count"]
        self.counts["oldest_pending_child_document_days"] = stats["oldest_days"]
        self.counts["pending_child_documents_expiring_soon"] = stats["expiring_soon"]
        self.counts["pending_supplements_remaining"] = stats["count"]
        return dict(self.counts)


# Backward-compatible alias
SupplementRunContext = ChildDocumentRunContext


def detect_child_document_type(filename: str) -> str | None:
    low = filename.lower()
    for child_type, patterns in CHILD_TYPE_PATTERNS:
        for pat in patterns:
            if re.search(pat, low):
                return child_type
    return None


def is_child_document(filename: str) -> bool:
    return detect_child_document_type(filename) is not None


def is_supplement(filename: str) -> bool:
    return detect_child_document_type(filename) == "supplement" or bool(
        re.search(r"supplement|supplementary|_supp|mmc\d", filename.lower())
    )


def _meta_path(pdf_path: Path) -> Path:
    return pdf_path.with_suffix(pdf_path.suffix + ".meta.json")


def _pending_data_files() -> list[Path]:
    """All pending child document files excluding sidecars."""
    if not PENDING_DIR.exists():
        return []
    return sorted(
        p
        for p in PENDING_DIR.iterdir()
        if p.is_file() and not p.name.endswith(".meta.json")
    )


def read_sidecar(pdf_path: Path) -> dict[str, Any]:
    meta_path = _meta_path(pdf_path)
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def write_sidecar(pdf_path: Path, meta: dict[str, Any]) -> None:
    meta_path = _meta_path(pdf_path)
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def ensure_sidecar(
    pdf_path: Path,
    *,
    child_type: str,
    original_path: str | None = None,
    hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = read_sidecar(pdf_path)
    now = _now_iso()
    if not meta.get("first_seen"):
        meta["first_seen"] = now
    meta.update(
        {
            "filename": pdf_path.name,
            "path": str(pdf_path),
            "child_type": child_type,
            "last_checked": now,
            "status": meta.get("status") or "pending_parent",
            "match_attempts": int(meta.get("match_attempts") or 0),
            "expiry_days": int(meta.get("expiry_days") or EXPIRY_DAYS),
        }
    )
    if original_path:
        meta["original_path"] = original_path
    if hints:
        for key in (
            "detected_parent_title",
            "detected_parent_doi",
            "detected_parent_authors",
            "detected_parent_year",
            "likely_parent_title",
            "likely_parent_doi",
            "likely_parent_authors",
            "likely_parent_year",
        ):
            if hints.get(key):
                meta[key] = hints[key]
    write_sidecar(pdf_path, meta)
    return meta


def _author_year_from_filename(filename: str) -> tuple[str | None, str | None]:
    name = filename.lower()
    stem = name
    for child_type, patterns in CHILD_TYPE_PATTERNS:
        for pat in patterns:
            stem = re.sub(pat, "", stem, count=1)
    stem = re.sub(r"[_-]+", "_", stem).strip("_")
    stem = re.sub(r"\.pdf$|\.docx?$|\.xlsx?$", "", stem)
    m = re.match(r"^([a-z]{3,})[_\-](\d{4})", stem)
    if m:
        return m.group(1), m.group(2)
    m2 = re.match(r"^[^a-z]*([a-z]{4,})", stem)
    if m2:
        return m2.group(1), None
    return None, None


def extract_parent_hints(filename: str) -> dict[str, Any]:
    author, year = _author_year_from_filename(filename)
    hints: dict[str, Any] = {}
    if author:
        hints["likely_parent_authors"] = author
    if year:
        hints["likely_parent_year"] = year
    return hints


def _filename_may_match_parent(candidate_name: str, author: str | None, year: str | None) -> bool:
    if not author:
        return False
    low = candidate_name.lower()
    if is_child_document(candidate_name):
        return False
    if year and low.startswith(f"{author}_{year}"):
        return True
    return low.startswith(f"{author}_")


def pending_folder_stats() -> dict[str, int]:
    if not PENDING_DIR.exists():
        return {"count": 0, "oldest_days": 0, "expiring_soon": 0}
    now = datetime.now(timezone.utc)
    count = 0
    oldest_days = 0
    expiring_soon = 0
    for pdf in _pending_data_files():
        count += 1
        meta = read_sidecar(pdf)
        first_seen = meta.get("first_seen") or _now_iso()
        try:
            seen = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            age_days = max(0, (now - seen).days)
        except ValueError:
            age_days = 0
        oldest_days = max(oldest_days, age_days)
        if age_days >= EXPIRY_DAYS - 30:
            expiring_soon += 1
    return {"count": count, "oldest_days": oldest_days, "expiring_soon": expiring_soon}


def count_pending_child_documents() -> int:
    return pending_folder_stats()["count"]


count_pending_supplements = count_pending_child_documents


def _normalize_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", str(text or "").lower())}


def _parent_match_clues(parent: dict) -> dict[str, Any]:
    bm = parent.get("basic_metadata") or {}
    fi = parent.get("file_info") or {}
    renamed = fi.get("renamed_filename") or fi.get("original_filename") or parent.get("id") or ""
    return {
        "id": str(parent.get("id") or ""),
        "doi": str(bm.get("doi") or "").lower().strip(),
        "title": str(bm.get("title") or "").lower(),
        "title_tokens": _normalize_tokens(bm.get("title") or ""),
        "authors": [str(a).lower() for a in (bm.get("authors") or []) if isinstance(a, str)],
        "author_surnames": {
            str(a).split()[-1].lower() for a in (bm.get("authors") or []) if isinstance(a, str) and str(a).split()
        },
        "year": str(bm.get("year") or "").strip(),
        "filename_stem": Path(renamed).stem.lower(),
    }


def _sidecar_may_match_parent(meta: dict, filename: str, parent: dict) -> bool:
    """Fast sidecar/filename filter — no PDF content reads."""
    clues = _parent_match_clues(parent)
    parent_id = clues["id"].lower()
    if not parent_id:
        return False

    side_doi = str(meta.get("likely_parent_doi") or meta.get("detected_parent_doi") or "").lower().strip()
    if side_doi and clues["doi"] and side_doi == clues["doi"]:
        return True

    side_year = str(meta.get("likely_parent_year") or meta.get("detected_parent_year") or "").strip()
    side_author = str(meta.get("likely_parent_authors") or meta.get("detected_parent_authors") or "").lower()
    if side_year and clues["year"] and side_year == clues["year"]:
        if not side_author or side_author in parent_id or any(side_author in s for s in clues["author_surnames"]):
            return True

    if side_author:
        if side_author in parent_id or side_author in clues["filename_stem"]:
            return True
        if clues["author_surnames"] and side_author in clues["author_surnames"]:
            return True

    side_title = str(meta.get("likely_parent_title") or meta.get("detected_parent_title") or "")
    if side_title and clues["title_tokens"]:
        overlap = _normalize_tokens(side_title) & clues["title_tokens"]
        if len(overlap) >= 2:
            return True

    author, year = _author_year_from_filename(filename)
    if author:
        if parent_id.startswith(f"{author}_") or clues["filename_stem"].startswith(f"{author}_"):
            return True
        if year and clues["year"] == year and (
            parent_id.startswith(f"{author}_{year}") or clues["filename_stem"].startswith(f"{author}_{year}")
        ):
            return True

    linked = str(meta.get("linked_parent_id") or "").lower()
    if linked and linked == parent_id:
        return True
    return False


def find_parent_for_child(
    filename: str,
    ctx: ChildDocumentRunContext | None = None,
    *,
    all_meta: list[dict] | None = None,
) -> dict | None:
    parent = _find_parent_paper_from_name(filename, all_meta=all_meta)
    if parent:
        return parent
    if not ctx or not ctx.run_paper_ids:
        return None
    author, year = _author_year_from_filename(filename)
    if not author:
        return None
    for paper_id in ctx.run_paper_ids:
        paper = papers_db.get_paper(paper_id)
        if not paper:
            continue
        fi = paper.get("file_info") or {}
        renamed = fi.get("renamed_filename") or fi.get("original_filename") or ""
        if _filename_may_match_parent(renamed, author, year) or _filename_may_match_parent(paper_id, author, year):
            return paper
    return None


def _linked_count_key(child_type: str) -> str:
    if child_type == "supplement":
        return "supplement_linked"
    if child_type in {"correction", "corrigendum"}:
        return "correction_linked"
    if child_type == "addendum":
        return "addendum_linked"
    if child_type == "erratum":
        return "erratum_linked"
    if child_type == "retraction":
        return "retraction_linked"
    if child_type == "expression_of_concern":
        return "expression_of_concern_linked"
    if child_type == "appendix":
        return "appendix_linked"
    return "supplement_linked"


def link_child_to_parent(pdf_path: Path, parent: dict, *, child_type: str | None = None) -> bool:
    pdf_path = Path(pdf_path)
    child_type = child_type or detect_child_document_type(pdf_path.name) or "supplement"
    parent_id = parent.get("id", "unknown")
    ext = pdf_path.suffix.lstrip(".") or "pdf"
    prefix = f"{parent_id}_{child_type}" if child_type != "supplement" else f"{parent_id}_supplement"
    dest = reserve_unique_path(
        SOURCE_PDFS,
        f"{prefix}_01.{ext}",
        content_token=f"{parent_id}_{child_type}_{pdf_path.name}",
    )

    try:
        if pdf_path.resolve() != dest.resolve():
            shutil.copy2(pdf_path, dest)
    except OSError as e:
        log_error(str(pdf_path), "CHILD_DOC_COPY", f"copy failed: {e}")
        return False

    supplements = parent.get("supplements") or {}
    if not isinstance(supplements, dict):
        supplements = {}
    items = supplements.get("items") or []
    if not isinstance(items, list):
        items = []
    if not any(isinstance(it, dict) and it.get("filename") == dest.name for it in items):
        size_mb = round(dest.stat().st_size / (1024 * 1024), 3) if dest.exists() else None
        item = {
            "filename": dest.name,
            "original_filename": pdf_path.name,
            "format": ext,
            "size_mb": size_mb,
            "type": child_type,
            "description": None,
            "added": _now_iso(),
            "child_document": True,
        }
        if child_type in {"retraction", "expression_of_concern"}:
            item["description"] = f"Related {child_type.replace('_', ' ')} (warning metadata; parent rating unchanged)"
            notes = parent.get("notes_and_flags") or {}
            if isinstance(notes, dict):
                warnings = notes.setdefault("child_document_warnings", [])
                if isinstance(warnings, list):
                    warnings.append({"type": child_type, "filename": dest.name, "linked_at": _now_iso()})
                parent["notes_and_flags"] = notes
        items.append(item)
    supplements["items"] = items
    supplements["has_supplements"] = True
    supplements["files"] = [it["filename"] for it in items if isinstance(it, dict) and it.get("filename")]
    parent["supplements"] = supplements

    if not parent.get("full_text"):
        existing = papers_db.get_paper(parent_id)
        if existing and existing.get("full_text"):
            parent["full_text"] = existing["full_text"]

    try:
        papers_db.save_paper(parent)
    except Exception as e:
        log_error(str(pdf_path), "CHILD_DOC_DB", f"DB update for parent {parent_id} failed: {e}")
        return False

    meta = ensure_sidecar(pdf_path, child_type=child_type)
    meta.update(
        {
            "status": "linked",
            "linked_parent_id": parent_id,
            "linked_filename": dest.name,
            "decision_reason": f"linked to parent {parent_id}",
            "last_checked": _now_iso(),
        }
    )
    write_sidecar(pdf_path, meta)
    try:
        pdf_path.unlink()
        _meta_path(pdf_path).unlink(missing_ok=True)
    except OSError:
        pass
    return True


link_supplement_to_parent = link_child_to_parent


def _delete_corrupt_pending_child(pdf_path: Path, reason: str) -> None:
    pdf_path = Path(pdf_path)
    try:
        pdf_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        _meta_path(pdf_path).unlink(missing_ok=True)
    except OSError:
        pass
    log_error(str(pdf_path), "CHILD_DOC_CORRUPT_DELETED", reason)


def _move_pending_to_staging(pdf_path: Path, recovered_metadata: dict[str, Any], *, reason: str) -> Path | None:
    from review_retry_tracking import record_staging_move

    config.STAGING.mkdir(parents=True, exist_ok=True)
    pdf_path = Path(pdf_path)
    dest = safe_destination_for_file(
        pdf_path,
        config.STAGING,
        reason=reason,
        max_filename=MAX_FILENAME - len(".review-retry.json"),
    )
    try:
        shutil.move(str(pdf_path), str(dest))
        _meta_path(pdf_path).unlink(missing_ok=True)
        record_staging_move(
            dest,
            source_review_folder="pending-parent-child-documents",
            original_review_reason=reason,
            recovered_metadata=recovered_metadata,
        )
        return dest
    except OSError as e:
        log_error(str(pdf_path), "CHILD_DOC_STAGING_MOVE", f"move failed: {e}")
        return None


def _repair_pending_sidecar(pdf_path: Path, *, child_type: str | None = None) -> dict[str, Any]:
    from pdf_content_guard import recover_metadata_for_path

    pdf_path = Path(pdf_path)
    child_type = child_type or detect_child_document_type(pdf_path.name) or "unknown_child_document"
    recovered = recover_metadata_for_path(pdf_path, pages=3)
    hints = extract_parent_hints(pdf_path.name)
    if recovered.get("title"):
        hints["likely_parent_title"] = recovered.get("title")
    if recovered.get("doi"):
        hints["likely_parent_doi"] = recovered.get("doi")
    if recovered.get("authors"):
        hints["likely_parent_authors"] = recovered.get("authors")
    if recovered.get("year"):
        hints["likely_parent_year"] = recovered.get("year")
    meta = ensure_sidecar(pdf_path, child_type=child_type, hints=hints)
    meta["status"] = "pending_parent"
    meta["expiry_days"] = EXPIRY_DAYS
    write_sidecar(pdf_path, meta)
    return meta


def audit_pending_child_documents(
    ctx: ChildDocumentRunContext | None = None,
    *,
    dry_run: bool = False,
    allow_paid_api: bool = False,
    allow_opus: bool = False,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Audit pending-parent-child-documents: delete corrupt, reclassify, link, repair sidecars."""
    from recovery_ladder import run_recovery_ladder

    ctx = ctx or ChildDocumentRunContext()
    counts = {
        "pending_child_start": 0,
        "pending_child_inspected": 0,
        "pending_child_corrupt_deleted": 0,
        "pending_child_sidecars_repaired": 0,
        "pending_child_linked": 0,
        "pending_child_moved_to_staging": 0,
        "pending_child_expired_deleted": 0,
        "pending_child_remaining": 0,
    }
    if not PENDING_DIR.exists():
        counts["pending_child_remaining"] = 0
        return counts

    pdfs = _pending_data_files()
    counts["pending_child_start"] = len(pdfs)
    now = datetime.now(timezone.utc)

    for pdf_path in pdfs:
        if not pdf_path.exists():
            continue
        counts["pending_child_inspected"] += 1

        from supplement_content_classifier import classify_supplement_file, is_deletable

        classified = classify_supplement_file(
            pdf_path,
            allow_paid_api=allow_paid_api and not dry_run,
            allow_opus=allow_opus and allow_paid_api and not dry_run,
        )
        classification = str(classified.get("classification") or "")

        if is_deletable(classification):
            reason = f"pending_child_audit_deleted:{classification.lower()}"
            if dry_run:
                if log_fn:
                    log_fn(f"  [dry-run] delete pending child ({classification}): {pdf_path.name}")
            else:
                if classification == "CONFIRMED_DUPLICATE":
                    from review_resolution import delete_confirmed_duplicate, ResolutionAudit

                    delete_confirmed_duplicate(pdf_path, audit=ResolutionAudit(), dry_run=False)
                else:
                    import intake_rejected

                    meta = read_sidecar(pdf_path)
                    original_name = Path(str(meta.get("original_path") or pdf_path.name)).name
                    rec = intake_rejected.move_file_to_rejected(
                        pdf_path,
                        status="deleted_junk",
                        reason=reason,
                        user_reason=intake_rejected.STATUS_TO_USER_REASON.get("deleted_junk"),
                        original_name=original_name,
                    )
                    if not rec:
                        _delete_corrupt_pending_child(pdf_path, reason)
            counts["pending_child_corrupt_deleted"] += 1
            ctx.bump("pending_child_corrupt_deleted")
            continue

        if classification == "MODEL_RECOVERY_REQUIRED":
            if dry_run:
                if log_fn:
                    log_fn(f"  [dry-run] model recovery required for pending child: {pdf_path.name}")
            else:
                meta = read_sidecar(pdf_path)
                meta["status"] = "model_recovery_required"
                meta["content_classification"] = classification
                meta["classification_evidence"] = classified.get("evidence")
                write_sidecar(pdf_path, meta)
            continue

        child_type = detect_child_document_type(pdf_path.name)
        sniff = classified.get("sniff") or {}
        is_valid_child = child_type or classification.startswith("VALID_") or sniff.get("detected_kind") != "pdf"
        if is_valid_child:
            if dry_run:
                counts["pending_child_sidecars_repaired"] += 1
            else:
                _repair_pending_sidecar(pdf_path, child_type=child_type)
                counts["pending_child_sidecars_repaired"] += 1

            meta = read_sidecar(pdf_path) if not dry_run else {}
            first_seen = meta.get("first_seen") or _now_iso()
            try:
                seen = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                age_days = (now - seen).days
            except ValueError:
                age_days = 0

            if age_days >= EXPIRY_DAYS and classification.startswith("VALID_"):
                if dry_run:
                    if log_fn:
                        log_fn(f"  [dry-run] delete expired pending child ({age_days}d): {pdf_path.name}")
                else:
                    try:
                        pdf_path.unlink()
                        _meta_path(pdf_path).unlink(missing_ok=True)
                        log_error(str(pdf_path), "CHILD_DOC_EXPIRED", f"expired after {EXPIRY_DAYS} days")
                    except OSError:
                        pass
                counts["pending_child_expired_deleted"] += 1
                ctx.bump("child_document_deleted_expired_no_parent")
                continue

            if not dry_run:
                parent = find_parent_for_child(pdf_path.name, ctx)
                if parent and link_child_to_parent(pdf_path, parent, child_type=child_type):
                    counts["pending_child_linked"] += 1
                    ctx.bump("child_document_linked_from_pending")
                    ctx.bump("child_documents_linked_this_run")
                    if log_fn:
                        log_fn(f"  Linked pending child to parent {parent.get('id')}: {pdf_path.name}")
            continue

        result = run_recovery_ladder(
            pdf_path,
            allow_paid_api=allow_paid_api and not dry_run,
            allow_opus=allow_opus and allow_paid_api and not dry_run,
            dry_run=dry_run,
        )
        decision = str(result.get("decision") or "")
        recovered = result.get("recovered_metadata") or {}

        if decision in {"DELETE_JUNK", "DELETE_DUPLICATE"}:
            if dry_run:
                if log_fn:
                    log_fn(f"  [dry-run] delete pending child ({decision}): {pdf_path.name}")
            else:
                if decision == "DELETE_DUPLICATE":
                    from review_resolution import delete_confirmed_duplicate, ResolutionAudit

                    delete_confirmed_duplicate(pdf_path, audit=ResolutionAudit(), dry_run=False)
                else:
                    _delete_corrupt_pending_child(
                        pdf_path,
                        str(result.get("evidence", {}).get("reason") or "pending_child_junk"),
                    )
            counts["pending_child_corrupt_deleted"] += 1
            continue

        if decision in {"RECOVERED_RESEARCH_PAPER_MOVE_TO_STAGING", "RECOVERED_NON_RATABLE_REFERENCE_MOVE_TO_STAGING"}:
            if dry_run:
                if log_fn:
                    log_fn(f"  [dry-run] move misclassified pending child to staging: {pdf_path.name}")
                counts["pending_child_moved_to_staging"] += 1
                continue
            dest = _move_pending_to_staging(pdf_path, recovered, reason=decision)
            if dest:
                counts["pending_child_moved_to_staging"] += 1
                if log_fn:
                    log_fn(f"  Moved pending child to staging (not a child doc): {dest.name}")
            continue

        if decision == "MODEL_RECOVERY_REQUIRED":
            if dry_run:
                if log_fn:
                    log_fn(f"  [dry-run] model recovery required for pending child: {pdf_path.name}")
            else:
                child_type = detect_child_document_type(pdf_path.name) or "supplement"
                _repair_pending_sidecar(pdf_path, child_type=child_type)
                meta = read_sidecar(pdf_path)
                meta["status"] = "model_recovery_required"
                meta["model_recovery_note"] = "held in pending-parent-child-documents; no review dust"
                sidecar_path = _meta_path(pdf_path)
                sidecar_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            continue

        if decision == "CHILD_DOCUMENT_TO_PENDING_PARENT":
            child_type = detect_child_document_type(pdf_path.name) or "supplement"
            if dry_run:
                counts["pending_child_sidecars_repaired"] += 1
            else:
                _repair_pending_sidecar(pdf_path, child_type=child_type)
                counts["pending_child_sidecars_repaired"] += 1
                parent = find_parent_for_child(pdf_path.name, ctx)
                if parent and link_child_to_parent(pdf_path, parent, child_type=child_type):
                    counts["pending_child_linked"] += 1
                    ctx.bump("child_document_linked_from_pending")
                    if log_fn:
                        log_fn(f"  Linked pending child to parent {parent.get('id')}: {pdf_path.name}")

    counts["pending_child_remaining"] = len(_pending_data_files())
    return counts


def _pending_file_sha256(doc_path: Path) -> str:
    import hashlib

    return hashlib.sha256(doc_path.read_bytes()).hexdigest()


def _find_pending_duplicate(doc_path: Path, *, sha256: str | None = None) -> Path | None:
    sha256 = sha256 or _pending_file_sha256(doc_path)
    orig_name = doc_path.name.lower()
    for existing in _pending_data_files():
        meta = read_sidecar(existing)
        if str(meta.get("sha256") or "") == sha256:
            return existing
        existing_orig = Path(str(meta.get("original_path") or existing.name)).name.lower()
        if existing_orig == orig_name:
            return existing
    return None


def dedupe_pending_child_documents(*, log_fn: Callable[[str], None] | None = None) -> int:
    """Remove duplicate pending-child rows (same original name or sha256). Keeps newest first_seen."""
    groups: dict[str, list[Path]] = {}
    for path in _pending_data_files():
        meta = read_sidecar(path)
        key = str(meta.get("sha256") or Path(str(meta.get("original_path") or path.name)).name.lower())
        groups.setdefault(key, []).append(path)

    removed = 0
    for paths in groups.values():
        if len(paths) <= 1:
            continue
        paths.sort(key=lambda p: str(read_sidecar(p).get("first_seen") or ""), reverse=True)
        for dup in paths[1:]:
            try:
                dup.unlink()
                _meta_path(dup).unlink(missing_ok=True)
                removed += 1
                if log_fn:
                    log_fn(f"  Removed duplicate pending child: {dup.name}")
            except OSError:
                pass
    return removed


def route_child_to_pending(
    doc_path: Path,
    *,
    child_type: str | None = None,
    reason: str = PENDING_REASON,
    hints: dict[str, Any] | None = None,
) -> Path | None:
    from supplement_content_classifier import classify_supplement_file, is_deletable

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    doc_path = Path(doc_path)
    if not doc_path.exists():
        return None

    sha256 = _pending_file_sha256(doc_path)
    existing = _find_pending_duplicate(doc_path, sha256=sha256)
    if existing and existing.exists():
        meta = read_sidecar(existing)
        meta["match_attempts"] = int(meta.get("match_attempts") or 0) + 1
        meta["last_checked"] = _now_iso()
        meta["decision_reason"] = reason
        if not meta.get("sha256"):
            meta["sha256"] = sha256
        write_sidecar(existing, meta)
        try:
            doc_path.unlink()
        except OSError as e:
            log_error(str(doc_path), "CHILD_DOC_PENDING", f"duplicate consume failed: {e}")
            return None
        return existing

    classified = classify_supplement_file(doc_path)
    if is_deletable(classified.get("classification", "")):
        _delete_corrupt_pending_child(
            doc_path,
            f"pending_child_route_rejected:{classified.get('classification')}",
        )
        return None

    from review_resolution import delete_confirmed_duplicate, ResolutionAudit

    dup = delete_confirmed_duplicate(doc_path, audit=ResolutionAudit(), dry_run=False)
    if dup.get("decision") == "DELETE_DUPLICATE":
        return None

    child_type = child_type or detect_child_document_type(doc_path.name) or "supplement"
    dest = safe_destination_for_file(
        doc_path,
        PENDING_DIR,
        reason=reason,
        max_filename=MAX_FILENAME - len(".meta.json"),
    )
    try:
        shutil.move(str(doc_path), str(dest))
        sniff = classified.get("sniff") or {}
        meta = ensure_sidecar(dest, child_type=child_type, original_path=str(doc_path), hints=hints or extract_parent_hints(dest.name))
        meta["match_attempts"] = int(meta.get("match_attempts") or 0) + 1
        meta["decision_reason"] = reason
        meta["content_classification"] = classified.get("classification")
        meta["file_type"] = doc_path.suffix.lstrip(".") or sniff.get("detected_kind")
        meta["actual_content_type"] = sniff.get("detected_kind")
        meta["sha256"] = sha256
        if classified.get("classification") == "MODEL_RECOVERY_REQUIRED":
            meta["status"] = "model_recovery_required"
        write_sidecar(dest, meta)
        log_error(str(dest), "CHILD_DOC_PENDING", reason)
        return dest
    except OSError as e:
        log_error(str(doc_path), "CHILD_DOC_PENDING", f"move failed: {e}")
        return None


route_supplement_to_pending = route_child_to_pending


def handle_staging_child_document(
    pdf_path: Path,
    ctx: ChildDocumentRunContext,
    *,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    pdf_path = Path(pdf_path)
    name = pdf_path.name
    from file_disposition_engine import disposition_file

    result = disposition_file(pdf_path, source_context="staging", child_ctx=ctx)
    action = str(result.get("action") or "")

    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    if action == "LINK_CHILD_TO_PARENT" and result.get("linked_parent"):
        parent_id = result.get("linked_parent")
        child_type = detect_child_document_type(name) or "supplement"
        key = _linked_count_key(child_type)
        ctx.bump(key)
        ctx.bump("child_documents_linked_this_run")
        _log(f"  Child document ({child_type}) linked to parent '{parent_id}'")
        return {
            "status": key,
            "child_type": child_type,
            "reason": f"linked to parent {parent_id}",
            "paper_id": parent_id,
            "stage": "child_document_check",
        }

    if action == "HOLD_PENDING_PARENT":
        child_type = detect_child_document_type(name) or "supplement"
        ctx.bump("child_document_pending_parent")
        ctx.bump("child_documents_moved_to_pending")
        ctx.bump("supplement_pending_parent")
        dest = result.get("dest")
        _log(f"  No parent matched -> {PENDING_DIR.name}/{Path(dest).name if dest else name}")
        _log(f"  {PENDING_REASON}")
        return {
            "status": "child_document_pending_parent",
            "child_type": child_type,
            "reason": PENDING_REASON,
            "review_path": dest,
            "stage": "child_document_check",
            "legacy_status": "supplement_pending_parent",
        }

    if action in {"DELETE_CONFIRMED_DUPLICATE", "DELETE_CONFIRMED_JUNK", "DELETE_TRUE_CORRUPT_UNUSABLE"}:
        return {
            "status": "deleted_junk",
            "reason": action,
            "stage": "child_document_check",
        }

    if action in {"INGEST_RESEARCH_PAPER", "INGEST_NON_RATABLE_REFERENCE"}:
        return {"status": "needs_ingest", "disposition_action": action, "stage": "child_document_check"}

    return {"status": "failed", "reason": f"disposition:{action}", "stage": "child_document_check"}


handle_staging_supplement = handle_staging_child_document


def retry_pending_child_documents(
    ctx: ChildDocumentRunContext,
    *,
    log_fn: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    verbose: bool = False,
    candidate_paths: list[Path] | None = None,
    all_meta: list[dict] | None = None,
) -> int:
    """Fast sidecar-only pending match. Full folder scan only when candidate_paths is None."""
    if not PENDING_DIR.exists():
        return 0
    linked = 0
    pdfs = candidate_paths if candidate_paths is not None else _pending_data_files()
    total = len(pdfs)
    if all_meta is None:
        all_meta = papers_db.get_all_metadata()
    for idx, pdf_path in enumerate(pdfs, 1):
        if should_stop and should_stop():
            break
        if on_progress:
            on_progress({"index": idx, "total": total, "file": pdf_path.name})
        if verbose and log_fn:
            log_fn(f"  Checking pending child/support doc {idx}/{total}: {pdf_path.name}")
        ctx.bump("pending_child_startup_checked")
        ctx.bump("pending_child_inspected")
        meta = read_sidecar(pdf_path)
        meta["last_checked"] = _now_iso()
        meta["match_attempts"] = int(meta.get("match_attempts") or 0) + 1
        write_sidecar(pdf_path, meta)
        child_type = meta.get("child_type") or detect_child_document_type(pdf_path.name) or "supplement"
        parent = find_parent_for_child(pdf_path.name, ctx, all_meta=all_meta)
        if not parent:
            continue
        if link_child_to_parent(pdf_path, parent, child_type=child_type):
            key = _linked_count_key(child_type)
            ctx.bump("child_document_linked_from_pending")
            ctx.bump("supplement_linked_from_pending")
            ctx.bump("child_documents_linked_this_run")
            ctx.bump(key)
            linked += 1
            if verbose and log_fn:
                log_fn(f"  Pending child document linked to '{parent.get('id')}': {pdf_path.name}")
    if log_fn and not verbose:
        log_fn(f"Pending child/support match: checked {total} sidecar(s), linked {linked}")
    return linked


def match_pending_children_for_parent(
    parent_id: str,
    ctx: ChildDocumentRunContext,
    *,
    parent: dict | None = None,
    log_fn: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Targeted pending match for one newly ingested parent — sidecar filter only."""
    parent = parent or papers_db.get_paper(parent_id)
    if not parent:
        return {"parent_id": parent_id, "checked": 0, "linked": 0, "skipped": "parent_not_found"}
    ctx.register_successful_parent(parent_id)
    candidates: list[Path] = []
    for pdf_path in _pending_data_files():
        meta = read_sidecar(pdf_path)
        if _sidecar_may_match_parent(meta, pdf_path.name, parent):
            candidates.append(pdf_path)
    if not candidates:
        if log_fn and not verbose:
            log_fn(f"Pending match for parent {parent_id}: checked 0 candidates, linked 0")
        return {"parent_id": parent_id, "checked": 0, "linked": 0}
    all_meta = papers_db.get_all_metadata()
    linked = retry_pending_child_documents(
        ctx,
        log_fn=None,
        should_stop=should_stop,
        verbose=verbose,
        candidate_paths=candidates,
        all_meta=all_meta,
    )
    if log_fn and not verbose:
        log_fn(f"Pending match for parent {parent_id}: checked {len(candidates)} candidate(s), linked {linked}")
    return {"parent_id": parent_id, "checked": len(candidates), "linked": linked}


def match_pending_for_new_parents(
    parent_ids: list[str],
    ctx: ChildDocumentRunContext,
    *,
    log_fn: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run targeted pending match for each newly added parent row."""
    results: list[dict[str, Any]] = []
    total_linked = 0
    for parent_id in parent_ids:
        if should_stop and should_stop():
            break
        if not parent_id:
            continue
        rec = match_pending_children_for_parent(
            parent_id,
            ctx,
            log_fn=log_fn,
            should_stop=should_stop,
            verbose=verbose,
        )
        results.append(rec)
        total_linked += int(rec.get("linked") or 0)
    return {"parents": len(parent_ids), "linked": total_linked, "results": results}


retry_pending_supplements = retry_pending_child_documents


def migrate_legacy_holding_folders(*, log_fn: Callable[[str], None] | None = None) -> int:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    for src_dir in LEGACY_MIGRATION_DIRS:
        if not src_dir.exists():
            continue
        for pdf in sorted(src_dir.glob("*.pdf")):
            dest = safe_destination_for_file(
                pdf,
                PENDING_DIR,
                reason="legacy_holding_migration",
                max_filename=MAX_FILENAME - len(".meta.json"),
            )
            try:
                shutil.move(str(pdf), str(dest))
                old_meta = src_dir / f"{pdf.name}.meta.json"
                if old_meta.exists():
                    shutil.move(str(old_meta), str(_meta_path(dest)))
                else:
                    ensure_sidecar(
                        dest,
                        child_type=detect_child_document_type(dest.name) or "supplement",
                        original_path=str(src_dir / pdf.name),
                    )
                moved += 1
                if log_fn:
                    log_fn(f"  Migrated {pdf.name} from {src_dir.name} -> pending-parent-child-documents/")
            except OSError as e:
                if log_fn:
                    log_fn(f"  Could not migrate {pdf.name}: {e}")
    return moved


migrate_unlinked_to_pending = migrate_legacy_holding_folders


def migrate_unowned_child_docs_from_source_pdfs(*, log_fn: Callable[[str], None] | None = None) -> int:
    """Move unowned parent-dependent child PDFs out of source-pdfs into pending holding."""
    from source_pdf_ownership import build_audit

    audit = build_audit()
    unowned = audit.get("unowned_source_pdfs") or []
    moved = 0
    for rel in unowned:
        name = Path(rel).name
        if not is_child_document(name) and not name.lower().startswith("unidentified_"):
            continue
        src = config.MASTER_PROJECT / rel.replace("/", "\\") if "\\" not in rel else config.MASTER_PROJECT / rel
        if not src.exists():
            src = config.SOURCE_PDFS / name
        if not src.exists():
            continue
        child_type = detect_child_document_type(name) or "supplement"
        dest = route_child_to_pending(src, child_type=child_type, reason="migrated from unowned source-pdfs")
        if dest:
            moved += 1
            if log_fn:
                log_fn(f"  Moved unowned child doc from source-pdfs: {name}")
    return moved


def quarantine_non_child_unowned_source_pdfs(*, log_fn: Callable[[str], None] | None = None) -> int:
    from source_pdf_ownership import build_audit

    quarantine = config.REVIEW_DIR / "quarantine-orphan-source"
    quarantine.mkdir(parents=True, exist_ok=True)
    audit = build_audit()
    moved = 0
    for rel in audit.get("unowned_source_pdfs") or []:
        name = Path(rel).name
        if is_child_document(name) or name.lower().startswith("unidentified_"):
            continue
        src = config.SOURCE_PDFS / name
        if not src.exists():
            continue
        dest = safe_destination_for_file(src, quarantine, reason="orphan_source_quarantine")
        try:
            shutil.move(str(src), str(dest))
            moved += 1
            if log_fn:
                log_fn(f"  Quarantined orphan source PDF: {name}")
        except OSError as e:
            if log_fn:
                log_fn(f"  Could not quarantine {name}: {e}")
    return moved


def scan_pending_at_run_start(
    ctx: ChildDocumentRunContext,
    *,
    log_fn: Callable[[str], None] | None = None,
    deep_audit: bool = False,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if should_stop and should_stop():
        stats = pending_folder_stats()
        return {
            "pending_child_start_mode": "fast",
            "pending_child_scan_stopped": True,
            "child_docs_migrated_from_legacy": 0,
            "child_docs_moved_from_source_pdfs": 0,
            "orphan_source_pdfs_quarantined": 0,
            "child_docs_linked_at_start": 0,
            "pending_child_start": stats["count"],
            "pending_child_inspected": 0,
            **stats,
            **ctx.to_summary(),
        }

    if not deep_audit:
        pdfs = _pending_data_files()
        if on_progress:
            on_progress({"index": 0, "total": len(pdfs), "file": ""})
        linked = retry_pending_child_documents(
            ctx,
            log_fn=log_fn,
            should_stop=should_stop,
            on_progress=on_progress,
            verbose=False,
        )
        stats = pending_folder_stats()
        return {
            "pending_child_start_mode": "fast",
            "pending_child_scan_stopped": bool(should_stop and should_stop()),
            "child_docs_migrated_from_legacy": 0,
            "child_docs_moved_from_source_pdfs": 0,
            "orphan_source_pdfs_quarantined": 0,
            "child_docs_linked_at_start": linked,
            "pending_child_start": len(pdfs),
            "pending_child_inspected": min(len(pdfs), int(ctx.counts.get("pending_child_startup_checked") or len(pdfs))),
            "pending_child_deep_audit_skipped": True,
            **stats,
            **ctx.to_summary(),
        }

    migrated = migrate_legacy_holding_folders(log_fn=log_fn)
    if should_stop and should_stop():
        stats = pending_folder_stats()
        return {
            "pending_child_start_mode": "deep",
            "pending_child_scan_stopped": True,
            "child_docs_migrated_from_legacy": migrated,
            **stats,
            **ctx.to_summary(),
        }
    moved_src = migrate_unowned_child_docs_from_source_pdfs(log_fn=log_fn)
    if should_stop and should_stop():
        stats = pending_folder_stats()
        return {
            "pending_child_start_mode": "deep",
            "pending_child_scan_stopped": True,
            "child_docs_migrated_from_legacy": migrated,
            "child_docs_moved_from_source_pdfs": moved_src,
            **stats,
            **ctx.to_summary(),
        }
    quarantined = quarantine_non_child_unowned_source_pdfs(log_fn=log_fn)
    pending_audit = audit_pending_child_documents(ctx, dry_run=False, log_fn=log_fn)
    linked = retry_pending_child_documents(
        ctx,
        log_fn=log_fn,
        should_stop=should_stop,
        on_progress=on_progress,
    )
    stats = pending_folder_stats()
    return {
        "pending_child_start_mode": "deep",
        "child_docs_migrated_from_legacy": migrated,
        "child_docs_moved_from_source_pdfs": moved_src,
        "orphan_source_pdfs_quarantined": quarantined,
        "child_docs_linked_at_start": linked,
        **pending_audit,
        **stats,
        **ctx.to_summary(),
    }


def finalize_pending_child_documents(
    ctx: ChildDocumentRunContext,
    *,
    run_completed_normally: bool,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "child_document_finalization": "skipped" if not run_completed_normally else "completed"
    }

    if not run_completed_normally:
        stats = pending_folder_stats()
        result.update(
            {
                "pending_child_documents_preserved": True,
                **stats,
                **ctx.to_summary(),
            }
        )
        if log_fn:
            log_fn(f"Run not fully completed — preserving {stats['count']} pending child document(s).")
        return result

    if not PENDING_DIR.exists():
        result.update(ctx.to_summary())
        return result

    now = datetime.now(timezone.utc)
    for pdf_path in _pending_data_files():
        meta = read_sidecar(pdf_path)
        first_seen = meta.get("first_seen") or _now_iso()
        try:
            seen = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            age_days = (now - seen).days
        except ValueError:
            age_days = 0

        if age_days < EXPIRY_DAYS:
            continue

        try:
            pdf_path.unlink()
            _meta_path(pdf_path).unlink(missing_ok=True)
            ctx.bump("child_document_deleted_expired_no_parent")
            log_error(
                str(pdf_path),
                "CHILD_DOC_EXPIRED",
                f"no parent matched after {EXPIRY_DAYS} days in pending-parent-child-documents",
            )
            if log_fn:
                log_fn(f"  Deleted expired pending child document ({age_days}d): {pdf_path.name}")
        except OSError as e:
            if log_fn:
                log_fn(f"  Could not delete expired pending child document {pdf_path.name}: {e}")

    result.update(ctx.to_summary())
    return result


finalize_pending_supplements = finalize_pending_child_documents


def handle_supplement_file(doc_path: Path) -> bool:
    doc_path = Path(doc_path)
    from supplement_content_classifier import classify_supplement_file, is_deletable

    classified = classify_supplement_file(doc_path)
    if is_deletable(classified.get("classification", "")):
        _delete_corrupt_pending_child(
            doc_path,
            f"child_document_deleted_at_ingest:{classified.get('classification')}",
        )
        return False
    child_type = detect_child_document_type(doc_path.name) or "supplement"
    parent = find_parent_for_child(doc_path.name)
    if parent:
        link_child_to_parent(doc_path, parent, child_type=child_type)
        return False
    route_child_to_pending(doc_path, child_type=child_type)
    return False


empty_supplement_counts = empty_child_counts
