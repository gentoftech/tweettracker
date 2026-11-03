"""Issue #1 Test Suite: Reply-Yield Core Learning, 28d Seed Bootstrap Decay, SimClusters Affinity & Unit-Budget Pacing.

Covers:
1. Zero follower-size cliffs (`8k`, `1.5M`, `3.2M` core-affiliated voices promoted; `15k` & `5M` passive follows rejected).
2. Zero fixed per-sync promotion caps (`18` qualifying candidates promoted in a single sync).
3. Hysteresis affinity pruning (`< 0.32` after `48h` grace) + cold-start co-follow decay (`gamma_boot`) + hysteresis stability band (`[0.32, 0.55)`).
4. Multi-sync convergence (`adds + prunes -> 0`) and persistent cross-sync `PeerEndorsement` evidence accumulation.
5. Bootstrap seed `28`-day grace protection + no per-sync or total tracked-account cap, followed by post-`28d` low-yield / dormancy fade & pruning (`MIN_ACTIVE_CORE_FLOOR = 10` unattended safety floor).
6. `100,000` monthly unit budget projection, `$0.00025`/unit parity, `throttled` (`>=80%`) and `paused` (`>=95%`) pacing, and `SpendCapExceededError` enforcement.
7. Per-account reply/quote yield ingestion (`/tw-v2/user/tweets-and-replies` + `OwnFollowerSnapshot` + `2h-48h` batch refresh) and impression-weighted `[0h, 24h]` follower attribution.
8. Yield-learned core (`C_yield`) driving crawl priority (`crawl_live_seed_network`) and 2-hop candidate discovery (`refresh_rising_candidates`).
9. `Hot Posts` (`live_queue=true`) ranking by `expected_reply_reach_score` to maximize reach per reply.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tweettracker.api.main import create_app
from tweettracker.crypto import clear_runtime_secrets
from tweettracker.db import configure_database, dispose_database
from tweettracker.graph.live_crawler import crawl_live_seed_network
from tweettracker.graph.reply_yield import (
    MIN_ACTIVE_CORE_FLOOR,
    SEED_BOOTSTRAP_GRACE_DAYS,
)
from tweettracker.graph.service import recompute_workspace_graph
from tweettracker.models import (
    AccountCommunityMembership,
    ApiCreditLedger,
    Community,
    DiscoveredCandidate,
    OwnFollowerSnapshot,
    OwnReplyOutcome,
    PeerEndorsement,
    Post,
    RelationshipEdge,
    TrackedAccount,
    Workspace,
)
from tweettracker.providers.base import SpendCapExceededError
from tweettracker.providers.spend import (
    enforce_spend_caps,
    estimate_workspace_monthly_units,
)
from tweettracker.providers.tweetapi import DEFAULT_USD_PER_UNIT
from tweettracker.rising.discovery import (
    CORE_AFFINITY_ADD_THRESHOLD,
    CORE_AFFINITY_PRUNE_THRESHOLD,
    CORE_AFFINITY_THROTTLED_ADD_THRESHOLD,
    auto_sync_workspace_network,
    refresh_rising_candidates,
)
from tweettracker.seed import bootstrap_database


@pytest.fixture
async def issue1_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncGenerator[tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]], None]:
    db_file = tmp_path / "issue1_affinity.db"
    vault_file = tmp_path / ".issue1_vault.key"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("AUTO_MIGRATE", "true")
    monkeypatch.setenv("SEED_FIXTURES", "false")
    monkeypatch.setenv("TWEETTRACKER_VAULT_FILE", str(vault_file))
    monkeypatch.delenv("TWEETTRACKER_SECRET_KEY", raising=False)
    monkeypatch.delenv("TWEETTRACKER_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("TWEETAPI_API_KEY", raising=False)

    clear_runtime_secrets()
    engine, session_factory = configure_database(db_url)
    await bootstrap_database(engine, session_factory, auto_migrate=True, seed_fixtures=False)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, session_factory

    clear_runtime_secrets()
    await dispose_database()


async def _seed_core_workspace(
    session: AsyncSession,
    *,
    name: str = "Affinity Core Lab",
    provider: str = "fixture",
    n_seeds: int = 4,
    monthly_unit_budget: int = 100_000,
    now: datetime,
) -> tuple[Workspace, Community, list[TrackedAccount]]:
    ws = Workspace(
        name=name,
        slug=name.lower().replace(" ", "-"),
        my_x_handle="operator_hq",
        x_data_provider=provider,
        llm_provider="google_gemini",
        monthly_unit_budget=monthly_unit_budget,
        daily_spend_cap_usd=50.0,
        monthly_spend_cap_usd=250.0,
    )
    session.add(ws)
    await session.flush()

    comm = Community(
        workspace_id=ws.id,
        name="Core Circle",
        slug="core-circle",
        color="#38bdf8",
        description="Core anchor cluster",
        sort_order=0,
    )
    session.add(comm)
    await session.flush()

    seeds: list[TrackedAccount] = []
    for idx in range(n_seeds):
        acct = TrackedAccount(
            workspace_id=ws.id,
            x_user_id=f"seed_uid_{idx + 1}",
            handle=f"core_seed_{idx + 1}",
            display_name=f"Core Seed {idx + 1}",
            bio="Manual core anchor",
            followers_count=25_000 + idx * 10_000,
            following_count=450,
            is_tracked=True,
            is_auto_tracked=False,
            follows_me=(idx < 2),
            followed_by_me=(idx < 2),
        )
        session.add(acct)
        await session.flush()
        session.add(
            AccountCommunityMembership(
                workspace_id=ws.id,
                account_id=acct.id,
                community_id=comm.id,
                is_manual_tag=True,
                nmf_known_for=1.0,
                nmf_interested_in=1.0,
            )
        )
        seeds.append(acct)

    # Connect core seeds conversationally so PageRank & Louvain establish a cohesive core
    for i in range(n_seeds):
        nxt = seeds[(i + 1) % n_seeds]
        if nxt.id != seeds[i].id:
            session.add(
                RelationshipEdge(
                    workspace_id=ws.id,
                    source_account_id=seeds[i].id,
                    target_account_id=nxt.id,
                    edge_type="quote",
                    interaction_count=2,
                    decayed_weight=10.0,
                    last_event_at=now - timedelta(hours=6),
                )
            )

    await session.flush()
    await recompute_workspace_graph(session, ws.id, commit=True, reference_time=now)
    return ws, comm, seeds


@pytest.mark.asyncio
async def test_zero_follower_size_cliff_behavioral_core_affinity(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """3.2M-follower core-engaged heavy hitter IS promoted; 15k & 5M passive follows are NOT."""
    client, session_factory = issue1_env
    now = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        ws, _comm, seeds = await _seed_core_workspace(session, n_seeds=4, now=now)
        ws_id = ws.id

        # Candidate A: 3,200,000 followers (above old 500k AND 2M hard cliffs!), actively quoted/replied to by 3 core seeds
        mega_core_peer = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="cand_mega_core_3m",
            handle="mega_core_founder_3m",
            display_name="Mega Core Founder",
            bio="Deep technical AI builder with 3.2M followers",
            followers_count=3_200_000,
            following_count=820,
            status="over_cap",
        )
        # Candidate B: 8,500 followers (<50k rising), actively engaged by 3 core seeds
        rising_core_peer = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="cand_rising_8k",
            handle="rising_researcher_8k",
            display_name="Rising Researcher",
            bio="Systems researcher",
            followers_count=8_500,
            following_count=310,
            status="active",
        )
        # Candidate C: 5,000,000 followers, passive follow by only 2 seeds (zero replies/quotes)
        passive_celeb = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="cand_passive_5m",
            handle="passive_popstar_5m",
            display_name="Passive Popstar",
            bio="Generic celebrity",
            followers_count=5_000_000,
            following_count=50,
            status="over_cap",
        )
        # Candidate D: 15,000 followers, ALSO passive follow by only 2 seeds (proving rejection is behavioral, not size-based)
        passive_small = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="cand_passive_15k",
            handle="passive_tourist_15k",
            display_name="Passive Tourist",
            bio="Unengaged bystander",
            followers_count=15_000,
            following_count=400,
            status="active",
        )
        session.add_all([mega_core_peer, rising_core_peer, passive_celeb, passive_small])
        await session.flush()

        for s in seeds[:3]:
            for cand in (mega_core_peer, rising_core_peer):
                session.add(
                    PeerEndorsement(
                        workspace_id=ws.id,
                        candidate_id=cand.id,
                        endorser_account_id=s.id,
                        endorsement_type="quote",
                        weight_contribution=15.0,
                        source_x_post_id=f"q_{s.id}_{cand.id}",
                        occurred_at=now - timedelta(hours=4),
                    )
                )
                session.add(
                    PeerEndorsement(
                        workspace_id=ws.id,
                        candidate_id=cand.id,
                        endorser_account_id=s.id,
                        endorsement_type="follow",
                        weight_contribution=2.0,
                        occurred_at=now - timedelta(hours=12),
                    )
                )

        for s in seeds[:2]:
            for cand in (passive_celeb, passive_small):
                session.add(
                    PeerEndorsement(
                        workspace_id=ws.id,
                        candidate_id=cand.id,
                        endorser_account_id=s.id,
                        endorsement_type="follow",
                        weight_contribution=2.0,
                        occurred_at=now - timedelta(hours=12),
                    )
                )
        await session.commit()

        sync_res = await auto_sync_workspace_network(session, ws_id, now=now)
        assert "mega_core_founder_3m" in sync_res["auto_added_handles"]
        assert "rising_researcher_8k" in sync_res["auto_added_handles"]
        assert "passive_popstar_5m" not in sync_res["auto_added_handles"]
        assert "passive_tourist_15k" not in sync_res["auto_added_handles"]

    # Verify API surfaces core_affinity_score and scopes rising_only strictly to <50k
    all_resp = await client.get(f"/api/workspaces/{ws_id}/network/accounts")
    assert all_resp.status_code == 200
    by_h = {a["handle"]: a for a in all_resp.json()}
    assert by_h["mega_core_founder_3m"]["followers_count"] == 3_200_000
    assert by_h["mega_core_founder_3m"]["core_affinity_score"] >= CORE_AFFINITY_ADD_THRESHOLD
    assert by_h["mega_core_founder_3m"]["status_badge"] == "auto"
    assert by_h["rising_researcher_8k"]["status_badge"] == "rising"

    rising_resp = await client.get(f"/api/workspaces/{ws_id}/network/accounts?rising_only=true")
    rising_handles = {a["handle"] for a in rising_resp.json()}
    assert "rising_researcher_8k" in rising_handles
    assert "mega_core_founder_3m" not in rising_handles


@pytest.mark.asyncio
async def test_no_fixed_per_sync_cap_promotes_all_qualifying_core_candidates(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """All 18 qualifying candidates promote in ONE sync (no max_snowball=6 or MAX_AUTO_PROMOTIONS=8 cap)."""
    _client, session_factory = issue1_env
    now = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        ws, _comm, seeds = await _seed_core_workspace(session, name="Uncapped Batch", n_seeds=4, now=now)

        for idx in range(18):
            followers = 6_000 + idx * 18_000  # Spans 6k up to 312k across <50k and >=50k
            cand = DiscoveredCandidate(
                workspace_id=ws.id,
                x_user_id=f"batch_cand_{idx}",
                handle=f"core_builder_{idx}",
                display_name=f"Core Builder {idx}",
                followers_count=followers,
                following_count=300,
                status="active" if followers < 50_000 else "over_cap",
            )
            session.add(cand)
            await session.flush()

            for s in seeds[:3]:
                session.add(
                    PeerEndorsement(
                        workspace_id=ws.id,
                        candidate_id=cand.id,
                        endorser_account_id=s.id,
                        endorsement_type="reply",
                        weight_contribution=10.0,
                        source_x_post_id=f"rep_{s.id}_{idx}",
                        occurred_at=now - timedelta(hours=3),
                    )
                )
                session.add(
                    PeerEndorsement(
                        workspace_id=ws.id,
                        candidate_id=cand.id,
                        endorser_account_id=s.id,
                        endorsement_type="follow",
                        weight_contribution=2.0,
                        occurred_at=now - timedelta(hours=8),
                    )
                )
        await session.commit()

        sync_res = await auto_sync_workspace_network(session, ws.id, now=now)
        assert sync_res["auto_added_count"] == 18
        assert len(sync_res["auto_added_handles"]) == 18


@pytest.mark.asyncio
async def test_hysteresis_affinity_prune_and_cold_start_cofollow_decay(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """Off-core auto-adds prune after 48h even if active today; cold-start co-follows decay if unengaged."""
    _client, session_factory = issue1_env
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        ws, comm, seeds = await _seed_core_workspace(session, name="Hysteresis Lab", n_seeds=6, now=t0)

        # 1. Off-core auto-tracked account that posted 1h ago (<30d!), inside 48h grace at t0 + 12h
        drifted_acct = TrackedAccount(
            workspace_id=ws.id,
            x_user_id="drifted_uid_1",
            handle="drifted_voice",
            display_name="Drifted Voice",
            followers_count=18_000,
            following_count=200,
            is_tracked=True,
            is_auto_tracked=True,
            auto_tracked_at=t0,
        )
        # 2. Cold-start co-follow-only auto-tracked account: followed by all 6 core seeds, 0 replies/quotes
        cofollow_only_acct = TrackedAccount(
            workspace_id=ws.id,
            x_user_id="cofollow_uid_2",
            handle="cofollow_bootstrap",
            display_name="CoFollow Bootstrap",
            followers_count=90_000,
            following_count=400,
            is_tracked=True,
            is_auto_tracked=True,
            auto_tracked_at=t0,
        )
        # 3. Hysteresis-band auto-tracked account: moderate conversational tie keeping score in [0.32, 0.55)
        band_acct = TrackedAccount(
            workspace_id=ws.id,
            x_user_id="band_uid_3",
            handle="hysteresis_steady",
            display_name="Hysteresis Steady",
            followers_count=22_000,
            following_count=300,
            is_tracked=True,
            is_auto_tracked=True,
            auto_tracked_at=t0 - timedelta(days=5),
        )
        session.add_all([drifted_acct, cofollow_only_acct, band_acct])
        await session.flush()

        for a in (drifted_acct, cofollow_only_acct, band_acct):
            session.add(
                AccountCommunityMembership(
                    workspace_id=ws.id,
                    account_id=a.id,
                    community_id=comm.id,
                    is_manual_tag=False,
                    nmf_known_for=0.5,
                    nmf_interested_in=0.5,
                )
            )
            # Recent post so none of these are 30d-inactive!
            session.add(
                Post(
                    workspace_id=ws.id,
                    x_post_id=f"recent_post_{a.id}",
                    author_account_id=a.id,
                    text=f"Active post by @{a.handle}",
                    created_at_x=t0,
                    latest_views=4_000,
                )
            )

        # Wire 6 passive follows -> cofollow_only_acct (strong at t0, decays after 48h without conversation)
        for s in seeds:
            session.add(
                RelationshipEdge(
                    workspace_id=ws.id,
                    source_account_id=s.id,
                    target_account_id=cofollow_only_acct.id,
                    edge_type="follow",
                    interaction_count=1,
                    decayed_weight=2.0,
                    last_event_at=t0,
                )
            )

        # Wire 1 reply from seeds[0] -> band_acct so its affinity sits inside [0.32, 0.55)
        session.add(
            RelationshipEdge(
                workspace_id=ws.id,
                source_account_id=seeds[0].id,
                target_account_id=band_acct.id,
                edge_type="reply",
                interaction_count=1,
                decayed_weight=5.0,
                last_event_at=t0,
            )
        )
        await session.commit()

        # Pass A at t0 + 12h (inside 48h grace window): neither drifted_voice nor cofollow_bootstrap is pruned
        t_12h = t0 + timedelta(hours=12)
        res_12h = await auto_sync_workspace_network(session, ws.id, now=t_12h)
        assert "drifted_voice" not in res_12h["auto_pruned_handles"]
        assert "cofollow_bootstrap" not in res_12h["auto_pruned_handles"]

        await session.refresh(cofollow_only_acct)
        assert cofollow_only_acct.core_affinity_score >= 0.45

        # Pass B at t0 + 60h (past 48h grace): drifted_voice (< 0.32) IS pruned despite recent post;
        # hysteresis_steady (in [0.32, 0.55)) stays tracked without flip-flopping!
        t_60h = t0 + timedelta(hours=60)
        res_60h = await auto_sync_workspace_network(session, ws.id, now=t_60h)
        assert "drifted_voice" in res_60h["auto_pruned_handles"]
        assert "hysteresis_steady" not in res_60h["auto_pruned_handles"]

        await session.refresh(band_acct)
        assert CORE_AFFINITY_PRUNE_THRESHOLD <= band_acct.core_affinity_score < CORE_AFFINITY_ADD_THRESHOLD

        # Pass C at t0 + 11 days: cofollow_bootstrap has had 0 conversational interactions -> gamma_boot decays it < 0.32 -> pruned!
        for a in (cofollow_only_acct, band_acct):
            session.add(
                Post(
                    workspace_id=ws.id,
                    x_post_id=f"fresh_post_11d_{a.id}",
                    author_account_id=a.id,
                    text=f"Still tweeting at day 11 @{a.handle}",
                    created_at_x=t0 + timedelta(days=10),
                    latest_views=3_500,
                )
            )
        # Keep band_acct's reply fresh so only cofollow_bootstrap decays
        band_edge = (
            await session.execute(
                select(RelationshipEdge).where(RelationshipEdge.target_account_id == band_acct.id)
            )
        ).scalar_one()
        band_edge.last_event_at = t0 + timedelta(days=10)
        await session.commit()

        t_11d = t0 + timedelta(days=11)
        res_11d = await auto_sync_workspace_network(session, ws.id, now=t_11d)
        assert "cofollow_bootstrap" in res_11d["auto_pruned_handles"]
        assert "hysteresis_steady" not in res_11d["auto_pruned_handles"]


@pytest.mark.asyncio
async def test_multi_sync_convergence_and_cross_sync_evidence_accumulation(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """Endorsements accumulate across syncs without wiping prior evidence, and adds+prunes converge to 0."""
    client, session_factory = issue1_env
    t0 = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)

    await client.put(
        "/api/settings/secrets",
        json={"secrets": {"TWEETAPI_API_KEY": "test-convergence-key-7777"}},
    )

    async with session_factory() as session:
        ws, _comm, seeds = await _seed_core_workspace(
            session, name="Convergence Live", provider="tweetapi_com", n_seeds=4, now=t0
        )
        ws_id = ws.id
        # Mark seeds 2..4 as recently crawled so Sync 1 crawls ONLY core_seed_1,
        # then at t0 + 13h (>12h cooldown) Sync 2 crawls the remaining seeds!
        seeds[0].last_follow_synced_at = None
        for s in seeds[1:]:
            s.last_follow_synced_at = t0 - timedelta(hours=2)
        await session.commit()

    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.tweetapi.com/tw-v2/user/by-username").mock(
            return_value=httpx.Response(200, json={"data": {"id": "999", "username": "operator_hq"}})
        )
        router.get("https://api.tweetapi.com/tw-v2/user/tweets-and-replies").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        router.get("https://api.tweetapi.com/tw-v2/user/by-usernames").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "555",
                            "username": "shared_core_peer",
                            "name": "Shared Core Peer",
                            "followers_count": 78_000,
                            "following_count": 410,
                        }
                    ]
                },
            )
        )

        def _following_handler(request: httpx.Request) -> httpx.Response:
            uid = request.url.params.get("userId") or ""
            if uid == "999":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "555",
                            "username": "shared_core_peer",
                            "name": "Shared Core Peer",
                            "followers_count": 78_000,
                            "following_count": 410,
                        }
                    ]
                },
            )

        router.get("https://api.tweetapi.com/tw-v2/user/following-list").mock(
            side_effect=_following_handler
        )

        sync_phase = {"step": 1}

        def _search_handler(_request: httpx.Request) -> httpx.Response:
            if sync_phase["step"] == 1:
                # Sync 1: only core_seed_1 has replied so far -> staged, below 0.55 threshold
                return httpx.Response(200, json={"data": []})
            # Sync 2+: core_seed_2 and core_seed_3 quote @shared_core_peer
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "9001",
                            "text": "Deep dive with @shared_core_peer on distributed kernels",
                            "created_at": "2026-11-02T22:30:00Z",
                            "is_quote_status": True,
                            "author": {"id": "seed_uid_2", "username": "core_seed_2"},
                            "views_count": 12_000,
                        },
                        {
                            "id": "9002",
                            "text": "Agree with @shared_core_peer on latency budgets",
                            "created_at": "2026-11-02T22:45:00Z",
                            "is_quote_status": True,
                            "author": {"id": "seed_uid_3", "username": "core_seed_3"},
                            "views_count": 14_500,
                        },
                    ]
                },
            )

        router.get("https://api.tweetapi.com/tw-v2/search").mock(side_effect=_search_handler)

        async with session_factory() as session:
            # Sync 1 at t0: crawls core_seed_1 (1 passive follow) -> staged in DiscoveredCandidate, 0 promotions
            s1 = await auto_sync_workspace_network(session, ws_id, now=t0)
            assert s1["auto_added_count"] == 0

            # Sync 2 at t0 + 13h: crawls core_seed_2..4 + quotes, accumulating onto Sync 1's endorsement -> promotes!
            sync_phase["step"] = 2
            t_sync2 = t0 + timedelta(hours=13)
            s2 = await auto_sync_workspace_network(session, ws_id, now=t_sync2)
            assert "shared_core_peer" in s2["auto_added_handles"]
            assert s2["auto_pruned_count"] == 0

            # Sync 3 at t0 + 14h: core has stabilized -> 0 adds and 0 prunes (strict convergence!)
            t_sync3 = t0 + timedelta(hours=14)
            s3 = await auto_sync_workspace_network(session, ws_id, now=t_sync3)
            assert s3["auto_added_count"] == 0
            assert s3["auto_pruned_count"] == 0

            # Sync 4 at t0 + 15h: remains converged at 0 changes
            t_sync4 = t0 + timedelta(hours=15)
            s4 = await auto_sync_workspace_network(session, ws_id, now=t_sync4)
            assert s4["auto_added_count"] == 0
            assert s4["auto_pruned_count"] == 0


@pytest.mark.asyncio
async def test_manual_seed_protection_and_no_tracked_account_cap(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """Bootstrap seeds inside their 28-day grace period are protected; a qualifying candidate is added without pushing anyone out."""
    _client, session_factory = issue1_env
    now = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        # 4 manual seeds + 2 existing auto-tracked accounts
        ws, comm, seeds = await _seed_core_workspace(
            session,
            name="No Cap Lab",
            n_seeds=4,
            now=now,
        )

        # Make manual seed #4 completely quiet (>45 days old post, 0 inbound edges) to prove manual seed immunity
        session.add(
            Post(
                workspace_id=ws.id,
                x_post_id="old_manual_post",
                author_account_id=seeds[3].id,
                text="Quiet manual seed post from 45 days ago",
                created_at_x=now - timedelta(days=45),
                latest_views=500,
            )
        )

        # Auto-tracked #1: moderate affinity (~0.42, inside the hysteresis band, so never pruned)
        weaker_auto = TrackedAccount(
            workspace_id=ws.id,
            x_user_id="auto_weak_1",
            handle="auto_moderate_peer",
            display_name="Auto Moderate Peer",
            followers_count=30_000,
            following_count=250,
            is_tracked=True,
            is_auto_tracked=True,
            auto_tracked_at=now - timedelta(days=4),
        )
        # Auto-tracked #2: very strong affinity (~0.75)
        strong_auto = TrackedAccount(
            workspace_id=ws.id,
            x_user_id="auto_strong_2",
            handle="auto_strong_peer",
            display_name="Auto Strong Peer",
            followers_count=65_000,
            following_count=400,
            is_tracked=True,
            is_auto_tracked=True,
            auto_tracked_at=now - timedelta(days=4),
        )
        session.add_all([weaker_auto, strong_auto])
        await session.flush()

        for a in (weaker_auto, strong_auto):
            session.add(
                AccountCommunityMembership(
                    workspace_id=ws.id,
                    account_id=a.id,
                    community_id=comm.id,
                    is_manual_tag=False,
                    nmf_known_for=0.6,
                    nmf_interested_in=0.6,
                )
            )
            session.add(
                Post(
                    workspace_id=ws.id,
                    x_post_id=f"active_cap_post_{a.id}",
                    author_account_id=a.id,
                    text="Recent post",
                    created_at_x=now - timedelta(hours=3),
                    latest_views=5_000,
                )
            )

        # 1 reply -> weaker_auto (~0.42 affinity); 4 quotes -> strong_auto (~0.78 affinity)
        session.add(
            RelationshipEdge(
                workspace_id=ws.id,
                source_account_id=seeds[0].id,
                target_account_id=weaker_auto.id,
                edge_type="reply",
                interaction_count=1,
                decayed_weight=5.0,
                last_event_at=now - timedelta(hours=5),
            )
        )
        for s in seeds:
            session.add(
                RelationshipEdge(
                    workspace_id=ws.id,
                    source_account_id=s.id,
                    target_account_id=strong_auto.id,
                    edge_type="quote",
                    interaction_count=2,
                    decayed_weight=10.0,
                    last_event_at=now - timedelta(hours=2),
                )
            )

        # Stage elite candidate quoted + followed by all 4 core seeds (affinity ~0.82)
        elite_cand = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="cand_elite_99",
            handle="elite_core_challenger",
            display_name="Elite Core Challenger",
            followers_count=120_000,
            following_count=510,
            status="over_cap",
        )
        session.add(elite_cand)
        await session.flush()

        for s in seeds:
            session.add(
                PeerEndorsement(
                    workspace_id=ws.id,
                    candidate_id=elite_cand.id,
                    endorser_account_id=s.id,
                    endorsement_type="quote",
                    weight_contribution=15.0,
                    source_x_post_id=f"el_q_{s.id}",
                    occurred_at=now - timedelta(hours=1),
                )
            )
            session.add(
                PeerEndorsement(
                    workspace_id=ws.id,
                    candidate_id=elite_cand.id,
                    endorser_account_id=s.id,
                    endorsement_type="follow",
                    weight_contribution=2.0,
                    occurred_at=now - timedelta(hours=3),
                )
            )
        await session.commit()

        sync_res = await auto_sync_workspace_network(session, ws.id, now=now)
        # No cap: the elite candidate is added and nobody is pushed out to make room
        assert sync_res["active_tracked_count"] == 7
        assert "elite_core_challenger" in sync_res["auto_added_handles"]
        assert sync_res["auto_pruned_handles"] == []
        # Manual quiet seed #4 MUST remain tracked!
        await session.refresh(seeds[3])
        assert seeds[3].is_tracked is True
        assert seeds[3].handle not in sync_res["auto_pruned_handles"]


@pytest.mark.asyncio
async def test_monthly_unit_budget_projection_throttling_and_pause(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """Verifies $0.00025/unit parity, 80% throttled mode, 95% paused mode, and hard unit-cap enforcement."""
    _client, session_factory = issue1_env
    # Mid-month timestamp (day 15 of 30-day window -> elapsed ~14.5 days)
    now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)

    assert DEFAULT_USD_PER_UNIT == pytest.approx(0.00025)

    async with session_factory() as session:
        ws, _comm, _seeds = await _seed_core_workspace(
            session,
            name="Budget Pacing Lab",
            provider="tweetapi_com",
            n_seeds=3,
            monthly_unit_budget=100_000,
            now=now,
        )

        # 1. Log 83,000 units already consumed this month -> projected >= 80% ("throttled")
        session.add(
            ApiCreditLedger(
                workspace_id=ws.id,
                provider="tweetapi_com",
                endpoint="/tw-v2/search",
                units_consumed=83_000,
                estimated_usd=83_000 * DEFAULT_USD_PER_UNIT,
                created_at=now - timedelta(days=1),
            )
        )
        await session.commit()

        est_throttled = await estimate_workspace_monthly_units(session, ws, now=now)
        assert est_throttled["discovery_throttle_mode"] == "throttled"
        assert est_throttled["projected_monthly_pct"] >= 80.0

        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.tweetapi.com/tw-v2/user/by-username").mock(
                return_value=httpx.Response(200, json={"data": {"id": "999", "username": "operator_hq"}})
            )
            router.get("https://api.tweetapi.com/tw-v2/user/following-list").mock(
                return_value=httpx.Response(200, json={"data": []})
            )
            router.get("https://api.tweetapi.com/tw-v2/search").mock(
                return_value=httpx.Response(200, json={"data": []})
            )
            router.get("https://api.tweetapi.com/tw-v2/user/tweets-and-replies").mock(
                return_value=httpx.Response(200, json={"data": []})
            )

            sync_thr = await auto_sync_workspace_network(session, ws.id, now=now)
            assert sync_thr["budget_throttle_mode"] == "throttled"
            assert sync_thr["add_threshold_used"] == pytest.approx(
                CORE_AFFINITY_THROTTLED_ADD_THRESHOLD
            )

        # 2. Push monthly units to 96,000 / 100,000 -> projected >= 95% ("paused"): zero external HTTP calls!
        session.add(
            ApiCreditLedger(
                workspace_id=ws.id,
                provider="tweetapi_com",
                endpoint="/tw-v2/search",
                units_consumed=13_000,
                estimated_usd=13_000 * DEFAULT_USD_PER_UNIT,
                created_at=now - timedelta(hours=2),
            )
        )
        await session.commit()

        est_paused = await estimate_workspace_monthly_units(session, ws, now=now)
        assert est_paused["discovery_throttle_mode"] == "paused"

        with respx.mock(assert_all_called=True) as strict_router:
            # Zero routes registered -> any HTTP call would fail respx immediately!
            _ = strict_router
            sync_paused = await auto_sync_workspace_network(session, ws.id, now=now)
            assert sync_paused["budget_throttle_mode"] == "paused"
            assert sync_paused["auto_added_count"] == 0

        # 3. Attempting to consume 5,000 more units (96k + 5k > 100k budget) raises SpendCapExceededError
        with pytest.raises(SpendCapExceededError, match="monthly unit budget"):
            await enforce_spend_caps(
                session,
                ws,
                projected_usd=5_000 * DEFAULT_USD_PER_UNIT,
                projected_units=5_000,
                now=now,
            )


@pytest.mark.asyncio
async def test_follow_only_account_settles_across_two_weeks_of_syncs(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """Regression: a pruned co-follow-only account must not re-qualify as an undecayed candidate.

    Runs a sync every 6h for 14 days. The account is added once, pruned once after its co-follow
    evidence decays without conversation, then stays out; only a NEW reply/quote brings it back.
    """
    _client, session_factory = issue1_env
    t0 = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        ws, _comm, seeds = await _seed_core_workspace(session, n_seeds=6, now=t0)
        ws_id = ws.id
        cand = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="cand_cofollow_20k",
            handle="cofollow_peer_20k",
            display_name="Co-follow Peer",
            followers_count=20_000,
            following_count=600,
            status="active",
        )
        session.add(cand)
        await session.flush()
        for s in seeds:
            session.add(
                PeerEndorsement(
                    workspace_id=ws.id,
                    candidate_id=cand.id,
                    endorser_account_id=s.id,
                    endorsement_type="follow",
                    weight_contribution=2.0,
                    occurred_at=t0,
                )
            )
        await session.commit()

        adds: list[datetime] = []
        prunes: list[datetime] = []
        changes_by_sync: list[int] = []
        t = t0
        while t <= t0 + timedelta(days=14):
            res = await auto_sync_workspace_network(session, ws_id, now=t)
            if "cofollow_peer_20k" in res["auto_added_handles"]:
                adds.append(t)
            if "cofollow_peer_20k" in res["auto_pruned_handles"]:
                prunes.append(t)
            changes_by_sync.append(res["auto_added_count"] + res["auto_pruned_count"])
            t += timedelta(hours=6)

        assert adds == [t0], f"expected one add at t0, got {adds}"
        assert len(prunes) == 1, f"expected exactly one prune, got {prunes}"
        assert prunes[0] > t0 + timedelta(hours=48)
        # Settled: no adds or prunes anywhere in the final third of the run
        assert sum(changes_by_sync[-(len(changes_by_sync) // 3):]) == 0

        row = (
            await session.execute(
                select(DiscoveredCandidate).where(DiscoveredCandidate.id == cand.id)
            )
        ).scalar_one()
        assert row.status == "pruned"
        assert row.pruned_at is not None

        # New conversational evidence after the prune re-qualifies it
        t_reply = t + timedelta(hours=1)
        for s in seeds[:3]:
            session.add(
                PeerEndorsement(
                    workspace_id=ws.id,
                    candidate_id=cand.id,
                    endorser_account_id=s.id,
                    endorsement_type="quote",
                    weight_contribution=5.0,
                    source_x_post_id=f"q_after_prune_{s.id}",
                    occurred_at=t_reply,
                )
            )
        await session.commit()
        res_back = await auto_sync_workspace_network(session, ws_id, now=t_reply + timedelta(hours=1))
        assert "cofollow_peer_20k" in res_back["auto_added_handles"]


def test_follow_only_ties_are_discounted_by_audience_size_but_conversation_is_not() -> None:
    """Passive co-follows of a huge account say less about the core than of a niche one;
    replies/quotes count the same at any size (big accounts the core talks with still belong)."""
    from tweettracker.graph.builder import AccountNodeInput
    from tweettracker.graph.nmf_simclusters import (
        build_core_graph_context,
        follow_specificity,
        score_behavioral_core_affinity,
    )

    seeds = [AccountNodeInput(id=i, handle=f"seed_{i}", is_auto_tracked=False) for i in range(1, 7)]
    ctx = build_core_graph_context(seeds)
    now = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)
    follows = {i: 2.0 for i in range(1, 7)}
    quotes = {i: 5.0 for i in range(1, 4)}

    def _score(followers: int, *, inbound_follow: dict[int, float], inbound_conv: dict[int, float]) -> float:
        return score_behavioral_core_affinity(
            ctx,
            node_id=-1,
            inbound_conv=inbound_conv,
            outbound_conv={},
            inbound_follow=inbound_follow,
            outbound_follow={},
            auto_tracked_at=now,
            reference_time=now,
            followers_count=followers,
        )

    assert follow_specificity(20_000) == 1.0
    assert follow_specificity(500_000) == pytest.approx(0.5)

    niche_follow = _score(20_000, inbound_follow=follows, inbound_conv={})
    mega_follow = _score(200_000_000, inbound_follow=follows, inbound_conv={})
    assert mega_follow < niche_follow
    assert mega_follow < CORE_AFFINITY_ADD_THRESHOLD

    niche_conv = _score(20_000, inbound_follow={}, inbound_conv=quotes)
    mega_conv = _score(200_000_000, inbound_follow={}, inbound_conv=quotes)
    assert mega_conv == pytest.approx(niche_conv)

    # Seeds define the core: no audience-size discount on a manual seed, whatever its size
    def _seed_score(followers: int) -> float:
        return score_behavioral_core_affinity(
            ctx,
            node_id=1,
            inbound_conv={},
            outbound_conv={},
            inbound_follow={i: 2.0 for i in range(2, 7)},
            outbound_follow={},
            is_auto_tracked=False,
            is_self_core_anchor=True,
            reference_time=now,
            followers_count=followers,
        )

    assert _seed_score(5_000_000) == pytest.approx(_seed_score(20_000))


@pytest.mark.asyncio
async def test_reply_yield_ingestion_maturing_refresh_and_follower_attribution(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """Ingests `/tw-v2/user/tweets-and-replies` + `OwnFollowerSnapshot`, refreshes maturing `2h-48h`
    off-page replies via `/tw-v2/tweet/details-by-ids`, conserves attributed follower gains across
    overlapping replies, and updates per-account `yield_score`.
    """
    client, session_factory = issue1_env
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)

    await client.put(
        "/api/settings/secrets",
        json={"secrets": {"TWEETAPI_API_KEY": "test-yield-key-1111"}},
    )

    async with session_factory() as session:
        ws, comm, _seeds = await _seed_core_workspace(
            session,
            name="Reply Yield Lab",
            provider="tweetapi_com",
            n_seeds=3,
            now=t0,
        )
        ws.my_x_handle = "gentoftech"
        ws_id = ws.id

        winner_voice = TrackedAccount(
            workspace_id=ws.id,
            x_user_id="8801",
            handle="winner_voice",
            display_name="Winner Voice",
            followers_count=95_000,
            following_count=410,
            is_tracked=True,
            is_auto_tracked=True,
            auto_tracked_at=t0 - timedelta(days=5),
            follows_me=True,
            followed_by_me=True,
        )
        flop_seed = TrackedAccount(
            workspace_id=ws.id,
            x_user_id="8802",
            handle="flop_seed",
            display_name="Flop Seed",
            followers_count=20_000,
            following_count=300,
            is_tracked=True,
            is_auto_tracked=False,
            follows_me=False,
            followed_by_me=False,
        )
        session.add_all([winner_voice, flop_seed])
        await session.flush()

        for acct in (winner_voice, flop_seed):
            session.add(
                AccountCommunityMembership(
                    workspace_id=ws.id,
                    account_id=acct.id,
                    community_id=comm.id,
                    is_manual_tag=(not acct.is_auto_tracked),
                    nmf_known_for=0.8,
                    nmf_interested_in=0.8,
                )
            )

        hot_post = Post(
            workspace_id=ws.id,
            x_post_id="hot_post_winner_1",
            author_account_id=winner_voice.id,
            text="Deep thread on GPU memory bandwidth bottlenecks",
            created_at_x=t0 - timedelta(minutes=25),
            latest_views=18_500,
            latest_likes=240,
            latest_replies=34,
            latest_quotes=12,
            latest_bookmarks=55,
            breakout_score=83.5,
            queue_status="open",
        )
        session.add(hot_post)
        await session.flush()
        hot_post_id = hot_post.id

        # Pre-existing 6h-old un-matured reply that already scrolled off page 1 of tweets-and-replies
        session.add(
            OwnReplyOutcome(
                workspace_id=ws.id,
                x_reply_post_id="older_offpage_reply_0",
                target_account_id=winner_voice.id,
                target_handle="winner_voice",
                action_kind="reply",
                from_hot_posts=False,
                created_at_x=t0 - timedelta(hours=6),
                was_mutual_at_reply=True,
                latest_views=450,
                matured_24h=False,
                last_refreshed_at=t0 - timedelta(hours=5),
            )
        )
        await session.commit()

    # Record a Reply action directly from Hot Posts queue endpoint
    q_resp = await client.post(
        f"/api/workspaces/{ws_id}/radar/posts/{hot_post_id}/queue",
        json={"status": "acted", "action_kind": "reply"},
    )
    assert q_resp.status_code == 200
    assert q_resp.json()["queue_status"] == "acted"
    assert q_resp.json()["acted_action_kind"] == "reply"

    phase = {"sync": 1}

    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.tweetapi.com/tw-v2/user/by-username").mock(
            side_effect=lambda req: httpx.Response(
                200,
                json={
                    "data": {
                        "id": "777" if req.url.params.get("username") == "gentoftech" else "999",
                        "username": req.url.params.get("username") or "gentoftech",
                    }
                },
            )
        )
        router.get("https://api.tweetapi.com/tw-v2/user/following-list").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        router.get("https://api.tweetapi.com/tw-v2/user/by-usernames").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        router.get("https://api.tweetapi.com/tw-v2/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        def _tweets_and_replies_handler(_req: httpx.Request) -> httpx.Response:
            foll_cnt = 4_100 if phase["sync"] == 1 else 4_126
            win_views = 6_800 if phase["sync"] == 1 else 8_400
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "rep_win_1",
                            "type": "reply",
                            "text": "@winner_voice Coalesced loads cut this in half on Hopper.",
                            "created_at": (t0 - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                            "replyTo": {"tweetId": "hot_post_winner_1", "username": "winner_voice"},
                            "viewCount": win_views,
                            "likeCount": 82,
                            "replyCount": 7,
                            "bookmarkCount": 19,
                            "author": {
                                "id": "777",
                                "username": "gentoftech",
                                "followerCount": foll_cnt,
                            },
                        },
                        {
                            "id": "rep_flop_1",
                            "type": "reply",
                            "text": "@flop_seed Nice update",
                            "created_at": (t0 - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
                            "replyTo": {"tweetId": "flop_p1", "username": "flop_seed"},
                            "viewCount": 14,
                            "author": {
                                "id": "777",
                                "username": "gentoftech",
                                "followerCount": foll_cnt,
                            },
                        },
                        {
                            "id": "rep_flop_2",
                            "type": "reply",
                            "text": "@flop_seed Agreed",
                            "created_at": (t0 - timedelta(minutes=40)).isoformat().replace("+00:00", "Z"),
                            "replyTo": {"tweetId": "flop_p2", "username": "flop_seed"},
                            "viewCount": 12,
                            "author": {
                                "id": "777",
                                "username": "gentoftech",
                                "followerCount": foll_cnt,
                            },
                        },
                        {
                            "id": "rep_flop_3",
                            "type": "reply",
                            "text": "@flop_seed Interesting",
                            "created_at": (t0 - timedelta(hours=1, minutes=15)).isoformat().replace("+00:00", "Z"),
                            "replyTo": {"tweetId": "flop_p3", "username": "flop_seed"},
                            "viewCount": 18,
                            "author": {
                                "id": "777",
                                "username": "gentoftech",
                                "followerCount": foll_cnt,
                            },
                        },
                        {
                            "id": "rep_flop_4",
                            "type": "reply",
                            "text": "@flop_seed Curious how that holds up",
                            "created_at": (t0 - timedelta(hours=1, minutes=50)).isoformat().replace("+00:00", "Z"),
                            "replyTo": {"tweetId": "flop_p4", "username": "flop_seed"},
                            "viewCount": 16,
                            "author": {
                                "id": "777",
                                "username": "gentoftech",
                                "followerCount": foll_cnt,
                            },
                        },
                    ]
                },
            )

        router.get("https://api.tweetapi.com/tw-v2/user/tweets-and-replies").mock(
            side_effect=_tweets_and_replies_handler
        )
        details_route = router.get("https://api.tweetapi.com/tw-v2/tweet/details-by-ids").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "older_offpage_reply_0",
                            "type": "reply",
                            "text": "@winner_voice Earlier benchmark note",
                            "created_at": (t0 - timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
                            "viewCount": 5_200,
                            "likeCount": 49,
                            "author": {"id": "777", "username": "gentoftech"},
                        }
                    ]
                },
            )
        )

        async with session_factory() as session:
            # Sync 1 at t0: records OwnFollowerSnapshot=4100, ingests 5 replies + rehydrates 1 = 6
            r1 = await auto_sync_workspace_network(session, ws_id, now=t0)
            assert r1["own_replies_synced"] == 6
            assert details_route.called

            # Sync 2 at t0 + 4h: followerCount jumps 4100 -> 4126 (+26 follows gained)
            phase["sync"] = 2
            t_4h = t0 + timedelta(hours=4)
            await auto_sync_workspace_network(session, ws_id, now=t_4h)

            snaps = list(
                (
                    await session.execute(
                        select(OwnFollowerSnapshot)
                        .where(OwnFollowerSnapshot.workspace_id == ws_id)
                        .order_by(OwnFollowerSnapshot.recorded_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert [s.followers_count for s in snaps] == [4_100, 4_126]

            outcomes = list(
                (
                    await session.execute(
                        select(OwnReplyOutcome).where(OwnReplyOutcome.workspace_id == ws_id)
                    )
                )
                .scalars()
                .all()
            )
            by_rid = {o.x_reply_post_id: o for o in outcomes}
            assert by_rid["older_offpage_reply_0"].latest_views == 5_200
            assert by_rid["rep_win_1"].latest_views == 8_400
            assert by_rid["rep_win_1"].from_hot_posts is True
            assert by_rid["rep_win_1"].source_post_id == hot_post_id

            # Total attributed follower gains conserve the exact +26 delta without double counting
            total_attributed = sum(float(o.attributed_follows_gained or 0.0) for o in outcomes)
            assert total_attributed == pytest.approx(26.0, abs=0.05)

            winner_follows = (
                float(by_rid["rep_win_1"].attributed_follows_gained)
                + float(by_rid["older_offpage_reply_0"].attributed_follows_gained)
            )
            assert winner_follows > 25.5

            accts = {
                a.handle: a
                for a in (
                    await session.execute(
                        select(TrackedAccount).where(TrackedAccount.workspace_id == ws_id)
                    )
                )
                .scalars()
                .all()
            }
            assert accts["winner_voice"].yield_score >= 0.60
            assert accts["winner_voice"].reply_sample_count == 2
            assert accts["winner_voice"].attributed_follows_30d > 25.5
            assert accts["flop_seed"].reply_sample_count == 4
            assert accts["flop_seed"].yield_score < 0.30


@pytest.mark.asyncio
async def test_bootstrap_seeds_have_28d_grace_then_fade_and_prune_on_low_yield_or_dormancy(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """Seeds are a 28-day bootstrap only: protected for 28 days, then low-yield or dormant seeds
    fade and prune (`< 0.32`) once healthy discovered accounts anchor the workspace, while an
    unattended workspace preserves `MIN_ACTIVE_CORE_FLOOR = 10`.
    """
    assert SEED_BOOTSTRAP_GRACE_DAYS == 28
    assert MIN_ACTIVE_CORE_FLOOR == 10

    _client, session_factory = issue1_env
    t0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        ws, comm, seeds = await _seed_core_workspace(
            session,
            name="Bootstrap Decay Lab",
            n_seeds=5,
            now=t0,
        )
        ws.my_x_handle = "gentoftech"

        seed_handles = [s.handle for s in seeds]
        for s in seeds:
            s.created_at = t0
            s.follows_me = False
            s.followed_by_me = False

        # Give seeds 0 & 1 four flopped replies (~12 views) and 0 climbing Hot Posts;
        # leave seeds 2, 3, 4 completely dormant (0 replies, 0 climbing Hot Posts).
        for flop_idx in (0, 1):
            for r_i in range(4):
                session.add(
                    OwnReplyOutcome(
                        workspace_id=ws.id,
                        x_reply_post_id=f"seed_flop_{flop_idx}_{r_i}",
                        target_account_id=seeds[flop_idx].id,
                        target_handle=seeds[flop_idx].handle,
                        action_kind="reply",
                        created_at_x=t0 + timedelta(days=20, hours=r_i),
                        latest_views=12,
                        matured_24h=True,
                    )
                )

        # Add 5 proven high-yield auto-discovered creators that take over the workspace core
        winners: list[TrackedAccount] = []
        for w_idx in range(5):
            win = TrackedAccount(
                workspace_id=ws.id,
                x_user_id=f"auto_win_{w_idx}",
                handle=f"auto_yield_winner_{w_idx}",
                display_name=f"Auto Yield Winner {w_idx}",
                followers_count=42_000 + w_idx * 15_000,
                following_count=350,
                is_tracked=True,
                is_auto_tracked=True,
                auto_tracked_at=t0 + timedelta(days=6),
                follows_me=True,
                followed_by_me=True,
            )
            session.add(win)
            await session.flush()
            session.add(
                AccountCommunityMembership(
                    workspace_id=ws.id,
                    account_id=win.id,
                    community_id=comm.id,
                    is_manual_tag=False,
                    nmf_known_for=0.85,
                    nmf_interested_in=0.85,
                )
            )
            for r_i in range(3):
                session.add(
                    OwnReplyOutcome(
                        workspace_id=ws.id,
                        x_reply_post_id=f"win_rep_{w_idx}_{r_i}",
                        target_account_id=win.id,
                        target_handle=win.handle,
                        action_kind="reply",
                        created_at_x=t0 + timedelta(days=24, hours=r_i),
                        was_mutual_at_reply=True,
                        latest_views=5_400,
                        attributed_follows_gained=3.5,
                        matured_24h=True,
                    )
                )
            winners.append(win)

        for i in range(len(winners)):
            nxt = winners[(i + 1) % len(winners)]
            session.add(
                RelationshipEdge(
                    workspace_id=ws.id,
                    source_account_id=winners[i].id,
                    target_account_id=nxt.id,
                    edge_type="quote",
                    interaction_count=3,
                    decayed_weight=12.0,
                    last_event_at=t0 + timedelta(days=25),
                )
            )
        await session.commit()

        # 1. At t0 + 27 days (inside 28-day bootstrap grace period): ZERO bootstrap seeds pruned!
        t_27d = t0 + timedelta(days=27)
        for win in winners:
            session.add(
                Post(
                    workspace_id=ws.id,
                    x_post_id=f"win_post_27d_{win.id}",
                    author_account_id=win.id,
                    text="Climbing technical thread",
                    created_at_x=t_27d - timedelta(hours=4),
                    latest_views=16_000,
                    breakout_score=78.0,
                )
            )
        await session.commit()

        res_27d = await auto_sync_workspace_network(session, ws.id, now=t_27d)
        assert set(seed_handles).isdisjoint(res_27d["auto_pruned_handles"])

        # 2. At t0 + 56 days (4 weeks past 28-day grace): ALL 5 bad/dormant bootstrap seeds prune,
        # while all 5 proven high-yield auto-discovered creators remain tracked and anchor the core!
        t_56d = t0 + timedelta(days=56)
        for win in winners:
            session.add(
                Post(
                    workspace_id=ws.id,
                    x_post_id=f"win_post_56d_{win.id}",
                    author_account_id=win.id,
                    text="Fresh climbing post at day 56",
                    created_at_x=t_56d - timedelta(hours=3),
                    latest_views=19_000,
                    breakout_score=80.0,
                )
            )
            session.add(
                OwnReplyOutcome(
                    workspace_id=ws.id,
                    x_reply_post_id=f"win_rep_56d_{win.id}",
                    target_account_id=win.id,
                    target_handle=win.handle,
                    action_kind="reply",
                    created_at_x=t_56d - timedelta(days=2),
                    was_mutual_at_reply=True,
                    latest_views=6_100,
                    attributed_follows_gained=4.0,
                    matured_24h=True,
                )
            )
        await session.commit()

        res_56d = await auto_sync_workspace_network(session, ws.id, now=t_56d)
        assert set(seed_handles).issubset(set(res_56d["auto_pruned_handles"]))
        assert res_56d["active_tracked_count"] == 5

        winner_handles = {w.handle for w in winners}
        remaining_handles = {
            a.handle
            for a in (
                await session.execute(
                    select(TrackedAccount).where(
                        TrackedAccount.workspace_id == ws.id,
                        TrackedAccount.is_tracked.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        }
        assert remaining_handles == winner_handles

        # 3. Safety floor check: an unattended workspace of 14 completely idle seeds never wipes below 10
        ws_idle, _comm_idle, idle_seeds = await _seed_core_workspace(
            session,
            name="Unattended Floor Lab",
            n_seeds=14,
            now=t0,
        )
        for s in idle_seeds:
            s.created_at = t0
            s.follows_me = False
            s.followed_by_me = False
        await session.commit()

        res_idle = await auto_sync_workspace_network(session, ws_idle.id, now=t_56d)
        assert res_idle["active_tracked_count"] == MIN_ACTIVE_CORE_FLOOR
        assert res_idle["auto_pruned_count"] == 4


@pytest.mark.asyncio
async def test_learned_yield_core_drives_crawl_priority_and_simclusters_discovery(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """High-yield auto-discovered accounts enter `C_yield` (`core_anchor_ids`), crawl ahead of
    faded/untested seeds, and drive 2-hop candidate discovery while faded seeds lose core status.
    """
    client, session_factory = issue1_env
    now = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)

    await client.put(
        "/api/settings/secrets",
        json={"secrets": {"TWEETAPI_API_KEY": "test-crawl-prio-key-2222"}},
    )

    async with session_factory() as session:
        ws, comm, seeds = await _seed_core_workspace(
            session,
            name="Learned Core Crawl",
            provider="tweetapi_com",
            n_seeds=3,
            now=now,
        )
        ws.my_x_handle = "gentoftech"

        # Seed #3 is a 40-day-old bootstrap seed whose replies consistently flopped
        low_yield_seed = seeds[2]
        low_yield_seed.handle = "low_yield_seed"
        low_yield_seed.x_user_id = "3003"
        low_yield_seed.created_at = now - timedelta(days=40)
        low_yield_seed.follows_me = False
        low_yield_seed.followed_by_me = False

        # Auto-discovered creator with proven high reply reach & follower conversion
        proven_winner = TrackedAccount(
            workspace_id=ws.id,
            x_user_id="9009",
            handle="proven_auto_winner",
            display_name="Proven Auto Winner",
            followers_count=62_000,
            following_count=380,
            is_tracked=True,
            is_auto_tracked=True,
            auto_tracked_at=now - timedelta(days=12),
            follows_me=True,
            followed_by_me=True,
        )
        session.add(proven_winner)
        await session.flush()
        session.add(
            AccountCommunityMembership(
                workspace_id=ws.id,
                account_id=proven_winner.id,
                community_id=comm.id,
                is_manual_tag=False,
                nmf_known_for=0.9,
                nmf_interested_in=0.9,
            )
        )

        for r_i in range(4):
            session.add(
                OwnReplyOutcome(
                    workspace_id=ws.id,
                    x_reply_post_id=f"lys_flop_{r_i}",
                    target_account_id=low_yield_seed.id,
                    target_handle=low_yield_seed.handle,
                    action_kind="reply",
                    created_at_x=now - timedelta(days=4, hours=r_i),
                    latest_views=11,
                    matured_24h=True,
                )
            )
        for r_i in range(3):
            session.add(
                OwnReplyOutcome(
                    workspace_id=ws.id,
                    x_reply_post_id=f"paw_win_{r_i}",
                    target_account_id=proven_winner.id,
                    target_handle=proven_winner.handle,
                    action_kind="reply",
                    created_at_x=now - timedelta(days=2, hours=r_i),
                    was_mutual_at_reply=True,
                    latest_views=7_800,
                    attributed_follows_gained=4.0,
                    matured_24h=True,
                )
            )

        await recompute_workspace_graph(session, ws.id, commit=True, reference_time=now)
        await session.refresh(proven_winner)
        await session.refresh(low_yield_seed)
        assert proven_winner.yield_score > 0.70
        assert low_yield_seed.yield_score < 0.30

        from tweettracker.graph.builder import AccountNodeInput
        from tweettracker.graph.nmf_simclusters import build_core_graph_context

        active_accts = [seeds[0], seeds[1], low_yield_seed, proven_winner]
        core_ctx = build_core_graph_context(
            [
                AccountNodeInput(
                    id=a.id,
                    handle=a.handle,
                    followers_count=a.followers_count,
                    following_count=a.following_count,
                    follows_me=a.follows_me,
                    followed_by_me=a.followed_by_me,
                    is_auto_tracked=bool(a.is_auto_tracked),
                    auto_tracked_at=a.auto_tracked_at,
                    created_at=a.created_at,
                    yield_score=float(a.yield_score or 0.0),
                    reply_sample_count=int(a.reply_sample_count or 0),
                )
                for a in active_accts
            ],
            my_x_handle=ws.my_x_handle,
        )

        # Proven auto-discovered winner enters C_yield; faded low-yield seed drops out of C_yield!
        assert proven_winner.id in core_ctx.core_ids
        assert low_yield_seed.id not in core_ctx.core_ids

        # Stage 2-hop candidates: one endorsed by proven_auto_winner + core_seed_1, one ONLY by low_yield_seed
        cand_from_winner = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="cand_w_1",
            handle="winner_circle_peer",
            display_name="Winner Circle Peer",
            followers_count=28_000,
            following_count=310,
            status="active",
        )
        cand_from_faded = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="cand_f_2",
            handle="faded_seed_peer",
            display_name="Faded Seed Peer",
            followers_count=28_000,
            following_count=310,
            status="active",
        )
        session.add_all([cand_from_winner, cand_from_faded])
        await session.flush()

        for endorser in (proven_winner, seeds[0], seeds[1]):
            session.add(
                PeerEndorsement(
                    workspace_id=ws.id,
                    candidate_id=cand_from_winner.id,
                    endorser_account_id=endorser.id,
                    endorsement_type="quote",
                    weight_contribution=15.0,
                    source_x_post_id=f"wq_{endorser.id}",
                    occurred_at=now - timedelta(hours=3),
                )
            )
            session.add(
                PeerEndorsement(
                    workspace_id=ws.id,
                    candidate_id=cand_from_winner.id,
                    endorser_account_id=endorser.id,
                    endorsement_type="follow",
                    weight_contribution=2.0,
                    occurred_at=now - timedelta(hours=6),
                )
            )

        session.add(
            PeerEndorsement(
                workspace_id=ws.id,
                candidate_id=cand_from_faded.id,
                endorser_account_id=low_yield_seed.id,
                endorsement_type="quote",
                weight_contribution=15.0,
                source_x_post_id="fq_1",
                occurred_at=now - timedelta(hours=3),
            )
        )
        await session.commit()

        await refresh_rising_candidates(
            session,
            ws.id,
            harvest_from_provider=False,
            commit=True,
            reference_time=now,
        )
        await session.refresh(cand_from_winner)
        await session.refresh(cand_from_faded)
        assert cand_from_winner.yield_score >= CORE_AFFINITY_ADD_THRESHOLD
        assert cand_from_faded.core_affinity_score < 0.20
        assert cand_from_faded.yield_score < CORE_AFFINITY_PRUNE_THRESHOLD

        # Verify crawl priority: in throttled mode (3 stale accounts crawled), proven_auto_winner is crawled #1
        # and low_yield_seed (tier 2) is skipped behind proven_auto_winner + core_seed_1 + core_seed_2!
        uid_by_handle = {
            "gentoftech": "777",
            "proven_auto_winner": "9009",
            "core_seed_1": "1001",
            "core_seed_2": "1002",
            "low_yield_seed": "3003",
        }
        crawled_uids: list[str] = []

        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.tweetapi.com/tw-v2/user/by-username").mock(
                side_effect=lambda req: httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": uid_by_handle.get(req.url.params.get("username") or "", "9999"),
                            "username": req.url.params.get("username") or "",
                        }
                    },
                )
            )

            def _record_following(req: httpx.Request) -> httpx.Response:
                uid = req.url.params.get("userId") or ""
                if uid != "777":
                    crawled_uids.append(uid)
                return httpx.Response(200, json={"data": []})

            router.get("https://api.tweetapi.com/tw-v2/user/following-list").mock(
                side_effect=_record_following
            )
            router.get("https://api.tweetapi.com/tw-v2/search").mock(
                return_value=httpx.Response(200, json={"data": []})
            )

            await crawl_live_seed_network(session, ws, throttle_mode="throttled", now=now)

        assert crawled_uids[0] == "9009"  # proven_auto_winner crawled FIRST!
        assert "3003" not in crawled_uids  # faded low_yield_seed skipped!


@pytest.mark.asyncio
async def test_hot_posts_live_queue_ranks_by_expected_reply_reach(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """`Hot Posts` (`/radar?live_queue=true`) ranks live climbing threads by `expected_reply_reach_score`
    so a climbing post from a proven high-yield mutual outranks a slightly higher-velocity post from an
    account where replies consistently flop.
    """
    client, session_factory = issue1_env
    now = datetime.now(timezone.utc)

    async with session_factory() as session:
        ws, comm, _seeds = await _seed_core_workspace(
            session,
            name="Hot Posts Yield Rank",
            provider="tweetapi_com",
            n_seeds=2,
            now=now,
        )
        ws.my_x_handle = "gentoftech"
        ws_id = ws.id

        low_yield_poster = TrackedAccount(
            workspace_id=ws.id,
            x_user_id="6001",
            handle="low_yield_poster",
            display_name="Low Yield Poster",
            followers_count=30_000,
            following_count=220,
            is_tracked=True,
            is_auto_tracked=True,
            auto_tracked_at=now - timedelta(days=10),
            follows_me=False,
            followed_by_me=False,
        )
        high_yield_mutual = TrackedAccount(
            workspace_id=ws.id,
            x_user_id="6002",
            handle="high_yield_mutual",
            display_name="High Yield Mutual",
            followers_count=85_000,
            following_count=410,
            is_tracked=True,
            is_auto_tracked=True,
            auto_tracked_at=now - timedelta(days=10),
            follows_me=True,
            followed_by_me=True,
        )
        session.add_all([low_yield_poster, high_yield_mutual])
        await session.flush()

        for acct in (low_yield_poster, high_yield_mutual):
            session.add(
                AccountCommunityMembership(
                    workspace_id=ws.id,
                    account_id=acct.id,
                    community_id=comm.id,
                    is_manual_tag=False,
                    nmf_known_for=0.85,
                    nmf_interested_in=0.85,
                )
            )

        for r_i in range(4):
            session.add(
                OwnReplyOutcome(
                    workspace_id=ws.id,
                    x_reply_post_id=f"lyp_rep_{r_i}",
                    target_account_id=low_yield_poster.id,
                    target_handle=low_yield_poster.handle,
                    action_kind="reply",
                    created_at_x=now - timedelta(days=3, hours=r_i),
                    latest_views=20,
                    matured_24h=True,
                )
            )
        for r_i in range(3):
            session.add(
                OwnReplyOutcome(
                    workspace_id=ws.id,
                    x_reply_post_id=f"hym_rep_{r_i}",
                    target_account_id=high_yield_mutual.id,
                    target_handle=high_yield_mutual.handle,
                    action_kind="reply",
                    created_at_x=now - timedelta(days=2, hours=r_i),
                    was_mutual_at_reply=True,
                    latest_views=9_500,
                    attributed_follows_gained=3.0,
                    matured_24h=True,
                )
            )

        await recompute_workspace_graph(session, ws.id, commit=True, reference_time=now)

        # Insert two live 25m-old climbing posts: low_yield_poster has slightly higher breakout_score (81 vs 76)
        session.add_all(
            [
                Post(
                    workspace_id=ws.id,
                    x_post_id="live_post_low_yield",
                    author_account_id=low_yield_poster.id,
                    text="Hot take thread with closed audience",
                    created_at_x=now - timedelta(minutes=25),
                    latest_views=4_200,
                    latest_likes=60,
                    latest_replies=9,
                    breakout_score=81.0,
                    queue_status="open",
                    matured_48h=False,
                ),
                Post(
                    workspace_id=ws.id,
                    x_post_id="live_post_high_yield",
                    author_account_id=high_yield_mutual.id,
                    text="Deep technical breakdown where mutual replies get top placement",
                    created_at_x=now - timedelta(minutes=25),
                    latest_views=19_500,
                    latest_likes=210,
                    latest_replies=28,
                    latest_quotes=11,
                    latest_bookmarks=40,
                    breakout_score=76.0,
                    queue_status="open",
                    matured_48h=False,
                ),
            ]
        )
        await session.commit()

    resp = await client.get(f"/api/workspaces/{ws_id}/radar?live_queue=true")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2

    top_card, second_card = items[0], items[1]
    assert top_card["author"]["handle"] == "high_yield_mutual"
    assert second_card["author"]["handle"] == "low_yield_poster"
    assert top_card["expected_reply_reach_score"] > second_card["expected_reply_reach_score"]
    assert top_card["author"]["avg_reply_views"] == pytest.approx(9_500.0, rel=0.02)
    assert any(
        "average ~9,500 views" in reason for reason in top_card["hover_summary"]["why_reasons"]
    )


@pytest.mark.asyncio
async def test_manual_import_respects_paused_unit_budget_without_crawling(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """When a workspace is in `paused` budget mode (>=95% used), manual `POST /accounts/import`
    hydrates and saves the requested handle without running `following-list` or `search` crawls."""
    client, session_factory = issue1_env
    now = datetime.now(timezone.utc)

    await client.put(
        "/api/settings/secrets",
        json={"secrets": {"TWEETAPI_API_KEY": "test-paused-import-key-3333"}},
    )

    async with session_factory() as session:
        ws, comm, _seeds = await _seed_core_workspace(
            session,
            name="Paused Import Guard",
            provider="tweetapi_com",
            n_seeds=3,
            monthly_unit_budget=100_000,
            now=now,
        )
        ws.my_x_handle = "gentoftech"
        ws_id = ws.id
        comm_id = comm.id

        session.add(
            ApiCreditLedger(
                workspace_id=ws.id,
                provider="tweetapi_com",
                endpoint="/tw-v2/search",
                units_consumed=96_500.0,
                estimated_usd=96_500.0 * 0.00025,
                created_at=now - timedelta(hours=1),
            )
        )
        await session.commit()

    with respx.mock(assert_all_called=False) as router:
        hydrate_route = router.get("https://api.tweetapi.com/tw-v2/user/by-usernames").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "5501",
                            "username": "imported_when_paused",
                            "name": "Imported When Paused",
                            "followerCount": 19_400,
                            "followingCount": 260,
                        }
                    ]
                },
            )
        )
        follow_route = router.get("https://api.tweetapi.com/tw-v2/user/following-list").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        search_route = router.get("https://api.tweetapi.com/tw-v2/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        imp_resp = await client.post(
            f"/api/workspaces/{ws_id}/network/accounts/import",
            json={
                "handles": ["@imported_when_paused"],
                "community_ids": [comm_id],
                "recompute": True,
            },
        )
        assert imp_resp.status_code == 201
        assert imp_resp.json()["imported_count"] == 1
        assert hydrate_route.call_count == 1
        assert follow_route.call_count == 0
        assert search_route.call_count == 0


@pytest.mark.asyncio
async def test_worker_discovery_cooldown_persists_across_restart(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persists `Workspace.last_discovery_sync_at` so a worker restart with a fresh in-memory
    `WorkerScheduleState` respects the 6-hour discovery cooldown."""
    import tweettracker.worker.main as worker_mod
    from tweettracker.worker.main import WorkerScheduleState, run_worker_cycle

    _client, session_factory = issue1_env
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        ws, _comm, _seeds = await _seed_core_workspace(
            session, name="Worker Cooldown Persist", n_seeds=3, now=t0
        )
        ws_id = ws.id

    sync_calls: list[tuple[int, datetime]] = []
    real_auto_sync = worker_mod.auto_sync_workspace_network

    async def _spy_auto_sync(
        session: AsyncSession, workspace_id: int, *, now: datetime | None = None
    ) -> dict[str, object]:
        sync_calls.append((workspace_id, now or t0))
        return await real_auto_sync(session, workspace_id, now=now)

    monkeypatch.setattr(worker_mod, "auto_sync_workspace_network", _spy_auto_sync)

    # First boot at t0: runs discovery sync and persists `ws.last_discovery_sync_at`
    res_t0 = await run_worker_cycle(
        session_factory, state=WorkerScheduleState(), run_upstream_sync=False, now=t0
    )
    assert res_t0["discovery_synced_workspaces"] >= 1
    assert any(wid == ws_id for wid, _ in sync_calls)

    async with session_factory() as session:
        ws_after = await session.get(Workspace, ws_id)
        assert ws_after is not None
        assert ws_after.last_discovery_sync_at is not None

    # Simulate deploy / process restart at t0 + 2h with a brand-new WorkerScheduleState()
    sync_calls.clear()
    res_restart_2h = await run_worker_cycle(
        session_factory,
        state=WorkerScheduleState(),
        run_upstream_sync=False,
        now=t0 + timedelta(hours=2),
    )
    assert res_restart_2h["hourly_refreshed"] >= 1
    assert res_restart_2h["discovery_synced_workspaces"] == 0
    assert sync_calls == []

    # Simulate another tick at t0 + 6h05m (past 6h cooldown): discovery runs again
    res_after_6h = await run_worker_cycle(
        session_factory,
        state=WorkerScheduleState(),
        run_upstream_sync=False,
        now=t0 + timedelta(hours=6, minutes=5),
    )
    assert res_after_6h["discovery_synced_workspaces"] >= 1
    assert any(wid == ws_id for wid, _ in sync_calls)


