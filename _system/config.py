"""
config.py - Single source of truth for paths and runtime settings.

Portable layout:
  CorpusPipeline-LIVE/          <- PROJECT_ROOT (copy this folder)
    README.md
    SETUP.bat / RUN.bat / RUN_NoGUI.bat
    papers-staging/             <- drop candidate PDFs here
    papers-rejected/            <- not added (auto-purged per file after 14 days)
    INTAKE_LAST_RUN.md          <- human summary after each run
    _system/                    <- pipeline internals (do not edit manually)
      config.py
      pipeline/
      CorpusStore/
      ...

Set PAPERS_BASE to PROJECT_ROOT if you relocate the folder.
"""
import json
import os
import shutil
import sys
from pathlib import Path

# Provenance marker: Scott Leimroth, Copyright 2026.

# ---------------------------------------------------------------------------
# Base path resolution
# ---------------------------------------------------------------------------
_config_dir = Path(__file__).resolve().parent
_env_root = os.environ.get("PAPERS_BASE", "").strip()

if _env_root:
    PROJECT_ROOT = Path(_env_root).resolve()
    SYSTEM_ROOT = PROJECT_ROOT / "_system"
elif _config_dir.name == "_system":
    SYSTEM_ROOT = _config_dir
    PROJECT_ROOT = SYSTEM_ROOT.parent
else:
    # Legacy flat layout (pre-_system folder)
    SYSTEM_ROOT = _config_dir
    PROJECT_ROOT = _config_dir

# Backward-compatible alias: internal data/code paths live under SYSTEM_ROOT.
BASE = SYSTEM_ROOT

# ---------------------------------------------------------------------------
# Core directories — CorpusStore holds live DB, PDFs, backups, exports
# ---------------------------------------------------------------------------
CORPUS_STORE = SYSTEM_ROOT / "CorpusStore"
MASTER_PROJECT = CORPUS_STORE  # backward-compatible alias used by pipeline imports
DB_PATH        = CORPUS_STORE / "papers.db"
BACKUP_DIR     = CORPUS_STORE / "backups"
SOURCE_PDFS    = CORPUS_STORE / "source-pdfs"
CSV_PATH       = CORPUS_STORE / "papers-master-audit.csv"

# User-facing intake at project root
STAGING        = PROJECT_ROOT / "papers-staging"
PAPERS_REJECTED = PROJECT_ROOT / "papers-rejected"
INTAKE_LAST_RUN_MD = PROJECT_ROOT / "INTAKE_LAST_RUN.md"
REJECTED_RETENTION_DAYS = 14
STAGING_METADATA = CORPUS_STORE / "staging-metadata"
RECOVERED_DELETION_AUDIT = SYSTEM_ROOT / "recovered-deletion-audit"
FILE_DISPOSITION_AUDIT_DIR = CORPUS_STORE / "audit" / "file-dispositions"
DUPLICATES_DIR = SYSTEM_ROOT / "duplicates"

LOGS_DIR       = SYSTEM_ROOT / "logs"
DELETION_RECOVERY_LOG_DIR = LOGS_DIR / "deletion-recovery-audit"
REVIEW_RESOLUTION_LOG_DIR = LOGS_DIR / "review-resolution"
RUNS_DIR       = LOGS_DIR / "runs"
STAGING_LOGS   = LOGS_DIR
ERROR_LOG      = LOGS_DIR / "pipeline-errors.log"
DUPE_LOG       = LOGS_DIR / "dupe-review.log"
EVAL_LOG       = LOGS_DIR / "evaluation.log"

REVIEW_DIR               = SYSTEM_ROOT / "review"
REVIEW_UNIDENTIFIED      = REVIEW_DIR / "unidentified"
REVIEW_SUPPLEMENTS       = REVIEW_DIR / "supplements-unlinked"
REVIEW_SUPPLEMENTS_PENDING = REVIEW_DIR / "supplements-pending-parent"
REVIEW_DUPLICATES        = REVIEW_DIR / "duplicates"
REVIEW_DUPLICATE_AMBIGUOUS = REVIEW_DIR / "duplicate-or-ambiguous"
REVIEW_NEEDS_METADATA    = REVIEW_DIR / "needs-metadata"
REVIEW_INSUFFICIENT_TEXT = REVIEW_DIR / "insufficient-text"
REVIEW_EVAL_FAILED       = REVIEW_DIR / "evaluation-failed"
REVIEW_EVAL_ERROR        = REVIEW_DIR / "evaluation-error"
REVIEW_TECHNICAL_FAILURE = REVIEW_DIR / "technical-failure"
REVIEW_RECOVERY_PENDING = REVIEW_DIR / "recovery-pending"
PENDING_PARENT_CHILD_DOCUMENTS = SYSTEM_ROOT / "pending-parent-child-documents"
PENDING_CHILD_EXPIRY_DAYS = 180
STAGING_REVIEW           = REVIEW_DIR

