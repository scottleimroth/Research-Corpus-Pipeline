"""Resolve duplicate/orphan/review bloat: delete confirmed dupes, resolve ambiguous via checks + Opus."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import papers_db
from path_governance import MAX_FILENAME, safe_destination_for_file

DECISIONS = frozenset(
    {
        "DELETE_DUPLICATE",
        "KEEP_AND_PROCESS",
        "DELETE_JUNK",
        "MOVE_TO_STAGING_FOR_PROCESSING",
        "TECHNICAL_FAILURE_KEEP_IN_REVIEW",
    }
)

TECHNICAL_REVIEW = config.REVIEW_TECHNICAL_FAILURE
RESOLUTION_LOG_DIR = config.REVIEW_RESOLUTION_LOG_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class ResolutionAudit:
    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id or f"review-resolution-{datetime.now():%Y%m%d-%H%M%S}"
        self.log_dir = RESOLUTION_LOG_DIR / self.run_id
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.log_dir / "actions.jsonl"
        self.summary: dict[str, Any] = {
            "run_id": self.run_id,
            "created_at": _now(),
            "confirmed_duplicates_deleted": 0,
            "ambiguous_resolved": 0,
            "moved_to_staging": 0,
            "deleted_junk": 0,
            "technical_failures": 0,
            "opus_calls": 0,
            "opus_input_tokens": 0,
            "opus_output_tokens": 0,
            "actions": [],
        }

    def record(self, **fields: Any) -> None:
        entry = {"at": _now(), **fields}
        self.summary["actions"].append(entry)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def bump(self, key: str, n: int = 1) -> None:
        self.summary[key] = int(self.summary.get(key, 0)) + n

    def finalize(self) -> dict[str, Any]:
        self.summary["finished_at"] = _now()
        out = self.log_dir / "summary.json"
        out.write_text(json.dumps(self.summary, indent=2, default=str), encoding="utf-8")
        self.summary["summary_path"] = str(out)
        return self.summary


def _source_hash_index() -> dict[str, list[dict[str, str]]]:
    index: dict[str, list[dict[str, str]]] = {}
    if not config.SOURCE_PDFS.exists():
        return index
    for path in config.SOURCE_PDFS.glob("*.pdf"):
        try:
            digest = _sha256(path)
            index.setdefault(digest, []).append({"path": str(path), "name": path.name})
        except OSError:
            continue
    return index


def _staging_hash_index() -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    if not config.STAGING.exists():
        return index
    for path in config.STAGING.glob("*.pdf"):
        try:
            index.setdefault(_sha256(path), []).append(path.name)
        except OSError:
            continue
    return index


def _other_staging_paths_for_hash(digest: str, exclude: Path | None = None) -> list[str]:
    """Return other staging filenames with the same hash (never the file itself)."""
    digest = (digest or "").strip().lower()
    if not digest:
        return []
    names = list(_staging_hash_index().get(digest) or [])
    if exclude is None:
        return names
    ex_name = exclude.name
    return [name for name in names if name != ex_name]


def is_staging_duplicate_of_other(path: Path, digest: str | None = None) -> bool:
    """True when another staging PDF shares this hash."""
    path = Path(path)
    digest = (digest or "").strip().lower() or _sha256(path)
    return bool(_other_staging_paths_for_hash(digest, exclude=path))


def _unowned_source_rel_set() -> set[str]:
    try:
        from source_pdf_ownership import build_audit

        audit = build_audit()
        return {str(p).lower().replace("\\", "/") for p in audit.get("unowned_source_pdfs") or []}
    except Exception:
        return set()


def _canonical_owner_for_hash(digest: str, hash_index: dict | None = None) -> dict[str, Any] | None:
    index = hash_index or _source_hash_index()
    owners = index.get(digest) or []
    if not owners:
        return None
    unowned = _unowned_source_rel_set()
    for owner in owners:
        path = Path(owner["path"])
        if not path.exists():
            continue
        try:
            rel = str(path.relative_to(config.MASTER_PROJECT)).replace("\\", "/").lower()
        except ValueError:
            rel = f"source-pdfs/{path.name}".lower()
        if rel in unowned:
            continue
        return {"canonical_path": path, "canonical_name": owner["name"], "digest": digest}
    return None


def _is_valid_pdf(path: Path) -> tuple[bool, str]:
    try:
        if path.stat().st_size < 500:
            return False, "file_too_small"
        with path.open("rb") as f:
            header = f.read(8)
        if not header.startswith(b"%PDF"):
            return False, "invalid_pdf_header"
        return True, ""
    except OSError as e:
        return False, f"os_error:{e}"


def _extract_text(path: Path, *, pages: int | None = None) -> tuple[str, str | None]:
    from document_text_extraction import extract_document_text, normalize_extracted_text

    result = extract_document_text(path, pages=pages, attempt_ocr=False)
    if result.extractor_status in {"EXTRACTOR_UNAVAILABLE", "EXTRACTOR_CRASHED", "WRONG_PATH_OR_MISSING_FILE"}:
        return "", result.errors[0] if result.errors else result.extractor_status
    text = normalize_extracted_text(result.text)
    if text:
        return text, None
    if result.errors:
        return "", result.errors[0]
    return "", result.extractor_status


def _pdf_page_count(path: Path) -> int | None:
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            return None
        for line in (result.stdout or "").splitlines():
            if line.lower().startswith("pages:"):
                return int(line.split(":", 1)[1].strip())
    except (ValueError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _db_papers_index() -> list[dict[str, Any]]:
    papers = []
    for paper in papers_db.get_all_metadata():
        fi = paper.get("file_info") or {}
        bm = paper.get("basic_metadata") or {}
        papers.append(
            {
                "id": paper.get("id"),
                "title": bm.get("title") or "",
                "authors": bm.get("authors") or [],
                "year": str(bm.get("year") or ""),
                "doi": (bm.get("doi") or "").lower().strip(),
                "renamed_filename": fi.get("renamed_filename") or "",
                "original_filename": fi.get("original_filename") or "",
            }
        )
    return papers


def _find_candidate_matches(pdf_path: Path, *, papers: list[dict] | None = None) -> list[dict[str, Any]]:
    papers = papers or _db_papers_index()
    low_name = pdf_path.name.lower()
    stem_norm = _norm(pdf_path.stem)
    years = re.findall(r"(19|20)\d{2}", pdf_path.stem)
    year_hint = years[0] if years else ""
    matches: list[dict[str, Any]] = []
    for p in papers:
        score = 0
        reasons: list[str] = []
        if p["original_filename"].lower() == low_name or p["renamed_filename"].lower() == low_name:
            score += 3
            reasons.append("filename_exact")
        title_norm = _norm(p["title"])
        if title_norm and len(title_norm) > 15 and (title_norm in stem_norm or stem_norm in title_norm):
            score += 2
            reasons.append("title_overlap")
        if year_hint and p["year"] == year_hint:
            score += 1
            reasons.append("year_match")
        if score >= 2:
            matches.append({"paper": p, "score": score, "reasons": reasons})
    matches.sort(key=lambda m: m["score"], reverse=True)
    return matches[:5]


def _dupe_signals_from_paths(candidate: Path, canonical: Path, paper: dict) -> list[str]:
    from add_paper import _author_overlap, _compute_dupe_signals, _is_confirmed_dupe

    text, _ = _extract_text(candidate, pages=2)
    doi = None
    m = re.search(r"10\.\d{4,9}/[^\s]+", text)
    if m:
        doi = m.group(0).rstrip(".,)")
    title = paper.get("title") or ""
    authors = paper.get("authors") or []
    year = paper.get("year") or ""
    abstract = ""
    if text:
        abstract = text[:2000]
    signals = _compute_dupe_signals(
        candidate,
        {"id": paper.get("id"), "file_info": {"renamed_filename": paper.get("renamed_filename")}, "basic_metadata": paper, "abstract_keywords": {"abstract": ""}},
        incoming_doi=doi,
        incoming_title=title,
        incoming_authors=authors,
        incoming_year=year,
        incoming_abstract=abstract,
    )
    if _sha256(candidate) == _sha256(canonical):
        if "content_hash" not in signals:
            signals.insert(0, "content_hash")
    return signals


def _text_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    a_trim = a[:12000]
    b_trim = b[:12000]
    return SequenceMatcher(None, a_trim, b_trim).ratio()


def _deterministic_decision(pdf_path: Path, *, hash_index: dict | None = None) -> tuple[str | None, dict[str, Any]]:
    """Return (decision, evidence) or (None, evidence) if inconclusive."""
    evidence: dict[str, Any] = {"path": str(pdf_path)}
    ok, reason = _is_valid_pdf(pdf_path)
    if not ok:
        if reason in {"file_too_small", "invalid_pdf_header"}:
            return "DELETE_JUNK", {**evidence, "reason": reason}
        return "TECHNICAL_FAILURE_KEEP_IN_REVIEW", {**evidence, "reason": reason}

    digest = _sha256(pdf_path)
    evidence["sha256"] = digest
    owner = _canonical_owner_for_hash(digest, hash_index)
    if owner:
        return "DELETE_DUPLICATE", {**evidence, "canonical": owner["canonical_name"], "match": "hash_identical"}

    staging_others = _other_staging_paths_for_hash(digest, exclude=pdf_path)
    if staging_others:
        return "DELETE_DUPLICATE", {**evidence, "match": "staging_hash_identical", "staging_files": staging_others}

    text, err = _extract_text(pdf_path)
    evidence["text_length"] = len(text)
    evidence["extract_error"] = err
    if err and len(text) < 200:
        return "TECHNICAL_FAILURE_KEEP_IN_REVIEW", {**evidence, "reason": "extraction_failed"}

    if len(text) < 80 and pdf_path.stat().st_size < 5000:
        return "DELETE_JUNK", {**evidence, "reason": "insufficient_content"}

    matches = _find_candidate_matches(pdf_path)
    evidence["candidate_matches"] = [{"id": m["paper"]["id"], "score": m["score"], "reasons": m["reasons"]} for m in matches]

    if matches:
        best = matches[0]["paper"]
        canonical_path = config.SOURCE_PDFS / best["renamed_filename"] if best.get("renamed_filename") else None
        if canonical_path and canonical_path.exists():
            signals = _dupe_signals_from_paths(pdf_path, canonical_path, best)
            evidence["dupe_signals"] = signals
            from add_paper import _is_confirmed_dupe

            if _is_confirmed_dupe(signals):
                return "DELETE_DUPLICATE", {**evidence, "canonical_id": best["id"], "match": "metadata_signals"}

            canon_text, _ = _extract_text(canonical_path)
            sim = _text_similarity(text, canon_text)
            evidence["text_similarity"] = round(sim, 4)
            if sim >= 0.92:
                return "DELETE_DUPLICATE", {**evidence, "canonical_id": best["id"], "match": "full_text_similarity"}
            if sim <= 0.35 and len(text) >= 500:
                return "MOVE_TO_STAGING_FOR_PROCESSING", {**evidence, "reason": "distinct_paper_by_text"}

    if len(text) >= 500:
        return "MOVE_TO_STAGING_FOR_PROCESSING", {**evidence, "reason": "valid_unprocessed_no_strong_dupe_match"}

    return None, evidence


def _opus_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return round(
        (input_tokens / 1_000_000) * config.OPUS_INPUT_USD_PER_MTOK
        + (output_tokens / 1_000_000) * config.OPUS_OUTPUT_USD_PER_MTOK,
        4,
    )


def _opus_decision(pdf_path: Path, evidence: dict[str, Any], *, audit: ResolutionAudit) -> tuple[str, dict[str, Any]]:
    if not config.ANTHROPIC_API_KEY:
        return "TECHNICAL_FAILURE_KEEP_IN_REVIEW", {**evidence, "reason": "opus_unavailable_for_resolution"}

    try:
        import anthropic
    except ImportError:
        return "TECHNICAL_FAILURE_KEEP_IN_REVIEW", {**evidence, "reason": "opus_unavailable_for_resolution"}

    candidate_text, _ = _extract_text(pdf_path)
    candidate_text = candidate_text[:15000]
    canonical_block = ""
    for m in evidence.get("candidate_matches") or []:
        paper = papers_db.get_paper(m["id"])
        if not paper:
            continue
        fi = paper.get("file_info") or {}
        canon_name = fi.get("renamed_filename")
        if not canon_name:
            continue
        canon_path = config.SOURCE_PDFS / canon_name
        if not canon_path.exists():
            continue
        canon_text, _ = _extract_text(canon_path)
        bm = paper.get("basic_metadata") or {}
        canonical_block += (
            f"\n--- Canonical candidate id={m['id']} title={bm.get('title')} year={bm.get('year')} ---\n"
            f"{canon_text[:12000]}\n"
        )
        break

    prompt = f"""You are resolving whether a PDF is a duplicate, junk, or a distinct paper for corpus ingest.

