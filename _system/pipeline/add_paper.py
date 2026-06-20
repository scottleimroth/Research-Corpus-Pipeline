#!/usr/bin/env python3
"""
add_paper.py — Add a paper to the SQLite database + CSV + source-pdfs pipeline.

Usage:
    python3 add_paper.py <pdf_path> [--id ID] [--title TITLE] [--authors "A; B; C"]
        [--year YEAR] [--journal JOURNAL] [--doi DOI] [--volume VOL] [--issue ISS]
        [--pages PAGES] [--tags "tag1,tag2"] [--rating RATING] [--extract-metadata]

If --extract-metadata is passed, attempts to pull metadata from CrossRef (via DOI)
and/or PDF first pages. Manual args override extracted values.

Examples:
    # Minimal — just a PDF, extract everything automatically
    python3 add_paper.py /tmp/smith_2024_music.pdf --doi 10.1234/example --extract-metadata

    # Full manual entry
    python3 add_paper.py /tmp/smith_2024_music.pdf --id Smith_2024_Music \
        --title "Music and the brain" --authors "John Smith; Jane Doe" \
        --year 2024 --journal "Nature" --doi 10.1234/example

    # Batch mode — multiple PDFs in a directory
    python3 add_paper.py /tmp/papers/*.pdf --extract-metadata
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

# --- Portable path bootstrap (Phase 2 repath) ---
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from config import (  # noqa: E402
    CSV_PATH, SOURCE_PDFS, BACKUP_DIR,
    STAGING, ERROR_LOG, DUPE_LOG,
    CROSSREF_USER_AGENT, OLLAMA_URL, OLLAMA_MODEL,
)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import papers_db  # noqa: E402
import metadata_extractors  # noqa: E402
from path_governance import (  # noqa: E402
    file_token,
    make_safe_filename,
    reserve_unique_path,
    shorten_existing_filename,
    validate_path_policy,
)

BASE = papers_db.BASE
config.ensure_dirs()


def log_error(paper_ref, stage, error_msg):
    """Log pipeline errors to a persistent error log."""
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {stage} | {paper_ref} | {error_msg}\n")
    except OSError:
        pass


import hashlib

def _sha256_of_file(path, chunk=8192):
    """Compute sha256 of a file. Returns hex string or None on error."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(chunk), b""):
                h.update(block)
        return h.hexdigest()
    except Exception:
        return None


def _author_overlap(incoming, existing):
    """True if any surname in incoming matches any surname in existing (case-insensitive)."""
    def surnames(lst):
        out = set()
        for a in lst or []:
            if isinstance(a, str) and a.split():
                out.add(a.split()[-1].lower())
        return out
    return bool(surnames(incoming) & surnames(existing))


def _compute_dupe_signals(staging_pdf, paper,
                          incoming_doi=None,
                          incoming_title=None,
                          incoming_authors=None,
                          incoming_year=None,
                          incoming_abstract=None):
    """Compute which dupe signals fire between staging_pdf and an existing DB paper.

    Signals (each appended as a string when it fires):
      content_hash : sha256 of staging file == sha256 of existing source-pdfs file
      doi          : incoming DOI == existing DOI
      author       : at least one surname overlap
      title_NN     : title fuzzy ratio > 0.85   (NN is integer percent)
      year         : year exact match
      abstract_NN  : abstract fuzzy ratio > 0.80
    """
    signals = []

    # content_hash - strongest signal, file identity
    fi = paper.get("file_info") or {}
    renamed = fi.get("renamed_filename") if isinstance(fi, dict) else None
    if renamed:
        existing_path = SOURCE_PDFS / renamed
        if existing_path.exists():
            h_in = _sha256_of_file(staging_pdf)
            h_db = _sha256_of_file(existing_path)
            if h_in and h_db and h_in == h_db:
                signals.append("content_hash")

    bm = paper.get("basic_metadata") or {}

    # DOI
    ex_doi = (bm.get("doi") or "").lower().strip()
    if incoming_doi and ex_doi and incoming_doi.lower().strip() == ex_doi:
        signals.append("doi")

    # Author overlap
    if incoming_authors and bm.get("authors"):
        if _author_overlap(incoming_authors, bm.get("authors")):
            signals.append("author")

    # Title fuzzy
    ex_title = (bm.get("title") or "").strip().lower()
    in_title = (incoming_title or "").strip().lower()
    if ex_title and in_title:
        ratio = SequenceMatcher(None, in_title, ex_title).ratio()
        if ratio > 0.85:
            signals.append(f"title_{int(ratio*100)}")

    # Year exact
    ex_year = str(bm.get("year") or "").strip()
    in_year = str(incoming_year or "").strip()
    if ex_year and in_year and ex_year == in_year:
        signals.append("year")

    # Abstract fuzzy (optional, only if both sides have one)
    ex_ab = ((paper.get("abstract_keywords") or {}).get("abstract") or "").strip().lower()
    in_ab = (incoming_abstract or "").strip().lower()
    if ex_ab and in_ab and min(len(ex_ab), len(in_ab)) > 80:
        ratio_ab = SequenceMatcher(None, in_ab[:2000], ex_ab[:2000]).ratio()
        if ratio_ab > 0.80:
            signals.append(f"abstract_{int(ratio_ab*100)}")

    return signals


def _is_confirmed_dupe(signals):
    """Decide if signal set is enough to call this a confirmed dupe.

    Policy (locked with Scott 2026-05-27):
      - content_hash alone           -> dupe (identical bytes)
      - doi + any other              -> dupe
      - author + title + (year|abstract) -> dupe
      Otherwise: NOT a dupe.
    """
    has_title = any(s.startswith("title_") for s in signals)
    has_abstract = any(s.startswith("abstract_") for s in signals)
    if "content_hash" in signals:
        return True
    if "doi" in signals and len(signals) >= 2:
        return True
    if "author" in signals and has_title and ("year" in signals or has_abstract):
        return True
    return False


