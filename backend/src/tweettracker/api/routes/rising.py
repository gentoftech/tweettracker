"""Epic 4 FastAPI routes for <50k Rising Voices discovery, co-signs, promotion & dismissal."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.api.deps import get_db_session, require_admin_auth
from tweettracker.db import get_workspace_lock
from tweettracker.models import (
    AccountCommunityMembership,
    Community,
    DiscoveredCandidate,
    PeerEndorsement,
    TrackedAccount,
    Workspace,
)
from tweettracker.rising import (
    COLD_START_FOLLOWER_CAP,
    ENDORSEMENT_SIGNAL_WEIGHTS,
    MIN_POSTS_ABOVE_3X,
    MIN_SAMPLED_ORIGINAL_POSTS,
    classify_follower_bracket,
    compute_community_projection,
    dismiss_candidate,
    matches_follower_bracket,
    promote_candidate_to_community,
    refresh_rising_candidates,
)

router = APIRouter(
    prefix="/api/workspaces/{workspace_id}/rising",
    tags=["rising"],
    dependencies=[Depends(require_admin_auth)],
)


class PromoteCandidateRequest(BaseModel):
    community_ids: list[int] = Field(default_factory=list)
    is_tracked: bool = True


class DismissCandidateRequest(BaseModel):
    restore: bool = False


def _is_active_membership(m: AccountCommunityMembership) -> bool:
    return bool(m.is_manual_tag) or (
        max(float(m.nmf_interested_in or 0.0), float(m.nmf_known_for or 0.0)) >= 0.25
    )


def _serialize_candidate(
    cand: DiscoveredCandidate,
    *,
    communities: list[Community],
    endorsements: list[PeerEndorsement],
    accounts_by_id: dict[int, TrackedAccount],
    memberships_by_account: dict[int, list[AccountCommunityMembership]],
) -> dict[str, Any]:
    comm_by_slug = {c.slug: c for c in communities}
    proj_map = cand.community_projection_json or {}
    if not proj_map and communities:
        proj_map = compute_community_projection(
            communities=communities,
            endorsements=endorsements,
            accounts_by_id=accounts_by_id,
            memberships_by_account=memberships_by_account,
        )

    projection_items: list[dict[str, Any]] = []
    for slug, weight in sorted(proj_map.items(), key=lambda kv: (-float(kv[1]), kv[0])):
        comm = comm_by_slug.get(slug)
        if comm is not None and float(weight) > 0.0:
            projection_items.append(
                {
                    "community_id": comm.id,
                    "slug": comm.slug,
                    "name": comm.name,
                    "color": comm.color,
                    "weight": round(float(weight), 4),
                }
            )

    serialized_endorsements: list[dict[str, Any]] = []
    distinct_endorsers: set[int] = set()
    sorted_ends = sorted(
        endorsements,
        key=lambda e: (
            -ENDORSEMENT_SIGNAL_WEIGHTS.get(e.endorsement_type.strip().lower(), 1.0),
            -float(e.weight_contribution),
        ),
    )
    for end in sorted_ends:
        endorser = accounts_by_id.get(end.endorser_account_id)
        if endorser is None:
            continue
        distinct_endorsers.add(endorser.id)
        e_type = end.endorsement_type.strip().lower()
        sig_mult = ENDORSEMENT_SIGNAL_WEIGHTS.get(e_type, 1.0)
        if end.source_x_post_id:
            inter_url = f"https://x.com/{endorser.handle}/status/{end.source_x_post_id}"
        else:
            inter_url = f"https://x.com/{endorser.handle}"

        serialized_endorsements.append(
            {
                "id": end.id,
                "endorser_account_id": endorser.id,
                "handle": endorser.handle,
                "display_name": endorser.display_name,
                "heavy_hitter_score": endorser.heavy_hitter_score,
                "salsa_scout": endorser.salsa_scout,
                "endorsement_type": e_type,
                "signal_multiplier": sig_mult,
                "weight_contribution": round(float(end.weight_contribution), 3),
                "source_x_post_id": end.source_x_post_id,
                "interaction_url": inter_url,
                "occurred_at": end.occurred_at.isoformat() if end.occurred_at else None,
            }
        )

    top_posts: list[dict[str, Any]] = []
    for p in cand.top_posts_json or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("x_post_id") or "")
        top_posts.append(
            {
                "x_post_id": pid,
                "text": str(p.get("text") or ""),
                "views": int(p.get("views") or 0),
                "reach_multiple": float(p.get("reach_multiple") or 0.0),
                "post_url": (
                    f"https://x.com/{cand.handle}/status/{pid}"
                    if pid
                    else f"https://x.com/{cand.handle}"
                ),
            }
        )

    return {
        "id": cand.id,
        "workspace_id": cand.workspace_id,
        "x_user_id": cand.x_user_id,
        "handle": cand.handle,
        "display_name": cand.display_name,
        "bio": cand.bio,
        "profile_url": f"https://x.com/{cand.handle}",
        "followers_count": cand.followers_count,
        "followers_growth_rate": cand.followers_growth_rate,
        "follower_bracket": classify_follower_bracket(cand.followers_count),
        "sampled_posts_count": cand.sampled_posts_count,
        "posts_above_3x_count": cand.posts_above_3x_count,
        "median_reach_multiple": cand.median_reach_multiple,
        "meets_consistency_gate": (
            cand.sampled_posts_count >= MIN_SAMPLED_ORIGINAL_POSTS
            and cand.posts_above_3x_count >= MIN_POSTS_ABOVE_3X
        ),
        "punch_above_weight_score": cand.punch_above_weight_score,
        "peer_respect_score": cand.peer_respect_score,
        "combined_rising_score": cand.combined_rising_score,
        "core_affinity_score": round(float(getattr(cand, "core_affinity_score", 0.0) or 0.0), 4),
        "yield_score": round(float(getattr(cand, "yield_score", 0.0) or 0.0), 4),
        "reply_sample_count": int(getattr(cand, "reply_sample_count", 0) or 0),
        "avg_reply_views": round(float(getattr(cand, "avg_reply_views", 0.0) or 0.0), 1),
        "attributed_follows_30d": round(float(getattr(cand, "attributed_follows_30d", 0.0) or 0.0), 2),
        "endorsements_count": len(serialized_endorsements),
        "distinct_endorsers_count": len(distinct_endorsers),
        "endorsements": serialized_endorsements,
        "community_projection_json": proj_map,
        "community_projection": projection_items,
        "top_posts_json": top_posts,
        "status": cand.status,
        "updated_at": cand.updated_at.isoformat() if cand.updated_at else None,
    }


async def _load_workspace_rising_bundle(
    session: AsyncSession,
    workspace_id: int,
) -> tuple[
    Workspace,
    list[Community],
    dict[int, TrackedAccount],
    set[str],
    dict[int, list[AccountCommunityMembership]],
    list[DiscoveredCandidate],
    dict[int, list[PeerEndorsement]],
]:
    ws = await session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {workspace_id} not found.",
        )

    communities = list(
        (
            await session.execute(
                select(Community)
                .where(Community.workspace_id == workspace_id)
                .order_by(Community.sort_order.asc(), Community.id.asc())
            )
        )
        .scalars()
        .all()
    )
    accounts = list(
        (
            await session.execute(
                select(TrackedAccount).where(TrackedAccount.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    accounts_by_id = {a.id: a for a in accounts}
    active_tracked_handles = {a.handle.strip().lower() for a in accounts if a.is_tracked}

    memberships = list(
        (
            await session.execute(
                select(AccountCommunityMembership).where(
                    AccountCommunityMembership.workspace_id == workspace_id
                )
            )
        )
        .scalars()
        .all()
    )
    memberships_by_account: dict[int, list[AccountCommunityMembership]] = defaultdict(list)
    for m in memberships:
        if _is_active_membership(m):
            memberships_by_account[m.account_id].append(m)

    candidates = list(
        (
            await session.execute(
                select(DiscoveredCandidate).where(
                    DiscoveredCandidate.workspace_id == workspace_id
                )
            )
        )
        .scalars()
        .all()
    )
    endorsements = list(
        (
            await session.execute(
                select(PeerEndorsement).where(PeerEndorsement.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    endorsements_by_cand: dict[int, list[PeerEndorsement]] = defaultdict(list)
    for end in endorsements:
        endorsements_by_cand[end.candidate_id].append(end)

    return (
        ws,
        communities,
        accounts_by_id,
        active_tracked_handles,
        memberships_by_account,
        candidates,
        endorsements_by_cand,
    )


@router.get("")
async def list_rising_candidates(
    workspace_id: int,
    community_id: int | None = Query(default=None),
    bracket: str | None = Query(default=None),
    min_endorsements: int | None = Query(default=None, ge=0),
    status_filter: str = Query(default="active", alias="status"),
    sort_by: str = Query(default="combined"),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    (
        _ws,
        communities,
        accounts_by_id,
        active_tracked_handles,
        memberships_by_account,
        candidates,
        endorsements_by_cand,
    ) = await _load_workspace_rising_bundle(session, workspace_id)

    comm_by_id = {c.id: c for c in communities}
    target_comm: Community | None = None
    if community_id is not None:
        target_comm = comm_by_id.get(community_id)
        if target_comm is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Community {community_id} not found in workspace {workspace_id}.",
            )

    norm_status = status_filter.strip().lower()
    if norm_status not in ("active", "promoted", "dismissed", "all"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="status must be one of: active, promoted, dismissed, all",
        )

    norm_bracket = (bracket or "all").strip().lower()
    if norm_bracket not in ("all", "under_2500", "2500_10k", "10k_25k", "25k_50k"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bracket must be one of: all, under_2500, 2500_10k, 10k_25k, 25k_50k",
        )

    norm_sort = sort_by.strip().lower()
    if norm_sort not in ("combined", "punch", "peer_respect", "growth"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="sort_by must be one of: combined, punch, peer_respect, growth",
        )

    counts = {"active": 0, "promoted": 0, "dismissed": 0}
    filtered_items: list[dict[str, Any]] = []

    for cand in candidates:
        # Strict ColdStartFollowerCap (<50k) check
        if cand.followers_count < 0 or cand.followers_count >= COLD_START_FOLLOWER_CAP:
            continue

        eff_status = cand.status
        if eff_status == "active" and cand.handle.strip().lower() in active_tracked_handles:
            eff_status = "promoted"

        if eff_status in counts:
            counts[eff_status] += 1

        if norm_status != "all" and eff_status != norm_status:
            continue

        if not matches_follower_bracket(cand.followers_count, norm_bracket):
            continue

        item = _serialize_candidate(
            cand,
            communities=communities,
            endorsements=endorsements_by_cand.get(cand.id, []),
            accounts_by_id=accounts_by_id,
            memberships_by_account=memberships_by_account,
        )
        item["status"] = eff_status

        if min_endorsements is not None and item["endorsements_count"] < min_endorsements:
            continue

        if target_comm is not None:
            proj = item["community_projection"]
            top_slug = proj[0]["slug"] if proj else None
            target_w = float((item["community_projection_json"] or {}).get(target_comm.slug, 0.0))
            if top_slug != target_comm.slug and target_w < 0.25:
                continue

        filtered_items.append(item)

    if norm_sort == "punch":
        filtered_items.sort(
            key=lambda x: (-x["punch_above_weight_score"], -x["median_reach_multiple"])
        )
    elif norm_sort == "peer_respect":
        filtered_items.sort(
            key=lambda x: (-x["peer_respect_score"], -x["distinct_endorsers_count"])
        )
    elif norm_sort == "growth":
        filtered_items.sort(
            key=lambda x: (-x["followers_growth_rate"], -x["combined_rising_score"])
        )
    else:
        filtered_items.sort(
            key=lambda x: (-x["combined_rising_score"], -x["punch_above_weight_score"])
        )

    return {
        "workspace_id": workspace_id,
        "cold_start_follower_cap": COLD_START_FOLLOWER_CAP,
        "counts": counts,
        "candidates": filtered_items,
    }


@router.post("/refresh")
async def refresh_workspace_rising(
    workspace_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    async with get_workspace_lock(workspace_id):
        ws = await session.get(Workspace, workspace_id)
        if ws is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workspace {workspace_id} not found.",
            )

        try:
            refreshed = await refresh_rising_candidates(
                session, workspace_id, harvest_from_provider=True
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Upstream X data provider request failed.",
            ) from exc

        listing = await list_rising_candidates(
            workspace_id=workspace_id,
            community_id=None,
            bracket="all",
            min_endorsements=None,
            status_filter="active",
            sort_by="combined",
            session=session,
        )
        return {
            "workspace_id": workspace_id,
            "refreshed_count": len(refreshed),
            "counts": listing["counts"],
            "candidates": listing["candidates"],
        }


@router.post("/{candidate_id}/promote")
async def promote_rising_candidate(
    workspace_id: int,
    candidate_id: int,
    payload: PromoteCandidateRequest | None = Body(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    req = payload or PromoteCandidateRequest()
    async with get_workspace_lock(workspace_id):
        try:
            acct = await promote_candidate_to_community(
                session,
                workspace_id=workspace_id,
                candidate_id=candidate_id,
                community_ids=req.community_ids,
                is_tracked=req.is_tracked,
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        m_ids = list(
            (
                await session.execute(
                    select(AccountCommunityMembership.community_id).where(
                        AccountCommunityMembership.account_id == acct.id
                    )
                )
            )
            .scalars()
            .all()
        )
        return {
            "status": "promoted",
            "candidate_id": candidate_id,
            "workspace_id": workspace_id,
            "tracked_account": {
                "id": acct.id,
                "handle": acct.handle,
                "display_name": acct.display_name,
                "followers_count": acct.followers_count,
                "is_tracked": acct.is_tracked,
                "user_cred_v2": acct.user_cred_v2,
                "bridge_role": acct.bridge_role,
                "community_ids": m_ids,
            },
        }


@router.post("/{candidate_id}/dismiss")
async def dismiss_rising_candidate(
    workspace_id: int,
    candidate_id: int,
    restore_query: bool | None = Query(default=None, alias="restore"),
    payload: DismissCandidateRequest | None = Body(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    restore_flag = (
        restore_query
        if restore_query is not None
        else (payload.restore if payload is not None else False)
    )
    async with get_workspace_lock(workspace_id):
        try:
            cand = await dismiss_candidate(
                session,
                workspace_id=workspace_id,
                candidate_id=candidate_id,
                restore=restore_flag,
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

        return {
            "candidate_id": cand.id,
            "workspace_id": cand.workspace_id,
            "handle": cand.handle,
            "status": cand.status,
        }
