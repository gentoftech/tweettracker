"""Comprehensive Epic 2 unit & integration tests: Velocity Z, SEISMIC Hawkes,
Two-Stage x-algorithm (9106e9f) Scorer + DPP Crowding, Live SSE, and HMAC Webhooks.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json

import httpx
import pytest
import respx

from tweettracker.algo import compute_text_embedding
from tweettracker.api.main import create_app
from tweettracker.db import configure_database, dispose_database, get_session_factory
from tweettracker.models import (
    AlertDelivery,
    Community,
    Post,
    PostInteraction,
    TrackedAccount,
    Workspace,
)
from tweettracker.radar import (
    BASELINE_AGES,
    CascadeSnapshotPoint,
    RadarCandidateBundle,
    RadarSSEHub,
    compute_hmac_signature,
    compute_velocity_metrics,
    default_baseline_curves,
    dispatch_breakout_alert,
    dispatch_signed_webhook,
    estimate_seismic,
    interpolate_baseline_quantiles,
    is_within_quiet_hours,
    rebuild_author_baseline_curves,
    score_radar_batch,
    score_single_post,
    verify_hmac_signature,
)
from tweettracker.seed import WS1_LIVE_POST_TOPICS, bootstrap_database


@pytest.fixture
async def radar_client(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[httpx.AsyncClient, None]:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.delenv("TWEETTRACKER_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("TWEETTRACKER_WEBHOOK_SECRET", "epic2-test-hmac-secret-from-env")

    engine, session_factory = configure_database("sqlite+aiosqlite:///:memory:")
    await bootstrap_database(engine, session_factory, auto_migrate=True, seed_fixtures=True)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    await dispose_database()


# =============================================================================
# 1. Author Age-Matched Velocity Z-Score & Baseline Interpolation (15m..48h)
# =============================================================================


def test_velocity_interpolation_and_author_matched_z_score() -> None:
    small_author_curves = default_baseline_curves(median_48h_views=3_000)
    mega_author_curves = default_baseline_curves(median_48h_views=250_000)

    # Exact values at all canonical knots (15, 30, 45, 60, 120, 360, 1440, 2880)
    prev_p50 = 0.0
    for age in BASELINE_AGES:
        q = interpolate_baseline_quantiles(small_author_curves, float(age))
        assert q.p25 == float(small_author_curves["p25"][str(age)])
        assert q.p50 == float(small_author_curves["p50"][str(age)])
        assert q.p75 == float(small_author_curves["p75"][str(age)])
        assert q.p90 == float(small_author_curves["p90"][str(age)])
        assert q.p25 <= q.p50 <= q.p75 <= q.p90
        assert q.p50 >= prev_p50
        prev_p50 = q.p50

    # Smooth interior interpolation (22.5m is midpoint of 15m and 30m)
    q_15 = interpolate_baseline_quantiles(small_author_curves, 15.0)
    q_22_5 = interpolate_baseline_quantiles(small_author_curves, 22.5)
    q_30 = interpolate_baseline_quantiles(small_author_curves, 30.0)
    assert q_22_5.p50 == pytest.approx(0.5 * (q_15.p50 + q_30.p50), rel=1e-5)

    # Pre-15m scaling and >48h ceiling clamping
    q_7_5 = interpolate_baseline_quantiles(small_author_curves, 7.5)
    assert q_7_5.p50 == pytest.approx(0.5 * q_15.p50, rel=1e-5)
    q_3600 = interpolate_baseline_quantiles(small_author_curves, 3600.0)
    assert q_3600.p50 == float(small_author_curves["p50"]["2880"])

    # Same raw view count (2,400 views @ 25m) is a massive breakout for small author
    # and an underperforming post for mega author:
    vel_small = compute_velocity_metrics(2_400, 25.0, small_author_curves)
    vel_mega = compute_velocity_metrics(2_400, 25.0, mega_author_curves)
    assert vel_small.velocity_multiple > 5.0
    assert vel_small.velocity_z_score > 4.0
    assert vel_mega.velocity_multiple < 0.15
    assert vel_mega.velocity_z_score < -0.5

    # Rebuild author baseline curves from snapshot tuples
    synthetic_snaps = [
        (15, 300),
        (15, 420),
        (15, 550),
        (15, 820),
        (60, 1_100),
        (60, 1_500),
        (60, 2_000),
        (60, 3_100),
        (2880, 3_800),
        (2880, 4_900),
        (2880, 6_400),
        (2880, 9_500),
    ]
    rebuilt = rebuild_author_baseline_curves(synthetic_snaps)
    for age in BASELINE_AGES:
        k = str(age)
        assert rebuilt["p25"][k] <= rebuilt["p50"][k] < rebuilt["p75"][k] < rebuilt["p90"][k]
    assert rebuilt["p50"]["15"] < rebuilt["p50"]["60"] < rebuilt["p50"]["2880"]


# =============================================================================
# 2. Multi-Signal SEISMIC Hawkes Infectiousness Monotonicity & Regularization
# =============================================================================


def test_seismic_hawkes_monotonicity_and_bayesian_regularization() -> None:
    # Flat low-rate post across 15m -> 30m -> 45m
    flat_snaps = [
        CascadeSnapshotPoint(age_minutes=15, views=900, likes=8, replies=0, reposts=1, quotes=0, bookmarks=1),
        CascadeSnapshotPoint(age_minutes=30, views=1_600, likes=14, replies=1, reposts=1, quotes=0, bookmarks=1),
        CascadeSnapshotPoint(age_minutes=45, views=2_100, likes=18, replies=1, reposts=2, quotes=0, bookmarks=2),
    ]
    flat_est = estimate_seismic(
        age_minutes=45,
        views=2_100,
        likes=18,
        replies=1,
        reposts=2,
        quotes=0,
        bookmarks=2,
        snapshots=flat_snaps,
    )

    # Rapid early quote + bookmark accelerating surge across 15m -> 30m -> 45m
    surge_snaps = [
        CascadeSnapshotPoint(age_minutes=15, views=1_200, likes=35, replies=6, reposts=5, quotes=4, bookmarks=14),
        CascadeSnapshotPoint(age_minutes=30, views=3_400, likes=120, replies=24, reposts=19, quotes=18, bookmarks=52),
        CascadeSnapshotPoint(age_minutes=45, views=7_800, likes=310, replies=68, reposts=54, quotes=55, bookmarks=140),
    ]
    surge_est = estimate_seismic(
        age_minutes=45,
        views=7_800,
        likes=310,
        replies=68,
        reposts=54,
        quotes=55,
        bookmarks=140,
        snapshots=surge_snaps,
    )

    assert 0.0 <= flat_est.infectiousness < 0.25
    assert 0.65 < surge_est.infectiousness <= 1.0
    assert surge_est.infectiousness > flat_est.infectiousness
    assert surge_est.acceleration_factor > flat_est.acceleration_factor

    # Signal weight hierarchy: adding 20 quotes (3.0x) lifts p_t more than 20 likes (0.5x)
    likes_only = estimate_seismic(age_minutes=30, views=3_000, likes=40)
    quotes_bookmarks = estimate_seismic(age_minutes=30, views=3_000, likes=40, quotes=20, bookmarks=20)
    assert quotes_bookmarks.infectiousness > likes_only.infectiousness

    # Bayesian prior regularization prevents tiny-view noise (e.g. 12 views, 2 bookmarks)
    # from saturating p_t compared to sustained 6,000-view cascade at the same raw rate:
    tiny_noise = estimate_seismic(age_minutes=15, views=12, bookmarks=2, quotes=1)
    sustained_viral = estimate_seismic(age_minutes=30, views=6_000, bookmarks=500, quotes=250)
    assert tiny_noise.infectiousness < 0.45
    assert sustained_viral.infectiousness > 0.78
    assert sustained_viral.infectiousness > tiny_noise.infectiousness


# =============================================================================
# 3. Stage 1 + Stage 2 Scorer: DPP Crowding & Mutual 20x vs OON Quote Guidance
# =============================================================================


def test_two_stage_scorer_dpp_crowding_and_workspace_handle_guidance() -> None:
    now = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)

    ws1 = Workspace(
        id=1,
        name="Operator Workspace",
        slug="operator-ws",
        my_x_handle="operator_hq",
        watch_threshold=65.0,
        surge_threshold=82.0,
    )
    ws2_no_handle = Workspace(
        id=2,
        name="Ecosystem Workspace",
        slug="ecosystem-ws",
        my_x_handle=None,
        watch_threshold=64.0,
        surge_threshold=80.0,
    )

    mutual_author = TrackedAccount(
        id=10,
        workspace_id=1,
        x_user_id="10010",
        handle="tensor_architect",
        display_name="Elena Vance",
        followers_count=142_000,
        follows_me=True,
        followed_by_me=True,
        relationship_state="mutual_active",
        heavy_hitter_score=88.0,
        user_cred_v2=74.0,
        baseline_curves_json=default_baseline_curves(22_000),
    )
    oon_author = TrackedAccount(
        id=11,
        workspace_id=1,
        x_user_id="10011",
        handle="model_router",
        display_name="Nadia Al-Mansoor",
        followers_count=44_900,
        follows_me=False,
        followed_by_me=True,
        relationship_state="outbound_warming",
        heavy_hitter_score=76.0,
        user_cred_v2=65.0,
        baseline_curves_json=default_baseline_curves(8_000),
    )

    # Topic 0 (original KV-cache post) & Topic 8 (near-duplicate KV-cache post) from WS1_LIVE_POST_TOPICS
    orig_text = WS1_LIVE_POST_TOPICS[0][2]
    dup_text = WS1_LIVE_POST_TOPICS[8][2]

    orig_post = Post(
        id=101,
        workspace_id=1,
        x_post_id="1895001000",
        author_account_id=mutual_author.id,
        text=orig_text,
        created_at_x=now - timedelta(minutes=22),
        is_reply=False,
        is_retweet=False,
        has_media=True,
        media_type="photo",
        embedding_json=compute_text_embedding(orig_text),
        latest_views=12_500,
        latest_likes=320,
        latest_replies=62,
        latest_quotes=28,
        latest_reposts=44,
        latest_bookmarks=110,
    )
    dup_post = Post(
        id=102,
        workspace_id=1,
        x_post_id="1895001008",
        author_account_id=oon_author.id,
        text=dup_text,
        created_at_x=now - timedelta(minutes=29),
        is_reply=False,
        is_retweet=False,
        has_media=False,
        embedding_json=compute_text_embedding(dup_text),
        latest_views=4_200,
        latest_likes=95,
        latest_replies=18,
        latest_quotes=9,
        latest_reposts=14,
        latest_bookmarks=36,
    )
    catalyst = PostInteraction(
        id=1,
        workspace_id=1,
        post_id=101,
        actor_account_id=oon_author.id,
        actor_x_user_id=oon_author.x_user_id,
        actor_handle=oon_author.handle,
        actor_followers=oon_author.followers_count,
        interaction_type="quote",
        occurred_at=orig_post.created_at_x + timedelta(minutes=10),
    )

    batch_results = score_radar_batch(
        ws1,
        [
            RadarCandidateBundle(post=orig_post, author=mutual_author, interactions=[catalyst]),
            RadarCandidateBundle(post=dup_post, author=oon_author),
        ],
        actor_accounts_by_id={oon_author.id: oon_author},
        now=now,
    )
    orig_res, dup_res = batch_results[0], batch_results[1]

    # DPP semantic crowding discount: near-duplicate KV-cache post receives lower multiplier
    assert orig_res.dpp_novelty_multiplier == pytest.approx(1.0, abs=1e-4)
    assert dup_res.dpp_novelty_multiplier < orig_res.dpp_novelty_multiplier

    # Tight semantic duplicate triggers <0.85 strong crowding discount
    close_dup_text = (
        "We cut KV-cache memory 58% in production without dropping accuracy on tool-calling tasks. "
        "Asymmetric sink-token pinning + chunked prefill scheduling across 1M requests."
    )
    close_dup_post = Post(
        id=104,
        workspace_id=1,
        x_post_id="1895001099",
        author_account_id=oon_author.id,
        text=close_dup_text,
        created_at_x=now - timedelta(minutes=25),
        embedding_json=compute_text_embedding(close_dup_text),
        latest_views=3_800,
        latest_likes=80,
    )
    close_dup_res = score_single_post(
        ws1,
        close_dup_post,
        oon_author,
        reference_embeddings=[orig_res.embedding],
        now=now,
    )
    assert close_dup_res.dpp_novelty_multiplier < 0.85

    # Workspace 1 (my_x_handle="operator_hq"): mutual -> reply_mutual_20x (+15.0 boost = 20.0),
    # non-mutual -> quote_oon_safe
    assert orig_res.breakout_score >= ws1.surge_threshold
    assert orig_res.recommended_action == "reply_mutual_20x"
    assert orig_res.value_breakdown.bidirectional_boost_active is True
    reply_head = next(h for h in orig_res.value_breakdown.heads if h.head == "reply")
    assert reply_head.effective_weight == pytest.approx(20.0)
    assert reply_head.is_boosted is True

    assert dup_res.breakout_score >= ws1.watch_threshold
    assert dup_res.recommended_action == "quote_oon_safe"
    assert dup_res.value_breakdown.bidirectional_boost_active is False

    # Workspace 2 (my_x_handle=None): even if author flags follows_me/followed_by_me=True,
    # pure Ecosystem Mode ALWAYS recommends quote_oon_safe above watch threshold
    ws2_res = score_single_post(
        ws2_no_handle,
        orig_post,
        mutual_author,
        interactions=[catalyst],
        actor_accounts_by_id={oon_author.id: oon_author},
        now=now,
    )
    assert ws2_res.breakout_score >= ws2_no_handle.watch_threshold
    assert ws2_res.recommended_action == "quote_oon_safe"
    assert ws2_res.value_breakdown.bidirectional_boost_active is False

    # Per-community threshold override respected
    strict_comm = Community(
        id=5,
        workspace_id=1,
        name="High Bar Circle",
        slug="high-bar",
        color="#38bdf8",
        watch_threshold_override=95.0,
        surge_threshold_override=99.0,
    )
    overridden_res = score_single_post(
        ws1,
        dup_post,
        oon_author,
        communities=[strict_comm],
        now=now,
    )
    assert overridden_res.effective_watch_threshold == 95.0
    assert overridden_res.alert_tier == "none"
    assert overridden_res.recommended_action == "watch"

    # Stage 1 OONRetweetReplyFilter drops OON retweet -> score 0.0
    oon_rt = Post(
        id=103,
        workspace_id=2,
        x_post_id="1895009999",
        author_account_id=oon_author.id,
        text="RT @someone big announcement",
        created_at_x=now - timedelta(minutes=20),
        is_reply=False,
        is_retweet=True,
        latest_views=50_000,
        latest_likes=1_000,
    )
    rt_res = score_single_post(ws2_no_handle, oon_rt, oon_author, now=now)
    assert rt_res.stage1_filters.passed_oon_retweet_reply is False
    assert rt_res.breakout_score == 0.0


# =============================================================================
# 4. HMAC-SHA256 Signature, Tier Deduplication, Quiet Hours & Hourly Rate Cap
# =============================================================================


@respx.mock
async def test_hmac_webhooks_dedup_quiet_hours_and_rate_cap(
    radar_client: httpx.AsyncClient,
) -> None:
    hook_url = "https://hooks.example.com/tweettracker"
    captured_requests: list[httpx.Request] = []

    def _mock_receiver(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"ok": True})

    respx.post(hook_url).mock(side_effect=_mock_receiver)

    # Verify overnight quiet hours helper across midnight ("23:00" -> "06:30")
    assert is_within_quiet_hours(datetime(2026, 11, 1, 23, 30, tzinfo=timezone.utc), "23:00", "06:30") is True
    assert is_within_quiet_hours(datetime(2026, 11, 2, 2, 15, tzinfo=timezone.utc), "23:00", "06:30") is True
    assert is_within_quiet_hours(datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc), "23:00", "06:30") is False
    assert is_within_quiet_hours(datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc), "23:00", "06:30") is False

    session_factory = get_session_factory()
    hub = RadarSSEHub()
    active_time = datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)
    quiet_time = datetime(2026, 11, 1, 23, 45, tzinfo=timezone.utc)

    async with session_factory() as session:
        ws = Workspace(
            name="Webhook Policy Test WS",
            slug="webhook-policy-ws",
            my_x_handle="operator_hq",
            watch_threshold=65.0,
            surge_threshold=82.0,
            quiet_hours_start="23:00",
            quiet_hours_end="06:30",
            max_alerts_per_hour=2,
            webhook_urls_json=[hook_url],
        )
        session.add(ws)
        await session.flush()

        author = TrackedAccount(
            workspace_id=ws.id,
            x_user_id="555001",
            handle="alpha_founder",
            display_name="Alpha Founder",
            followers_count=55_000,
            follows_me=True,
            followed_by_me=True,
            relationship_state="mutual_active",
            heavy_hitter_score=82.0,
            user_cred_v2=68.0,
            baseline_curves_json=default_baseline_curves(8_000),
        )
        session.add(author)
        await session.flush()

        post1 = Post(
            workspace_id=ws.id,
            x_post_id="2990001",
            author_account_id=author.id,
            text="Shipping zero-copy RPC framing in Rust.",
            created_at_x=active_time - timedelta(minutes=20),
            latest_views=4_200,
            latest_likes=100,
            latest_replies=18,
            latest_quotes=8,
            latest_bookmarks=35,
            breakout_score=74.0,
        )
        session.add(post1)
        await session.flush()

        # 4a. Quiet-hours suppression check (at 23:45 UTC inside 23:00..06:30)
        watch_scored = score_single_post(ws, post1, author, now=active_time)
        watch_scored.breakout_score = 74.0
        watch_scored.alert_tier = "watch"

        quiet_out = await dispatch_breakout_alert(
            session,
            ws,
            post1,
            author,
            watch_scored,
            now=quiet_time,
            sse_hub=hub,
            retry_delays=(0.0,),
        )
        assert len(captured_requests) == 0
        assert len(quiet_out.suppressed_deliveries) == 1
        assert quiet_out.suppressed_deliveries[0].status == "suppressed_quiet_hours"

        # 4b. Active-hours delivery + HMAC-SHA256 verification
        d1 = await dispatch_breakout_alert(
            session,
            ws,
            post1,
            author,
            watch_scored,
            now=active_time,
            sse_hub=hub,
            retry_delays=(0.0,),
        )
        assert len(d1.webhook_outcomes) == 1
        assert d1.webhook_outcomes[0].delivery.status == "delivered"
        assert len(captured_requests) == 1

        req = captured_requests[0]
        sig_hdr = req.headers["X-TweetTracker-Signature-256"]
        expected_hex = hmac.new(
            b"epic2-test-hmac-secret-from-env",
            req.content,
            hashlib.sha256,
        ).hexdigest()
        assert sig_hdr == f"sha256={expected_hex}"
        assert verify_hmac_signature(req.content, sig_hdr) is True
        assert compute_hmac_signature(req.content) == sig_hdr
        assert req.headers["X-TweetTracker-Event"] == "breakout.watch"
        assert len(req.headers["X-TweetTracker-Delivery-Id"]) >= 16

        # 4c. Tier deduplication: second `watch` on post1 is NOT re-sent
        d2 = await dispatch_breakout_alert(
            session,
            ws,
            post1,
            author,
            watch_scored,
            now=active_time + timedelta(minutes=5),
            sse_hub=hub,
            retry_delays=(0.0,),
        )
        assert len(captured_requests) == 1
        assert hook_url in d2.deduplicated_webhook_urls

        # 4d. Upgrade `watch -> surge` on post1 fires ONCE (this reaches 2/2 hourly cap!)
        surge_scored = score_single_post(ws, post1, author, now=active_time)
        surge_scored.breakout_score = 90.5
        surge_scored.alert_tier = "surge"

        d3 = await dispatch_breakout_alert(
            session,
            ws,
            post1,
            author,
            surge_scored,
            now=active_time + timedelta(minutes=10),
            sse_hub=hub,
            retry_delays=(0.0,),
        )
        assert len(d3.webhook_outcomes) == 1
        assert len(captured_requests) == 2
        assert captured_requests[1].headers["X-TweetTracker-Event"] == "breakout.surge"

        # 4e. Duplicate `surge` AND subsequent lower `watch` on post1 are BOTH deduplicated
        await dispatch_breakout_alert(
            session,
            ws,
            post1,
            author,
            surge_scored,
            now=active_time + timedelta(minutes=12),
            sse_hub=hub,
            retry_delays=(0.0,),
        )
        await dispatch_breakout_alert(
            session,
            ws,
            post1,
            author,
            watch_scored,
            now=active_time + timedelta(minutes=14),
            sse_hub=hub,
            retry_delays=(0.0,),
        )
        assert len(captured_requests) == 2

        # 4f. `max_alerts_per_hour = 2` cap suppresses a 3rd webhook within the same hour
        post2 = Post(
            workspace_id=ws.id,
            x_post_id="2990002",
            author_account_id=author.id,
            text="Second independent breakout post within the same hour.",
            created_at_x=active_time - timedelta(minutes=12),
            latest_views=6_000,
            breakout_score=86.0,
        )
        session.add(post2)
        await session.flush()

        p2_scored = score_single_post(ws, post2, author, now=active_time)
        p2_scored.breakout_score = 86.0
        p2_scored.alert_tier = "surge"

        d_capped = await dispatch_breakout_alert(
            session,
            ws,
            post2,
            author,
            p2_scored,
            now=active_time + timedelta(minutes=20),
            sse_hub=hub,
            retry_delays=(0.0,),
        )
        assert len(captured_requests) == 2
        assert len(d_capped.suppressed_deliveries) == 1
        assert d_capped.suppressed_deliveries[0].status == "suppressed_rate_limit"

        await session.commit()


# =============================================================================
# 5. FastAPI Radar Feed Filters, Post Detail, Poll, SSE Stream & Test Webhook
# =============================================================================


@respx.mock
async def test_radar_api_routes_filters_detail_poll_sse_and_webhook_test(
    radar_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces = (await radar_client.get("/api/workspaces")).json()
    ws1, ws2 = workspaces[0], workspaces[1]
    ws1_id = ws1["id"]
    ws2_id = ws2["id"]
    comm0_id = ws1["communities"][0]["id"]

    # Base WS1 Radar feed
    feed_resp = await radar_client.get(f"/api/workspaces/{ws1_id}/radar")
    assert feed_resp.status_code == 200
    feed = feed_resp.json()
    assert feed["total_matching"] == 14
    assert feed["surge_count"] >= 1
    assert feed["query_ms"] < 500.0
    first_card = feed["items"][0]
    assert "x_links" in first_card
    assert "intent/tweet?in_reply_to=" in first_card["x_links"]["reply_intent_url"]
    assert len(first_card["author"]["communities"]) >= 1

    # Filter 1: community_id
    c_feed = (
        await radar_client.get(f"/api/workspaces/{ws1_id}/radar?community_id={comm0_id}")
    ).json()
    assert c_feed["total_matching"] > 0
    for item in c_feed["items"]:
        c_ids = [c["id"] for c in item["author"]["communities"]]
        assert comm0_id in c_ids

    # Filter 2: bridges_only
    b_feed = (
        await radar_client.get(f"/api/workspaces/{ws1_id}/radar?bridges_only=true")
    ).json()
    assert b_feed["total_matching"] > 0
    for item in b_feed["items"]:
        assert item["author"]["is_bridge"] is True
        assert (
            len(item["author"]["communities"]) >= 2
            or item["author"]["bridge_score"] >= 45.0
        )

    # Filter 3: mutuals_only on WS1 (my_x_handle="operator_hq") vs WS2 (my_x_handle=None)
    m_feed_ws1 = (
        await radar_client.get(f"/api/workspaces/{ws1_id}/radar?mutuals_only=true")
    ).json()
    assert m_feed_ws1["total_matching"] > 0
    for item in m_feed_ws1["items"]:
        assert item["author"]["follows_me"] is True
        assert item["author"]["followed_by_me"] is True
        if item["breakout_score"] >= item["effective_watch_threshold"]:
            assert item["recommended_action"] == "reply_mutual_20x"

    m_feed_ws2 = (
        await radar_client.get(f"/api/workspaces/{ws2_id}/radar?mutuals_only=true")
    ).json()
    assert m_feed_ws2["total_matching"] == 0

    ws2_all = (await radar_client.get(f"/api/workspaces/{ws2_id}/radar")).json()
    assert ws2_all["total_matching"] > 0
    for item in ws2_all["items"]:
        assert item["recommended_action"] in ("quote_oon_safe", "watch")

    # Filter 4: max_age_minutes & tier
    age30 = (
        await radar_client.get(f"/api/workspaces/{ws1_id}/radar?max_age_minutes=30")
    ).json()
    assert 0 < age30["total_matching"] < feed["total_matching"]
    for item in age30["items"]:
        assert item["age_minutes"] <= 31

    surge_only = (
        await radar_client.get(f"/api/workspaces/{ws1_id}/radar?tier=surge")
    ).json()
    assert surge_only["total_matching"] == feed["surge_count"]
    for item in surge_only["items"]:
        assert item["alert_tier"] == "surge"

    # Post trajectory & 29-head ValueModel breakdown endpoint
    target_post_id = first_card["id"]
    detail_resp = await radar_client.get(
        f"/api/workspaces/{ws1_id}/radar/posts/{target_post_id}"
    )
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["post"]["id"] == target_post_id
    assert len(detail["value_model_breakdown"]["heads"]) == 29
    assert len(detail["trajectory_comparison"]) >= len(BASELINE_AGES)
    assert detail["algo_version"]["upstream_commit_sha"] == "9106e9f"

    # Immediate Radar Poll endpoint (preserves matured 48h posts)
    poll_resp = await radar_client.post(f"/api/workspaces/{ws1_id}/radar/poll")
    assert poll_resp.status_code == 200
    poll_data = poll_resp.json()
    assert poll_data["active_posts_count"] == 14
    assert len(poll_data["top_items"]) > 0

    ov_after_poll = (
        await radar_client.get(f"/api/workspaces/{ws1_id}/overview")
    ).json()
    assert ov_after_poll["matured_posts_count"] == 50

    # Test HMAC-SHA256 Webhook endpoint & Alerts audit history
    test_hook_url = "https://hooks.example.com/ping"
    respx.post(test_hook_url).mock(return_value=httpx.Response(202, json={"received": True}))

    wh_resp = await radar_client.post(
        f"/api/workspaces/{ws1_id}/webhooks/test",
        json={"target_url": test_hook_url, "note": "Pytest verification"},
    )
    assert wh_resp.status_code == 200
    wh_data = wh_resp.json()
    assert wh_data["event_type"] == "webhook.test"
    assert wh_data["signature_header"] == "X-TweetTracker-Signature-256"
    assert wh_data["signature_preview"].startswith("sha256=")
    assert wh_data["webhook_secret_configured"] is True
    assert wh_data["deliveries"][0]["status"] == "delivered"
    assert wh_data["deliveries"][0]["http_status"] == 202

    alerts_resp = await radar_client.get(f"/api/workspaces/{ws1_id}/alerts")
    assert alerts_resp.status_code == 200
    alerts_data = alerts_resp.json()
    assert alerts_data["total_returned"] >= 2
    channels_seen = {a["channel"] for a in alerts_data["items"]}
    assert "sse" in channels_seen
    assert "webhook" in channels_seen

    # Live SSE endpoint + ?token= query param auth when TWEETTRACKER_ADMIN_PASSWORD is set
    monkeypatch.setenv("TWEETTRACKER_ADMIN_PASSWORD", "sse-secret-admin-pw")
    unauth_sse = await radar_client.get(f"/api/workspaces/{ws1_id}/events?max_events=1")
    assert unauth_sse.status_code == 401

    wrong_sse = await radar_client.get(
        f"/api/workspaces/{ws1_id}/events?max_events=1&token=wrong"
    )
    assert wrong_sse.status_code == 401

    auth_sse = await radar_client.get(
        f"/api/workspaces/{ws1_id}/events?max_events=1&token=sse-secret-admin-pw"
    )
    assert auth_sse.status_code == 200
    assert auth_sse.headers["content-type"].startswith("text/event-stream")
    assert "event: connected" in auth_sse.text
    parsed_lines = [
        line[len("data: ") :]
        for line in auth_sse.text.splitlines()
        if line.startswith("data: ")
    ]
    assert len(parsed_lines) == 1
    connected_payload = json.loads(parsed_lines[0])
    assert connected_payload["workspace_id"] == ws1_id
    assert connected_payload["status"] == "live"


@respx.mock
async def test_webhook_retry_backoff_and_sse_fanout_hub(
    radar_client: httpx.AsyncClient,
) -> None:
    flaky_url = "https://hooks.example.com/flaky"
    call_counter = 0

    def _flaky_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal call_counter
        call_counter += 1
        if call_counter == 1:
            return httpx.Response(502, json={"error": "bad gateway"})
        return httpx.Response(200, json={"ok": True})

    respx.post(flaky_url).mock(side_effect=_flaky_handler)

    session_factory = get_session_factory()
    async with session_factory() as session:
        outcome = await dispatch_signed_webhook(
            session,
            workspace_id=1,
            post_id=None,
            target_url=flaky_url,
            event_type="breakout.surge",
            alert_tier="surge",
            payload={"event_type": "breakout.surge", "score": 91.2},
            max_attempts=3,
            retry_delays=(0.0, 0.0),
        )
        await session.commit()

        assert call_counter == 2
        assert outcome.delivery.status == "delivered"
        assert outcome.delivery.http_status == 200
        assert outcome.delivery.attempts == 2

    # Verify per-workspace SSE pub/sub queue isolation & unsubscribe cleanup
    hub = RadarSSEHub()
    q_ws1 = hub.subscribe(1)
    q_ws2 = hub.subscribe(2)
    assert hub.subscriber_count(1) == 1
    assert hub.subscriber_count(2) == 1

    hub.publish(1, "breakout.surge", {"x_post_id": "1895001000", "breakout_score": 92.4})
    assert not q_ws1.empty()
    assert q_ws2.empty()

    msg = q_ws1.get_nowait()
    assert msg.event_type == "breakout.surge"
    assert "event: breakout.surge" in msg.encode_sse()

    hub.unsubscribe(1, q_ws1)
    hub.unsubscribe(2, q_ws2)
    assert hub.subscriber_count() == 0