EXPORT_DIR     = CORPUS_STORE / "exports"
VECTOR_DB_DIR  = CORPUS_STORE / "vector-db"
VECTOR_DB_CHROMA_DIR = VECTOR_DB_DIR / "chroma"
VECTOR_DB_MANIFEST = VECTOR_DB_DIR / "index_manifest.json"
VECTOR_MODEL_DIR = SYSTEM_ROOT / "models" / "sentence-transformers"
METACHECK_CACHE_DIR = CORPUS_STORE / "cache" / "metacheck"
GROBID_CACHE_DIR = CORPUS_STORE / "cache" / "grobid"
METACHECK_LOG_DIR = LOGS_DIR / "metacheck"

PIPELINE_DIR   = SYSTEM_ROOT / "pipeline"
DOCS_DIR       = SYSTEM_ROOT / "docs"
ARCHIVE_DIR    = SYSTEM_ROOT / "archive"
LAUNCHER_DIR   = SYSTEM_ROOT / "launcher"
VENV_DIR       = SYSTEM_ROOT / "venv"
TOOLS_DIR      = SYSTEM_ROOT / "tools"
SECRETS_DIR    = SYSTEM_ROOT / "secrets"
CORPUS_PROFILE_FILE = SYSTEM_ROOT / "corpus_profile.json"
ANTHROPIC_KEY_FILE = SECRETS_DIR / "anthropic.env"
ANTHROPIC_ENC_FILE = SECRETS_DIR / "anthropic.env.enc"
DEEPSEEK_KEY_FILE = SECRETS_DIR / "deepseek.env"

PORTABLE_TOOL_BIN_DIRS = [
    TOOLS_DIR / "poppler" / "Library" / "bin",
    TOOLS_DIR / "tesseract",
    TOOLS_DIR / "ghostscript" / "bin",
    TOOLS_DIR / "python-embed",
]


def apply_portable_tool_path() -> list[str]:
    """Prepend bundled tool folders to PATH (Windows portable copies)."""
    added: list[str] = []
    parts = [str(p) for p in PORTABLE_TOOL_BIN_DIRS if p.is_dir()]
    venv_scripts = VENV_DIR / "Scripts"
    if venv_scripts.is_dir():
        parts.append(str(venv_scripts))
    if not parts:
        return added
    current = os.environ.get("PATH", "")
    prefix = os.pathsep.join(parts)
    if not current.startswith(prefix):
        os.environ["PATH"] = prefix + os.pathsep + current
    added.extend(parts)
    return added


def _load_folder_secrets() -> None:
    """Load folder-local secrets.

    Encrypted vault (anthropic.env.enc) is unlocked at RUN time via passphrase.
    Legacy plaintext anthropic.env is loaded only if no .enc exists (migrate via SETUP).
    """
    if ANTHROPIC_ENC_FILE.is_file():
        return
    if not ANTHROPIC_KEY_FILE.is_file():
        return
    try:
        for raw in ANTHROPIC_KEY_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                os.environ[key] = value
    except OSError:
        pass


apply_portable_tool_path()
_load_folder_secrets()


def _load_corpus_profile() -> dict:
    if not CORPUS_PROFILE_FILE.is_file():
        return {"profile": "personal", "api_mode": "anthropic"}
    try:
        data = json.loads(CORPUS_PROFILE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"profile": "personal", "api_mode": "anthropic"}
        if "profile" not in data:
            data["profile"] = "personal"
        if "api_mode" not in data:
            data["api_mode"] = "deepseek"
        if "eval_model_key" not in data:
            data["eval_model_key"] = "deepseek-v4-pro"
        return data
    except (OSError, json.JSONDecodeError):
        return {"profile": "personal", "api_mode": "deepseek", "eval_model_key": "deepseek-v4-pro"}


