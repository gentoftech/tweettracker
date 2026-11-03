"""Exact mathematical parity tests against xai-org/x-algorithm (9106e9f)."""

from __future__ import annotations

import math

import networkx as nx
import numpy as np

from tweettracker.algo import (
    AgeFilter,
    AuthorColdStart,
    CandidateScoringInputs,
    ColdStartConfig,
    DppCandidate,
    FilterCandidate,
    OONRetweetReplyFilter,
    ObservablePostMetrics,
    PhoenixScores,
    QueryScoringContext,
    SelfReplyChainFilter,
    UserCredV2,
    ValueModelWeights,
    build_candidate_scoring_inputs,
    compute_text_embedding,
    compute_value_scores,
    dpp_greedy_rescore,
    dpp_novelty_multiplier,
    fuse_heads,
    offset_score,
    recommend_engagement_action,
    shrink_propensity,
    slpa_overlapping_communities,
    unoffset_score,
)


def test_hand_computed_parity() -> None:
    """Reproduces the exact 4-candidate test from xai-value-model/src/scoring.rs lines 342-479."""
    weights = ValueModelWeights(
        favorite=2.0,
        reply=4.0,
        retweet=1.0,
        dwell=0.5,
        vqv=3.0,
        cont_dwell_time=0.1,
        cont_click_dwell_time=0.2,
        post_unexplored=1.5,
        post_video=2.5,
        report=-100.0,
        not_interested=-10.0,
        bidirectional_follow_reply_weight_boost=1.0,
        bidirectional_follow_dwell_weight_boost=0.5,
        enable_author_diversity=True,
        author_diversity_decay=0.5,
        author_diversity_floor=0.25,
        oon_rescore_in_network_replies_retweets=True,
    )
    ctx = QueryScoringContext(effective_oon_weight=0.75)

    mutual_original = CandidateScoringInputs(
        phoenix_scores=PhoenixScores(
            favorite_score=0.5,
            reply_score=0.1,
            dwell_score=0.4,
            dwell_time=10.0,
            post_unexplored_score=0.2,
            post_video_score=0.04,
        ),
        author_id=1,
        in_network=True,
        is_mutual_follow_author=True,
    )
    ineligible_video_oon = CandidateScoringInputs(
        phoenix_scores=PhoenixScores(
            favorite_score=0.2,
            vqv_score=0.9,
            click_dwell_time=5.0,
            post_unexplored_score=0.2,
        ),
        author_id=2,
        in_network=False,
        vqv_eligible=False,
    )
    eligible_video_same_author = CandidateScoringInputs(
        phoenix_scores=PhoenixScores(
            favorite_score=0.05,
            vqv_score=0.5,
            click_dwell_time=5.0,
        ),
        author_id=1,
        in_network=True,
        vqv_eligible=True,
    )
    reported_in_network_reply = CandidateScoringInputs(
        phoenix_scores=PhoenixScores(
            favorite_score=0.01,
            report_score=0.05,
        ),
        author_id=3,
        in_network=True,
        is_reply=True,
        is_mutual_follow_author=True,
    )
    candidates = [
        mutual_original,
        ineligible_video_oon,
        eligible_video_same_author,
        reported_in_network_reply,
    ]

    offset = 0.001
    positive_weight_sum = 2.0 + 4.0 + 1.0 + 0.5 + 3.0 + 1.5 + 2.5
    negative_weight_magnitude = 10.0 + 100.0
    total_weight_sum = positive_weight_sum + negative_weight_magnitude

    boosted_reply_weight = 4.0 + 1.0
    boosted_dwell_weight = 0.5 + 0.5
    weighted_mutual_original = (
        2.0 * 0.5
        + boosted_reply_weight * 0.1
        + boosted_dwell_weight * 0.4
        + 0.1 * 10.0
        + 1.5 * 0.2
        + 2.5 * 0.04
        + offset
    )

    weighted_ineligible_video_oon = 2.0 * 0.2 + 0.2 * 5.0 + offset

    weighted_eligible_video_same_author = 2.0 * 0.05 + 3.0 * 0.5 + 0.2 * 5.0 + offset

    net_reported_reply = 2.0 * 0.01 - 100.0 * 0.05
    weighted_reported_reply = (
        (net_reported_reply + negative_weight_magnitude) / total_weight_sum * offset
    )

    diversity_second_from_author = (1.0 - 0.25) * 0.5 + 0.25

    expected_scores = [
        weighted_mutual_original,
        weighted_ineligible_video_oon * 0.75,
        weighted_eligible_video_same_author * diversity_second_from_author,
        weighted_reported_reply * 0.75,
    ]
    expected_weighted = [
        weighted_mutual_original,
        weighted_ineligible_video_oon,
        weighted_eligible_video_same_author,
        weighted_reported_reply,
    ]

    result = compute_value_scores(weights, ctx, candidates)
    for i, (got, want) in enumerate(zip(result.scores, expected_scores, strict=True)):
        assert abs(got - want) < 1e-12, f"candidate {i}: got {got} want {want}"

    for i, (got, want) in enumerate(zip(result.weighted, expected_weighted, strict=True)):
        assert abs(got - want) < 1e-12, f"weighted {i}: got {got} want {want}"

    assert abs(fuse_heads(weights, candidates[0]) - weighted_mutual_original) < 1e-12


