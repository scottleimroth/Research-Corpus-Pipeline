#!/usr/bin/env python3
"""One-shot: complete first-pass eval for rows left after staging ingest bug."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import papers_db
from first_pass_finalize import run_until_evaluation_complete


def _candidates(tag: str) -> list[str]:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT id FROM papers
            WHERE tags LIKE ?
              AND (
                json_extract(notes_and_flags, '$.first_pass_finalize_required') = 1
                OR json_extract(classification, '$.rating') IS NULL
                OR trim(json_extract(classification, '$.rating')) = ''
              )
            ORDER BY timestamp_added DESC
            """,
            (f"%{tag}%",),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _pdf_for_paper(paper_id: str) -> Path | None:
    paper = papers_db.get_paper(paper_id) or {}
    fi = paper.get("file_info") or {}
    rel = fi.get("filepath") or ""
    if not rel:
        return None
    path = Path(rel) if Path(rel).is_absolute() else config.CORPUS_STORE / rel.replace("\\", "/").lstrip("/")
    return path if path.is_file() else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="all-staging-ingest")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    papers_db.init_db()
    ids = _candidates(args.tag)
    if not ids:
        print("No candidates.")
        return 0

    print(f"Candidates ({len(ids)}):", ", ".join(ids[:20]), ("..." if len(ids) > 20 else ""))

    if args.dry_run:
        return 0

    import anthropic
    from pipeline_llm import paid_api_ready

    ok, msg = paid_api_ready()
    if not ok:
        print(f"ERROR: {msg}", file=sys.stderr)
        return 1
    client = anthropic.Anthropic()

    failed: list[dict[str, str]] = []
    for paper_id in ids:
        pdf = _pdf_for_paper(paper_id)
        if pdf is None:
            failed.append({"paper_id": paper_id, "reason": "missing_source_pdf"})
            continue
        print(f"\n=== First-pass backfill: {paper_id} ===", flush=True)
        fin = run_until_evaluation_complete(
            paper_id,
            pdf,
            client,
            non_ratable=False,
            force_research_eval=True,
            llm_forced=True,
            allow_paid_api=True,
            max_attempts=6,
        )
        paper = papers_db.get_paper(paper_id) or {}
        cl = paper.get("classification") or {}
        nf = paper.get("notes_and_flags") or {}
        if fin.get("ok"):
            nf.pop("first_pass_finalize_required", None)
            paper["notes_and_flags"] = nf
            papers_db.save_paper(paper)
            rt = "yes" if str(cl.get("red_team_notes") or "").strip() else "no"
            print(f"  OK rating={cl.get('rating')} red_team={rt}", flush=True)
        else:
            failed.append({"paper_id": paper_id, "reason": str(fin.get("reason") or "failed")})
            print(f"  FAIL: {fin.get('reason')}", flush=True)

    out = config.LOGS_DIR / "first_pass_backfill_result.json"
    out.write_text(json.dumps({"ok": len(failed) == 0, "failed": failed}, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
