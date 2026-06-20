"""Dump full metadata for suspicious dedupe groups so Claude can audit them.

Reads the DB, finds the listed ids, dumps each row's full metadata + full_text head
to JSON for inspection.

Usage:
  python pipeline/inspect_dupe_groups.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
import config  # noqa: E402
import papers_db  # noqa: E402


SUSPICIOUS_GROUPS = [
    # (label, [ids in group])
    ("Lee1918 vs Bharucha2006", [
        "Lee1918",
        "Bharucha_Varieties_of_musical_experience_2006_Cognition",
    ]),
    ("Bonett2000 vs Ferguson", [
        "Bonett2000",
        "FERGUSON_ACHIEVING_STATISTICS_SELF_ACTUALIZATION__FACULTY_SU",
    ]),
    ("Barry_Disruption vs Barry_Fact_or_artifact", [
        "1_s2_0_0031938478902718_main",
        "Barry_Fact_or_artifact__reply_to_Lobstein__Turpin_and_Siddle",
    ]),
    ("BarryMaltzman 1985 vs BARRY_1966_Heart", [
        "BarryMaltzman1985",
        "BARRY_1966_Heart",
    ]),
    ("Bishop1933 vs ajplegacy_1932", [
        "Bishop1933",
        "ajplegacy_1932_103_1_213",
    ]),
    ("Borges2024 vs Teixeira_Borges_2019", [
        "Borges2024",
        "Teixeira_Borges_et_al___2019___Scaling_behaviour_in_music_an",
    ]),
    ("CowleyFranchini2004 vs Franchini_Primer_book", [
        "CowleyFranchini2004",
        "Franchini_Primer_on_the_Autonomic_Nervous_System__Second_Edi",
    ]),
    ("Morgenstern + Haas (3-way)", [
        "Morgenstern_2009_Influence",
        "MorgensternUnknown",
        "Haas_Effects_of_perceived_musical_rhythm_on_respiratory_patt",
    ]),
    ("Bracewell1966 vs Osgood_Fourier", [
        "Bracewell1966",
        "Osgood_YYYY_Fourier",
    ]),
    ("Fox2009 vs Fox_illusion_of_time", [
        "Fox2009",
        "Fox_The_illusion_of_time_2009_New_Scientist",
    ]),
    ("Susan2018 vs Susan_Hallam_Routledge", [
        "Susan2018",
        "Susan_Hallam___Psychology_of_Music_Routledge__2018_",
    ]),
    ("Iversen2021 vs EEGLAB_2021_UCSD", [
        "Iversen2021",
        "EEGLAB_2021_UCSD",
    ]),
]


def trim_text(s, n=2000):
    if not s:
        return ""
    s = str(s)
    if len(s) <= n:
        return s
    return s[:n] + f"... [TRUNCATED, total {len(s)} chars]"


def main():
    all_meta = {p.get("id"): p for p in papers_db.get_all_metadata()}
    out = {}
    for label, ids in SUSPICIOUS_GROUPS:
        group = []
        for pid in ids:
            paper = all_meta.get(pid)
            if not paper:
                group.append({"id": pid, "NOT_FOUND": True})
                continue
            row = {
                "id": pid,
                "title": (paper.get("basic_metadata") or {}).get("title"),
                "authors": (paper.get("basic_metadata") or {}).get("authors"),
                "year": (paper.get("basic_metadata") or {}).get("year"),
                "journal": (paper.get("basic_metadata") or {}).get("journal"),
                "doi": (paper.get("identifiers") or {}).get("doi"),
                "abstract": trim_text((paper.get("abstract_keywords") or {}).get("abstract"), 1500),
                "renamed_filename": (paper.get("file_info") or {}).get("renamed_filename"),
                "tags": paper.get("tags"),
                "full_text_head": trim_text(paper.get("full_text"), 2000),
            }
            group.append(row)
        out[label] = group

    out_path = ROOT / "logs" / "dupe-inspect.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(f"Groups inspected: {len(out)}")
    for label, group in out.items():
        print(f"  {label}: {len(group)} rows")


if __name__ == "__main__":
    main()
