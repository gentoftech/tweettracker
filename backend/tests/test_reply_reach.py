"""Reply reach (issue #1): discovery and ranking measured from everyone's early replies.

1. Score scale, relative add/prune thresholds and follower-band / Premium matching.
2. Two-phase sampling of tracked accounts: early replies only, author/owner/platform excluded,
   views read after 24h, estimates computed, and strong thread repliers staged as candidates.
3. Candidate testing reads up to three 24-96h-old posts by the candidate itself.
4. Network sync with zero owner replies: measured low-reach seeds prune (no protection period),
   and a high-reach candidate that fails the relevance floor is not admitted.
5. Hot Posts author factor: measured reach, neutral when unmeasured, owner replies take over.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tweettracker.api.routes.radar import UNMEASURED_AUTHOR_FACTOR, author_reply_factor
from tweettracker.crypto import clear_runtime_secrets
from tweettracker.db import configure_database, dispose_database
from tweettracker.graph import reply_reach as rr
from tweettracker.models import (
    DiscoveredCandidate,
    PeerEndorsement,
    Post,
    ReplyReachSample,
    TrackedAccount,
    Workspace,
)
from tweettracker.providers.base import PostRecord, ProfileRecord, ReplyRecord
from tweettracker.seed import bootstrap_database

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def reach_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'reach.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("AUTO_MIGRATE", "true")
    monkeypatch.setenv("SEED_FIXTURES", "false")
    monkeypatch.setenv("TWEETTRACKER_VAULT_FILE", str(tmp_path / ".vault.key"))
    monkeypatch.delenv("TWEETTRACKER_SECRET_KEY", raising=False)
    monkeypatch.delenv("TWEETAPI_API_KEY", raising=False)
    clear_runtime_secrets()
    engine, session_factory = configure_database(db_url)
    await bootstrap_database(engine, session_factory, auto_migrate=True, seed_fixtures=False)
    yield session_factory
    clear_runtime_secrets()
    await dispose_database()


class FakeReachProvider:
    """Records calls; serves canned replies, matured views, timelines and the owner's profile."""

    def __init__(self) -> None:
        self.replies_by_post: dict[str, list[ReplyRecord]] = {}
        self.views_by_id: dict[str, int] = {}
        self.timelines: dict[str, list[PostRecord]] = {}
        self.owner_profile: ProfileRecord | None = None
        self.reply_calls: list[str] = []

    async def get_profiles_by_handles(self, _s: Any, _ws: Any, handles: list[str]) -> list[ProfileRecord]:
        return [self.owner_profile] if self.owner_profile else []

    async def get_tweet_replies(
        self, _s: Any, _ws: Any, tweet_id: str, *, sort_by: str = "Recency", cursor: str | None = None
    ) -> tuple[list[ReplyRecord], str | None]:
        self.reply_calls.append(tweet_id)
        return list(self.replies_by_post.get(tweet_id, [])), None

    async def get_posts_by_ids(
        self, _s: Any, _ws: Any, ids: list[str], *, ledger_label: str | None = None
    ) -> list[PostRecord]:
        return [
            PostRecord(x_post_id=i, author_x_user_id="", author_handle="", text="",
                       created_at_x=NOW, views=self.views_by_id[i])
            for i in ids
            if i in self.views_by_id
        ]

    async def get_user_tweets_and_replies(
        self, _s: Any, _ws: Any, target: str, *, count: int = 40, ledger_label: str | None = None
    ) -> list[PostRecord]:
        return list(self.timelines.get(target, []))


def _reply(rid: str, handle: str, created: datetime, *, followers: int = 8_000,
           premium: bool = True, views: int = 50) -> ReplyRecord:
    return ReplyRecord(
        x_post_id=rid, author_x_user_id=f"uid_{handle}", author_handle=handle,
        created_at_x=created, author_display_name=handle, author_followers=followers,
        author_premium=premium, views=views,
    )


def _sample(ws_id: int, author: str, i: int, views: int, *, followers: int = 8_000,
            premium: bool = True, matured: bool = True, parent_at: datetime = NOW) -> ReplyReachSample:
    return ReplyReachSample(
        workspace_id=ws_id, author_handle=author, parent_x_post_id=f"p_{author}",
        parent_created_at=parent_at, reply_x_post_id=f"r_{author}_{i}",
        replier_handle=f"rep_{author}_{i}", replier_x_user_id=f"ru_{author}_{i}",
        replier_display_name="r", replier_followers=followers, replier_premium=premium,
        reply_created_at=parent_at + timedelta(minutes=5 + i), minutes_after_parent=5 + i,
        views=views, matured=matured, sampled_at=parent_at, refreshed_at=parent_at,
    )