def _handle_dupe(staging_pdf, existing_paper, signals):
    """Confirmed-dupe disposition: delete extra staging copy when canonical exists."""
    pdf_path = Path(staging_pdf)
    existing_id = existing_paper.get("id", "?")
    existing_renamed = ((existing_paper.get("file_info") or {}).get("renamed_filename")
                        if isinstance(existing_paper.get("file_info"), dict) else "?")

    from duplicate_disposition import delete_confirmed_duplicate_file

    reason = f"confirmed_dupe_of:{existing_id}"
    try:
        result = delete_confirmed_duplicate_file(pdf_path, reason, signals=list(signals))
        deleted = str(result.get("decision") or "").upper() == "DELETE_DUPLICATE" or not pdf_path.exists()
        if not deleted and pdf_path.exists():
            # Metadata-confirmed duplicate can still return SKIP in hash-only delete
            # helpers. Do not leave it in staging to be retried/requeued.
            pdf_path.unlink(missing_ok=True)
            deleted = True
            result = {**result, "decision": "DELETE_DUPLICATE", "forced_metadata_delete": True}
        print(
            f"  Confirmed dupe of {existing_id} ({','.join(signals)}) -> "
            f"{'deleted' if deleted else 'not deleted'}"
        )
    except OSError as e:
        print(f"  Delete duplicate failed: {e}")
        log_error(str(pdf_path), "DUPLICATE", f"delete failed: {e}")
        return False

    ts = datetime.now().isoformat()
    lines = [
        f"[{ts}] DUPE confirmed ({len(signals)} signal(s): {','.join(signals)})",
        f"  Staging: {pdf_path.name}",
        f"  Existing: id={existing_id}  renamed={existing_renamed}",
        f"  Disposition: deleted (canonical retained)",
        f"  Result: {result}",
    ]
    msg = "\n".join(lines) + "\n\n"
    DUPE_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(DUPE_LOG, "a", encoding="utf-8") as f:
            f.write(msg)
    except OSError:
        pass
    return False




def extract_text_from_pdf(pdf_path, pages=2):
    """Extract text from first N pages of PDF using the central extraction contract."""
    from document_text_extraction import extract_document_text

    result = extract_document_text(pdf_path, pages=pages, attempt_ocr=False)
    return result.text


def extract_full_text(pdf_path, *, attempt_ocr: bool = True, use_cache: bool = True):
    """Extract full text from PDF with OCR fallback when needed."""
    from document_text_extraction import extract_document_text, format_extraction_diagnostic

    global LAST_INGEST_EXTRACTION
    result = extract_document_text(pdf_path, attempt_ocr=attempt_ocr, use_cache=use_cache)
    LAST_INGEST_EXTRACTION = result.to_dict()
    if result.extractor_status != "TEXT_OK":
        print(f"  {format_extraction_diagnostic(result)}")
        if result.errors:
            print(f"  extraction errors: {'; '.join(result.errors[:3])}")
    elif result.extraction_source == "ocr":
        print(f"  OCR extracted {result.text_length} chars")
    return result.text


LAST_INGEST_EXTRACTION: dict | None = None
LAST_EVIDENCE_PACKAGE: dict | None = None
# Set whenever the acceptance gate blocks DB insert so the staging runner can
# adjudicate the same file immediately instead of treating it as a bare failure.
LAST_ACCEPTANCE_BLOCK: dict | None = None


def _record_acceptance_block(pdf_path, acceptance, coverage, sha256) -> None:
    """Capture a coverage/acceptance block so the caller can adjudicate now."""
    global LAST_ACCEPTANCE_BLOCK
    acc = acceptance.to_dict() if hasattr(acceptance, "to_dict") else dict(acceptance or {})
    cov = coverage or {}
    LAST_ACCEPTANCE_BLOCK = {
        "filename": Path(pdf_path).name,
        "sha256": sha256,
        "action": acc.get("action"),
        "reason": acc.get("reason"),
        "required_next_action": acc.get("required_next_action"),
        "coverage_status": cov.get("coverage_status") or (acc.get("audit_fields") or {}).get("coverage_status"),
        "likely_journal_article": cov.get("likely_journal_article")
        if cov else (acc.get("audit_fields") or {}).get("likely_journal_article"),
        "evidence_can_support_rating": (acc.get("audit_fields") or {}).get("evidence_can_support_rating"),
        "evidence_can_support_not_applicable": (acc.get("audit_fields") or {}).get("evidence_can_support_not_applicable"),
    }


def _record_manual_acceptance_block(
    pdf_path: Path | str,
    *,
    action: str,
    reason: str,
    sha256: str | None = None,
    required_next_action: str | None = None,
) -> None:
    _record_acceptance_block(
        Path(pdf_path),
        {
            "action": action,
            "reason": reason,
            "required_next_action": required_next_action,
            "audit_fields": {},
        },
        {},
        sha256,
    )


def pop_last_acceptance_block() -> dict | None:
    """Return and clear the last acceptance block snapshot."""
    global LAST_ACCEPTANCE_BLOCK
    block = dict(LAST_ACCEPTANCE_BLOCK) if LAST_ACCEPTANCE_BLOCK else None
    LAST_ACCEPTANCE_BLOCK = None
    return block


def parse_sections(full_text):
    """Parse full text into sections using regex. Best-effort, ~70% accuracy."""
    sections = {}
    section_patterns = [
        (r'(?i)\babstract\b', 'abstract'),
        (r'(?i)\bintroduction\b', 'introduction'),
        (r'(?i)\b(?:materials?\s+and\s+)?methods?\b', 'methods'),
        (r'(?i)\bresults?\b', 'results'),
        (r'(?i)\bdiscussion\b', 'discussion'),
        (r'(?i)\bconclusions?\b', 'conclusion'),
        (r'(?i)\backnowledg(?:e)?ments?\b', 'acknowledgements'),
        (r'(?i)\breferences\b', 'references'),
        (r'(?i)\bappendix\b', 'appendix'),
        (r'(?i)\bsupplementary\b', 'supplementary'),
    ]

    found = []
    for pattern, name in section_patterns:
        for m in re.finditer(pattern, full_text):
            pos = m.start()
            if pos == 0 or full_text[pos-1] in '\n\r':
                found.append((pos, name))
            elif pos > 1 and full_text[pos-2:pos] in ['. ', '.\n']:
                found.append((pos, name))

    found.sort(key=lambda x: x[0])

    for i, (pos, name) in enumerate(found):
        end = found[i+1][0] if i+1 < len(found) else len(full_text)
        header_end = full_text.find('\n', pos)
        if header_end == -1:
            header_end = pos
        section_text = full_text[header_end:end].strip()
        if section_text:
            sections[name] = section_text

    sections['full'] = full_text
    return sections