def test_offset_and_unoffset_roundtrip() -> None:
    w = ValueModelWeights.vm_ranker_defaults()
    for val in (-15.0, -0.25, 0.0, 0.42, 12.5):
        encoded = offset_score(val, w)
        assert encoded >= 0.0
        decoded = unoffset_score(encoded, w)
        assert abs(decoded - val) < 1e-11


def test_oon_retweet_reply_filter_parity() -> None:
    """Reproduces oon_retweet_reply_filter.rs lines 56-79."""
    flt = OONRetweetReplyFilter()
    candidates = [
        FilterCandidate(tweet_id=1, retweeted_tweet_id=100, in_network=False),
        FilterCandidate(tweet_id=2, in_reply_to_tweet_id=200, ancestors=[200], in_network=False),
        FilterCandidate(tweet_id=3, in_network=False),
        FilterCandidate(tweet_id=4, retweeted_tweet_id=400, in_network=True),
        FilterCandidate(tweet_id=5, in_reply_to_tweet_id=500, ancestors=[500], in_network=True),
        FilterCandidate(tweet_id=6, in_network=True),
        FilterCandidate(tweet_id=7, retweeted_tweet_id=700, in_network=None),
        FilterCandidate(tweet_id=8, in_reply_to_tweet_id=800, ancestors=[], in_network=True),
        FilterCandidate(tweet_id=9, in_reply_to_tweet_id=900, ancestors=[], in_network=None),
    ]
    res = flt.filter(candidates)
    assert [c.tweet_id for c in res.removed] == [1, 2, 8, 9]
    assert [c.tweet_id for c in res.kept] == [3, 4, 5, 6, 7]


def test_self_reply_chain_filter_parity() -> None:
    """Reproduces self_reply_chain_filter.rs unit cases."""
    flt = SelfReplyChainFilter(viewer_id=1, followed_user_ids=[10, 20])
    candidates = [
        FilterCandidate(tweet_id=1, author_id=10, in_reply_to_tweet_id=None, ancestor_users=[]),
        FilterCandidate(tweet_id=2, author_id=10, in_reply_to_tweet_id=100, ancestor_users=[10, 10]),
        FilterCandidate(tweet_id=3, author_id=10, in_reply_to_tweet_id=101, ancestor_users=[10, 20]),
        FilterCandidate(tweet_id=4, author_id=10, in_reply_to_tweet_id=102, ancestor_users=[10, 1]),
        FilterCandidate(tweet_id=5, author_id=10, in_reply_to_tweet_id=103, ancestor_users=[10, 99]),
    ]
    res = flt.filter(candidates)
    assert [c.tweet_id for c in res.kept] == [1, 2, 3, 4]
    assert [c.tweet_id for c in res.removed] == [5]


def test_age_filter_and_cold_start_thompson() -> None:
    age_flt = AgeFilter(max_age_hours=48.0)
    res = age_flt.filter(
        [
            FilterCandidate(tweet_id=1, age_secs=3600.0),
            FilterCandidate(tweet_id=2, age_secs=49.0 * 3600.0),
        ]
    )
    assert [c.tweet_id for c in res.kept] == [1]
    assert [c.tweet_id for c in res.removed] == [2]

    cs = AuthorColdStart(
        ColdStartConfig(
            slot_min=0,
            slot_max=1,
            follower_cap=50_000,
            impression_threshold=200,
            max_post_age_secs=7200.0,
            max_position_ratio=1.0,
            use_thompson_sampling=True,
        )
    )
    cs_candidates = [
        FilterCandidate(
            tweet_id=100,
            author_id=1,
            age_secs=600.0,
            author_followers_count=120_000,
            view_count_on_home=5000,
            fav_count=200,
        ),
        FilterCandidate(
            tweet_id=101,
            author_id=2,
            age_secs=900.0,
            author_followers_count=8_500,
            view_count_on_home=45,
            fav_count=12,
        ),
    ]
    decision = cs.apply(cs_candidates, [40.0, 5.0], rng=np.random.default_rng(7))
    assert decision.lifted_index == 1
    assert decision.scores == [40.0, 40.0]