async def _workspace(session: AsyncSession, name: str, *, owner_followers: int = 0) -> Workspace:
    ws = Workspace(
        name=name, slug=name.lower().replace(" ", "-"), my_x_handle="owner_me",
        x_data_provider="tweetapi_com", llm_provider="google_gemini",
        monthly_unit_budget=100_000, daily_spend_cap_usd=50.0, monthly_spend_cap_usd=250.0,
        owner_followers_count=owner_followers,
    )
    session.add(ws)
    await session.flush()
    return ws


# ---------------------------------------------------------------- 1. scoring and matching


def test_score_scale_and_relative_thresholds() -> None:
    assert rr.reply_reach_score(None, 0.5) is None
    assert rr.reply_reach_score(10, 0.0) == 0.0
    assert rr.reply_reach_score(10_000, 1.0) == 1.0
    assert rr.reply_reach_score(1_000, 0.5) == pytest.approx(0.5 * (2 / 3) + 0.25, abs=1e-3)

    # Too few measured accounts: fixed add bar, fixed prune floor
    assert rr.reach_thresholds([0.9, 0.1]) == (rr.DEFAULT_ADD_THRESHOLD, rr.REACH_PRUNE_FLOOR)

    # Enough measured: add at the network median; prune stays at the fixed floor
    scores = [i / 10 for i in range(10)]  # 0.0 .. 0.9
    assert rr.reach_thresholds(scores) == (0.5, rr.REACH_PRUNE_FLOOR)

    # No ratchet: pruning the weakest accounts never raises the prune floor
    survivors = [x for x in scores if x >= rr.REACH_PRUNE_FLOOR]
    assert rr.reach_thresholds(survivors + [0.6, 0.7])[1] == rr.REACH_PRUNE_FLOOR

    # A weak network still keeps a gap between add and prune (hysteresis)
    add_w, prune_w = rr.reach_thresholds([0.05] * 12)
    assert add_w - prune_w >= rr.MIN_ADD_PRUNE_GAP - 1e-9


def test_follower_band_and_premium_matching_with_fallback() -> None:
    assert rr.follower_band(SimpleNamespace(owner_followers_count=0)) == rr.DEFAULT_FOLLOWER_BAND
    assert rr.follower_band(SimpleNamespace(owner_followers_count=5_000)) == (500, 50_000)

    band = (500, 50_000)
    in_band_premium = [_sample(1, "a", i, 1_000, premium=True) for i in range(5)]
    in_band_free = [_sample(1, "a", 10 + i, 20, premium=False) for i in range(5)]
    out_of_band = [_sample(1, "a", 20 + i, 90_000, followers=400_000) for i in range(5)]  # >10x
    unmatured = [_sample(1, "a", 30 + i, 90_000, matured=False) for i in range(5)]

    # Premium owner: compares only with Premium repliers in the band
    med, share, n = rr.estimate_from_samples(
        in_band_premium + in_band_free + out_of_band + unmatured, band, True
    )
    assert (med, share, n) == (1_000.0, 1.0, 5)

    # Non-Premium owner: only non-Premium repliers
    med, share, n = rr.estimate_from_samples(in_band_premium + in_band_free, band, False)
    assert (med, share, n) == (20.0, 0.0, 5)

    # Too few Premium matches: falls back to everyone in the band
    med, share, n = rr.estimate_from_samples(in_band_premium[:2] + in_band_free, band, True)
    assert n == 7

    # Too few in the band at all: no estimate
    assert rr.estimate_from_samples(in_band_premium[:4] + out_of_band, band, True) == (None, None, 4)


# ---------------------------------------------------------------- 2. two-phase sampling


