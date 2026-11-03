"""TweetTracker FastAPI Core Application (`uvicorn tweettracker.api.main:app`)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hmac
import math
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from tweettracker import UPSTREAM_XALGO_COMMIT_SHA, __version__
from tweettracker.api.deps import (
    bearer_scheme,
    create_sse_ticket,
    extract_supplied_password,
    get_db_session,
    is_request_admin_authenticated,
    require_admin_auth,
)
from tweettracker.api.routes import (
    algo,
    algo_sync,
    backtest,
    network,
    radar,
    rising,
    workspaces,
)
from tweettracker.api.schemas import (
    AuthVerifyRequest,
    SecretsListOut,
    SecretsUpdateRequest,
)
from tweettracker.config import Settings, get_settings
from tweettracker.crypto import list_secret_statuses, upsert_secrets
from tweettracker.db import configure_database, dispose_database
from tweettracker.seed import bootstrap_database


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine, session_factory = configure_database(settings.database_url)
    await bootstrap_database(
        engine,
        session_factory,
        auto_migrate=settings.auto_migrate,
        seed_fixtures=settings.seed_fixtures,
    )
    try:
        yield
    finally:
        await dispose_database()


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title="TweetTracker API",
        version=__version__,
        description=(
            "Multi-workspace X community graph, xai-org/x-algorithm (9106e9f) tracer, "
            "15-60m breakout radar, and 48h self-calibrating backtester."
        ),
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _sanitize_json_safe(val: Any) -> Any:
        if isinstance(val, float):
            return str(val) if not math.isfinite(val) else val
        if isinstance(val, dict):
            return {str(k): _sanitize_json_safe(v) for k, v in val.items()}
        if isinstance(val, (list, tuple)):
            return [_sanitize_json_safe(v) for v in val]
        if isinstance(val, Exception):
            return str(val)
        return val

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": _sanitize_json_safe(exc.errors())},
        )

    @application.get("/api/health", tags=["system"])
    async def health_check(request: Request) -> dict[str, Any]:
        auth_req = bool(Settings.admin_password())
        body: dict[str, Any] = {
            "status": "ok",
            "version": __version__,
            "upstream_commit_sha": UPSTREAM_XALGO_COMMIT_SHA,
            "auth_required": auth_req,
        }
        if (not auth_req) or is_request_admin_authenticated(request):
            body["providers_configured"] = Settings.provider_env_status()
        return body

    @application.get("/api/auth/status", tags=["system"])
    async def auth_status(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
        x_admin_password: str | None = Header(default=None, alias="X-Admin-Password"),
    ) -> dict[str, bool]:
        expected = Settings.admin_password()
        if not expected:
            return {"auth_required": False, "authenticated": True}
        supplied = extract_supplied_password(credentials, x_admin_password)
        is_valid = bool(supplied) and hmac.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        )
        return {"auth_required": True, "authenticated": is_valid}

    @application.post("/api/auth/verify", tags=["system"])
    async def verify_auth(payload: AuthVerifyRequest) -> dict[str, bool]:
        expected = Settings.admin_password()
        if not expected:
            return {"auth_required": False, "authenticated": True}
        supplied = payload.password.strip()
        if not supplied or not hmac.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid admin password.",
            )
        return {"auth_required": True, "authenticated": True}

    @application.post(
        "/api/auth/sse-ticket",
        tags=["system"],
        dependencies=[Depends(require_admin_auth)],
    )
    async def issue_sse_ticket() -> dict[str, Any]:
        return create_sse_ticket()

    @application.get(
        "/api/settings/secrets",
        response_model=SecretsListOut,
        tags=["system"],
        dependencies=[Depends(require_admin_auth)],
    )
    async def get_managed_secrets(
        session: Any = Depends(get_db_session),
    ) -> dict[str, Any]:
        statuses = await list_secret_statuses(session)
        return {"secrets": statuses}

    @application.put(
        "/api/settings/secrets",
        response_model=SecretsListOut,
        tags=["system"],
        dependencies=[Depends(require_admin_auth)],
    )
    async def update_managed_secrets(
        payload: SecretsUpdateRequest,
        session: Any = Depends(get_db_session),
    ) -> dict[str, Any]:
        try:
            statuses = await upsert_secrets(session, payload.secrets)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        return {"secrets": statuses}

    application.include_router(workspaces.router)
    application.include_router(network.router)
    application.include_router(radar.router)
    application.include_router(backtest.router)
    application.include_router(rising.router)
    application.include_router(algo_sync.router)
    application.include_router(algo.router)
    return application


app = create_app()
