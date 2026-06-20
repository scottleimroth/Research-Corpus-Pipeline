"""Phase 3a: extract methodological rubrics from all guideline PDFs.

Reads guidelines-intersect.json (built by cross_ref_guidelines.py), calls DeepSeek V3
once per guideline to produce a CONCISE structured rubric (~500-800 chars), and
compiles them all into standards-rubric.md which evaluate_paper.py loads.

Cost: ~$0.01-0.02 per guideline with DeepSeek V3. ~95 guidelines = ~$1-2 total.

Usage:
  python pipeline/extract_guideline_rubrics.py             # dry-run
  python pipeline/extract_guideline_rubrics.py --execute   # actually extract
  python pipeline/extract_guideline_rubrics.py --execute --force   # re-extract all
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
import config  # noqa: E402
from llm_providers import call_llm  # noqa: E402

STANDARDS_DIR = ROOT / "Methods Paper Project" / "standards"
INTERSECT_FILE = STANDARDS_DIR / "guidelines-intersect.json"
OUT_FILE = STANDARDS_DIR / "standards-rubric.md"
PER_GUIDELINE_DIR = STANDARDS_DIR / "per-guideline"

MODEL = "deepseek-v3"  # cheap and adequate for summarisation

# Guidelines that should NOT be extracted, with reason. Run-time skip list.
# Format: pdf_filename -> reason. Skipped before extraction is attempted.
SKIP_PDFS = {
    "knott_1993_regarding_american_electroencephalographic_society.pdf":
        "Letter-to-editor commentary critiquing AEEGS 1991 proposal. Image-only PDF "
        "(Wolters Kluwer watermarked scan) so text extraction fails anyway. Not a "
        "guideline itself - argues against renaming T3/4->T7/8. The Sharbrough 1991 "
        "and Sharbrough 1993 response are already extracted and capture the debate.",
}

EXTRACTION_PROMPT = """You are extracting a CONCISE methodological rubric from a published guideline paper.
Output exactly in this format. Keep it tight - aim for under 800 characters total.

# {short_name} ({year})

**Applies to:** [one-line field description, e.g. "EEG/MEG preprocessing"]
**Era:** Papers from {year}+ should follow these standards.

**Required reporting (checklist):**
- [3-6 most concrete required items, one sentence each, no fluff]

**Red flags (downgrade indicators):**
- [3-5 specific patterns that indicate weak methodology relative to these standards]

**Key recommendations:**
- [2-4 specific recommendations on technique/measurement/analysis]

Be specific. Use concrete metric names, thresholds, or procedures named in the guideline.
DO NOT include preamble, summary paragraphs, or text outside this format."""

# Public reporting standards not in corpus - hardcoded summaries
HARDCODED_RUBRICS = """
---

# STROBE Statement (2007)

**Applies to:** Observational research (cohort, case-control, cross-sectional)
**Era:** All observational studies 2007+

**Required reporting (checklist):**
- Title/abstract: study design indicated
- Methods: design, setting, participants, variables, bias, sample size, statistical methods
- Results: participant flow diagram, descriptive data, outcome data, main results
- Discussion: key results, limitations, interpretation, generalisability
- Funding declaration

**Red flags:**
- No flow diagram or participant accounting
- Selection bias not addressed
- Confounders not discussed
- Effect sizes absent

---

# CONSORT Statement (2010)

**Applies to:** Randomised controlled trials
**Era:** All RCTs 2010+

**Required reporting (checklist):**
- CONSORT flow diagram REQUIRED
- Randomisation: sequence generation + allocation concealment + implementation
- Blinding status (participants, providers, assessors)
- Sample size calculation
- Pre-registration of trial

**Red flags:**
- No CONSORT flow diagram
- Randomisation method not described or inadequate
- Allocation concealment not described
- Trial not pre-registered

---

# PRISMA 2020

**Applies to:** Systematic reviews and meta-analyses
**Era:** All systematic reviews 2020+

**Required reporting (checklist):**
- PRISMA flow diagram REQUIRED
- Search strategy must be reproducible (databases + dates + terms)
- Multiple databases searched (not single)
- Risk-of-bias assessment per study
- PROSPERO registration

**Red flags:**
- No PRISMA flow diagram
- Search strategy not reproducible
- Single-database search only
- No risk-of-bias assessment
- Not registered in PROSPERO

---

# APA 7th Edition Manuscript Standards (2020)

**Applies to:** Psychology manuscripts
**Era:** 2020+ (6th edition for 2010-2019)

