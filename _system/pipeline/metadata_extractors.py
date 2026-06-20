#!/usr/bin/env python3
"""
metadata_extractors.py - Six-tier metadata fallback chain.

Each tier is a function that tries to extract paper metadata. The pipeline
calls tiers in config.METADATA_TIERS order until it has title + authors + year,
or runs out of tiers.

A tier returns dict with optional fields:
    title (str), authors (list[str]), year (str), journal (str),
    volume, issue, pages, abstract, keywords, doi,
    source (str) - tier name that produced the data,
    confidence (float, 0..1) - tier-self-assessment

If a tier finds nothing or errors, it returns {} (the chain moves on).
"""
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

# --- Portable path bootstrap (works both as imported module and as script) ---
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

YEAR_JOURNAL_TAIL_RX = re.compile(r"-\d{4}-[A-Za-z]{2,12}$")


# ---------------------------------------------------------------------------
# Tier 1: CrossRef DOI lookup
# ---------------------------------------------------------------------------
def extract_crossref(pdf_path, first_pages_text, doi=None):
    """DOI -> CrossRef API -> metadata. Free, fast, accurate when DOI present."""
    if not doi:
        return {}
    try:
        import requests
        url = "https://api.crossref.org/works/" + doi
        headers = {"User-Agent": config.CROSSREF_USER_AGENT}
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return {}
        item = r.json().get("message", {})

        authors = []
        for a in item.get("author", []):
            given = a.get("given", "")
            family = a.get("family", "")
            if given and family:
                authors.append(given + " " + family)
            elif family:
                authors.append(family)

        title_list = item.get("title", [])
        title = title_list[0] if title_list else None

        journal_list = item.get("container-title", [])
        journal = journal_list[0] if journal_list else None

        year = None
        for date_field in ["published-print", "published-online", "issued"]:
            parts = item.get(date_field, {}).get("date-parts", [[]])
            if parts and parts[0] and parts[0][0]:
                year = str(parts[0][0])
                break

        abstract = item.get("abstract", "")
        if abstract:
            abstract = re.sub(r"<[^>]+>", "", abstract).strip()

        if title or authors:
            return {
                "title": title,
                "authors": authors,
                "year": year,
                "journal": journal,
                "volume": item.get("volume"),
                "issue": item.get("issue"),
                "pages": item.get("page"),
                "abstract": abstract,
                "keywords": item.get("subject", []),
                "doi": doi,
                "source": "crossref",
                "confidence": 0.95,
            }
    except Exception as e:
        print("  [tier crossref] failed:", e)
    return {}


# ---------------------------------------------------------------------------
# Tier 2: PDF embedded metadata (/Title /Author fields)
# ---------------------------------------------------------------------------
def extract_pdf_embed(pdf_path, first_pages_text=None, doi=None):
    """Read /Title /Author from PDF metadata block via pypdf. Free, instant."""
    from pdf_content_guard import read_pdf_metadata_safely

    meta = read_pdf_metadata_safely(Path(pdf_path), tier="pdf_embed")
    if meta.get("error"):
        return {}
    try:
        title = str(meta.get("title") or "").strip()
        author_str = str(meta.get("author") or "").strip()

        junk_titles = {"untitled", "microsoft word", "microsoft powerpoint",
                       "pdf document", "document1", "untitled.pdf"}
        if title.lower() in junk_titles or len(title) < 5:
            title = None
        else:
            from document_evidence import is_invalid_canonical_title

            if is_invalid_canonical_title(title)[0]:
                title = None

        authors = []
        if author_str:
            for sep in [";", ",", " and "]:
                if sep in author_str:
                    authors = [a.strip() for a in author_str.split(sep) if a.strip()]
                    break
            if not authors:
                authors = [author_str]

        if title or authors:
            return {
                "title": title,
                "authors": authors,
                "source": "pdf_embed",
                "confidence": 0.6,
            }
    except Exception:
        return {}
    return {}


