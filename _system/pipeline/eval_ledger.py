"""
Append-only job ledger for idempotent evaluation runs (JSONL, staging only).
No papers.db writes.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from eval_constants import EVAL_LEDGER_PATH

STATUSES = (
    "queued",
    "running",
    "done",
    "failed",
    "saved",
    "skipped",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_event(
    paper_id: str,
    status: str,
    *,
    pass_type: str = "",
    custom_id: str = "",
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    error: str = "",
    extra: dict | None = None,
) -> None:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    EVAL_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": _now(),
        "paper_id": paper_id,
        "status": status,
        "pass_type": pass_type,
        "custom_id": custom_id,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "error": error,
    }
    if extra:
        row["extra"] = extra
    with open(EVAL_LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def load_events() -> list[dict]:
    if not EVAL_LEDGER_PATH.exists():
        return []
    out = []
    for line in EVAL_LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def latest_status_by_paper() -> dict[str, str]:
    latest = {}
    for ev in load_events():
        pid = ev.get("paper_id")
        if pid:
            latest[pid] = ev.get("status", "")
    return latest


def is_saved(paper_id: str) -> bool:
    for ev in reversed(load_events()):
        if ev.get("paper_id") == paper_id and ev.get("status") == "saved":
            return True
    return False


def custom_id_already_done(custom_id: str) -> bool:
    for ev in load_events():
        if ev.get("custom_id") == custom_id and ev.get("status") in ("done", "saved"):
            return True
    return False
