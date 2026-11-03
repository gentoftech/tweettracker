"""Hourly sync re-hydrates stale / zero follower counts on tracked accounts."""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tweettracker.db import build_async_engine
from tweettracker.graph.live_crawler import refresh_tracked_profiles
from tweettracker.models import Base, TrackedAccount, Workspace
from tweettracker.providers import TWEETAPI_BASE_URL


@pytest.fixture
async def db_session() -> AsyncSession:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


@respx.mock
async def test_refresh_tracked_profiles_fills_zero_follower_counts(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWEETAPI_API_KEY", "unit-test-dummy-key")
    ws = Workspace(name="Refresh WS", slug="refresh-ws", x_data_provider="tweetapi_com")
    db_session.add(ws)
    await db_session.flush()
    seed = TrackedAccount(workspace_id=ws.id, x_user_id="gregisenberg", handle="gregisenberg")
    untracked = TrackedAccount(
        workspace_id=ws.id, x_user_id="555", handle="dormant_one", is_tracked=False
    )
    db_session.add_all([seed, untracked])
    await db_session.commit()

    route = respx.get(f"{TWEETAPI_BASE_URL}/user/by-usernames").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "14642331",
                        "username": "GREGISENBERG",
                        "name": "GREG ISENBERG",
                        "bio": "ceo of late checkout",
                        "avatar": "https://pbs.twimg.com/a.jpg",
                        "followerCount": 512000,
                        "followingCount": 3100,
                    }
                ]
            },
        )
    )

    updated = await refresh_tracked_profiles(db_session, ws)

    assert updated == 1
    assert route.call_count == 1
    assert route.calls[0].request.url.params["usernames"] == "gregisenberg"
    assert seed.followers_count == 512000
    assert seed.following_count == 3100
    assert seed.x_user_id == "14642331"
    assert seed.avatar_url == "https://pbs.twimg.com/a.jpg"


async def test_refresh_tracked_profiles_skips_fixture_workspaces(db_session: AsyncSession) -> None:
    ws = Workspace(name="Demo WS", slug="demo-ws", x_data_provider="fixture")
    db_session.add(ws)
    await db_session.commit()
    assert await refresh_tracked_profiles(db_session, ws) == 0