# ---------------------------------------------------------------------------
# Tier 3: Claude API (Haiku model)
# ---------------------------------------------------------------------------
_CLAUDE_PROMPT = """Extract bibliographic metadata from this academic paper text.

Return ONLY a JSON object with these exact keys (use null for any you cannot determine):
{
  "title": "exact paper title as printed",
  "authors": ["First Last", "First Last"],
  "year": "YYYY",
  "journal": "journal name",
  "doi": "10.xxxx/yyy or null"
}

Do not invent or guess. If a field is not clearly present in the text, use null.

Paper text:
"""


def extract_claude(pdf_path, first_pages_text, doi=None):
    """DeepSeek-first metadata extraction (escalates to Sonnet/Opus if needed)."""
    if not first_pages_text or len(first_pages_text.strip()) < 50:
        return {}
    try:
        from pipeline_llm import call_llm_escalation

        text, _usage, _mk = call_llm_escalation(
            "Extract bibliographic metadata. Return JSON only.",
            _CLAUDE_PROMPT + first_pages_text[:4000],
            max_tokens=600,
        )
        m = re.search(r'\{[^{}]*"title"[^{}]*\}', text, re.DOTALL)
        if not m:
            return {}
        data = json.loads(m.group(0))

        title = data.get("title") or None
        authors = data.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]
        year = data.get("year") or None
        journal = data.get("journal") or None
        ext_doi = data.get("doi") or None

        if title or authors:
            return {
                "title": title,
                "authors": authors,
                "year": str(year) if year else None,
                "journal": journal,
                "doi": ext_doi or doi,
                "source": "claude",
                "confidence": 0.9,
            }
    except Exception as e:
        print("  [tier claude] failed:", e)
    return {}


# ---------------------------------------------------------------------------
# Tier 4: Local Ollama LLM
# ---------------------------------------------------------------------------
_OLLAMA_PROMPT = """Extract the following from this academic paper text. Return ONLY valid JSON, nothing else.

{"title": "exact paper title", "authors": ["First Last", "First Last"], "year": "YYYY", "journal": "journal name or empty string"}

Text:
"""


def _sanitize_ollama_output(text):
    """Strip thinking preamble and conversational artifacts from local LLM output."""
    if not text or not isinstance(text, str):
        return text
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(
        r"^(Okay,?\s*(let\'?s?|so)|Let\'?s?\s|Let me\s|Here\'?s?\s|Sure,?\s|"
        r"I\'ll\s|Alright,?\s|Now,?\s|So,?\s).*?[\.\!\n]",
        "", text, count=1, flags=re.IGNORECASE
    ).strip()
    return text.strip()


