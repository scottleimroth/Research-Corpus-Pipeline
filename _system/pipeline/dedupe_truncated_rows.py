"""Detect and merge duplicate DB rows created by Alex's broken pipeline.

Two patterns of duplication:
  A) Truncated-id pairs: same paper saved twice with different id truncation lengths.
     e.g. Boer_Relationships_between_short_term_blood_pressure_fluct_1
     and  Boer_Relationships_between_short_term_blood_pressure_fluctua
     (one was truncated to 60 chars, the other to 62; same paper).
  B) Same file_info.renamed_filename: post-recovery, two rows point at the same PDF.
     If their metadata also matches, they're dupes.

For each detected dupe group:
  - Choose the "canonical" row (most complete metadata: title length, author count,
    abstract length, tags variety, rating present, doi present)
  - Merge into canonical: union of tags, prefer non-null fields from canonical first
  - Delete the others

Dry-run by default. Pass --execute to apply.
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


def norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def id_prefix_score(a, b):
    """How long is the common prefix of a, b? Returns int."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def metadata_richness(paper):
    """Score how 'rich' a paper row's metadata is - higher means keep this one."""
    score = 0
    bm = paper.get("basic_metadata") or {}
    if bm.get("title"):
        score += len(bm["title"])
    authors = bm.get("authors") or []
    if isinstance(authors, list):
        score += 10 * len(authors)
    elif isinstance(authors, str):
        score += 10
    if bm.get("year"):
        score += 5
    ids = paper.get("identifiers") or {}
    if isinstance(ids, dict) and ids.get("doi"):
        score += 20
    ak = paper.get("abstract_keywords") or {}
    if isinstance(ak, dict) and ak.get("abstract"):
        score += len(ak["abstract"]) // 10
    tags = paper.get("tags") or []
    if isinstance(tags, list):
        score += len(tags)
    cl = paper.get("classification") or {}
    if isinstance(cl, dict) and cl.get("rating"):
        score += 50
    if isinstance(cl, dict) and cl.get("red_team_survival"):
        score += 50
    return score


# IDs to NEVER merge as DROP. Confirmed false positives or parent-supp pairs
# from manual audit + Sonnet subagent verification 2026-05-27.
# If any group contains one of these as a non-canonical member, the group is
# skipped entirely (we don't lose the real paper hidden behind the dupe match).
SKIP_DROP_IDS = {
    # False positives: different papers, dedupe matched on shared filename/keywords
    "Bharucha_Varieties_of_musical_experience_2006_Cognition",        # G1: Bharucha 2006 ≠ Lee 1918
    "FERGUSON_ACHIEVING_STATISTICS_SELF_ACTUALIZATION__FACULTY_SU",   # G2: Ferguson ≠ Bonett 2000
    "Barry_Fact_or_artifact__reply_to_Lobstein__Turpin_and_Siddle",   # G3: Barry 1979 paper, KEEP id is phantom PII
    "BARRY_1966_Heart",                                                # G4: real 1966 paper, not 1985
    "Franchini_Primer_on_the_Autonomic_Nervous_System__Second_Edi",   # G7: book vs chapter
    "Haas_Effects_of_perceived_musical_rhythm_on_respiratory_patt",   # G8: Haas 1986 ≠ Morgenstern 2009
    "Osgood_YYYY_Fourier",                                             # G9: phantom; Bracewell ≠ Osgood
    # Parent-supplement pairs (need supplements.items[] linking, not merge)
    "Teixeira_Borges_et_al___2019___Scaling_behaviour_in_music_an",   # G6: parent paper of Borges2024 supplement
    "Susan_Hallam___Psychology_of_Music_Routledge__2018_",            # G11: parent book of Hallam chapters
    # Uncertain year mismatch — defer until investigated
    "2004_can_the_q_link_ally_a_form_of_sympathetic_resonance_tec",   # G58: Croft 2002 vs 2004 unclear
}


TRUNC_TAIL_RX = re.compile(r"\s+(or|hyp|a|m|1|h\s*1|s\s*u|n[oa]|the)$|[^a-z0-9]$|_\d$", re.IGNORECASE)


