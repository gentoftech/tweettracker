"""Encrypted-at-rest secret vault for in-app API key configuration (`app_secrets`)."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.models import AppSecret

logger = logging.getLogger("tweettracker.crypto")

DEFAULT_VAULT_KEY_PATH = Path(__file__).resolve().parents[2] / "data" / ".vault.key"
_KDF_SALT = b"tweettracker-app-secrets-v1-salt"

MANAGED_SECRET_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        "TWEETAPI_API_KEY",
        "TweetAPI.com API Key",
        "Required for live X profiles, Hot Posts polling, mutual checks, and social graph crawling.",
    ),
    (
        "GOOGLE_AI_API_KEY",
        "Google Gemini API Key",
        "Used for Gemini 3.8 Flash summaries when upstream x-algorithm Rust ranking code changes.",
    ),
    (
        "XAI_API_KEY",
        "xAI Grok API Key",
        "Optional Grok 4.7 alternative for upstream algorithm diff summaries.",
    ),
    (
        "GITHUB_TOKEN",
        "GitHub Token",
        "Optional fine-grained PAT (issues:write) to auto-open algo-sync issues in cloud hosting.",
    ),
    (
        "TWEETTRACKER_WEBHOOK_SECRET",
        "Webhook Signing Secret",
        "Optional HMAC-SHA256 secret used to sign outbound breakout webhook payloads.",
    ),
)

ALLOWED_SECRET_KEYS: frozenset[str] = frozenset(spec[0] for spec in MANAGED_SECRET_SPECS)

# Process-local runtime cache hydrated from `app_secrets` (takes precedence over os.environ)
_RUNTIME_SECRETS: dict[str, str] = {}


def mask_secret_value(raw: str) -> str:
    """Returns a non-reversible masked preview such as `••••••••4f9a`."""
    cleaned = raw.strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 6:
        return "••••••••"
    return f"••••••••{cleaned[-4:]}"


def _derive_fernet_from_passphrase(passphrase: str) -> Fernet:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_KDF_SALT,
        iterations=200_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
    return Fernet(key)


def _get_or_create_local_vault_key(vault_path: Path = DEFAULT_VAULT_KEY_PATH) -> bytes:
    env_override = os.environ.get("TWEETTRACKER_VAULT_FILE", "").strip()
    target_path = Path(env_override) if env_override else vault_path
    if target_path.exists():
        raw = target_path.read_bytes().strip()
        if raw:
            return raw

    target_path.parent.mkdir(parents=True, exist_ok=True)
    generated = Fernet.generate_key()
    target_path.write_bytes(generated)
    try:
        os.chmod(target_path, 0o600)
    except OSError:
        pass
    return generated


def get_fernet() -> Fernet:
    """Resolves the active Fernet cipher from TWEETTRACKER_SECRET_KEY, TWEETTRACKER_ADMIN_PASSWORD,
    or a persistent local `.vault.key` file under `backend/data/`.
    """
    explicit_master = os.environ.get("TWEETTRACKER_SECRET_KEY", "").strip()
    if explicit_master:
        return _derive_fernet_from_passphrase(explicit_master)

    admin_pw = os.environ.get("TWEETTRACKER_ADMIN_PASSWORD", "").strip()
    if admin_pw:
        return _derive_fernet_from_passphrase(admin_pw)

    return Fernet(_get_or_create_local_vault_key())


def encrypt_secret(plaintext: str) -> str:
    cipher = get_fernet()
    return cipher.encrypt(plaintext.strip().encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    cipher = get_fernet()
    return cipher.decrypt(ciphertext.encode("ascii")).decode("utf-8")


def clear_runtime_secrets() -> None:
    """Clears the in-memory decrypted secret cache (used on DB bootstrap / test teardown)."""
    _RUNTIME_SECRETS.clear()


def resolve_secret(key_name: str) -> str:
    """Returns decrypted in-app secret if configured, else falls back to `os.environ`."""
    cached = _RUNTIME_SECRETS.get(key_name, "").strip()
    if cached:
        return cached
    return os.environ.get(key_name, "").strip()


def resolve_secret_with_source(key_name: str) -> tuple[str, str]:
    """Returns `(value, source)` where source is `'app'`, `'env'`, or `'none'`."""
    cached = _RUNTIME_SECRETS.get(key_name, "").strip()
    if cached:
        return cached, "app"
    env_val = os.environ.get(key_name, "").strip()
    if env_val:
        return env_val, "env"
    return "", "none"


async def refresh_runtime_secrets(session: AsyncSession) -> dict[str, str]:
    """Reloads all encrypted `AppSecret` rows from DB into the in-memory runtime cache."""
    rows = list((await session.execute(select(AppSecret))).scalars().all())
    loaded: dict[str, str] = {}
    for row in rows:
        if row.key_name not in ALLOWED_SECRET_KEYS:
            continue
        try:
            val = decrypt_secret(row.encrypted_value).strip()
            if val:
                loaded[row.key_name] = val
        except (InvalidToken, ValueError):
            logger.warning(
                "Could not decrypt stored secret '%s' (encryption key changed); falling back to env.",
                row.key_name,
            )

    _RUNTIME_SECRETS.clear()
    _RUNTIME_SECRETS.update(loaded)
    return dict(_RUNTIME_SECRETS)


async def list_secret_statuses(session: AsyncSession) -> list[dict[str, Any]]:
    """Returns safe masked metadata for every managed API key without exposing raw values."""
    await refresh_runtime_secrets(session)
    rows_by_key = {
        r.key_name: r
        for r in (await session.execute(select(AppSecret))).scalars().all()
    }

    statuses: list[dict[str, Any]] = []
    for key_name, label, description in MANAGED_SECRET_SPECS:
        app_val = _RUNTIME_SECRETS.get(key_name, "").strip()
        env_val = os.environ.get(key_name, "").strip()
        row = rows_by_key.get(key_name)

        if app_val:
            source = "app"
            configured = True
            hint = (row.masked_hint if row and row.masked_hint else mask_secret_value(app_val))
            updated_at = row.updated_at if row else None
        elif env_val:
            source = "env"
            configured = True
            hint = mask_secret_value(env_val)
            updated_at = None
        else:
            source = "none"
            configured = False
            hint = ""
            updated_at = None

        statuses.append(
            {
                "key_name": key_name,
                "label": label,
                "description": description,
                "configured": configured,
                "source": source,
                "masked_hint": hint,
                "has_env_fallback": bool(env_val),
                "updated_at": updated_at,
            }
        )
    return statuses


async def upsert_secrets(
    session: AsyncSession,
    updates: dict[str, str | None],
) -> list[dict[str, Any]]:
    """Encrypts and persists non-empty keys; deletes keys explicitly passed as `""`."""
    now = datetime.now(timezone.utc)
    for raw_key, raw_val in updates.items():
        key_name = raw_key.strip()
        if key_name not in ALLOWED_SECRET_KEYS:
            raise ValueError(f"Unsupported secret key '{key_name}'.")
        if raw_val is None:
            continue

        cleaned = raw_val.strip()
        if cleaned == "":
            # Empty string clears the in-app override (reverting to env var if present)
            await session.execute(delete(AppSecret).where(AppSecret.key_name == key_name))
            _RUNTIME_SECRETS.pop(key_name, None)
        else:
            enc = encrypt_secret(cleaned)
            hint = mask_secret_value(cleaned)
            existing = await session.get(AppSecret, key_name)
            if existing is None:
                session.add(
                    AppSecret(
                        key_name=key_name,
                        encrypted_value=enc,
                        masked_hint=hint,
                        updated_at=now,
                    )
                )
            else:
                existing.encrypted_value = enc
                existing.masked_hint = hint
                existing.updated_at = now
            _RUNTIME_SECRETS[key_name] = cleaned

    await session.commit()
    return await list_secret_statuses(session)


def generate_random_secret_hex(nbytes: int = 24) -> str:
    return secrets.token_hex(nbytes)
