#!/usr/bin/env python3
"""Folder-local encrypted API secrets (AES-256-GCM + PBKDF2)."""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError as exc:  # pragma: no cover - setup installs cryptography
    raise SystemExit(
        "cryptography package required. Run SETUP.bat or: pip install cryptography"
    ) from exc

VAULT_VERSION = 1
KDF_ITERATIONS = 600_000
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32

SYSTEM_ROOT = Path(__file__).resolve().parent
SECRETS_DIR = SYSTEM_ROOT / "secrets"
ENC_FILE = SECRETS_DIR / "api_keys.env.enc"
LEGACY_FILE = SECRETS_DIR / "api_keys.env"

ALLOWED_SECRET_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    }
)


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        salt,
        KDF_ITERATIONS,
        dklen=KEY_BYTES,
    )


def _payload_secrets(secret_map: dict[str, str]) -> bytes:
    lines = []
    for key in sorted(secret_map):
        val = str(secret_map[key] or "").strip()
        if val:
            lines.append(f"{key}={val}")
    if not lines:
        raise ValueError("No secrets to encrypt")
    return "\n".join(lines).encode("utf-8")


def _parse_plaintext_secrets(plain: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in plain.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name in ALLOWED_SECRET_KEYS and value:
            out[name] = value
    return out


def validate_secrets(secret_map: dict[str, str], *, require_any: bool = True) -> None:
    if require_any and not secret_map:
        raise ValueError("At least one API key is required")
    ant = secret_map.get("ANTHROPIC_API_KEY", "")
    if ant and not ant.startswith("sk-ant-"):
        raise ValueError("ANTHROPIC_API_KEY must start with sk-ant-")
    ds = secret_map.get("DEEPSEEK_API_KEY", "")
    if ds and len(ds) < 8:
        raise ValueError("DEEPSEEK_API_KEY looks too short")
    oa = secret_map.get("OPENAI_API_KEY", "")
    if oa and not oa.startswith("sk-"):
        raise ValueError("OPENAI_API_KEY must start with sk-")
    or_key = secret_map.get("OPENROUTER_API_KEY", "")
    if or_key and not or_key.startswith("sk-or-"):
        raise ValueError("OPENROUTER_API_KEY must start with sk-or-")


def encrypt_secrets(secret_map: dict[str, str], passphrase: str) -> dict[str, Any]:
    validate_secrets(secret_map)
    if len(passphrase) < 8:
        raise ValueError("Passphrase must be at least 8 characters")

    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    key = _derive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, _payload_secrets(secret_map), None)
    return {
        "version": VAULT_VERSION,
        "kdf": "pbkdf2-sha256",
        "iterations": KDF_ITERATIONS,
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }


def decrypt_secrets(passphrase: str, path: Path | None = None) -> dict[str, str]:
    enc_path = path or ENC_FILE
    if not enc_path.is_file():
        raise FileNotFoundError(f"Encrypted secrets not found: {enc_path}")

    blob = json.loads(enc_path.read_text(encoding="utf-8"))
    if blob.get("version") != VAULT_VERSION:
        raise ValueError("Unsupported vault version")

    salt = base64.b64decode(blob["salt_b64"])
    nonce = base64.b64decode(blob["nonce_b64"])
    ciphertext = base64.b64decode(blob["ciphertext_b64"])
    iterations = int(blob.get("iterations", KDF_ITERATIONS))
    key = hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        salt,
        iterations,
        dklen=KEY_BYTES,
    )

    try:
        plain = AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception as exc:
        raise ValueError("Wrong passphrase or corrupted secrets file") from exc

    secret_map = _parse_plaintext_secrets(plain)
    if not secret_map:
        raise ValueError("Decrypted payload did not contain any API keys")
    return secret_map


def decrypt_api_key(passphrase: str, path: Path | None = None) -> str:
    """Backward-compatible: return Anthropic key only."""
    secret_map = decrypt_secrets(passphrase, path=path)
    value = secret_map.get("ANTHROPIC_API_KEY", "")
    if not value.startswith("sk-ant-"):
        raise ValueError("Decrypted payload did not contain a valid Anthropic API key")
    return value


def write_encrypted_secrets(secret_map: dict[str, str], passphrase: str) -> Path:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    data = encrypt_secrets(secret_map, passphrase)
    ENC_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _restrict_acl(ENC_FILE)
    _remove_legacy_plaintext()
    return ENC_FILE


def write_encrypted(api_key: str, passphrase: str) -> Path:
    return write_encrypted_secrets({"ANTHROPIC_API_KEY": api_key.strip()}, passphrase)