@pytest.mark.asyncio
async def test_auto_sync_reuses_precomputed_analytics_and_cleans_stale_candidate_staging(
    issue1_env: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auto_sync_workspace_network` reuses precomputed analytics inside `refresh_rising_candidates`
    (zero duplicate graph rebuild) and bulk-cleans stale follow-only noise & orphaned endorsements."""
    import tweettracker.graph.service as graph_service

    _client, session_factory = issue1_env
    now = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)

    analytics_calls = {"count": 0}
    real_build_analytics = graph_service.build_analytics_from_orm

    def _spy_build_analytics(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        analytics_calls["count"] += 1
        return real_build_analytics(*args, **kwargs)

    monkeypatch.setattr(graph_service, "build_analytics_from_orm", _spy_build_analytics)

    async with session_factory() as session:
        ws, comm, seeds = await _seed_core_workspace(
            session, name="Staging GC Lab", n_seeds=4, now=now
        )
        ws_id = ws.id

        muted_acct = TrackedAccount(
            workspace_id=ws.id,
            x_user_id="muted_999",
            handle="muted_endorser",
            display_name="Muted Endorser",
            followers_count=12_000,
            is_tracked=False,
            is_muted=True,
        )
        session.add(muted_acct)
        await session.flush()

        qualifying_cand = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="c_qual_1",
            handle="qualifying_core_peer",
            display_name="Qualifying Core Peer",
            followers_count=22_000,
            status="active",
            updated_at=now - timedelta(hours=2),
        )
        stale_follow_noise = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="c_noise_2",
            handle="stale_follow_noise",
            display_name="Stale Follow Noise",
            followers_count=350_000,
            status="over_cap",
            updated_at=now - timedelta(days=9),
        )
        orphaned_cand = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="c_orph_3",
            handle="orphaned_from_muted",
            display_name="Orphaned From Muted",
            followers_count=18_000,
            status="active",
            updated_at=now - timedelta(days=2),
        )
        dismissed_cand = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="c_dis_4",
            handle="user_dismissed_voice",
            display_name="User Dismissed Voice",
            followers_count=14_000,
            status="dismissed",
            updated_at=now - timedelta(days=25),
        )
        pruned_tombstone = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="c_prn_5",
            handle="recently_pruned_voice",
            display_name="Recently Pruned Voice",
            followers_count=19_000,
            status="pruned",
            pruned_at=now - timedelta(days=10),
            updated_at=now - timedelta(days=10),
        )
        # Pruned long ago: must still be kept, or the next crawl re-stages it as a fresh candidate
        old_pruned_tombstone = DiscoveredCandidate(
            workspace_id=ws.id,
            x_user_id="c_prn_6",
            handle="long_ago_pruned_voice",
            display_name="Long Ago Pruned Voice",
            followers_count=21_000,
            status="pruned",
            pruned_at=now - timedelta(days=200),
            updated_at=now - timedelta(days=200),
        )
        session.add_all(
            [
                qualifying_cand,
                stale_follow_noise,
                orphaned_cand,
                dismissed_cand,
                pruned_tombstone,
                old_pruned_tombstone,
            ]
        )
        await session.flush()

        for s in seeds[:3]:
            session.add(
                PeerEndorsement(
                    workspace_id=ws.id,
                    candidate_id=qualifying_cand.id,
                    endorser_account_id=s.id,
                    endorsement_type="quote",
                    source_x_post_id=f"q_gc_{s.id}",
                    weight_contribution=5.0,
                    occurred_at=now - timedelta(hours=3),
                )
            )
        session.add(
            PeerEndorsement(
                workspace_id=ws.id,
                candidate_id=stale_follow_noise.id,
                endorser_account_id=seeds[0].id,
                endorsement_type="follow",
                weight_contribution=1.0,
                occurred_at=now - timedelta(days=9),
            )
        )
        session.add(
            PeerEndorsement(
                workspace_id=ws.id,
                candidate_id=orphaned_cand.id,
                endorser_account_id=muted_acct.id,
                endorsement_type="follow",
                weight_contribution=1.0,
                occurred_at=now - timedelta(days=2),
            )
        )
        await session.commit()

        # Sync 1 promotes `qualifying_core_peer`: analytics runs twice (pre-promo + post-promo), NOT 3x
        analytics_calls["count"] = 0
        res1 = await auto_sync_workspace_network(session, ws_id, now=now)
        assert "qualifying_core_peer" in res1["auto_added_handles"]
        assert analytics_calls["count"] == 2
        assert res1["candidates_cleaned"] == 2
        assert res1["endorsements_cleaned"] >= 5

        surviving_cands = {
            c.handle: c.status
            for c in (
                await session.execute(
                    select(DiscoveredCandidate).where(DiscoveredCandidate.workspace_id == ws_id)
                )
            )
            .scalars()
            .all()
        }
        assert "stale_follow_noise" not in surviving_cands
        assert "orphaned_from_muted" not in surviving_cands
        assert surviving_cands["qualifying_core_peer"] == "promoted"
        assert surviving_cands["user_dismissed_voice"] == "dismissed"
        assert surviving_cands["recently_pruned_voice"] == "pruned"
        assert surviving_cands["long_ago_pruned_voice"] == "pruned"

        surviving_ends = list(
            (
                await session.execute(
                    select(PeerEndorsement).where(PeerEndorsement.workspace_id == ws_id)
                )
            )
            .scalars()
            .all()
        )
        assert surviving_ends == []

        # Steady-state Sync 2 (zero adds/prunes): analytics runs ONCE total across the entire sync!
        analytics_calls["count"] = 0
        res2 = await auto_sync_workspace_network(session, ws_id, now=now + timedelta(hours=6))
        assert res2["auto_added_count"] == 0
        assert res2["auto_pruned_count"] == 0
        assert analytics_calls["count"] == 1


