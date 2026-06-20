#!/usr/bin/env python3
"""
papers_db.py - Unified read/write interface for the papers SQLite database.

All scripts should use this module instead of directly accessing the database.
Replaces the old JSON-based papers_db.py (backed up as papers_db.py.bak).

Migration date: 1 March 2026
"""
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# --- Portable path bootstrap (Phase 2 repath) ---
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (  # noqa: E402
    MASTER_PROJECT as BASE,
    DB_PATH, BACKUP_DIR, SOURCE_PDFS, CSV_PATH,
)
from path_governance import reserve_unique_path, sanitize_filename_component  # noqa: E402

SCHEMA_VERSION = 1

# Fields stored as JSON strings in SQLite
JSON_FIELDS = [
    "basic_metadata", "identifiers", "abstract_keywords",
    "file_info", "classification", "tags", "full_text",
    "sample_characteristics", "methodology", "statistical_analysis",
    "quality_indicators", "measurement_quality", "stimulus_and_procedures",
    "analysis_pipeline", "reporting_standards", "meta_analysis_specific",
    "supplements", "notes_and_flags",
    "ethics_and_consent", "replication_and_preregistration"
]

# All column names in the papers table
ALL_COLUMNS = [
    "id", "timestamp_added", "document_type",
    "basic_metadata", "identifiers", "abstract_keywords",
    "ethics_and_consent", "sample_characteristics",
    "replication_and_preregistration", "methodology",
    "statistical_analysis", "quality_indicators",
    "measurement_quality", "stimulus_and_procedures",
    "analysis_pipeline", "reporting_standards",
    "meta_analysis_specific", "supplements", "notes_and_flags",
    "classification", "rating", "red_team_survival",
    "tags", "file_info", "full_text"
]

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY,
    timestamp_added TEXT,
    document_type TEXT,
    basic_metadata TEXT,
    identifiers TEXT,
    abstract_keywords TEXT,
    ethics_and_consent TEXT,
    sample_characteristics TEXT,
    replication_and_preregistration TEXT,
    methodology TEXT,
    statistical_analysis TEXT,
    quality_indicators TEXT,
    measurement_quality TEXT,
    stimulus_and_procedures TEXT,
    analysis_pipeline TEXT,
    reporting_standards TEXT,
    meta_analysis_specific TEXT,
    supplements TEXT,
    notes_and_flags TEXT,
    classification TEXT,
    rating TEXT,
    red_team_survival TEXT,
    tags TEXT,
    file_info TEXT,
    full_text TEXT
);
"""

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_rating ON papers(rating);",
    "CREATE INDEX IF NOT EXISTS idx_document_type ON papers(document_type);",
    "CREATE INDEX IF NOT EXISTS idx_timestamp ON papers(timestamp_added);",
]


def get_connection():
    """Get a new SQLite connection with WAL mode and Row factory."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the database and tables if they don't exist."""
    conn = get_connection()
    try:
        conn.execute(CREATE_TABLE_SQL)
        for idx_sql in CREATE_INDEXES_SQL:
            conn.execute(idx_sql)
        conn.commit()
    finally:
        conn.close()


