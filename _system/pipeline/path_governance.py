"""Central Windows-safe path and filename policy for corpus files.

All pipeline code that creates, copies, moves, or persists corpus-managed file
paths should route filenames through this module before touching the filesystem.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

MAX_FULL_PATH = 240
MAX_FILENAME = 120
MAX_STEM = 100
HASH_SUFFIX_LEN = 8

SAFE_CHARS_RX = re.compile(r"[^a-z0-9_.-]+")
REPEATED_UNDERSCORE_RX = re.compile(r"_+")
TIMESTAMP_CHAIN_RX = re.compile(r"(?:[_-](?:\d{8}-\d{6}|\d{10,}|\d{7,})){1,}$")
RETRY_SUFFIX_CHAIN_RX = re.compile(r"(?:[_-](?:retry|copy|duplicate|recovered|staging))*$", re.I)
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def stable_token(*parts: Any, length: int = HASH_SUFFIX_LEN) -> str:
    h = hashlib.sha256()
    for part in parts:
        if part is None:
            continue
        if isinstance(part, bytes):
            data = part
        else:
            data = str(part).encode("utf-8", errors="replace")
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()[:length]


def file_token(path: Path, *, length: int = HASH_SUFFIX_LEN) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:length]


def normalize_extension(extension: str | None, fallback: str = ".pdf") -> str:
    ext = (extension or fallback or "").strip().lower()
    if not ext:
        return ""
    if not ext:
        return ""
    if not ext.startswith("."):
        ext = "." + ext
    ext = SAFE_CHARS_RX.sub("", ext)
    return ext[:20] or fallback


def strip_uncontrolled_suffixes(stem: str) -> str:
    previous = None
    current = stem
    while previous != current:
        previous = current
        current = TIMESTAMP_CHAIN_RX.sub("", current)
        current = re.sub(r"(?:[_-]\d{1,4}){3,}$", "", current)
    current = RETRY_SUFFIX_CHAIN_RX.sub("", current)
    return current.strip("._- ")


def sanitize_filename_component(value: Any, *, max_len: int | None = None, fallback: str = "file") -> str:
    text = str(value or "").strip().lower()
    text = text.replace("&", " and ")
    text = text.replace("+", " plus ")
    text = re.sub(r"[\\/:\*\?\"<>\|\x00-\x1f]+", "_", text)
    text = SAFE_CHARS_RX.sub("_", text)
    text = REPEATED_UNDERSCORE_RX.sub("_", text)
    text = strip_uncontrolled_suffixes(text)
    text = text.strip("._- ")
    if not text:
        text = fallback
    if text.split(".", 1)[0] in WINDOWS_RESERVED_NAMES:
        text = f"{text}_file"
    if max_len is not None and len(text) > max_len:
        text = text[:max_len].rstrip("._- ")
    return text or fallback


def _author_token(authors: Any) -> str:
    if isinstance(authors, str):
        authors = [authors]
    if not authors:
        return "unknown"
    first = str(authors[0] or "").strip()
    surname = first.split()[-1] if first.split() else first
    token = sanitize_filename_component(surname, max_len=24, fallback="unknown")
    return token if token and "unknown" not in token else "unknown"


def _year_token(year: Any) -> str:
    m = re.search(r"(?:19|20)\d{2}", str(year or ""))
    return m.group(0) if m else "unknown"


def _title_token(title: Any, *, max_len: int = 58) -> str:
    stop = {"the", "and", "for", "with", "from", "that", "this", "into", "onto", "pii", "doi"}
    words = []
    for raw in re.split(r"\s+", str(title or "")):
        word = sanitize_filename_component(raw, fallback="")
        if len(word) <= 2 or word in stop:
            continue
        words.append(word)
        if len("_".join(words)) >= max_len:
            break
    return sanitize_filename_component("_".join(words), max_len=max_len, fallback="")


def build_safe_stem(
    *,
    authors: Any = None,
    year: Any = None,
    title: Any = None,
    fallback: Any = None,
    hash_source: Any = None,
    max_stem: int = MAX_STEM,
    include_hash: bool = True,
) -> str:
    author = _author_token(authors)
    year_s = _year_token(year)
    title_s = _title_token(title)
    parts = [author, year_s]
    if title_s:
        parts.append(title_s)
    elif fallback:
        parts.append(sanitize_filename_component(Path(str(fallback)).stem, max_len=48, fallback="file"))
    stem = sanitize_filename_component("_".join(p for p in parts if p), max_len=max_stem, fallback="file")
    if include_hash:
        token = stable_token(author, year_s, title_s, fallback, hash_source)
        room = max_stem - len(token) - 1
        stem = f"{stem[:room].rstrip('._-')}_{token}" if room > 0 else token
    return sanitize_filename_component(stem, max_len=max_stem, fallback="file")


def make_safe_filename(
    *,
    authors: Any = None,
    year: Any = None,
    title: Any = None,
    original_filename: str | None = None,
    extension: str | None = None,
    hash_source: Any = None,
    max_filename: int = MAX_FILENAME,
    max_stem: int = MAX_STEM,
    include_hash: bool = True,
) -> str:
    ext = normalize_extension(extension or Path(str(original_filename or "")).suffix or ".pdf")
    stem_limit = min(max_stem, max_filename - len(ext))
    stem = build_safe_stem(
        authors=authors,
        year=year,
        title=title,
        fallback=original_filename,
        hash_source=hash_source,
        max_stem=max(12, stem_limit),
        include_hash=include_hash,
    )
    return sanitize_filename_component(stem, max_len=max(12, stem_limit), fallback="file") + ext


def shorten_existing_filename(
    filename: str,
    *,
    authors: Any = None,
    year: Any = None,
    title: Any = None,
    hash_source: Any = None,
    max_filename: int = MAX_FILENAME,
) -> str:
    path = Path(filename)
    return make_safe_filename(
        authors=authors,
        year=year,
        title=title,
        original_filename=path.name,
        extension=path.suffix,
        hash_source=hash_source or path.name,
        max_filename=max_filename,
        include_hash=True,
    )


def validate_path_policy(path: Path | str, *, max_full_path: int = MAX_FULL_PATH, max_filename: int = MAX_FILENAME) -> dict[str, Any]:
    p = Path(path)
    name = p.name
    stem = p.stem
    violations: list[str] = []
    if len(str(p)) > max_full_path:
        violations.append("full_path_too_long")
    if len(name) > max_filename:
        violations.append("filename_too_long")
    if len(stem) > MAX_STEM:
        violations.append("stem_too_long")
    if name != name.strip(" ."):
        violations.append("trailing_space_or_dot")
    if any(sep in name for sep in ("/", "\\")):
        violations.append("separator_in_filename")
    if sanitize_filename_component(stem, fallback="") != stem.lower().strip(" ."):
        violations.append("illegal_or_non_normalized_chars")
    if stem.lower().split(".", 1)[0] in WINDOWS_RESERVED_NAMES:
        violations.append("windows_reserved_name")
    if TIMESTAMP_CHAIN_RX.search(stem) or re.search(r"(?:[_-]\d{7,}){2,}$", stem):
        violations.append("timestamp_suffix_chain")
    return {
        "ok": not violations,
        "violations": violations,
        "full_path_length": len(str(p)),
        "filename_length": len(name),
        "path": str(p),
    }


def classify_path_violation(path: Path | str, *, max_full_path: int = MAX_FULL_PATH, max_filename: int = MAX_FILENAME) -> str:
    check = validate_path_policy(path, max_full_path=max_full_path, max_filename=max_filename)
    if check["ok"]:
        return "none"
    return ",".join(check["violations"])


def _fit_filename_to_path(directory: Path, filename: str, *, max_full_path: int, max_filename: int) -> str:
    ext = normalize_extension(Path(filename).suffix, fallback="")
    stem = sanitize_filename_component(Path(filename).stem, max_len=max_filename - len(ext), fallback="file")
    allowed_by_path = max_full_path - len(str(Path(directory) / (ext or "")))
    stem_limit = max(8, min(len(stem), max_filename - len(ext), allowed_by_path))
    if len(stem) > stem_limit:
        token = stable_token(stem, length=HASH_SUFFIX_LEN)
        room = max(1, stem_limit - len(token) - 1)
        stem = f"{stem[:room].rstrip('._-')}_{token}"
    return sanitize_filename_component(stem, max_len=stem_limit, fallback="file") + ext


def reserve_unique_path(
    directory: Path | str,
    desired_filename: str,
    *,
    content_token: str | None = None,
    max_full_path: int = MAX_FULL_PATH,
    max_filename: int = MAX_FILENAME,
    existing_ok: Path | None = None,
) -> Path:
    directory = Path(directory)
    ext = normalize_extension(Path(desired_filename).suffix, fallback="")
    stem = sanitize_filename_component(Path(desired_filename).stem, max_len=max_filename - len(ext), fallback="file")
    base = _fit_filename_to_path(directory, stem + ext, max_full_path=max_full_path, max_filename=max_filename)
    candidate = directory / base
    if existing_ok and candidate.resolve() == Path(existing_ok).resolve():
        return candidate
    if not candidate.exists():
        return candidate
    token = sanitize_filename_component(content_token or stable_token(desired_filename), max_len=16, fallback="file")
    base_stem = sanitize_filename_component(Path(base).stem, max_len=max_filename - len(ext) - len(token) - 1, fallback="file")
    candidate_name = _fit_filename_to_path(directory, f"{base_stem}_{token}{ext}", max_full_path=max_full_path, max_filename=max_filename)
    candidate = directory / candidate_name
    if existing_ok and candidate.resolve() == Path(existing_ok).resolve():
        return candidate
    if not candidate.exists():
        return candidate
    for counter in range(2, 1000):
        suffix = f"{token}_{counter}"
        stem_limit = max_filename - len(ext) - len(suffix) - 1
        retry_stem = sanitize_filename_component(base_stem, max_len=max(8, stem_limit), fallback="file")
        candidate_name = _fit_filename_to_path(directory, f"{retry_stem}_{suffix}{ext}", max_full_path=max_full_path, max_filename=max_filename)
        candidate = directory / candidate_name
        if existing_ok and candidate.resolve() == Path(existing_ok).resolve():
            return candidate
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not reserve unique path for {desired_filename!r} in {directory}")


def safe_destination_for_file(
    source: Path | str,
    directory: Path | str,
    *,
    reason: str = "",
    desired_filename: str | None = None,
    max_full_path: int = MAX_FULL_PATH,
    max_filename: int = MAX_FILENAME,
) -> Path:
    source = Path(source)
    try:
        token = file_token(source)
    except OSError:
        token = stable_token(source.name, reason)
    desired = desired_filename or shorten_existing_filename(
        source.name,
        title=Path(source).stem,
        hash_source=f"{token}:{reason}",
        max_filename=max_filename,
    )
    return reserve_unique_path(directory, desired, content_token=token, max_full_path=max_full_path, max_filename=max_filename)


def paired_sidecar_paths(path: Path | str) -> list[Path]:
    p = Path(path)
    candidates = [
        p.with_suffix(p.suffix + ".meta.json"),
        p.with_suffix(p.suffix + ".review-retry.json"),
        p.with_suffix(p.suffix + ".reason.json"),
    ]
    try:
        import config  # type: ignore

        candidates.append(config.STAGING_METADATA / f"{p.name}.review-retry.json")
    except Exception:
        pass
    seen: set[str] = set()
    out: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def _update_sidecar_payload(path: Path, old: Path, new: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    changed = False
    for key in ("filename", "path", "original_path"):
        value = data.get(key)
        if value == old.name:
            data[key] = new.name
            changed = True
        elif value == str(old):
            data[key] = str(new)
            changed = True
    if changed:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def safe_runtime_rename(
    source: Path | str,
    destination: Path | str,
    *,
    sidecars: list[Path] | None = None,
    update_sidecar_json: bool = True,
) -> dict[str, Any]:
    source = Path(source)
    destination = Path(destination)
    before_hash = file_token(source) if source.exists() and source.is_file() else None
    if not validate_path_policy(destination)["ok"]:
        raise ValueError(f"destination violates path policy: {destination}")
    sidecars = [p for p in (sidecars if sidecars is not None else paired_sidecar_paths(source)) if p.exists()]
    moves: list[tuple[Path, Path]] = []
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != destination.resolve():
            if destination.exists():
                raise FileExistsError(destination)
            shutil.move(str(source), str(destination))
            moves.append((destination, source))
        moved_sidecars = []
        for sidecar in sidecars:
            if sidecar.parent.name == "staging-metadata":
                target = sidecar.parent / sidecar.name.replace(source.name, destination.name, 1)
            else:
                suffix = sidecar.name[len(source.name):] if sidecar.name.startswith(source.name) else "".join(sidecar.suffixes[-2:])
                target = destination.parent / f"{destination.name}{suffix}"
            target = reserve_unique_path(target.parent, target.name, content_token=stable_token(sidecar, destination), max_filename=MAX_FILENAME)
            if sidecar.resolve() != target.resolve():
                shutil.move(str(sidecar), str(target))
                moves.append((target, sidecar))
            if update_sidecar_json:
                _update_sidecar_payload(target, source, destination)
            moved_sidecars.append({"old": str(sidecar), "new": str(target)})
        after_hash = file_token(destination) if destination.exists() and destination.is_file() else None
        if before_hash and after_hash and before_hash != after_hash:
            raise RuntimeError("file hash changed during rename")
        return {"ok": True, "old": str(source), "new": str(destination), "sidecars": moved_sidecars, "sha256_8": after_hash}
    except Exception:
        for moved, original in reversed(moves):
            if moved.exists() and not original.exists():
                shutil.move(str(moved), str(original))
        raise


def active_ingest_lock_present(lock_path: Path | str) -> bool:
    lock = Path(lock_path)
    if not lock.exists():
        return False
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
        pid = int(data.get("pid") or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return True
    if not pid:
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
