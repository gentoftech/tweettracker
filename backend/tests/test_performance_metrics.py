"""Settings growth (success) and Hot Posts accuracy metrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tweettracker.backtest import compute_workspace_performance
from tweettracker.db import build_async_engine
from tweettracker.models import (
    Base,
    OwnFollowerSnapshot,
    OwnReplyOutcome,
    Post,
    PostSnapshot,
    TrackedAccount,
    Workspace,
)
from tweettracker.seed import _build_baseline_curves

NOW = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db_session() -> AsyncSession:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


def _matured_post(ws_id: int, author_id: int, x_id: str, first_age: int, first_views: int, final: int) -> tuple[Post, PostSnapshot]:
    created = NOW - timedelta(days=3)
    post = Post(
        workspace_id=ws_id, x_post_id=x_id, author_account_id=author_id, text=x_id,
        created_at_x=created, latest_views=final, actual_48h_views=final, matured_48h=True,
    )
    snap = PostSnapshot(
        age_minutes=first_age, captured_at=created + timedelta(minutes=first_age), views=first_views,
    )
    return post, snap


async def test_success_and_accuracy_metrics(db_session: AsyncSession) -> None:
    ws = Workspace(
        name="Perf WS", slug="perf-ws", x_data_provider="tweetapi_com",
        hot_posts_min_projected_views=5_000,
    )
    db_session.add(ws)
    await db_session.flush()

    # Followers: previous week 6,950 -> 6,960; this week 6,960 -> 6,971
    for days_ago, count in ((10, 6_950), (7.5, 6_960), (6, 6_965), (1, 6_971)):
        db_session.add(
            OwnFollowerSnapshot(
                workspace_id=ws.id, recorded_at=NOW - timedelta(days=days_ago), followers_count=count,
            )
        )

    # Replies: 3 this week (100, 40, 300 views; one via Hot Posts), 1 last week (50 views)
    for i, (days_ago, views, hot) in enumerate(((1, 100, True), (2, 40, False), (3, 300, False), (9, 50, False))):
        db_session.add(
            OwnReplyOutcome(
                workspace_id=ws.id, x_reply_post_id=f"r{i}", target_handle="someone",
                action_kind="reply", from_hot_posts=hot,
                created_at_x=NOW - timedelta(days=days_ago), latest_views=views,
            )
        )

    big = TrackedAccount(workspace_id=ws.id, x_user_id="u1", handle="big", baseline_curves_json=_build_baseline_curves(10_000))
    small = TrackedAccount(workspace_id=ws.id, x_user_id="u2", handle="small", baseline_curves_json=_build_baseline_curves(1_000))
    db_session.add_all([big, small])
    await db_session.flush()

    # p50 at 15m is 9% of the 48h median, so 900 views at 15m projects exactly 10,000
    cases = [
        _matured_post(ws.id, big.id, "on_target", 15, 900, 12_000),   # 1.2x projection
        _matured_post(ws.id, big.id, "under_called", 15, 900, 30_000),  # 3.0x projection
        _matured_post(ws.id, big.id, "seen_late", 120, 5_000, 20_000),  # first seen after 90m: excluded
        _matured_post(ws.id, small.id, "too_small", 15, 90, 4_000),     # projects 1,000 < 5,000: excluded
    ]
    for post, snap in cases:
        db_session.add(post)
        await db_session.flush()
        snap.post_id = post.id
        db_session.add(snap)
    await db_session.commit()

    perf = await compute_workspace_performance(db_session, ws.id, now=NOW)
    s, a = perf["success"], perf["accuracy"]

    assert s["net_new_followers"] == 11
    assert s["prev_net_new_followers"] == 10
    assert s["replies_sent"] == 3
    assert s["reply_impressions"] == 440
    assert s["median_views_per_reply"] == 100
    assert s["prev_replies_sent"] == 1
    assert s["prev_reply_impressions"] == 50

    assert a["graded_posts"] == 2
    assert a["median_actual_vs_projected"] == pytest.approx(2.1)
    assert a["share_within_2x"] == pytest.approx(0.5)
    # (12,000 - 900) / 12,000 and (30,000 - 900) / 30,000
    assert a["median_share_views_still_ahead"] == pytest.approx((0.925 + 0.97) / 2)
    assert a["hot_posts_replies"] == 1 and a["other_replies"] == 3
    assert a["pick_lift"] is None  # fewer than 5 replies in a group


async def test_performance_handles_an_empty_workspace(db_session: AsyncSession) -> None:
    ws = Workspace(name="Empty WS", slug="empty-ws")
    db_session.add(ws)
    await db_session.commit()
    perf = await compute_workspace_performance(db_session, ws.id, now=NOW)
    assert perf["success"]["net_new_followers"] is None
    assert perf["success"]["replies_sent"] == 0
    assert perf["success"]["median_views_per_reply"] is None
    assert perf["accuracy"]["graded_posts"] == 0
    assert perf["accuracy"]["median_actual_vs_projected"] is None
