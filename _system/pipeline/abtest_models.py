"""A/B test models on the same papers.

Picks N unrated papers from DB, runs them through 2+ models, dumps side-by-side
ratings + costs so Scott can compare agreement before committing to a model
for the bulk eval.

Usage:
  python pipeline/abtest_models.py --models sonnet-4.5,gpt-5-mini,deepseek-v3 --n 10
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
import papers_db  # noqa: E402
from evaluate_paper import (  # noqa: E402
    EVAL_SYSTEM_PROMPT, build_eval_system_prompt, VALID_RATINGS,
)
from llm_providers import call_llm, MODELS  # noqa: E402


def pick_unrated(n):
    """Pick N rows that have no rating. Prefer rows with substantial full_text."""
    import sqlite3
    import config
    conn = sqlite3.connect(str(config.DB_PATH))
    rows = conn.execute("""
        SELECT id, classification, full_text
        FROM papers
        WHERE full_text IS NOT NULL AND length(full_text) > 3000
        ORDER BY RANDOM()
        LIMIT ?
    """, (n * 4,)).fetchall()
    conn.close()
    out = []
    for pid, cl_json, ftxt in rows:
        try:
            cl = json.loads(cl_json) if cl_json else {}
        except Exception:
            cl = {}
        if not cl.get("rating"):
            out.append((pid, ftxt))
            if len(out) >= n:
                break
    return out


def extract_rating(text):
    """Pull the rating field from JSON output, robust to markdown fences."""
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r'"rating"\s*:\s*"([^"]+)"', text)
        return m.group(1) if m else None
    cl = data.get("classification", {})
    if isinstance(cl, dict):
        return cl.get("rating")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True,
                    help="comma-separated model keys, e.g. sonnet-4.5,gpt-5-mini,deepseek-v3")
    ap.add_argument("--n", type=int, default=10, help="number of papers to test")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",")]
    unknown = [m for m in models if m not in MODELS]
    if unknown:
        sys.exit(f"unknown models: {unknown}. Available: {list(MODELS)}")

    # Validate API keys are set
    need_keys = set()
    for m in models:
        provider, *_ = MODELS[m]
        if provider == "anthropic":
            need_keys.add("ANTHROPIC_API_KEY")
        elif provider == "openai":
            need_keys.add("OPENAI_API_KEY")
        elif provider == "deepseek":
            need_keys.add("DEEPSEEK_API_KEY")
    missing = [k for k in need_keys if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing env vars: {missing}")

    papers = pick_unrated(args.n)
    print(f"Selected {len(papers)} unrated papers for A/B test")
    print(f"Models: {models}")
    print()

    system_prompt = build_eval_system_prompt()
    results = []  # list of {paper_id, per_model: {model_key: {rating, cost, text}}}

    for i, (pid, ftxt) in enumerate(papers, 1):
        print(f"[{i}/{len(papers)}] {pid}")
        if len(ftxt) > 80000:
            ftxt = ftxt[:80000] + "\n[TRUNCATED]"
        user_msg = (f"Evaluate this paper. Return the COMPLETE JSON schema.\n\n"
                    f"{ftxt}\n\nReturn ONLY the JSON evaluation. No markdown fences.")
        row = {"paper_id": pid, "per_model": {}}
        for m in models:
            print(f"  -> {m}")
            try:
                text, usage = call_llm(m, system_prompt, user_msg, max_tokens=8192)
                rating = extract_rating(text)
                row["per_model"][m] = {
                    "rating": rating,
                    "cost_usd": usage["cost_usd"],
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "raw_chars": len(text),
                }
                print(f"     rating={rating} cost=${usage['cost_usd']:.4f}")
            except Exception as e:
                row["per_model"][m] = {"error": str(e)}
                print(f"     ERROR: {e}")
            time.sleep(1)
        results.append(row)

    # Summary table
    print("\n" + "=" * 80)
    print("Side-by-side ratings")
    print("=" * 80)
    header = f"{'paper_id':<40}  " + "  ".join(f"{m:<20}" for m in models)
    print(header)
    print("-" * len(header))
    for r in results:
        pid = r["paper_id"][:38]
        cells = []
        for m in models:
            rec = r["per_model"].get(m, {})
            cells.append(f"{(rec.get('rating') or 'ERR'):<20}")
        print(f"{pid:<40}  " + "  ".join(cells))

    # Agreement summary
    print("\nAgreement summary:")
    n_agree_all = sum(
        1 for r in results
        if len({r["per_model"].get(m, {}).get("rating") for m in models}) == 1
        and r["per_model"].get(models[0], {}).get("rating") is not None
    )
    print(f"  All models agree: {n_agree_all}/{len(results)}")

    # Cost summary
    print("\nCost summary:")
    for m in models:
        total = sum(r["per_model"].get(m, {}).get("cost_usd", 0) for r in results)
        print(f"  {m}: ${total:.4f} for {len(results)} papers (${total / max(1, len(results)):.4f}/paper)")
        print(f"    extrapolated to 826 papers: ${total / max(1, len(results)) * 826:.2f}")

    # Save detail
    out_path = ROOT / f"abtest-results-{datetime.now():%Y-%m-%d-%H%M%S}.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nFull results: {out_path.name}")


if __name__ == "__main__":
    main()
