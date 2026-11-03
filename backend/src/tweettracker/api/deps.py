"""Authentication and database session dependencies for FastAPI routes."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from tweettracker.config import Settings
from tweettracker.db import get_db_session

bearer_scheme = HTTPBearer(auto_error=False)
SSE_TICKET_TTL_SECONDS = 120


def extract_supplied_password(
    credentials: HTTPAuthorizationCredentials | None,
    x_admin_password: str | None,
) -> str:
    if credentials and credentials.scheme.lower() == "bearer" and credentials.credentials:
        return credentials.credentials.strip()
    if x_admin_password:
        return x_admin_password.strip()
    return ""


def is_request_admin_authenticated(request: Request) -> bool:
    expected = Settings.admin_password()
    if not expected:
        return True
    auth_hdr = (request.headers.get("authorization") or "").strip()
    supplied = ""
    if auth_hdr.lower().startswith("bearer "):
        supplied = auth_hdr[7:].strip()
    elif request.headers.get("x-admin-password"):
        supplied = (request.headers.get("x-admin-password") or "").strip()
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def create_sse_ticket(
    ttl_seconds: int = SSE_TICKET_TTL_SECONDS,
    *,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Creates a short-lived HMAC-SHA256 signed SSE ticket (`exp:nonce:sig`) keyed
    by `TWEETTRACKER_ADMIN_PASSWORD` so raw admin passwords are never passed in URLs.
    """
    ttl = max(5, min(int(ttl_seconds), 600))
    base_ts = int(time.time()) if now_ts is None else int(now_ts)
    exp = base_ts + ttl
    nonce = secrets.token_hex(12)
    secret = Settings.admin_password()
    if not secret:
        return {
            "ticket": "",
            "expires_at": exp,
            "ttl_seconds": ttl,
            "auth_required": False,
        }
    signing_key = hmac.new(
        secret.encode("utf-8"),
        b"tweettracker-sse-ticket-v1",
        hashlib.sha256,
    ).digest()
    msg = f"{exp}:{nonce}".encode("utf-8")
    sig = hmac.new(signing_key, msg, hashlib.sha256).hexdigest()
    return {
        "ticket": f"{exp}:{nonce}:{sig}",
        "expires_at": exp,
        "ttl_seconds": ttl,
        "auth_required": True,
    }


def verify_sse_ticket(ticket: str | None, *, now_ts: float | None = None) -> bool:
    """Verifies an `exp:nonce:sig` HMAC-SHA256 SSE ticket in constant time."""
    secret = Settings.admin_password()
    if not secret:
        return True
    if not ticket or not isinstance(ticket, str):
        return False
    parts = ticket.strip().split(":")
    if len(parts) != 3:
        return False
    exp_str, nonce, supplied_sig = parts
    if not exp_str.isdigit() or not nonce or len(nonce) > 64 or not supplied_sig:
        return False
    exp = int(exp_str)
    cur = float(time.time() if now_ts is None else now_ts)
    if cur > float(exp):
        return False
    signing_key = hmac.new(
        secret.encode("utf-8"),
        b"tweettracker-sse-ticket-v1",
        hashlib.sha256,
    ).digest()
    expected_sig = hmac.new(
        signing_key,
        f"{exp}:{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(supplied_sig.encode("utf-8"), expected_sig.encode("utf-8"))


async def require_admin_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_admin_password: str | None = Header(default=None, alias="X-Admin-Password"),
) -> None:
    """Unlocks automatically in local dev when TWEETTRACKER_ADMIN_PASSWORD is unset;
    enforces constant-time bearer/header password check when set.
    """
    expected = Settings.admin_password()
    if not expected:
        return

    supplied = extract_supplied_password(credentials, x_admin_password)
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin password required or invalid.",
            headers={"WWW-Authenticate": "Bearer"},
        )


__all__ = [
    "SSE_TICKET_TTL_SECONDS",
    "create_sse_ticket",
    "extract_supplied_password",
    "get_db_session",
    "is_request_admin_authenticated",
    "require_admin_auth",
    "verify_sse_ticket",
]