CORPUS_PROFILE = _load_corpus_profile()
CORPUS_PROFILE_NAME = str(CORPUS_PROFILE.get("profile") or "personal").lower()
CORPUS_API_MODE = str(CORPUS_PROFILE.get("api_mode") or "deepseek").lower()
USE_LOCAL_OLLAMA_ONLY = CORPUS_PROFILE_NAME == "public" and CORPUS_API_MODE == "ollama_local"

# ---------------------------------------------------------------------------
# External services
# ---------------------------------------------------------------------------
CROSSREF_USER_AGENT = "PapersCorpus/1.0 (mailto:scott.leimroth@gmail.com)"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
CLAUDE_MODEL      = "claude-haiku-4-5-20251001"

sys.path.insert(0, str(PIPELINE_DIR))
from eval_model_config import (  # noqa: E402
    assert_eval_api_key,
    resolve_eval_model_key,
    resolve_model_api_id,
)

EVAL_MODEL_KEY = resolve_eval_model_key(profile=CORPUS_PROFILE, api_mode=CORPUS_API_MODE)
EVAL_MODEL = resolve_model_api_id(EVAL_MODEL_KEY)
# Legacy alias: staging multimodal arbitration still references Sonnet API id when api_mode=anthropic.
STAGING_LLM_MODEL = os.environ.get("STAGING_LLM_MODEL", "").strip() or (
    EVAL_MODEL if CORPUS_API_MODE == "anthropic" else "claude-sonnet-4-5-20250929"
)
OPUS_RESOLUTION_MODEL = "claude-opus-4-20250514"
OPUS_INPUT_USD_PER_MTOK = 15.0
OPUS_OUTPUT_USD_PER_MTOK = 75.0
SONNET_ESTIMATE_USD_PER_PAPER = 0.07
USD_TO_AUD = float(os.environ.get("USD_TO_AUD", "1.55"))

OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
METACHECK_MODE = "advanced"
METACHECK_ENABLED = True
METACHECK_API_URL = os.environ.get("METACHECK_API_URL", "http://127.0.0.1:2005").rstrip("/")
GROBID_API_URL = os.environ.get("GROBID_API_URL", "http://127.0.0.1:8070").rstrip("/")
METACHECK_MODULES = [
    x.strip()
    for x in os.environ.get(
        "METACHECK_MODULES",
        "stat_check,stat_p_exact,stat_p_nonsig,stat_effect_size,power,"
        "ref_retraction,ref_pubpeer,ref_accuracy,ref_consistency,"
        "open_practices,funding_check,coi_check,prereg_check",
    ).split(",")
    if x.strip()
]

METADATA_TIERS = ["crossref", "pdf_embed", "claude", "ollama", "filename", "review"]

REQUIRED_BINARIES = {
    "pdftotext": "Part of Poppler. Linux: apt install poppler-utils. Windows: github.com/oschwartz10612/poppler-windows",
    "tesseract": "OCR engine. Linux: apt install tesseract-ocr. Windows: github.com/UB-Mannheim/tesseract",
    "ocrmypdf":  "PDF OCR wrapper. pip install ocrmypdf (after tesseract + ghostscript).",
}


def check_binaries(strict=False):
    """Verify required external binaries are on PATH."""
    found = {}
    missing = []
    for name in REQUIRED_BINARIES:
        path = shutil.which(name)
        found[name] = path
        if not path:
            missing.append(name)
    if missing and strict:
        msg = "Missing required binaries: " + ", ".join(missing) + "\n"
        for name in missing:
            msg += f"  {name}: {REQUIRED_BINARIES[name]}\n"
        raise RuntimeError(msg)
    return found