def _serialize_field(value):
    """Serialize a field value to a string for storage."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _deserialize_field(value, field_name):
    """Deserialize a stored string back to its Python type."""
    if value is None:
        return None
    if field_name in JSON_FIELDS:
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def _row_to_dict(row):
    """Convert a sqlite3.Row to a paper dict matching the original JSON schema."""
    if row is None:
        return None
    d = {}
    for col in ALL_COLUMNS:
        val = row[col]
        if col in ("rating", "red_team_survival"):
            # These are extracted index columns, not part of the paper dict
            continue
        d[col] = _deserialize_field(val, col)
    return d


def _paper_to_row(paper):
    """Convert a paper dict to a tuple of values for INSERT."""
    # Extract rating and red_team_survival from classification for indexing
    cl = paper.get("classification", {})
    if isinstance(cl, dict):
        rating = cl.get("rating")
        rts = cl.get("red_team_survival")
        if rts is not None:
            rts = str(rts)
    else:
        rating = None
        rts = None

    values = []
    for col in ALL_COLUMNS:
        if col == "rating":
            values.append(rating)
        elif col == "red_team_survival":
            values.append(rts)
        elif col in JSON_FIELDS:
            values.append(_serialize_field(paper.get(col)))
        else:
            values.append(paper.get(col))
    return tuple(values)


def get_paper(paper_id):
    """Get a single paper by ID. Returns dict or None."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def get_paper_by_doi(doi):
    """Get a paper by DOI. Searches basic_metadata JSON field."""
    if not doi:
        return None
    doi_lower = doi.lower().strip()
    conn = get_connection()
    try:
        # Use LIKE for fast pre-filter, then verify exact match
        rows = conn.execute(
            "SELECT * FROM papers WHERE basic_metadata LIKE ?",
            (f"%{doi_lower}%",)
        ).fetchall()
        for row in rows:
            paper = _row_to_dict(row)
            bm = paper.get("basic_metadata", {})
            if isinstance(bm, dict):
                bm_doi = (bm.get("doi") or "").lower().strip()
                if bm_doi == doi_lower:
                    return paper
        return None
    finally:
        conn.close()


def save_paper(paper, *, allow_replace: bool = True):
    """Insert a paper row (or replace when explicitly allowed)."""
    valid, errors = validate_paper(paper)
    if not valid:
        raise ValueError(f"Paper validation failed: {'; '.join(errors)}")

    values = _paper_to_row(paper)
    placeholders = ",".join(["?"] * len(ALL_COLUMNS))
    cols = ",".join(ALL_COLUMNS)
    insert_verb = "INSERT OR REPLACE" if allow_replace else "INSERT"
    sql = f"{insert_verb} INTO papers ({cols}) VALUES ({placeholders})"

    conn = get_connection()
    try:
        conn.execute(sql, values)
        conn.commit()
        return True
    finally:
        conn.close()


def get_unevaluated(limit=None):
    """Get papers needing evaluation (no rating or NO_RATING, journal_article only).
    Returns list of dicts WITHOUT full_text for speed."""
    conn = get_connection()
    try:
        sql = """
            SELECT id, timestamp_added, document_type, basic_metadata,
                   classification, tags, file_info, rating
            FROM papers
            WHERE (rating IS NULL OR rating = '' OR rating = 'NO_RATING')
            AND (document_type = 'journal_article' OR document_type IS NULL)
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql).fetchall()
        result = []
        for row in rows:
            d = {
                "id": row["id"],
                "timestamp_added": row["timestamp_added"],
                "document_type": row["document_type"],
                "basic_metadata": _deserialize_field(row["basic_metadata"], "basic_metadata"),
                "classification": _deserialize_field(row["classification"], "classification"),
                "tags": _deserialize_field(row["tags"], "tags"),
                "file_info": _deserialize_field(row["file_info"], "file_info"),
                "rating": row["rating"],
            }
            result.append(d)
        return result
    finally:
        conn.close()


def get_all_metadata():
    """Get all papers WITHOUT full_text (fast). Returns list of dicts."""
    conn = get_connection()
    try:
        cols = [c for c in ALL_COLUMNS if c not in ("full_text", "rating", "red_team_survival")]
        sql = f"SELECT {','.join(cols)} FROM papers"
        rows = conn.execute(sql).fetchall()
        result = []
        for row in rows:
            d = {}
            for col in cols:
                d[col] = _deserialize_field(row[col], col)
            result.append(d)
        return result
    finally:
        conn.close()


def get_paper_count():
    """Get total number of papers."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) FROM papers").fetchone()
        return row[0]
    finally:
        conn.close()


