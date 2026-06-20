"""Ingest scaffold documentation/helpers (no side effects in this phase)."""

from __future__ import annotations

INGEST_STAGES = [
    "Compute PDF hash and metadata fingerprints",
    "Extract text (pdftotext; OCR fallback) and section parse",
    "Extract metadata (DOI/title/authors/year/journal) using multi-tier extractors",
    "Detect duplicates by hash, DOI, title/year/authors, and optional full-text similarity",
    "Resolve preprint/final and supplement relationships with agent-assisted checks",
    "Generate safe canonical IDs (reject unsafe/synthetic IDs)",
    "Validate candidate row schema and JSON integrity",
    "Only then move/copy managed PDF and commit DB row (future live mode)",
]

MACHINE_FIRST_EXCEPTION_POLICIES = {
    "duplicate_suspected": [
        "hash/DOI/title checks",
        "full-text comparison",
        "version relationship logic",
        "human fallback only if unresolved",
    ],
    "metadata_conflict": [
        "PDF metadata + title page + DOI + external metadata cross-check",
        "agent evidence synthesis",
        "human fallback only if unresolved",
    ],
    "ocr_needed": [
        "automatic OCR",
        "re-extract and continue",
        "human fallback only if OCR still fails",
    ],
    "supplement_linking": [
        "document type detection",
        "parent-paper matching",
        "store + tag if link confidence high",
        "human fallback only when ambiguous",
    ],
}


def scaffold_summary() -> dict:
    return {
        "ingest_stages": INGEST_STAGES,
        "machine_first_exception_policies": MACHINE_FIRST_EXCEPTION_POLICIES,
        "note": "Scaffold only. No file/DB operations in this implementation step.",
    }