def ensure_dirs():
    """Create runtime directories that should always exist."""
    for d in [
        SECRETS_DIR,
        TOOLS_DIR,
        BACKUP_DIR, SOURCE_PDFS, STAGING, PAPERS_REJECTED, STAGING_METADATA, DUPLICATES_DIR, LOGS_DIR, RUNS_DIR,
        EXPORT_DIR, VECTOR_DB_DIR,
        VECTOR_DB_CHROMA_DIR, VECTOR_MODEL_DIR,
        METACHECK_CACHE_DIR, GROBID_CACHE_DIR, METACHECK_LOG_DIR,
        REVIEW_UNIDENTIFIED, REVIEW_SUPPLEMENTS, REVIEW_SUPPLEMENTS_PENDING, REVIEW_DUPLICATES, REVIEW_DUPLICATE_AMBIGUOUS,
        REVIEW_NEEDS_METADATA, REVIEW_INSUFFICIENT_TEXT,
        REVIEW_EVAL_FAILED, REVIEW_EVAL_ERROR, REVIEW_TECHNICAL_FAILURE,
        REVIEW_RECOVERY_PENDING,
        PENDING_PARENT_CHILD_DOCUMENTS,
        RECOVERED_DELETION_AUDIT, DELETION_RECOVERY_LOG_DIR,
        REVIEW_RESOLUTION_LOG_DIR, FILE_DISPOSITION_AUDIT_DIR,
    ]:
        d.mkdir(parents=True, exist_ok=True)


def _secret_key_status(env_name: str, enc_file: Path = ANTHROPIC_ENC_FILE) -> str:
    val = os.environ.get(env_name, "").strip()
    if val:
        if enc_file.is_file():
            return "set (unlocked or from env; vault on disk is encrypted)"
        return "set"
    if enc_file.is_file():
        return "encrypted on disk - unlock at RUN with master passphrase"
    return "NOT SET"


def _anthropic_key_status() -> str:
    if ANTHROPIC_API_KEY:
        if ANTHROPIC_ENC_FILE.is_file():
            return "set (unlocked or from env; vault on disk is encrypted)"
        if ANTHROPIC_KEY_FILE.is_file():
            return "set (legacy plaintext - run SETUP to encrypt)"
        return "set"
    if ANTHROPIC_ENC_FILE.is_file():
        return "encrypted on disk - unlock at RUN with master passphrase"
    if ANTHROPIC_KEY_FILE.is_file():
        return "legacy plaintext on disk - run SETUP to encrypt"
    return "NOT SET"


if __name__ == "__main__":
    print("PapersCorpus config")
    print("=" * 60)
    print(f"PROJECT_ROOT:   {PROJECT_ROOT}")
    print(f"SYSTEM_ROOT:    {SYSTEM_ROOT}")
    print(f"CORPUS_STORE:   {CORPUS_STORE}  exists={CORPUS_STORE.exists()}")
    print(f"DB_PATH:        {DB_PATH}  exists={DB_PATH.exists()}")
    print(f"SOURCE_PDFS:    {SOURCE_PDFS}  exists={SOURCE_PDFS.exists()}")
    print(f"STAGING:        {STAGING}  exists={STAGING.exists()}")
    print(f"REVIEW_DIR:     {REVIEW_DIR}  exists={REVIEW_DIR.exists()}")
    print(f"LOGS_DIR:       {LOGS_DIR}  exists={LOGS_DIR.exists()}")
    print(f"RUNS_DIR:       {RUNS_DIR}  exists={RUNS_DIR.exists()}")
    print(f"EVAL_MODEL_KEY: {EVAL_MODEL_KEY}")
    print(f"EVAL_MODEL:     {EVAL_MODEL}")
    print(f"EXPORT_DIR:     {EXPORT_DIR}  exists={EXPORT_DIR.exists()}")
    print(f"VECTOR_DB_DIR:  {VECTOR_DB_DIR}  exists={VECTOR_DB_DIR.exists()}")
    print()
    print("Metadata tier chain:")
    print(f"  {' -> '.join(METADATA_TIERS)}")
    print()
    print("External services:")
    print(f"  CORPUS_PROFILE:     {CORPUS_PROFILE_NAME} (api_mode={CORPUS_API_MODE})")
    print(f"  ANTHROPIC_API_KEY: {_anthropic_key_status()}")
    print(f"  DEEPSEEK_API_KEY:  {_secret_key_status('DEEPSEEK_API_KEY')}")
    print(f"  OPENROUTER_API_KEY:{_secret_key_status('OPENROUTER_API_KEY')}")
    print(f"  OLLAMA_URL:        {OLLAMA_URL}")
    print(f"  OLLAMA_MODEL:      {OLLAMA_MODEL}")
    print()
    print("Required binaries:")
    found = check_binaries(strict=False)
    for name, path in found.items():
        status = path if path else "MISSING — " + REQUIRED_BINARIES[name]
        print(f"  {name:12s} {status}")
