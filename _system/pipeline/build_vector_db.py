#!/usr/bin/env python3
"""Build or rebuild sentence-transformers vector index from papers.db."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from vector_index import DEFAULT_CHUNK_CHARS, DEFAULT_MODEL, DEFAULT_OVERLAP_CHARS, VectorBuildConfig, build_or_update


def main() -> int:
    parser = argparse.ArgumentParser(description="Build vector DB from papers.db")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    parser.add_argument("--overlap-chars", type=int, default=DEFAULT_OVERLAP_CHARS)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--limit", type=int, default=None, help="Optional test cap on papers")
    parser.add_argument("--fresh", action="store_true", help="Delete existing collection and rebuild")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress lines")
    args = parser.parse_args()

    cfg = VectorBuildConfig(
        model_name=args.model,
        chunk_chars=args.chunk_chars,
        overlap_chars=args.overlap_chars,
        batch_size=args.batch_size,
        fresh=args.fresh,
        limit=args.limit,
        progress=not args.quiet,
    )
    result = build_or_update(cfg)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