def extract_ollama(pdf_path, first_pages_text, doi=None):
    """Local Ollama LLM. Free, slower, requires Ollama running with model pulled."""
    if not first_pages_text or len(first_pages_text.strip()) < 50:
        return {}
    try:
        import requests
        r = requests.post(config.OLLAMA_URL + "/api/generate", json={
            "model": config.OLLAMA_MODEL,
            "prompt": _OLLAMA_PROMPT + first_pages_text[:3000],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 500}
        }, timeout=180)
        if r.status_code != 200:
            return {}

        rdata = r.json()
        response_text = rdata.get("response", "")
        thinking_text = rdata.get("thinking", "")
        combined = response_text + "\n" + thinking_text

        m = re.search(r'\{[^{}]*"title"[^{}]*\}', combined, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                data = {}
            title = _sanitize_ollama_output(str(data.get("title", "")))
            authors = data.get("authors", [])
            if isinstance(authors, str):
                authors = [authors]
            year = data.get("year", "")
            journal = _sanitize_ollama_output(str(data.get("journal", "")))
            if title and len(title) > 5:
                return {
                    "title": title,
                    "authors": authors,
                    "year": str(year) if year else None,
                    "journal": journal or None,
                    "source": "ollama",
                    "confidence": 0.7,
                }
    except Exception as e:
        print("  [tier ollama] unavailable:", e)
    return {}


# ---------------------------------------------------------------------------
# Tier 5: Filename regex
# ---------------------------------------------------------------------------
def extract_filename_human(pdf_path, first_pages_text=None, doi=None):
    """Parse common human-readable filename patterns (Author - Title, etc.)."""
    stem = Path(pdf_path).stem
    original = stem

    # Strip pipeline timestamp suffix clusters (_1780044968_1780046615...)
    stem = re.sub(r"(?:_\d{7,})+$", "", stem).strip()
    stem = re.sub(r"(?:_\d{7,})+", " ", stem).strip()

    year = None
    m_year_paren = re.search(r"\((\d{4})\)\s*$", stem)
    if m_year_paren:
        year = m_year_paren.group(1)
        stem = stem[: m_year_paren.start()].strip()

    title = None
    authors: list[str] = []

    if " - " in stem:
        left, right = stem.split(" - ", 1)
        left, right = left.strip(), right.strip()
        if len(left) >= 2 and len(right) >= 4:
            if re.search(r"\d{4}", left) and not re.search(r"\d{4}", right):
                m = re.search(r"(\d{4})", left)
                if m:
                    year = year or m.group(1)
                authors = [re.sub(r"\s+\d{4}.*", "", left).strip()]
                title = right
            else:
                authors = [a.strip() for a in re.split(r",\s*", left) if a.strip()]
                if len(authors) == 1 and len(authors[0]) < 4:
                    authors = [authors[0]]
                title = right
    elif re.match(r"^[A-Za-z]+-\S", stem):
        parts = stem.split("-", 2)
        if len(parts) >= 3 and re.match(r"^\d{4}$", parts[2][:4] if len(parts[2]) >= 4 else ""):
            year = parts[2][:4]
            title = parts[2][4:].strip(" -_") or parts[1]
            authors = [parts[0]]
        elif len(parts) >= 2:
            title = parts[-1].replace("_", " ").strip()
            authors = [parts[0]]

    if not title:
        m = re.match(r"^(.+?)\s+(\d{4})\s*$", stem)
        if m:
            title = m.group(1).strip()
            year = year or m.group(2)
            if not authors and " " in title:
                words = title.split()
                if words[-1][0].isupper() and len(words[-1]) > 2:
                    authors = [words[-1]]
                    title = " ".join(words[:-1])

    if title:
        title = YEAR_JOURNAL_TAIL_RX.sub("", title).strip()
        title = re.sub(r"-\d{4}$", "", title).strip()
        title = re.sub(r"[_\s]+", " ", title).strip()
    if not authors and title:
        words = [w for w in re.split(r"[\s,]+", original) if w and w[0].isupper()]
        if words and len(words[0]) > 2:
            authors = [words[0]]

    if title and len(title) >= 4:
        return {
            "title": title,
            "authors": authors,
            "year": year,
            "source": "filename_human",
            "confidence": 0.55,
        }

    # Plain descriptive filename (e.g. "A Probabilistic Atlas of the Human Brain Theory and Rational")
    plain = re.sub(r"[_\s]+", " ", stem).strip()
    if len(plain) >= 12 and " " in plain and re.search(r"[A-Za-z]{3,}", plain):
        if not re.match(r"^(unknown|unidentified|uow)", plain, re.I):
            return {
                "title": plain,
                "authors": authors or [],
                "year": year,
                "source": "filename_plain_title",
                "confidence": 0.4,
            }
    return {}


def extract_clean_title_from_filename(pdf_path) -> str:
    """Best-effort title from filename stem after stripping timestamp noise."""
    data = extract_filename_human(pdf_path)
    title = str(data.get("title") or "").strip()
    if title:
        return title
    stem = Path(pdf_path).stem
    stem = re.sub(r"(?:_\d{7,})+$", "", stem).strip()
    stem = re.sub(r"(?:_\d{7,})+", " ", stem).strip()
    plain = re.sub(r"[_\s]+", " ", stem).strip()
    if len(plain) >= 8 and not re.match(r"^(unknown|unidentified)", plain, re.I):
        return plain
    return ""


def extract_first_page_heuristic(pdf_path, first_pages_text="", doi=None):
    """Infer title/authors/year from first-page text layout."""
    text = (first_pages_text or "").strip()
    if len(text) < 80:
        return {}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if len(ln) > 3 and not re.match(r"^(page|vol\.|doi:|http)", ln, re.I)]

    year = None
    ym = re.search(r"\b(19|20)\d{2}\b", text[:2000])
    if ym:
        year = ym.group(0)

    doi_found = doi
    if not doi_found:
        dm = re.search(r"10\.\d{4,9}/[^\s\])>]+", text[:3000])
        if dm:
            doi_found = dm.group(0).rstrip(".,;")

    title = None
    authors: list[str] = []
    for i, ln in enumerate(lines[:25]):
        if 20 <= len(ln) <= 220 and ln[0].isupper() and not re.match(r"^(abstract|introduction|keywords|summary)\b", ln, re.I):
            if not title:
                title = ln
                if i > 0 and 5 <= len(lines[i - 1]) <= 120:
                    prev = lines[i - 1]
                    if not re.search(r"\d{4}", prev) and prev[0].isupper():
                        authors = [a.strip() for a in re.split(r",|\band\b", prev) if a.strip()]
                break

    if title and len(title) >= 8:
        from document_evidence import is_invalid_canonical_title

        if is_invalid_canonical_title(title)[0]:
            return {}
        return {
            "title": title,
            "authors": authors,
            "year": year,
            "doi": doi_found,
            "source": "first_page_heuristic",
            "confidence": 0.45,
        }
    return {}