def _get_title(p):
    bm = p.get("basic_metadata") or {}
    return bm.get("title") or ""


def _get_abstract(p):
    ak = p.get("abstract_keywords") or {}
    if isinstance(ak, dict):
        return ak.get("abstract") or ""
    return ""


def _get_doi(p):
    ids = p.get("identifiers") or {}
    if isinstance(ids, dict):
        return (ids.get("doi") or "").lower().strip()
    return ""


def _get_renamed(p):
    fi = p.get("file_info") or {}
    if isinstance(fi, dict):
        return fi.get("renamed_filename") or ""
    return ""


def _title_looks_truncated(t):
    """A 'clean' title is >= 40 chars, ends with a letter/digit/?/. and doesn't end in
    single-letter or word fragments like 'h 1', '_or', '_a'."""
    if not t or len(t) < 40:
        return True
    raw = t.strip()
    if not raw[-1].isalnum() and raw[-1] not in ".?!":
        return True
    if TRUNC_TAIL_RX.search(raw):
        return True
    return False


def _trunc_pair_pattern(id1, id2):
    """One id is the other with a 1-3 char tail diff at a truncation boundary."""
    n = id_prefix_score(id1, id2)
    if n < 50:
        return False
    tail1 = id1[n:]
    tail2 = id2[n:]
    if len(tail1) <= 4 and len(tail2) <= 4:
        return True
    return False


def likely_duplicates(p1, p2):
    """Return True iff two papers are confidently the same paper.

    Strategy: permissive positive signals, but with HARD REJECT rules that block
    the known false-positive patterns (Barry reviews part I vs II, Clarke
    methylphenidate single-study vs three-study series).

    Hard rejects (any one of these -> NOT dupe, stop):
      R1) Both titles look CLEAN (proper, not truncated) AND title fuzzy < 0.95
          -> different papers with different titles
      R2) Both abstracts >= 100 chars AND abstract fuzzy < 0.85
          -> abstracts clearly describe different studies

    Positive signals (any one -> dupe):
      P1) DOI exact match
      P2) Title-norm exact match OR title fuzzy >= 0.92
      P3) Abstract fuzzy >= 0.92 (both >= 200 chars)
      P4) Same non-empty renamed_filename AND title fuzzy >= 0.5
      P5) Trunc-pair ids (prefix>=50, tail<=4) AND title-norm matches OR fn matches
    """
    id1 = p1.get("id") or ""
    id2 = p2.get("id") or ""

    d1 = _get_doi(p1); d2 = _get_doi(p2)
    if d1 and d2 and d1 == d2:
        return True

    t1 = norm(_get_title(p1))
    t2 = norm(_get_title(p2))
    raw1 = _get_title(p1); raw2 = _get_title(p2)
    a1 = _get_abstract(p1); a2 = _get_abstract(p2)
    fn1 = _get_renamed(p1); fn2 = _get_renamed(p2)

    clean1 = not _title_looks_truncated(raw1)
    clean2 = not _title_looks_truncated(raw2)
    title_ratio = SequenceMatcher(None, t1, t2).ratio() if (t1 and t2) else 0.0

    # R1: both clean, titles don't strongly match -> NOT dupes
    if clean1 and clean2 and title_ratio < 0.95:
        return False

    # R2: abstracts clearly differ -> NOT dupes
    # Tighter threshold when BOTH titles are truncated (we can't trust title-fuzzy
    # for those - any two papers by same author with same opening words look identical).
    if (a1 and a2 and len(a1) >= 100 and len(a2) >= 100):
        abstract_ratio = SequenceMatcher(None, a1[:1000], a2[:1000]).ratio()
        if not clean1 and not clean2:
            # Both titles unreliable: abstract must be very similar
            if abstract_ratio < 0.92:
                return False
        else:
            if abstract_ratio < 0.85:
                return False
        if abstract_ratio >= 0.92 and len(a1) >= 200 and len(a2) >= 200:
            return True  # P3

    # P2: title strong
    if t1 and t2:
        if t1 == t2:
            return True
        if title_ratio >= 0.92:
            return True

    # P4: same renamed_filename + titles overlap meaningfully
    if fn1 and fn2 and fn1 == fn2:
        if title_ratio >= 0.5:
            return True
        # If titles disagree heavily on a shared file, the recovery may have
        # mismatched. Be conservative: only auto-merge if at least one title is
        # effectively empty (was synthesized from id).
        if (not t1 or t1 == norm(id1)) or (not t2 or t2 == norm(id2)):
            return True

    # P5: trunc-pair ids
    if _trunc_pair_pattern(id1, id2):
        if (t1 and t2 and t1 == t2) or (fn1 and fn2 and fn1 == fn2):
            return True

    return False


