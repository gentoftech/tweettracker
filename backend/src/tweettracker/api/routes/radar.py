"""Epic 2 Breakout Post Radar, Post Trajectory Inspector, Live SSE & HMAC Webhook Routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hmac
import math
import os
import time
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
import httpx
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.algo import recommend_engagement_action
from tweettracker.api.deps import (
    bearer_scheme,
    extract_supplied_password,
    get_db_session,
    require_admin_auth,
    verify_sse_ticket,
)
from tweettracker.config import Settings
from tweettracker.db import get_session_factory, get_workspace_lock
from tweettracker.models import (
    AccountCommunityMembership,
    AlertDelivery,
    BreakoutPrediction,
    Community,
    Post,
    PostInteraction,
    PostSnapshot,
    TrackedAccount,
    Workspace,
)
from tweettracker.providers import ProviderNotConfiguredError, SpendCapExceededError
from tweettracker.radar import (
    BASELINE_AGES,
    build_tactical_rationale,
    compute_velocity_metrics,
    default_baseline_curves,
    interpolate_baseline_quantiles,
    load_active_algo_bundle,
    project_48h_views,
    poll_workspace_radar,
    radar_sse_hub,
    resolve_effective_thresholds,
    score_single_post,
    send_test_webhook,
    validate_outbound_webhook_url,
)

# Hot Posts reply window: past this, a new reply lands far down the thread
HOT_POSTS_MAX_AGE_MINUTES: float = 90.0

router = APIRouter(
    prefix="/api/workspaces",
    tags=["radar"],
)


def _active_membership_clause() -> Any:
    return or_(
        AccountCommunityMembership.is_manual_tag.is_(True),
        AccountCommunityMembership.nmf_interested_in >= 0.25,
        AccountCommunityMembership.nmf_known_for >= 0.25,
    )


class WebhookTestRequest(BaseModel):
    target_url: str | None = Field(default=None, max_length=512)
    note: str | None = Field(default=None, max_length=280)

    @field_validator("target_url")
    @classmethod
    def _validate_target_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if not s:
            return None
        return validate_outbound_webhook_url(s, resolve_dns=False)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def require_sse_or_admin_auth(
    ticket: str | None = Query(default=None),
    token: str | None = Query(default=None),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_admin_password: str | None = Header(default=None, alias="X-Admin-Password"),
) -> None:
    """Authenticates Browser `EventSource` SSE connections via short-lived HMAC-signed
    `?ticket=` query param (preferred), `?token=`, or `Authorization: Bearer` /
    `X-Admin-Password` headers when `TWEETTRACKER_ADMIN_PASSWORD` is configured.
    """
    expected = Settings.admin_password()
    if not expected:
        return

    if ticket and verify_sse_ticket(ticket):
        return

    supplied = extract_supplied_password(credentials, x_admin_password)
    if not supplied and token:
        supplied = token.strip()

    if not supplied or not hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin password or valid SSE ticket required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _get_workspace_or_404(session: AsyncSession, workspace_id: int) -> Workspace:
    ws = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one_or_none()
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {workspace_id} not found.",
        )
    return ws


def _serialize_alert_delivery(row: AlertDelivery) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "post_id": row.post_id,
        "event_type": row.event_type,
        "channel": row.channel,
        "target_url": row.target_url,
        "alert_tier": row.alert_tier,
        "payload_json": row.payload_json or {},
        "status": row.status,
        "http_status": row.http_status,
        "attempts": row.attempts,
        "error_message": row.error_message,
        "created_at": _ensure_utc(row.created_at).isoformat(),
    }


def _build_post_card(
    *,
    ws: Workspace,
    post: Post,
    author: TrackedAccount,
    memberships_with_comms: list[tuple[AccountCommunityMembership, Community]],
    snapshots: list[PostSnapshot],
    interactions: list[PostInteraction],
    actor_accounts_by_id: dict[int, TrackedAccount],
    now_utc: datetime,
    prob_midpoint: float = 65.0,
    prob_scale: float = 9.5,
) -> dict[str, Any]:
    created_utc = _ensure_utc(post.created_at_x)
    age_minutes = max(1, int(round((now_utc - created_utc).total_seconds() / 60.0)))

    active_mems = [
        (mem, comm)
        for mem, comm in memberships_with_comms
        if mem.is_manual_tag
        or max(float(mem.nmf_interested_in), float(mem.nmf_known_for)) >= 0.25
    ]
    author_comms = [comm for _, comm in active_mems]
    eff_watch, eff_surge = resolve_effective_thresholds(ws, author_comms)

    vel = compute_velocity_metrics(
        views=post.latest_views,
        age_minutes=float(age_minutes),
        baseline_curves=author.baseline_curves_json,
        followers_fallback=author.followers_count,
    )

    raw_b_score = float(post.breakout_score)
    b_score = round(raw_b_score, 1)
    if raw_b_score >= eff_surge:
        alert_tier = "surge"
    elif raw_b_score >= eff_watch:
        alert_tier = "watch"
    else:
        alert_tier = "none"

    has_my_handle = bool(ws.my_x_handle and ws.my_x_handle.strip())
    is_mutual = has_my_handle and bool(author.follows_me and author.followed_by_me)
    is_bridge = len(author_comms) >= 2 or float(author.bridge_score) >= 45.0

    rec_action = recommend_engagement_action(
        is_mutual_follow_author=is_mutual,
        is_reply=post.is_reply,
        is_retweet=post.is_retweet,
        breakout_score=raw_b_score,
        watch_threshold=eff_watch,
    )
    rationale = build_tactical_rationale(
        recommended_action=rec_action,
        my_x_handle=ws.my_x_handle,
        is_reply=post.is_reply,
        is_retweet=post.is_retweet,
        breakout_score=raw_b_score,
        watch_threshold=eff_watch,
    )

    logit = max(-40.0, min(40.0, (raw_b_score - prob_midpoint) / max(prob_scale, 0.5)))
    breakout_prob = 0.0 if raw_b_score <= 0.0 else round(1.0 / (1.0 + math.exp(-logit)), 4)

    post_url = f"https://x.com/{author.handle}/status/{post.x_post_id}"
    reply_intent_url = f"https://x.com/intent/tweet?in_reply_to={post.x_post_id}"
    quote_intent_url = f"https://x.com/intent/tweet?url={quote(post_url, safe='')}"

    comm_badges = [
        {
            "id": comm.id,
            "name": comm.name,
            "slug": comm.slug,
            "color": comm.color,
            "nmf_known_for": round(float(mem.nmf_known_for), 3),
            "nmf_interested_in": round(float(mem.nmf_interested_in), 3),
            "watch_threshold_override": comm.watch_threshold_override,
            "surge_threshold_override": comm.surge_threshold_override,
        }
        for mem, comm in active_mems
    ]

    snap_items = [
        {
            "id": s.id,
            "age_minutes": s.age_minutes,
            "captured_at": _ensure_utc(s.captured_at).isoformat(),
            "views": s.views,
            "likes": s.likes,
            "replies": s.replies,
            "quotes": s.quotes,
            "reposts": s.reposts,
            "bookmarks": s.bookmarks,
            "breakout_score_at_capture": round(float(s.breakout_score_at_capture), 1),
        }
        for s in snapshots
    ]

    inter_items = []
    for inter in interactions:
        occ_utc = _ensure_utc(inter.occurred_at)
        mins_after = max(0, int(round((occ_utc - created_utc).total_seconds() / 60.0)))
        actor_acct = (
            actor_accounts_by_id.get(inter.actor_account_id)
            if inter.actor_account_id is not None
            else None
        )
        inter_items.append(
            {
                "id": inter.id,
                "actor_account_id": inter.actor_account_id,
                "actor_x_user_id": inter.actor_x_user_id,
                "actor_handle": inter.actor_handle,
                "actor_display_name": actor_acct.display_name if actor_acct else inter.actor_handle,
                "actor_followers": inter.actor_followers,
                "actor_heavy_hitter_score": (
                    round(float(actor_acct.heavy_hitter_score), 1)
                    if actor_acct is not None
                    else None
                ),
                "interaction_type": inter.interaction_type,
                "occurred_at": occ_utc.isoformat(),
                "minutes_after_post": mins_after,
            }
        )

    baseline_curves = (
        author.baseline_curves_json
        if author.baseline_curves_json
        else default_baseline_curves(max(400, int(author.followers_count * 0.18)))
    )

    cold_start_eligible = bool(
        (not post.is_reply)
        and (not post.is_retweet)
        and author.followers_count <= 50_000
        and age_minutes <= 120
        and post.latest_views < 200
    )

    pace_mult = round(float(vel.velocity_multiple), 1)
    typical_views = max(1, int(round(vel.expected_p50)))

    rep_samples = int(getattr(author, "reply_sample_count", 0) or 0)
    avg_rep_views = round(float(getattr(author, "avg_reply_views", 0.0) or 0.0), 1)
    attr_follows = round(float(getattr(author, "attributed_follows_30d", 0.0) or 0.0), 2)
    raw_yield = float(getattr(author, "yield_score", 0.0) or 0.0)
    if rep_samples > 0 or raw_yield > 0.0:
        eff_yield = min(1.0, max(0.0, raw_yield))
    else:
        eff_yield = (
            min(1.0, max(0.0, float(author.core_affinity_score or 0.5)))
            if float(author.core_affinity_score or 0.0) > 0.0
            else 0.5
        )
    projected_48h_views = project_48h_views(
        views=post.latest_views,
        age_minutes=float(age_minutes),
        baseline_curves=author.baseline_curves_json,
        followers_fallback=author.followers_count,
    )
    # Goal is reach + followers for the reply, not the post beating its author's usual:
    # - projected reach (55): a reply's reach is capped by how many people see the post;
    #   momentum is already inside the projection, so it is not counted again on its own
    # - reply yield (24): has replying to this author paid off before
    # - earliness (15): early replies sit near the top of the thread
    # - mutual follow (6): X weights replies between mutuals 20x
    # log10 scale: 1k projected views -> 0, 10k -> 0.33, 100k -> 0.67, 1M -> 1.0
    reach_scale_norm = min(1.0, max(0.0, math.log10(max(projected_48h_views, 1.0) / 1_000.0) / 3.0))
    early_norm = min(1.0, max(0.0, 1.0 - float(age_minutes) / HOT_POSTS_MAX_AGE_MINUTES))
    mutual_bonus = 6.0 if is_mutual else (2.0 if (has_my_handle and bool(author.follows_me)) else 0.0)
    expected_reply_reach_score = round(
        min(
            100.0,
            max(
                0.0,
                55.0 * reach_scale_norm + 24.0 * eff_yield + 15.0 * early_norm + mutual_bonus,
            ),
        ),
        1,
    )

    why_reasons: list[str] = [
        f"{post.latest_views:,} views in {age_minutes}m vs ~{typical_views:,} typical for @{author.handle}"
    ]
    if rep_samples > 0 and avg_rep_views >= 100.0:
        foll_suffix = (
            f" (+{attr_follows:.1f} follows/30d)" if attr_follows >= 0.5 else ""
        )
        why_reasons.append(
            f"Your replies to @{author.handle} average ~{int(round(avg_rep_views)):,} views{foll_suffix}"
        )
    if inter_items:
        notable = ", ".join(f"@{it['actor_handle']}" for it in inter_items[:2])
        why_reasons.append(f"Network voices ({notable}) engaged early")
    if (post.latest_quotes + post.latest_bookmarks) >= 8:
        why_reasons.append(
            f"High-intent saves & quotes ({post.latest_quotes} quotes · {post.latest_bookmarks} bookmarks)"
        )
    if is_mutual:
        why_reasons.append("Mutual connection — your reply gets prioritized thread placement")
    if len(why_reasons) < 2:
        pct_rank = min(99, max(60, int(round(breakout_prob * 100.0))))
        why_reasons.append(f"Moving faster than {pct_rank}% of comparable posts in your network")

    queue_status = (post.queue_status or "open").strip().lower()
    acted_at_iso = _ensure_utc(post.acted_at).isoformat() if post.acted_at is not None else None
    acted_action_kind = getattr(post, "acted_action_kind", None)

    return {
        "id": post.id,
        "workspace_id": post.workspace_id,
        "x_post_id": post.x_post_id,
        "text": post.text,
        "created_at_x": created_utc.isoformat(),
        "age_minutes": age_minutes,
        "queue_status": queue_status,
        "acted_at": acted_at_iso,
        "acted_action_kind": acted_action_kind,
        "is_reply": post.is_reply,
        "is_retweet": post.is_retweet,
        "is_quote": post.is_quote,
        "quoted_post_id": post.quoted_post_id,
        "has_url": post.has_url,
        "has_media": post.has_media,
        "media_type": post.media_type,
        "video_duration_ms": post.video_duration_ms,
        "matured_48h": post.matured_48h,
        "latest_views": post.latest_views,
        "latest_likes": post.latest_likes,
        "latest_replies": post.latest_replies,
        "latest_quotes": post.latest_quotes,
        "latest_reposts": post.latest_reposts,
        "latest_bookmarks": post.latest_bookmarks,
        "breakout_score": b_score,
        "expected_reply_reach_score": expected_reply_reach_score,
        "projected_48h_views": int(round(projected_48h_views)),
        "breakout_probability": breakout_prob,
        "velocity_z_score": round(float(post.velocity_z_score), 2),
        "velocity_multiple": round(float(vel.velocity_multiple), 2),
        "expected_p50_views": int(round(vel.expected_p50)),
        "seismic_infectiousness": round(float(post.seismic_infectiousness), 3),
        "value_model_score": round(float(post.value_model_score), 4),
        "dpp_novelty_multiplier": round(float(post.dpp_novelty_multiplier), 3),
        "dpp_crowding_discounted": bool(float(post.dpp_novelty_multiplier) < 0.85),
        "catalysis_boost": round(float(post.catalysis_boost), 2),
        "cold_start_eligible": cold_start_eligible,
        "recommended_action": rec_action,
        "tactical_rationale": rationale,
        "alert_tier": alert_tier,
        "effective_watch_threshold": eff_watch,
        "effective_surge_threshold": eff_surge,
        "hover_summary": {
            "pace_label": f"{pace_mult}x usual views at {age_minutes}m",
            "typical_views": typical_views,
            "why_reasons": why_reasons[:3],
        },
        "author": {
            "id": author.id,
            "x_user_id": author.x_user_id,
            "handle": author.handle,
            "display_name": author.display_name,
            "avatar_url": author.avatar_url,
            "followers_count": author.followers_count,
            "is_auto_tracked": bool(author.is_auto_tracked),
            "follows_me": bool(author.follows_me) if has_my_handle else False,
            "followed_by_me": bool(author.followed_by_me) if has_my_handle else False,
            "is_mutual": is_mutual,
            "relationship_state": author.relationship_state if has_my_handle else "none",
            "heavy_hitter_score": round(float(author.heavy_hitter_score), 1),
            "user_cred_v2": round(float(author.user_cred_v2), 1),
            "yield_score": round(eff_yield, 4),
            "reply_sample_count": rep_samples,
            "avg_reply_views": avg_rep_views,
            "attributed_follows_30d": attr_follows,
            "catalysis_lift": round(float(author.catalysis_lift), 2),
            "bridge_score": round(float(author.bridge_score), 1),
            "bridge_role": author.bridge_role,
            "is_bridge": is_bridge,
            "communities": comm_badges,
            "baseline_curves_json": baseline_curves,
        },
        "snapshots": snap_items,
        "interactions": inter_items,
        "x_links": {
            "post_url": post_url,
            "reply_intent_url": reply_intent_url,
            "quote_intent_url": quote_intent_url,
        },
    }


async def _hydrate_post_cards(
    session: AsyncSession,
    ws: Workspace,
    rows: list[tuple[Post, TrackedAccount]],
    now_utc: datetime,
) -> list[dict[str, Any]]:
    if not rows:
        return []

    algo_bundle = await load_active_algo_bundle(session, ws.id, ensure_db_row=False)
    fus = getattr(algo_bundle, "fusion_params", None)
    prob_midpoint = float(getattr(fus, "prob_midpoint", 65.0))
    prob_scale = float(getattr(fus, "prob_scale", 9.5))

    post_ids = [p.id for p, _ in rows]
    author_ids = list({a.id for _, a in rows})

    mem_rows = (
        await session.execute(
            select(AccountCommunityMembership, Community)
            .join(Community, AccountCommunityMembership.community_id == Community.id)
            .where(
                AccountCommunityMembership.workspace_id == ws.id,
                AccountCommunityMembership.account_id.in_(author_ids),
                _active_membership_clause(),
            )
            .order_by(Community.sort_order.asc(), Community.id.asc())
        )
    ).all()
    mems_by_acct: dict[int, list[tuple[AccountCommunityMembership, Community]]] = {}
    for mem, comm in mem_rows:
        mems_by_acct.setdefault(mem.account_id, []).append((mem, comm))

    snap_rows = list(
        (
            await session.execute(
                select(PostSnapshot)
                .where(PostSnapshot.post_id.in_(post_ids))
                .order_by(PostSnapshot.post_id.asc(), PostSnapshot.age_minutes.asc())
            )
        )
        .scalars()
        .all()
    )
    snaps_by_post: dict[int, list[PostSnapshot]] = {}
    for s in snap_rows:
        snaps_by_post.setdefault(s.post_id, []).append(s)

    inter_rows = list(
        (
            await session.execute(
                select(PostInteraction)
                .where(PostInteraction.post_id.in_(post_ids))
                .order_by(PostInteraction.occurred_at.asc())
            )
        )
        .scalars()
        .all()
    )
    inters_by_post: dict[int, list[PostInteraction]] = {}
    actor_ids: set[int] = set()
    for inter in inter_rows:
        inters_by_post.setdefault(inter.post_id, []).append(inter)
        if inter.actor_account_id is not None:
            actor_ids.add(inter.actor_account_id)

    actors_by_id: dict[int, TrackedAccount] = {a.id: a for _, a in rows}
    missing_actor_ids = [aid for aid in actor_ids if aid not in actors_by_id]
    if missing_actor_ids:
        extra_actors = list(
            (
                await session.execute(
                    select(TrackedAccount).where(
                        TrackedAccount.workspace_id == ws.id,
                        TrackedAccount.id.in_(missing_actor_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        for act in extra_actors:
            actors_by_id[act.id] = act

    return [
        _build_post_card(
            ws=ws,
            post=post,
            author=author,
            memberships_with_comms=mems_by_acct.get(author.id, []),
            snapshots=snaps_by_post.get(post.id, []),
            interactions=inters_by_post.get(post.id, []),
            actor_accounts_by_id=actors_by_id,
            now_utc=now_utc,
            prob_midpoint=prob_midpoint,
            prob_scale=prob_scale,
        )
        for post, author in rows
    ]


async def _rejuvenate_stale_fixture_posts_if_needed(
    session: AsyncSession,
    ws: Workspace,
    now_utc: datetime,
) -> bool:
    """Keeps open demo-mode fixture posts fresh when a local dev server sits idle for >85m."""
    if ws.x_data_provider != "fixture":
        return False

    open_posts = list(
        (
            await session.execute(
                select(Post)
                .where(
                    Post.workspace_id == ws.id,
                    Post.matured_48h.is_(False),
                    Post.queue_status == "open",
                )
                .order_by(Post.breakout_score.desc(), Post.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not open_posts:
        return False

    ages = [(now_utc - _ensure_utc(p.created_at_x)).total_seconds() / 60.0 for p in open_posts]
    if min(ages) <= 85.0:
        return False

    post_ids = [p.id for p in open_posts]
    snaps = list(
        (
            await session.execute(
                select(PostSnapshot).where(PostSnapshot.post_id.in_(post_ids))
            )
        )
        .scalars()
        .all()
    )
    snaps_by_post: dict[int, list[PostSnapshot]] = {}
    for s in snaps:
        snaps_by_post.setdefault(s.post_id, []).append(s)

    for idx, p in enumerate(open_posts):
        target_age = 18 + ((idx * 3) % 38)
        new_created = now_utc - timedelta(minutes=target_age)
        p.created_at_x = new_created
        for s in snaps_by_post.get(p.id, []):
            s.captured_at = new_created + timedelta(minutes=min(s.age_minutes, target_age))

    await session.commit()
    return True


class QueueStatusUpdateRequest(BaseModel):
    status: Literal["open", "acted", "skipped", "saved"]
    action_kind: Literal["reply", "quote"] | None = None


@router.get(
    "/{workspace_id}/radar",
    dependencies=[Depends(require_admin_auth)],
)
async def get_breakout_radar_feed(
    workspace_id: int,
    community_id: int | None = Query(default=None),
    bridges_only: bool = Query(default=False),
    mutuals_only: bool = Query(default=False),
    max_age_minutes: int | None = Query(default=None, ge=1),
    min_score: float | None = Query(default=None, ge=0.0, le=100.0),
    tier: str | None = Query(default=None),
    include_matured: bool = Query(default=False),
    live_queue: bool = Query(default=False),
    saved_only: bool = Query(default=False),
    limit: int = Query(default=60, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Sub-150ms indexed Breakout Radar feed with dynamic K-community, bridge,
    mutual, age-window, live ephemeral queue, and tier filters.
    """
    t0 = time.perf_counter()
    ws = await _get_workspace_or_404(session, workspace_id)
    now_utc = datetime.now(timezone.utc)
    has_my_handle = bool(ws.my_x_handle and ws.my_x_handle.strip())

    # Only demo (fixture) workspaces need the write lock; live workspaces must never queue
    # this read behind a long-running network sync holding the same lock.
    if live_queue and ws.x_data_provider == "fixture":
        async with get_workspace_lock(ws.id):
            await _rejuvenate_stale_fixture_posts_if_needed(session, ws, now_utc)

    if mutuals_only and not has_my_handle:
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return {
            "workspace_id": ws.id,
            "my_x_handle": ws.my_x_handle,
            "watch_threshold": ws.watch_threshold,
            "surge_threshold": ws.surge_threshold,
            "total_matching": 0,
            "surge_count": 0,
            "watch_count": 0,
            "mutual_count": 0,
            "query_ms": elapsed_ms,
            "generated_at": now_utc.isoformat(),
            "items": [],
        }

    stmt = (
        select(Post, TrackedAccount)
        .join(TrackedAccount, Post.author_account_id == TrackedAccount.id)
        .where(Post.workspace_id == ws.id)
    )

    if saved_only:
        # Saved-for-later posts stay listed after they leave the live window or mature at 48h
        stmt = stmt.where(Post.queue_status == "saved")
    elif not include_matured:
        stmt = stmt.where(Post.matured_48h.is_(False))

    if live_queue and not saved_only:
        stmt = stmt.where(
            Post.queue_status == "open",
            TrackedAccount.is_tracked.is_(True),
            TrackedAccount.is_muted.is_(False),
            Post.created_at_x >= now_utc - timedelta(minutes=HOT_POSTS_MAX_AGE_MINUTES),
        )

    if community_id is not None:
        comm_subq = select(AccountCommunityMembership.account_id).where(
            AccountCommunityMembership.workspace_id == ws.id,
            AccountCommunityMembership.community_id == community_id,
            _active_membership_clause(),
        )
        stmt = stmt.where(Post.author_account_id.in_(comm_subq))

    if bridges_only:
        multi_comm_subq = (
            select(AccountCommunityMembership.account_id)
            .where(
                AccountCommunityMembership.workspace_id == ws.id,
                _active_membership_clause(),
            )
            .group_by(AccountCommunityMembership.account_id)
            .having(func.count(AccountCommunityMembership.community_id) >= 2)
        )
        stmt = stmt.where(
            or_(
                TrackedAccount.bridge_score >= 45.0,
                TrackedAccount.id.in_(multi_comm_subq),
            )
        )

    if mutuals_only:
        stmt = stmt.where(
            TrackedAccount.follows_me.is_(True),
            TrackedAccount.followed_by_me.is_(True),
        )

    if max_age_minutes is not None:
        cutoff = now_utc - timedelta(minutes=int(max_age_minutes))
        stmt = stmt.where(Post.created_at_x >= cutoff)

    if min_score is not None:
        stmt = stmt.where(Post.breakout_score >= float(min_score))

    if saved_only:
        stmt = stmt.order_by(Post.acted_at.desc(), Post.id.desc()).limit(limit)
    elif live_queue:
        # Every open post in the reply window is a candidate; ranking happens after hydration
        stmt = stmt.order_by(Post.created_at_x.desc()).limit(max(limit, 500))
    else:
        stmt = stmt.order_by(Post.breakout_score.desc(), Post.created_at_x.desc()).limit(limit)
    rows = list((await session.execute(stmt)).all())

    cards = await _hydrate_post_cards(session, ws, rows, now_utc)

    if live_queue and not saved_only:
        # Early enough to reply near the top of the thread, and projected big enough to be worth
        # it. No "beating its author's usual" gate: pace already shapes the projection.
        min_projected = int(getattr(ws, "hot_posts_min_projected_views", 5_000) or 0)
        cards = [
            c
            for c in cards
            if c["age_minutes"] <= HOT_POSTS_MAX_AGE_MINUTES
            and c["projected_48h_views"] >= min_projected
        ]
        cards.sort(
            key=lambda c: (
                -float(c.get("expected_reply_reach_score", c["breakout_score"])),
                -float(c["breakout_score"]),
                -int(c["id"]),
            )
        )
        cards = cards[:limit]

    if tier:
        t_norm = tier.strip().lower()
        if t_norm == "surge":
            cards = [c for c in cards if c["alert_tier"] == "surge"]
        elif t_norm == "watch":
            cards = [c for c in cards if c["alert_tier"] in ("watch", "surge")]
        elif t_norm == "watch_only":
            cards = [c for c in cards if c["alert_tier"] == "watch"]
        elif t_norm == "none":
            cards = [c for c in cards if c["alert_tier"] == "none"]

    surge_cnt = sum(1 for c in cards if c["alert_tier"] == "surge")
    watch_cnt = sum(1 for c in cards if c["alert_tier"] == "watch")
    mutual_cnt = sum(1 for c in cards if c["author"]["is_mutual"])
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    return {
        "workspace_id": ws.id,
        "my_x_handle": ws.my_x_handle,
        "watch_threshold": ws.watch_threshold,
        "surge_threshold": ws.surge_threshold,
        "total_matching": len(cards),
        "surge_count": surge_cnt,
        "watch_count": watch_cnt,
        "mutual_count": mutual_cnt,
        "query_ms": elapsed_ms,
        "generated_at": now_utc.isoformat(),
        "items": cards,
    }


