"""Quick diagnostic: count how many rows need evaluate_paper.py run.

Categorises rows by evaluation state:
  - has_rating + survived_red_team    -> fully evaluated
  - has_rating + no_red_team_data     -> rated but red team never run
  - has_rating but rating not in VALID_RATINGS -> stale/invalid
  - no_rating                          -> NEW backlog (Phase 2u target)
"""
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
import papers_db  # noqa: E402

VALID_RATINGS = {
    "landmark", "strong", "adequate", "weak", "flawed",
    "not_applicable", "not_ratable_reference_material"
}

NEEDS_RED_TEAM = {"landmark", "strong"}


def main():
    all_meta = papers_db.get_all_metadata()
    print(f"Total rows: {len(all_meta)}")

    state = Counter()
    rating_dist = Counter()
    no_rating_sample = []
    red_team_missing = []
    invalid_rating = []

    for p in all_meta:
        pid = p.get("id")
        cl = p.get("classification") or {}
        rating = None
        red_team = None
        if isinstance(cl, dict):
            rating = cl.get("rating")
            red_team = cl.get("red_team_survival")

        # Also check top-level rating column (papers_db stores both)
        if not rating:
            rating = p.get("rating")

        if not rating:
            state["no_rating"] += 1
            if len(no_rating_sample) < 15:
                title = ((p.get("basic_metadata") or {}).get("title") or "")[:60]
                no_rating_sample.append(f"  {pid}: {title}")
            continue

        rating_dist[rating] += 1
        if rating not in VALID_RATINGS:
            state["invalid_rating"] += 1
            if len(invalid_rating) < 10:
                invalid_rating.append(f"  {pid}: {rating}")
            continue

        if rating in NEEDS_RED_TEAM and red_team is None:
            state["needs_red_team"] += 1
            if len(red_team_missing) < 10:
                red_team_missing.append(f"  {pid}: rating={rating}, red_team=None")
        else:
            state["fully_evaluated"] += 1

    print()
    print("Evaluation state:")
    for k, v in state.most_common():
        print(f"  {k}: {v}")
    print()
    print("Rating distribution (among rated):")
    for k, v in rating_dist.most_common():
        print(f"  {k}: {v}")
    print()
    if no_rating_sample:
        print(f"Sample unrated (will need full eval):")
        for line in no_rating_sample:
            print(line)
        print()
    if red_team_missing:
        print(f"Sample rated but red team missing:")
        for line in red_team_missing:
            print(line)
        print()
    if invalid_rating:
        print(f"Sample invalid ratings:")
        for line in invalid_rating:
            print(line)
        print()

    # Cost estimate
    cost_full = state.get("no_rating", 0) * 0.17
    cost_red_team = state.get("needs_red_team", 0) * 0.08  # red team is cheaper - only one pass
    print(f"Estimated Phase 2u cost (Sonnet 4.5):")
    print(f"  Full eval ({state.get('no_rating', 0)} papers × $0.17): ${cost_full:.2f}")
    print(f"  Red team only ({state.get('needs_red_team', 0)} papers × $0.08): ${cost_red_team:.2f}")
    print(f"  Total: ${cost_full + cost_red_team:.2f}")


if __name__ == "__main__":
    main()