Candidate file: {pdf_path.name}
Evidence JSON: {json.dumps(evidence, ensure_ascii=False)[:4000]}

Candidate text (truncated):
{candidate_text[:12000]}

Canonical comparison text (if any):
{canonical_block[:12000]}

Respond with JSON only:
{{"decision": "<one of DELETE_DUPLICATE|KEEP_AND_PROCESS|DELETE_JUNK|MOVE_TO_STAGING_FOR_PROCESSING|TECHNICAL_FAILURE_KEEP_IN_REVIEW>", "reason": "<short reason>"}}
"""
    audit.bump("opus_calls")
    try:
        from pipeline_llm import call_llm_json_escalation

        data, usage, model_key = call_llm_json_escalation(
            "Resolve duplicate/junk/ingest. Return JSON only.",
            prompt,
            max_tokens=400,
        )
        audit.summary["opus_input_tokens"] = int(audit.summary.get("opus_input_tokens", 0)) + int(
            usage.get("input_tokens", 0) or 0
        )
        audit.summary["opus_output_tokens"] = int(audit.summary.get("opus_output_tokens", 0)) + int(
            usage.get("output_tokens", 0) or 0
        )
        decision = str(data.get("decision") or "").strip()
        if decision not in DECISIONS:
            return "TECHNICAL_FAILURE_KEEP_IN_REVIEW", {**evidence, "reason": "invalid_decision"}
        return decision, {**evidence, "opus_reason": data.get("reason"), "model": model_key}
    except Exception as e:
        return "TECHNICAL_FAILURE_KEEP_IN_REVIEW", {**evidence, "reason": f"resolution_api_error:{e}"}


def _unique_staging_dest(name: str) -> Path:
    return safe_destination_for_file(
        config.STAGING / name,
        config.STAGING,
        reason="review_resolution",
        max_filename=MAX_FILENAME - len(".review-retry.json"),
    )


def _apply_decision(
    pdf_path: Path,
    decision: str,
    evidence: dict[str, Any],
    *,
    audit: ResolutionAudit,
    dry_run: bool = False,
    hash_index: dict | None = None,
) -> dict[str, Any]:
    result = {"original_path": str(pdf_path), "decision": decision, "evidence": evidence, "dry_run": dry_run}
    if dry_run:
        audit.record(action="dry_run", **result)
        return result

    if decision == "DELETE_DUPLICATE":
        if pdf_path.exists() and not evidence.get("sha256"):
            evidence["sha256"] = _sha256(pdf_path)
        owner = _canonical_owner_for_hash(evidence.get("sha256", ""), hash_index)
        staging_match = evidence.get("match") == "staging_hash_identical"
        if not owner and not staging_match:
            decision = "TECHNICAL_FAILURE_KEEP_IN_REVIEW"
            evidence["reason"] = "delete_blocked_no_canonical_owner"
            TECHNICAL_REVIEW.mkdir(parents=True, exist_ok=True)
            dest = safe_destination_for_file(pdf_path, TECHNICAL_REVIEW, reason="technical_failure")
            shutil.move(str(pdf_path), str(dest))
            audit.bump("technical_failures")
            audit.record(action="technical_failure", destination=str(dest), decision=decision, evidence=evidence)
            return result
        pdf_path.unlink(missing_ok=True)
        audit.bump("confirmed_duplicates_deleted")
        audit.record(action="confirmed_duplicate_deleted", **result)
        return result

    if decision == "DELETE_JUNK":
        pdf_path.unlink(missing_ok=True)
        audit.bump("deleted_junk")
        audit.record(action="deleted_junk", **result)
        return result

    if decision in {"KEEP_AND_PROCESS", "MOVE_TO_STAGING_FOR_PROCESSING"}:
        dest = _unique_staging_dest(pdf_path.name)
        shutil.move(str(pdf_path), str(dest))
        audit.bump("moved_to_staging")
        audit.bump("ambiguous_resolved")
        audit.record(action="moved_to_staging", destination=str(dest), **result)
        return result

    TECHNICAL_REVIEW.mkdir(parents=True, exist_ok=True)
    dest = safe_destination_for_file(pdf_path, TECHNICAL_REVIEW, reason="technical_failure")
    shutil.move(str(pdf_path), str(dest))
    audit.bump("technical_failures")
    audit.record(action="technical_failure", destination=str(dest), **result)
    return result


def resolve_pdf(
    pdf_path: Path,
    *,
    audit: ResolutionAudit,
    allow_opus: bool = False,
    dry_run: bool = False,
    hash_index: dict | None = None,
) -> dict[str, Any]:
    decision, evidence = _deterministic_decision(pdf_path, hash_index=hash_index)
    model_used = None
    if decision is None:
        if allow_opus:
            decision, evidence = _opus_decision(pdf_path, evidence, audit=audit)
            model_used = evidence.get("model")
        else:
            decision = "TECHNICAL_FAILURE_KEEP_IN_REVIEW"
            evidence["reason"] = evidence.get("reason") or "opus_unavailable_for_resolution"
    if decision == "KEEP_AND_PROCESS":
        decision = "MOVE_TO_STAGING_FOR_PROCESSING"
    result = _apply_decision(pdf_path, decision, evidence, audit=audit, dry_run=dry_run, hash_index=hash_index)
    if model_used:
        result["model_used"] = model_used
    if decision not in {"DELETE_DUPLICATE", "DELETE_JUNK"} and decision != "TECHNICAL_FAILURE_KEEP_IN_REVIEW":
        audit.bump("ambiguous_resolved")
    return result


def delete_confirmed_duplicate(
    pdf_path: Path,
    *,
    audit: ResolutionAudit | None = None,
    dry_run: bool = False,
    hash_index: dict | None = None,
    digest: str | None = None,
) -> dict[str, Any]:
    audit = audit or ResolutionAudit()
    digest = (digest or "").strip().lower() or _sha256(pdf_path)
    owner = _canonical_owner_for_hash(digest, hash_index)
    if not owner:
        staging = _other_staging_paths_for_hash(digest, exclude=pdf_path)
        if staging:
            evidence = {"sha256": digest, "match": "staging_hash_identical", "staging_files": staging}
            if dry_run:
                return {"decision": "DELETE_DUPLICATE", "dry_run": True, "evidence": evidence}
            pdf_path.unlink(missing_ok=True)
            audit.bump("confirmed_duplicates_deleted")
            audit.record(action="confirmed_duplicate_deleted", original_path=str(pdf_path), evidence=evidence)
            return {"decision": "DELETE_DUPLICATE", "evidence": evidence}
        return {"decision": "SKIP", "reason": "no_canonical_owner_for_hash"}
    evidence = {"sha256": digest, "canonical": owner["canonical_name"], "match": "hash_identical"}
    if dry_run:
        return {"decision": "DELETE_DUPLICATE", "dry_run": True, "evidence": evidence}
    pdf_path.unlink(missing_ok=True)
    audit.bump("confirmed_duplicates_deleted")
    audit.record(action="confirmed_duplicate_deleted", original_path=str(pdf_path), evidence=evidence)
    return {"decision": "DELETE_DUPLICATE", "evidence": evidence}


def list_technical_failure_pdfs() -> list[Path]:
    if not TECHNICAL_REVIEW.exists():
        return []
    return sorted(TECHNICAL_REVIEW.glob("*.pdf"))


def run_technical_failure_resolution(
    *,
    run_id: str,
    allow_opus: bool = False,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    after_item: Callable[[], None] | None = None,
    after_all: Callable[[], None] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Resolve review/technical-failure items after staging; deterministic first, then Opus."""
    audit = ResolutionAudit(run_id=f"{run_id}-review-resolution")
    hash_index = _source_hash_index()
    pdfs = list_technical_failure_pdfs()
    start_count = len(pdfs)
    resolved = 0
    deleted_dup = 0
    deleted_junk = 0
    moved_staging = 0
    remaining = 0
    stopped = False

    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)
        else:
            print(msg, flush=True)

    _log(f"Review resolution starting: {start_count} technical-failure item(s), opus={'on' if allow_opus else 'off'}")

    for idx, pdf_path in enumerate(pdfs, 1):
        if should_stop and should_stop():
            stopped = True
            _log("Review resolution stopped by user/signal.")
            break

        if on_progress:
            on_progress(
                {
                    "phase": "review_resolution",
                    "review_resolution_index": idx,
                    "review_resolution_total": start_count,
                    "current_review_pdf": pdf_path.name,
                }
            )

        digest = None
        try:
            digest = _sha256(pdf_path)
        except OSError:
            resolve_pdf(pdf_path, audit=audit, allow_opus=allow_opus, hash_index=hash_index)
            resolved += 1
            continue

        if _canonical_owner_for_hash(digest, hash_index) or is_staging_duplicate_of_other(pdf_path, digest):
            delete_confirmed_duplicate(pdf_path, audit=audit, hash_index=hash_index)
            deleted_dup += 1
            resolved += 1
            continue

        before_exists = pdf_path.exists()
        result = resolve_pdf(pdf_path, audit=audit, allow_opus=allow_opus, hash_index=hash_index)
        decision = result.get("decision", "")
        if decision == "DELETE_DUPLICATE":
            deleted_dup += 1
        elif decision == "DELETE_JUNK":
            deleted_junk += 1
        elif decision in {"MOVE_TO_STAGING_FOR_PROCESSING", "KEEP_AND_PROCESS"}:
            moved_staging += 1
        elif decision == "TECHNICAL_FAILURE_KEEP_IN_REVIEW" and before_exists and pdf_path.exists():
            remaining += 1
        resolved += 1
        if after_item:
            after_item()

    if after_all:
        after_all()

    audit_summary = audit.finalize()
    opus_in = int(audit_summary.get("opus_input_tokens") or 0)
    opus_out = int(audit_summary.get("opus_output_tokens") or 0)
    opus_cost = _opus_cost_usd(opus_in, opus_out)
    tf_remaining = len(list_technical_failure_pdfs())

    out = {
        "review_resolution_enabled": True,
        "review_resolution_allow_opus": allow_opus,
        "review_resolution_stopped": stopped,
        "review_items_start": start_count,
        "review_items_resolved": resolved,
        "review_items_deleted_duplicate": deleted_dup,
        "review_items_deleted_junk": deleted_junk,
        "review_items_moved_to_staging": moved_staging,
        "review_items_remaining_technical_failure": tf_remaining,
        "opus_calls_made": int(audit_summary.get("opus_calls") or 0),
        "opus_input_tokens": opus_in,
        "opus_output_tokens": opus_out,
        "opus_cost_usd": opus_cost,
        "review_resolution_log_dir": str(audit.log_dir),
        "review_resolution_summary_path": audit_summary.get("summary_path"),
    }
    _log(
        f"Review resolution done: checked={resolved}, dup_deleted={deleted_dup}, "
        f"junk_deleted={deleted_junk}, moved_to_staging={moved_staging}, "
        f"remaining={tf_remaining}, opus_calls={out['opus_calls_made']}"
    )
    return out