def extract_filename(pdf_path, first_pages_text=None, doi=None):
    """Parse Author_Year_Title.pdf filename pattern. Last-resort, low confidence."""
    stem = Path(pdf_path).stem
    m = re.match(r"^([A-Za-z]+?)_?(\d{4})_?(.*)", stem)
    if not m:
        return {}
    author = m.group(1)
    year = m.group(2)
    title = m.group(3).replace("_", " ").replace("-", " ").strip()
    if not title:
        title = stem.replace("_", " ").replace("-", " ").strip()
    return {
        "title": title or None,
        "authors": [author] if author else [],
        "year": year if year else None,
        "source": "filename",
        "confidence": 0.3,
    }


# ---------------------------------------------------------------------------
# Tier 6: Review queue (always succeeds; flags for human attention)
# ---------------------------------------------------------------------------
def extract_review(pdf_path, first_pages_text=None, doi=None):
    """Final fallback. Returns stub and flags for review queue.

    Caller should move the PDF to review/needs-metadata/ if this tier fires.
    """
    return {
        "title": None,
        "authors": [],
        "year": None,
        "journal": None,
        "source": "review",
        "confidence": 0.0,
        "needs_review": True,
    }


# ---------------------------------------------------------------------------
# Tier registry + orchestrator
# ---------------------------------------------------------------------------
TIERS = {
    "crossref":  extract_crossref,
    "pdf_embed": extract_pdf_embed,
    "claude":    extract_claude,
    "ollama":    extract_ollama,
    "filename":  extract_filename,
    "filename_human": extract_filename_human,
    "first_page_heuristic": extract_first_page_heuristic,
    "review":    extract_review,
}