@pytest.mark.asyncio
async def test_two_phase_sampling_measures_tracked_account_and_stages_thread_repliers(
    reach_env: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeReachProvider()
    fake.owner_profile = ProfileRecord(x_user_id="1", handle="owner_me", followers_count=6_000, is_premium=True)
    monkeypatch.setattr(rr, "get_x_data_provider", lambda _name: fake)

    async with reach_env() as session:
        ws = await _workspace(session, "Two Phase")
        voice = TrackedAccount(
            workspace_id=ws.id, x_user_id="77", handle="big_voice", display_name="Big Voice",
            followers_count=90_000, is_tracked=True,
        )
        session.add(voice)
        await session.flush()
        parent_at = NOW - timedelta(minutes=90)
        session.add(Post(
            workspace_id=ws.id, x_post_id="parent_1", author_account_id=voice.id,
            text="A post", created_at_x=parent_at, latest_views=30_000, latest_replies=40,
        ))
        await session.commit()

        good = [_reply(f"g{i}", f"peer_{i}", parent_at + timedelta(minutes=3 + 5 * i)) for i in range(6)]
        fake.replies_by_post["parent_1"] = good + [
            _reply("self", "big_voice", parent_at + timedelta(minutes=1)),
            _reply("mine", "owner_me", parent_at + timedelta(minutes=2)),
            _reply("bot", "grok", parent_at + timedelta(minutes=2)),
            _reply("late", "late_peer", parent_at + timedelta(minutes=75)),
            _reply("huge", "huge_peer", parent_at + timedelta(minutes=4), followers=900_000),
        ]

        # Phase 1: early replies collected, views not final yet, so nothing is measured
        s1 = await rr.run_reply_reach_cycle(session, ws, now=NOW, throttle_mode="normal")
        await session.commit()
        assert s1["posts_sampled"] == 1
        assert (ws.owner_followers_count, ws.owner_is_premium) == (6_000, True)
        stored = (await session.execute(select(ReplyReachSample))).scalars().all()
        assert {s.replier_handle for s in stored} == {f"peer_{i}" for i in range(6)} | {"huge_peer"}
        assert not any(s.matured for s in stored)
        await session.refresh(voice)
        assert not rr.is_measured(voice)

        # Re-running inside the resample window doesn't spend another read
        await rr.run_reply_reach_cycle(session, ws, now=NOW + timedelta(minutes=30), throttle_mode="normal")
        assert fake.reply_calls == ["parent_1"]

        # Phase 2: a day later the views are read and the account is measured
        fake.views_by_id = {f"g{i}": v for i, v in enumerate([800, 900, 1_200, 300, 2_000, 650])}
        fake.views_by_id["huge"] = 50_000
        s2 = await rr.run_reply_reach_cycle(session, ws, now=NOW + timedelta(hours=25), throttle_mode="normal")
        await session.commit()
        assert s2["samples_matured"] == 7
        await session.refresh(voice)
        assert rr.is_measured(voice)
        assert voice.reply_reach_samples == 6  # the 900k-follower replier is outside the band
        assert voice.reply_reach_median == pytest.approx(850.0)
        assert voice.reply_reach_share_500 == pytest.approx(5 / 6)
        assert voice.reply_reach_score == rr.reply_reach_score(850.0, 5 / 6)

        # Repliers whose early replies cleared 500 views become candidates endorsed by big_voice
        cands = {c.handle for c in (await session.execute(select(DiscoveredCandidate))).scalars().all()}
        assert {"peer_0", "peer_1", "peer_2", "peer_4", "peer_5", "huge_peer"} <= cands
        assert "peer_3" not in cands  # 300 views
        ends = (await session.execute(select(PeerEndorsement))).scalars().all()
        assert ends and all(e.endorsement_type == "reply" and e.endorser_account_id == voice.id for e in ends)


@pytest.mark.asyncio
async def test_candidate_test_uses_candidates_own_post_in_age_window(
    reach_env: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeReachProvider()
    monkeypatch.setattr(rr, "get_x_data_provider", lambda _name: fake)

    def _post(pid: str, author_id: str, age_h: float, *, replies: int = 10, is_reply: bool = False) -> PostRecord:
        return PostRecord(
            x_post_id=pid, author_x_user_id=author_id, author_handle="", text="",
            created_at_x=NOW - timedelta(hours=age_h), replies=replies, is_reply=is_reply,
        )

    fake.timelines["555"] = [
        _post("too_young", "555", 3),
        _post("pinned_other", "999", 48),  # someone else's post on the timeline
        _post("a_reply", "555", 48, is_reply=True),
        _post("huge_thread", "555", 60, replies=400),
        _post("quiet", "555", 50, replies=12),
        _post("lively", "555", 30, replies=40),
        _post("too_old", "555", 200),
    ]
    async with reach_env() as session:
        ws = await _workspace(session, "Candidate Test")
        cand = DiscoveredCandidate(workspace_id=ws.id, x_user_id="555", handle="rising_peer",
                                   display_name="Rising", followers_count=20_000, status="active")
        session.add(cand)
        await session.flush()
        tested = await rr.test_candidates(session, ws, [cand], now=NOW, throttle_mode="normal")
        assert tested == 1
        # The candidate's own original posts in the age window: busiest readable threads first,
        # oversized threads last
        assert fake.reply_calls == ["lively", "quiet", "huge_thread"]

        # Tested candidates wait three days before another test; a paused budget tests nobody
        assert await rr.test_candidates(session, ws, [cand], now=NOW + timedelta(days=2), throttle_mode="normal") == 0
        assert await rr.test_candidates(session, ws, [cand], now=NOW + timedelta(days=8), throttle_mode="paused") == 0


# ---------------------------------------------------------------- 4. network sync


@pytest.mark.asyncio
async def test_sync_prunes_low_reach_seeds_and_respects_relevance_floor_with_zero_owner_replies(
    reach_env: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tweettracker.graph import live_crawler
    from tweettracker.rising import discovery

    async def _noop_crawl(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {}

    async def _no_profiles(*_a: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr(live_crawler, "crawl_live_seed_network", _noop_crawl)
    monkeypatch.setattr(live_crawler, "refresh_tracked_profiles", _no_profiles)
    monkeypatch.setattr(discovery, "sync_own_reply_yield", _noop_crawl)
    fake = FakeReachProvider()
    monkeypatch.setattr(rr, "get_x_data_provider", lambda _name: fake)

    async with reach_env() as session:
        ws = await _workspace(session, "Reach Sync")
        seeds: list[TrackedAccount] = []
        for i in range(12):
            acct = TrackedAccount(
                workspace_id=ws.id, x_user_id=f"{100 + i}", handle=f"seed_{i}", display_name=f"Seed {i}",
                followers_count=30_000, is_tracked=True, is_auto_tracked=False,
                created_at=NOW - timedelta(days=2),
            )
            session.add(acct)
            seeds.append(acct)
        session.add(DiscoveredCandidate(
            workspace_id=ws.id, x_user_id="900", handle="offtopic_star", display_name="Off-topic Star",
            followers_count=40_000, status="active",
        ))
        await session.flush()
        # Seeds 0-9: early replies do well. Seeds 10-11: they go unseen. Nobody has an owner reply.
        for i, acct in enumerate(seeds):
            views = 2_000 if i < 10 else 20
            for k in range(6):
                session.add(_sample(ws.id, acct.handle, k, views))
        for k in range(6):
            session.add(_sample(ws.id, "offtopic_star", k, 5_000))
        await session.flush()
        await rr.recompute_reach_estimates(session, ws, now=NOW)
        await session.commit()

        res = await discovery.auto_sync_workspace_network(session, ws.id, now=NOW)

    assert set(res["auto_pruned_handles"]) == {"seed_10", "seed_11"}
    assert res["reach_prune_threshold"] < res["reach_add_threshold"]
    # Strong reach alone doesn't admit an account with no tie to the network
    assert "offtopic_star" not in res["auto_added_handles"]
    assert res["own_replies_synced"] == 0


# ---------------------------------------------------------------- 5. Hot Posts author factor


def test_hot_posts_author_factor_blends_reach_with_owner_replies() -> None:
    def _author(score: float | None, samples: int, owner_replies: int = 0, yield_score: float = 0.0) -> Any:
        return SimpleNamespace(
            reply_reach_score=score, reply_reach_samples=samples,
            reply_sample_count=owner_replies, yield_score=yield_score,
        )

    # Measured, zero owner replies: the measured reach decides
    assert author_reply_factor(_author(0.8, 6)) == pytest.approx(0.8)
    # Unmeasured: neutral, regardless of similarity to the seeds
    assert author_reply_factor(_author(None, 0)) == pytest.approx(UNMEASURED_AUTHOR_FACTOR)
    assert author_reply_factor(_author(0.9, 3)) == pytest.approx(UNMEASURED_AUTHOR_FACTOR)
    # Owner replies adjust it, and fully replace it at 5 replies
    assert author_reply_factor(_author(0.8, 6, owner_replies=1, yield_score=0.3)) == pytest.approx(0.7)
    assert author_reply_factor(_author(0.8, 6, owner_replies=5, yield_score=0.3)) == pytest.approx(0.3)
