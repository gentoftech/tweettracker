"""FastAPI routes for Epic 3 48h Scorecard, Regularized Auto-Tuner, Changelog & Backtester."""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.api.deps import get_db_session, require_admin_auth
from tweettracker.backtest import (
    compute_workspace_performance,
    compute_workspace_scorecard,
    grade_workspace_outcomes,
    list_workspace_changelog,
    promote_candidate_params,
    rollback_to_version,
    run_auto_calibration,
    run_backtest_replay,
)
from tweettracker.db import get_workspace_lock
from tweettracker.models import Workspace

router = APIRouter(
    prefix="/api/workspaces",
    tags=["backtest"],
    dependencies=[Depends(require_admin_auth)],
)


async def _ensure_workspace_exists(session: AsyncSession, workspace_id: int) -> Workspace:
    ws = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one_or_none()
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {workspace_id} not found.",
        )
    return ws


def _assert_finite_tree(value: Any, path: str = "payload") -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Non-finite float in '{path}' is not permitted.")
    elif isinstance(value, Mapping):
        for k, v in value.items():
            _assert_finite_tree(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            _assert_finite_tree(item, f"{path}[{idx}]")


class ScorecardGradeRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    breakout_multiple: float | None = Field(default=None, ge=1.0, le=50.0)
    community_floor_views: int | None = Field(default=None, ge=0, le=10_000_000)
    community_floors: dict[str, int] | None = None


class CalibrateRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    min_matured_posts: int = Field(default=30, ge=5, le=10_000)
    l2_reg: float = Field(default=0.25, ge=0.001, le=50.0)
    max_drift_pct: float = Field(default=0.20, ge=0.01, le=0.20)
    force_stage_only: bool = False


class ActivateVersionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=400)


