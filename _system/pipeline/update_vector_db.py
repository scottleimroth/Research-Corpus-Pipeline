#!/usr/bin/env python3
"""Incrementally update sentence-transformers vector index from papers.db."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from vector_index import DEFAULT_CHUNK_CHARS, DEFAULT_MODEL, DEFAULT_OVERLAP_CHARS, VectorBuildConfig, build_or_update, load_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Incremental vector DB update")
    parser.add_argument("--model", default="", help="Optional model override; defaults to manifest model or built-in default")
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    parser.add_argument("--overlap-chars", type=int, default=DEFAULT_OVERLAP_CHARS)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    manifest = load_manifest()
    model = args.model or str(manifest.get("model") or DEFAULT_MODEL)
    cfg = VectorBuildConfig(
        model_name=model,
        chunk_chars=int(manifest.get("chunk_chars") or args.chunk_chars),
        overlap_chars=int(manifest.get("overlap_chars") or args.overlap_chars),
        batch_size=args.batch_size,
        fresh=False,
        limit=args.limit,
    )
    result = build_or_update(cfg)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
