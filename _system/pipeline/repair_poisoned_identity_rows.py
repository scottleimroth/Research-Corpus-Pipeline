#!/usr/bin/env python3
"""Repair DB rows poisoned by PII titles or bad identity from stopped runs."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import config  # noqa: E402
import papers_db  # noqa: E402
from add_paper import generate_filename, generate_id  # noqa: E402
from document_evidence import (  # noqa: E402
    build_evidence_package,
    is_invalid_canonical_title,
    recover_title_from_filename,
)
from path_governance import reserve_unique_path, safe_runtime_rename  # noqa: E402

REPAIR_RUN_PREFIX = "2026-05-31T11"
REPORT_PATH = config.LOGS_DIR / "recovery" / "poisoned_identity_repair_report.json"
# First mistaken repair renamed source PDFs on disk while DB was later restored.
DISK_RENAME_ALIASES = {
    "barry_1991_pii_.pdf": "serial_1991_barry__pii.pdf",
    "barry_unknown_pii_.pdf": "rj_1993_barry_unknown_pii.pdf",
    "barry_1988_pii_.pdf": "elsevier_1988_barry__pii.pdf",
    "barry_1987_pii_.pdf": "elsevier_1987_barry__pii.pdf",
    "barry_1998_pii_s.pdf": "barry_1998_barry__pii.pdf",
    "barry_unknown_pii_s.pdf": "barry_1998_barry_unknown_pii.pdf",
    "barry_1998_pii_s_2.pdf": "journal_1998_barry__pii.pdf",
    "anderson_1995_pii_d.pdf": "research_1995_anderson__pii.pdf",
    "northholland_1987_pii_.pdf": "northholland_1987_northholland__pii.pdf",
    "sokolov_2000_pii_s.pdf": "sokolov_2000_sokolov__pii.pdf",
    "unknown_unknown_pii_s_4.pdf": "unknown_1998_structures_episodic_memory_functions.pdf",
}


def _title_from_paper_id(paper_id: str) -> str | None:
    pid = re.sub(r"_b$", "", str(paper_id or ""))
    if "_" not in pid:
        return None
    author, rest = pid.split("_", 1)
    if rest.upper() == "PII" or rest.lower().endswith("_pii"):
        return None
    title = rest.replace("_", " ").strip()
    if len(title) >= 12 and not is_invalid_canonical_title(title)[0]:
        return title
    return None


def _is_poisoned(paper: dict) -> tuple[bool, str]:
    bm = paper.get("basic_metadata") or {}
    fi = paper.get("file_info") or {}
    cl = paper.get("classification") or {}
    title = str(bm.get("title") or "")
    fn = str(fi.get("renamed_filename") or "")
    pid = str(paper.get("id") or "")
    rating = str(cl.get("rating") or "").lower()
    ts = str(paper.get("timestamp_added") or "")
    from_run = ts.startswith(REPAIR_RUN_PREFIX)

    if not from_run:
        if title.upper().startswith("PII:"):
            return True, "pii_title"
        return False, ""

    if title.upper().startswith("PII:") or is_invalid_canonical_title(title)[0]:
        return True, "invalid_title"
    if "pii" in fn.lower():
        return True, "pii_filename"
    if "pii" in pid.lower():
        return True, "pii_id"
    if rating in {"not_ratable_reference_material", "not_applicable"} and (
        title.upper().startswith("PII:")
        or is_invalid_canonical_title(title)[0]
        or "pii" in fn.lower()
    ):
        return True, f"bad_rating:{rating}"
    return False, ""


def _source_path(paper: dict) -> Path | None:
    fi = paper.get("file_info") or {}
    rel = fi.get("filepath") or ""
    if rel:
        p = config.MASTER_PROJECT / rel.replace("/", "\\")
        if p.exists():
            return p
    name = fi.get("renamed_filename")
    if name:
        p = config.SOURCE_PDFS / name
        if p.exists():
            return p
        alias = DISK_RENAME_ALIASES.get(str(name).lower())
        if alias:
            alt = config.SOURCE_PDFS / alias
            if alt.exists():
                return alt
    return None


def _recover_identity(paper: dict, src: Path, pkg) -> tuple[str | None, list, str | None, str | None]:
    bm = paper.get("basic_metadata") or {}
    fi = paper.get("file_info") or {}
    old_title = str(bm.get("title") or "")

    if old_title and not is_invalid_canonical_title(old_title)[0]:
        return old_title, list(bm.get("authors") or []), bm.get("year"), "existing_valid_title"

    for label, name in (
        ("original_filename", fi.get("original_filename")),
        ("source_filename", src.name),
        ("paper_id", paper.get("id")),
    ):
        if not name:
            continue
        if label == "paper_id":
            title = _title_from_paper_id(str(name))
            if title:
                return title, list(bm.get("authors") or []), bm.get("year"), "recovered_from_paper_id"
            continue
        hint = str(name)
        fn_rec = recover_title_from_filename(Path(hint))
        title = fn_rec.get("title")
        if title and not is_invalid_canonical_title(title)[0]:
            return (
                title,
                list(fn_rec.get("authors") or bm.get("authors") or []),
                fn_rec.get("year") or bm.get("year"),
                f"recovered_from_{label}",
            )

    ident = pkg.selected_identity or {}
    title = ident.get("title")
    if title and not is_invalid_canonical_title(title)[0]:
        return (
            title,
            list(ident.get("authors") or bm.get("authors") or []),
            ident.get("year") or bm.get("year"),
            str(ident.get("title_source") or "evidence_package"),
        )
    return None, list(bm.get("authors") or []), bm.get("year"), None


def repair(*, execute: bool = False) -> dict:
    if execute:
        papers_db.backup(suffix="pre-poisoned-identity-repair")
    rows_inspected = 0
    rows_repaired = 0
    rows_reeval = 0
    source_renamed = 0
    unresolved: list[dict] = []
    details: list[dict] = []

    for paper in papers_db.get_all_metadata():
        rows_inspected += 1
        poisoned, reason = _is_poisoned(paper)
        if not poisoned:
            continue

        pid = paper["id"]
        src = _source_path(paper)
        rec = {"paper_id": pid, "reason": reason, "status": "unresolved"}
        if not src:
            rec["error"] = "source_pdf_missing"
            unresolved.append(rec)
            details.append(rec)
            continue

        full_paper = papers_db.get_paper(pid) or paper
        pkg = build_evidence_package(
            src,
            args={"extract_metadata": True, "no_claude": True, "no_ollama": True},
            attempt_ocr=False,
        )
        new_title, authors, year, title_source = _recover_identity(full_paper, src, pkg)
        if not new_title:
            rec["error"] = "no_recoverable_title"
            unresolved.append(rec)
            details.append(rec)
            continue

        bm = dict(full_paper.get("basic_metadata") or {})
        old_title = bm.get("title")
        bm["title"] = new_title
        bm["authors"] = authors
        if year:
            bm["year"] = year
        bm["title_source"] = title_source
        full_paper["basic_metadata"] = bm

        ident = pkg.selected_identity or {}
        ids = dict(full_paper.get("identifiers") or {})
        if ident.get("pii"):
            ids["pii"] = ident["pii"]
        if ident.get("doi"):
            ids["doi"] = ident["doi"]
            bm["doi"] = ident["doi"]
        full_paper["identifiers"] = ids

        fi = dict(full_paper.get("file_info") or {})
        orig_name = fi.get("original_filename") or src.name
        new_id = generate_id(authors, year, new_title, pdf_filename=str(orig_name))
        new_fn = generate_filename(authors, year, new_title, original_filename=str(orig_name))
        old_fn = fi.get("renamed_filename")

        cl = dict(full_paper.get("classification") or {})
        old_rating = cl.get("rating")
        ts = str(full_paper.get("timestamp_added") or "")
        if ts.startswith(REPAIR_RUN_PREFIX) and str(old_rating or "").lower() in {
            "not_ratable_reference_material",
            "not_applicable",
        }:
            cl["rating"] = None
            cl["guard_blocked_rating"] = old_rating
            full_paper["classification"] = cl
            notes = dict(full_paper.get("notes_and_flags") or {})
            notes["reevaluation_required"] = reason
            full_paper["notes_and_flags"] = notes
            rows_reeval += 1

        rec.update(
            {
                "old_title": old_title,
                "new_title": new_title,
                "old_id": pid,
                "new_id": new_id,
                "old_filename": old_fn,
                "new_filename": new_fn,
                "title_source": title_source,
            }
        )

        if execute:
            dest = reserve_unique_path(config.SOURCE_PDFS, new_fn, existing_ok=src)
            if dest.resolve() != src.resolve():
                safe_runtime_rename(src, dest, sidecars=[])
                source_renamed += 1
                new_fn = dest.name
            fi["renamed_filename"] = new_fn
            fi["filepath"] = f"source-pdfs/{new_fn}"
            full_paper["file_info"] = fi
            if new_id != pid and not papers_db.paper_exists(new_id):
                conn = papers_db.get_connection()
                try:
                    conn.execute("DELETE FROM papers WHERE id = ?", (pid,))
                    conn.commit()
                finally:
                    conn.close()
                full_paper["id"] = new_id
            papers_db.save_paper(full_paper)
            rec["status"] = "repaired"
            rows_repaired += 1
        else:
            rec["status"] = "would_repair"
            rows_repaired += 1
        details.append(rec)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execute": execute,
        "rows_inspected": rows_inspected,
        "rows_repaired": rows_repaired,
        "rows_marked_for_reevaluation": rows_reeval,
        "source_files_renamed": source_renamed,
        "unresolved_count": len(unresolved),
        "details": details,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def restore_db(backup_path: Path) -> None:
    import shutil

    shutil.copy2(backup_path, config.DB_PATH)


def cleanup_repair_orphans(*, execute: bool = False) -> dict:
    """Remove or restore orphan PDFs left by mistaken repair passes."""
    import hashlib
    import shutil

    restore_map = {
        "barry_2013_ssmain.pdf": "barry_2013_psychophysiology_australasia.pdf",
        "bolton_1894_american_journal_psychology.pdf": "bolton_unknown_source_american_journal_psychology.pdf",
        "bolton_1894_rhythm_bolton.pdf": "rhythm_unknown_source_american_journal_psychology.pdf",
        "krumhansl_1997_proceedi.pdf": "krumhansl_1997b_psychophysiology_musical_emotions.pdf",
        "acar_2023_review_paper_reporting_practices_task_2.pdf": "acar_2023_review_paper_reporting_practices_task.pdf",
        "al_unknown_guidelines_eeg_meg_authors_checklist.pdf": "checklist_unknown_important_guidelines_psychophysiology.pdf",
        "av_2014_occupational_noise_management_part_auditory.pdf": "as_2014_occupational_noise_management_part_audi.pdf",
        "sokolov_2000_sokolov__pii.pdf": "sokolov_2000_pii_s.pdf",
        "unknown_unknown_unidentified.pdf": "unidentified_22.pdf",
        "unknown_unknown_unknown_unknown_rrhrv.pdf": "unknown_unknown_rrhrv.pdf",
    }
    delete_only = {
        "acar_2023_review_paper_reporting_practices_task_3.pdf",
        "al_unknown_checklist_unknown_important_guidelines_psychophysiology.pdf",
        "as_2014__occupational_noise_management_part.pdf",
    }

    restored = []
    deleted = []
    for src_name, dest_name in restore_map.items():
        src = config.SOURCE_PDFS / src_name
        dest = reserve_unique_path(config.SOURCE_PDFS, dest_name, existing_ok=src)
        if not src.exists() or dest.exists():
            continue
        if execute:
            shutil.move(str(src), str(dest))
        restored.append({"from": src_name, "to": dest_name})

    for name in delete_only:
        path = config.SOURCE_PDFS / name
        if not path.exists():
            continue
        if execute:
            path.unlink()
        deleted.append(name)

    return {"execute": execute, "restored": restored, "deleted": deleted}


def main() -> int:
    if "--restore" in sys.argv:
        backup = config.BACKUP_DIR / "papers-20260531-115932-pre-poisoned-identity-repair.db"
        if not backup.exists():
            backups = sorted(config.BACKUP_DIR.glob("papers-*-pre-poisoned-identity-repair.db"))
            backup = backups[-1] if backups else None
        if not backup or not backup.exists():
            print("No pre-repair backup found", file=sys.stderr)
            return 1
        restore_db(backup)
        print(f"Restored DB from {backup}")
        return 0

    if "--cleanup-orphans" in sys.argv:
        report = cleanup_repair_orphans(execute="--execute" in sys.argv)
        print(json.dumps(report, indent=2))
        return 0

    execute = "--execute" in sys.argv
    report = repair(execute=execute)
    print(json.dumps({k: v for k, v in report.items() if k != "details"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
