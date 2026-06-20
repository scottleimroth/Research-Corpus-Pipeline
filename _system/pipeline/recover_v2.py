"""Final recovery pass: tighter matching, p4p search, nulled pointers for true losses.

What this does differently from recover_multi_owned.py:
  1. Tighter T3: require title length >= 30 chars AND substring-only match (no fuzzy).
     Reason: short titles match citations in other papers' references (false positives).
  2. New tier P4P: search papers-for-processing/ for missing PDFs by id-transform
     and title-in-first-page. If found, copy to source-pdfs/ and link.
  3. For rows that genuinely cannot be matched anywhere:
     - Null out file_info.renamed_filename (don't keep the wrong pointer)
     - Add tag 'pdf-missing'
     - Preserve all other metadata (title, authors, abstract, ratings, etc.)
  4. Merge true dupe DB rows (same title, same author surnames, same year, same abstract).

Cache is reused from .source-pdfs-text-cache.json. New cache .p4p-text-cache.json built once.

Usage:
  python pipeline/recover_v2.py             # diagnose
  python pipeline/recover_v2.py --execute   # apply all changes (with backup)
"""
import argparse
import hashlib
import json
import re
import shutil
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

SOURCE_CACHE = ROOT / ".source-pdfs-text-cache.json"
P4P_CACHE = ROOT / ".p4p-text-cache.json"
P4P = ROOT / "papers-for-processing"

DOI_RX = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b", re.IGNORECASE)
MIN_TITLE_FOR_SUBSTRING = 30


def norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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


def elsevier_pii_from_id(pid):
    """Extract the Elsevier PII (S-prefix code) from an id like 1_s2_0_S1388245710003585_main."""
    m = re.search(r"_(S\d{16}|0\d{15}|\d{16})_", pid)
    if m:
        return m.group(1)
    return None


def build_text_cache(folder, cache_file, force=False):
    if cache_file.exists() and not force:
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    from pdf_content_guard import extract_pypdf_text_safely, read_pdf_metadata_safely

    print(f"  Building text cache for {folder.name}/ ...")
    pdfs = sorted(folder.glob("*.pdf"))
    out = {}
    for i, pdf in enumerate(pdfs):
        if i and i % 300 == 0:
            print(f"    indexed {i}/{len(pdfs)}")
        entry = {"title": None, "first_page": "", "doi": None}
        meta = read_pdf_metadata_safely(pdf, tier="recover_v2_metadata")
        if meta.get("title"):
            entry["title"] = str(meta["title"])[:300]
        t, _ = extract_pypdf_text_safely(pdf, pages=1, tier="recover_v2_first_page")
        entry["first_page"] = t[:3000]
        m = DOI_RX.search(t or "")
        if m:
            entry["doi"] = m.group(0).rstrip(".,;)")
        out[pdf.name] = entry
    cache_file.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def match_against_index(paper, fulltext, pdf_index, placeholder_fn, source_filenames_lower):
    """Try tiers in confidence order. Return (filename, tier, score) or (None, None, 0)."""
    pid = paper.get("id") or ""
    bm = paper.get("basic_metadata") or {}
    ids_field = paper.get("identifiers") or {}
    db_title = (bm.get("title") or "").strip()
    db_title_norm = norm(db_title)
    db_doi = (ids_field.get("doi") if isinstance(ids_field, dict) else None) or ""
    db_doi_lower = db_doi.lower()

    # T1: id-transform direct filename match
    for guess in id_to_filename_guesses(pid):
        if guess.lower() in source_filenames_lower:
            real = source_filenames_lower[guess.lower()]
            if real != placeholder_fn:
                return real, "T1:id-transform", 1.0

    # T1b: Elsevier PII substring in any PDF filename
    pii = elsevier_pii_from_id(pid)
    if pii:
        pii_lower = pii.lower()
        for lo, orig in source_filenames_lower.items():
            if pii_lower in lo and orig != placeholder_fn:
                return orig, "T1b:pii-in-filename", 1.0

    # T2: DB title vs PDF /Title metadata (>=0.85 fuzzy)
    if db_title_norm and len(db_title_norm) > 15:
        best = (None, 0.0)
        for fn, entry in pdf_index.items():
            if fn == placeholder_fn:
                continue
            pdf_title = norm(entry.get("title") or "")
            if not pdf_title or len(pdf_title) < 10:
                continue
            r = SequenceMatcher(None, db_title_norm, pdf_title).ratio()
            if r > best[1]:
                best = (fn, r)
        if best[1] >= 0.85:
            return best[0], f"T2:title-meta-{int(best[1]*100)}", best[1]

    # T5: DOI in PDF first-page
    if db_doi:
        for fn, entry in pdf_index.items():
            if fn == placeholder_fn:
                continue
            if db_doi_lower in (entry.get("first_page") or "").lower():
                return fn, "T5:doi-in-first-page", 1.0

    # T3: title substring in TOP of first-page (TIGHTENED - title in top 800 chars only,
    # to avoid matching the title in a references section of another paper)
    if db_title_norm and len(db_title_norm) >= MIN_TITLE_FOR_SUBSTRING:
        for fn, entry in pdf_index.items():
            if fn == placeholder_fn:
                continue
            fp_norm = norm(entry.get("first_page") or "")[:800]
            if db_title_norm in fp_norm:
                return fn, "T3:title-in-top-800", 1.0

    # T6: author surnames + year in first-page
    authors = bm.get("authors") or []
    year = str(bm.get("year") or "")
    if authors and year and len(year) == 4:
        surnames = []
        if isinstance(authors, str):
            authors_list = [a.strip() for a in re.split(r"[;,]", authors)]
        else:
            authors_list = authors
        for a in authors_list[:3]:
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
                if year in fp_norm and all(s in fp_norm for s in surnames):
                    return fn, "T6:authors+year", 0.95

    # T4: fulltext fuzzy
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
        if best[1] >= 0.55:
            return best[0], f"T4:fulltext-fuzzy-{int(best[1]*100)}", best[1]

    return None, None, 0.0


