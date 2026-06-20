"""Snapshot DB/storage metrics for batch ingest reporting."""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import config
import papers_db
from eval_completion import is_ingest_evaluation_complete, is_non_ratable_rating, is_research_rating
from evaluate_paper import normalize_rating_label
from source_pdf_ownership import _missing_db_row_ids, build_audit


def snapshot() -> dict:
    audit = build_audit()
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        qc = conn.execute("PRAGMA quick_check").fetchone()[0]
        ft_count = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE full_text IS NOT NULL AND length(full_text) > 100"
        ).fetchone()[0]
    finally:
        conn.close()

    research_complete = non_rat_complete = 0
    for p in papers_db.get_all_metadata():
        cl = p.get("classification") or {}
        r = normalize_rating_label(cl.get("rating"))
        ok, _ = is_ingest_evaluation_complete(p)
        if is_research_rating(r) and ok:
            research_complete += 1
        if is_non_ratable_rating(r) and ok:
            non_rat_complete += 1

    return {
        "active_rows": papers_db.get_paper_count(),
        "source_pdfs": len(list(config.SOURCE_PDFS.glob("*.pdf"))),
        "staging_pdfs": len(list(config.STAGING.glob("*.pdf"))),
        "review_pdfs": len(list(config.REVIEW_DIR.rglob("*.pdf"))),
        "full_text_count": ft_count,
        "research_eval_complete": research_complete,
        "non_ratable_complete": non_rat_complete,
        "unowned_source_pdfs": audit.get("unowned_total"),
        "ambiguous_source_ownership": audit.get("ambiguous_total"),
        "weird_orphan_active_ids": len(_missing_db_row_ids(audit)),
        "pragma_quick_check": qc,
    }


if __name__ == "__main__":
    print(json.dumps(snapshot(), indent=2))
