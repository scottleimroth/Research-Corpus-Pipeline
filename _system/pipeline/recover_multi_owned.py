"""Recover broken DB rows by matching their stored full_text against actual PDFs.

Use the available full-text evidence already stored in the database.

Strategy:
  1. Find broken rows: file_info.renamed_filename is multi-owned (shared with >=1 other row).
  2. For each broken row, gather identifying signal:
       - basic_metadata.title (if present)
       - identifiers.doi (if present)
       - full_text first 3000 chars
  3. Build a content index of source-pdfs/: first-page text + pypdf /Title metadata
     for every PDF. Cached to disk so subsequent runs are fast.
  4. For each broken row, try matches in order:
       Tier 1: id-transform (Elsevier 1_s2_0_X -> 1-s2.0-X.pdf)
       Tier 2: DB title fuzzy vs PDF /Title metadata
       Tier 3: DB title fuzzy vs PDF first-page text
       Tier 4: DB full_text first 1000 chars vs PDF first-page text (substring + fuzzy)
       Tier 5: DB DOI lookup vs PDF first-page text (DOI string search)
  5. Best match -> update file_info.renamed_filename. Preserve ALL other row data.
  6. No match -> flag for manual review. NEVER delete on no-match.

Usage:
  python pipeline/recover_multi_owned.py             # diagnose, no DB changes
  python pipeline/recover_multi_owned.py --execute   # apply file_info updates
  python pipeline/recover_multi_owned.py --rebuild-index  # force rebuild of PDF text cache
"""
import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
import config  # noqa: E402
import papers_db  # noqa: E402

CACHE_FILE = ROOT / ".source-pdfs-text-cache.json"
DOI_RX = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b", re.IGNORECASE)


def norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def id_to_filename_guesses(pid):
    out = set()
    out.add(pid + ".pdf")
    out.add(pid.replace("_", "-") + ".pdf")
    if pid.startswith("1_s2_0_"):
        rest = pid[len("1_s2_0_"):]
        out.add(f"1-s2.0-{rest.replace('_', '-')}.pdf")
        out.add(f"1-s2.0-{rest.replace('_', '-')} (1).pdf")
        out.add(f"1-s2.0-{rest.replace('_', '-')} (2).pdf")
    out.add(pid.lower() + ".pdf")
    out.add(pid.lower().replace("_", "-") + ".pdf")
    if pid.endswith("_s") or pid.endswith("_1"):
        out.add(pid[:-2] + ".pdf")
        out.add(pid[:-2].replace("_", "-") + ".pdf")
    return out


def build_pdf_index(force=False):
    """Build (or load) a {filename -> {title, first_page_text, doi}} cache."""
    if CACHE_FILE.exists() and not force:
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            print(f"  Loaded text cache ({len(data)} PDFs) from {CACHE_FILE.name}")
            return data
        except Exception:
            pass

    from pdf_content_guard import extract_pypdf_text_safely, read_pdf_metadata_safely

    print(f"  Building PDF text index for source-pdfs/ (this takes a few minutes)...")
    pdfs = sorted(config.SOURCE_PDFS.glob("*.pdf"))
    out = {}
    for i, pdf in enumerate(pdfs):
        if i and i % 200 == 0:
            print(f"    indexed {i}/{len(pdfs)}")
        entry = {"title": None, "first_page": "", "doi": None}
        meta = read_pdf_metadata_safely(pdf, tier="recover_multi_owned_metadata")
        if meta.get("title"):
            entry["title"] = str(meta["title"])[:300]
        text, _ = extract_pypdf_text_safely(pdf, pages=1, tier="recover_multi_owned_first_page")
        entry["first_page"] = text[:3000]
        # DOI from first page
        m = DOI_RX.search(text or "")
        if m:
            entry["doi"] = m.group(0).rstrip(".,;)")
        out[pdf.name] = entry

    CACHE_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  Cache written to {CACHE_FILE.name}")
    return out


def gather_broken_rows(all_meta):
    """Return list of (paper_dict, placeholder_filename) for rows pointing at multi-owned files."""
    fname_owners = defaultdict(list)
    for p in all_meta:
        fi = p.get("file_info") or {}
        if isinstance(fi, dict):
            fn = fi.get("renamed_filename")
            if fn:
                fname_owners[fn].append(p.get("id"))
    placeholders = {fn for fn, ids in fname_owners.items() if len(ids) >= 2}
    broken = []
    by_id = {p.get("id"): p for p in all_meta}
    for fn in placeholders:
        for pid in fname_owners[fn]:
            broken.append((by_id[pid], fn))
    return broken