def get_full_text(pid, conn):
    row = conn.execute("SELECT full_text FROM papers WHERE id = ?", (pid,)).fetchone()
    if not row or not row[0]:
        return ""
    ft = row[0]
    if ft and (ft.startswith("{") or ft.startswith("[")):
        try:
            data = json.loads(ft)
            if isinstance(data, dict):
                return data.get("text") or data.get("content") or ""
            if isinstance(data, list):
                return " ".join(str(x) for x in data)[:5000]
        except Exception:
            pass
    return ft


def gather_broken(all_meta):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print(f"Recovery v2  (mode: {'EXECUTE' if args.execute else 'DRY RUN'})")
    print("=" * 70)

    all_meta = papers_db.get_all_metadata()
    broken = gather_broken(all_meta)
    print(f"Broken rows: {len(broken)}")

    # Load caches
    print("Loading source-pdfs cache...")
    source_index = build_text_cache(config.SOURCE_PDFS, SOURCE_CACHE)
    source_filenames_lower = {fn.lower(): fn for fn in source_index.keys()}
    print(f"  source-pdfs entries: {len(source_index)}")

    print("Loading papers-for-processing cache...")
    p4p_index = build_text_cache(P4P, P4P_CACHE)
    p4p_filenames_lower = {fn.lower(): fn for fn in p4p_index.keys()}
    print(f"  papers-for-processing entries: {len(p4p_index)}")

    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row

    # Plans
    source_matches = []  # (pid, placeholder, real_filename, tier)
    p4p_matches = []     # (pid, placeholder, p4p_filename, tier) - need copy to source-pdfs
    losses = []          # (pid, placeholder) - null out file_info

    # Pass 1: tiers 1-6 against source-pdfs + p4p
    placeholder_claimants_unmatched = defaultdict(list)  # placeholder -> [(paper, fulltext)]
    for paper, placeholder in broken:
        pid = paper.get("id")
        fulltext = get_full_text(pid, conn)

        match = match_against_index(paper, fulltext, source_index, placeholder, source_filenames_lower)
        if match[0]:
            source_matches.append((pid, placeholder, match[0], match[1]))
            continue
        match = match_against_index(paper, fulltext, p4p_index, None, p4p_filenames_lower)
        if match[0]:
            p4p_matches.append((pid, placeholder, match[0], match[1]))
            continue
        placeholder_claimants_unmatched[placeholder].append((paper, fulltext))

    # Pass 2 (T7): for each placeholder, the placeholder file itself is a real PDF.
    # Pick its rightful owner from the remaining unmatched claimants by scoring each
    # claimant's title/authors/year against the placeholder's first-page text.
    placeholder_winners = {}  # placeholder_fn -> winning paper_id
    for placeholder, claimants in placeholder_claimants_unmatched.items():
        entry = source_index.get(placeholder)
        if not entry:
            continue
        ph_fp = norm(entry.get("first_page") or "")[:1500]
        ph_title = norm(entry.get("title") or "")
        if not ph_fp and not ph_title:
            continue
        best = (None, 0.0, None)
        for paper, _ in claimants:
            bm = paper.get("basic_metadata") or {}
            db_title = norm(bm.get("title") or "")
            score = 0.0
            # Title in first-page top
            if db_title and len(db_title) >= 20 and db_title in ph_fp[:800]:
                score = max(score, 1.0)
            # Title vs PDF /Title metadata
            if db_title and ph_title and len(db_title) >= 15:
                r = SequenceMatcher(None, db_title, ph_title).ratio()
                score = max(score, r)
            # Authors+year
            authors = bm.get("authors") or []
            year = str(bm.get("year") or "")
            if authors and year and len(year) == 4:
                surnames = []
                authors_list = [a.strip() for a in re.split(r"[;,]", authors)] if isinstance(authors, str) else authors
                for a in authors_list[:3]:
                    if isinstance(a, str) and a.split():
                        surnames.append(norm(a.split()[-1]))
                surnames = [s for s in surnames if len(s) >= 4]
                if surnames and year in ph_fp and all(s in ph_fp for s in surnames):
                    score = max(score, 0.93)
            if score > best[1]:
                best = (paper.get("id"), score, paper)
        if best[0] and best[1] >= 0.75:
            placeholder_winners[placeholder] = best[0]
            source_matches.append((best[0], placeholder, placeholder, "T7:placeholder-owner"))

    # Pass 3: remaining unmatched are losses
    for placeholder, claimants in placeholder_claimants_unmatched.items():
        winner = placeholder_winners.get(placeholder)
        for paper, _ in claimants:
            if paper.get("id") != winner:
                losses.append((paper.get("id"), placeholder))

    print()
    print(f"Source-pdfs matches:           {len(source_matches)}")
    print(f"Papers-for-processing matches: {len(p4p_matches)}")
    print(f"True losses (no PDF found):    {len(losses)}")
    print()

    # Show tier breakdowns
    src_tiers = defaultdict(int)
    for _, _, _, t in source_matches:
        src_tiers[t.split(":")[0]] += 1
    print("Source-pdfs tier breakdown:")
    for k in sorted(src_tiers):
        print(f"  {k}: {src_tiers[k]}")
    p4p_tiers = defaultdict(int)
    for _, _, _, t in p4p_matches:
        p4p_tiers[t.split(":")[0]] += 1
    if p4p_tiers:
        print("P4P tier breakdown:")
        for k in sorted(p4p_tiers):
            print(f"  {k}: {p4p_tiers[k]}")
    print()

    if p4p_matches:
        print("Sample P4P recoveries:")
        for pid, ph, p4p_fn, t in p4p_matches[:15]:
            print(f"  {pid}  <-  {p4p_fn}  [{t}]")
        print()

    if losses:
        print(f"Losses (broken file_info will be nulled, metadata preserved):")
        by_id = {p.get("id"): p for p in all_meta}
        for pid, ph in losses[:25]:
            bm = by_id.get(pid, {}).get("basic_metadata") or {}
            t = (bm.get("title") or "(no title)")[:80]
            print(f"  {pid}: {t}")
        if len(losses) > 25:
            print(f"  ...and {len(losses) - 25} more")
        print()

    if not args.execute:
        conn.close()
        print("DRY RUN. Pass --execute to apply.")
        return

    # Execute
    from papers_db import _serialize_field

    # 1. Copy P4P matches into source-pdfs
    for pid, ph, p4p_fn, t in p4p_matches:
        src = P4P / p4p_fn
        dst = config.SOURCE_PDFS / p4p_fn
        if not dst.exists() and src.exists():
            shutil.copy2(src, dst)

    # 2. Update file_info pointers
    by_id = {p.get("id"): p for p in all_meta}
    updates = 0
    nulled = 0
    try:
        for pid, ph, real_fn, tier in source_matches:
            paper = by_id[pid]
            fi = paper.get("file_info") or {}
            if not isinstance(fi, dict):
                fi = {}
            fi["renamed_filename"] = real_fn
            fi["recovered_via"] = tier
            fi["recovered_at"] = datetime.now().isoformat()
            conn.execute("UPDATE papers SET file_info = ? WHERE id = ?",
                         (_serialize_field(fi), pid))
            updates += 1
        for pid, ph, p4p_fn, tier in p4p_matches:
            paper = by_id[pid]
            fi = paper.get("file_info") or {}
            if not isinstance(fi, dict):
                fi = {}
            fi["renamed_filename"] = p4p_fn
            fi["recovered_via"] = tier + " (from p4p)"
            fi["recovered_at"] = datetime.now().isoformat()
            conn.execute("UPDATE papers SET file_info = ? WHERE id = ?",
                         (_serialize_field(fi), pid))
            updates += 1
        for pid, ph in losses:
            paper = by_id[pid]
            fi = paper.get("file_info") or {}
            if isinstance(fi, dict):
                fi["renamed_filename"] = None
                fi["pdf_missing"] = True
                fi["was_pointing_at"] = ph
                fi["nulled_at"] = datetime.now().isoformat()
            # add 'pdf-missing' tag
            tags = paper.get("tags") or []
            if isinstance(tags, list) and "pdf-missing" not in tags:
                tags.append("pdf-missing")
            elif isinstance(tags, str):
                tags = [tags, "pdf-missing"]
            conn.execute("UPDATE papers SET file_info = ?, tags = ? WHERE id = ?",
                         (_serialize_field(fi), _serialize_field(tags), pid))
            nulled += 1
        conn.commit()
    finally:
        conn.close()

    print()
    print(f"Updates: {updates}")
    print(f"Nulled (pdf-missing tag added): {nulled}")
    print("Done.")


if __name__ == "__main__":
    main()