def run_bloat_cleanup(*, dry_run: bool = False, allow_opus: bool = False, max_opus_calls: int = 2) -> dict[str, Any]:
    audit = ResolutionAudit()
    hash_index = _source_hash_index()

    scan_dirs: list[Path] = []
    if config.DUPLICATES_DIR.exists():
        scan_dirs.extend(sorted(config.DUPLICATES_DIR.glob("*.pdf")))
    for sub in (
        "duplicate-or-ambiguous",
        "duplicates",
        "quarantine-orphan-source",
        "needs-metadata",
        "evaluation-failed",
        "evaluation-error",
        "unidentified",
        "insufficient-text",
        "supplements-pending-parent",
    ):
        d = config.REVIEW_DIR / sub
        if d.exists():
            scan_dirs.extend(sorted(d.rglob("*.pdf")))

    opus_used = 0
    for pdf_path in scan_dirs:
        digest = None
        try:
            digest = _sha256(pdf_path)
        except OSError:
            resolve_pdf(pdf_path, audit=audit, allow_opus=False, dry_run=dry_run)
            continue

        if _canonical_owner_for_hash(digest, hash_index) or digest in _staging_hash_index():
            delete_confirmed_duplicate(pdf_path, audit=audit, dry_run=dry_run, hash_index=hash_index)
            continue

        use_opus = allow_opus and opus_used < max_opus_calls
        result = resolve_pdf(pdf_path, audit=audit, allow_opus=use_opus, dry_run=dry_run, hash_index=hash_index)
        if result.get("model_used"):
            opus_used += 1

    if config.DUPLICATES_DIR.exists() and not dry_run:
        remaining = list(config.DUPLICATES_DIR.glob("*.pdf"))
        if not remaining:
            try:
                config.DUPLICATES_DIR.rmdir()
            except OSError:
                pass

    summary = audit.finalize()
    summary["duplicates_dir_empty"] = not config.DUPLICATES_DIR.exists() or not list(config.DUPLICATES_DIR.glob("*.pdf"))
    summary["review_technical_failure_count"] = len(list(TECHNICAL_REVIEW.rglob("*.pdf"))) if TECHNICAL_REVIEW.exists() else 0
    summary["quarantine_remaining"] = len(list((config.REVIEW_DIR / "quarantine-orphan-source").glob("*.pdf"))) if (config.REVIEW_DIR / "quarantine-orphan-source").exists() else 0
    summary["review_total_pdfs"] = len(list(config.REVIEW_DIR.rglob("*.pdf")))
    return summary


def main() -> None:
    dry = "--dry-run" in sys.argv
    allow_opus = "--allow-opus-resolution" in sys.argv
    report = run_bloat_cleanup(dry_run=dry, allow_opus=allow_opus)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