def get_full_text(pid, conn):
    row = conn.execute("SELECT full_text FROM papers WHERE id = ?", (pid,)).fetchone()
    if not row or not row[0]:
        return ""
    ft = row[0]
    # full_text column is sometimes JSON-encoded (papers_db serializes dicts)
    if ft.startswith("{") or ft.startswith("["):
        try:
            data = json.loads(ft)
            if isinstance(data, dict):
                return data.get("text") or data.get("content") or ""
            if isinstance(data, list):
                return " ".join(str(x) for x in data)[:5000]
        except Exception:
            pass
    return ft


def match_paper_to_pdf(paper, fulltext, pdf_index, placeholder_fn, source_filenames_lower):
    """Try all tiers. Return (pdf_filename, tier, score) or (None, None, 0)."""
    pid = paper.get("id") or ""
    bm = paper.get("basic_metadata") or {}
    ids_field = paper.get("identifiers") or {}
    db_title = norm(bm.get("title") or "")
    db_doi = (ids_field.get("doi") if isinstance(ids_field, dict) else None) or ""
    db_doi_lower = db_doi.lower()

    # Tier 1: id-transform
    for guess in id_to_filename_guesses(pid):
        if guess.lower() in source_filenames_lower:
            real = source_filenames_lower[guess.lower()]
            if real != placeholder_fn:
                return real, "T1:id-transform", 1.0

    # Tier 2: DB title vs PDF /Title metadata
    if db_title and len(db_title) > 15:
        best = (None, 0.0)
        for fn, entry in pdf_index.items():
            if fn == placeholder_fn:
                continue
            pdf_title = norm(entry.get("title") or "")
            if not pdf_title or len(pdf_title) < 10:
                continue
            r = SequenceMatcher(None, db_title, pdf_title).ratio()
            if r > best[1]:
                best = (fn, r)
        if best[1] >= 0.85:
            return best[0], f"T2:title-meta-{int(best[1]*100)}", best[1]

    # Tier 5: DOI -> PDF first-page text (run before T3/T4 because DOI is unambiguous)
    if db_doi:
        for fn, entry in pdf_index.items():
            if fn == placeholder_fn:
                continue
            fp = entry.get("first_page") or ""
            if db_doi_lower in fp.lower():
                return fn, "T5:doi-in-first-page", 1.0

    # Tier 3: DB title vs PDF first-page text (substring + fuzzy)
    if db_title and len(db_title) > 15:
        for fn, entry in pdf_index.items():
            if fn == placeholder_fn:
                continue
            fp_norm = norm(entry.get("first_page") or "")
            if db_title in fp_norm:
                return fn, "T3:title-in-first-page", 1.0
        best = (None, 0.0)
        for fn, entry in pdf_index.items():
            if fn == placeholder_fn:
                continue
            fp_norm = norm(entry.get("first_page") or "")[:300]
            if len(fp_norm) < 30:
                continue
            r = SequenceMatcher(None, db_title[:200], fp_norm).ratio()
            if r > best[1]:
                best = (fn, r)
        if best[1] >= 0.55:
            return best[0], f"T3:title-fuzzy-{int(best[1]*100)}", best[1]

    # Tier 4: DB full_text first 600 chars vs PDF first-page text
    if fulltext and len(fulltext) > 100:
        ft_norm = norm(fulltext[:1000])[:600]
        best = (None, 0.0)
        for fn, entry in pdf_index.items():
            if fn == placeholder_fn:
                continue
            fp_norm = norm(entry.get("first_page") or "")[:600]
            if len(fp_norm) < 50:
                continue
            r = SequenceMatcher(None, ft_norm, fp_norm).ratio()
            if r > best[1]:
                best = (fn, r)
        if best[1] >= 0.40:
            return best[0], f"T4:fulltext-fuzzy-{int(best[1]*100)}", best[1]

    # Tier 6: author surname + year overlap with PDF first-page
    bm_authors = (paper.get("basic_metadata") or {}).get("authors") or []
    bm_year = str((paper.get("basic_metadata") or {}).get("year") or "")
    if bm_authors and bm_year and len(bm_year) == 4:
        surnames = []
        for a in bm_authors[:3]:
            if isinstance(a, str):
                toks = a.split()
                if toks:
                    surnames.append(norm(toks[-1]))
        surnames = [s for s in surnames if len(s) >= 4]
        if surnames:
            for fn, entry in pdf_index.items():
                if fn == placeholder_fn:
                    continue
                fp_norm = norm(entry.get("first_page") or "")[:1500]
                if bm_year in fp_norm and all(s in fp_norm for s in surnames):
                    return fn, "T6:authors+year", 0.95

    return None, None, 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--rebuild-index", action="store_true",
                    help="force rebuild of source-pdfs text cache")
    args = ap.parse_args()

    print("=" * 70)
    print(f"Multi-owned row recovery (using full_text)")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print("=" * 70)

    all_meta = papers_db.get_all_metadata()
    broken = gather_broken_rows(all_meta)
    print(f"Broken rows (multi-owned filename pointer): {len(broken)}")

    pdf_index = build_pdf_index(force=args.rebuild_index)
    # filename lookups
    source_filenames_lower = {fn.lower(): fn for fn in pdf_index.keys()}

    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row

    # Cluster results
    tier_counts = defaultdict(int)
    plan = []      # (paper_id, placeholder_fn, real_fn, tier, score)
    unmatched = []  # (paper_id, placeholder_fn, has_rich_meta)

    for paper, placeholder in broken:
        pid = paper.get("id")
        fulltext = get_full_text(pid, conn)
        real, tier, score = match_paper_to_pdf(
            paper, fulltext, pdf_index, placeholder, source_filenames_lower)
        if real:
            plan.append((pid, placeholder, real, tier, score))
            tier_counts[tier.split(":")[0]] += 1
        else:
            bm = paper.get("basic_metadata") or {}
            has_rich = bool(bm.get("title") or bm.get("abstract") or paper.get("abstract_keywords"))
            unmatched.append((pid, placeholder, has_rich, len(fulltext)))

    print()
    print(f"Resolved: {len(plan)}")
    for k in sorted(tier_counts):
        print(f"  {k}: {tier_counts[k]}")
    print(f"Unmatched: {len(unmatched)}")
    print()

    # Sample resolved
    print("Sample matches (first 25):")
    for pid, ph, real, tier, score in plan[:25]:
        print(f"  {pid}")
        print(f"    placeholder: {ph}")
        print(f"    real PDF:    {real}  [{tier}]")
    print()

    # Dump unmatched cases with rich detail so Claude can review them via Read tool
    if unmatched:
        by_id = {p.get("id"): p for p in all_meta}
        dump = []
        for pid, ph, rich, ft_len in unmatched:
            paper = by_id.get(pid, {})
            bm = paper.get("basic_metadata") or {}
            ids_field = paper.get("identifiers") or {}
            ak = paper.get("abstract_keywords") or {}
            fulltext = get_full_text(pid, conn)
            dump.append({
                "paper_id": pid,
                "placeholder": ph,
                "basic_metadata_title": bm.get("title"),
                "basic_metadata_authors": bm.get("authors"),
                "basic_metadata_year": bm.get("year"),
                "doi": ids_field.get("doi") if isinstance(ids_field, dict) else None,
                "abstract": (ak.get("abstract") or "")[:1000] if isinstance(ak, dict) else "",
                "fulltext_head": fulltext[:2000],
                "tags": paper.get("tags"),
                "rating": (paper.get("classification") or {}).get("rating") if isinstance(paper.get("classification"), dict) else None,
            })
        dump_path = ROOT / f"unmatched-recovery-{datetime.now():%Y-%m-%d-%H%M%S}.json"
        dump_path.write_text(json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"UNMATCHED dumped to {dump_path.name}  ({len(unmatched)} rows)")
        print("(Claude will Read this file and make calls on each.)")
    print()

    if not args.execute:
        conn.close()
        print("DRY RUN. Pass --execute to UPDATE file_info.renamed_filename for resolved rows.")
        print("Unmatched rows are NOT deleted - they stay for manual review.")
        return

    # Execute: update file_info.renamed_filename for resolved rows
    print(f"Applying {len(plan)} file_info updates...")
    from papers_db import _serialize_field

    by_id = {p.get("id"): p for p in all_meta}
    try:
        for pid, _, real, tier, score in plan:
            paper = by_id[pid]
            fi = paper.get("file_info") or {}
            if not isinstance(fi, dict):
                fi = {}
            fi["renamed_filename"] = real
            fi["recovered_via"] = tier
            fi["recovered_at"] = datetime.now().isoformat()
            new_fi_json = _serialize_field(fi)
            conn.execute("UPDATE papers SET file_info = ? WHERE id = ?", (new_fi_json, pid))
        conn.commit()
        print("Transaction committed.")
    finally:
        conn.close()

    print()
    print(f"Done. {len(plan)} rows fixed. {len(unmatched)} rows still need manual review.")


if __name__ == "__main__":
    main()
