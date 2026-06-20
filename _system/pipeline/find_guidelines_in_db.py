"""Find guideline papers in the DB by searching titles/abstracts."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
import papers_db  # noqa: E402

PATTERNS = {
    "STROBE": re.compile(r"STROBE|Strengthening the Reporting of Observational|von Elm", re.I),
    "CONSORT": re.compile(r"CONSORT|Consolidated Standards.*Reporting.*Trial|Schulz.*2010.*trial", re.I),
    "PRISMA": re.compile(r"PRISMA|Preferred Reporting Items.*Systematic|Page.*2021.*systematic.*review", re.I),
    "APA Manual": re.compile(r"Publication Manual.*American Psychological", re.I),
    "EQUATOR": re.compile(r"EQUATOR Network|reporting guideline.*registry", re.I),
}

all_meta = papers_db.get_all_metadata()
for label, rx in PATTERNS.items():
    hits = []
    for p in all_meta:
        bm = p.get("basic_metadata") or {}
        title = bm.get("title") or ""
        abstract = ""
        ak = p.get("abstract_keywords")
        if isinstance(ak, dict):
            abstract = ak.get("abstract") or ""
        if rx.search(title) or rx.search(abstract):
            hits.append((p.get("id"), title[:100]))
    print(f"\n{label}:  {len(hits)} hits")
    for pid, title in hits[:10]:
        print(f"  {pid}: {title}")
