"""FastAPI routes for Automated Upstream xai-org/x-algorithm Sync Watcher."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
import httpx
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.algo import UPSTREAM_COMMIT_SHA
from tweettracker.algo_sync import (
    UPSTREAM_XALGO_REPO,
    WATCHED_RANKING_PATHS,
    activate_staged_param_version,
    check_upstream_algo_sync,
    get_last_synced_sha,
    resolve_github_token,
    resolve_target_github_repo,
    validate_git_ref,
)
from tweettracker.api.deps import get_db_session, require_admin_auth
from tweettracker.db import get_workspace_lock
from tweettracker.models import AlgoParamVersion, UpstreamAlgoEvent, Workspace

router = APIRouter(
    prefix="/api/algo-sync",
    tags=["algo-sync"],
    dependencies=[Depends(require_admin_auth)],
)


class AlgoSyncCheckRequest(BaseModel):
    simulate_drift: bool = False
    auto_create_issue: bool = True
    create_issue_on_simulate: bool = False
    llm_provider: str = "google_gemini"
    workspace_id: int | None = None
    target_ref: str = Field(default="main", min_length=1, max_length=120)

    @field_validator("target_ref")
    @classmethod
    def _validate_target_ref(cls, value: str) -> str:
        return validate_git_ref(value, field_name="target_ref")


class ActivateStagedRequest(BaseModel):
    workspace_id: int | None = None


def _serialize_param_version(ver: AlgoParamVersion | None) -> dict[str, Any] | None:
    if ver is None:
        return None
    return {
        "id": ver.id,
        "workspace_id": ver.workspace_id,
        "version_number": ver.version_number,
        "label": ver.label,
        "source": ver.source,
        "upstream_commit_sha": ver.upstream_commit_sha,
        "is_active": ver.is_active,
        "weights_json": ver.weights_json,
        "thresholds_json": ver.thresholds_json,
        "changelog_note": ver.changelog_note,
        "created_at": ver.created_at.isoformat() if ver.created_at else None,
    }


def _serialize_event(
    event: UpstreamAlgoEvent,
    staged_version: AlgoParamVersion | None = None,
) -> dict[str, Any]:
    return {
        "id": event.id,
        "base_sha": event.base_sha,
        "head_sha": event.head_sha,
        "detected_at": event.detected_at.isoformat() if event.detected_at else None,
        "watched_files_changed_json": event.watched_files_changed_json or [],
        "param_diffs_json": event.param_diffs_json or [],
        "structural_summary": event.structural_summary,
        "github_issue_number": event.github_issue_number,
        "github_issue_url": event.github_issue_url,
        "staged_param_version_id": event.staged_param_version_id,
        "staged_param_version": _serialize_param_version(staged_version),
        "status": event.status,
    }


@router.get("/status")
async def get_algo_sync_status(
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    # Status polling checks env configuration only — never spawns `gh auth token` subprocesses.
    token, auth_source = resolve_github_token(allow_gh_cli_fallback=False)
    last_synced = await get_last_synced_sha(session)

    events = list(
        (
            await session.execute(
                select(UpstreamAlgoEvent).order_by(UpstreamAlgoEvent.id.desc()).limit(25)
            )
        )
        .scalars()
        .all()
    )
    staged_ids = {e.staged_param_version_id for e in events if e.staged_param_version_id}
    versions_by_id: dict[int, AlgoParamVersion] = {}
    if staged_ids:
        rows = list(
            (
                await session.execute(
                    select(AlgoParamVersion).where(AlgoParamVersion.id.in_(staged_ids))
                )
            )
            .scalars()
            .all()
        )
        versions_by_id = {v.id: v for v in rows}

    return {
        "last_synced_sha": last_synced,
        "baseline_sha": UPSTREAM_COMMIT_SHA,
        "upstream_repo": UPSTREAM_XALGO_REPO,
        "target_github_repo": resolve_target_github_repo(),
        "github_token_configured": bool(token),
        "github_auth_source": auth_source,
        "llm_providers_configured": {
            "google_gemini": bool(os.environ.get("GOOGLE_AI_API_KEY", "").strip()),
            "xai_grok": bool(os.environ.get("XAI_API_KEY", "").strip()),
        },
        "watched_paths": list(WATCHED_RANKING_PATHS),
        "ignored_paths": ["ads/", ".github/", "ci/", "README.md", "Cargo.lock"],
        "events": [
            _serialize_event(
                e,
                versions_by_id.get(e.staged_param_version_id)
                if e.staged_param_version_id
                else None,
            )
            for e in events
        ],
    }


@router.post("/check")
async def trigger_algo_sync_check(
    payload: AlgoSyncCheckRequest | None = Body(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    req = payload or AlgoSyncCheckRequest()
    ws: Workspace | None = None
    if req.workspace_id is not None:
        ws = await session.get(Workspace, req.workspace_id)

    allow_cli = "PYTEST_CURRENT_TEST" not in os.environ
    try:
        outcome = await check_upstream_algo_sync(
            session,
            target_ref=req.target_ref,
            simulate_drift=req.simulate_drift,
            auto_create_issue=req.auto_create_issue,
            create_issue_on_simulate=req.create_issue_on_simulate,
            allow_gh_cli_fallback=allow_cli,
            llm_provider_name=(ws.llm_provider if ws else req.llm_provider),
            workspace=ws,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Upstream GitHub API request failed.",
        ) from exc

    last_synced = await get_last_synced_sha(session)
    return {
        "last_synced_sha": last_synced,
        "deduplicated": outcome.deduplicated,
        "event": _serialize_event(outcome.event, outcome.staged_version),
        "staged_param_version": _serialize_param_version(outcome.staged_version),
    }


@router.post("/events/{event_id}/activate-staged")
async def activate_event_staged_weights(
    event_id: int,
    payload: ActivateStagedRequest | None = Body(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    req = payload or ActivateStagedRequest()
    try:
        if req.workspace_id is not None:
            async with get_workspace_lock(req.workspace_id):
                event, activated = await activate_staged_param_version(
                    session,
                    event_id=event_id,
                    workspace_id=req.workspace_id,
                )
        else:
            event, activated = await activate_staged_param_version(
                session,
                event_id=event_id,
                workspace_id=None,
            )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    last_synced = await get_last_synced_sha(session)
    return {
        "last_synced_sha": last_synced,
        "event": _serialize_event(event, activated),
        "activated_param_version": _serialize_param_version(activated),
    }
