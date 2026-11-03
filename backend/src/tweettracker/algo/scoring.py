"""Exact mathematical port of xai-value-model/src/scoring.rs and inputs.rs (9106e9f)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import cmp_to_key
import math

from tweettracker.algo.weights import NEGATIVE_SCORES_OFFSET, ValueModelWeights


@dataclass(slots=True)
class PhoenixScores:
    """28 Phoenix model prediction heads matching xai-value-model/phoenix_scores.rs."""

    favorite_score: float | None = None
    reply_score: float | None = None
    retweet_score: float | None = None
    photo_expand_score: float | None = None
    video_open_score: float | None = None
    click_score: float | None = None
    open_link_score: float | None = None
    profile_click_score: float | None = None
    vqv_score: float | None = None
    share_score: float | None = None
    share_via_dm_score: float | None = None
    share_via_copy_link_score: float | None = None
    dwell_score: float | None = None
    quote_score: float | None = None
    quoted_click_score: float | None = None
    quoted_vqv_score: float | None = None
    dwell_time: float | None = None
    click_dwell_time: float | None = None
    home_video_continuation_secs: float | None = None
    home_profile_visit_secs: float | None = None
    follow_author_score: float | None = None
    not_interested_score: float | None = None
    block_author_score: float | None = None
    mute_author_score: float | None = None
    report_score: float | None = None
    not_dwelled_score: float | None = None
    post_unexplored_score: float | None = None
    post_video_score: float | None = None


@dataclass(slots=True)
class CandidateScoringInputs:
    """Exact port of xai-value-model/src/inputs.rs CandidateScoringInputs."""

    phoenix_scores: PhoenixScores = field(default_factory=PhoenixScores)
    author_id: int = 0
    in_network: bool | None = None
    is_reply: bool = False
    is_retweet: bool = False
    is_mutual_follow_author: bool = False
    vqv_eligible: bool = False
    quoted_vqv_eligible: bool = False
    cold_start_lift_to_rank: int | None = None
    weighted_score: float | None = None
    author_exploration_bonus: float = 0.0
    user_video_continuation_secs: float | None = None

    def bidirectional_boost_eligible(self) -> bool:
        """Grants mutual reply/dwell boost strictly on original and quote posts (!reply && !retweet)."""
        return (not self.is_reply) and (not self.is_retweet) and self.is_mutual_follow_author


@dataclass(slots=True)
class QueryScoringContext:
    """Exact port of xai-value-model/src/inputs.rs QueryScoringContext."""

    effective_oon_weight: float = 1.0


@dataclass(slots=True)
class ValueScores:
    """Exact port of xai-value-model/src/scoring.rs ValueScores."""

    weighted: list[float] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)


@dataclass(slots=True)
class PostFusionMultiplier:
    """Exact port of xai-value-model/src/scoring.rs PostFusionMultiplier."""

    author_diversity: float
    oon: float

    def combined(self) -> float:
        return self.author_diversity * self.oon

    def apply(self, score: float) -> float:
        return score * self.author_diversity * self.oon


def _apply(score: float | None, weight: float) -> float:
    return (0.0 if score is None else score) * weight


def _product(probability: float | None, conditional_value: float | None) -> float | None:
    if probability is None or conditional_value is None:
        return None
    return probability * conditional_value


def fuse_heads(weights: ValueModelWeights, candidate: CandidateScoringInputs) -> float:
    return offset_score(compute_weighted_score(weights, candidate), weights)


def compute_weighted_score(
    weights: ValueModelWeights,
    candidate: CandidateScoringInputs,
) -> float:
    """Exact 29-head left-to-right IEEE-754 fold matching scoring.rs lines 69-140."""
    scores = candidate.phoenix_scores

    vqv_weight = weights.vqv if candidate.vqv_eligible else 0.0
    quoted_vqv_weight = weights.quoted_vqv if candidate.quoted_vqv_eligible else 0.0
    post_unexplored_weight = (
        weights.post_unexplored
        if (weights.post_unexplored_include_out_of_network or candidate.in_network is True)
        else 0.0
    )

    terms = (
        _apply(scores.favorite_score, weights.favorite),
        _apply(scores.reply_score, weights.reply_weight_for(candidate)),
        _apply(scores.retweet_score, weights.retweet),
        _apply(scores.photo_expand_score, weights.photo_expand),
        _apply(scores.video_open_score, weights.video_open),
        _apply(scores.click_score, weights.click),
        _apply(scores.open_link_score, weights.open_link),
        _apply(scores.profile_click_score, weights.profile_click),
        _apply(scores.vqv_score, vqv_weight),
        _apply(scores.share_score, weights.share),
        _apply(scores.share_via_dm_score, weights.share_via_dm),
        _apply(scores.share_via_copy_link_score, weights.share_via_copy_link),
        _apply(scores.dwell_score, weights.dwell_weight_for(candidate)),
        _apply(scores.quote_score, weights.quote),
        _apply(scores.quoted_click_score, weights.quoted_click),
        _apply(scores.quoted_vqv_score, quoted_vqv_weight),
        _apply(scores.dwell_time, weights.cont_dwell_time),
        _apply(scores.click_dwell_time, weights.cont_click_dwell_time),
        _apply(
            _product(scores.video_open_score, scores.home_video_continuation_secs),
            weights.video_continuation,
        ),
        _apply(
            _product(scores.video_open_score, candidate.user_video_continuation_secs),
            weights.user_video_continuation,
        ),
        _apply(
            _product(scores.profile_click_score, scores.home_profile_visit_secs),
            weights.profile_visit_secs,
        ),
        _apply(scores.follow_author_score, weights.follow_author),
        _apply(scores.not_interested_score, weights.not_interested),
        _apply(scores.block_author_score, weights.block_author),
        _apply(scores.mute_author_score, weights.mute_author),
        _apply(scores.report_score, weights.report),
        _apply(scores.not_dwelled_score, weights.not_dwelled),
        _apply(scores.post_unexplored_score, post_unexplored_weight),
        _apply(scores.post_video_score, weights.post_video),
    )

    total = 0.0
    for term in terms:
        total += term
    return total


def set_user_video_continuation(candidates: Sequence[CandidateScoringInputs]) -> None:
    """Exact port of scoring.rs lines 142-159."""
    open_weighted_secs = 0.0
    opens = 0.0
    for c in candidates:
        scores = c.phoenix_scores
        if (
            scores.video_open_score is not None
            and scores.home_video_continuation_secs is not None
        ):
            open_weighted_secs += scores.video_open_score * scores.home_video_continuation_secs
            opens += scores.video_open_score

    user_secs = (open_weighted_secs / opens) if opens > 0.0 else None
    for c in candidates:
        c.user_video_continuation_secs = user_secs


def offset_score(combined_score: float, w: ValueModelWeights) -> float:
    """Exact port of scoring.rs lines 161-170."""
    total_sum = w.total_sum()
    if total_sum == 0.0:
        return max(combined_score, 0.0)
    if combined_score < 0.0:
        return (combined_score + w.negative_sum()) / total_sum * NEGATIVE_SCORES_OFFSET
    return combined_score + NEGATIVE_SCORES_OFFSET


def unoffset_score(weighted_score: float, w: ValueModelWeights) -> float:
    """Exact port of scoring.rs lines 172-181."""
    total_sum = w.total_sum()
    if total_sum == 0.0:
        return weighted_score
    if weighted_score < NEGATIVE_SCORES_OFFSET:
        return weighted_score / NEGATIVE_SCORES_OFFSET * total_sum - w.negative_sum()
    return weighted_score - NEGATIVE_SCORES_OFFSET


def diversity_multiplier(decay_factor: float, floor: float, exponent: float) -> float:
    """Exact port of scoring.rs lines 183-185: (1.0 - floor) * decay_factor^exponent + floor."""
    return (1.0 - floor) * math.pow(decay_factor, float(exponent)) + floor


def _cmp_partial_desc(item_a: tuple[int, float], item_b: tuple[int, float]) -> int:
    a = item_a[1]
    b = item_b[1]
    if math.isnan(a) or math.isnan(b):
        return 0
    if b > a:
        return 1
    if b < a:
        return -1
    return 0


def author_pool_counts(author_ids: Sequence[int], ordering_scores: Sequence[float]) -> list[int]:
    """Exact port of scoring.rs lines 187-204 (stable sort descending by score)."""
    indexed = list(enumerate(ordering_scores))
    indexed.sort(key=cmp_to_key(_cmp_partial_desc))

    counts = [0] * len(author_ids)
    author_counts: dict[int, int] = {}
    for idx, _ in indexed:
        author_id = author_ids[idx]
        k = author_counts.get(author_id, 0)
        counts[idx] = k
        author_counts[author_id] = k + 1
    return counts


def oon_applies(weights: ValueModelWeights, candidate: CandidateScoringInputs) -> bool:
    """Exact port of scoring.rs lines 206-215."""
    if candidate.in_network is False:
        return True
    if candidate.in_network is True:
        return weights.oon_rescore_in_network_replies_retweets and (
            candidate.is_reply or candidate.is_retweet
        )
    return False


def post_fusion_multipliers(
    weights: ValueModelWeights,
    ctx: QueryScoringContext,
    candidates: Sequence[CandidateScoringInputs],
    ordering_scores: Sequence[float],
) -> list[PostFusionMultiplier]:
    """Exact port of scoring.rs lines 217-251."""
    if weights.enable_author_diversity:
        author_ids = [c.author_id for c in candidates]
        author_diversity = [
            diversity_multiplier(
                weights.author_diversity_decay,
                weights.author_diversity_floor,
                float(k),
            )
            for k in author_pool_counts(author_ids, ordering_scores)
        ]
    else:
        author_diversity = [1.0] * len(candidates)

    return [
        PostFusionMultiplier(
            author_diversity=div,
            oon=(ctx.effective_oon_weight if oon_applies(weights, c) else 1.0),
        )
        for c, div in zip(candidates, author_diversity, strict=True)
    ]


def apply_cold_start_decisions(
    scores: Sequence[float],
    candidates: Sequence[CandidateScoringInputs],
) -> list[float]:
    """Exact port of scoring.rs lines 263-280."""
    effective = list(scores)
    lift: tuple[int, int] | None = None
    for i, c in enumerate(candidates):
        if c.cold_start_lift_to_rank is not None:
            lift = (i, int(c.cold_start_lift_to_rank))
            break
    if lift is not None:
        index, rank = lift
        if 0 <= rank < len(scores):
            ranked = sorted(scores, reverse=True)
            effective[index] = max(effective[index], ranked[rank])
    return effective


def _add_exploration_bonus(weighted: float, bonus: float, weights: ValueModelWeights) -> float:
    if bonus == 0.0:
        return weighted
    return offset_score(unoffset_score(weighted, weights) + bonus, weights)


def compute_value_scores_with_adjustment(
    weights: ValueModelWeights,
    ctx: QueryScoringContext,
    candidates: Sequence[CandidateScoringInputs],
    adjust_base_scores: Callable[[Sequence[float]], list[float]],
) -> ValueScores:
    """Exact port of scoring.rs lines 282-328."""
    weighted = [
        (
            c.weighted_score
            if c.weighted_score is not None
            else offset_score(compute_weighted_score(weights, c), weights)
        )
        for c in candidates
    ]

    if weights.multiplier_pre_offset:
        multipliers = post_fusion_multipliers(weights, ctx, candidates, weighted)
        pre_offset_scaled: list[float] = []
        for w_val, c, m in zip(weighted, candidates, multipliers, strict=True):
            net = unoffset_score(w_val, weights) + c.author_exploration_bonus
            scaled = (m.combined() * net) if net >= 0.0 else net
            pre_offset_scaled.append(offset_score(scaled, weights))
        scores = adjust_base_scores(pre_offset_scaled)
        return ValueScores(weighted=weighted, scores=scores)

    base = [
        _add_exploration_bonus(w_val, c.author_exploration_bonus, weights)
        for w_val, c in zip(weighted, candidates, strict=True)
    ]
    adjusted = adjust_base_scores(base)
    multipliers = post_fusion_multipliers(weights, ctx, candidates, adjusted)
    scores = [m.apply(s) for s, m in zip(adjusted, multipliers, strict=True)]
    return ValueScores(weighted=weighted, scores=scores)


def compute_value_scores(
    weights: ValueModelWeights,
    ctx: QueryScoringContext,
    candidates: Sequence[CandidateScoringInputs],
) -> ValueScores:
    """Exact port of scoring.rs lines 253-261."""
    return compute_value_scores_with_adjustment(
        weights,
        ctx,
        candidates,
        lambda s: apply_cold_start_decisions(s, candidates),
    )
