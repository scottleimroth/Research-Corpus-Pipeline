#!/usr/bin/env python3
"""Reset corpus to empty DB + empty source PDFs (PUBLIC template)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import config  # noqa: E402
import papers_db  # noqa: E402


def _clear_dir(path: Path, *, keep_gitkeep: bool = True) -> int:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return 0
    removed = 0
    for item in path.iterdir():
        if item.name == ".gitkeep":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink(missing_ok=True)
        removed += 1
    if keep_gitkeep and not any(path.iterdir()):
        (path / ".gitkeep").write_text("", encoding="utf-8")
    return removed


def main() -> int:
    profile = "personal"
    if config.CORPUS_PROFILE_FILE.is_file():
        try:
            profile = __import__("json").loads(
                config.CORPUS_PROFILE_FILE.read_text(encoding="utf-8")
            ).get("profile", "personal")
        except Exception:
            pass
    force = "--force" in sys.argv
    is_public_folder = "PUBLIC" in config.PROJECT_ROOT.name.upper()
    if profile != "public" and not is_public_folder and not force:
        print(
            "ERROR: reset_public_corpus refuses to run on a personal/LIVE copy.\n"
            "  Use CorpusPipeline-PUBLIC, or pass --force if you really mean it.",
            file=sys.stderr,
        )
        return 1

    for suffix in ("", "-wal", "-shm"):
        p = Path(str(config.DB_PATH) + suffix)
        p.unlink(missing_ok=True)

    papers_db.init_db()
    if papers_db.get_paper_count() != 0:
        print("ERROR: DB not empty after init", file=sys.stderr)
        return 1

    pdf_removed = _clear_dir(config.SOURCE_PDFS)
    staging_removed = _clear_dir(config.STAGING)
    _clear_dir(config.PENDING_PARENT_CHILD_DOCUMENTS, keep_gitkeep=False)
    _clear_dir(config.REVIEW_DIR, keep_gitkeep=False)

    secrets = config.SECRETS_DIR / "anthropic.env"
    enc = config.SECRETS_DIR / "anthropic.env.enc"
    if secrets.exists():
        secrets.unlink()
    if enc.exists():
        enc.unlink()

    profile = {
        "profile": "public",
        "api_mode": "openrouter",
        "eval_model_key": "openrouter-deepseek-v4-pro",
        "eval_escalation": [
            "openrouter-deepseek-v4-pro",
            "openrouter-kimi-k2.5",
        ],
        "vision_model_key": "openrouter-qwen3-vl-32b",
        "allow_anthropic": True,
        "allow_openai": True,
        "description": (
            "Empty public MetaCheck starter corpus. SETUP lets the user choose OpenRouter, "
            "DeepSeek, OpenAI, Anthropic, local/free mode, or guided help."
        ),
    }
    config.CORPUS_PROFILE_FILE.write_text(
        __import__("json").dumps(profile, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"PUBLIC reset OK: db_rows=0, source_pdfs_removed={pdf_removed}, staging_cleared={staging_removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
