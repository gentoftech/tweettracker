"""Two-Stage x-algorithm (9106e9f) Breakout Tracer & 29-Head ValueModel Inspector."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.algo import (
    DEFAULT_DPP_THETA,
    NEGATIVE_SCORES_OFFSET,
    UPSTREAM_COMMIT_SHA,
    AgeFilter,
    CandidateScoringInputs,
    DppCandidate,
    FilterCandidate,
    FusionCalibrationParams,
    OONRetweetReplyFilter,
    ObservablePostMetrics,
    PipelineParams,
    QueryScoringContext,
    SelfReplyChainFilter,
    SurrogateCalibrationParams,
    UserCredV2,
    ValueModelWeights,
    build_candidate_scoring_inputs,
    compute_text_embedding,
    compute_value_scores,
    compute_weighted_score,
    dpp_greedy_rescore,
    dpp_novelty_multiplier,
    is_cold_start_eligible,
    offset_score,
    oon_applies,
    post_fusion_multipliers,
    recommend_engagement_action,
    set_user_video_continuation,
)
from tweettracker.models import (
    AlgoParamVersion,
    Community,
    Post,
    PostInteraction,
    PostSnapshot,
    TrackedAccount,
    Workspace,
)
from tweettracker.radar.seismic import SeismicEstimate, estimate_seismic
from tweettracker.radar.velocity import VelocityMetrics, compute_velocity_metrics


def _stable_int_id(raw_id: str | int | None) -> int:
    if raw_id is None:
        return 0
    if isinstance(raw_id, int):
        return raw_id
    cleaned = str(raw_id).strip()
    if not cleaned:
        return 0
    if cleaned.isdigit():
        return int(cleaned)
    digest = hashlib.blake2b(cleaned.encode("utf-8"), digest_size=6).digest()
    return int.from_bytes(digest, "little")


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(slots=True)
class AlgoBundle:
    """Resolved active x-algorithm parameter bundle for a workspace."""

    param_version_id: int | None
    version_label: str
    upstream_commit_sha: str
    weights: ValueModelWeights
    surrogate_params: SurrogateCalibrationParams
    pipeline_params: PipelineParams
    fusion_params: FusionCalibrationParams = field(default_factory=FusionCalibrationParams)


@dataclass(slots=True, frozen=True)
class Stage1FilterReport:
    """Pre-scoring home-mixer filter & AuthorColdStart evaluation report."""

    passed_oon_retweet_reply: bool
    passed_self_reply_chain: bool
    passed_age_48h: bool
    cold_start_eligible: bool
    stage1_visible: bool
    drop_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed_oon_retweet_reply": self.passed_oon_retweet_reply,
            "passed_self_reply_chain": self.passed_self_reply_chain,
            "passed_age_48h": self.passed_age_48h,
            "cold_start_eligible": self.cold_start_eligible,
            "stage1_visible": self.stage1_visible,
            "drop_reason": self.drop_reason,
        }


@dataclass(slots=True, frozen=True)
class HeadContribution:
    """Single head contribution inside the 29-head xai-value-model fold."""

    head: str
    phoenix_score: float
    base_weight: float
    effective_weight: float
    weighted_contribution: float
    is_boosted: bool
    polarity: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "phoenix_score": round(self.phoenix_score, 6),
            "base_weight": round(self.base_weight, 4),
            "effective_weight": round(self.effective_weight, 4),
            "weighted_contribution": round(self.weighted_contribution, 6),
            "is_boosted": self.is_boosted,
            "polarity": self.polarity,
        }


@dataclass(slots=True)
class ValueModelBreakdown:
    """Full 29-head PhoenixScores + ValueModelWeights fusion breakdown."""

    raw_weighted_sum: float
    offset_weighted_score: float
    oon_multiplier: float
    author_diversity_multiplier: float
    combined_post_fusion_multiplier: float
    final_value_score: float
    bidirectional_boost_active: bool
    in_network: bool
    heads: list[HeadContribution] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        sorted_heads = sorted(
            self.heads,
            key=lambda h: abs(h.weighted_contribution),
            reverse=True,
        )
        return {
            "raw_weighted_sum": round(self.raw_weighted_sum, 6),
            "offset_weighted_score": round(self.offset_weighted_score, 6),
            "negative_scores_offset": NEGATIVE_SCORES_OFFSET,
            "oon_multiplier": round(self.oon_multiplier, 4),
            "author_diversity_multiplier": round(self.author_diversity_multiplier, 4),
            "combined_post_fusion_multiplier": round(self.combined_post_fusion_multiplier, 4),
            "final_value_score": round(self.final_value_score, 6),
            "bidirectional_boost_active": self.bidirectional_boost_active,
            "in_network": self.in_network,
            "heads": [h.to_dict() for h in self.heads],
            "top_positive_heads": [
                h.to_dict() for h in sorted_heads if h.weighted_contribution > 0
            ][:10],
            "negative_heads": [
                h.to_dict() for h in self.heads if h.polarity == "negative"
            ],
        }


@dataclass(slots=True)
class ScoredPostResult:
    """Complete Stage 1 + Stage 2 breakout evaluation for a post."""

    post_id: int | None
    x_post_id: str
    age_minutes: int
    stage1_filters: Stage1FilterReport
    velocity: VelocityMetrics
    seismic: SeismicEstimate
    catalysis_boost: float
    value_model_score: float
    dpp_novelty_multiplier: float
    dpp_selected: bool
    dpp_rank: int | None
    breakout_score: float
    breakout_probability: float
    alert_tier: str
    recommended_action: str
    tactical_rationale: str
    effective_watch_threshold: float
    effective_surge_threshold: float
    embedding: list[float]
    value_breakdown: ValueModelBreakdown


async def load_active_algo_bundle(
    session: AsyncSession,
    workspace_id: int | None = None,
    *,
    ensure_db_row: bool = False,
) -> AlgoBundle:
    """Loads the workspace's active `AlgoParamVersion`, falling back to global active
    `AlgoParamVersion`, or `ValueModelWeights.vm_ranker_defaults()` (`9106e9f`).
    """
    active_row: AlgoParamVersion | None = None
    if workspace_id is not None:
        active_row = (
            await session.execute(
                select(AlgoParamVersion)
                .where(
                    AlgoParamVersion.workspace_id == workspace_id,
                    AlgoParamVersion.is_active.is_(True),
                )
                .order_by(AlgoParamVersion.version_number.desc(), AlgoParamVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if active_row is None:
        active_row = (
            await session.execute(
                select(AlgoParamVersion)
                .where(
                    AlgoParamVersion.workspace_id.is_(None),
                    AlgoParamVersion.is_active.is_(True),
                )
                .order_by(AlgoParamVersion.version_number.desc(), AlgoParamVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if active_row is None and ensure_db_row:
        vm_def = ValueModelWeights.vm_ranker_defaults()
        sur_def = SurrogateCalibrationParams()
        pipe_def = PipelineParams()
        fus_def = FusionCalibrationParams()
        active_row = AlgoParamVersion(
            workspace_id=None,
            version_number=1,
            label="xai-org/x-algorithm @ 9106e9f (vm-ranker baseline)",
            source="upstream_xalgo",
            upstream_commit_sha=UPSTREAM_COMMIT_SHA,
            weights_json=vm_def.to_dict(),
            surrogate_params_json=sur_def.to_dict(),
            thresholds_json={
                "watch_threshold": 65.0,
                "surge_threshold": 82.0,
                "pipeline": pipe_def.to_dict(),
                "fusion": fus_def.to_dict(),
            },
            holdout_metrics_json={},
            changelog_note="Auto-initialized 9106e9f baseline.",
            is_active=True,
        )
        session.add(active_row)
        await session.flush()

    if active_row is not None:
        weights = ValueModelWeights.vm_ranker_defaults()
        if active_row.weights_json:
            merged_w = weights.to_dict()
            merged_w.update(active_row.weights_json)
            weights = ValueModelWeights.from_dict(merged_w)

        surrogate = SurrogateCalibrationParams()
        if active_row.surrogate_params_json:
            merged_s = surrogate.to_dict()
            merged_s.update(active_row.surrogate_params_json)
            surrogate = SurrogateCalibrationParams.from_dict(merged_s)

        thresholds_map = (
            active_row.thresholds_json
            if isinstance(active_row.thresholds_json, Mapping)
            else {}
        )
        raw_pipe = thresholds_map.get("pipeline")
        if isinstance(raw_pipe, Mapping):
            merged_p = PipelineParams().to_dict()
            merged_p.update(raw_pipe)
            pipeline = PipelineParams.from_dict(merged_p)
        else:
            pipeline = PipelineParams()

        raw_fusion = thresholds_map.get("fusion")
        fusion = (
            FusionCalibrationParams.from_dict(raw_fusion)
            if isinstance(raw_fusion, Mapping)
            else FusionCalibrationParams()
        )

        return AlgoBundle(
            param_version_id=active_row.id,
            version_label=active_row.label,
            upstream_commit_sha=active_row.upstream_commit_sha or UPSTREAM_COMMIT_SHA,
            weights=weights,
            surrogate_params=surrogate,
            pipeline_params=pipeline,
            fusion_params=fusion,
        )

    return AlgoBundle(
        param_version_id=None,
        version_label="xai-org/x-algorithm @ 9106e9f (vm-ranker defaults)",
        upstream_commit_sha=UPSTREAM_COMMIT_SHA,
        weights=ValueModelWeights.vm_ranker_defaults(),
        surrogate_params=SurrogateCalibrationParams(),
        pipeline_params=PipelineParams(),
        fusion_params=FusionCalibrationParams(),
    )


def resolve_effective_thresholds(
    workspace: Workspace,
    author_communities: Sequence[Community] = (),
) -> tuple[float, float]:
    """Resolves `(effective_watch_threshold, effective_surge_threshold)` honoring
    per-community overrides when configured.
    """
    watch_overrides = [
        float(c.watch_threshold_override)
        for c in author_communities
        if c.watch_threshold_override is not None
    ]
    base_watch = float(workspace.watch_threshold if workspace.watch_threshold is not None else 65.0)
    eff_watch = min(watch_overrides) if watch_overrides else base_watch

    surge_overrides = [
        float(c.surge_threshold_override)
        for c in author_communities
        if c.surge_threshold_override is not None
    ]
    base_surge = float(workspace.surge_threshold if workspace.surge_threshold is not None else 82.0)
    raw_surge = min(surge_overrides) if surge_overrides else base_surge
    eff_surge = max(eff_watch + 1.0, raw_surge)
    return round(eff_watch, 2), round(eff_surge, 2)


def evaluate_stage1_filters(
    post: Post,
    author: TrackedAccount,
    workspace: Workspace,
    *,
    age_minutes: float,
    followed_author_ids: Sequence[int] = (),
    pipeline_params: PipelineParams | None = None,
) -> Stage1FilterReport:
    """Runs `OONRetweetReplyFilter`, `SelfReplyChainFilter`, `AgeFilter` (48h ceiling),
    and `is_cold_start_eligible` (`<50k` followers, `<=2h`, `<200` views).
    """
    pipe = pipeline_params or PipelineParams()
    has_my_handle = bool(workspace.my_x_handle and workspace.my_x_handle.strip())
    in_network = bool(author.followed_by_me) if has_my_handle else False

    author_int_id = author.id or _stable_int_id(author.x_user_id)
    tweet_int_id = post.id or _stable_int_id(post.x_post_id)

    in_reply_to_id = (
        _stable_int_id(post.in_reply_to_post_id)
        if post.is_reply
        else None
    )
    if post.is_reply and in_reply_to_id == 0:
        in_reply_to_id = 1

    retweet_id = 1 if post.is_retweet else None
    conv_id = _stable_int_id(post.conversation_id) if post.conversation_id else in_reply_to_id
    ancestor_users = [_stable_int_id(u) for u in (post.ancestor_author_ids_json or []) if u]
    ancestors = [in_reply_to_id] if (post.is_reply and post.in_reply_to_post_id) else (
        [1] if (post.is_reply and ancestor_users) else []
    )

    cand = FilterCandidate(
        tweet_id=tweet_int_id,
        author_id=author_int_id,
        in_network=in_network,
        in_reply_to_tweet_id=in_reply_to_id,
        retweeted_tweet_id=retweet_id,
        conversation_id=conv_id,
        ancestors=ancestors,
        ancestor_users=ancestor_users,
        age_secs=max(float(age_minutes), 0.0) * 60.0,
        author_followers_count=author.followers_count,
        view_count_on_home=post.latest_views,
        fav_count=post.latest_likes,
        is_phoenix_home_retrieval=not in_network,
    )

    oon_filter = OONRetweetReplyFilter(
        enable_phoenix_oon_replies=pipe.enable_phoenix_oon_replies,
        phoenix_oon_replies_root_only=pipe.phoenix_oon_replies_root_only,
    )
    oon_passed = len(oon_filter.filter([cand]).kept) == 1

    viewer_id = _stable_int_id(workspace.my_x_handle) if has_my_handle else 0
    allowed_ids = list(followed_author_ids)
    if in_network:
        allowed_ids.append(author_int_id)
    self_chain_filter = SelfReplyChainFilter(viewer_id=viewer_id, followed_user_ids=allowed_ids)
    chain_passed = len(self_chain_filter.filter([cand]).kept) == 1

    age_filter = AgeFilter(max_age_hours=float(pipe.simclusters_max_candidate_age_hours))
    age_passed = len(age_filter.filter([cand]).kept) == 1

    cold_start = is_cold_start_eligible(
        cand,
        follower_cap=pipe.cold_start_follower_cap,
        max_post_age_secs=float(pipe.cold_start_max_post_age_secs),
        impression_threshold=pipe.cold_start_impression_threshold,
    )

    drop_reason: str | None = None
    if not oon_passed:
        drop_reason = "OONRetweetReplyFilter"
    elif not chain_passed:
        drop_reason = "SelfReplyChainFilter"
    elif not age_passed:
        drop_reason = "AgeFilter_48h"

    stage1_visible = oon_passed and chain_passed and age_passed
    return Stage1FilterReport(
        passed_oon_retweet_reply=oon_passed,
        passed_self_reply_chain=chain_passed,
        passed_age_48h=age_passed,
        cold_start_eligible=cold_start,
        stage1_visible=stage1_visible,
        drop_reason=drop_reason,
    )


def compute_catalysis_boost(
    post: Post,
    interactions: Sequence[PostInteraction],
    actor_accounts_by_id: Mapping[int, TrackedAccount] | None = None,
) -> float:
    """Computes Heavy-Hitter Catalysis Boost from early co-signs (`quote`, `reply`, `repost`)
    weighted by actor `heavy_hitter_score` & `user_cred_v2` and decayed past 60 minutes.
    """
    if not interactions:
        return 0.0

    type_weights = {
        "quote": 0.48,
        "reply": 0.34,
        "repost": 0.26,
        "follow": 0.20,
        "like": 0.08,
    }
    post_created = _ensure_utc(post.created_at_x)
    actor_lookup = actor_accounts_by_id or {}

    seen_actors: set[str] = set()
    raw_boost = 0.0

    for inter in interactions:
        actor_key = f"{inter.actor_x_user_id or inter.actor_handle}:{inter.interaction_type}"
        if actor_key in seen_actors:
            continue
        seen_actors.add(actor_key)

        i_weight = type_weights.get(inter.interaction_type.lower().strip(), 0.20)
        occurred = _ensure_utc(inter.occurred_at)
        delta_mins = max(0.0, (occurred - post_created).total_seconds() / 60.0)
        time_decay = 1.0 if delta_mins <= 60.0 else math.exp(-(delta_mins - 60.0) / 60.0)

        actor_acct = (
            actor_lookup.get(inter.actor_account_id)
            if inter.actor_account_id is not None
            else None
        )
        if actor_acct is not None:
            hh = max(0.0, min(100.0, float(actor_acct.heavy_hitter_score or 0.0)))
            uc = max(0.0, min(100.0, float(actor_acct.user_cred_v2 or 0.0)))
            cat_lift_bonus = min(
                0.25, max(0.0, (float(actor_acct.catalysis_lift or 0.0) - 1.0) * 0.12)
            )
        else:
            mass = max(1e-11, float(max(inter.actor_followers or 0, 100)) / 2_500_000.0)
            uc = UserCredV2.from_mass(0, mass).score
            hh = min(90.0, 20.0 + 0.65 * uc)
            cat_lift_bonus = 0.0

        authority = 0.65 * (hh / 100.0) + 0.35 * (uc / 100.0) + cat_lift_bonus
        raw_boost += i_weight * authority * time_decay

    # Smooth saturation in [0.0, 1.35]
    saturated = 1.35 * (1.0 - math.exp(-1.15 * raw_boost))
    return round(saturated, 4)


def compute_value_head_breakdown(
    weights: ValueModelWeights,
    ctx: QueryScoringContext,
    candidate: CandidateScoringInputs,
    *,
    author_diversity_mult: float = 1.0,
    final_value_score: float | None = None,
) -> ValueModelBreakdown:
    """Computes exact 29-head contributions matching `xai-value-model/scoring.rs`."""
    s = candidate.phoenix_scores
    boost_eligible = candidate.bidirectional_boost_eligible()

    vqv_w = weights.vqv if candidate.vqv_eligible else 0.0
    quoted_vqv_w = weights.quoted_vqv if candidate.quoted_vqv_eligible else 0.0
    unexplored_w = (
        weights.post_unexplored
        if (weights.post_unexplored_include_out_of_network or candidate.in_network is True)
        else 0.0
    )
    reply_eff_w = weights.reply_weight_for(candidate)
    dwell_eff_w = weights.dwell_weight_for(candidate)

    vid_cont_val = (
        (s.video_open_score * s.home_video_continuation_secs)
        if (s.video_open_score is not None and s.home_video_continuation_secs is not None)
        else 0.0
    )
    user_vid_cont_val = (
        (s.video_open_score * candidate.user_video_continuation_secs)
        if (s.video_open_score is not None and candidate.user_video_continuation_secs is not None)
        else 0.0
    )
    prof_visit_val = (
        (s.profile_click_score * s.home_profile_visit_secs)
        if (s.profile_click_score is not None and s.home_profile_visit_secs is not None)
        else 0.0
    )

    specs: list[tuple[str, float, float, float, bool, str]] = [
        ("favorite", s.favorite_score or 0.0, weights.favorite, weights.favorite, False, "positive"),
        (
            "reply",
            s.reply_score or 0.0,
            weights.reply,
            reply_eff_w,
            bool(boost_eligible and weights.bidirectional_follow_reply_weight_boost != 0.0),
            "positive",
        ),
        ("retweet", s.retweet_score or 0.0, weights.retweet, weights.retweet, False, "positive"),
        ("photo_expand", s.photo_expand_score or 0.0, weights.photo_expand, weights.photo_expand, False, "positive"),
        ("video_open", s.video_open_score or 0.0, weights.video_open, weights.video_open, False, "positive"),
        ("click", s.click_score or 0.0, weights.click, weights.click, False, "positive"),
        ("open_link", s.open_link_score or 0.0, weights.open_link, weights.open_link, False, "positive"),
        ("profile_click", s.profile_click_score or 0.0, weights.profile_click, weights.profile_click, False, "positive"),
        ("vqv", s.vqv_score or 0.0, weights.vqv, vqv_w, False, "positive"),
        ("share", s.share_score or 0.0, weights.share, weights.share, False, "positive"),
        ("share_via_dm", s.share_via_dm_score or 0.0, weights.share_via_dm, weights.share_via_dm, False, "positive"),
        (
            "share_via_copy_link",
            s.share_via_copy_link_score or 0.0,
            weights.share_via_copy_link,
            weights.share_via_copy_link,
            False,
            "positive",
        ),
        (
            "dwell",
            s.dwell_score or 0.0,
            weights.dwell,
            dwell_eff_w,
            bool(boost_eligible and weights.bidirectional_follow_dwell_weight_boost != 0.0),
            "positive",
        ),
        ("quote", s.quote_score or 0.0, weights.quote, weights.quote, False, "positive"),
        ("quoted_click", s.quoted_click_score or 0.0, weights.quoted_click, weights.quoted_click, False, "positive"),
        ("quoted_vqv", s.quoted_vqv_score or 0.0, weights.quoted_vqv, quoted_vqv_w, False, "positive"),
        ("dwell_time", s.dwell_time or 0.0, weights.cont_dwell_time, weights.cont_dwell_time, False, "positive"),
        (
            "click_dwell_time",
            s.click_dwell_time or 0.0,
            weights.cont_click_dwell_time,
            weights.cont_click_dwell_time,
            False,
            "positive",
        ),
        ("video_continuation", vid_cont_val, weights.video_continuation, weights.video_continuation, False, "positive"),
        (
            "user_video_continuation",
            user_vid_cont_val,
            weights.user_video_continuation,
            weights.user_video_continuation,
            False,
            "positive",
        ),
        ("profile_visit_secs", prof_visit_val, weights.profile_visit_secs, weights.profile_visit_secs, False, "positive"),
        ("follow_author", s.follow_author_score or 0.0, weights.follow_author, weights.follow_author, False, "positive"),
        ("not_interested", s.not_interested_score or 0.0, weights.not_interested, weights.not_interested, False, "negative"),
        ("block_author", s.block_author_score or 0.0, weights.block_author, weights.block_author, False, "negative"),
        ("mute_author", s.mute_author_score or 0.0, weights.mute_author, weights.mute_author, False, "negative"),
        ("report", s.report_score or 0.0, weights.report, weights.report, False, "negative"),
        ("not_dwelled", s.not_dwelled_score or 0.0, weights.not_dwelled, weights.not_dwelled, False, "negative"),
        ("post_unexplored", s.post_unexplored_score or 0.0, weights.post_unexplored, unexplored_w, False, "positive"),
        ("post_video", s.post_video_score or 0.0, weights.post_video, weights.post_video, False, "positive"),
    ]

    heads: list[HeadContribution] = [
        HeadContribution(
            head=name,
            phoenix_score=float(p_val),
            base_weight=float(base_w),
            effective_weight=float(eff_w),
            weighted_contribution=float(p_val) * float(eff_w),
            is_boosted=boosted,
            polarity=pol,
        )
        for name, p_val, base_w, eff_w, boosted, pol in specs
    ]

    raw_sum = compute_weighted_score(weights, candidate)
    off_score = offset_score(raw_sum, weights)
    oon_mult = ctx.effective_oon_weight if oon_applies(weights, candidate) else 1.0
    combined_mult = author_diversity_mult * oon_mult
    resolved_final = (
        final_value_score
        if final_value_score is not None
        else off_score * combined_mult
    )

    return ValueModelBreakdown(
        raw_weighted_sum=raw_sum,
        offset_weighted_score=off_score,
        oon_multiplier=oon_mult,
        author_diversity_multiplier=author_diversity_mult,
        combined_post_fusion_multiplier=combined_mult,
        final_value_score=resolved_final,
        bidirectional_boost_active=boost_eligible,
        in_network=bool(candidate.in_network),
        heads=heads,
    )


def _calibrate_breakout_score(
    *,
    velocity_z: float,
    seismic_p: float,
    value_model_score: float,
    catalysis_boost: float,
    dpp_novelty: float,
    cold_start_eligible: bool,
    stage1_dropped_hard: bool,
    fusion_params: FusionCalibrationParams | None = None,
) -> float:
    """Combines Stage 1 retrieval velocity/SEISMIC/catalysis and Stage 2 xai-value-model
    + DPP crowding discount into a calibrated `[0, 100]` breakout score.
    Matches `backtest.replay.vectorized_score_trajectories` to IEEE-754 float64 precision.
    """
    if stage1_dropped_hard:
        return 0.0

    fus = fusion_params or FusionCalibrationParams()
    cold_start_bonus = float(fus.cold_start_bonus) if cold_start_eligible else 0.0
    vm_clipped = min(max(float(value_model_score), 0.0), max(float(fus.vm_cap), 0.0))
    base_signal = (
        float(fus.intercept)
        + float(fus.vel_weight) * float(velocity_z)
        + float(fus.seismic_weight) * float(seismic_p)
        + float(fus.vm_weight) * vm_clipped
        + float(fus.catalysis_weight) * float(catalysis_boost)
        + cold_start_bonus
    )
    dpp_blend = min(max(float(fus.dpp_blend), 0.0), 1.0)
    dpp_clamped = min(max(float(dpp_novelty), 0.0), 1.0)
    crowded_scaled = base_signal * ((1.0 - dpp_blend) + dpp_blend * dpp_clamped)

    if crowded_scaled <= 0.0:
        return 0.0
    if crowded_scaled <= 85.0:
        return float(crowded_scaled)
    # Smooth hyperbolic compression in [85.0, 99.5] preserving strict monotonicity
    compressed = 85.0 + 14.5 * math.tanh((crowded_scaled - 85.0) / 26.0)
    return float(min(99.5, compressed))


def _calibrate_breakout_probability(
    breakout_score: float,
    *,
    stage1_dropped_hard: bool,
    fusion_params: FusionCalibrationParams | None = None,
) -> float:
    if stage1_dropped_hard:
        return 0.0
    fus = fusion_params or FusionCalibrationParams()
    prob_scale = max(float(fus.prob_scale), 0.5)
    logit = min(max(-(float(breakout_score) - float(fus.prob_midpoint)) / prob_scale, -40.0), 40.0)
    return float(1.0 / (1.0 + math.exp(logit)))


def build_tactical_rationale(
    *,
    recommended_action: str,
    my_x_handle: str | None,
    is_reply: bool,
    is_retweet: bool,
    breakout_score: float,
    watch_threshold: float,
) -> str:
    clean_handle = (my_x_handle or "").strip().lstrip("@")
    if recommended_action == "reply_mutual_20x":
        return (
            f"Mutual follow with @{clean_handle} on original/quote post unlocks X's "
            f"+15.0 BidirectionalFollowReplyWeightBoost (20.0x total reply weight vs 5.0 baseline)."
        )
    if recommended_action == "quote_oon_safe":
        if not clean_handle:
            return (
                "Ecosystem Mode (no my_x_handle configured): Quote-Post creates an original "
                "root candidate, bypassing OONRetweetReplyFilter and the 0.75x OON reply rescore."
            )
        if is_reply or is_retweet:
            return (
                "Target is a reply/repost (+15.0 mutual boost requires !is_reply && !is_retweet): "
                "Quote-Post avoids the 0.75x reply/repost rescore."
            )
        return (
            f"Non-mutual / OON author relative to @{clean_handle}: Quote-Post bypasses "
            f"OONRetweetReplyFilter and the 0.75x OON reply rescore."
        )
    return (
        f"Monitoring early trajectory ({breakout_score:.1f} < {watch_threshold:.1f} watch threshold) "
        f"before committing high-leverage engagement."
    )


@dataclass(slots=True)
class RadarCandidateBundle:
    """Input bundle for batch Stage 1 + Stage 2 radar scoring."""

    post: Post
    author: TrackedAccount
    communities: Sequence[Community] = ()
    snapshots: Sequence[PostSnapshot] = ()
    interactions: Sequence[PostInteraction] = ()


def score_radar_batch(
    workspace: Workspace,
    items: Sequence[RadarCandidateBundle],
    *,
    algo_bundle: AlgoBundle | None = None,
    actor_accounts_by_id: Mapping[int, TrackedAccount] | None = None,
    followed_author_ids: Sequence[int] = (),
    now: datetime | None = None,
    enforce_age_48h_drop: bool = True,
) -> list[ScoredPostResult]:
    """Scores a batch of radar candidates through Stage 1 (filters, velocity Z, SEISMIC,
    catalysis) and Stage 2 (`xai-value-model` + `vm-ranker` + `dpp_model.rs` `theta=0.65`).
    """
    if not items:
        return []

    now_utc = _ensure_utc(now) if now is not None else datetime.now(timezone.utc)
    bundle = algo_bundle or AlgoBundle(
        param_version_id=None,
        version_label="vm_ranker_defaults",
        upstream_commit_sha=UPSTREAM_COMMIT_SHA,
        weights=ValueModelWeights.vm_ranker_defaults(),
        surrogate_params=SurrogateCalibrationParams(),
        pipeline_params=PipelineParams(),
        fusion_params=FusionCalibrationParams(),
    )
    weights = bundle.weights
    surrogate = bundle.surrogate_params
    pipe = bundle.pipeline_params
    fus = bundle.fusion_params
    ctx = QueryScoringContext(effective_oon_weight=pipe.oon_weight_factor)
    has_my_handle = bool(workspace.my_x_handle and workspace.my_x_handle.strip())

    ages: list[int] = []
    stage1_reports: list[Stage1FilterReport] = []
    velocities: list[VelocityMetrics] = []
    seismics: list[SeismicEstimate] = []
    catalysis_list: list[float] = []
    embeddings: list[list[float]] = []
    cand_inputs: list[CandidateScoringInputs] = []
    is_mutual_flags: list[bool] = []

    for item in items:
        post = item.post
        author = item.author
        created_utc = _ensure_utc(post.created_at_x)
        age_mins = max(1, int(round((now_utc - created_utc).total_seconds() / 60.0)))
        ages.append(age_mins)

        s1 = evaluate_stage1_filters(
            post,
            author,
            workspace,
            age_minutes=float(age_mins),
            followed_author_ids=followed_author_ids,
            pipeline_params=pipe,
        )
        stage1_reports.append(s1)

        vel = compute_velocity_metrics(
            views=post.latest_views or 0,
            age_minutes=float(age_mins),
            baseline_curves=author.baseline_curves_json,
            followers_fallback=author.followers_count or 10_000,
        )
        velocities.append(vel)

        seis = estimate_seismic(
            age_minutes=float(age_mins),
            views=post.latest_views or 0,
            likes=post.latest_likes or 0,
            replies=post.latest_replies or 0,
            reposts=post.latest_reposts or 0,
            quotes=post.latest_quotes or 0,
            bookmarks=post.latest_bookmarks or 0,
            snapshots=item.snapshots,
        )
        seismics.append(seis)

        cat = compute_catalysis_boost(post, item.interactions, actor_accounts_by_id)
        catalysis_list.append(cat)

        emb = list(post.embedding_json) if post.embedding_json else compute_text_embedding(post.text or "")
        embeddings.append(emb)

        is_mutual = has_my_handle and bool(author.follows_me and author.followed_by_me)
        is_mutual_flags.append(is_mutual)
        in_network = bool(author.followed_by_me) if has_my_handle else False

        c_in = build_candidate_scoring_inputs(
            ObservablePostMetrics(
                views=int(post.latest_views or 0),
                likes=int(post.latest_likes or 0),
                replies=int(post.latest_replies or 0),
                reposts=int(post.latest_reposts or 0),
                quotes=int(post.latest_quotes or 0),
                bookmarks=int(post.latest_bookmarks or 0),
                text=post.text or "",
                has_url=bool(post.has_url),
                has_media=bool(post.has_media),
                media_type=post.media_type,
                video_duration_ms=post.video_duration_ms,
            ),
            author_id=author.id or _stable_int_id(author.x_user_id),
            in_network=in_network,
            is_reply=bool(post.is_reply),
            is_retweet=bool(post.is_retweet),
            is_mutual_follow_author=is_mutual,
            min_video_duration_ms=weights.min_video_duration_ms,
            params=surrogate,
        )
        cand_inputs.append(c_in)

    # Stage 2: Exact xai-value-model batch scoring (video continuation + author diversity + OON)
    set_user_video_continuation(cand_inputs)
    vm_out = compute_value_scores(weights, ctx, cand_inputs)
    fusion_mults = post_fusion_multipliers(weights, ctx, cand_inputs, vm_out.weighted)

    # Pre-DPP relevance ordering for Greedy MAP DPP semantic crowding (`theta = 0.65`)
    pre_dpp_scores: list[float] = []
    for idx in range(len(items)):
        s1 = stage1_reports[idx]
        dropped_hard = (not s1.passed_oon_retweet_reply) or (not s1.passed_self_reply_chain) or (
            enforce_age_48h_drop and not s1.passed_age_48h
        )
        pre_s = _calibrate_breakout_score(
            velocity_z=velocities[idx].velocity_z_score,
            seismic_p=seismics[idx].infectiousness,
            value_model_score=vm_out.scores[idx],
            catalysis_boost=catalysis_list[idx],
            dpp_novelty=1.0,
            cold_start_eligible=s1.cold_start_eligible,
            stage1_dropped_hard=dropped_hard,
            fusion_params=fus,
        )
        pre_dpp_scores.append(pre_s)

    if pipe.dpp_enabled and len(items) > 1:
        dpp_items = dpp_greedy_rescore(
            [
                DppCandidate(id=idx, score=max(pre_dpp_scores[idx], 0.0), embedding=embeddings[idx])
                for idx in range(len(items))
            ],
            theta=pipe.dpp_theta,
            max_selected_rank=pipe.dpp_max_selected_rank,
        )
    else:
        dpp_items = None

    results: list[ScoredPostResult] = []
    for idx, item in enumerate(items):
        post = item.post
        s1 = stage1_reports[idx]
        dropped_hard = (not s1.passed_oon_retweet_reply) or (not s1.passed_self_reply_chain) or (
            enforce_age_48h_drop and not s1.passed_age_48h
        )

        if dpp_items is not None:
            dpp_res = dpp_items[idx]
            dpp_mult = float(dpp_res.novelty_multiplier)
            dpp_sel = dpp_res.selected
            dpp_rnk = dpp_res.rank
        else:
            dpp_mult = 1.0
            dpp_sel = pre_dpp_scores[idx] > 0.0
            dpp_rnk = 1 if dpp_sel else None

        b_score = _calibrate_breakout_score(
            velocity_z=velocities[idx].velocity_z_score,
            seismic_p=seismics[idx].infectiousness,
            value_model_score=vm_out.scores[idx],
            catalysis_boost=catalysis_list[idx],
            dpp_novelty=dpp_mult,
            cold_start_eligible=s1.cold_start_eligible,
            stage1_dropped_hard=dropped_hard,
            fusion_params=fus,
        )

        eff_watch, eff_surge = resolve_effective_thresholds(workspace, item.communities)
        prob = _calibrate_breakout_probability(
            b_score,
            stage1_dropped_hard=dropped_hard,
            fusion_params=fus,
        )

        if b_score >= eff_surge:
            tier = "surge"
        elif b_score >= eff_watch:
            tier = "watch"
        else:
            tier = "none"

        rec_action = recommend_engagement_action(
            is_mutual_follow_author=is_mutual_flags[idx],
            is_reply=bool(post.is_reply),
            is_retweet=bool(post.is_retweet),
            breakout_score=b_score,
            watch_threshold=eff_watch,
        )
        rationale = build_tactical_rationale(
            recommended_action=rec_action,
            my_x_handle=workspace.my_x_handle,
            is_reply=bool(post.is_reply),
            is_retweet=bool(post.is_retweet),
            breakout_score=b_score,
            watch_threshold=eff_watch,
        )

        breakdown = compute_value_head_breakdown(
            weights,
            ctx,
            cand_inputs[idx],
            author_diversity_mult=fusion_mults[idx].author_diversity,
            final_value_score=vm_out.scores[idx],
        )

        results.append(
            ScoredPostResult(
                post_id=post.id,
                x_post_id=post.x_post_id,
                age_minutes=ages[idx],
                stage1_filters=s1,
                velocity=velocities[idx],
                seismic=seismics[idx],
                catalysis_boost=catalysis_list[idx],
                value_model_score=float(vm_out.scores[idx]),
                dpp_novelty_multiplier=dpp_mult,
                dpp_selected=dpp_sel,
                dpp_rank=dpp_rnk,
                breakout_score=b_score,
                breakout_probability=prob,
                alert_tier=tier,
                recommended_action=rec_action,
                tactical_rationale=rationale,
                effective_watch_threshold=eff_watch,
                effective_surge_threshold=eff_surge,
                embedding=embeddings[idx],
                value_breakdown=breakdown,
            )
        )

    return results


def score_single_post(
    workspace: Workspace,
    post: Post,
    author: TrackedAccount,
    *,
    communities: Sequence[Community] = (),
    snapshots: Sequence[PostSnapshot] = (),
    interactions: Sequence[PostInteraction] = (),
    reference_embeddings: Sequence[Sequence[float]] = (),
    algo_bundle: AlgoBundle | None = None,
    actor_accounts_by_id: Mapping[int, TrackedAccount] | None = None,
    followed_author_ids: Sequence[int] = (),
    now: datetime | None = None,
    enforce_age_48h_drop: bool = True,
) -> ScoredPostResult:
    """Scores a single post against optional `reference_embeddings` (`dpp_theta = 0.65`)."""
    res = score_radar_batch(
        workspace,
        [
            RadarCandidateBundle(
                post=post,
                author=author,
                communities=communities,
                snapshots=snapshots,
                interactions=interactions,
            )
        ],
        algo_bundle=algo_bundle,
        actor_accounts_by_id=actor_accounts_by_id,
        followed_author_ids=followed_author_ids,
        now=now,
        enforce_age_48h_drop=enforce_age_48h_drop,
    )[0]

    dpp_enabled = algo_bundle.pipeline_params.dpp_enabled if algo_bundle else True
    if reference_embeddings and dpp_enabled:
        theta = algo_bundle.pipeline_params.dpp_theta if algo_bundle else DEFAULT_DPP_THETA
        fus = algo_bundle.fusion_params if algo_bundle else FusionCalibrationParams()
        dpp_mult = float(dpp_novelty_multiplier(res.embedding, reference_embeddings, theta=theta))
        s1 = res.stage1_filters
        dropped_hard = (not s1.passed_oon_retweet_reply) or (not s1.passed_self_reply_chain) or (
            enforce_age_48h_drop and not s1.passed_age_48h
        )
        b_score = _calibrate_breakout_score(
            velocity_z=res.velocity.velocity_z_score,
            seismic_p=res.seismic.infectiousness,
            value_model_score=res.value_model_score,
            catalysis_boost=res.catalysis_boost,
            dpp_novelty=dpp_mult,
            cold_start_eligible=s1.cold_start_eligible,
            stage1_dropped_hard=dropped_hard,
            fusion_params=fus,
        )
        eff_watch = res.effective_watch_threshold
        eff_surge = res.effective_surge_threshold
        prob = _calibrate_breakout_probability(
            b_score,
            stage1_dropped_hard=dropped_hard,
            fusion_params=fus,
        )
        tier = "surge" if b_score >= eff_surge else ("watch" if b_score >= eff_watch else "none")
        has_my_handle = bool(workspace.my_x_handle and workspace.my_x_handle.strip())
        is_mutual = has_my_handle and bool(author.follows_me and author.followed_by_me)
        rec_action = recommend_engagement_action(
            is_mutual_follow_author=is_mutual,
            is_reply=bool(post.is_reply),
            is_retweet=bool(post.is_retweet),
            breakout_score=b_score,
            watch_threshold=eff_watch,
        )
        rationale = build_tactical_rationale(
            recommended_action=rec_action,
            my_x_handle=workspace.my_x_handle,
            is_reply=bool(post.is_reply),
            is_retweet=bool(post.is_retweet),
            breakout_score=b_score,
            watch_threshold=eff_watch,
        )
        res.dpp_novelty_multiplier = dpp_mult
        res.breakout_score = b_score
        res.breakout_probability = prob
        res.alert_tier = tier
        res.recommended_action = rec_action
        res.tactical_rationale = rationale

    return res
