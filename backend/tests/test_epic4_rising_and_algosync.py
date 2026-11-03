"""Comprehensive Epic 4 (<50k Rising Voices) & Upstream Algo Sync Watcher pytest suite."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.algo_sync import (
    GITHUB_API_BASE,
    UPSTREAM_XALGO_REPO,
    check_upstream_algo_sync,
    is_watched_ranking_path,
    parse_patch_param_diffs,
    resolve_github_token,
)
from tweettracker.api.main import create_app
from tweettracker.db import configure_database, dispose_database, get_session_factory
from tweettracker.models import (
    AccountCommunityMembership,
    AlgoParamVersion,
    Community,
    DiscoveredCandidate,
    PeerEndorsement,
    RelationshipEdge,
    TrackedAccount,
    UpstreamAlgoEvent,
    Workspace,
)
from tweettracker.rising import (
    COLD_START_FOLLOWER_CAP,
    CandidateSamplePost,
    EndorsementScoringItem,
    compute_combined_rising_score,
    compute_community_projection,
    compute_peer_respect_score,
    compute_punch_above_weight_score,
    dismiss_candidate,
    evaluate_sampled_posts,
    promote_candidate_to_community,
    refresh_rising_candidates,
    restore_candidate,
)
from tweettracker.seed import bootstrap_database


@pytest.fixture(autouse=True)
def _isolate_test_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantees tests never inherit live GITHUB_TOKEN / LLM keys unless explicitly set in a test."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("TWEETTRACKER_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("TWEETTRACKER_GITHUB_REPO", "gentoftech/tweettracker")


@pytest.fixture
async def seeded_env(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[httpx.AsyncClient, AsyncSession], None]:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    engine, session_factory = configure_database("sqlite+aiosqlite:///:memory:")
    await bootstrap_database(engine, session_factory, auto_migrate=True, seed_fixtures=True)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    factory = get_session_factory()
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
        factory() as session,
    ):
        yield client, session

    await dispose_database()


def test_cold_start_cap_and_one_hit_wonder_penalty() -> None:
    """Verifies <50k cap, original-post filtering, and >=3 of >=5 >=3.0x consistency gate."""
    assert COLD_START_FOLLOWER_CAP == 50_000

    # 50,000+ followers strictly rejected (0.0 score)
    over_cap = compute_punch_above_weight_score(
        followers_count=50_000,
        sampled_posts_count=8,
        posts_above_3x_count=7,
        median_reach_multiple=9.0,
        followers_growth_rate=0.25,
    )
    assert over_cap == 0.0

    just_under_cap = compute_punch_above_weight_score(
        followers_count=49_999,
        sampled_posts_count=8,
        posts_above_3x_count=6,
        median_reach_multiple=6.0,
        followers_growth_rate=0.15,
    )
    assert just_under_cap > 70.0

    # Sampled post evaluator ignores replies and retweets, penalizing one-hit wonders
    one_hit_samples = [
        CandidateSamplePost("p1", "1 viral fluke", views=120_000, likes=2_400),  # 30x
        CandidateSamplePost("p2", "normal post", views=3_200, likes=45),  # 0.8x
        CandidateSamplePost("p3", "normal post 2", views=3_600, likes=50),  # 0.9x
        CandidateSamplePost("p4", "normal post 3", views=2_800, likes=35),  # 0.7x
        CandidateSamplePost("p5", "normal post 4", views=4_000, likes=55),  # 1.0x
        CandidateSamplePost("r1", "viral reply ignored", views=95_000, is_reply=True),
        CandidateSamplePost("rt1", "viral RT ignored", views=150_000, is_retweet=True),
    ]
    one_hit_eval = evaluate_sampled_posts(one_hit_samples, followers_count=4_000)
    assert one_hit_eval.sampled_posts_count == 5
    assert one_hit_eval.posts_above_3x_count == 1
    assert one_hit_eval.median_reach_multiple == 0.9

    consistent_samples = [
        CandidateSamplePost(f"c{i}", f"breakout {i}", views=20_000 + i * 2_000, likes=520)
        for i in range(4)
    ] + [CandidateSamplePost("c4", "solid base", views=6_000, likes=120)]
    consistent_eval = evaluate_sampled_posts(consistent_samples, followers_count=4_000)
    assert consistent_eval.sampled_posts_count == 5
    assert consistent_eval.posts_above_3x_count == 4
    assert consistent_eval.median_reach_multiple >= 5.0

    one_hit_score = compute_punch_above_weight_score(
        followers_count=4_000,
        sampled_posts_count=one_hit_eval.sampled_posts_count,
        posts_above_3x_count=one_hit_eval.posts_above_3x_count,
        median_reach_multiple=one_hit_eval.median_reach_multiple,
        followers_growth_rate=0.10,
        cold_start_propensity_lift=one_hit_eval.cold_start_propensity_lift,
    )
    consistent_score = compute_punch_above_weight_score(
        followers_count=4_000,
        sampled_posts_count=consistent_eval.sampled_posts_count,
        posts_above_3x_count=consistent_eval.posts_above_3x_count,
        median_reach_multiple=consistent_eval.median_reach_multiple,
        followers_growth_rate=0.10,
        cold_start_propensity_lift=consistent_eval.cold_start_propensity_lift,
    )
    assert one_hit_score < 20.0
    assert consistent_score > 75.0

    # Direct gate check with identical median_reach_multiple: 2/6 above 3x < 4/6 above 3x
    two_of_six = compute_punch_above_weight_score(
        followers_count=5_000,
        sampled_posts_count=6,
        posts_above_3x_count=2,
        median_reach_multiple=3.4,
        followers_growth_rate=0.12,
    )
    four_of_six = compute_punch_above_weight_score(
        followers_count=5_000,
        sampled_posts_count=6,
        posts_above_3x_count=4,
        median_reach_multiple=3.4,
        followers_growth_rate=0.12,
    )
    assert four_of_six > two_of_six + 20.0


def test_peer_respect_signal_hierarchy_authority_and_diversity() -> None:
    """Verifies 3x follow > 2x quote > 1x reply, HH/SALSA scaling, and 4 peers > 4 replies from 1 friend."""
    reply_score, _ = compute_peer_respect_score(
        [EndorsementScoringItem(1, endorser_account_id=10, endorsement_type="reply", heavy_hitter_score=80.0, salsa_scout=0.7)]
    )
    quote_score, _ = compute_peer_respect_score(
        [EndorsementScoringItem(2, endorser_account_id=10, endorsement_type="quote", heavy_hitter_score=80.0, salsa_scout=0.7)]
    )
    follow_score, _ = compute_peer_respect_score(
        [EndorsementScoringItem(3, endorser_account_id=10, endorsement_type="follow", heavy_hitter_score=80.0, salsa_scout=0.7)]
    )
    assert follow_score > quote_score > reply_score > 0.0

    # Endorser authority + SALSA scout scaling
    low_auth_follow, _ = compute_peer_respect_score(
        [EndorsementScoringItem(4, endorser_account_id=11, endorsement_type="follow", heavy_hitter_score=20.0, salsa_scout=0.15)]
    )
    high_auth_follow, _ = compute_peer_respect_score(
        [EndorsementScoringItem(5, endorser_account_id=12, endorsement_type="follow", heavy_hitter_score=95.0, salsa_scout=0.92)]
    )
    assert high_auth_follow > low_auth_follow * 1.8

    # 4 distinct Heavy Hitters beat 4 replies from 1 friend
    one_friend_spam, spam_contribs = compute_peer_respect_score(
        [
            EndorsementScoringItem(100 + i, endorser_account_id=50, endorsement_type="reply", heavy_hitter_score=82.0, salsa_scout=0.75)
            for i in range(4)
        ]
    )
    four_distinct_peers, _ = compute_peer_respect_score(
        [
            EndorsementScoringItem(200 + i, endorser_account_id=60 + i, endorsement_type="reply", heavy_hitter_score=82.0, salsa_scout=0.75)
            for i in range(4)
        ]
    )
    assert four_distinct_peers > one_friend_spam * 2.0
    assert spam_contribs[100] > spam_contribs[103]

    assert compute_combined_rising_score(80.0, 60.0) == 71.0


async def test_k_community_projection_across_ws1_k3_and_ws2_k4_and_promotion(
    seeded_env: tuple[httpx.AsyncClient, AsyncSession],
) -> None:
    """Verifies K=3 & K=4 SimClusters projection summing to 1.0, plus 1-click promote & dismiss/restore."""
    _client, session = seeded_env
    workspaces = list((await session.execute(select(Workspace).order_by(Workspace.id.asc()))).scalars().all())
    ws1, ws2 = workspaces[0], workspaces[1]

    ws1_refreshed = await refresh_rising_candidates(session, ws1.id)
    ws2_refreshed = await refresh_rising_candidates(session, ws2.id)
    assert len(ws1_refreshed) == 8
    assert len(ws2_refreshed) == 6

    ws1_slugs = {
        c.slug
        for c in (await session.execute(select(Community).where(Community.workspace_id == ws1.id))).scalars().all()
    }
    ws2_slugs = {
        c.slug
        for c in (await session.execute(select(Community).where(Community.workspace_id == ws2.id))).scalars().all()
    }
    assert len(ws1_slugs) == 3
    assert len(ws2_slugs) == 4

    for cand in ws1_refreshed:
        assert cand.followers_count < COLD_START_FOLLOWER_CAP
        assert abs(sum(cand.community_projection_json.values()) - 1.0) < 1e-3
        assert set(cand.community_projection_json.keys()).issubset(ws1_slugs)

    for cand in ws2_refreshed:
        assert cand.followers_count < COLD_START_FOLLOWER_CAP
        assert abs(sum(cand.community_projection_json.values()) - 1.0) < 1e-3
        assert set(cand.community_projection_json.keys()).issubset(ws2_slugs)

    # Test multi-community K=4 projection directly when endorsers span all 4 WS2 circles
    ws2_comms = list((await session.execute(select(Community).where(Community.workspace_id == ws2.id))).scalars().all())
    ws2_accts = {
        a.id: a
        for a in (await session.execute(select(TrackedAccount).where(TrackedAccount.workspace_id == ws2.id))).scalars().all()
    }
    ws2_mems_raw = list(
        (await session.execute(select(AccountCommunityMembership).where(AccountCommunityMembership.workspace_id == ws2.id)))
        .scalars()
        .all()
    )
    mems_by_acct: dict[int, list[AccountCommunityMembership]] = {}
    for m in ws2_mems_raw:
        mems_by_acct.setdefault(m.account_id, []).append(m)

    # Pick one endorser from each of the 4 communities
    synthetic_ends = [
        PeerEndorsement(
            workspace_id=ws2.id,
            candidate_id=ws2_refreshed[0].id,
            endorser_account_id=next(m.account_id for m in ws2_mems_raw if m.community_id == c.id),
            endorsement_type="quote",
        )
        for c in ws2_comms
    ]
    k4_proj = compute_community_projection(ws2_comms, synthetic_ends, ws2_accts, mems_by_acct)
    assert len(k4_proj) == 4
    assert abs(sum(k4_proj.values()) - 1.0) < 1e-3

    # Test 1-click promotion in WS1 -> TrackedAccount + Memberships + RelationshipEdges
    target_cand = ws1_refreshed[0]
    ws1_comms = list((await session.execute(select(Community).where(Community.workspace_id == ws1.id))).scalars().all())
    chosen_comm_ids = [ws1_comms[0].id, ws1_comms[1].id]
    promoted_acct = await promote_candidate_to_community(
        session,
        workspace_id=ws1.id,
        candidate_id=target_cand.id,
        community_ids=chosen_comm_ids,
    )
    assert promoted_acct.is_tracked is True
    assert promoted_acct.handle == target_cand.handle
    assert promoted_acct.user_cred_v2 > 0.0
    assert "p50" in promoted_acct.baseline_curves_json
    assert promoted_acct.bridge_role == "two_way_bridge"

    mem_rows = list(
        (
            await session.execute(
                select(AccountCommunityMembership).where(
                    AccountCommunityMembership.account_id == promoted_acct.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {m.community_id for m in mem_rows} == set(chosen_comm_ids)

    edge_rows = list(
        (
            await session.execute(
                select(RelationshipEdge).where(
                    RelationshipEdge.workspace_id == ws1.id,
                    RelationshipEdge.target_account_id == promoted_acct.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(edge_rows) >= 3

    refreshed_after_promo = await refresh_rising_candidates(session, ws1.id)
    assert all(c.id != target_cand.id for c in refreshed_after_promo)

    # Dismiss and restore another candidate
    dismiss_target = refreshed_after_promo[0]
    dismissed = await dismiss_candidate(session, ws1.id, dismiss_target.id)
    assert dismissed.status == "dismissed"
    after_dismiss = await refresh_rising_candidates(session, ws1.id)
    assert all(c.id != dismiss_target.id for c in after_dismiss)

    restored = await restore_candidate(session, ws1.id, dismiss_target.id)
    assert restored.status == "active"


@respx.mock
async def test_algo_sync_watcher_ads_ignore_rust_diff_staging_and_issue_deduplication(
    seeded_env: tuple[httpx.AsyncClient, AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies ads/-only ignore, multi-file Rust param!() parsing, staging, and 1-time GitHub issue creation."""
    _client, session = seeded_env
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_mock_test_token_only")
    monkeypatch.setenv("TWEETTRACKER_GITHUB_REPO", "gentoftech/tweettracker")

    # Safety assertion: gh CLI fallback never activates under pytest even if requested
    monkeypatch.delenv("GITHUB_TOKEN")
    tok_empty, src_empty = resolve_github_token(allow_gh_cli_fallback=True)
    assert tok_empty == "" and src_empty == "none"
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_mock_test_token_only")

    assert is_watched_ranking_path("vm-ranker/src/params.rs") is True
    assert is_watched_ranking_path("home-mixer/params/param.rs") is True
    assert is_watched_ranking_path("xai-value-model/src/scoring.rs") is True
    assert is_watched_ranking_path("src/scala/com/twitter/interaction_graph/scio/agg_all/user_cred_v2/UserCredV2.scala") is True
    assert is_watched_ranking_path("ads/pacing/budget_pacing.rs") is False
    assert is_watched_ranking_path(".github/workflows/ci.yml") is False
    assert is_watched_ranking_path("README.md") is False

    # Also test struct literal & SCORE_INTERCEPT constant parsing
    const_diffs = parse_patch_param_diffs(
        "src/scala/com/twitter/interaction_graph/scio/agg_all/user_cred_v2/UserCredV2.scala",
        "@@ -4,3 +4,3 @@\n-val SCORE_INTERCEPT: Double = 165.2\n+val SCORE_INTERCEPT: Double = 168.0\n",
    )
    assert len(const_diffs) == 1
    assert const_diffs[0]["mapped_field"] == "score_intercept"
    assert const_diffs[0]["new_value"] == 168.0

    # 1) ads/-only upstream commit -> status == "ignored_non_ranking", zero GitHub issues created
    compare_url = f"{GITHUB_API_BASE}/repos/{UPSTREAM_XALGO_REPO}/compare/9106e9f...main"
    issues_post_route = respx.post(
        f"{GITHUB_API_BASE}/repos/gentoftech/tweettracker/issues"
    ).mock(
        return_value=httpx.Response(
            201,
            json={
                "number": 42,
                "html_url": "https://github.com/gentoftech/tweettracker/issues/42",
            },
        )
    )

    respx.get(compare_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "commits": [{"sha": "ad50000111111111111111111111111111111111"}],
                "files": [
                    {
                        "filename": "ads/mixer/ad_auction_scorer.rs",
                        "status": "modified",
                        "additions": 18,
                        "deletions": 4,
                        "patch": '@@ -1,3 +1,3 @@\n-param!(AdBidFloor, f64, "ad_bid", 1.0);\n+param!(AdBidFloor, f64, "ad_bid", 1.5);\n',
                    },
                    {
                        "filename": ".github/workflows/rust.yml",
                        "status": "modified",
                        "additions": 2,
                        "deletions": 2,
                    },
                ],
            },
        )
    )

    ignored_outcome = await check_upstream_algo_sync(session)
    assert ignored_outcome.event.status == "ignored_non_ranking"
    assert ignored_outcome.event.github_issue_number is None
    assert ignored_outcome.staged_version is None
    assert issues_post_route.call_count == 0

    # 2) Ranking multi-file Rust diff modifying FavoriteWeight (0.5->0.65),
    #    BidirectionalFollowReplyWeightBoost (15.0->18.0), and DppTheta (0.65->0.70)
    drift_sha = "c0ffee778899aabbccddeeff0011223344556677"
    respx.get(compare_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "commits": [{"sha": drift_sha}],
                "files": [
                    {
                        "filename": "vm-ranker/src/params.rs",
                        "status": "modified",
                        "additions": 2,
                        "deletions": 2,
                        "patch": (
                            "@@ -10,6 +10,6 @@\n"
                            '-param!(FavoriteWeight, f64, "favorite_weight", 0.5);\n'
                            '+param!(FavoriteWeight, f64, "favorite_weight", 0.65);\n'
                            '-param!(BidirectionalFollowReplyWeightBoost, f64, "bidirectional_follow_reply_weight_boost", 15.0);\n'
                            '+param!(BidirectionalFollowReplyWeightBoost, f64, "bidirectional_follow_reply_weight_boost", 18.0);\n'
                        ),
                    },
                    {
                        "filename": "vm-ranker/src/scoring/dpp_model.rs",
                        "status": "modified",
                        "additions": 2,
                        "deletions": 1,
                        "patch": (
                            "@@ -35,4 +35,5 @@ pub fn rescore_dpp() {\n"
                            '-param!(DppTheta, f64, "dpp_theta", 0.65);\n'
                            '+param!(DppTheta, f64, "dpp_theta", 0.70);\n'
                        ),
                    },
                    {
                        "filename": "ads/targeting/ignore_me.rs",
                        "status": "modified",
                        "additions": 5,
                        "deletions": 1,
                    },
                ],
            },
        )
    )

    open_issues_list: list[dict[str, object]] = []
    respx.get(f"{GITHUB_API_BASE}/repos/gentoftech/tweettracker/issues").mock(
        side_effect=lambda _req: httpx.Response(200, json=open_issues_list)
    )
    label_get_route = respx.get(
        f"{GITHUB_API_BASE}/repos/gentoftech/tweettracker/labels/algo-sync"
    ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    label_post_route = respx.post(
        f"{GITHUB_API_BASE}/repos/gentoftech/tweettracker/labels"
    ).mock(return_value=httpx.Response(201, json={"name": "algo-sync"}))

    def _create_issue_side_effect(req: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(req.content.decode("utf-8"))
        created_obj = {
            "number": 42,
            "title": body["title"],
            "body": body["body"],
            "html_url": "https://github.com/gentoftech/tweettracker/issues/42",
        }
        open_issues_list.append(created_obj)
        return httpx.Response(201, json=created_obj)

    issues_post_route.mock(side_effect=_create_issue_side_effect)

    first_run = await check_upstream_algo_sync(session)
    assert first_run.deduplicated is False
    assert first_run.event.status == "drift_detected"
    assert first_run.event.github_issue_number == 42
    assert first_run.event.github_issue_url == "https://github.com/gentoftech/tweettracker/issues/42"
    assert first_run.event.watched_files_changed_json == [
        "vm-ranker/src/params.rs",
        "vm-ranker/src/scoring/dpp_model.rs",
    ]
    assert label_get_route.call_count == 1
    assert label_post_route.call_count == 1
    assert issues_post_route.call_count == 1

    diffs_by_ident = {d["rust_ident"]: d for d in first_run.event.param_diffs_json}
    assert diffs_by_ident["FavoriteWeight"]["mapped_field"] == "favorite"
    assert diffs_by_ident["FavoriteWeight"]["old_value"] == 0.5
    assert diffs_by_ident["FavoriteWeight"]["new_value"] == 0.65
    assert diffs_by_ident["FavoriteWeight"]["pct_change"] == 30.0
    assert diffs_by_ident["BidirectionalFollowReplyWeightBoost"]["new_value"] == 18.0
    assert diffs_by_ident["DppTheta"]["mapped_field"] == "dpp_theta"
    assert diffs_by_ident["DppTheta"]["new_value"] == 0.70

    # Staged AlgoParamVersion verified
    assert first_run.staged_version is not None
    assert first_run.staged_version.is_active is False
    assert first_run.staged_version.source == "upstream_algo_sync"
    assert first_run.staged_version.weights_json["favorite"] == 0.65
    assert first_run.staged_version.weights_json["bidirectional_follow_reply_weight_boost"] == 18.0
    assert first_run.staged_version.thresholds_json["pipeline"]["dpp_theta"] == 0.70

    # 3) Second run for the SAME head_sha -> deduplicated, POST /issues STILL called only ONCE!
    second_run = await check_upstream_algo_sync(session)
    assert second_run.deduplicated is True
    assert second_run.event.id == first_run.event.id
    assert second_run.event.github_issue_number == 42
    assert issues_post_route.call_count == 1

    # 4) Even if DB UpstreamAlgoEvent row is deleted, GitHub issue search deduplicates it!
    await session.delete(first_run.event)
    await session.commit()
    third_run = await check_upstream_algo_sync(session)
    assert third_run.deduplicated is True
    assert third_run.event.github_issue_number == 42
    assert issues_post_route.call_count == 1


@respx.mock
async def test_fastapi_rising_and_algo_sync_routes(
    seeded_env: tuple[httpx.AsyncClient, AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end API route tests for /rising, /promote, /dismiss, and /api/algo-sync/*."""
    client, _session = seeded_env
    workspaces = (await client.get("/api/workspaces")).json()
    ws1, ws2 = workspaces[0], workspaces[1]

    # 1) GET /api/workspaces/{ws1}/rising with bracket, community, sort_by filters
    r_all = await client.get(f"/api/workspaces/{ws1['id']}/rising")
    assert r_all.status_code == 200
    data_all = r_all.json()
    assert data_all["cold_start_follower_cap"] == 50_000
    assert len(data_all["candidates"]) == 8
    first_cand = data_all["candidates"][0]
    assert len(first_cand["endorsements"]) == 3
    assert len(first_cand["community_projection"]) >= 1

    r_under_2500 = (
        await client.get(f"/api/workspaces/{ws1['id']}/rising", params={"bracket": "under_2500"})
    ).json()
    assert len(r_under_2500["candidates"]) >= 1
    assert all(c["followers_count"] < 2_500 for c in r_under_2500["candidates"])

    ai_comm_id = ws1["communities"][0]["id"]
    r_comm = (
        await client.get(
            f"/api/workspaces/{ws1['id']}/rising",
            params={"community_id": ai_comm_id, "sort_by": "punch"},
        )
    ).json()
    assert len(r_comm["candidates"]) >= 2
    scores_punch = [c["punch_above_weight_score"] for c in r_comm["candidates"]]
    assert scores_punch == sorted(scores_punch, reverse=True)

    # Refresh WS2 (K=4) and verify rich endorsements + projections
    refresh_ws2 = await client.post(f"/api/workspaces/{ws2['id']}/rising/refresh")
    assert refresh_ws2.status_code == 200
    assert refresh_ws2.json()["refreshed_count"] == 6
    assert len(refresh_ws2.json()["candidates"][0]["endorsements"]) >= 3

    # Promote candidate via API
    promo_resp = await client.post(
        f"/api/workspaces/{ws1['id']}/rising/{first_cand['id']}/promote",
        json={"community_ids": [ai_comm_id]},
    )
    assert promo_resp.status_code == 200
    promo_json = promo_resp.json()
    assert promo_json["status"] == "promoted"
    assert promo_json["tracked_account"]["handle"] == first_cand["handle"]
    assert ai_comm_id in promo_json["tracked_account"]["community_ids"]

    # Dismiss & restore second candidate via API
    second_cand_id = data_all["candidates"][1]["id"]
    dis_resp = await client.post(
        f"/api/workspaces/{ws1['id']}/rising/{second_cand_id}/dismiss",
        json={"restore": False},
    )
    assert dis_resp.status_code == 200
    assert dis_resp.json()["status"] == "dismissed"

    res_resp = await client.post(
        f"/api/workspaces/{ws1['id']}/rising/{second_cand_id}/dismiss",
        json={"restore": True},
    )
    assert res_resp.status_code == 200
    assert res_resp.json()["status"] == "active"

    # 2) Algo Sync status + dry-run simulation + 1-click activate staged weights
    st_resp = await client.get("/api/algo-sync/status")
    assert st_resp.status_code == 200
    st_json = st_resp.json()
    assert st_json["last_synced_sha"] == "9106e9f"
    assert st_json["target_github_repo"] == "gentoftech/tweettracker"

    sim_resp = await client.post("/api/algo-sync/check", json={"simulate_drift": True})
    assert sim_resp.status_code == 200
    sim_json = sim_resp.json()
    assert sim_json["event"]["status"] == "drift_detected"
    assert len(sim_json["event"]["param_diffs_json"]) == 4
    assert sim_json["staged_param_version"]["is_active"] is False

    act_resp = await client.post(
        f"/api/algo-sync/events/{sim_json['event']['id']}/activate-staged"
    )
    assert act_resp.status_code == 200
    act_json = act_resp.json()
    assert act_json["event"]["status"] == "activated"
    assert act_json["activated_param_version"]["is_active"] is True
    assert act_json["last_synced_sha"].startswith("94a21c8")

    # 3) Admin auth guard on Epic 4 & Algo Sync routes
    monkeypatch.setenv("TWEETTRACKER_ADMIN_PASSWORD", "epic4-test-secret")
    assert (await client.get(f"/api/workspaces/{ws1['id']}/rising")).status_code == 401
    assert (await client.get("/api/algo-sync/status")).status_code == 401
    authed = await client.get(
        "/api/algo-sync/status",
        headers={"Authorization": "Bearer epic4-test-secret"},
    )
    assert authed.status_code == 200


@respx.mock
async def test_llm_structural_diff_summary_and_tracked_overcap_exclusion(
    seeded_env: tuple[httpx.AsyncClient, AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies Gemini/Grok structural diff summarization, fallback, and <50k/tracked exclusions."""
    client, session = seeded_env
    workspaces = list((await session.execute(select(Workspace).order_by(Workspace.id.asc()))).scalars().all())
    ws1 = workspaces[0]

    # Insert an over-cap (75k followers) candidate and an already-tracked handle candidate
    over_cap_cand = DiscoveredCandidate(
        workspace_id=ws1.id,
        x_user_id="777000111",
        handle="big_whale_acct",
        display_name="Big Whale",
        followers_count=75_000,
        sampled_posts_count=10,
        posts_above_3x_count=8,
        median_reach_multiple=6.0,
        status="active",
    )
    already_tracked_cand = DiscoveredCandidate(
        workspace_id=ws1.id,
        x_user_id="777000222",
        handle="tensor_architect",  # Already active TrackedAccount in WS1
        display_name="Elena Vance",
        followers_count=12_000,
        sampled_posts_count=8,
        posts_above_3x_count=6,
        median_reach_multiple=5.0,
        status="active",
    )
    session.add_all([over_cap_cand, already_tracked_cand])
    await session.commit()

    refreshed = await refresh_rising_candidates(session, ws1.id, harvest_from_provider=False)
    refreshed_handles = {c.handle for c in refreshed}
    assert "big_whale_acct" not in refreshed_handles
    assert "tensor_architect" not in refreshed_handles
    await session.refresh(over_cap_cand)
    await session.refresh(already_tracked_cand)
    assert over_cap_cand.status == "over_cap"
    assert already_tracked_cand.status == "promoted"

    # Multi-line param!() + underscored integer literal parsing
    multiline_patch = (
        "@@ -100,12 +100,12 @@\n"
        " param!(\n"
        "     ColdStartFollowerCap,\n"
        "     u64,\n"
        '     "cold_start_follower_cap",\n'
        "-    50_000,\n"
        "+    40_000,\n"
        " );\n"
    )
    ml_diffs = parse_patch_param_diffs("home-mixer/params/param.rs", multiline_patch)
    assert len(ml_diffs) == 1
    assert ml_diffs[0]["rust_ident"] == "ColdStartFollowerCap"
    assert ml_diffs[0]["mapped_field"] == "cold_start_follower_cap"
    assert ml_diffs[0]["old_value"] == 50_000
    assert ml_diffs[0]["new_value"] == 40_000
    assert ml_diffs[0]["pct_change"] == -20.0

    # Structural Rust diff summarized via mocked Gemini LLM
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "mock-gemini-key-only")
    gemini_route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": "Gemini summary: scoring.rs adjusted offset_score normalization for negative weights."
                                }
                            ]
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 90, "candidatesTokenCount": 25},
            },
        )
    )
    compare_url = f"{GITHUB_API_BASE}/repos/{UPSTREAM_XALGO_REPO}/compare/9106e9f...main"
    respx.get(compare_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "commits": [{"sha": "f1e2d3c4b5a697887766554433221100ffeeddcc"}],
                "files": [
                    {
                        "filename": "xai-value-model/src/scoring.rs",
                        "status": "modified",
                        "additions": 6,
                        "deletions": 2,
                        "patch": "@@ -161,4 +161,8 @@ pub fn offset_score(combined_score: f64) -> f64 {\n+    // clamp guard\n",
                    }
                ],
            },
        )
    )

    check_resp = await client.post(
        "/api/algo-sync/check",
        json={"simulate_drift": False, "auto_create_issue": False, "workspace_id": ws1.id},
    )
    assert check_resp.status_code == 200
    assert gemini_route.call_count == 1
    assert "Gemini summary:" in check_resp.json()["event"]["structural_summary"]