class CandidateParamsPayload(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    overrides: dict[str, Any] = Field(default_factory=dict)
    weights: dict[str, Any] | None = None
    surrogate_params: dict[str, Any] | None = None
    pipeline: dict[str, Any] | None = None
    fusion: dict[str, Any] | None = None
    thresholds: dict[str, Any] | None = None

    watch_threshold: float | None = Field(default=None, ge=1.0, le=99.0)
    surge_threshold: float | None = Field(default=None, ge=2.0, le=100.0)
    breakout_multiple: float | None = Field(default=None, ge=1.0, le=50.0)
    community_floor_views: int | None = Field(default=None, ge=0, le=10_000_000)

    bidirectional_follow_reply_weight_boost: float | None = Field(
        default=None, ge=-1_000.0, le=1_000.0
    )
    share_via_copy_link: float | None = Field(default=None, ge=-1_000.0, le=1_000.0)
    quote: float | None = Field(default=None, ge=-1_000.0, le=1_000.0)
    reply: float | None = Field(default=None, ge=-1_000.0, le=1_000.0)
    not_interested: float | None = Field(default=None, ge=-1_000.0, le=1_000.0)
    dpp_theta: float | None = Field(default=None, ge=0.0, le=1.0)
    oon_weight_factor: float | None = Field(default=None, ge=0.0, le=2.0)
    prior_views_alpha0: float | None = Field(default=None, ge=1.0, le=10_000.0)
    dwell_max_secs: float | None = Field(default=None, ge=0.5, le=3_600.0)
    copy_link_from_bookmark_mult: float | None = Field(default=None, ge=0.0, le=2.0)
    url_dwell_discount: float | None = Field(default=None, ge=0.05, le=1.5)

    @model_validator(mode="after")
    def _validate_candidate_params(self) -> CandidateParamsPayload:
        merged = self.merged_overrides()
        _assert_finite_tree(merged, "overrides")

        eff_watch = merged.get("watch_threshold")
        eff_surge = merged.get("surge_threshold")
        if isinstance(merged.get("thresholds"), Mapping):
            th_map = merged["thresholds"]
            if eff_watch is None:
                eff_watch = th_map.get("watch_threshold")
            if eff_surge is None:
                eff_surge = th_map.get("surge_threshold")
        if eff_watch is not None and not (1.0 <= float(eff_watch) <= 99.0):
            raise ValueError("watch_threshold must be between 1.0 and 99.0.")
        if eff_surge is not None and not (2.0 <= float(eff_surge) <= 100.0):
            raise ValueError("surge_threshold must be between 2.0 and 100.0.")
        if (
            eff_watch is not None
            and eff_surge is not None
            and float(eff_watch) >= float(eff_surge)
        ):
            raise ValueError("watch_threshold must be strictly less than surge_threshold.")

        for sur_key in ("surrogate", "surrogate_params"):
            sur_map = merged.get(sur_key)
            if isinstance(sur_map, Mapping):
                if "dwell_max_secs" in sur_map and sur_map["dwell_max_secs"] is not None:
                    if not (0.5 <= float(sur_map["dwell_max_secs"]) <= 3_600.0):
                        raise ValueError("dwell_max_secs must be between 0.5 and 3600.0.")
                if "prior_views_alpha0" in sur_map and sur_map["prior_views_alpha0"] is not None:
                    if not (1.0 <= float(sur_map["prior_views_alpha0"]) <= 10_000.0):
                        raise ValueError("prior_views_alpha0 must be between 1.0 and 10000.0.")

        w_map = merged.get("weights")
        if isinstance(w_map, Mapping):
            for wk, wv in w_map.items():
                if isinstance(wv, (int, float)) and not isinstance(wv, bool):
                    if not (-1_000.0 <= float(wv) <= 1_000.0):
                        raise ValueError(
                            f"Weight override '{wk}' must be within [-1000.0, 1000.0]."
                        )

        return self

    def merged_overrides(self) -> dict[str, Any]:
        merged: dict[str, Any] = dict(self.overrides or {})
        raw = self.model_dump(exclude_none=True)
        raw.pop("overrides", None)
        raw.pop("benchmark_10k", None)
        raw.pop("benchmark_size", None)
        raw.pop("label", None)
        raw.pop("changelog_note", None)
        for k, v in raw.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
        return merged


class BacktestReplayRequest(CandidateParamsPayload):
    benchmark_10k: bool = False
    benchmark_size: int = Field(default=10_000, ge=100, le=50_000)


class BacktestPromoteRequest(CandidateParamsPayload):
    label: str | None = Field(default=None, max_length=160)
    changelog_note: str | None = Field(default=None, max_length=1_000)


@router.get("/{workspace_id}/scorecard")
async def get_scorecard(
    workspace_id: int,
    breakout_multiple: float | None = Query(default=None, ge=1.0, le=50.0),
    community_floor_views: int | None = Query(default=None, ge=0, le=10_000_000),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    async with get_workspace_lock(workspace_id):
        await _ensure_workspace_exists(session, workspace_id)
        return await compute_workspace_scorecard(
            session,
            workspace_id,
            auto_grade=True,
            breakout_multiple=breakout_multiple,
            community_floor_views=community_floor_views,
        )


@router.get("/{workspace_id}/performance")
async def get_performance(
    workspace_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Growth and Hot Posts accuracy (read-only; no workspace lock so it never waits on a sync)."""
    try:
        return await compute_workspace_performance(session, workspace_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{workspace_id}/scorecard/grade")
async def grade_scorecard(
    workspace_id: int,
    payload: ScorecardGradeRequest | None = Body(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    async with get_workspace_lock(workspace_id):
        await _ensure_workspace_exists(session, workspace_id)
        req = payload or ScorecardGradeRequest()
        grade_res = await grade_workspace_outcomes(
            session,
            workspace_id,
            breakout_multiple=req.breakout_multiple,
            community_floor_views=req.community_floor_views,
            community_floors=req.community_floors,
        )
        scorecard = await compute_workspace_scorecard(
            session,
            workspace_id,
            auto_grade=False,
            breakout_multiple=req.breakout_multiple,
            community_floor_views=req.community_floor_views,
        )
        scorecard["grade_summary"] = grade_res
        return scorecard


@router.get("/{workspace_id}/changelog")
async def get_changelog(
    workspace_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    await _ensure_workspace_exists(session, workspace_id)
    return await list_workspace_changelog(session, workspace_id)


@router.post("/{workspace_id}/calibrate")
async def calibrate_workspace(
    workspace_id: int,
    payload: CalibrateRequest | None = Body(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    async with get_workspace_lock(workspace_id):
        await _ensure_workspace_exists(session, workspace_id)
        req = payload or CalibrateRequest()
        return await run_auto_calibration(
            session,
            workspace_id,
            min_matured_posts=req.min_matured_posts,
            l2_reg=req.l2_reg,
            max_drift_pct=req.max_drift_pct,
            force_stage_only=req.force_stage_only,
        )


@router.post("/{workspace_id}/changelog/{version_id}/activate")
async def activate_changelog_version(
    workspace_id: int,
    version_id: int,
    payload: ActivateVersionRequest | None = Body(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    async with get_workspace_lock(workspace_id):
        await _ensure_workspace_exists(session, workspace_id)
        try:
            return await rollback_to_version(
                session,
                workspace_id,
                version_id,
                reason=(payload.reason if payload is not None else None),
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc


@router.post("/{workspace_id}/backtest/replay")
async def replay_backtest(
    workspace_id: int,
    payload: BacktestReplayRequest | None = Body(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    await _ensure_workspace_exists(session, workspace_id)
    req = payload or BacktestReplayRequest()
    try:
        return await run_backtest_replay(
            session,
            workspace_id,
            req.merged_overrides(),
            benchmark_10k=req.benchmark_10k,
            benchmark_size=req.benchmark_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{workspace_id}/backtest/promote")
async def promote_backtest(
    workspace_id: int,
    payload: BacktestPromoteRequest | None = Body(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    async with get_workspace_lock(workspace_id):
        await _ensure_workspace_exists(session, workspace_id)
        req = payload or BacktestPromoteRequest()
        try:
            return await promote_candidate_params(
                session,
                workspace_id,
                req.merged_overrides(),
                label=req.label,
                changelog_note=req.changelog_note,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
