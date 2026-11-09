"""Growth (success) and Hot Posts accuracy metrics for the Settings dashboard.

Success is measured only on posts the workspace owner wrote and on their own follower
count, so every number can be checked against X's own analytics. Accuracy grades Hot
Posts on the moment a post was first seen (when it could have been shown), never on a
later re-check that already knows the outcome.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.graph.reply_reach import (
    MIN_MATCHED_SAMPLES,
    REPLY_REACH_LEDGER_ENDPOINTS,
    SAMPLE_WINDOW_DAYS,
)
from tweettracker.models import (
    ApiCreditLedger,
    DiscoveredCandidate,
    OwnFollowerSnapshot,
    OwnReplyOutcome,
    Post,
    PostSnapshot,
    TrackedAccount,
    Workspace,
)
from tweettracker.radar.velocity import project_48h_views

SUCCESS_WINDOW_DAYS = 7
ACCURACY_WINDOW_DAYS = 14
PICK_LIFT_WINDOW_DAYS = 30
# Hot Posts only shows posts under 90 minutes old, so only those can be graded as picks
HOT_POSTS_WINDOW_MINUTES = 90
MIN_SAMPLES_PER_GROUP = 5


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _median_or_none(values: list[float]) -> float | None:
    return float(median(values)) if values else None


async def _follower_change(
    session: AsyncSession, workspace_id: int, start: datetime, end: datetime
) -> int | None:
    """Last reading in the window minus the last reading at or before its start (or its first)."""
    in_window = list(
        (
            await session.execute(
                select(OwnFollowerSnapshot)
                .where(
                    OwnFollowerSnapshot.workspace_id == workspace_id,
                    OwnFollowerSnapshot.recorded_at <= end,
                )
                .order_by(OwnFollowerSnapshot.recorded_at.asc())
            )
        )
        .scalars()
        .all()
    )
    before = [s for s in in_window if _utc(s.recorded_at) <= start]
    during = [s for s in in_window if start < _utc(s.recorded_at) <= end]
    if not during:
        return None
    baseline = before[-1] if before else during[0]
    if baseline is during[-1]:
        return None
    return int(during[-1].followers_count) - int(baseline.followers_count)


async def _reply_stats(
    session: AsyncSession, workspace_id: int, start: datetime, end: datetime
) -> dict[str, Any]:
    rows = list(
        (
            await session.execute(
                select(OwnReplyOutcome).where(
                    OwnReplyOutcome.workspace_id == workspace_id,
                    OwnReplyOutcome.created_at_x > start,
                    OwnReplyOutcome.created_at_x <= end,
                )
            )
        )
        .scalars()
        .all()
    )
    views = [float(max(0, r.latest_views or 0)) for r in rows]
    return {
        "replies_sent": len(rows),
        "reply_impressions": int(sum(views)),
        "median_views_per_reply": _median_or_none(views),
    }


async def _success_block(
    session: AsyncSession, workspace_id: int, now: datetime
) -> dict[str, Any]:
    cur_start = now - timedelta(days=SUCCESS_WINDOW_DAYS)
    prev_start = cur_start - timedelta(days=SUCCESS_WINDOW_DAYS)
    cur = await _reply_stats(session, workspace_id, cur_start, now)
    prev = await _reply_stats(session, workspace_id, prev_start, cur_start)
    return {
        "window_days": SUCCESS_WINDOW_DAYS,
        "net_new_followers": await _follower_change(session, workspace_id, cur_start, now),
        "prev_net_new_followers": await _follower_change(
            session, workspace_id, prev_start, cur_start
        ),
        **cur,
        "prev_replies_sent": prev["replies_sent"],
        "prev_reply_impressions": prev["reply_impressions"],
        "prev_median_views_per_reply": prev["median_views_per_reply"],
    }


async def _accuracy_block(
    session: AsyncSession, ws: Workspace, now: datetime
) -> dict[str, Any]:
    since = now - timedelta(days=ACCURACY_WINDOW_DAYS)
    first_age = (
        select(PostSnapshot.post_id, func.min(PostSnapshot.age_minutes).label("first_age"))
        .group_by(PostSnapshot.post_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Post, TrackedAccount, PostSnapshot)
            .join(TrackedAccount, Post.author_account_id == TrackedAccount.id)
            .join(first_age, first_age.c.post_id == Post.id)
            .join(
                PostSnapshot,
                (PostSnapshot.post_id == Post.id)
                & (PostSnapshot.age_minutes == first_age.c.first_age),
            )
            .where(
                Post.workspace_id == ws.id,
                Post.matured_48h.is_(True),
                Post.created_at_x >= since,
                first_age.c.first_age <= HOT_POSTS_WINDOW_MINUTES,
            )
        )
    ).all()

    min_projected = int(getattr(ws, "hot_posts_min_projected_views", 0) or 0)
    ratios: list[float] = []
    still_ahead: list[float] = []
    seen_posts: set[int] = set()
    for post, author, snap in rows:
        if post.id in seen_posts:
            continue
        seen_posts.add(post.id)
        final_views = int(post.actual_48h_views or post.latest_views or 0)
        if final_views <= 0:
            continue
        projected = project_48h_views(
            views=snap.views,
            age_minutes=float(snap.age_minutes),
            baseline_curves=author.baseline_curves_json,
            followers_fallback=author.followers_count,
        )
        # Only posts that cleared the minimum could have been shown in Hot Posts
        if projected < max(min_projected, 1):
            continue
        ratios.append(final_views / projected)
        still_ahead.append(max(0.0, (final_views - snap.views) / final_views))

    within_2x = [r for r in ratios if 0.5 <= r <= 2.0]

    lift_since = now - timedelta(days=PICK_LIFT_WINDOW_DAYS)
    replies = list(
        (
            await session.execute(
                select(OwnReplyOutcome).where(
                    OwnReplyOutcome.workspace_id == ws.id,
                    OwnReplyOutcome.created_at_x >= lift_since,
                )
            )
        )
        .scalars()
        .all()
    )
    hot = [float(r.latest_views or 0) for r in replies if r.from_hot_posts]
    other = [float(r.latest_views or 0) for r in replies if not r.from_hot_posts]
    enough = len(hot) >= MIN_SAMPLES_PER_GROUP and len(other) >= MIN_SAMPLES_PER_GROUP
    hot_med, other_med = _median_or_none(hot), _median_or_none(other)

    return {
        "window_days": ACCURACY_WINDOW_DAYS,
        "graded_posts": len(ratios),
        "median_actual_vs_projected": _median_or_none(ratios),
        "share_within_2x": (len(within_2x) / len(ratios)) if ratios else None,
        "median_share_views_still_ahead": _median_or_none(still_ahead),
        "pick_lift_window_days": PICK_LIFT_WINDOW_DAYS,
        "hot_posts_replies": len(hot),
        "other_replies": len(other),
        "hot_posts_median_views": hot_med,
        "other_median_views": other_med,
        "pick_lift": (
            (hot_med / other_med) if (enough and hot_med is not None and other_med) else None
        ),
        "pick_lift_min_samples": MIN_SAMPLES_PER_GROUP,
    }


async def _reach_coverage_block(
    session: AsyncSession, ws: Workspace, now: datetime
) -> dict[str, Any]:
    """How much of the network has a measured reply reach, and what measuring it costs."""
    tracked = list(
        (
            await session.execute(
                select(TrackedAccount).where(
                    TrackedAccount.workspace_id == ws.id,
                    TrackedAccount.is_tracked.is_(True),
                    TrackedAccount.is_muted.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    measured = [a for a in tracked if int(a.reply_reach_samples or 0) >= MIN_MATCHED_SAMPLES]
    cands_measured = int(
        (
            await session.execute(
                select(func.count(DiscoveredCandidate.id)).where(
                    DiscoveredCandidate.workspace_id == ws.id,
                    DiscoveredCandidate.reply_reach_samples >= MIN_MATCHED_SAMPLES,
                )
            )
        ).scalar_one()
        or 0
    )
    units_30d = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(ApiCreditLedger.units_consumed), 0.0)).where(
                    ApiCreditLedger.workspace_id == ws.id,
                    ApiCreditLedger.endpoint.in_(REPLY_REACH_LEDGER_ENDPOINTS),
                    ApiCreditLedger.created_at >= now - timedelta(days=30),
                )
            )
        ).scalar_one()
        or 0.0
    )
    scores = [float(a.reply_reach_score) for a in measured if a.reply_reach_score is not None]
    medians = [float(a.reply_reach_median) for a in measured if a.reply_reach_median is not None]
    return {
        "sample_window_days": SAMPLE_WINDOW_DAYS,
        "tracked_accounts": len(tracked),
        "tracked_measured": len(measured),
        "candidates_measured": cands_measured,
        "network_median_reach_score": _median_or_none(scores),
        "network_median_reply_views": _median_or_none(medians),
        "units_30d": int(units_30d),
        "units_per_account_month": (units_30d / len(tracked)) if tracked else None,
    }


async def compute_workspace_performance(
    session: AsyncSession, workspace_id: int, *, now: datetime | None = None
) -> dict[str, Any]:
    """Success (followers, reply reach, activity) and Hot Posts accuracy for one workspace."""
    now_utc = _utc(now) if now is not None else datetime.now(timezone.utc)
    ws = await session.get(Workspace, workspace_id)
    if ws is None:
        raise LookupError(f"Workspace {workspace_id} not found.")
    return {
        "workspace_id": workspace_id,
        "generated_at": now_utc.isoformat(),
        "success": await _success_block(session, workspace_id, now_utc),
        "accuracy": await _accuracy_block(session, ws, now_utc),
        "reach": await _reach_coverage_block(session, ws, now_utc),
    }
