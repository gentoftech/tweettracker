"""Workspace, Community, Overview, and Spend Ledger CRUD routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tweettracker.api.deps import get_db_session, require_admin_auth
from tweettracker.api.schemas import (
    CommunityCreate,
    CommunityOut,
    CommunityUpdate,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceOverviewOut,
    WorkspaceUpdate,
    slugify,
)
from tweettracker.db import get_workspace_lock
from tweettracker.graph.service import recompute_workspace_graph
from tweettracker.models import (
    AccountCommunityMembership,
    Community,
    DiscoveredCandidate,
    Post,
    TrackedAccount,
    Workspace,
    utc_now,
)
from tweettracker.providers import (
    ProviderNotConfiguredError,
    build_spend_summary,
    get_x_data_provider,
)

router = APIRouter(
    prefix="/api/workspaces",
    tags=["workspaces"],
    dependencies=[Depends(require_admin_auth)],
)


def _active_membership_filter():
    return or_(
        AccountCommunityMembership.is_manual_tag.is_(True),
        AccountCommunityMembership.nmf_interested_in >= 0.25,
        AccountCommunityMembership.nmf_known_for >= 0.25,
    )


async def _unique_workspace_slug(session: AsyncSession, base_text: str) -> str:
    base = slugify(base_text)
    candidate = base
    counter = 2
    while True:
        exists = (
            await session.execute(select(Workspace.id).where(Workspace.slug == candidate))
        ).scalar_one_or_none()
        if exists is None:
            return candidate
        candidate = f"{base}-{counter}"
        counter += 1


async def _unique_community_slug(
    session: AsyncSession, workspace_id: int, base_text: str
) -> str:
    base = slugify(base_text)
    candidate = base
    counter = 2
    while True:
        exists = (
            await session.execute(
                select(Community.id).where(
                    Community.workspace_id == workspace_id,
                    Community.slug == candidate,
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            return candidate
        candidate = f"{base}-{counter}"
        counter += 1


async def _serialize_workspace(session: AsyncSession, ws: Workspace) -> WorkspaceOut:
    comms_stmt = (
        select(Community)
        .where(Community.workspace_id == ws.id)
        .order_by(Community.sort_order.asc(), Community.id.asc())
    )
    comms = list((await session.execute(comms_stmt)).scalars().all())

    mem_counts_rows = (
        await session.execute(
            select(
                AccountCommunityMembership.community_id,
                func.count(AccountCommunityMembership.id),
            )
            .where(
                AccountCommunityMembership.workspace_id == ws.id,
                _active_membership_filter(),
            )
            .group_by(AccountCommunityMembership.community_id)
        )
    ).all()
    mem_counts = {int(cid): int(cnt) for cid, cnt in mem_counts_rows}

    acct_cnt = int(
        (
            await session.execute(
                select(func.count(TrackedAccount.id)).where(
                    TrackedAccount.workspace_id == ws.id,
                    TrackedAccount.is_tracked.is_(True),
                )
            )
        ).scalar_one()
        or 0
    )
    posts_cnt = int(
        (
            await session.execute(
                select(func.count(Post.id)).where(Post.workspace_id == ws.id)
            )
        ).scalar_one()
        or 0
    )

    comm_outs = [
        CommunityOut(
            id=c.id,
            workspace_id=c.workspace_id,
            name=c.name,
            slug=c.slug,
            color=c.color,
            description=c.description,
            x_list_id=c.x_list_id,
            watch_threshold_override=c.watch_threshold_override,
            surge_threshold_override=c.surge_threshold_override,
            sort_order=c.sort_order,
            accounts_count=mem_counts.get(c.id, 0),
        )
        for c in comms
    ]

    return WorkspaceOut(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        my_x_handle=ws.my_x_handle,
        x_data_provider=ws.x_data_provider,
        llm_provider=ws.llm_provider,
        radar_poll_minutes=ws.radar_poll_minutes,
        use_x_lists=ws.use_x_lists,
        x_list_ids_json=list(ws.x_list_ids_json or []),
        monthly_unit_budget=int(getattr(ws, "monthly_unit_budget", 100_000) or 100_000),
        hot_posts_min_projected_views=int(getattr(ws, "hot_posts_min_projected_views", 5_000) or 0),
        daily_spend_cap_usd=ws.daily_spend_cap_usd,
        monthly_spend_cap_usd=ws.monthly_spend_cap_usd,
        watch_threshold=ws.watch_threshold,
        surge_threshold=ws.surge_threshold,
        quiet_hours_start=ws.quiet_hours_start,
        quiet_hours_end=ws.quiet_hours_end,
        max_alerts_per_hour=ws.max_alerts_per_hour,
        webhook_urls_json=list(ws.webhook_urls_json or []),
        created_at=ws.created_at,
        updated_at=ws.updated_at,
        communities=comm_outs,
        tracked_accounts_count=acct_cnt,
        posts_count=posts_cnt,
    )


async def _get_workspace_or_404(session: AsyncSession, workspace_id: int) -> Workspace:
    ws = (
        await session.execute(
            select(Workspace)
            .options(selectinload(Workspace.communities))
            .where(Workspace.id == workspace_id)
        )
    ).scalar_one_or_none()
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {workspace_id} not found.",
        )
    return ws


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    session: AsyncSession = Depends(get_db_session),
) -> list[WorkspaceOut]:
    workspaces = list(
        (await session.execute(select(Workspace).order_by(Workspace.id.asc()))).scalars().all()
    )
    return [await _serialize_workspace(session, ws) for ws in workspaces]


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreate,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceOut:
    initial_comms = payload.initial_communities
    if initial_comms is None:
        initial_comms = [
            CommunityCreate(
                name="Workspace Network",
                color="#38bdf8",
                description="Default workspace network.",
            )
        ]

    for c_in in initial_comms:
        eff_watch = (
            c_in.watch_threshold_override
            if c_in.watch_threshold_override is not None
            else payload.watch_threshold
        )
        eff_surge = (
            c_in.surge_threshold_override
            if c_in.surge_threshold_override is not None
            else payload.surge_threshold
        )
        if eff_watch >= eff_surge:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Effective community watch_threshold must be strictly less than surge_threshold.",
            )

    slug = await _unique_workspace_slug(session, payload.slug or payload.name)
    ws = Workspace(
        name=payload.name.strip(),
        slug=slug,
        my_x_handle=payload.my_x_handle,
        x_data_provider=payload.x_data_provider,
        llm_provider=payload.llm_provider,
        radar_poll_minutes=payload.radar_poll_minutes,
        use_x_lists=payload.use_x_lists,
        x_list_ids_json=list(payload.x_list_ids_json or []),
        monthly_unit_budget=payload.monthly_unit_budget,
        hot_posts_min_projected_views=payload.hot_posts_min_projected_views,
        daily_spend_cap_usd=payload.daily_spend_cap_usd,
        monthly_spend_cap_usd=payload.monthly_spend_cap_usd,
        watch_threshold=payload.watch_threshold,
        surge_threshold=payload.surge_threshold,
        quiet_hours_start=(payload.quiet_hours_start or None),
        quiet_hours_end=(payload.quiet_hours_end or None),
        max_alerts_per_hour=payload.max_alerts_per_hour,
        webhook_urls_json=[u.strip() for u in payload.webhook_urls_json if u.strip()],
    )
    session.add(ws)
    await session.flush()

    for idx, c_in in enumerate(initial_comms):
        c_slug = await _unique_community_slug(session, ws.id, c_in.slug or c_in.name)
        session.add(
            Community(
                workspace_id=ws.id,
                name=c_in.name.strip(),
                slug=c_slug,
                color=c_in.color,
                description=c_in.description,
                x_list_id=c_in.x_list_id,
                watch_threshold_override=c_in.watch_threshold_override,
                surge_threshold_override=c_in.surge_threshold_override,
                sort_order=(c_in.sort_order if c_in.sort_order is not None else idx),
            )
        )

    await session.commit()
    await session.refresh(ws)
    return await _serialize_workspace(session, ws)


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceOut:
    ws = await _get_workspace_or_404(session, workspace_id)
    return await _serialize_workspace(session, ws)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    workspace_id: int,
    payload: WorkspaceUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceOut:
    async with get_workspace_lock(workspace_id):
        ws = await _get_workspace_or_404(session, workspace_id)
        fields = payload.model_fields_set

        next_watch = (
            payload.watch_threshold
            if ("watch_threshold" in fields and payload.watch_threshold is not None)
            else ws.watch_threshold
        )
        next_surge = (
            payload.surge_threshold
            if ("surge_threshold" in fields and payload.surge_threshold is not None)
            else ws.surge_threshold
        )
        if next_watch >= next_surge:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="watch_threshold must be strictly less than surge_threshold.",
            )

        next_q_start = (
            (payload.quiet_hours_start or "").strip() or None
            if "quiet_hours_start" in fields
            else ws.quiet_hours_start
        )
        next_q_end = (
            (payload.quiet_hours_end or "").strip() or None
            if "quiet_hours_end" in fields
            else ws.quiet_hours_end
        )
        if bool(next_q_start) != bool(next_q_end):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="quiet_hours_start and quiet_hours_end must both be set or both be empty.",
            )

        if "name" in fields and payload.name is not None:
            ws.name = payload.name.strip()
        if "my_x_handle" in fields:
            ws.my_x_handle = payload.my_x_handle
        if "x_data_provider" in fields and payload.x_data_provider is not None:
            ws.x_data_provider = payload.x_data_provider
        if "llm_provider" in fields and payload.llm_provider is not None:
            ws.llm_provider = payload.llm_provider
        if "radar_poll_minutes" in fields and payload.radar_poll_minutes is not None:
            ws.radar_poll_minutes = payload.radar_poll_minutes
        if "use_x_lists" in fields and payload.use_x_lists is not None:
            ws.use_x_lists = payload.use_x_lists
        if "x_list_ids_json" in fields and payload.x_list_ids_json is not None:
            ws.x_list_ids_json = list(payload.x_list_ids_json)
        if "monthly_unit_budget" in fields and payload.monthly_unit_budget is not None:
            ws.monthly_unit_budget = payload.monthly_unit_budget
        if (
            "hot_posts_min_projected_views" in fields
            and payload.hot_posts_min_projected_views is not None
        ):
            ws.hot_posts_min_projected_views = payload.hot_posts_min_projected_views
        if "daily_spend_cap_usd" in fields and payload.daily_spend_cap_usd is not None:
            ws.daily_spend_cap_usd = payload.daily_spend_cap_usd
        if "monthly_spend_cap_usd" in fields and payload.monthly_spend_cap_usd is not None:
            ws.monthly_spend_cap_usd = payload.monthly_spend_cap_usd
        if "watch_threshold" in fields and payload.watch_threshold is not None:
            ws.watch_threshold = payload.watch_threshold
        if "surge_threshold" in fields and payload.surge_threshold is not None:
            ws.surge_threshold = payload.surge_threshold
        if "quiet_hours_start" in fields:
            ws.quiet_hours_start = next_q_start
        if "quiet_hours_end" in fields:
            ws.quiet_hours_end = next_q_end
        if "max_alerts_per_hour" in fields and payload.max_alerts_per_hour is not None:
            ws.max_alerts_per_hour = payload.max_alerts_per_hour
        if "webhook_urls_json" in fields and payload.webhook_urls_json is not None:
            ws.webhook_urls_json = [
                u.strip() for u in payload.webhook_urls_json if u and u.strip()
            ]

        ws.updated_at = utc_now()
        await session.flush()
        if "my_x_handle" in fields:
            await recompute_workspace_graph(session, ws.id, commit=False)
        await session.commit()
        await session.refresh(ws)
        return await _serialize_workspace(session, ws)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    async with get_workspace_lock(workspace_id):
        ws = await _get_workspace_or_404(session, workspace_id)
        await session.delete(ws)
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{workspace_id}/overview", response_model=WorkspaceOverviewOut)
async def get_workspace_overview(
    workspace_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceOverviewOut:
    ws = await _get_workspace_or_404(session, workspace_id)

    comms_cnt = int(
        (
            await session.execute(
                select(func.count(Community.id)).where(Community.workspace_id == ws.id)
            )
        ).scalar_one()
        or 0
    )
    tracked_cnt = int(
        (
            await session.execute(
                select(func.count(TrackedAccount.id)).where(
                    TrackedAccount.workspace_id == ws.id,
                    TrackedAccount.is_tracked.is_(True),
                )
            )
        ).scalar_one()
        or 0
    )
    mutual_cnt = (
        int(
            (
                await session.execute(
                    select(func.count(TrackedAccount.id)).where(
                        TrackedAccount.workspace_id == ws.id,
                        TrackedAccount.is_tracked.is_(True),
                        TrackedAccount.follows_me.is_(True),
                        TrackedAccount.followed_by_me.is_(True),
                    )
                )
            ).scalar_one()
            or 0
        )
        if ws.my_x_handle
        else 0
    )
    bridge_cnt = int(
        (
            await session.execute(
                select(func.count(TrackedAccount.id)).where(
                    TrackedAccount.workspace_id == ws.id,
                    TrackedAccount.bridge_role != "provincial",
                )
            )
        ).scalar_one()
        or 0
    )
    active_posts_cnt = int(
        (
            await session.execute(
                select(func.count(Post.id)).where(
                    Post.workspace_id == ws.id,
                    Post.matured_48h.is_(False),
                )
            )
        ).scalar_one()
        or 0
    )
    matured_posts_cnt = int(
        (
            await session.execute(
                select(func.count(Post.id)).where(
                    Post.workspace_id == ws.id,
                    Post.matured_48h.is_(True),
                )
            )
        ).scalar_one()
        or 0
    )
    rising_cnt = int(
        (
            await session.execute(
                select(func.count(DiscoveredCandidate.id)).where(
                    DiscoveredCandidate.workspace_id == ws.id,
                    DiscoveredCandidate.status == "active",
                )
            )
        ).scalar_one()
        or 0
    )

    top_rows = (
        await session.execute(
            select(Post, TrackedAccount)
            .join(TrackedAccount, Post.author_account_id == TrackedAccount.id)
            .where(Post.workspace_id == ws.id, Post.matured_48h.is_(False))
            .order_by(Post.breakout_score.desc())
            .limit(5)
        )
    ).all()

    top_live = [
        {
            "id": p.id,
            "x_post_id": p.x_post_id,
            "author_handle": a.handle,
            "author_display_name": a.display_name,
            "text": p.text,
            "breakout_score": p.breakout_score,
            "recommended_action": p.recommended_action,
            "latest_views": p.latest_views,
            "dpp_novelty_multiplier": p.dpp_novelty_multiplier,
        }
        for p, a in top_rows
    ]

    return WorkspaceOverviewOut(
        workspace_id=ws.id,
        communities_count=comms_cnt,
        tracked_accounts_count=tracked_cnt,
        mutual_accounts_count=mutual_cnt,
        bridge_accounts_count=bridge_cnt,
        active_posts_count=active_posts_cnt,
        matured_posts_count=matured_posts_cnt,
        rising_candidates_count=rising_cnt,
        top_live_posts=top_live,
    )


@router.get("/{workspace_id}/communities", response_model=list[CommunityOut])
async def list_communities(
    workspace_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> list[CommunityOut]:
    ws_out = await _serialize_workspace(
        session, await _get_workspace_or_404(session, workspace_id)
    )
    return ws_out.communities


@router.post(
    "/{workspace_id}/communities",
    response_model=CommunityOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_community(
    workspace_id: int,
    payload: CommunityCreate,
    session: AsyncSession = Depends(get_db_session),
) -> CommunityOut:
    async with get_workspace_lock(workspace_id):
        ws = await _get_workspace_or_404(session, workspace_id)
        eff_watch = (
            payload.watch_threshold_override
            if payload.watch_threshold_override is not None
            else ws.watch_threshold
        )
        eff_surge = (
            payload.surge_threshold_override
            if payload.surge_threshold_override is not None
            else ws.surge_threshold
        )
        if eff_watch >= eff_surge:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Effective community watch_threshold must be strictly less than surge_threshold.",
            )

        max_order = int(
            (
                await session.execute(
                    select(func.coalesce(func.max(Community.sort_order), -1)).where(
                        Community.workspace_id == ws.id
                    )
                )
            ).scalar_one()
        )
        c_slug = await _unique_community_slug(session, ws.id, payload.slug or payload.name)
        comm = Community(
            workspace_id=ws.id,
            name=payload.name.strip(),
            slug=c_slug,
            color=payload.color,
            description=payload.description,
            x_list_id=payload.x_list_id,
            watch_threshold_override=payload.watch_threshold_override,
            surge_threshold_override=payload.surge_threshold_override,
            sort_order=(payload.sort_order if payload.sort_order is not None else (max_order + 1)),
        )
        session.add(comm)
        await session.flush()
        await recompute_workspace_graph(session, ws.id, commit=False)
        await session.commit()
        await session.refresh(comm)
        acct_cnt = int(
            (
                await session.execute(
                    select(func.count(AccountCommunityMembership.id)).where(
                        AccountCommunityMembership.community_id == comm.id,
                        _active_membership_filter(),
                    )
                )
            ).scalar_one()
            or 0
        )
        return CommunityOut(
            id=comm.id,
            workspace_id=comm.workspace_id,
            name=comm.name,
            slug=comm.slug,
            color=comm.color,
            description=comm.description,
            x_list_id=comm.x_list_id,
            watch_threshold_override=comm.watch_threshold_override,
            surge_threshold_override=comm.surge_threshold_override,
            sort_order=comm.sort_order,
            accounts_count=acct_cnt,
        )


@router.patch(
    "/{workspace_id}/communities/{community_id}",
    response_model=CommunityOut,
)
async def update_community(
    workspace_id: int,
    community_id: int,
    payload: CommunityUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> CommunityOut:
    async with get_workspace_lock(workspace_id):
        ws = await _get_workspace_or_404(session, workspace_id)
        comm = (
            await session.execute(
                select(Community).where(
                    Community.id == community_id,
                    Community.workspace_id == workspace_id,
                )
            )
        ).scalar_one_or_none()
        if comm is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Community {community_id} not found in workspace {workspace_id}.",
            )

        fields = payload.model_fields_set
        next_watch_ov = (
            payload.watch_threshold_override
            if "watch_threshold_override" in fields
            else comm.watch_threshold_override
        )
        next_surge_ov = (
            payload.surge_threshold_override
            if "surge_threshold_override" in fields
            else comm.surge_threshold_override
        )
        eff_watch = next_watch_ov if next_watch_ov is not None else ws.watch_threshold
        eff_surge = next_surge_ov if next_surge_ov is not None else ws.surge_threshold
        if eff_watch >= eff_surge:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Effective community watch_threshold must be strictly less than surge_threshold.",
            )

        if "name" in fields and payload.name is not None:
            comm.name = payload.name.strip()
        if "color" in fields and payload.color is not None:
            comm.color = payload.color
        if "description" in fields and payload.description is not None:
            comm.description = payload.description
        if "x_list_id" in fields:
            comm.x_list_id = (payload.x_list_id or "").strip() or None
        if "watch_threshold_override" in fields:
            comm.watch_threshold_override = payload.watch_threshold_override
        if "surge_threshold_override" in fields:
            comm.surge_threshold_override = payload.surge_threshold_override
        if "sort_order" in fields and payload.sort_order is not None:
            comm.sort_order = payload.sort_order

        await session.flush()
        if "name" in fields or "color" in fields or "sort_order" in fields:
            await recompute_workspace_graph(session, workspace_id, commit=False)
        await session.commit()
        await session.refresh(comm)

        acct_cnt = int(
            (
                await session.execute(
                    select(func.count(AccountCommunityMembership.id)).where(
                        AccountCommunityMembership.community_id == comm.id,
                        _active_membership_filter(),
                    )
                )
            ).scalar_one()
            or 0
        )
        return CommunityOut(
            id=comm.id,
            workspace_id=comm.workspace_id,
            name=comm.name,
            slug=comm.slug,
            color=comm.color,
            description=comm.description,
            x_list_id=comm.x_list_id,
            watch_threshold_override=comm.watch_threshold_override,
            surge_threshold_override=comm.surge_threshold_override,
            sort_order=comm.sort_order,
            accounts_count=acct_cnt,
        )


@router.delete(
    "/{workspace_id}/communities/{community_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_community(
    workspace_id: int,
    community_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    async with get_workspace_lock(workspace_id):
        await _get_workspace_or_404(session, workspace_id)
        comm = (
            await session.execute(
                select(Community).where(
                    Community.id == community_id,
                    Community.workspace_id == workspace_id,
                )
            )
        ).scalar_one_or_none()
        if comm is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Community {community_id} not found in workspace {workspace_id}.",
            )

        comm_count = int(
            (
                await session.execute(
                    select(func.count(Community.id)).where(
                        Community.workspace_id == workspace_id
                    )
                )
            ).scalar_one()
            or 0
        )
        if comm_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Workspace must retain at least 1 community (K >= 1).",
            )

        await session.delete(comm)
        await session.flush()
        await recompute_workspace_graph(session, workspace_id, commit=False)
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{workspace_id}/spend")
async def get_workspace_spend(
    workspace_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    ws = await _get_workspace_or_404(session, workspace_id)
    return await build_spend_summary(session, ws)


@router.post("/{workspace_id}/spend/reconcile")
async def reconcile_workspace_spend(
    workspace_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    async with get_workspace_lock(workspace_id):
        ws = await _get_workspace_or_404(session, workspace_id)
        try:
            provider = get_x_data_provider(ws.x_data_provider)
            usage = await provider.get_account_usage(session, workspace_id=ws.id)
            await session.commit()
        except ProviderNotConfiguredError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Upstream X data provider request failed.",
            ) from exc

        summary = await build_spend_summary(session, ws)
        summary["reconciled_usage"] = {
            "provider": usage.provider,
            "remaining_units": usage.remaining_units,
            "used_units": usage.used_units,
            "plan_name": usage.plan_name,
        }
        return summary