def extract_metadata(pdf_path, first_pages_text="", doi=None, tier_order=None, verbose=True):
    """Run the metadata fallback chain.

    Returns merged dict with the best available fields and a `tiers_log`
    list showing which tiers fired and what they contributed.

    The chain stops once we have title + authors + year. Earlier-tier fields
    take precedence; later tiers fill in only what is missing.
    """
    tier_order = tier_order or config.METADATA_TIERS
    result = {
        "title": None,
        "authors": [],
        "year": None,
        "journal": None,
        "volume": None,
        "issue": None,
        "pages": None,
        "abstract": "",
        "keywords": [],
        "doi": doi,
        "tiers_log": [],
        "needs_review": False,
    }

    for tier_name in tier_order:
        if tier_name not in TIERS:
            if verbose:
                print("  [tier", tier_name, "] unknown, skipping")
            continue
        if verbose:
            print("  [tier", tier_name, "] trying...")
        data = TIERS[tier_name](pdf_path, first_pages_text, doi)

        contributed = 0
        if data:
            for field in ("title", "year", "journal", "volume", "issue", "pages", "abstract"):
                if not result.get(field) and data.get(field):
                    result[field] = data[field]
                    contributed += 1
            if not result["authors"] and data.get("authors"):
                result["authors"] = data["authors"]
                contributed += 1
            if not result["keywords"] and data.get("keywords"):
                result["keywords"] = data["keywords"]
                contributed += 1
            if not result["doi"] and data.get("doi"):
                result["doi"] = data["doi"]
                contributed += 1
            if data.get("needs_review"):
                result["needs_review"] = True

        result["tiers_log"].append((tier_name, contributed, bool(data)))
        if verbose:
            print("  [tier", tier_name, "] contributed", contributed, "field(s)")

        if result["title"] and result["authors"] and result["year"]:
            if verbose:
                print("  [chain] essentials filled by tier", repr(tier_name), "stopping")
            break

    return result


def recover_metadata_deterministic(
    pdf_path,
    first_pages_text: str = "",
    *,
    doi: str | None = None,
    pages: int = 5,
    verbose: bool = False,
) -> dict[str, Any]:
    """Deterministic metadata recovery for review resolver (no Claude/Ollama/review tier)."""
    from pdf_content_guard import extract_text_preview, is_actual_pdf, recover_metadata_for_path

    pdf_path = Path(pdf_path)
    if not first_pages_text:
        first_pages_text, _, _ = extract_text_preview(pdf_path, pages=pages)

    if not is_actual_pdf(pdf_path):
        return recover_metadata_for_path(pdf_path, pages=pages, doi=doi, verbose=verbose)

    if not doi and first_pages_text:
        from add_paper import doi_from_pdf_text

        doi = doi_from_pdf_text(first_pages_text)

    tier_order = [
        "crossref",
        "pdf_embed",
        "filename_human",
        "first_page_heuristic",
        "filename",
    ]
    merged = extract_metadata(
        str(pdf_path),
        first_pages_text,
        doi=doi,
        tier_order=tier_order,
        verbose=verbose,
    )
    merged.pop("needs_review", None)
    if merged.get("title") and not merged.get("authors"):
        fh = extract_filename_human(pdf_path, first_pages_text, doi)
        if fh.get("authors"):
            merged["authors"] = fh["authors"]
    return merged


# ---------------------------------------------------------------------------
# CLI for manual testing
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import subprocess
    p = argparse.ArgumentParser(description="Test metadata fallback chain on a PDF")
    p.add_argument("pdf", help="Path to a PDF")
    p.add_argument("--doi", default=None, help="Known DOI (optional)")
    p.add_argument("--tiers", default=None, help="Comma-separated tier names (defaults to config)")
    args = p.parse_args()

    res = subprocess.run(["pdftotext", "-l", "2", args.pdf, "-"],
                         capture_output=True, encoding="utf-8", errors="replace",
                         timeout=30)
    first_pages = res.stdout or ""

    tiers = args.tiers.split(",") if args.tiers else None
    result = extract_metadata(args.pdf, first_pages, args.doi, tier_order=tiers)

    print()
    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    for k in ("title", "authors", "year", "journal", "doi", "needs_review"):
        print(f"{k:12s} {result[k]!r}")
    print(f"tiers_log    {result['tiers_log']}")