def merge_field(canonical_val, other_val):
    """Prefer canonical if non-empty, else other."""
    if canonical_val:
        if isinstance(canonical_val, list) and not canonical_val:
            return other_val
        if isinstance(canonical_val, dict) and not canonical_val:
            return other_val
        return canonical_val
    return other_val


def merge_tags(t1, t2):
    """Union of tag lists."""
    def to_list(t):
        if not t:
            return []
        if isinstance(t, list):
            return t
        if isinstance(t, str):
            try:
                return json.loads(t)
            except Exception:
                return [t]
        return []
    out = []
    seen = set()
    for x in to_list(t1) + to_list(t2):
        if isinstance(x, str) and x not in seen:
            seen.add(x)
            out.append(x)
        elif isinstance(x, list):
            for y in x:
                if isinstance(y, str) and y not in seen:
                    seen.add(y)
                    out.append(y)
    return out


def merge_rows(canonical, other):
    """Merge `other` into `canonical`. Returns merged dict."""
    out = dict(canonical)
    for key in ["basic_metadata", "identifiers", "abstract_keywords",
                "file_info", "classification", "sample_characteristics",
                "methodology", "supplements", "notes_and_flags"]:
        c_val = canonical.get(key)
        o_val = other.get(key)
        if isinstance(c_val, dict) and isinstance(o_val, dict):
            merged = dict(c_val)
            for k, v in o_val.items():
                if v and not merged.get(k):
                    merged[k] = v
            out[key] = merged
        else:
            out[key] = merge_field(c_val, o_val)
    out["tags"] = merge_tags(canonical.get("tags"), other.get("tags"))
    return out


def find_dupe_groups(all_meta):
    """Group ids that look like duplicates."""
    by_id = {p.get("id"): p for p in all_meta}

    # Group 1: by file_info.renamed_filename (post-recovery same-file rows)
    file_groups = defaultdict(list)
    for p in all_meta:
        fi = p.get("file_info") or {}
        if isinstance(fi, dict):
            fn = fi.get("renamed_filename")
            if fn:
                file_groups[fn].append(p.get("id"))
    same_file_groups = [ids for ids in file_groups.values() if len(ids) >= 2]

    # Group 2: truncated-id pairs (one id is a substring/prefix-extension of another)
    # Build by checking pairs whose id share a common prefix >=40 chars
    ids_sorted = sorted(p.get("id", "") for p in all_meta if p.get("id"))
    trunc_pairs = []
    for i in range(len(ids_sorted) - 1):
        a, b = ids_sorted[i], ids_sorted[i + 1]
        if id_prefix_score(a, b) >= 40 and a != b:
            trunc_pairs.append((a, b))

    # Now confirm each candidate group is actually duplicates by content
    confirmed_groups = []
    for ids in same_file_groups:
        papers = [by_id[i] for i in ids if i in by_id]
        # Cluster within: pairwise duplicate test
        clusters = []
        used = set()
        for i, p in enumerate(papers):
            if i in used:
                continue
            grp = [p]
            used.add(i)
            for j in range(i + 1, len(papers)):
                if j in used:
                    continue
                if likely_duplicates(p, papers[j]):
                    grp.append(papers[j])
                    used.add(j)
            if len(grp) >= 2:
                clusters.append([x.get("id") for x in grp])
        confirmed_groups.extend(clusters)
    for a, b in trunc_pairs:
        if a not in by_id or b not in by_id:
            continue
        if likely_duplicates(by_id[a], by_id[b]):
            # Already in a same-file group?
            in_existing = False
            for grp in confirmed_groups:
                if a in grp and b in grp:
                    in_existing = True
                    break
                if a in grp:
                    grp.append(b)
                    in_existing = True
                    break
                if b in grp:
                    grp.append(a)
                    in_existing = True
                    break
            if not in_existing:
                confirmed_groups.append([a, b])

    # Deduplicate groups (same set of ids)
    seen = set()
    unique = []
    for grp in confirmed_groups:
        key = tuple(sorted(set(grp)))
        if key not in seen and len(key) >= 2:
            seen.add(key)
            unique.append(list(key))
    return unique


