"""Read-only: section structure for batch-10 papers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import papers_db
from evaluate_paper import get_paper_text

IDS = [
    "Kustova_2025_Psychophysiological",
    "Ishida_2025_Active",
    "Duda_2025_Differential",
    "SteinerLim_2025_Exploring",
    "Gautier_2025_Characterizing",
    "Duda_2025_Mindfulness",
    "Ueberholz_2025_Effectiveness",
    "Hansen_2025_Normative",
    "Yanagi_2025_Concurrent",
    "Watson_2025_RestingState",
]

for pid in IDS:
    ft = papers_db.load_fulltext(pid)
    sections = ft.get("sections") or {}
    concat = get_paper_text(papers_db.get_paper(pid))
    full_len = len(str(sections.get("full", "")))
    struct_len = sum(len(str(v)) for k, v in sections.items() if k.lower() != "full")
    print(f"{pid}")
    print(f"  keys: {list(sections.keys())}")
    print(f"  db total_chars: {ft.get('total_chars')}  concat: {len(concat)}  full_sec: {full_len}  struct_only: {struct_len}")