@router.post(
    "/{workspace_id}/radar/posts/{post_id}/queue",
    dependencies=[Depends(require_admin_auth)],
)
async def update_radar_post_queue_status(
    workspace_id: int,
    post_id: int,
    payload: QueueStatusUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Marks a live queue post as acted ('Reply'/'Quote'), skipped, saved for later, or open (undo)."""
    async with get_workspace_lock(workspace_id):
        ws = await _get_workspace_or_404(session, workspace_id)
        post = (
            await session.execute(
                select(Post).where(Post.workspace_id == ws.id, Post.id == post_id)
            )
        ).scalar_one_or_none()
        if post is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Post {post_id} not found in workspace {workspace_id}.",
            )

        now_utc = datetime.now(timezone.utc)
        post.queue_status = payload.status
        post.acted_at = now_utc if payload.status in ("acted", "skipped", "saved") else None
        if payload.status == "acted":
            post.acted_action_kind = payload.action_kind or post.acted_action_kind or "reply"
        elif payload.status == "open":
            post.acted_action_kind = None
        await session.commit()

        return {
            "workspace_id": ws.id,
            "post_id": post.id,
            "queue_status": post.queue_status,
            "acted_at": post.acted_at.isoformat() if post.acted_at is not None else None,
            "acted_action_kind": post.acted_action_kind,
        }


@router.get(
    "/{workspace_id}/radar/posts/{post_id}",
    dependencies=[Depends(require_admin_auth)],
)
async def get_radar_post_detail(
    workspace_id: int,
    post_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Detailed post trajectory + full 29-head `PhoenixScores` & `ValueModelWeights`
    contribution breakdown + author P25/P50/P75/P90 curve comparison.
    """
    ws = await _get_workspace_or_404(session, workspace_id)
    now_utc = datetime.now(timezone.utc)

    row = (
        await session.execute(
            select(Post, TrackedAccount)
            .join(TrackedAccount, Post.author_account_id == TrackedAccount.id)
            .where(Post.workspace_id == ws.id, Post.id == post_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post {post_id} not found in workspace {workspace_id}.",
        )
    post, author = row

    cards = await _hydrate_post_cards(session, ws, [(post, author)], now_utc)
    card = cards[0]

    mem_rows = (
        await session.execute(
            select(Community)
            .join(
                AccountCommunityMembership,
                AccountCommunityMembership.community_id == Community.id,
            )
            .where(
                AccountCommunityMembership.workspace_id == ws.id,
                AccountCommunityMembership.account_id == author.id,
                _active_membership_clause(),
            )
            .order_by(Community.sort_order.asc())
        )
    ).scalars().all()
    author_comms = list(mem_rows)

    snapshots = list(
        (
            await session.execute(
                select(PostSnapshot)
                .where(PostSnapshot.post_id == post.id)
                .order_by(PostSnapshot.age_minutes.asc())
            )
        )
        .scalars()
        .all()
    )
    interactions = list(
        (
            await session.execute(
                select(PostInteraction)
                .where(PostInteraction.post_id == post.id)
                .order_by(PostInteraction.occurred_at.asc())
            )
        )
        .scalars()
        .all()
    )
    actor_ids = [i.actor_account_id for i in interactions if i.actor_account_id is not None]
    actors_map: dict[int, TrackedAccount] = {}
    if actor_ids:
        for act in (
            await session.execute(
                select(TrackedAccount).where(
                    TrackedAccount.workspace_id == ws.id,
                    TrackedAccount.id.in_(actor_ids),
                )
            )
        ).scalars().all():
            actors_map[act.id] = act

    algo_bundle = await load_active_algo_bundle(session, ws.id, ensure_db_row=False)
    scored = score_single_post(
        ws,
        post,
        author,
        communities=author_comms,
        snapshots=snapshots,
        interactions=interactions,
        algo_bundle=algo_bundle,
        actor_accounts_by_id=actors_map,
        now=now_utc,
        enforce_age_48h_drop=False,
    )

    snap_by_age = {int(s.age_minutes): s for s in snapshots}
    curve_ages = sorted(set(BASELINE_AGES) | set(snap_by_age.keys()) | {min(card["age_minutes"], 2880)})
    trajectory_comparison: list[dict[str, Any]] = []
    for age_pt in curve_ages:
        q = interpolate_baseline_quantiles(
            author.baseline_curves_json,
            float(age_pt),
            followers_fallback=author.followers_count,
        )
        actual_views: int | None = None
        actual_score: float | None = None
        if age_pt in snap_by_age:
            actual_views = snap_by_age[age_pt].views
            actual_score = round(float(snap_by_age[age_pt].breakout_score_at_capture), 1)
        elif age_pt == min(card["age_minutes"], 2880):
            actual_views = post.latest_views
            actual_score = card["breakout_score"]

        trajectory_comparison.append(
            {
                "age_minutes": age_pt,
                "p25": int(round(q.p25)),
                "p50": int(round(q.p50)),
                "p75": int(round(q.p75)),
                "p90": int(round(q.p90)),
                "actual_views": actual_views,
                "breakout_score": actual_score,
            }
        )

    predictions = list(
        (
            await session.execute(
                select(BreakoutPrediction)
                .where(BreakoutPrediction.post_id == post.id)
                .order_by(BreakoutPrediction.id.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    deliveries = list(
        (
            await session.execute(
                select(AlertDelivery)
                .where(AlertDelivery.post_id == post.id)
                .order_by(AlertDelivery.id.desc())
                .limit(15)
            )
        )
        .scalars()
        .all()
    )

    return {
        "post": card,
        "algo_version": {
            "param_version_id": algo_bundle.param_version_id,
            "label": algo_bundle.version_label,
            "upstream_commit_sha": algo_bundle.upstream_commit_sha,
            "dpp_theta": algo_bundle.pipeline_params.dpp_theta,
            "oon_weight_factor": algo_bundle.pipeline_params.oon_weight_factor,
        },
        "stage1_filters": scored.stage1_filters.to_dict(),
        "velocity_detail": {
            "age_minutes": scored.velocity.age_minutes,
            "views": int(scored.velocity.views),
            "expected_p25": scored.velocity.expected_p25,
            "expected_p50": scored.velocity.expected_p50,
            "expected_p75": scored.velocity.expected_p75,
            "expected_p90": scored.velocity.expected_p90,
            "velocity_multiple": round(scored.velocity.velocity_multiple, 3),
            "iqr_sigma": round(scored.velocity.iqr_sigma, 2),
            "velocity_z_score": round(scored.velocity.velocity_z_score, 3),
        },
        "seismic_detail": {
            "infectiousness": scored.seismic.infectiousness,
            "weighted_cascade_events": scored.seismic.weighted_cascade_events,
            "decayed_cascade_events": scored.seismic.decayed_cascade_events,
            "decayed_exposure_views": scored.seismic.decayed_exposure_views,
            "regularized_rate_per_view": scored.seismic.regularized_rate_per_view,
            "cascade_velocity_per_min": scored.seismic.cascade_velocity_per_min,
            "acceleration_factor": scored.seismic.acceleration_factor,
            "signal_breakdown": scored.seismic.signal_breakdown,
        },
        "value_model_breakdown": scored.value_breakdown.to_dict(),
        "trajectory_comparison": trajectory_comparison,
        "predictions": [
            {
                "id": pr.id,
                "param_version_id": pr.param_version_id,
                "predicted_at": _ensure_utc(pr.predicted_at).isoformat(),
                "post_age_minutes": pr.post_age_minutes,
                "breakout_probability": pr.breakout_probability,
                "breakout_score": pr.breakout_score,
                "alert_tier": pr.alert_tier,
                "outcome_class": pr.outcome_class,
                "brier_error": pr.brier_error,
            }
            for pr in predictions
        ],
        "alert_deliveries": [_serialize_alert_delivery(d) for d in deliveries],
    }


@router.post(
    "/{workspace_id}/radar/poll",
    dependencies=[Depends(require_admin_auth)],
)
async def trigger_radar_poll(
    workspace_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Triggers an immediate paced radar poll + Stage 1/Stage 2 rescore pass for
    the workspace and returns updated feed summary + new alerts count.
    """
    async with get_workspace_lock(workspace_id):
        ws = await _get_workspace_or_404(session, workspace_id)
        try:
            poll_summary = await poll_workspace_radar(session, ws)
        except SpendCapExceededError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(exc),
            ) from exc
        except ProviderNotConfiguredError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Upstream X data provider error ({exc.__class__.__name__}).",
            ) from exc

        now_utc = datetime.now(timezone.utc)
        top_rows = list(
            (
                await session.execute(
                    select(Post, TrackedAccount)
                    .join(TrackedAccount, Post.author_account_id == TrackedAccount.id)
                    .where(Post.workspace_id == ws.id, Post.matured_48h.is_(False))
                    .order_by(Post.breakout_score.desc(), Post.created_at_x.desc())
                    .limit(20)
                )
            ).all()
        )
        top_cards = await _hydrate_post_cards(session, ws, top_rows, now_utc)

        return {
            **poll_summary.to_dict(),
            "polled_at": now_utc.isoformat(),
            "top_items": top_cards,
        }


@router.get("/{workspace_id}/events")
async def stream_workspace_radar_events(
    workspace_id: int,
    request: Request,
    max_events: int | None = Query(default=None, ge=1, le=200),
    heartbeat_seconds: float = Query(default=15.0, ge=0.1, le=60.0),
    _auth: None = Depends(require_sse_or_admin_auth),
) -> StreamingResponse:
    """Real-time `text/event-stream` SSE endpoint streaming live `breakout.watch`,
    `breakout.surge`, `radar.polled`, `webhook.tested`, and periodic `heartbeat` frames
    without holding a SQLAlchemy connection while the stream stays open.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        await _get_workspace_or_404(session, workspace_id)

    try:
        queue = radar_sse_hub.subscribe(workspace_id)
    except OverflowError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc

    return StreamingResponse(
        radar_sse_hub.stream_events(
            workspace_id,
            heartbeat_interval_seconds=heartbeat_seconds,
            max_events=max_events,
            request=request,
            pre_subscribed_queue=queue,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/{workspace_id}/alerts",
    dependencies=[Depends(require_admin_auth)],
)
async def list_workspace_alerts(
    workspace_id: int,
    channel: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Returns recent `AlertDelivery` history (`sse` & `webhook`, status, HTTP code,
    attempts, timestamps) for the workspace.
    """
    ws = await _get_workspace_or_404(session, workspace_id)
    stmt = select(AlertDelivery).where(AlertDelivery.workspace_id == ws.id)
    if channel and channel.strip():
        stmt = stmt.where(AlertDelivery.channel == channel.strip().lower())
    if status_filter and status_filter.strip():
        stmt = stmt.where(AlertDelivery.status == status_filter.strip().lower())

    stmt = stmt.order_by(AlertDelivery.created_at.desc(), AlertDelivery.id.desc()).limit(limit)
    rows = list((await session.execute(stmt)).scalars().all())

    return {
        "workspace_id": ws.id,
        "webhook_secret_configured": bool(os.environ.get("TWEETTRACKER_WEBHOOK_SECRET", "")),
        "configured_webhook_urls": list(ws.webhook_urls_json or []),
        "quiet_hours_start": ws.quiet_hours_start,
        "quiet_hours_end": ws.quiet_hours_end,
        "max_alerts_per_hour": ws.max_alerts_per_hour,
        "total_returned": len(rows),
        "items": [_serialize_alert_delivery(r) for r in rows],
    }


@router.post(
    "/{workspace_id}/webhooks/test",
    dependencies=[Depends(require_admin_auth)],
)
async def test_workspace_webhook(
    workspace_id: int,
    payload: WebhookTestRequest | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Sends a signed `HMAC-SHA256` test webhook (`event_type="webhook.test"`) to
    `target_url` (or `workspace.webhook_urls_json`), logs `AlertDelivery`, and returns
    the truncated `X-TweetTracker-Signature-256` preview + delivery audit results.
    """
    ws = await _get_workspace_or_404(session, workspace_id)
    req = payload or WebhookTestRequest()
    try:
        summary = await send_test_webhook(
            session,
            ws,
            target_url=req.target_url,
            custom_note=req.note,
            retry_delays=(0.05, 0.1),
            max_attempts=2,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    await session.commit()

    return {
        "workspace_id": summary.workspace_id,
        "event_type": summary.event_type,
        "signature_header": summary.signature_header,
        "signature_preview": summary.signature_preview,
        "webhook_secret_configured": summary.webhook_secret_configured,
        "signed": summary.signed,
        "payload": summary.payload,
        "deliveries": [_serialize_alert_delivery(o.delivery) for o in summary.outcomes],
    }