def get_rating_distribution():
    """Get counts by rating. Returns dict like {'strong': 968, ...}."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT COALESCE(rating, 'NO_RATING') as r, COUNT(*) as c "
            "FROM papers GROUP BY r ORDER BY c DESC"
        ).fetchall()
        return {row["r"]: row["c"] for row in rows}
    finally:
        conn.close()


def paper_exists(paper_id):
    """Check if a paper ID exists in the database."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT 1 FROM papers WHERE id = ?", (paper_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def doi_exists(doi):
    """Check if a DOI exists in the database. Searches basic_metadata JSON."""
    if not doi:
        return False
    doi_lower = doi.lower().strip()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT basic_metadata FROM papers WHERE basic_metadata LIKE ?",
            (f"%{doi_lower}%",)
        ).fetchall()
        for row in rows:
            bm = _deserialize_field(row["basic_metadata"], "basic_metadata")
            if isinstance(bm, dict):
                bm_doi = (bm.get("doi") or "").lower().strip()
                if bm_doi == doi_lower:
                    return True
        return False
    finally:
        conn.close()


def backup(suffix=None):
    """Create a timestamped backup of papers.db. Keeps last 3 .db backups."""
    if not DB_PATH.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"papers-{ts}"
    if suffix:
        name += f"-{sanitize_filename_component(suffix, max_len=48)}"
    name += ".db"
    dest = reserve_unique_path(BACKUP_DIR, name)
    shutil.copy2(DB_PATH, dest)
    _rotate_backups()
    return dest


def _rotate_backups():
    """Keep last 3 .db backups, delete older. Delete all legacy .json backups."""
    if not BACKUP_DIR.exists():
        return
    # Delete legacy .json backups (pre-SQLite system)
    for f in sorted(BACKUP_DIR.glob("*.json")):
        f.unlink()
    # Keep last 3 .db backups, delete older
    db_backups = sorted(BACKUP_DIR.glob("papers-*.db"), key=lambda p: p.stat().st_mtime)
    if len(db_backups) > 3:
        for f in db_backups[:-3]:
            f.unlink()


def validate_paper(paper):
    """Validate a paper dict. Returns (bool, list[str])."""
    errors = []
    if not paper.get("id"):
        errors.append("Missing required field: id")
    bm = paper.get("basic_metadata", {})
    if not isinstance(bm, dict):
        errors.append("basic_metadata must be a dict")
    elif not bm.get("title"):
        errors.append("Missing required field: basic_metadata.title")
    return (len(errors) == 0, errors)


def dump_all_to_json(output_path):
    """Export the full database to JSON matching the original schema."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM papers").fetchall()
        papers = []
        for row in rows:
            paper = _row_to_dict(row)
            paper.pop("full_text_file", None)
            paper.pop("full_text_chars", None)
            papers.append(paper)
        data = {
            "metadata": {
                "exported_from": "papers.db",
                "export_date": datetime.now().isoformat(),
                "total_papers": len(papers)
            },
            "papers": papers
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return len(papers)
    finally:
        conn.close()


# === Legacy compatibility functions ===
# Match the old papers_db.py API so scripts work during transition.

def load_meta():
    """Legacy compat: returns {'papers': [...]} with all papers (no full_text)."""
    papers = get_all_metadata()
    return {"papers": papers}


def save_meta(data):
    """Legacy compat: save all papers from a {'papers': [...]} dict."""
    conn = get_connection()
    try:
        for paper in data.get("papers", []):
            values = _paper_to_row(paper)
            placeholders = ",".join(["?"] * len(ALL_COLUMNS))
            cols = ",".join(ALL_COLUMNS)
            sql = f"INSERT OR REPLACE INTO papers ({cols}) VALUES ({placeholders})"
            conn.execute(sql, values)
        conn.commit()
    finally:
        conn.close()


def load_fulltext(paper_id):
    """Legacy compat: load full_text for a paper."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT full_text FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if row and row["full_text"]:
            return _deserialize_field(row["full_text"], "full_text")
        return {}
    finally:
        conn.close()


def save_fulltext(paper_id, full_text):
    """Legacy compat: save full_text for a paper."""
    ft_json = _serialize_field(full_text)
    conn = get_connection()
    try:
        conn.execute("UPDATE papers SET full_text = ? WHERE id = ?", (ft_json, paper_id))
        conn.commit()
    finally:
        conn.close()


# Expose paths for backwards compatibility
META_PATH = DB_PATH
FULLTEXT_DIR = BASE / "papers-fulltext"
LEGACY_PATH = BASE / "papers-master.json"