def require_backup_for_execute():
    backups = sorted(config.BACKUP_DIR.glob("papers-*.db"))
    if backups:
        return True
    print(
        "ABORT: No database backup found in "
        f"{config.BACKUP_DIR}. Create a papers-*.db backup before --execute."
    )
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print(f"Truncated/duplicate row dedupe (mode: {'EXECUTE' if args.execute else 'DRY RUN'})")
    print("=" * 70)

    all_meta = papers_db.get_all_metadata()
    by_id = {p.get("id"): p for p in all_meta}
    groups = find_dupe_groups(all_meta)
    raw_count = len(groups)

    # Apply manual SKIP_DROP_IDS exclusions: any group touching one of these is dropped.
    # (See SKIP_DROP_IDS constant at top for the audited false positives + parent-supp pairs.)
    filtered = []
    skipped_groups = []
    for grp in groups:
        if any(pid in SKIP_DROP_IDS for pid in grp):
            skipped_groups.append(grp)
            continue
        filtered.append(grp)
    groups = filtered

    print(f"Detected {raw_count} duplicate group(s); "
          f"{len(skipped_groups)} skipped via SKIP_DROP_IDS; "
          f"{len(groups)} groups to process\n")
    if skipped_groups:
        print("Skipped groups (manually excluded):")
        for grp in skipped_groups:
            print(f"  {grp}")
        print()

    plan = []  # (canonical_id, merged_paper, drop_ids)
    for grp in groups:
        papers = [by_id[i] for i in grp]
        # Pick canonical = richest metadata
        papers.sort(key=metadata_richness, reverse=True)
        canonical = papers[0]
        others = papers[1:]
        merged = canonical
        for o in others:
            merged = merge_rows(merged, o)
        drop_ids = [o.get("id") for o in others]
        plan.append((canonical.get("id"), merged, drop_ids))

        print(f"  KEEP: {canonical.get('id')}")
        title = (canonical.get("basic_metadata") or {}).get("title") or ""
        print(f"    title: {title[:80]}")
        for o in others:
            o_title = (o.get("basic_metadata") or {}).get("title") or ""
            print(f"  DROP: {o.get('id')}")
            print(f"    title: {o_title[:80]}")
        print()

    if not args.execute:
        print("DRY RUN. Pass --execute to apply.")
        return
    if not require_backup_for_execute():
        return

    from papers_db import ALL_COLUMNS, JSON_FIELDS, _serialize_field
    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        for canonical_id, merged, drop_ids in plan:
            # Update canonical row with merged content
            cl = merged.get("classification", {}) or {}
            rating = cl.get("rating") if isinstance(cl, dict) else None
            rts = cl.get("red_team_survival") if isinstance(cl, dict) else None
            if rts is not None:
                rts = str(rts)
            vals = []
            for col in ALL_COLUMNS:
                if col == "id":
                    vals.append(canonical_id)
                elif col == "rating":
                    vals.append(rating)
                elif col == "red_team_survival":
                    vals.append(rts)
                elif col in JSON_FIELDS:
                    vals.append(_serialize_field(merged.get(col)))
                else:
                    vals.append(merged.get(col))
            placeholders = ",".join(["?"] * len(ALL_COLUMNS))
            conn.execute(f"INSERT OR REPLACE INTO papers ({','.join(ALL_COLUMNS)}) VALUES ({placeholders})", tuple(vals))
            for did in drop_ids:
                conn.execute("DELETE FROM papers WHERE id = ?", (did,))
        conn.commit()
        print(f"Merged {len(plan)} group(s), deleted {sum(len(d) for _, _, d in plan)} duplicate row(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