def _remove_legacy_plaintext() -> bool:
    if not LEGACY_FILE.is_file():
        return True
    try:
        if os.name == "nt":
            import stat
            import subprocess as sp

            username = os.environ.get("USERNAME", "")
            sp.run(["icacls", str(LEGACY_FILE), "/reset"], check=False, capture_output=True)
            if username:
                sp.run(
                    ["icacls", str(LEGACY_FILE), "/inheritance:r", "/grant:r", f"{username}:(F)"],
                    check=False,
                    capture_output=True,
                )
            try:
                LEGACY_FILE.chmod(stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
        try:
            LEGACY_FILE.write_text("# removed - keys are in api_keys.env.enc\n", encoding="utf-8")
        except OSError:
            pass
        LEGACY_FILE.unlink(missing_ok=True)
        return not LEGACY_FILE.is_file()
    except OSError as exc:
        print(
            f"WARNING: could not delete plaintext {LEGACY_FILE.name}: {exc}",
            file=sys.stderr,
        )
        return False


def _restrict_acl(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    try:
        import subprocess as sp

        grant = f"{os.environ.get('USERNAME', '')}:(F)"
        if grant.startswith(":"):
            return
        sp.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            check=False,
            capture_output=True,
        )
    except OSError:
        pass


def read_legacy_plaintext() -> dict[str, str]:
    if not LEGACY_FILE.is_file():
        return {}
    return _parse_plaintext_secrets(LEGACY_FILE.read_text(encoding="utf-8"))


def prompt_passphrase(*, confirm: bool = False) -> str:
    while True:
        p1 = getpass.getpass("Choose a master passphrase (min 8 chars, not stored in folder): ")
        if len(p1) < 8:
            print("Passphrase too short.", file=sys.stderr)
            continue
        if not confirm:
            return p1
        p2 = getpass.getpass("Confirm master passphrase: ")
        if p1 == p2:
            return p1
        print("Passphrases did not match.", file=sys.stderr)


def unlock_interactive() -> dict[str, str]:
    return decrypt_secrets(getpass.getpass("Enter secrets passphrase: "))


def _secrets_from_stdin() -> dict[str, str]:
    return _parse_plaintext_secrets(sys.stdin.read())


def cmd_encrypt_from_stdin() -> int:
    lines = sys.stdin.read().strip().splitlines()
    if not lines:
        print("ERROR: no input on stdin", file=sys.stderr)
        return 1
    if len(lines) == 1 and lines[0].startswith("sk-ant-"):
        secret_map = {"ANTHROPIC_API_KEY": lines[0].strip()}
    else:
        secret_map = _parse_plaintext_secrets("\n".join(lines))
    try:
        validate_secrets(secret_map)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    confirm = not ENC_FILE.is_file()
    passphrase = prompt_passphrase(confirm=confirm)
    write_encrypted_secrets(secret_map, passphrase)
    print(f"OK: wrote encrypted secrets to {ENC_FILE.relative_to(SYSTEM_ROOT.parent)}")
    return 0


def cmd_merge_secrets_stdin() -> int:
    if not ENC_FILE.is_file():
        print("ERROR: no vault to merge into; use encrypt-secrets-stdin first", file=sys.stderr)
        return 1
    incoming = _secrets_from_stdin()
    if not incoming:
        print("ERROR: no secrets on stdin", file=sys.stderr)
        return 1
    try:
        validate_secrets(incoming, require_any=True)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    passphrase = getpass.getpass("Enter secrets passphrase: ")
    try:
        merged = decrypt_secrets(passphrase)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    merged.update(incoming)
    validate_secrets(merged)
    write_encrypted_secrets(merged, passphrase)
    print(f"OK: merged secrets into {ENC_FILE.relative_to(SYSTEM_ROOT.parent)}")
    return 0


def cmd_run_unlocked() -> int:
    if len(sys.argv) < 3:
        print("Usage: secrets_vault.py run-unlocked <script.py> [args...]", file=sys.stderr)
        return 2
    script_rel = sys.argv[2]
    script_args = sys.argv[3:]
    script_path = (SYSTEM_ROOT / script_rel).resolve()
    if not script_path.is_file():
        print(f"ERROR: script not found: {script_path}", file=sys.stderr)
        return 1

    secret_map = unlock_interactive()
    env = os.environ.copy()
    for key, value in secret_map.items():
        env[key] = value
    cmd = [sys.executable, str(script_path), *script_args]
    return subprocess.call(cmd, env=env, cwd=str(SYSTEM_ROOT))


def cmd_migrate_legacy() -> int:
    existing = read_legacy_plaintext()
    if not existing:
        print("No plaintext api_keys.env to migrate.", file=sys.stderr)
        return 1
    if ENC_FILE.is_file():
        print("Encrypted secrets already exist; use merge-secrets-stdin to add keys.", file=sys.stderr)
        return 1
    passphrase = prompt_passphrase(confirm=True)
    write_encrypted_secrets(existing, passphrase)
    print("OK: migrated plaintext keys to api_keys.env.enc")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: secrets_vault.py encrypt-stdin | encrypt-secrets-stdin | merge-secrets-stdin | "
            "run-unlocked | migrate-legacy",
            file=sys.stderr,
        )
        return 2
    cmd = sys.argv[1]
    if cmd in ("encrypt-stdin", "encrypt-secrets-stdin"):
        return cmd_encrypt_from_stdin()
    if cmd == "merge-secrets-stdin":
        return cmd_merge_secrets_stdin()
    if cmd == "run-unlocked":
        return cmd_run_unlocked()
    if cmd == "migrate-legacy":
        return cmd_migrate_legacy()
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