def doi_from_pdf_text(text):
    """Try to extract DOI from PDF text."""
    patterns = [
        r'(?:doi|DOI)[:\s]*\s*(10\.\d{4,}/[^\s]+)',
        r'(10\.\d{4,}/[^\s]+)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            doi = m.group(1).rstrip(".,;)")
            return doi
    return None


SYNTHETIC_AUTHOR_TOKENS = {
    "unknown", "author", "authors", "paper", "article", "review",
    "appraisal", "american", "rhythm", "continuous", "download",
    "exact", "title", "journal", "editor", "editors", "team",
    "orphan", "orphans", "anonymous", "editorial", "publisher",
    "book", "chapter", "manual", "guide", "handbook", "series",
}


def is_unsafe_generated_id(paper_id):
    """True if generated id looks like a junk placeholder rename target."""
    if not paper_id:
        return True
    low = paper_id.lower()
    if "_unknown_" in low:
        return True
    if low.startswith("unknown_unknown"):
        return True
    if any("unknown" in part for part in low.split("_")):
        return True
    return False


def _extract_author_surname(authors):
    """Return a usable surname token or empty string for synthetic/garbage values."""
    if not authors:
        return ""
    first_author = authors[0] if isinstance(authors[0], str) else ""
    if not first_author:
        return ""
    surname = re.sub(r"[^a-zA-Z]", "", first_author.split()[-1]).strip()
    if not surname:
        return ""
    low = surname.lower()
    if low in SYNTHETIC_AUTHOR_TOKENS or "unknown" in low:
        return ""
    if len(surname) < 3:
        return ""
    return surname


def generate_id(authors, year, title, pdf_filename=None):
    """Generate a paper ID like Author_Year_KeyWord. Falls back to filename."""
    first = _extract_author_surname(authors)

    keyword = ""
    if title:
        words = [w for w in title.split() if len(w) > 3 and w.lower() not in
                 {"with", "from", "that", "this", "their", "about", "between", "through", "pii"}]
        keyword = re.sub(r'[^a-zA-Z]', '', words[0]) if words else ""
        if keyword.lower() in {"pii", "unknown", "untitled", "doi", "issn"}:
            keyword = ""

    year_str = str(year) if year else ""

    if first and year_str and keyword:
        return f"{first}_{year_str}_{keyword}"

    if pdf_filename:
        stem = Path(pdf_filename).stem
        if re.match(r'^[0-9a-f]{8}-', stem):
            return f"orphan_{stem[:8]}"
        clean = re.sub(r'[^a-zA-Z0-9_]', '_', stem)[:60]
        if "unknown" in clean.lower():
            token = re.sub(r'[^a-zA-Z0-9]', '', stem)[:8] or "file"
            return f"orphan_{token}"
        return clean

    return f"orphan_{int(time.time())}"


def generate_filename(authors, year, title, original_filename=""):
    """Generate a path-policy-compliant canonical source PDF filename."""
    candidate = make_safe_filename(
        authors=authors,
        year=year,
        title=title,
        original_filename=original_filename,
        extension=".pdf",
        hash_source=f"{authors}|{year}|{title}|{original_filename}",
    )
    return reserve_unique_path(SOURCE_PDFS, candidate).name


def add_to_csv(paper_entry):
    """Append paper to CSV."""
    if not CSV_PATH.exists():
        return "no_csv"
    with open(CSV_PATH, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    bm = paper_entry.get("basic_metadata", {})
    new_row = {fn: "" for fn in fieldnames}
    new_row["id"] = paper_entry.get("id", "")
    new_row["document_type"] = paper_entry.get("document_type", "journal_article")
    new_row["title"] = bm.get("title", "")
    authors = bm.get("authors", [])
    new_row["authors"] = "; ".join(authors) if isinstance(authors, list) else str(authors)
    new_row["year"] = str(bm.get("year", ""))
    new_row["journal"] = bm.get("journal", "")
    new_row["doi"] = bm.get("doi", "")
    fi = paper_entry.get("file_info", {})
    new_row["file-info_renamed_filename"] = fi.get("renamed_filename", "")
    new_row["file-info_filepath"] = fi.get("filepath", "")

    rows.append(new_row)

    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return len(rows)


def is_supplement(filename):
    """Check if a file is a supplementary material file."""
    name_lower = filename.lower()
    # Elsevier multimedia components (mmc1.pdf, mmc2.docx, etc.)
    if re.match(r'^mmc\d+\.', name_lower):
        return True
    # Common supplement patterns
    if any(pat in name_lower for pat in ['_supplement', '_supp_', '_supp.', '_appendix',
                                          '_supporting', '_table_s', '_figure_s',
                                          '-supplement', '-supp-', '-supp.',
                                          'supplementary', 'suppl_', 'suppl.']):
        return True
    # Elsevier supplement extensions with .doc/.docx/.xlsx
    if re.match(r'^(mmc|supp|supplement)\d*\.(doc|docx|xlsx|xls|csv|txt)$', name_lower):
        return True
    return False

_ORPHAN_ID_PATTERNS = (
    r"__supplement$",
    r"_supplement_\d+$",
    r"_supplement$",
    r"_Supplement$",
    r"_supp\d+$",
)


def _is_orphan_supplement_id(paper_id):
    """True if the paper id looks like an Alex-era orphan supplement row.

    These rows shouldn't be returned as supplement parents - we want the
    real parent paper instead.
    """
    if not paper_id:
        return False
    for pat in _ORPHAN_ID_PATTERNS:
        if re.search(pat, paper_id):
            return True
    return False


def _find_parent_paper_from_name(supp_filename, *, all_meta=None):
    """Best-effort: find the real parent paper in the DB matching the supplement filename.

    Looks at the first chunk of the supplement filename for an author surname
    (alphabetic prefix) and a 4-digit year. Then searches the DB for a paper
    whose renamed_filename starts with `<author>_<year>_`.

    Prefers real-paper rows over Alex-era orphan supplement rows (whose ids
    end with _supp1, _supplement, etc).

    Returns the paper dict on match, None otherwise.
    """
    name = supp_filename.lower()
    stem = re.sub(
        r"[_-](supplement|supp|appendix|supporting|suppl)[_.\d-]*.*$",
        "", name, count=1
    )
    stem = re.sub(r"\.pdf$|\.docx?$|\.xlsx?$", "", stem)

    m = re.match(r"^([a-z]{3,})[_\-](\d{4})", stem)
    author, year = (m.group(1), m.group(2)) if m else (None, None)
    if not (author and year):
        m2 = re.match(r"^[^a-z]*([a-z]{4,})", stem)
        if m2:
            author = m2.group(1)

    if not author:
        return None

    if all_meta is None:
        all_meta = papers_db.get_all_metadata()
    real_matches = []
    orphan_matches = []
    for p in all_meta:
        fi = p.get("file_info") or {}
        renamed = (fi.get("renamed_filename") or "").lower()
        if not renamed:
            continue
        matched = False
        if year and renamed.startswith(f"{author}_{year}_"):
            matched = True
        elif not year and renamed.startswith(f"{author}_"):
            matched = True
        if not matched:
            continue
        if _is_orphan_supplement_id(p.get("id", "")):
            orphan_matches.append(p)
        else:
            real_matches.append(p)

    # Prefer real parents over orphans
    if real_matches:
        return real_matches[0]
    if orphan_matches:
        return orphan_matches[0]
    return None


def _handle_supplement(pdf_path):
    """Route parent-dependent child documents via the pending-parent holding policy."""
    from child_document_matching import handle_supplement_file

    return handle_supplement_file(pdf_path)


def commit_accepted_corpus_paper(
    *,
    entry: dict,
    staging_pdf: Path,
    dest_pdf: Path,
    acceptance,
    force_acceptance: bool = False,
) -> bool:
    """Single DB/source transaction entry point — only after acceptance gate passes."""
    from document_evidence import AcceptanceDecision

    decision = acceptance if isinstance(acceptance, AcceptanceDecision) else AcceptanceDecision(**acceptance)
    if (not force_acceptance) and (not decision.can_insert_db or not decision.can_copy_to_source):
        raise ValueError(f"commit blocked: {decision.action} — {decision.reason}")

    paper_id = entry.get("id") or ""
    fi = dict(entry.get("file_info") or {})
    desired = fi.get("renamed_filename") or dest_pdf.name or staging_pdf.name
    if not validate_path_policy(SOURCE_PDFS / desired)["ok"]:
        bm = entry.get("basic_metadata") or {}
        desired = shorten_existing_filename(
            desired,
            authors=bm.get("authors"),
            year=bm.get("year"),
            title=bm.get("title"),
            hash_source=staging_pdf.name,
        )
    try:
        token = file_token(staging_pdf)
    except OSError:
        token = None
    dest_pdf = reserve_unique_path(SOURCE_PDFS, desired, content_token=token)
    fi["renamed_filename"] = dest_pdf.name
    fi["filepath"] = f"source-pdfs/{dest_pdf.name}"
    entry["file_info"] = fi
    shutil.copy2(staging_pdf, dest_pdf)
    print(f"  PDF -> source-pdfs/{dest_pdf.name}")
    try:
        papers_db.save_paper(entry, allow_replace=False)
    except Exception as e:
        if dest_pdf.exists():
            try:
                dest_pdf.unlink()
            except OSError:
                pass
        print(f"  Database write failed: {e}")
        log_error(paper_id, "DB_WRITE", str(e))
        return False
    return True


def rollback_accepted_paper(paper_id: str, *, reason: str = "", audit_context: dict | None = None) -> dict:
    """Remove accepted corpus row and canonical source PDF with audit record."""
    import sqlite3

    paper = papers_db.get_paper(paper_id)
    removed_pdf = None
    staging_hint = None
    if paper:
        fi = paper.get("file_info") or {}
        rel = fi.get("filepath") or fi.get("managed_pdf_relative_path") or ""
        if rel:
            pdf_path = Path(rel) if Path(rel).is_absolute() else config.MASTER_PROJECT / rel
            if pdf_path.exists():
                pdf_path.unlink()
                removed_pdf = str(pdf_path)
        staging_hint = fi.get("original_filename")

    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
        conn.commit()
    finally:
        conn.close()

    audit_dir = config.CORPUS_STORE / "audit" / "acceptance-rollbacks"
    audit_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now().isoformat(),
        "paper_id": paper_id,
        "reason": reason,
        "removed_source_pdf": removed_pdf,
        "original_filename": staging_hint,
        "context": audit_context or {},
    }
    audit_path = audit_dir / f"{paper_id}_{datetime.now():%Y%m%d-%H%M%S}.json"
    audit_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def process_paper(pdf_path, args):
    """Process a single PDF and add to the database + CSV."""
    from pdf_content_guard import is_actual_pdf

    # Reset per-file globals BEFORE any early return so a previous file's
    # extraction/evidence/acceptance can never leak to the caller (stale-state).
    global LAST_INGEST_EXTRACTION, LAST_EVIDENCE_PACKAGE, LAST_ACCEPTANCE_BLOCK
    LAST_INGEST_EXTRACTION = None
    LAST_EVIDENCE_PACKAGE = None
    LAST_ACCEPTANCE_BLOCK = None

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        print(f"  PDF not found: {pdf_path}")
        return False

    if not is_actual_pdf(pdf_path):
        print(f"  Not a PDF (content sniff): {pdf_path.name}")
        return False

    # Parent-dependent child document detection (supplement, addendum, erratum, etc.)
    from child_document_matching import is_child_document

    if is_child_document(pdf_path.name):
        print(f"  Child document detected: {pdf_path.name}")
        return _handle_supplement(pdf_path)

    if pdf_path.stat().st_size < 1000:
        print(f"  PDF too small or invalid: {pdf_path} ({pdf_path.stat().st_size} bytes). Skipping.")
        return False

    print(f"\nProcessing: {pdf_path.name}")

    from document_evidence import (
        build_evidence_package,
        decide_acceptance_from_evidence,
        format_acceptance_diagnostic,
        format_identity_diagnostic,
        is_invalid_canonical_title,
    )

    evidence_pkg = build_evidence_package(pdf_path, args=args, attempt_ocr=True)
    LAST_EVIDENCE_PACKAGE = evidence_pkg.to_dict()
    readiness = evidence_pkg.adjudication_readiness or {}
    identity = evidence_pkg.selected_identity or {}
    first_pages = (evidence_pkg.extraction.get("text") or "")[:8000]

    llm_force_acceptance = bool(args.get("llm_force_acceptance"))
    limited_preclassification = False
    if not bool(args.get("non_ratable_preclassification") or args.get("llm_force_non_ratable")):
        try:
            from document_corpus_classification import detect_limited_publication_item

            limited = detect_limited_publication_item(
                filename=pdf_path.name,
                title=str(identity.get("title") or ""),
                text=first_pages,
                page_count=(evidence_pkg.extraction or {}).get("page_count"),
                coverage=(LAST_EVIDENCE_PACKAGE or {}).get("coverage") or {},
            )
        except Exception:
            limited = {}
        if limited.get("is_limited_publication_item"):
            args["non_ratable_preclassification"] = True
            args["document_type"] = limited.get("document_type") or "conference_abstract"
            args["classification_reasons"] = limited.get("classification_reasons") or []
            limited_preclassification = True

    if not readiness.get("can_insert_db"):
        acceptance = readiness.get("acceptance") or {}
        reason = str(readiness.get("acceptance_reason") or readiness.get("reason") or "blocked")
        can_retry_after_full_extract = (
            str((acceptance or {}).get("action") or "") == "REJECT_NEEDS_RECOVERY"
            and "coverage:PARTIAL_NEEDS_RECOVERY" in reason
        )
        if llm_force_acceptance:
            print(
                "  Acceptance gate overridden by LLM final adjudication; "
                "continuing to full-text extraction."
            )
        elif can_retry_after_full_extract:
            print(
                "  Acceptance gate is PARTIAL_NEEDS_RECOVERY at preflight; "
                "continuing to full-text extraction before final decision."
            )
        elif limited_preclassification:
            print(
                "  Deterministic non-ratable classification at preflight; "
                "continuing without LLM adjudication."
            )
        else:
            print(
                f"  Acceptance gate blocked DB insert: "
                f"{format_acceptance_diagnostic(acceptance) if acceptance else format_identity_diagnostic(evidence_pkg)}"
            )
            log_error(str(pdf_path), "ACCEPTANCE_GATE", reason)
            _record_acceptance_block(
                pdf_path,
                acceptance,
                (LAST_EVIDENCE_PACKAGE or {}).get("coverage") or {},
                (evidence_pkg.file_identity or {}).get("sha256"),
            )
            return False

    # Step 2: DOI from identity or text
    doi = args.get("doi") or identity.get("doi")
    if not doi and args.get("extract_metadata"):
        doi = doi_from_pdf_text(first_pages)
        if doi:
            print(f"  DOI from PDF: {doi}")

    tags_str = args.get("tags", "alex-new")
    tags = [t.strip() for t in tags_str.split(",")]
    rating = args.get("rating")

    tier_meta = (evidence_pkg.metadata_candidates or {}).get("tier_meta") or {}
    meta = {
        **tier_meta,
        "title": identity.get("title"),
        "authors": identity.get("authors") or [],
        "year": identity.get("year"),
        "journal": identity.get("journal"),
        "doi": doi or identity.get("doi"),
        "tiers_log": tier_meta.get("tiers_log") or [],
        "needs_review": False,
        "identity_status": identity.get("identity_status"),
        "title_source": identity.get("title_source"),
        "metadata_source": identity.get("metadata_source"),
    }

    # Step 4: Apply CLI arg overrides (manual args trump extracted data)
    title = args.get("title") or meta.get("title") or ""
    authors_str = args.get("authors")
    if authors_str:
        authors = [a.strip() for a in authors_str.split(";")]
    else:
        authors = meta.get("authors") or []
    year = args.get("year") or meta.get("year") or ""
    journal = args.get("journal") or meta.get("journal") or ""
    volume = args.get("volume") or meta.get("volume") or ""
    issue = args.get("issue") or meta.get("issue") or ""
    pages = args.get("pages") or meta.get("pages") or ""
    doi = doi or meta.get("doi")

    from first_pass_finalize import enrich_identity_first_pass

    enriched_pre = enrich_identity_first_pass(
        pdf_path,
        title=title,
        authors=authors,
        year=year,
        journal=journal,
        doi=doi,
        first_pages_text=first_pages,
        identity_status=identity.get("identity_status") or meta.get("identity_status"),
        llm_forced=llm_force_acceptance,
    )
    title = str(enriched_pre.get("title") or title)
    authors = list(enriched_pre.get("authors") or authors)
    year = enriched_pre.get("year") or year
    journal = enriched_pre.get("journal") or journal
    doi = enriched_pre.get("doi") or doi
    if enriched_pre.get("abstract"):
        meta["abstract"] = enriched_pre["abstract"]

    document_type = args.get("document_type") or "journal_article"
    non_ratable = bool(args.get("non_ratable_preclassification") or args.get("llm_force_non_ratable"))
    if non_ratable and not authors:
        authors = ["Unknown"]

    # If the chain ended at the review tier, log only — do not tag active rows
    if meta.get("needs_review"):
        log_error(str(pdf_path), "METADATA",
                  "All tiers failed: tiers_log=" + str(meta.get("tiers_log")))

    if not title or not str(title).strip() or is_invalid_canonical_title(title)[0]:
        from metadata_extractors import recover_metadata_deterministic, extract_clean_title_from_filename

        rec = recover_metadata_deterministic(str(pdf_path), first_pages, pages=5)
        rec_title = rec.get("title")
        if rec_title and not is_invalid_canonical_title(rec_title)[0]:
            title = rec_title
            authors = authors or rec.get("authors") or []
            year = year or rec.get("year") or ""
            journal = journal or rec.get("journal") or ""
            doi = doi or rec.get("doi")
        else:
            plain = extract_clean_title_from_filename(pdf_path)
            if plain and not is_invalid_canonical_title(plain)[0]:
                title = plain
                if not authors:
                    authors = ["Unknown"]

    if not title or not str(title).strip():
        if non_ratable or args.get("non_ratable_preclassification"):
            plain = extract_clean_title_from_filename(pdf_path)
            if plain:
                title = plain
                if not authors:
                    authors = ["Unknown"]

    if not title or not str(title).strip() or is_invalid_canonical_title(title)[0]:
        if llm_force_acceptance:
            from metadata_extractors import extract_clean_title_from_filename

            plain = extract_clean_title_from_filename(pdf_path)
            if plain and not is_invalid_canonical_title(plain)[0]:
                title = plain
            else:
                print(
                    "  LLM forced ingest: title still weak after enrichment; "
                    "continuing with best available metadata"
                )
        else:
            log_error(str(pdf_path), "METADATA", "Missing or invalid basic_metadata.title after identity gate")
            return False

    if not authors and not non_ratable:
        if llm_force_acceptance:
            authors = ["Unknown"]
            print("  LLM forced ingest: no authors after enrichment; using placeholder", flush=True)
        else:
            log_error(str(pdf_path), "METADATA", "Missing authors")
            return False

    identity = {
        **identity,
        "title": title,
        "authors": authors,
        "year": year,
        "journal": journal,
        "doi": doi or identity.get("doi"),
        "identity_status": enriched_pre.get("identity_status") or identity.get("identity_status"),
        "metadata_source": identity.get("metadata_source") or meta.get("metadata_source") or meta.get("source"),
    }

    # Step 5: Generate ID and filename
    paper_id = args.get("id") or generate_id(authors, year, title, pdf_filename=str(pdf_path))

    # Dedup checks using SQLite (fast, no full JSON load)
    dupe_log = DUPE_LOG
    dupe_log.parent.mkdir(parents=True, exist_ok=True)

    # ID-match candidate: compute signals against the existing paper
    if papers_db.paper_exists(paper_id):
        existing = papers_db.get_paper(paper_id)
        if existing:
            sigs = _compute_dupe_signals(
                pdf_path, existing,
                incoming_doi=doi, incoming_title=title,
                incoming_authors=authors, incoming_year=year,
                incoming_abstract=meta.get("abstract", ""),
            )
            if _is_confirmed_dupe(sigs):
                return _handle_dupe(pdf_path, existing, sigs)
            print(f"  ID '{paper_id}' collides with existing but signals insufficient ({sigs}) - blocking insert")
            log_error(str(pdf_path), "ID_COLLISION", f"unresolved_id_collision:{paper_id}:signals={sigs}")
            _record_manual_acceptance_block(
                pdf_path,
                action="REJECT_DUPLICATE",
                reason=f"unresolved_id_collision:{paper_id}",
                sha256=(LAST_EVIDENCE_PACKAGE or {}).get("file_identity", {}).get("sha256"),
            )
            return False

    # DOI-match candidate: compute signals against the existing paper
    if doi and papers_db.doi_exists(doi):
        existing = papers_db.get_paper_by_doi(doi)
        if existing:
            sigs = _compute_dupe_signals(
                pdf_path, existing,
                incoming_doi=doi, incoming_title=title,
                incoming_authors=authors, incoming_year=year,
                incoming_abstract=meta.get("abstract", ""),
            )
            if _is_confirmed_dupe(sigs):
                return _handle_dupe(pdf_path, existing, sigs)
            print(f"  DOI {doi} matches existing but signals insufficient ({sigs}) - blocking insert")
            log_error(str(pdf_path), "DOI_COLLISION", f"unresolved_doi_collision:{doi}:signals={sigs}")
            _record_manual_acceptance_block(
                pdf_path,
                action="REJECT_DUPLICATE",
                reason=f"unresolved_doi_collision:{doi}",
                sha256=(LAST_EVIDENCE_PACKAGE or {}).get("file_identity", {}).get("sha256"),
            )
            return False

    # Author+year candidates: scan papers from the same year by same author surname
    if title and year and authors:
        in_surnames = {a.split()[-1].lower() for a in authors if a.split()}
        all_meta = papers_db.get_all_metadata()
        for p in all_meta:
            if paper_id == p.get("id"):
                continue
            bm = p.get("basic_metadata", {})
            if str(bm.get("year", "")).strip() != str(year).strip():
                continue
            ex_surnames = {a.split()[-1].lower() for a in (bm.get("authors") or [])
                           if isinstance(a, str) and a.split()}
            if not (in_surnames & ex_surnames):
                continue
            sigs = _compute_dupe_signals(
                pdf_path, p,
                incoming_doi=doi, incoming_title=title,
                incoming_authors=authors, incoming_year=year,
                incoming_abstract=meta.get("abstract", ""),
            )
            if _is_confirmed_dupe(sigs):
                return _handle_dupe(pdf_path, p, sigs)

    filename = generate_filename(authors, year, title, original_filename=pdf_path.name)
    dest_pdf = SOURCE_PDFS / filename

    # Step 6: Extract full text (already validated in evidence package)
    llm_force_full_ocr = bool(args.get("llm_force_full_ocr"))
    use_cache = not llm_force_full_ocr
    full_text = extract_full_text(
        str(pdf_path), attempt_ocr=True, use_cache=use_cache
    )
    from document_text_extraction import MIN_RESEARCH_TEXT_CHARS, meaningful_text_length

    extraction = LAST_INGEST_EXTRACTION or evidence_pkg.extraction or {}
    min_text_chars = MIN_RESEARCH_TEXT_CHARS
    text_len = meaningful_text_length(full_text)
    if (
        llm_force_acceptance
        and not non_ratable
        and (llm_force_full_ocr or text_len < min_text_chars)
    ):
        label = "scan PDF full OCR" if llm_force_full_ocr else "OCR retry"
        print(
            f"  LLM forced ingest: re-extracting with {label} (had {text_len} chars)..."
        )
        full_text = extract_full_text(str(pdf_path), attempt_ocr=True, use_cache=False)
        text_len = meaningful_text_length(full_text)
        extraction = LAST_INGEST_EXTRACTION or extraction
    if not non_ratable and text_len < min_text_chars:
        if llm_force_acceptance:
            print(
                f"  LLM forced ingest with thin text ({text_len} chars); "
                "storing metadata + flagging text_recovery_required"
            )
        else:
            status = str(extraction.get("extractor_status") or "unknown")
            print(
                f"  Insufficient extractable text ({text_len} chars, status={status}); not adding to DB"
            )
            log_error(
                str(pdf_path),
                "TEXT_EXTRACTION",
                f"status={status} chars={text_len} source={extraction.get('extraction_source')}",
            )
            return False

    sections = parse_sections(full_text) if full_text else {"full": ""}
    extraction_method = str(extraction.get("extraction_source") or "pdftotext")

    from evidence_coverage import assess_evidence_coverage, format_coverage_diagnostic

    article_signals = (LAST_EVIDENCE_PACKAGE or {}).get("article_type_evidence") or {}
    coverage = assess_evidence_coverage(
        raw_text=full_text,
        sections=sections,
        extraction=extraction,
        article_signals=article_signals,
        identity=identity,
        document_type=document_type,
        filename=pdf_path.name,
    )
    if not non_ratable and not coverage.get("evidence_can_support_rating"):
        try:
            from document_corpus_classification import detect_limited_publication_item

            limited = detect_limited_publication_item(
                filename=pdf_path.name,
                title=str(title or ""),
                text=full_text or "",
                page_count=coverage.get("page_count"),
                coverage=coverage,
            )
        except Exception:
            limited = {}
        if limited.get("is_limited_publication_item"):
            non_ratable = True
            document_type = limited.get("document_type") or "conference_abstract"
            coverage = assess_evidence_coverage(
                raw_text=full_text,
                sections=sections,
                extraction=extraction,
                article_signals={**article_signals, "reference_material_signals": max(2, int(article_signals.get("reference_material_signals") or 0))},
                identity=identity,
                document_type=document_type,
                filename=pdf_path.name,
            )
            print(
                "  Deterministic non-ratable classification: "
                f"{document_type} ({'; '.join(limited.get('classification_reasons') or [])})",
                flush=True,
            )

    acceptance = decide_acceptance_from_evidence(
        {
            "coverage": coverage,
            "selected_identity": identity,
            "extraction": {**extraction, "text": full_text, "text_length": text_len},
            "article_type_evidence": article_signals,
            "document_type": document_type,
            "file_identity": {"filename": pdf_path.name},
        },
        document_type=document_type,
        non_ratable_preclassification=non_ratable,
    )

    num_sections = len([k for k in sections if k != "full"])
    if num_sections > 0:
        print(f"  Parsed {num_sections} sections: {', '.join(k for k in sections if k != 'full')}")
    print(f"  Coverage: {format_coverage_diagnostic(coverage)}")
    print(f"  Acceptance: {format_acceptance_diagnostic(acceptance)}")

    if not acceptance.can_insert_db:
        if llm_force_acceptance:
            print(
                f"  Acceptance gate overridden by LLM final adjudication "
                f"({acceptance.action} - {acceptance.reason}); proceeding."
            )
        else:
            print(f"  Acceptance gate blocked DB insert: {acceptance.action} — {acceptance.reason}")
            log_error(str(pdf_path), "ACCEPTANCE_GATE", f"{acceptance.action}:{acceptance.reason}")
            _record_acceptance_block(
                pdf_path,
                acceptance,
                coverage,
                (LAST_EVIDENCE_PACKAGE or {}).get("file_identity", {}).get("sha256"),
            )
            return False

    abstract = meta.get("abstract", "") or sections.get("abstract", "")
    keywords = meta.get("keywords", [])

    from first_pass_finalize import enrich_identity_first_pass as _enrich_again

    enriched_post = _enrich_again(
        pdf_path,
        title=title,
        authors=authors,
        year=year,
        journal=journal,
        doi=doi,
        first_pages_text=(full_text or "")[:12000],
        identity_status=identity.get("identity_status"),
        llm_forced=llm_force_acceptance,
    )
    title = str(enriched_post.get("title") or title)
    authors = list(enriched_post.get("authors") or authors)
    year = enriched_post.get("year") or year
    journal = enriched_post.get("journal") or journal
    doi = enriched_post.get("doi") or doi
    identity["identity_status"] = enriched_post.get("identity_status") or identity.get("identity_status")

    # Step 8: Build entry
    entry = {
        "id": paper_id,
        "timestamp_added": datetime.now().isoformat(),
        "document_type": document_type,
        "file_info": {
            "filepath": f"source-pdfs/{filename}",
            "renamed_filename": filename,
            "original_filename": pdf_path.name,
            "file_format": "pdf",
            "readability_status": "complete" if len(full_text) > 100 else "partial"
        },
        "basic_metadata": {
            "title": title,
            "authors": authors,
            "year": year,
            "journal": journal,
            "identity_status": identity.get("identity_status") or "high_confidence",
            "doi": doi or "",
            "volume": volume,
            "issue": issue,
            "pages": pages,
            "publisher": "",
            "language": "English",
            "article_type": "empirical",
            "corresponding_author": authors[0] if authors else "",
            "corresponding_author_email": None
        },
        "identifiers": {
            k: v
            for k, v in {
                "doi": doi or "",
                "pii": identity.get("pii") or "",
            }.items()
            if v
        },
        "abstract_keywords": {
            "abstract": abstract,
            "keywords": keywords
        },
        "full_text": {
            "raw_full_text": full_text,
            "sections": sections,
            "extraction_method": extraction_method,
            "extraction_date": datetime.now().isoformat(),
            "extraction_quality": "complete" if len(full_text) > 500 else "partial",
            "total_chars": len(full_text),
            "page_count": extraction.get("page_count"),
            "coverage": coverage,
            "coverage_status": coverage.get("coverage_status"),
            "extraction": {
                "extraction_source": extraction_method,
                "extractor_status": extraction.get("extractor_status") or "TEXT_OK",
                "text_length": len(full_text),
                "page_count": extraction.get("page_count"),
            },
        },
        "classification": {
            "document_type": document_type,
            "rating": rating,
            "key_strengths": [],
            "citation_count": None
        },
        "tags": tags
    }
    entry.setdefault("notes_and_flags", {})["acceptance_action"] = acceptance.action
    entry["notes_and_flags"]["acceptance_reason"] = acceptance.reason
    entry["notes_and_flags"]["first_pass_finalize_required"] = True
    if llm_force_acceptance and text_len < min_text_chars:
        entry["notes_and_flags"]["text_recovery_required"] = (
            f"llm_forced_ingest;extracted_chars={text_len}"
        )

    # Step 9: DB/source transaction (single entry point, after acceptance)
    db_before = papers_db.get_paper_count()
    if not commit_accepted_corpus_paper(
        entry=entry,
        staging_pdf=pdf_path,
        dest_pdf=dest_pdf,
        acceptance=acceptance,
        force_acceptance=llm_force_acceptance,
    ):
        return False

    # Step 10: Add to CSV
    try:
        total_csv = add_to_csv(entry)
    except Exception as e:
        log_error(paper_id, "CSV_WRITE", str(e))
        print(f"  CSV write failed: {e} (database still updated)")
        total_csv = "error"

    total_db = papers_db.get_paper_count()
    db_delta = total_db - db_before
    if db_delta != 1:
        reason = f"count_delta={db_delta} before={db_before} after={total_db}"
        log_error(paper_id, "DB_COUNT_ANOMALY", reason)
        print(
            "  Database count anomaly after insert "
            f"(expected +1, saw {db_delta}); rolling back accepted row for safety"
        )
        try:
            rollback_accepted_paper(
                paper_id,
                reason=f"db_count_anomaly:{reason}",
                audit_context={
                    "db_before": db_before,
                    "db_after": total_db,
                    "db_delta": db_delta,
                    "source_pdf": str(dest_pdf),
                },
            )
        except Exception as rb_e:
            log_error(paper_id, "DB_COUNT_ANOMALY_ROLLBACK", str(rb_e))
            print(f"  Rollback after DB count anomaly failed: {rb_e}")
        return False
    print(f"  Added: {paper_id}")
    print(f"     Title: {title[:70]}{'...' if len(title) > 70 else ''}")
    print(f"     Authors: {'; '.join(authors[:3])}{'...' if len(authors) > 3 else ''}")
    print(f"     Year: {year} | Journal: {journal}")
    print(f"     DOI: {doi or 'none'}")
    print(f"     Tags: {', '.join(tags)}")
    print(f"     DB: {total_db} papers (delta +{db_delta}) | CSV: {total_csv} rows")

    # Auto-remove from papers-needed.md if this paper was on the list
    if doi or title:
        try:
            manage_script = Path(__file__).parent / "manage_papers_needed.py"
            if manage_script.exists():
                rm_args = ["python3", str(manage_script), "--remove"]
                if doi:
                    rm_args += ["--doi", doi]
                elif title:
                    rm_args += ["--title", title]
                result = subprocess.run(rm_args, capture_output=True, text=True, timeout=30)
                if "Removed" in result.stdout:
                    print(f"  Auto-removed from papers-needed.md")
        except Exception:
            pass

    return True


def main():
    parser = argparse.ArgumentParser(description="Add paper(s) to database + CSV pipeline")
    parser.add_argument("pdfs", nargs="+", help="PDF file path(s)")
    parser.add_argument("--id", help="Paper ID (auto-generated if omitted)")
    parser.add_argument("--title", help="Paper title")
    parser.add_argument("--authors", help="Authors separated by semicolons")
    parser.add_argument("--year", help="Publication year")
    parser.add_argument("--journal", help="Journal name")
    parser.add_argument("--doi", help="DOI (without https://doi.org/)")
    parser.add_argument("--volume", help="Volume")
    parser.add_argument("--issue", help="Issue")
    parser.add_argument("--pages", help="Pages")
    parser.add_argument("--tags", default="alex-new", help="Comma-separated tags")
    parser.add_argument("--rating", help="Rating (landmark/strong/adequate/weak/flawed)")
    parser.add_argument("--extract-metadata", action="store_true",
                        help="Auto-extract metadata from CrossRef/PDF")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip pre-add backup (force skip on batches of 10+)")
    parser.add_argument("--force-backup", action="store_true",
                        help="Force pre-add backup even for small batches (<10 papers)")
    parser.add_argument("--no-ollama", action="store_true",
                        help="Skip Ollama tier in the metadata fallback chain")
    parser.add_argument("--no-claude", action="store_true",
                        help="Skip Claude API tier in the metadata fallback chain")

    args = parser.parse_args()
    args_dict = vars(args)

    pdfs = args_dict.pop("pdfs")
    no_backup = args_dict.pop("no_backup")
    force_backup = args_dict.pop("force_backup")

    # Expand wildcards (PowerShell doesn't auto-expand globs the way Unix shells do)
    import glob as _glob
    expanded = []
    for p in pdfs:
        if any(c in p for c in "*?["):
            matches = sorted(_glob.glob(p))
            if matches:
                expanded.extend(matches)
            else:
                print(f"  No matches for pattern: {p}")
        else:
            expanded.append(p)
    pdfs = expanded

    print("=" * 60)
    print(f"Paper Pipeline — {len(pdfs)} file(s)")
    print("=" * 60)

    current_count = papers_db.get_paper_count()
    print(f"\nCurrent database: {current_count} papers")

    # Rolling backup — auto for batches of 10+, skip for smaller adds
    # Daily 3AM cron handles routine backups
    is_batch = len(pdfs) >= 10
    do_backup = (is_batch and not no_backup) or force_backup
    if do_backup:
        print(f"\nCreating pre-add backup...")
        backup_path = papers_db.backup(suffix="PRE-ADD")
        if backup_path:
            size_mb = backup_path.stat().st_size / (1024 * 1024)
            print(f"  Backup saved: {backup_path} ({size_mb:.1f} MB)")
    elif is_batch and no_backup:
        print("\nBackup skipped (--no-backup)")
    else:
        print("\nBackup skipped (< 10 papers; use --force-backup to override)")

    # Process each PDF
    added = 0
    added_pdfs = []
    for pdf in pdfs:
        if process_paper(pdf, args_dict):
            added += 1
            added_pdfs.append(pdf)

    # Clean up successfully processed PDFs from staging
    if added > 0:
        for pdf in added_pdfs:
            p = Path(pdf)
            if STAGING in p.parents or p.parent == STAGING:
                try:
                    p.unlink()
                    print(f"  Removed from staging: {p.name}")
                except OSError:
                    pass

    final_count = papers_db.get_paper_count()
    print(f"\n{'=' * 60}")
    print(f"Done. Added {added}/{len(pdfs)} papers. DB total: {final_count}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
