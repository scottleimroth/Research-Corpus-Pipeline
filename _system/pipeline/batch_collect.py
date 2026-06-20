#!/usr/bin/env python3
"""
Collect and validate batch results (dry-run / read-only by default).

Parses result JSONL, validates schema, writes staging validation report.
Does NOT write to papers.db unless EVAL_ALLOW_DB_WRITE=1 (not enabled in this phase).

Usage:
  python batch_collect.py --batch-id msgbatch_xxx --dry-run
  python batch_collect.py --results-file path/to/results.jsonl --dry-run
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_batch_common import parse_custom_id  # noqa: E402
from eval_constants import ALLOW_DB_WRITE, ALLOW_LIVE_API, EVAL_RESULTS_DIR  # noqa: E402
from eval_schema_v2 import validate_eval_payload_v2  # noqa: E402
from evaluate_paper import validate_eval_payload  # noqa: E402


def validate_result_body(text: str, *, schema_v2: bool) -> tuple[dict | None, str | None]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"json_parse:{e}"
    if schema_v2:
        return validate_eval_payload_v2(payload)
    return validate_eval_payload(payload)


def process_results_lines(lines: list[str], *, schema_v2: bool = True) -> dict:
    summary = {"ok": 0, "failed": 0, "items": []}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        cid = row.get("custom_id", "")
        parsed = parse_custom_id(cid)
        paper_id = parsed.get("paper_id", cid)
        if row.get("result", {}).get("type") == "errored":
            summary["failed"] += 1
            summary["items"].append(
                {"paper_id": paper_id, "custom_id": cid, "status": "errored"}
            )
            continue
        content = (
            row.get("result", {})
            .get("message", {})
            .get("content", [{}])[0]
            .get("text", "")
        )
        normalized, err = validate_result_body(content, schema_v2=schema_v2)
        if err:
            summary["failed"] += 1
            summary["items"].append(
                {"paper_id": paper_id, "custom_id": cid, "status": "invalid", "error": err}
            )
        else:
            summary["ok"] += 1
            summary["items"].append(
                {
                    "paper_id": paper_id,
                    "custom_id": cid,
                    "status": "validated",
                    "rating": normalized.get("classification", {}).get("rating"),
                }
            )
    return summary


def main():
    parser = argparse.ArgumentParser(description="Collect batch results")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--batch-id", type=str)
    parser.add_argument("--results-file", type=str)
    parser.add_argument("--schema-v2", action="store_true", default=True)
    args = parser.parse_args()

    if args.results_file:
        lines = Path(args.results_file).read_text(encoding="utf-8").splitlines()
    elif args.batch_id:
        if not ALLOW_LIVE_API:
            print("DRY RUN: would fetch batch results for", args.batch_id)
            print("Provide --results-file for offline validation, or set EVAL_ALLOW_LIVE_API=1")
            return
        import anthropic  # noqa: WPS433

        client = anthropic.Anthropic()
        # Placeholder: actual SDK result streaming varies by version
        print("Live batch fetch not wired in dry-run phase; use --results-file")
        return
    else:
        print("Provide --batch-id or --results-file")
        sys.exit(1)

    summary = process_results_lines(lines, schema_v2=args.schema_v2)
    EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = EVAL_RESULTS_DIR / "validation_summary.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Wrote {out}")
    if ALLOW_DB_WRITE:
        print("DB write path exists but remains disabled in this implementation phase.")
    else:
        print("No DB writes (EVAL_ALLOW_DB_WRITE not set).")


if __name__ == "__main__":
    main()
