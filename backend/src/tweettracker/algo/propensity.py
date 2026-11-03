"""Empirical-Bayes per-impression propensity shrinkage and latent PhoenixScores surrogates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import math
from typing import Any

from tweettracker.algo.scoring import CandidateScoringInputs, PhoenixScores


def shrink_propensity(
    count: int | float,
    views: int | float,
    prior_rate: float,
    alpha_0: float = 200.0,
) -> float:
    """Empirical-Bayes Beta-Binomial/Gamma-Poisson rate shrinkage:
    p_hat = (n_a + alpha_0 * p_bar_a) / (V + alpha_0), clamped to [0, 1].
    """
    v = max(float(views), 0.0)
    n = min(max(float(count), 0.0), v) if v > 0.0 else 0.0
    prior_strength = max(float(alpha_0), 1e-6)
    p_bar = min(max(float(prior_rate), 0.0), 1.0)
    shrunk = (n + prior_strength * p_bar) / (v + prior_strength)
    return min(max(shrunk, 0.0), 1.0)


def _validate_surrogate_float(
    key: str,
    val: Any,
    *,
    min_val: float = 0.0,
    max_val: float = 100_000.0,
    strict_positive: bool = False,
) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"Surrogate parameter '{key}' must be a finite number.")
    f_val = float(val)
    if not math.isfinite(f_val):
        raise ValueError(f"Surrogate parameter '{key}' must be finite (no NaN or Inf).")
    if strict_positive and f_val <= 0.0:
        raise ValueError(f"Surrogate parameter '{key}' ({f_val}) must be strictly > 0.")
    if f_val < min_val or f_val > max_val:
        raise ValueError(
            f"Surrogate parameter '{key}' ({f_val}) must be between {min_val} and {max_val}."
        )
    return f_val


@dataclass(slots=True)
class SurrogateCalibrationParams:
    """Calibrated observable-to-latent mapping parameters (tunable in Epic 3)."""

    prior_views_alpha0: float = 200.0
    prior_favorite_rate: float = 0.0120
    prior_reply_rate: float = 0.0012
    prior_retweet_rate: float = 0.0018
    prior_quote_rate: float = 0.0005
    prior_bookmark_rate: float = 0.0020

    copy_link_from_bookmark_mult: float = 0.38
    copy_link_from_quote_mult: float = 0.25
    dm_share_from_bookmark_mult: float = 0.30
    share_from_bookmark_mult: float = 0.55
    follow_from_bookmark_quote_mult: float = 0.14
    profile_click_from_engagement_mult: float = 0.45

    dwell_base_secs: float = 2.8
    dwell_per_100_chars_secs: float = 1.65
    dwell_max_secs: float = 24.0
    url_dwell_discount: float = 0.72
    open_link_prob_when_url: float = 0.065

    media_photo_expand_prob: float = 0.11
    media_video_open_prob: float = 0.18
    vqv_conditional_prob: float = 0.48
    not_dwelled_base_prob: float = 0.18

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SurrogateCalibrationParams:
        valid: dict[str, float] = {}
        for k, v in raw.items():
            if k not in cls.__dataclass_fields__ or v is None:
                continue
            if k == "dwell_max_secs":
                valid[k] = _validate_surrogate_float(
                    k, v, min_val=0.5, max_val=3_600.0, strict_positive=True
                )
            elif k == "prior_views_alpha0":
                valid[k] = _validate_surrogate_float(
                    k, v, min_val=1.0, max_val=100_000.0, strict_positive=True
                )
            elif k.startswith("prior_") or k.endswith("_prob"):
                valid[k] = _validate_surrogate_float(k, v, min_val=0.0, max_val=1.0)
            else:
                valid[k] = _validate_surrogate_float(k, v, min_val=0.0, max_val=1_000.0)
        return cls(**valid)


@dataclass(slots=True)
class ObservablePostMetrics:
    """Publicly observable X post engagement counts and structural metadata."""

    views: int
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    quotes: int = 0
    bookmarks: int = 0
    text: str = ""
    has_url: bool = False
    has_media: bool = False
    media_type: str | None = None
    video_duration_ms: int | None = None


def estimate_phoenix_scores(
    metrics: ObservablePostMetrics,
    params: SurrogateCalibrationParams | None = None,
) -> PhoenixScores:
    """Maps Empirical-Bayes shrunk propensities & post structure into 28 PhoenixScores heads."""
    cfg = params or SurrogateCalibrationParams()
    a0 = max(float(cfg.prior_views_alpha0), 1e-6)
    dwell_max_safe = max(float(cfg.dwell_max_secs), 1e-6)

    fav_p = shrink_propensity(metrics.likes, metrics.views, cfg.prior_favorite_rate, a0)
    reply_p = shrink_propensity(metrics.replies, metrics.views, cfg.prior_reply_rate, a0)
    rt_p = shrink_propensity(metrics.reposts, metrics.views, cfg.prior_retweet_rate, a0)
    quote_p = shrink_propensity(metrics.quotes, metrics.views, cfg.prior_quote_rate, a0)
    bmk_p = shrink_propensity(metrics.bookmarks, metrics.views, cfg.prior_bookmark_rate, a0)

    copy_link_p = min(
        1.0,
        cfg.copy_link_from_bookmark_mult * bmk_p + cfg.copy_link_from_quote_mult * quote_p,
    )
    dm_share_p = min(1.0, cfg.dm_share_from_bookmark_mult * bmk_p)
    share_p = min(1.0, cfg.share_from_bookmark_mult * bmk_p + 0.15 * quote_p)

    char_len = len(metrics.text.strip())
    line_breaks = metrics.text.count("\n")
    raw_dwell_secs = min(
        dwell_max_safe,
        cfg.dwell_base_secs
        + (char_len / 100.0) * cfg.dwell_per_100_chars_secs
        + min(line_breaks, 8) * 0.35
        + 120.0 * bmk_p,
    )
    if metrics.has_url:
        raw_dwell_secs *= cfg.url_dwell_discount

    dwell_prob = min(0.95, 0.12 + (raw_dwell_secs / dwell_max_safe) * 0.55 + 2.5 * bmk_p)
    click_prob = min(0.85, 0.04 + 0.35 * (1.0 if char_len > 240 else 0.2) + 4.0 * (bmk_p + reply_p))
    click_dwell_secs = raw_dwell_secs * click_prob * 1.4
    not_dwelled_prob = max(0.01, cfg.not_dwelled_base_prob * (1.0 - 0.7 * dwell_prob))

    is_photo = metrics.has_media and (metrics.media_type in ("photo", "image", None))
    is_video = metrics.has_media and (metrics.media_type in ("video", "animated_gif"))

    photo_expand_p = min(1.0, cfg.media_photo_expand_prob + 1.5 * fav_p) if is_photo else 0.0
    video_open_p = min(1.0, cfg.media_video_open_prob + 2.0 * bmk_p) if is_video else 0.0
    vqv_p = min(1.0, video_open_p * cfg.vqv_conditional_prob) if is_video else 0.0
    video_cont_secs = (
        min(45.0, float(metrics.video_duration_ms or 12_000) / 1000.0 * 0.42)
        if is_video
        else None
    )

    follow_p = min(1.0, cfg.follow_from_bookmark_quote_mult * (bmk_p + 2.0 * quote_p))
    profile_click_p = min(
        1.0, cfg.profile_click_from_engagement_mult * (reply_p + quote_p + bmk_p)
    )
    open_link_p = cfg.open_link_prob_when_url if metrics.has_url else 0.0
    unexplored_p = 1.0 / (1.0 + max(float(metrics.views), 0.0) / max(a0, 1.0))

    return PhoenixScores(
        favorite_score=fav_p,
        reply_score=reply_p,
        retweet_score=rt_p,
        photo_expand_score=photo_expand_p,
        video_open_score=video_open_p,
        click_score=click_prob,
        open_link_score=open_link_p,
        profile_click_score=profile_click_p,
        vqv_score=vqv_p,
        share_score=share_p,
        share_via_dm_score=dm_share_p,
        share_via_copy_link_score=copy_link_p,
        dwell_score=dwell_prob,
        quote_score=quote_p,
        quoted_click_score=(0.5 * click_prob if quote_p > 0.0 else 0.0),
        quoted_vqv_score=0.0,
        dwell_time=raw_dwell_secs,
        click_dwell_time=click_dwell_secs,
        home_video_continuation_secs=video_cont_secs,
        home_profile_visit_secs=4.5 if profile_click_p > 0.0 else None,
        follow_author_score=follow_p,
        not_interested_score=0.00015,
        block_author_score=0.00002,
        mute_author_score=0.00004,
        report_score=0.000005,
        not_dwelled_score=not_dwelled_prob,
        post_unexplored_score=unexplored_p,
        post_video_score=(1.0 if is_video else 0.0),
    )


def build_candidate_scoring_inputs(
    metrics: ObservablePostMetrics,
    author_id: int,
    *,
    in_network: bool | None = True,
    is_reply: bool = False,
    is_retweet: bool = False,
    is_mutual_follow_author: bool = False,
    min_video_duration_ms: int = 10_000,
    params: SurrogateCalibrationParams | None = None,
) -> CandidateScoringInputs:
    phoenix = estimate_phoenix_scores(metrics, params=params)
    vqv_eligible = bool(
        metrics.has_media
        and metrics.media_type == "video"
        and (metrics.video_duration_ms or 0) >= min_video_duration_ms
    )
    return CandidateScoringInputs(
        phoenix_scores=phoenix,
        author_id=author_id,
        in_network=in_network,
        is_reply=is_reply,
        is_retweet=is_retweet,
        is_mutual_follow_author=is_mutual_follow_author,
        vqv_eligible=vqv_eligible,
        quoted_vqv_eligible=False,
    )


def recommend_engagement_action(
    *,
    is_mutual_follow_author: bool,
    is_reply: bool,
    is_retweet: bool,
    breakout_score: float,
    watch_threshold: float = 65.0,
) -> str:
    """Returns tactical engagement guidance grounded in x-algorithm 9106e9f mechanics."""
    if breakout_score < watch_threshold:
        return "watch"
    if is_mutual_follow_author and (not is_reply) and (not is_retweet):
        return "reply_mutual_20x"
    return "quote_oon_safe"