**Required reporting (checklist):**
- Effect sizes for ALL inferential statistics (Cohen's d, η², etc.)
- Confidence intervals where applicable
- Exact p-values (not "p < .05")
- Methods reproducibility
- Limitations explicitly stated
- Open science encouraged (data/code/preregistration)

**Red flags:**
- p-values without effect sizes
- "Marginally significant" language
- Effect size types not specified
- Insufficient methods for replication
"""


def extract_rubric_for_pdf(pdf_path, short_name, year):
    """Read PDF text and call DeepSeek to produce the structured rubric."""
    from pdf_content_guard import extract_pypdf_text_safely

    # Extract first ~25 pages of PDF text (guidelines are usually front-loaded)
    text, err = extract_pypdf_text_safely(pdf_path, pages=25, tier="guideline_rubric_text")
    if err:
        return None, f"pdf read failed: {err}"

    if not text or len(text) < 500:
        return None, "PDF text too short or unreadable"

    if len(text) > 60000:
        text = text[:60000]

    prompt = EXTRACTION_PROMPT.format(short_name=short_name, year=year)
    user_msg = f"Guideline paper text:\n\n{text}\n\nProduce the rubric now."

    text_out, usage = call_llm(MODEL, system=prompt, user_text=user_msg, max_tokens=1500)
    return text_out.strip(), usage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-extract even if per-guideline file exists")
    ap.add_argument("--limit", type=int, default=None,
                    help="process at most N guidelines (for testing)")
    args = ap.parse_args()

    STANDARDS_DIR.mkdir(parents=True, exist_ok=True)
    PER_GUIDELINE_DIR.mkdir(parents=True, exist_ok=True)

    if not INTERSECT_FILE.exists():
        sys.exit(f"missing {INTERSECT_FILE} - run cross_ref_guidelines.py first")

    targets = json.loads(INTERSECT_FILE.read_text(encoding="utf-8"))
    print("=" * 70)
    print(f"Extract guideline rubrics (mode: {'EXECUTE' if args.execute else 'DRY RUN'})")
    print(f"Source: {INTERSECT_FILE.name} ({len(targets)} guidelines)")
    print(f"Model: {MODEL}")
    print("=" * 70)

    to_process = []
    existing = []
    skipped = []
    for rec in targets:
        pdf_name = rec.get("source_pdf")
        if not pdf_name:
            continue
        if pdf_name in SKIP_PDFS:
            skipped.append((pdf_name, SKIP_PDFS[pdf_name]))
            continue
        pdf_path = config.SOURCE_PDFS / pdf_name
        out_file = PER_GUIDELINE_DIR / (pdf_name.replace(".pdf", ".md"))
        if out_file.exists() and not args.force:
            existing.append(rec)
        else:
            to_process.append((pdf_path, rec, out_file))

    if skipped:
        print(f"Skipped (in SKIP_PDFS): {len(skipped)}")
        for pdf_name, reason in skipped:
            print(f"  {pdf_name}: {reason[:80]}")
        print()

    if args.limit:
        to_process = to_process[:args.limit]

    print(f"To process: {len(to_process)}")
    print(f"Already extracted: {len(existing)}")
    print()
    if to_process[:5]:
        print("First 5 to process:")
        for pdf, rec, out in to_process[:5]:
            print(f"  {rec.get('csv_first_author')} {rec.get('csv_year')}: "
                  f"{rec.get('csv_title', '')[:60]}")
        print()

    if not args.execute:
        print("DRY RUN. Pass --execute to actually extract.")
        return

    if not os.environ.get("DEEPSEEK_API_KEY"):
        sys.exit("DEEPSEEK_API_KEY env var not set.")

    total_cost = 0.0
    ok = 0
    failed = []
    for i, (pdf, rec, out) in enumerate(to_process, 1):
        short_name = f"{rec.get('csv_first_author', 'Unknown')} {rec.get('csv_year', '')}"
        year = rec.get("csv_year", "")
        print(f"\n[{i}/{len(to_process)}] {short_name}")
        try:
            rubric, usage = extract_rubric_for_pdf(pdf, short_name, year)
            if rubric is None:
                failed.append((short_name, usage))  # usage holds error msg here
                print(f"  !! {usage}")
                continue
            out.write_text(rubric, encoding="utf-8")
            cost = usage.get("cost_usd", 0)
            total_cost += cost
            ok += 1
            print(f"  -> {out.name} (cost ${cost:.4f}, total ${total_cost:.3f})")
        except Exception as e:
            failed.append((short_name, str(e)))
            print(f"  !! error: {e}")
        time.sleep(0.5)

    # Compile master
    print("\nCompiling master standards-rubric.md...")
    parts = [
        "# Methodological Standards Rubric",
        "",
        f"Auto-generated from {ok} guideline PDFs in source-pdfs/ via {MODEL} "
        f"(last updated {datetime.now():%Y-%m-%d}).",
        "",
        "Apply standards based on the paper's field and publication date. Where multiple "
        "guidelines apply, use the most recent and most specific. The hardcoded sections "
        "at the end (STROBE/CONSORT/PRISMA/APA) cover general reporting standards.",
        "",
    ]
    # Sort by year descending so newest guidelines are first
    sorted_recs = sorted(targets,
                         key=lambda r: -int(r.get("csv_year") or 0) if str(r.get("csv_year", "")).isdigit() else 0)
    for rec in sorted_recs:
        pdf_name = rec.get("source_pdf")
        if not pdf_name:
            continue
        out_file = PER_GUIDELINE_DIR / (pdf_name.replace(".pdf", ".md"))
        if out_file.exists():
            parts.append(out_file.read_text(encoding="utf-8"))
            parts.append("")
            parts.append("---")
            parts.append("")
    parts.append(HARDCODED_RUBRICS)
    OUT_FILE.write_text("\n".join(parts), encoding="utf-8")

    print(f"\nExtracted: {ok}")
    print(f"Failed: {len(failed)}")
    print(f"Total cost: ${total_cost:.3f}")
    print(f"Master file: {OUT_FILE}")
    if failed:
        print("\nFailed guidelines:")
        for name, err in failed[:10]:
            print(f"  {name}: {err}")


if __name__ == "__main__":
    main()