def test_user_cred_v2_from_mass() -> None:
    assert UserCredV2.from_mass(1, 0.0).score == 0.0
    assert UserCredV2.from_mass(2, -1.0).score == 0.0
    mid = UserCredV2.from_mass(3, 1e-8)
    expected = min(max(165.2 + 7.07 * math.log(1e-8), 0.0), 100.0)
    assert abs(mid.score - expected) < 1e-12
    assert UserCredV2.from_mass(4, 1.0).score == 100.0


def test_propensity_and_dpp_diversity() -> None:
    p_small = shrink_propensity(count=5, views=10, prior_rate=0.01, alpha_0=200.0)
    p_large = shrink_propensity(count=500, views=1000, prior_rate=0.01, alpha_0=200.0)
    assert p_small < 0.05
    assert p_large > 0.40

    metrics = ObservablePostMetrics(
        views=4000,
        likes=140,
        replies=28,
        reposts=19,
        quotes=11,
        bookmarks=55,
        text="Deep dive on latency budgets in multi-agent orchestration.\nHere is what broke at 10k req/s:",
        has_media=True,
        media_type="photo",
    )
    cand_mutual = build_candidate_scoring_inputs(
        metrics, author_id=42, in_network=True, is_mutual_follow_author=True
    )
    cand_oon = build_candidate_scoring_inputs(
        metrics, author_id=43, in_network=False, is_mutual_follow_author=False
    )
    weights = ValueModelWeights.vm_ranker_defaults()
    scores = compute_value_scores(weights, QueryScoringContext(0.75), [cand_mutual, cand_oon])
    assert scores.scores[0] > scores.scores[1]

    assert (
        recommend_engagement_action(
            is_mutual_follow_author=True,
            is_reply=False,
            is_retweet=False,
            breakout_score=85.0,
        )
        == "reply_mutual_20x"
    )
    assert (
        recommend_engagement_action(
            is_mutual_follow_author=False,
            is_reply=False,
            is_retweet=False,
            breakout_score=85.0,
        )
        == "quote_oon_safe"
    )

    emb_a = compute_text_embedding("Shipping Postgres 17 vector indexing benchmarks for AI ops")
    emb_dup = compute_text_embedding("Shipping Postgres 17 vector indexing benchmarks for AI ops!")
    emb_diff = compute_text_embedding("Small business cashflow forecasting and payroll tax tips")

    assert dpp_novelty_multiplier(emb_diff, [emb_a], theta=0.65) > 0.85
    assert dpp_novelty_multiplier(emb_dup, [emb_a], theta=0.65) < 0.35

    rescored = dpp_greedy_rescore(
        [
            DppCandidate(id="orig", score=10.0, embedding=emb_a),
            DppCandidate(id="dup", score=9.0, embedding=emb_dup),
            DppCandidate(id="novel", score=8.0, embedding=emb_diff),
        ],
        theta=0.65,
    )
    by_id = {r.id: r for r in rescored}
    assert by_id["orig"].selected is True
    assert by_id["novel"].selected is True
    assert by_id["dup"].selected is False
    assert by_id["dup"].rescored_score == 0.0


def test_slpa_overlapping_communities() -> None:
    g = nx.Graph()
    for u in (1, 2, 3, 4):
        for v in (1, 2, 3, 4):
            if u < v:
                g.add_edge(u, v, weight=3.0)
    for u in (5, 6, 7, 8):
        for v in (5, 6, 7, 8):
            if u < v:
                g.add_edge(u, v, weight=3.0)
    # Node 9 bridges both cliques
    for tgt in (1, 2, 5, 6):
        g.add_edge(9, tgt, weight=2.5)

    comms = slpa_overlapping_communities(g, iterations=25, threshold=0.2, seed=42)
    assert len(comms) == 9
    assert comms[1][0] != comms[5][0]
