"""Stage J: read-only final validation for the Koren parent-record pilot."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import config
from corpus_stage_d_preview import _read_pdf_metadata, _run_pdftotext

KOREN_NAME = "Koren-A kinetic study of protein-protein interactions-1976-B.pdf"
ISBN_RE = re.compile(r"\b(?:97[89][-\s]?)?\d[-\s]?\d{2,5}[-\s]?\d{2,7}[-\s]?\d{1,7}[-\s]?[\dX]\b")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _ascii_token(s: str) -> str:
    normalized = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^A-Za-z0-9]", "", normalized.encode("ascii", "ignore").decode("ascii"))


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


def _clean_lines(text: str, limit_chars: int | None = 180_000) -> list[str]:
    lines = []
    source = text[:limit_chars] if limit_chars is not None else text
    for raw in source.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return lines


def _unique(items: list[str], limit: int = 20) -> list[str]:
    out = []
    seen = set()
    for item in items:
        clean = re.sub(r"\s+", " ", item).strip(" ,;")
        if not clean or clean.lower() in seen:
            continue
        seen.add(clean.lower())
        out.append(clean)
        if len(out) >= limit:
            break
    return out


def _pages_from_text(text: str) -> list[list[str]]:
    return [_clean_lines(page, limit_chars=None) for page in text.split("\f") if page.strip()]


def _title_in_lines(lines: list[str]) -> bool:
    return any(_norm(line) == "handbook of emotions" for line in lines)


def _titlepage_score(lines: list[str]) -> tuple[int, list[str]]:
    low = "\n".join(lines).lower()
    reasons = []
    score = 0
    if _title_in_lines(lines):
        score += 10
        reasons.append("exact title line")
    if "fourth edition" in low:
        score += 8
        reasons.append("edition marker")
    if "edited by" in low or re.search(r"\beditors?\b", low):
        score += 8
        reasons.append("editor marker")
    if "the guilford press" in low:
        score += 5
        reasons.append("publisher marker")
    if "isbn" in low:
        score += 2
        reasons.append("isbn marker")
    if "also available" in low:
        score -= 12
        reasons.append("also-available front matter penalty")
    if "contents" in low:
        score -= 6
        reasons.append("contents page penalty")
    return score, reasons


def _titlepage_candidates(pages: list[list[str]]) -> list[dict[str, Any]]:
    candidates = []
    for page_index, lines in enumerate(pages[:80], start=1):
        score, reasons = _titlepage_score(lines)
        if not _title_in_lines(lines) and not (page_index <= 10 and score >= 16):
            continue
        candidates.append(
            {
                "page": page_index,
                "score": score,
                "reasons": reasons,
                "lines": lines[:80],
            }
        )
    return sorted(candidates, key=lambda c: (c["score"], -c["page"]), reverse=True)


BAD_NAME_TERMS = {
    "also available",
    "altering fate",
    "consciousness",
    "deception",
    "development",
    "emotion and",
    "guilford",
    "handbook",
    "isbn",
    "lying",
    "mind in context",
    "past does not predict",
    "psychological construction",
    "rise of consciousness",
}


def _looks_like_person_name(text: str) -> bool:
    clean = re.sub(r"\s+", " ", text).strip(" ,.;:")
    low = clean.lower()
    if not (5 <= len(clean) <= 80):
        return False
    if any(term in low for term in BAD_NAME_TERMS):
        return False
    tokens = [t for t in re.split(r"\s+", clean) if t]
    if not 2 <= len(tokens) <= 5:
        return False
    alpha_tokens = [re.sub(r"[^A-Za-z-]", "", t) for t in tokens]
    if len([t for t in alpha_tokens if len(t) >= 2]) < 2:
        return False
    return sum(1 for t in tokens if t[:1].isupper()) >= 2


def _split_person_names(raw: str) -> list[str]:
    text = re.sub(r"\s+", " ", raw).strip(" ,.;:")
    text = re.sub(r"(?i)^edited by\s+", "", text).strip(" ,.;:")
    text = re.sub(r"(?i)\bfourth edition\b.*$", "", text).strip(" ,.;:")
    text = re.sub(r"(?i)\bthe guilford press\b.*$", "", text).strip(" ,.;:")
    text = text.replace(" and ", ", ")
    parts = [p.strip(" ,.;:") for p in text.split(",") if p.strip(" ,.;:")]
    return _unique([p for p in parts if _looks_like_person_name(p)], 8)


def _extract_titlepage_editor_candidate(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if not candidate:
        return {"source": "title_page", "names": [], "confidence": "low", "evidence": [], "rejected": "no title-page candidate"}
    lines = candidate.get("lines", [])
    page_low = "\n".join(lines).lower()
    if "also available" in page_low and "fourth edition" not in page_low:
        return {"source": "title_page", "names": [], "confidence": "low", "evidence": lines[:20], "rejected": "advertising/front-matter page"}
    for i, line in enumerate(lines):
        low = line.lower()
        if "edited by" not in low:
            continue
        inline = re.sub(r"(?i)^.*?edited by", "", line).strip(" :")
        follow = []
        for next_line in lines[i + 1 : i + 8]:
            nlow = next_line.lower()
            if any(stop in nlow for stop in ("the guilford press", "new york", "london", "copyright", "isbn", "contents")):
                break
            if _norm(next_line) == "handbook of emotions" or nlow == "fourth edition":
                continue
            follow.append(next_line)
        names = _unique([name for name in ([inline] if inline else []) + follow if _looks_like_person_name(name)], 8)
        if not names:
            block = " ".join([inline] + follow)
            names = _split_person_names(block)
        evidence = [line] + follow
        if names:
            return {"source": "title_page", "names": names, "confidence": "medium", "evidence": evidence, "rejected": None}
    return {"source": "title_page", "names": [], "confidence": "low", "evidence": lines[:30], "rejected": "no clean edited-by block"}


def _extract_rejected_editor_marker_candidates(pages: list[list[str]], selected_page: int | None) -> list[dict[str, Any]]:
    rejected = []
    for page_index, lines in enumerate(pages[:80], start=1):
        for i, line in enumerate(lines):
            if "edited by" not in line.lower():
                continue
            follow = lines[i + 1 : i + 8]
            block = " ".join([re.sub(r"(?i)^.*?edited by", "", line).strip(" :")] + follow)
            names = _split_person_names(block)
            evidence = [line] + follow
            if page_index == selected_page:
                continue
            rejected.append(
                {
                    "source": "other_front_matter_editor_marker",
                    "page": page_index,
                    "names": names,
                    "confidence": "low",
                    "evidence": evidence,
                    "rejected": "editor marker is not on selected title page or CIP/cataloging page",
                }
            )
            if len(rejected) >= 12:
                return rejected
    return rejected


def _extract_cip_editor_candidates(pages: list[list[str]]) -> list[dict[str, Any]]:
    candidates = []
    for page_index, lines in enumerate(pages[:80], start=1):
        page_text = "\n".join(lines)
        low = page_text.lower()
        if not ("handbook of emotions" in low and ("cataloging" in low or "edited by" in low or "isbn" in low)):
            continue
        for match in re.finditer(r"(?is)edited by\s+(.{10,220}?)(?:\.|--|—|ISBN|Includes bibliographical|Library of Congress|$)", page_text):
            names = _split_person_names(match.group(1))
            if names:
                candidates.append(
                    {
                        "source": "cip_or_cataloging",
                        "page": page_index,
                        "names": names,
                        "confidence": "medium",
                        "evidence": [line for line in lines if "edited by" in line.lower() or any(n in line for n in names)][:12],
                    }
                )
    return candidates


def _extract_edition_evidence(pages: list[list[str]]) -> dict[str, Any]:
    candidates = []
    for page_index, lines in enumerate(pages[:80], start=1):
        for line in lines:
            if "edition" in line.lower() and len(line) <= 100:
                candidates.append({"page": page_index, "line": line})
    selected = next((c["line"] for c in candidates if "fourth edition" in c["line"].lower()), None)
    return {"edition": selected, "candidates": candidates[:20]}


def _extract_copyright_evidence(pages: list[list[str]]) -> dict[str, Any]:
    evidence = []
    years = []
    for lines in pages[:80]:
        for line in lines:
            low = line.lower()
            if any(k in low for k in ("copyright", "©", "the guilford press", "isbn")):
                evidence.append(line)
                years.extend(YEAR_RE.findall(line))
    modern = [y for y in years if int(y) >= 1990]
    return {"copyright_year": max(modern or years) if years else None, "evidence": _unique(evidence, 20)}


def _extract_toc_evidence(pages: list[list[str]]) -> dict[str, Any]:
    for page_index, lines in enumerate(pages[:100], start=1):
        if lines and lines[0].lower() == "contents":
            return {"toc_found": True, "page": page_index, "evidence": lines[:50]}
    for page_index, lines in enumerate(pages[:100], start=1):
        if any(line.lower() == "contents" for line in lines[:5]):
            return {"toc_found": True, "page": page_index, "evidence": lines[:50]}
    return {"toc_found": False, "page": None, "evidence": []}


def _validate_editor_candidates(title_candidate: dict[str, Any], cip_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    all_candidates = [title_candidate] + cip_candidates
    named = [c for c in all_candidates if c.get("names")]
    if not named:
        return {
            "status": "unresolved",
            "validated_names": [],
            "confidence": "low",
            "reason": "no clean title-page or CIP editor candidate",
            "candidate_lists": all_candidates,
        }
    primary = named[0]
    primary_names = primary["names"]
    corroborated = False
    for other in named[1:]:
        overlap = set(primary_names) & set(other["names"])
        if len(overlap) >= min(2, len(primary_names)):
            corroborated = True
            break
    confidence = "high" if corroborated else primary.get("confidence", "medium")
    return {
        "status": "validated",
        "validated_names": primary_names,
        "confidence": confidence,
        "reason": "clean explicit edited-by block" + (" corroborated by CIP/cataloging evidence" if corroborated else ""),
        "candidate_lists": all_candidates,
    }


def extract_koren_titlepage_editor_preview(text: str, pdf_meta: dict[str, Any]) -> dict[str, Any]:
    pages = _pages_from_text(text)
    candidates = _titlepage_candidates(pages)
    selected = candidates[0] if candidates else None
    title_editor_candidate = _extract_titlepage_editor_candidate(selected)
    cip_candidates = _extract_cip_editor_candidates(pages)
    editor_validation = _validate_editor_candidates(title_editor_candidate, cip_candidates)
    rejected_editor_markers = _extract_rejected_editor_marker_candidates(pages, selected.get("page") if selected else None)
    edition_info = _extract_edition_evidence(pages)
    copyright_info = _extract_copyright_evidence(pages)
    toc_info = _extract_toc_evidence(pages)
    selected_lines = selected.get("lines", []) if selected else []
    title = "Handbook of Emotions" if selected else None
    edition = edition_info.get("edition")
    publisher = "The Guilford Press" if any("guilford press" in line.lower() for page in pages[:80] for line in page) else None
    isbns = _extract_isbns("\n".join("\n".join(p) for p in pages[:80]))
    year = copyright_info.get("copyright_year")
    return {
        "title": title,
        "subtitle": None,
        "edition": edition,
        "editors_or_authors": editor_validation["validated_names"],
        "editor_validation_status": editor_validation["status"],
        "editor_validation_confidence": editor_validation["confidence"],
        "editor_validation_reason": editor_validation["reason"],
        "publisher": publisher,
        "publication_year": year,
        "copyright_year": copyright_info.get("copyright_year"),
        "isbn_candidates": isbns,
        "document_type": "book",
        "whole_book_or_other": "whole_book",
        "title_page_candidates": candidates[:8],
        "selected_title_page": selected,
        "candidate_editor_lists": editor_validation["candidate_lists"] + rejected_editor_markers,
        "title_page_evidence": selected_lines[:80],
        "edition_evidence": edition_info,
        "copyright_page_evidence": copyright_info["evidence"],
        "cataloging_cip_evidence": cip_candidates,
        "toc_evidence": toc_info,
        "pdf_metadata": pdf_meta,
    }


def _title_index(lines: list[str]) -> int | None:
    candidates: list[tuple[int, int]] = []
    for i, line in enumerate(lines[:800]):
        if _norm(line) == "handbook of emotions":
            window = " ".join(lines[i : min(i + 80, len(lines))]).lower()
            score = 10
            if i + 1 < len(lines) and lines[i + 1].lower() == "also available":
                score -= 10
            if "fourth edition" in window:
                score += 8
            if "edited by" in window:
                score += 5
            if "the guilford press" in window:
                score += 5
            candidates.append((score, i))
    if not candidates:
        return None
    return sorted(candidates, reverse=True)[0][1]


def _extract_edition(lines: list[str], title_idx: int | None) -> str | None:
    if title_idx is None:
        return None
    for line in lines[max(0, title_idx - 15) : title_idx + 80]:
        if "edition" in line.lower() and len(line) < 80:
            return line
    return None


def _extract_editors(lines: list[str], title_idx: int | None) -> tuple[list[str], list[str]]:
    evidence = []
    if title_idx is None:
        return [], evidence
    window = lines[title_idx : min(title_idx + 140, len(lines))]
    editors: list[str] = []
    for i, line in enumerate(window):
        low = line.lower()
        if low.startswith("edited by") or low == "edited by":
            evidence.append(line)
            candidate_text = line[len("Edited by") :].strip(" :") if low.startswith("edited by") else ""
            follow = []
            for next_line in window[i + 1 : i + 10]:
                nlow = next_line.lower()
                if any(stop in nlow for stop in ("the guilford press", "new york", "london", "copyright", "isbn", "fourth edition")):
                    break
                if nlow.startswith("also available"):
                    break
                follow.append(next_line)
            joined = re.sub(r"\s+", " ", " ".join([candidate_text] + follow)).strip()
            joined = re.sub(r"\band\b", ",", joined)
            parts = [p.strip(" ,") for p in joined.split(",") if p.strip(" ,")]
            editors = [
                p
                for p in parts
                if 3 <= len(p) <= 80
                and not p.lower().startswith("edited")
                and not any(bad in p.lower() for bad in ("guilford", "available", "lying", "deception", "mind in context"))
            ]
            if editors:
                evidence.extend(follow[:6])
                break
    return _unique(editors, 8), evidence


def _extract_year(lines: list[str], text: str, title_idx: int | None) -> tuple[str | None, list[str]]:
    evidence = []
    priority: list[str] = []
    if title_idx is not None:
        window = lines[title_idx : min(title_idx + 180, len(lines))]
        for line in window:
            if any(k in line.lower() for k in ("copyright", "©", "published", "guilford", "edition")):
                years = YEAR_RE.findall(line)
                if years:
                    priority.extend(years)
                    evidence.append(line)
    if not priority:
        for line in lines[:1200]:
            if any(k in line.lower() for k in ("copyright", "©", "isbn", "guilford")):
                years = YEAR_RE.findall(line)
                if years:
                    priority.extend(years)
                    evidence.append(line)
    if priority:
        modern = [y for y in priority if int(y) >= 1990]
        return (max(modern or priority), _unique(evidence, 8))
    years = YEAR_RE.findall(text[:120_000])
    return (max(years) if years else None, [])


def _extract_isbns(text: str) -> list[str]:
    candidates = []
    for raw in ISBN_RE.findall(text[:160_000]):
        clean = raw.strip()
        digits = re.sub(r"[^0-9X]", "", clean.upper())
        if len(digits) in {10, 13} and digits.startswith(("97", "0", "1")):
            candidates.append(clean)
    return _unique(candidates, 12)


def _extract_publisher(lines: list[str]) -> tuple[str | None, list[str]]:
    evidence = [line for line in lines[:1000] if "guilford press" in line.lower()]
    return ("The Guilford Press" if evidence else None, _unique(evidence, 5))


def _metadata_from_text(text: str, pdf_meta: dict[str, Any]) -> dict[str, Any]:
    focused = extract_koren_titlepage_editor_preview(text, pdf_meta)
    title = focused.get("title")
    edition = focused.get("edition")
    editors = focused.get("editors_or_authors") or []
    year = focused.get("publication_year")
    publisher = focused.get("publisher")
    isbns = focused.get("isbn_candidates") or []
    editor_validated = focused.get("editor_validation_status") == "validated"
    evidence = {
        "title_lines": focused.get("title_page_evidence", [])[:20],
        "editor_evidence": focused.get("candidate_editor_lists", []),
        "editor_evidence_rejected": not editor_validated,
        "editor_validation_status": focused.get("editor_validation_status"),
        "editor_validation_reason": focused.get("editor_validation_reason"),
        "year_evidence": focused.get("copyright_page_evidence", [])[:12],
        "publisher_evidence": [line for line in focused.get("copyright_page_evidence", []) if "guilford" in line.lower()][:5],
        "focused_titlepage_preview": focused,
        "pdf_metadata": pdf_meta,
    }
    confidence_parts = [bool(title), editor_validated, bool(year), bool(publisher), bool(isbns)]
    confidence = "high" if sum(confidence_parts) >= 5 else "medium" if sum(confidence_parts) >= 3 else "low"
    return {
        "title": title,
        "authors_or_editors": editors,
        "year": year,
        "publisher": publisher,
        "isbn_candidates": isbns,
        "doi": None,
        "edition": edition,
        "document_type": "book",
        "document_type_confidence": "high" if title and publisher and isbns else "medium",
        "whole_book_or_other": "whole_book",
        "parent_only_representation_sufficient_for_now": True,
        "child_chapter_records_should_be_deferred": True,
        "editor_validation_status": focused.get("editor_validation_status"),
        "editor_validation_confidence": focused.get("editor_validation_confidence"),
        "confidence": confidence,
        "evidence": evidence,
    }


def _db_rows() -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, document_type, basic_metadata, file_info FROM papers").fetchall()
    finally:
        conn.close()
    return [
        {
            "id": row["id"],
            "document_type": row["document_type"],
            "basic_metadata": _json_obj(row["basic_metadata"]),
            "file_info": _json_obj(row["file_info"]),
        }
        for row in rows
    ]


def _duplicate_reassessment(metadata: dict[str, Any], staged_path: Path, staged_hash: str) -> dict[str, Any]:
    matches = []
    title = metadata.get("title") or ""
    year = str(metadata.get("year") or "")
    isbns = {re.sub(r"[^0-9X]", "", x.upper()) for x in metadata.get("isbn_candidates", [])}
    editors = {_norm(str(e).split()[-1]) for e in metadata.get("authors_or_editors", []) if str(e).split()}
    for row in _db_rows():
        bm = row["basic_metadata"]
        fi = row["file_info"]
        signals = []
        db_title = str(bm.get("title") or "")
        title_ratio = SequenceMatcher(None, _norm(title), _norm(db_title)).ratio() if title and db_title else 0.0
        if title_ratio >= 0.90:
            signals.append(f"title_{int(title_ratio * 100)}")
        db_year = str(bm.get("year") or "")
        if year and db_year and year == db_year:
            signals.append("year_exact")
        db_people = bm.get("authors") or []
        db_surnames = {_norm(str(p).split()[-1]) for p in db_people if str(p).split()}
        if editors and db_surnames and editors & db_surnames:
            signals.append("editor_or_author_overlap")
        raw_json = json.dumps({"basic_metadata": bm, "file_info": fi}, ensure_ascii=False)
        db_isbns = {re.sub(r"[^0-9X]", "", x.upper()) for x in isbns if x and x in raw_json}
        if db_isbns:
            signals.append("isbn_text_match")
        renamed = fi.get("renamed_filename")
        if renamed:
            candidate = config.SOURCE_PDFS / str(renamed)
            try:
                if candidate.exists() and candidate.stat().st_size == staged_path.stat().st_size and _sha256(candidate) == staged_hash:
                    signals.append("managed_pdf_hash_match")
            except OSError:
                pass
        strong = (
            any(s.startswith("title_") for s in signals)
            or "isbn_text_match" in signals
            or "managed_pdf_hash_match" in signals
        )
        if strong:
            matches.append(
                {
                    "id": row["id"],
                    "document_type": row["document_type"],
                    "title": bm.get("title"),
                    "year": bm.get("year"),
                    "doi": bm.get("doi"),
                    "signals": signals,
                    "title_ratio": round(title_ratio, 3),
                }
            )
    if any(
        "managed_pdf_hash_match" in m["signals"]
        or ("isbn_text_match" in m["signals"] and any(s.startswith("title_") for s in m["signals"]))
        for m in matches
    ):
        status = "exact_duplicate_skip_candidate"
    elif matches:
        status = "possible_duplicate_needs_machine_comparison"
    else:
        status = "no_duplicate_signal"
    return {"status": status, "matches": matches[:10]}


def _safe_id_preview(metadata: dict[str, Any], duplicate_status: str) -> dict[str, Any]:
    if duplicate_status != "no_duplicate_signal":
        return {"candidate_id": None, "confidence": "low", "reason": "duplicate reassessment is not clear"}
    editors = metadata.get("authors_or_editors") or []
    if metadata.get("confidence") not in {"high", "medium"} or not editors:
        return {"candidate_id": None, "confidence": "low", "reason": "author/editor evidence is insufficient"}
    surname = _ascii_token(str(editors[0]).split()[-1])
    title_token = next((_ascii_token(w) for w in str(metadata.get("title") or "").split() if len(_ascii_token(w)) > 3), "")
    year = metadata.get("year")
    if not surname or not title_token or not year:
        return {"candidate_id": None, "confidence": "low", "reason": "missing safe surname/title/year token"}
    if surname.lower() in {"koren", "unknown", "editor", "author", "publisher"} and not any("Koren" in e for e in metadata["evidence"].get("editor_evidence", [])):
        return {"candidate_id": None, "confidence": "low", "reason": "primary token is from filename or otherwise unsupported"}
    return {
        "candidate_id": f"{surname}_{year}_{title_token}_book",
        "confidence": "medium",
        "reason": "preview only; based on validated title-page editor/title/year evidence",
    }


def _managed_destination_preview(candidate_id: str | None) -> str | None:
    if not candidate_id:
        return None
    return str(config.SOURCE_PDFS / f"{candidate_id}.pdf")


def _row_preview(metadata: dict[str, Any], staged_path: Path, staged_hash: str, id_preview: dict[str, Any]) -> dict[str, Any] | None:
    candidate_id = id_preview.get("candidate_id")
    if not candidate_id:
        return None
    return {
        "id_preview": candidate_id,
        "document_type": metadata.get("document_type"),
        "basic_metadata": {
            "title": metadata.get("title"),
            "authors": metadata.get("authors_or_editors"),
            "year": metadata.get("year"),
            "publisher": metadata.get("publisher"),
            "doi": metadata.get("doi") or "",
            "isbn": metadata.get("isbn_candidates", []),
            "edition": metadata.get("edition"),
            "language": "English",
            "article_type": "book_parent",
        },
        "file_info": {
            "original_filename": staged_path.name,
            "staged_sha256": staged_hash,
            "managed_pdf_destination_preview": _managed_destination_preview(candidate_id),
            "file_format": "pdf",
            "readability_status": "readable",
        },
        "tags": [
            "book_parent",
            "guideline_mismatch_or_new_guideline_needed_candidate",
            "not_journal_article_evaluation",
        ],
        "evaluation_status": "deferred_not_evaluated_for_book_parent",
        "child_chapter_policy": "defer child/chapter records until chapter metadata is separately validated",
    }


def _koren_staged_path(plan: dict[str, Any]) -> Path:
    koren_plans = [p for p in plan.get("pdf_plans", []) if p.get("name") == KOREN_NAME]
    if len(koren_plans) != 1:
        raise RuntimeError("Koren PDF plan not found exactly once")
    return Path(koren_plans[0]["path"])


def run_koren_titlepage_editor_preview(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    staged_path = _koren_staged_path(plan)
    text, text_error = _run_pdftotext(staged_path, first_pages_only=False)
    pdf_meta = _read_pdf_metadata(staged_path)
    preview = extract_koren_titlepage_editor_preview(text, pdf_meta)
    summary = {
        "run_id": run_dir.name,
        "stage": "koren-titlepage-editor-preview",
        "processed_only": [KOREN_NAME],
        "source_artifacts": {
            "stage_d": str(run_dir / "text_metadata_preview.json"),
            "stage_e": str(run_dir / "metadata_duplicate_resolution_preview.json"),
            "stage_f": str(run_dir / "final_route_plan_preview.json"),
            "stage_g": str(run_dir / "book_toc_chapter_preview.json"),
            "stage_h": str(run_dir / "final_pilot_execution_plan.json"),
            "stage_i": str(run_dir / "live_duplicate_skip_log.json"),
            "stage_j_previous": str(run_dir / "koren_parent_validation_preview.json"),
        },
        "text_extraction": {"chars": len(text), "error": text_error},
        "titlepage_editor_preview": preview,
        "side_effects": {
            "api_calls": "none",
            "papers_db_writes": "none",
            "new_db_rows": "none",
            "existing_db_row_updates": "none",
            "pdf_moves_copies_renames_deletes": "none",
            "id_changes": "none",
            "evaluations": "none",
        },
    }
    (run_dir / "koren_titlepage_editor_preview.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "stage_koren_titlepage_editor_preview_report.txt").write_text(render_koren_titlepage_editor_report(summary), encoding="utf-8")
    return summary


def run_koren_parent_validation_preview(plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    staged_path = _koren_staged_path(plan)
    text, text_error = _run_pdftotext(staged_path, first_pages_only=False)
    pdf_meta = _read_pdf_metadata(staged_path)
    staged_hash = _sha256(staged_path)
    metadata = _metadata_from_text(text, pdf_meta)
    duplicate = _duplicate_reassessment(metadata, staged_path, staged_hash)
    id_preview = _safe_id_preview(metadata, duplicate["status"])
    row_preview = _row_preview(metadata, staged_path, staged_hash, id_preview)
    ready = (
        metadata.get("confidence") in {"high", "medium"}
        and duplicate["status"] == "no_duplicate_signal"
        and id_preview.get("candidate_id")
        and row_preview is not None
    )
    decision = "ready_for_parent_record_live_pilot" if ready else (
        "exact_duplicate_skip_candidate" if duplicate["status"] == "exact_duplicate_skip_candidate" else "not_ready_needs_more_machine_escalation"
    )
    summary = {
        "run_id": run_dir.name,
        "stage": "koren-parent-validation-preview",
        "processed_only": [KOREN_NAME],
        "source_artifacts": {
            "stage_d": str(run_dir / "text_metadata_preview.json"),
            "stage_e": str(run_dir / "metadata_duplicate_resolution_preview.json"),
            "stage_f": str(run_dir / "final_route_plan_preview.json"),
            "stage_g": str(run_dir / "book_toc_chapter_preview.json"),
            "stage_h": str(run_dir / "final_pilot_execution_plan.json"),
            "stage_i": str(run_dir / "live_duplicate_skip_log.json"),
            "focused_titlepage_editor_preview": str(run_dir / "koren_titlepage_editor_preview.json"),
        },
        "text_extraction": {"chars": len(text), "error": text_error},
        "canonical_metadata_validation": metadata,
        "duplicate_reassessment": duplicate,
        "safe_parent_id_preview": id_preview,
        "proposed_parent_row_preview": row_preview,
        "child_chapter_policy": "parent-only representation is sufficient for now; child/chapter records deferred",
        "live_readiness_decision": decision,
        "human_review_needed_now": False,
        "later_stages_still_disabled": [
            "DB row creation/update",
            "managed PDF copy/move",
            "safe ID commit",
            "paid evaluation",
            "batch submit",
            "batch collect/save",
            "duplicate skip execution beyond run-level audit",
            "PDF replacement",
            "book/chapter splitting writes",
            "human review folder creation outside the run folder",
        ],
        "side_effects": {
            "api_calls": "none",
            "papers_db_writes": "none",
            "new_db_rows": "none",
            "existing_db_row_updates": "none",
            "pdf_moves_copies_renames_deletes": "none",
            "id_changes": "none",
            "evaluations": "none",
        },
    }
    (run_dir / "koren_parent_validation_preview.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "stage_koren_parent_validation_preview_report.txt").write_text(render_koren_parent_validation_report(summary), encoding="utf-8")
    return summary


def render_koren_titlepage_editor_report(summary: dict[str, Any]) -> str:
    preview = summary["titlepage_editor_preview"]
    lines = ["Koren Title-Page / Editor Preview (READ-ONLY)", "=" * 56]
    lines.append(f"Title: {preview.get('title')}")
    lines.append(f"Edition: {preview.get('edition')}")
    lines.append(f"Editors/authors: {preview.get('editors_or_authors')}")
    lines.append(
        f"Editor validation: {preview.get('editor_validation_status')} "
        f"({preview.get('editor_validation_confidence')}) — {preview.get('editor_validation_reason')}"
    )
    lines.append(f"Publisher: {preview.get('publisher')}")
    lines.append(f"Publication year: {preview.get('publication_year')}")
    lines.append(f"ISBNs: {preview.get('isbn_candidates')}")
    lines.append(f"Title-page candidates: {len(preview.get('title_page_candidates', []))}")
    for cand in preview.get("title_page_candidates", [])[:3]:
        lines.append(f"  page {cand.get('page')} score={cand.get('score')} reasons={cand.get('reasons')}")
    lines.append(f"Candidate editor lists: {preview.get('candidate_editor_lists')}")
    lines.append(f"TOC found: {preview.get('toc_evidence', {}).get('toc_found')} page={preview.get('toc_evidence', {}).get('page')}")
    lines.append("")
    lines.append("No API calls, papers.db writes, DB row changes, PDF operations, evaluations, or ID changes were performed.")
    return "\n".join(lines) + "\n"


def render_koren_parent_validation_report(summary: dict[str, Any]) -> str:
    meta = summary["canonical_metadata_validation"]
    dup = summary["duplicate_reassessment"]
    lines = ["Stage J Koren Parent Validation Preview (READ-ONLY)", "=" * 58]
    lines.append(f"Title: {meta.get('title')}")
    lines.append(f"Editors/authors: {meta.get('authors_or_editors')}")
    lines.append(f"Year: {meta.get('year')} | Publisher: {meta.get('publisher')} | Edition: {meta.get('edition')}")
    lines.append(f"ISBNs: {meta.get('isbn_candidates')}")
    lines.append(f"Document type: {meta.get('document_type')} ({meta.get('document_type_confidence')})")
    lines.append(f"Metadata confidence: {meta.get('confidence')}")
    lines.append(f"Duplicate reassessment: {dup.get('status')} matches={len(dup.get('matches', []))}")
    lines.append(f"ID preview: {summary['safe_parent_id_preview']}")
    lines.append(f"Parent row preview available: {summary['proposed_parent_row_preview'] is not None}")
    lines.append(f"Child/chapter policy: {summary['child_chapter_policy']}")
    lines.append(f"Live readiness: {summary['live_readiness_decision']}")
    lines.append("Human review needed now: False")
    lines.append("")
    lines.append("No API calls, papers.db writes, DB row changes, PDF operations, evaluations, or ID changes were performed.")
    return "\n".join(lines) + "\n"
