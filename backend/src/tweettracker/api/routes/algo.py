"""Algorithm parameter inspection routes (`vm-ranker` & `home-mixer` 9106e9f)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.algo import (
    NEGATIVE_SCORES_OFFSET,
    SCORE_INTERCEPT,
    SCORE_SLOPE,
    UPSTREAM_COMMIT_SHA,
    PipelineParams,
    SurrogateCalibrationParams,
    ValueModelWeights,
)
from tweettracker.api.deps import get_db_session, require_admin_auth
from tweettracker.models import AlgoParamVersion, UpstreamAlgoEvent

router = APIRouter(
    prefix="/api/algo",
    tags=["algo"],
    dependencies=[Depends(require_admin_auth)],
)


@router.get("/params")
async def get_algo_params(
    workspace_id: int | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    active_version: AlgoParamVersion | None = None
    if workspace_id is not None:
        active_version = (
            await session.execute(
                select(AlgoParamVersion)
                .where(
                    AlgoParamVersion.workspace_id == workspace_id,
                    AlgoParamVersion.is_active.is_(True),
                )
                .order_by(AlgoParamVersion.version_number.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if active_version is None:
        active_version = (
            await session.execute(
                select(AlgoParamVersion)
                .where(
                    AlgoParamVersion.workspace_id.is_(None),
                    AlgoParamVersion.is_active.is_(True),
                )
                .order_by(AlgoParamVersion.version_number.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    latest_sync = (
        await session.execute(
            select(UpstreamAlgoEvent).order_by(UpstreamAlgoEvent.id.desc()).limit(1)
        )
    ).scalar_one_or_none()

    vm_defaults = ValueModelWeights.vm_ranker_defaults()
    hm_defaults = ValueModelWeights.home_mixer_defaults()
    pipeline_defaults = PipelineParams()
    surrogate_defaults = SurrogateCalibrationParams()

    return {
        "upstream_commit_sha": (
            active_version.upstream_commit_sha if active_version else UPSTREAM_COMMIT_SHA
        ),
        "negative_scores_offset": NEGATIVE_SCORES_OFFSET,
        "active_version": (
            {
                "id": active_version.id,
                "workspace_id": active_version.workspace_id,
                "version_number": active_version.version_number,
                "label": active_version.label,
                "source": active_version.source,
                "upstream_commit_sha": active_version.upstream_commit_sha,
                "weights": active_version.weights_json,
                "surrogate_params": active_version.surrogate_params_json,
                "thresholds": active_version.thresholds_json,
                "holdout_metrics": active_version.holdout_metrics_json,
                "changelog_note": active_version.changelog_note,
                "created_at": active_version.created_at.isoformat(),
            }
            if active_version
            else None
        ),
        "vm_ranker_weights": vm_defaults.to_dict(),
        "home_mixer_weights": hm_defaults.to_dict(),
        "pipeline_params": pipeline_defaults.to_dict(),
        "surrogate_defaults": surrogate_defaults.to_dict(),
        "user_cred_v2": {
            "intercept": SCORE_INTERCEPT,
            "slope": SCORE_SLOPE,
            "formula": "clamp(165.2 + 7.07 * ln(mass), 0.0, 100.0)",
        },
        "latest_upstream_sync": (
            {
                "base_sha": latest_sync.base_sha,
                "head_sha": latest_sync.head_sha,
                "status": latest_sync.status,
                "detected_at": latest_sync.detected_at.isoformat(),
                "structural_summary": latest_sync.structural_summary,
                "github_issue_url": latest_sync.github_issue_url,
            }
            if latest_sync
            else None
        ),
    }
