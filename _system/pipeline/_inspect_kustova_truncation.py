"""Read-only: show what blind 100k cap would drop for Kustova."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import papers_db
from evaluate_paper import get_paper_text

PID = "Kustova_2025_Psychophysiological"
paper = papers_db.get_paper(PID)
ft = papers_db.load_fulltext(PID)
text = get_paper_text(paper)
cap = 100_000

print(f"DB total_chars: {ft.get('total_chars')}")
sections = ft.get("sections") or {}
print(f"Section keys ({len(sections)}): {list(sections.keys())}")
print(f"Concatenated text length: {len(text)}")

if len(text) > cap:
    kept = text[:cap]
    dropped = text[cap:]
    # Find which section headers appear in dropped part
    in_kept = set()
    in_dropped = set()
    for key in sections:
        marker = f"=== {key.upper()} ==="
        if marker in kept:
            in_kept.add(key)
        if marker in dropped or (marker not in kept and marker in text[cap - 5000 : cap + 5000]):
            pass
    # Simpler: walk sections in concat order
    pos = 0
    print("\nSection coverage under blind 100k cap:")
    for key, value in sections.items():
        block = f"=== {key.upper()} ===\n{str(value).strip()}"
        start = text.find(block[:80]) if block else -1
        if start < 0:
            # fallback by marker only
            marker = f"=== {key.upper()} ==="
            start = text.find(marker)
        end = start + len(block) if start >= 0 else -1
        if start < 0:
            status = "not_found_in_concat"
        elif end <= cap:
            status = "FULLY_KEPT"
        elif start >= cap:
            status = "FULLY_OMITTED"
        else:
            pct = max(0, (cap - start) / len(block) * 100) if block else 0
            status = f"PARTIAL ({pct:.0f}% kept)"
        print(f"  {key}: {len(str(value)):,} chars -> {status}")

    print(f"\nDropped tail: {len(dropped):,} chars")
    print(f"First 200 chars of omitted region:\n{dropped[:200]!r}")
