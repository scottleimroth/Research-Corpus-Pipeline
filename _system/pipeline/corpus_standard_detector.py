"""Detect existing DB evaluation standard from DB + logs (read-only)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import config
from evaluate_paper import MODEL


def _safe_count(conn: sqlite3.Connection, sql: str) -> int:
    try:
        return conn.execute(sql).fetchone()[0]
    except Exception:
        return 0


def detect_db_standard(*, db_path: str = "") -> dict[str, Any]:
    db = Path(db_path).resolve() if db_path else config.DB_PATH.resolve()
    if not db.exists():
        return {
            "db_exists": False,
            "summary_label": "No existing DB standard (blank corpus mode)",
        }

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        total = _safe_count(conn, "SELECT COUNT(*) FROM papers")
        rated = _safe_count(conn, "SELECT COUNT(*) FROM papers WHERE rating IS NOT NULL AND rating != ''")
        with_redteam = _safe_count(
            conn,
            "SELECT COUNT(*) FROM papers WHERE json_extract(classification, '$.red_team_notes') IS NOT NULL AND json_extract(classification, '$.red_team_notes') != ''",
        )
        with_v2 = _safe_count(
            conn,
            "SELECT COUNT(*) FROM papers WHERE json_extract(classification, '$.evidence_bullets') IS NOT NULL",
        )
    finally:
        conn.close()

    eval_log = config.STAGING_LOGS / "evaluation.log"
    strategy_counts: dict[str, int] = {}
    blind_false = 0
    if eval_log.exists():
        text = eval_log.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"INPUT \| .*? strategy=([^|]+) \|", text):
            s = m.group(1).strip()
            strategy_counts[s] = strategy_counts.get(s, 0) + 1
        blind_false = len(re.findall(r"blind_trunc=False", text))

    redteam_pct = (with_redteam / rated * 100) if rated else 0
    summary = (
        f"Model={MODEL}; rated={rated}/{total}; red-team on {redteam_pct:.1f}% of rated; "
        f"schema_v2_signals={with_v2}; input_strategies={strategy_counts or {'unknown': 0}}"
    )

    return {
        "db_exists": True,
        "model_inferred": MODEL,
        "total_rows": total,
        "rated_rows": rated,
        "redteam_rows": with_redteam,
        "redteam_percent": round(redteam_pct, 2),
        "schema_v2_rows": with_v2,
        "strategy_counts": strategy_counts,
        "blind_trunc_false_log_lines": blind_false,
        "summary_label": summary,
    }
