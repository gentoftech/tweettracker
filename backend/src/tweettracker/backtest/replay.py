"""Vectorized <2s Historical Backtester & Stage 1/2 xai-value-model Matrix Scorer (Story 3.3)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import math
import time
from typing import Any

import numpy as np
from scipy.stats import rankdata, spearmanr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.algo import (
    DEFAULT_DPP_THETA,
    NEGATIVE_SCORES_OFFSET,
    UPSTREAM_COMMIT_SHA,
    FilterCandidate,
    FusionCalibrationParams,
    PipelineParams,
    SelfReplyChainFilter,
    SurrogateCalibrationParams,
    ValueModelWeights,
)
from tweettracker.models import (
    AccountCommunityMembership,
    AlgoParamVersion,
    Community,
    Post,
    PostInteraction,
    PostSnapshot,
    TrackedAccount,
    Workspace,
    utc_now,
)
from tweettracker.radar.scorer import _stable_int_id, compute_catalysis_boost
from tweettracker.radar.seismic import estimate_seismic
from tweettracker.radar.velocity import compute_velocity_metrics


def ensure_utc(dt: datetime | None) -> datetime:
    """Normalizes naive SQLite or aware Postgres datetimes to UTC."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def extract_author_median_48h(author: TrackedAccount) -> int:
    """Extracts the author's baseline P50 48-hour (2880m) view benchmark."""
    curves = author.baseline_curves_json or {}
    p50 = curves.get("p50") if isinstance(curves, dict) else None
    if isinstance(p50, dict) and "2880" in p50:
        try:
            val = int(p50["2880"])
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return max(1_000, int(author.followers_count * 0.15))


def _is_active_membership(mem: AccountCommunityMembership) -> bool:
    return bool(
        mem.is_manual_tag
        or max(float(mem.nmf_interested_in or 0.0), float(mem.nmf_known_for or 0.0)) >= 0.25
    )


def _check_finite_float(key: str, val: Any, *, lo: float, hi: float) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"Parameter '{key}' must be a finite number.")
    f = float(val)
    if not math.isfinite(f) or f < lo or f > hi:
        raise ValueError(f"Parameter '{key}' ({f}) must be finite and within [{lo}, {hi}].")
    return f


@dataclass(slots=True)
class ResolvedAlgoConfig:
    """Complete resolved parameter bundle for scoring a workspace or candidate override."""

    version_id: int | None
    version_number: int
    label: str
    source: str
    upstream_commit_sha: str
    weights: ValueModelWeights
    surrogate: SurrogateCalibrationParams
    pipeline: PipelineParams
    fusion: FusionCalibrationParams
    watch_threshold: float = 65.0
    surge_threshold: float = 82.0
    breakout_multiple: float = 3.0
    community_floor_views: int = 5_000
    community_floors: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class TrajectoryMeta:
    """Human-readable post & author metadata aligned index-for-index with TrajectoryMatrix."""

    post_id: int
    x_post_id: str
    author_id: int
    author_handle: str
    author_display_name: str
    author_bridge_role: str
    communities: list[dict[str, Any]]
    text: str
    created_at_iso: str
    alert_age_minutes: int


@dataclass(slots=True)
class TrajectoryMatrix:
    """Contiguous float64 NumPy trajectory pack for <2s 10k-post vectorized replay."""

    views_at_eval: np.ndarray
    likes: np.ndarray
    replies: np.ndarray
    reposts: np.ndarray
    quotes: np.ndarray
    bookmarks: np.ndarray
    char_len: np.ndarray
    line_breaks: np.ndarray
    has_url: np.ndarray
    is_photo: np.ndarray
    is_video: np.ndarray
    video_dur_ms: np.ndarray
    is_mutual: np.ndarray
    in_network: np.ndarray
    is_reply: np.ndarray
    is_retweet: np.ndarray
    vel_z: np.ndarray
    seismic: np.ndarray
    cat_boost: np.ndarray
    dpp_mult: np.ndarray
    actual_48h_views: np.ndarray
    is_true_breakout_48h: np.ndarray
    post_ids: np.ndarray
    author_ids: np.ndarray
    created_ts: np.ndarray
    is_video_mp4: np.ndarray
    author_median_48h_views: np.ndarray
    community_floor_views: np.ndarray
    eval_age_mins: np.ndarray | None = None
    author_followers: np.ndarray | None = None
    reply_has_ancestors: np.ndarray | None = None
    replies_to_root: np.ndarray | None = None
    self_reply_passed: np.ndarray | None = None

    @property
    def size(self) -> int:
        return int(self.views_at_eval.shape[0])

    def slice_indices(self, idx: np.ndarray | slice) -> TrajectoryMatrix:
        """Returns a contiguous sub-matrix for train/holdout or community slicing."""
        return TrajectoryMatrix(
            views_at_eval=np.ascontiguousarray(self.views_at_eval[idx], dtype=np.float64),
            likes=np.ascontiguousarray(self.likes[idx], dtype=np.float64),
            replies=np.ascontiguousarray(self.replies[idx], dtype=np.float64),
            reposts=np.ascontiguousarray(self.reposts[idx], dtype=np.float64),
            quotes=np.ascontiguousarray(self.quotes[idx], dtype=np.float64),
            bookmarks=np.ascontiguousarray(self.bookmarks[idx], dtype=np.float64),
            char_len=np.ascontiguousarray(self.char_len[idx], dtype=np.float64),
            line_breaks=np.ascontiguousarray(self.line_breaks[idx], dtype=np.float64),
            has_url=np.ascontiguousarray(self.has_url[idx], dtype=np.float64),
            is_photo=np.ascontiguousarray(self.is_photo[idx], dtype=np.float64),
            is_video=np.ascontiguousarray(self.is_video[idx], dtype=np.float64),
            video_dur_ms=np.ascontiguousarray(self.video_dur_ms[idx], dtype=np.float64),
            is_mutual=np.ascontiguousarray(self.is_mutual[idx], dtype=np.float64),
            in_network=np.ascontiguousarray(self.in_network[idx], dtype=np.float64),
            is_reply=np.ascontiguousarray(self.is_reply[idx], dtype=np.float64),
            is_retweet=np.ascontiguousarray(self.is_retweet[idx], dtype=np.float64),
            vel_z=np.ascontiguousarray(self.vel_z[idx], dtype=np.float64),
            seismic=np.ascontiguousarray(self.seismic[idx], dtype=np.float64),
            cat_boost=np.ascontiguousarray(self.cat_boost[idx], dtype=np.float64),
            dpp_mult=np.ascontiguousarray(self.dpp_mult[idx], dtype=np.float64),
            actual_48h_views=np.ascontiguousarray(self.actual_48h_views[idx], dtype=np.float64),
            is_true_breakout_48h=np.ascontiguousarray(
                self.is_true_breakout_48h[idx], dtype=np.float64
            ),
            post_ids=np.ascontiguousarray(self.post_ids[idx], dtype=np.int64),
            author_ids=np.ascontiguousarray(self.author_ids[idx], dtype=np.int64),
            created_ts=np.ascontiguousarray(self.created_ts[idx], dtype=np.float64),
            is_video_mp4=np.ascontiguousarray(self.is_video_mp4[idx], dtype=np.float64),
            author_median_48h_views=np.ascontiguousarray(
                self.author_median_48h_views[idx], dtype=np.float64
            ),
            community_floor_views=np.ascontiguousarray(
                self.community_floor_views[idx], dtype=np.float64
            ),
            eval_age_mins=(
                np.ascontiguousarray(self.eval_age_mins[idx], dtype=np.float64)
                if self.eval_age_mins is not None
                else None
            ),
            author_followers=(
                np.ascontiguousarray(self.author_followers[idx], dtype=np.float64)
                if self.author_followers is not None
                else None
            ),
            reply_has_ancestors=(
                np.ascontiguousarray(self.reply_has_ancestors[idx], dtype=np.float64)
                if self.reply_has_ancestors is not None
                else None
            ),
            replies_to_root=(
                np.ascontiguousarray(self.replies_to_root[idx], dtype=np.float64)
                if self.replies_to_root is not None
                else None
            ),
            self_reply_passed=(
                np.ascontiguousarray(self.self_reply_passed[idx], dtype=np.float64)
                if self.self_reply_passed is not None
                else None
            ),
        )


@dataclass(slots=True)
class VectorizedScoreOutput:
    """Raw vectorized scoring arrays and aggregated evaluation metrics."""

    weighted_scores: np.ndarray
    vm_scores: np.ndarray
    effective_dpp_mult: np.ndarray
    breakout_scores: np.ndarray
    breakout_probs: np.ndarray
    watch_mask: np.ndarray
    surge_mask: np.ndarray
    y_true: np.ndarray
    metrics: dict[str, Any]


async def get_active_algo_version(
    session: AsyncSession, workspace_id: int
) -> AlgoParamVersion | None:
    """Fetches active workspace AlgoParamVersion, falling back to global 9106e9f baseline."""
    ws_active = (
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
    if ws_active is not None:
        return ws_active

    return (
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


def config_from_version(
    version: AlgoParamVersion | None,
    workspace: Workspace | None = None,
) -> ResolvedAlgoConfig:
    """Hydrates a ResolvedAlgoConfig from an AlgoParamVersion ORM row and Workspace."""
    default_watch = float(workspace.watch_threshold) if workspace is not None else 65.0
    default_surge = float(workspace.surge_threshold) if workspace is not None else 82.0

    if version is None:
        return ResolvedAlgoConfig(
            version_id=None,
            version_number=1,
            label="xai-org/x-algorithm @ 9106e9f (vm-ranker baseline)",
            source="upstream_xalgo",
            upstream_commit_sha=UPSTREAM_COMMIT_SHA,
            weights=ValueModelWeights.vm_ranker_defaults(),
            surrogate=SurrogateCalibrationParams(),
            pipeline=PipelineParams(),
            fusion=FusionCalibrationParams(),
            watch_threshold=default_watch,
            surge_threshold=default_surge,
        )

    weights = ValueModelWeights.vm_ranker_defaults()
    if version.weights_json:
        merged_w = weights.to_dict()
        merged_w.update(version.weights_json)
        weights = ValueModelWeights.from_dict(merged_w)

    surrogate = SurrogateCalibrationParams()
    if version.surrogate_params_json:
        merged_s = surrogate.to_dict()
        merged_s.update(version.surrogate_params_json)
        surrogate = SurrogateCalibrationParams.from_dict(merged_s)

    thresholds = dict(version.thresholds_json or {})
    pipe_dict = PipelineParams().to_dict()
    if isinstance(thresholds.get("pipeline"), Mapping):
        pipe_dict.update(thresholds["pipeline"])
    pipeline = PipelineParams.from_dict(pipe_dict)

    watch_t = float(thresholds.get("watch_threshold", default_watch))
    surge_t = float(thresholds.get("surge_threshold", default_surge))
    bo_mult = float(thresholds.get("breakout_multiple", 3.0))
    comm_floor = int(thresholds.get("community_floor_views", 5_000))
    raw_floors = thresholds.get("community_floors")
    comm_floors = (
        {str(k): int(v) for k, v in raw_floors.items()}
        if isinstance(raw_floors, Mapping)
        else {}
    )

    fusion_raw = thresholds.get("fusion")
    fusion = (
        FusionCalibrationParams.from_dict(fusion_raw)
        if isinstance(fusion_raw, Mapping)
        else FusionCalibrationParams()
    )

    return ResolvedAlgoConfig(
        version_id=version.id,
        version_number=version.version_number,
        label=version.label,
        source=version.source,
        upstream_commit_sha=version.upstream_commit_sha or UPSTREAM_COMMIT_SHA,
        weights=weights,
        surrogate=surrogate,
        pipeline=pipeline,
        fusion=fusion,
        watch_threshold=watch_t,
        surge_threshold=surge_t,
        breakout_multiple=bo_mult,
        community_floor_views=comm_floor,
        community_floors=comm_floors,
    )


def apply_candidate_overrides(
    base: ResolvedAlgoConfig,
    overrides: Mapping[str, Any] | None,
) -> ResolvedAlgoConfig:
    """Applies sandbox or candidate parameter overrides cleanly onto a base config."""
    if not overrides:
        return replace(
            base,
            weights=replace(base.weights),
            surrogate=replace(base.surrogate),
            pipeline=replace(base.pipeline),
            fusion=replace(base.fusion),
            community_floors=dict(base.community_floors),
        )

    w_dict = base.weights.to_dict()
    s_dict = base.surrogate.to_dict()
    p_dict = base.pipeline.to_dict()
    f_dict = base.fusion.to_dict()

    watch_t = base.watch_threshold
    surge_t = base.surge_threshold
    bo_mult = base.breakout_multiple
    comm_floor = base.community_floor_views
    comm_floors = dict(base.community_floors)

    if isinstance(overrides.get("weights"), Mapping):
        for k, v in overrides["weights"].items():
            if v is not None and k in w_dict:
                w_dict[k] = v
    if isinstance(overrides.get("surrogate_params"), Mapping):
        for k, v in overrides["surrogate_params"].items():
            if v is not None and k in s_dict:
                s_dict[k] = v
    if isinstance(overrides.get("pipeline"), Mapping):
        for k, v in overrides["pipeline"].items():
            if v is not None and k in p_dict:
                p_dict[k] = v
    if isinstance(overrides.get("fusion"), Mapping):
        for k, v in overrides["fusion"].items():
            if v is not None and k in f_dict:
                f_dict[k] = v
    if isinstance(overrides.get("thresholds"), Mapping):
        th = overrides["thresholds"]
        if th.get("watch_threshold") is not None:
            watch_t = _check_finite_float("watch_threshold", th["watch_threshold"], lo=1.0, hi=99.0)
        if th.get("surge_threshold") is not None:
            surge_t = _check_finite_float(
                "surge_threshold", th["surge_threshold"], lo=2.0, hi=100.0
            )
        if th.get("breakout_multiple") is not None:
            bo_mult = _check_finite_float(
                "breakout_multiple", th["breakout_multiple"], lo=1.0, hi=50.0
            )
        if th.get("community_floor_views") is not None:
            comm_floor = int(
                _check_finite_float(
                    "community_floor_views",
                    th["community_floor_views"],
                    lo=0.0,
                    hi=10_000_000.0,
                )
            )
        if isinstance(th.get("community_floors"), Mapping):
            for ck, cv in th["community_floors"].items():
                comm_floors[str(ck)] = int(
                    _check_finite_float(f"community_floors.{ck}", cv, lo=0.0, hi=10_000_000.0)
                )

    # Top-level flat convenience keys for direct sandbox sliders & API ergonomics
    for k, v in overrides.items():
        if v is None or k in ("weights", "surrogate_params", "pipeline", "fusion", "thresholds"):
            continue
        if k == "watch_threshold":
            watch_t = _check_finite_float("watch_threshold", v, lo=1.0, hi=99.0)
        elif k == "surge_threshold":
            surge_t = _check_finite_float("surge_threshold", v, lo=2.0, hi=100.0)
        elif k == "breakout_multiple":
            bo_mult = _check_finite_float("breakout_multiple", v, lo=1.0, hi=50.0)
        elif k == "community_floor_views":
            comm_floor = int(
                _check_finite_float("community_floor_views", v, lo=0.0, hi=10_000_000.0)
            )
        elif k in w_dict:
            w_dict[k] = v
        elif k in s_dict:
            s_dict[k] = v
        elif k in p_dict:
            p_dict[k] = v
        elif k in f_dict:
            f_dict[k] = v

    if watch_t >= surge_t:
        raise ValueError(
            f"watch_threshold ({watch_t}) must be strictly less than surge_threshold ({surge_t})."
        )

    return ResolvedAlgoConfig(
        version_id=None,
        version_number=base.version_number + 1,
        label="Candidate Sandbox Override",
        source="manual_sandbox",
        upstream_commit_sha=base.upstream_commit_sha,
        weights=ValueModelWeights.from_dict(w_dict),
        surrogate=SurrogateCalibrationParams.from_dict(s_dict),
        pipeline=PipelineParams.from_dict(p_dict),
        fusion=FusionCalibrationParams.from_dict(f_dict),
        watch_threshold=watch_t,
        surge_threshold=surge_t,
        breakout_multiple=bo_mult,
        community_floor_views=comm_floor,
        community_floors=comm_floors,
    )


def _shrink_propensity_vec(
    counts: np.ndarray,
    views: np.ndarray,
    prior_rate: float,
    alpha_0: float,
) -> np.ndarray:
    """Vectorized Empirical-Bayes rate shrinkage matching propensity.shrink_propensity."""
    v = np.maximum(views, 0.0)
    n = np.where(v > 0.0, np.clip(counts, 0.0, v), 0.0)
    prior_strength = max(float(alpha_0), 1e-6)
    p_bar = min(max(float(prior_rate), 0.0), 1.0)
    shrunk = (n + prior_strength * p_bar) / (v + prior_strength)
    return np.clip(shrunk, 0.0, 1.0)


def _offset_score_vec(combined: np.ndarray, weights: ValueModelWeights) -> np.ndarray:
    """Vectorized port of xai-value-model/scoring.rs offset_score."""
    total_sum = weights.total_sum()
    if total_sum == 0.0:
        return np.maximum(combined, 0.0)
    neg_sum = weights.negative_sum()
    return np.where(
        combined < 0.0,
        (combined + neg_sum) / total_sum * NEGATIVE_SCORES_OFFSET,
        combined + NEGATIVE_SCORES_OFFSET,
    )


def _author_diversity_vec(
    author_ids: np.ndarray,
    ordering_scores: np.ndarray,
    decay: float,
    floor: float,
) -> np.ndarray:
    """Vectorized stable rank-based author diversity multiplier when batch diversity is enabled."""
    n = ordering_scores.shape[0]
    if n == 0:
        return np.empty(0, dtype=np.float64)
    order = np.argsort(-ordering_scores, kind="mergesort")
    sorted_authors = author_ids[order]

    # Group identical author_ids using stable lexsort to count prior occurrences in O(N log N)
    lex_idx = np.lexsort((np.arange(n), sorted_authors))
    grouped_authors = sorted_authors[lex_idx]
    group_starts = np.ones(n, dtype=np.int64)
    if n > 1:
        group_starts[1:] = (grouped_authors[1:] != grouped_authors[:-1]).astype(np.int64)
    # Cumulative rank within each author group
    start_positions = np.maximum.accumulate(
        np.where(group_starts == 1, np.arange(n), 0)
    )
    within_group_k = np.arange(n) - start_positions

    k_in_sorted = np.empty(n, dtype=np.float64)
    k_in_sorted[lex_idx] = within_group_k.astype(np.float64)
    k_orig = np.empty(n, dtype=np.float64)
    k_orig[order] = k_in_sorted
    return (1.0 - floor) * np.power(decay, k_orig) + floor


def compute_calibration_metrics_vec(
    probs: np.ndarray,
    pred_pos_mask: np.ndarray,
    y_true_float: np.ndarray,
    views_at_eval: np.ndarray,
    actual_48h_views: np.ndarray,
    created_ts: np.ndarray | None = None,
    actual_multiples: np.ndarray | None = None,
) -> dict[str, Any]:
    """Computes vectorized Precision, Recall, F0.5, Brier, ROC-AUC, 10-bin calibration, & lead-time upside."""
    n = int(probs.shape[0])
    if n == 0:
        empty_bins = [
            {
                "bin_index": b,
                "bin_lower": round(b * 0.1, 2),
                "bin_upper": round((b + 1) * 0.1, 2),
                "bin_midpoint": round((b + 0.5) * 0.1, 2),
                "count": 0,
                "predicted_prob_mean": None,
                "observed_breakout_rate": None,
                "calibration_gap": None,
            }
            for b in range(10)
        ]
        return {
            "matured_posts_count": 0,
            "tp": 0,
            "fp": 0,
            "fn": 0,
            "tn": 0,
            "alerts_total": 0,
            "alerts_per_day": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "f05_score": 0.0,
            "brier_score": 0.0,
            "ece": 0.0,
            "roc_auc": 0.5,
            "spearman_rho": 0.0,
            "median_lead_time_growth_share": 0.0,
            "median_lead_time_multiple": 1.0,
            "calibration_bins": empty_bins,
        }

    y_bool = y_true_float >= 0.5
    p_bool = pred_pos_mask.astype(bool)

    tp = int(np.sum(p_bool & y_bool))
    fp = int(np.sum(p_bool & (~y_bool)))
    fn = int(np.sum((~p_bool) & y_bool))
    tn = int(np.sum((~p_bool) & (~y_bool)))

    precision = float(tp / (tp + fp)) if (tp + fp) > 0 else 0.0
    recall = float(tp / (tp + fn)) if (tp + fn) > 0 else 0.0
    denom_f05 = 0.25 * precision + recall
    f05 = float(1.25 * precision * recall / denom_f05) if denom_f05 > 0.0 else 0.0

    brier_errors = np.square(probs - y_true_float)
    brier_score = float(np.mean(brier_errors))

    # Exact ROC-AUC via Mann-Whitney U rank statistic
    n_pos = tp + fn
    n_neg = fp + tn
    if n_pos > 0 and n_neg > 0:
        ranks = rankdata(probs, method="average")
        sum_pos_ranks = float(np.sum(ranks[y_bool]))
        roc_auc = float((sum_pos_ranks - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))
    else:
        roc_auc = 0.5

    spearman_val = 0.0
    if actual_multiples is not None and n >= 3:
        if float(np.std(probs)) > 1e-12 and float(np.std(actual_multiples)) > 1e-12:
            rho, _ = spearmanr(probs, actual_multiples)
            if not math.isnan(float(rho)):
                spearman_val = float(rho)

    # 10-bin reliability / calibration curve via np.bincount
    bin_ids = np.clip(np.floor(probs * 10.0).astype(np.int64), 0, 9)
    bin_counts = np.bincount(bin_ids, minlength=10)
    bin_prob_sums = np.bincount(bin_ids, weights=probs, minlength=10)
    bin_obs_sums = np.bincount(bin_ids, weights=y_true_float, minlength=10)

    calibration_bins: list[dict[str, Any]] = []
    ece_accum = 0.0
    for b in range(10):
        cnt = int(bin_counts[b])
        if cnt > 0:
            p_mean = float(bin_prob_sums[b] / cnt)
            obs_rate = float(bin_obs_sums[b] / cnt)
            gap = abs(p_mean - obs_rate)
            ece_accum += (cnt / float(n)) * gap
            calibration_bins.append(
                {
                    "bin_index": b,
                    "bin_lower": round(b * 0.1, 2),
                    "bin_upper": round((b + 1) * 0.1, 2),
                    "bin_midpoint": round((b + 0.5) * 0.1, 2),
                    "count": cnt,
                    "predicted_prob_mean": round(p_mean, 4),
                    "observed_breakout_rate": round(obs_rate, 4),
                    "calibration_gap": round(gap, 4),
                }
            )
        else:
            calibration_bins.append(
                {
                    "bin_index": b,
                    "bin_lower": round(b * 0.1, 2),
                    "bin_upper": round((b + 1) * 0.1, 2),
                    "bin_midpoint": round((b + 0.5) * 0.1, 2),
                    "count": 0,
                    "predicted_prob_mean": None,
                    "observed_breakout_rate": None,
                    "calibration_gap": None,
                }
            )

    # Lead-time upside across triggered alerts (or true breakouts if 0 alerts)
    lead_mask = p_bool if (tp + fp) > 0 else y_bool
    if int(np.sum(lead_mask)) > 0:
        v_alert = np.maximum(views_at_eval[lead_mask], 1.0)
        v_final = np.maximum(actual_48h_views[lead_mask], 1.0)
        growth_shares = np.maximum(0.0, (v_final - v_alert) / v_final)
        growth_multiples = v_final / v_alert
        med_growth_share = float(np.median(growth_shares))
        med_growth_mult = float(np.median(growth_multiples))
    else:
        med_growth_share = 0.0
        med_growth_mult = 1.0

    alerts_total = tp + fp
    if created_ts is not None and created_ts.shape[0] > 1:
        span_days = max(1.0, float(np.max(created_ts) - np.min(created_ts)) / 86_400.0)
    else:
        span_days = max(1.0, n / 8.0)
    alerts_per_day = float(alerts_total / span_days)

    return {
        "matured_posts_count": n,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "alerts_total": alerts_total,
        "alerts_per_day": round(alerts_per_day, 2),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f05_score": round(f05, 4),
        "brier_score": round(brier_score, 5),
        "ece": round(ece_accum, 5),
        "roc_auc": round(roc_auc, 4),
        "spearman_rho": round(spearman_val, 4),
        "median_lead_time_growth_share": round(med_growth_share, 4),
        "median_lead_time_multiple": round(med_growth_mult, 2),
        "calibration_bins": calibration_bins,
    }


def vectorized_score_trajectories(
    matrix: TrajectoryMatrix,
    config: ResolvedAlgoConfig,
    *,
    batch_author_diversity: bool = False,
    recompute_ground_truth: bool = False,
    override_community_floor: bool = False,
) -> VectorizedScoreOutput:
    """Scores N trajectories simultaneously in pure NumPy matching xai-value-model 9106e9f
    and `radar.scorer` post-fusion breakout math to IEEE-754 float64 precision.
    """
    n = matrix.size
    if n == 0:
        empty = np.empty(0, dtype=np.float64)
        empty_b = np.empty(0, dtype=bool)
        return VectorizedScoreOutput(
            weighted_scores=empty,
            vm_scores=empty,
            effective_dpp_mult=empty,
            breakout_scores=empty,
            breakout_probs=empty,
            watch_mask=empty_b,
            surge_mask=empty_b,
            y_true=empty,
            metrics=compute_calibration_metrics_vec(empty, empty_b, empty, empty, empty),
        )

    cfg = config.surrogate
    weights = config.weights
    pipe = config.pipeline
    fus = config.fusion
    a0 = max(float(cfg.prior_views_alpha0), 1e-6)

    views = matrix.views_at_eval
    fav_p = _shrink_propensity_vec(matrix.likes, views, cfg.prior_favorite_rate, a0)
    reply_p = _shrink_propensity_vec(matrix.replies, views, cfg.prior_reply_rate, a0)
    rt_p = _shrink_propensity_vec(matrix.reposts, views, cfg.prior_retweet_rate, a0)
    quote_p = _shrink_propensity_vec(matrix.quotes, views, cfg.prior_quote_rate, a0)
    bmk_p = _shrink_propensity_vec(matrix.bookmarks, views, cfg.prior_bookmark_rate, a0)

    copy_link_p = np.minimum(
        1.0,
        cfg.copy_link_from_bookmark_mult * bmk_p + cfg.copy_link_from_quote_mult * quote_p,
    )
    dm_share_p = np.minimum(1.0, cfg.dm_share_from_bookmark_mult * bmk_p)
    share_p = np.minimum(1.0, cfg.share_from_bookmark_mult * bmk_p + 0.15 * quote_p)

    has_url = matrix.has_url > 0.5
    is_photo = matrix.is_photo > 0.5
    is_video = matrix.is_video > 0.5
    is_video_mp4 = matrix.is_video_mp4 > 0.5
    is_mutual = matrix.is_mutual > 0.5
    in_net_true = matrix.in_network > 0.5
    in_net_false = (matrix.in_network >= 0.0) & (matrix.in_network <= 0.5)
    is_reply = matrix.is_reply > 0.5
    is_retweet = matrix.is_retweet > 0.5

    dwell_max_safe = max(float(cfg.dwell_max_secs), 1e-6)
    raw_dwell_secs = np.minimum(
        dwell_max_safe,
        cfg.dwell_base_secs
        + (matrix.char_len / 100.0) * cfg.dwell_per_100_chars_secs
        + np.minimum(matrix.line_breaks, 8.0) * 0.35
        + 120.0 * bmk_p,
    )
    raw_dwell_secs = np.where(has_url, raw_dwell_secs * cfg.url_dwell_discount, raw_dwell_secs)

    dwell_prob = np.minimum(0.95, 0.12 + (raw_dwell_secs / dwell_max_safe) * 0.55 + 2.5 * bmk_p)
    click_prob = np.minimum(
        0.85,
        0.04 + 0.35 * np.where(matrix.char_len > 240.0, 1.0, 0.2) + 4.0 * (bmk_p + reply_p),
    )
    click_dwell_secs = raw_dwell_secs * click_prob * 1.4
    not_dwelled_prob = np.maximum(0.01, cfg.not_dwelled_base_prob * (1.0 - 0.7 * dwell_prob))

    photo_expand_p = np.where(
        is_photo, np.minimum(1.0, cfg.media_photo_expand_prob + 1.5 * fav_p), 0.0
    )
    video_open_p = np.where(
        is_video, np.minimum(1.0, cfg.media_video_open_prob + 2.0 * bmk_p), 0.0
    )
    vqv_p = np.where(
        is_video, np.minimum(1.0, video_open_p * cfg.vqv_conditional_prob), 0.0
    )
    eff_vid_ms = np.where(matrix.video_dur_ms > 0.0, matrix.video_dur_ms, 12_000.0)
    video_cont_secs = np.where(is_video, np.minimum(45.0, eff_vid_ms / 1000.0 * 0.42), 0.0)

    follow_p = np.minimum(1.0, cfg.follow_from_bookmark_quote_mult * (bmk_p + 2.0 * quote_p))
    profile_click_p = np.minimum(
        1.0, cfg.profile_click_from_engagement_mult * (reply_p + quote_p + bmk_p)
    )
    open_link_p = np.where(has_url, cfg.open_link_prob_when_url, 0.0)
    unexplored_p = 1.0 / (1.0 + np.maximum(views, 0.0) / max(a0, 1.0))
    quoted_click_p = np.where(quote_p > 0.0, 0.5 * click_prob, 0.0)
    home_profile_visit_secs = np.where(profile_click_p > 0.0, 4.5, 0.0)
    post_video_p = np.where(is_video, 1.0, 0.0)

    # Exact 29-head left-to-right fold matching xai-value-model/src/scoring.rs
    bidir_elig = (~is_reply) & (~is_retweet) & is_mutual
    reply_w = np.where(
        bidir_elig & (weights.bidirectional_follow_reply_weight_boost != 0.0),
        weights.reply + weights.bidirectional_follow_reply_weight_boost,
        weights.reply,
    )
    dwell_w = np.where(
        bidir_elig & (weights.bidirectional_follow_dwell_weight_boost != 0.0),
        weights.dwell + weights.bidirectional_follow_dwell_weight_boost,
        weights.dwell,
    )
    vqv_elig = is_video_mp4 & (matrix.video_dur_ms >= float(weights.min_video_duration_ms))
    vqv_w = np.where(vqv_elig, weights.vqv, 0.0)
    if weights.post_unexplored_include_out_of_network:
        unexplored_w: float | np.ndarray = weights.post_unexplored
    else:
        unexplored_w = np.where(in_net_true, weights.post_unexplored, 0.0)

    combined = np.zeros(n, dtype=np.float64)
    combined = combined + fav_p * weights.favorite
    combined = combined + reply_p * reply_w
    combined = combined + rt_p * weights.retweet
    combined = combined + photo_expand_p * weights.photo_expand
    combined = combined + video_open_p * weights.video_open
    combined = combined + click_prob * weights.click
    combined = combined + open_link_p * weights.open_link
    combined = combined + profile_click_p * weights.profile_click
    combined = combined + vqv_p * vqv_w
    combined = combined + share_p * weights.share
    combined = combined + dm_share_p * weights.share_via_dm
    combined = combined + copy_link_p * weights.share_via_copy_link
    combined = combined + dwell_prob * dwell_w
    combined = combined + quote_p * weights.quote
    combined = combined + quoted_click_p * weights.quoted_click
    # Head 16: quoted_vqv_score is 0.0 in observable propensity mapping
    combined = combined + 0.0
    combined = combined + raw_dwell_secs * weights.cont_dwell_time
    combined = combined + click_dwell_secs * weights.cont_click_dwell_time
    combined = combined + (video_open_p * video_cont_secs) * weights.video_continuation
    # Head 20: set_user_video_continuation parity when weight != 0
    if weights.user_video_continuation != 0.0:
        if batch_author_diversity:
            total_opens = float(np.sum(video_open_p))
            user_vid_cont_secs: float | np.ndarray = (
                float(np.sum(video_open_p * video_cont_secs) / total_opens)
                if total_opens > 0.0
                else 0.0
            )
        else:
            user_vid_cont_secs = np.where(video_open_p > 0.0, video_cont_secs, 0.0)
        combined = combined + (video_open_p * user_vid_cont_secs) * weights.user_video_continuation
    else:
        combined = combined + 0.0
    combined = combined + (profile_click_p * home_profile_visit_secs) * weights.profile_visit_secs
    combined = combined + follow_p * weights.follow_author
    combined = combined + 0.00015 * weights.not_interested
    combined = combined + 0.00002 * weights.block_author
    combined = combined + 0.00004 * weights.mute_author
    combined = combined + 0.000005 * weights.report
    combined = combined + not_dwelled_prob * weights.not_dwelled
    combined = combined + unexplored_p * unexplored_w
    combined = combined + post_video_p * weights.post_video

    weighted = _offset_score_vec(combined, weights)

    oon_mask = in_net_false | (
        in_net_true
        & bool(weights.oon_rescore_in_network_replies_retweets)
        & (is_reply | is_retweet)
    )
    oon_mult = np.where(oon_mask, float(pipe.oon_weight_factor), 1.0)

    if batch_author_diversity and weights.enable_author_diversity:
        div_mult = _author_diversity_vec(
            matrix.author_ids,
            weighted,
            weights.author_diversity_decay,
            weights.author_diversity_floor,
        )
    else:
        div_mult = np.ones(n, dtype=np.float64)

    post_mult = div_mult * oon_mult
    if weights.multiplier_pre_offset:
        scaled_net = np.where(combined >= 0.0, post_mult * combined, combined)
        vm_scores = _offset_score_vec(scaled_net, weights)
    else:
        vm_scores = weighted * post_mult

    # Rescale DPP novelty multiplier analytically for candidate dpp_theta:
    # base dpp_mult = (d_i^2)^(1 - 0.65) => d_i^2 = dpp_mult^(1 / 0.35)
    base_dpp = np.clip(matrix.dpp_mult, 0.0, 1.0)
    if pipe.dpp_enabled:
        theta_clamped = min(max(float(pipe.dpp_theta), 0.0), 1.0)
        if abs(theta_clamped - DEFAULT_DPP_THETA) <= 1e-9:
            eff_dpp = base_dpp
        else:
            residual_sq = np.where(
                base_dpp > 1e-12,
                np.power(base_dpp, 1.0 / (1.0 - DEFAULT_DPP_THETA)),
                0.0,
            )
            eff_dpp = np.where(
                residual_sq > 1e-12,
                np.power(residual_sq, 1.0 - theta_clamped),
                0.0,
            )
    else:
        eff_dpp = np.ones(n, dtype=np.float64)

    # Stage 1 pre-scoring filter & AuthorColdStart parity when structural metadata is packed
    if matrix.eval_age_mins is not None and matrix.author_followers is not None:
        age_secs = np.maximum(matrix.eval_age_mins, 0.0) * 60.0
        cold_start_mask = (
            (~is_reply)
            & (~is_retweet)
            & (matrix.author_followers <= float(pipe.cold_start_follower_cap))
            & (age_secs <= float(pipe.cold_start_max_post_age_secs))
            & (views < float(pipe.cold_start_impression_threshold))
        )
        cold_bonus = np.where(cold_start_mask, float(fus.cold_start_bonus), 0.0)

        has_anc = (
            (matrix.reply_has_ancestors > 0.5)
            if matrix.reply_has_ancestors is not None
            else np.ones(n, dtype=bool)
        )
        rep_root = (
            (matrix.replies_to_root > 0.5)
            if matrix.replies_to_root is not None
            else np.ones(n, dtype=bool)
        )
        chain_ok = (
            (matrix.self_reply_passed > 0.5)
            if matrix.self_reply_passed is not None
            else np.ones(n, dtype=bool)
        )
        is_phx_retrieval = ~in_net_true
        oon_reply_allowed = (
            bool(pipe.enable_phoenix_oon_replies)
            & is_phx_retrieval
            & ((not bool(pipe.phoenix_oon_replies_root_only)) | rep_root)
        )
        oon_reply_dropped = is_reply & (~oon_reply_allowed)
        oon_dropped = (in_net_false & (is_retweet | oon_reply_dropped)) | (is_reply & (~has_anc))
        age_dropped = age_secs > (float(pipe.simclusters_max_candidate_age_hours) * 3600.0)
        dropped_hard = oon_dropped | (~chain_ok) | age_dropped
    else:
        cold_bonus = np.zeros(n, dtype=np.float64)
        dropped_hard = np.zeros(n, dtype=bool)

    # Exact Stage 1 + Stage 2 breakout fusion & soft-knee curve matching radar.scorer
    dpp_blend = min(max(float(fus.dpp_blend), 0.0), 1.0)
    vm_clipped = np.clip(vm_scores, 0.0, max(float(fus.vm_cap), 0.0))
    base_signal = (
        float(fus.intercept)
        + float(fus.vel_weight) * matrix.vel_z
        + float(fus.seismic_weight) * matrix.seismic
        + float(fus.vm_weight) * vm_clipped
        + float(fus.catalysis_weight) * matrix.cat_boost
        + cold_bonus
    )
    crowded_scaled = base_signal * ((1.0 - dpp_blend) + dpp_blend * eff_dpp)
    compressed = np.minimum(99.5, 85.0 + 14.5 * np.tanh((crowded_scaled - 85.0) / 26.0))
    breakout_scores = np.where(
        dropped_hard | (crowded_scaled <= 0.0),
        0.0,
        np.where(crowded_scaled <= 85.0, crowded_scaled, compressed),
    )

    prob_scale = max(float(fus.prob_scale), 0.5)
    logits = np.clip(-(breakout_scores - float(fus.prob_midpoint)) / prob_scale, -40.0, 40.0)
    breakout_probs = np.where(dropped_hard, 0.0, 1.0 / (1.0 + np.exp(logits)))

    watch_mask = breakout_scores >= float(config.watch_threshold)
    surge_mask = breakout_scores >= float(config.surge_threshold)

    if recompute_ground_truth:
        eff_floors: float | np.ndarray = (
            float(config.community_floor_views)
            if override_community_floor
            else matrix.community_floor_views
        )
        y_true = (
            (matrix.actual_48h_views >= float(config.breakout_multiple) * matrix.author_median_48h_views)
            & (matrix.actual_48h_views >= eff_floors)
        ).astype(np.float64)
    else:
        y_true = matrix.is_true_breakout_48h

    actual_mults = matrix.actual_48h_views / np.maximum(matrix.author_median_48h_views, 1.0)
    metrics = compute_calibration_metrics_vec(
        probs=breakout_probs,
        pred_pos_mask=watch_mask,
        y_true_float=y_true,
        views_at_eval=matrix.views_at_eval,
        actual_48h_views=matrix.actual_48h_views,
        created_ts=matrix.created_ts,
        actual_multiples=actual_mults,
    )

    return VectorizedScoreOutput(
        weighted_scores=weighted,
        vm_scores=vm_scores,
        effective_dpp_mult=eff_dpp,
        breakout_scores=breakout_scores,
        breakout_probs=breakout_probs,
        watch_mask=watch_mask,
        surge_mask=surge_mask,
        y_true=y_true,
        metrics=metrics,
    )


async def pack_workspace_trajectories(
    session: AsyncSession,
    workspace_id: int,
    *,
    matured_only: bool = True,
    now: datetime | None = None,
) -> tuple[TrajectoryMatrix, list[TrajectoryMeta]]:
    """Loads workspace posts + 15-60m snapshots in bulk and packs contiguous float64 arrays
    without leaking post-60m velocity, SEISMIC, or catalysis values into historical replay.
    """
    ref_now = ensure_utc(now)
    ws = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one()
    active_cfg = config_from_version(await get_active_algo_version(session, workspace_id), ws)
    has_my_handle = bool(ws.my_x_handle and ws.my_x_handle.strip())

    ws_accounts = list(
        (
            await session.execute(
                select(TrackedAccount).where(TrackedAccount.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    ws_accounts_by_id: dict[int, TrackedAccount] = {a.id: a for a in ws_accounts}
    followed_author_ids: list[int] = [
        a.id for a in ws_accounts if has_my_handle and a.followed_by_me
    ]
    viewer_id = _stable_int_id(ws.my_x_handle) if has_my_handle else 0

    comms = list(
        (
            await session.execute(
                select(Community)
                .where(Community.workspace_id == workspace_id)
                .order_by(Community.sort_order.asc(), Community.id.asc())
            )
        )
        .scalars()
        .all()
    )
    comm_by_id = {c.id: c for c in comms}

    mem_rows = list(
        (
            await session.execute(
                select(AccountCommunityMembership).where(
                    AccountCommunityMembership.workspace_id == workspace_id
                )
            )
        )
        .scalars()
        .all()
    )
    acct_comms: dict[int, list[Community]] = {}
    for m in mem_rows:
        if not _is_active_membership(m):
            continue
        c_obj = comm_by_id.get(m.community_id)
        if c_obj is not None:
            acct_comms.setdefault(m.account_id, []).append(c_obj)

    rows = list(
        (
            await session.execute(
                select(Post, TrackedAccount)
                .join(TrackedAccount, Post.author_account_id == TrackedAccount.id)
                .where(Post.workspace_id == workspace_id)
                .order_by(Post.created_at_x.asc(), Post.id.asc())
            )
        ).all()
    )

    filtered: list[tuple[Post, TrackedAccount]] = []
    for post, author in rows:
        age_hours = (ref_now - ensure_utc(post.created_at_x)).total_seconds() / 3600.0
        is_matured = bool(post.matured_48h) or age_hours >= 48.0
        if matured_only and not is_matured:
            continue
        filtered.append((post, author))

    n = len(filtered)
    if n == 0:
        z_f = np.zeros(0, dtype=np.float64)
        z_i = np.zeros(0, dtype=np.int64)
        return (
            TrajectoryMatrix(
                views_at_eval=z_f,
                likes=z_f,
                replies=z_f,
                reposts=z_f,
                quotes=z_f,
                bookmarks=z_f,
                char_len=z_f,
                line_breaks=z_f,
                has_url=z_f,
                is_photo=z_f,
                is_video=z_f,
                video_dur_ms=z_f,
                is_mutual=z_f,
                in_network=z_f,
                is_reply=z_f,
                is_retweet=z_f,
                vel_z=z_f,
                seismic=z_f,
                cat_boost=z_f,
                dpp_mult=z_f,
                actual_48h_views=z_f,
                is_true_breakout_48h=z_f,
                post_ids=z_i,
                author_ids=z_i,
                created_ts=z_f,
                is_video_mp4=z_f,
                author_median_48h_views=z_f,
                community_floor_views=z_f,
                eval_age_mins=z_f,
                author_followers=z_f,
                reply_has_ancestors=z_f,
                replies_to_root=z_f,
                self_reply_passed=z_f,
            ),
            [],
        )

    post_id_list = [p.id for p, _ in filtered]
    snap_rows = list(
        (
            await session.execute(
                select(PostSnapshot)
                .where(PostSnapshot.post_id.in_(post_id_list))
                .order_by(PostSnapshot.post_id.asc(), PostSnapshot.age_minutes.asc())
            )
        )
        .scalars()
        .all()
    )
    snaps_by_post: dict[int, list[PostSnapshot]] = {}
    for s in snap_rows:
        snaps_by_post.setdefault(s.post_id, []).append(s)

    inter_rows = list(
        (
            await session.execute(
                select(PostInteraction)
                .where(PostInteraction.post_id.in_(post_id_list))
                .order_by(PostInteraction.occurred_at.asc(), PostInteraction.id.asc())
            )
        )
        .scalars()
        .all()
    )
    inters_by_post: dict[int, list[PostInteraction]] = {}
    for inter in inter_rows:
        inters_by_post.setdefault(inter.post_id, []).append(inter)

    views_at_eval = np.empty(n, dtype=np.float64)
    likes = np.empty(n, dtype=np.float64)
    replies = np.empty(n, dtype=np.float64)
    reposts = np.empty(n, dtype=np.float64)
    quotes = np.empty(n, dtype=np.float64)
    bookmarks = np.empty(n, dtype=np.float64)
    char_len = np.empty(n, dtype=np.float64)
    line_breaks = np.empty(n, dtype=np.float64)
    has_url = np.empty(n, dtype=np.float64)
    is_photo = np.empty(n, dtype=np.float64)
    is_video = np.empty(n, dtype=np.float64)
    is_video_mp4 = np.empty(n, dtype=np.float64)
    video_dur_ms = np.empty(n, dtype=np.float64)
    is_mutual = np.empty(n, dtype=np.float64)
    in_network = np.empty(n, dtype=np.float64)
    is_reply = np.empty(n, dtype=np.float64)
    is_retweet = np.empty(n, dtype=np.float64)
    vel_z = np.empty(n, dtype=np.float64)
    seismic = np.empty(n, dtype=np.float64)
    cat_boost = np.empty(n, dtype=np.float64)
    dpp_mult = np.empty(n, dtype=np.float64)
    actual_48h_views = np.empty(n, dtype=np.float64)
    author_median_48h = np.empty(n, dtype=np.float64)
    comm_floors = np.empty(n, dtype=np.float64)
    is_true_bo = np.empty(n, dtype=np.float64)
    post_ids = np.empty(n, dtype=np.int64)
    author_ids = np.empty(n, dtype=np.int64)
    created_ts = np.empty(n, dtype=np.float64)
    eval_age_mins = np.empty(n, dtype=np.float64)
    author_followers = np.empty(n, dtype=np.float64)
    reply_has_ancestors = np.empty(n, dtype=np.float64)
    replies_to_root = np.empty(n, dtype=np.float64)
    self_reply_passed = np.empty(n, dtype=np.float64)

    metas: list[TrajectoryMeta] = []

    for i, (post, author) in enumerate(filtered):
        dt_utc = ensure_utc(post.created_at_x)
        snaps = snaps_by_post.get(post.id, [])
        early_snaps = [s for s in snaps if s.age_minutes <= 60]
        chosen_snap: PostSnapshot | None = None
        if early_snaps:
            chosen_snap = min(early_snaps, key=lambda s: abs(s.age_minutes - 30))
        elif snaps and snaps[0].age_minutes <= 120:
            chosen_snap = snaps[0]

        if chosen_snap is not None:
            views_at_eval[i] = float(chosen_snap.views)
            likes[i] = float(chosen_snap.likes)
            replies[i] = float(chosen_snap.replies)
            reposts[i] = float(chosen_snap.reposts)
            quotes[i] = float(chosen_snap.quotes)
            bookmarks[i] = float(chosen_snap.bookmarks)
            eval_age = max(1, int(chosen_snap.age_minutes))
        elif not post.matured_48h and (ref_now - dt_utc).total_seconds() < 48.0 * 3600.0:
            views_at_eval[i] = float(post.latest_views or 0)
            likes[i] = float(post.latest_likes or 0)
            replies[i] = float(post.latest_replies or 0)
            reposts[i] = float(post.latest_reposts or 0)
            quotes[i] = float(post.latest_quotes or 0)
            bookmarks[i] = float(post.latest_bookmarks or 0)
            eval_age = max(1, int(round((ref_now - dt_utc).total_seconds() / 60.0)))
        else:
            views_at_eval[i] = float(max(1, int((post.latest_views or 0) * 0.25)))
            likes[i] = float(int((post.latest_likes or 0) * 0.25))
            replies[i] = float(int((post.latest_replies or 0) * 0.25))
            reposts[i] = float(int((post.latest_reposts or 0) * 0.25))
            quotes[i] = float(int((post.latest_quotes or 0) * 0.25))
            bookmarks[i] = float(int((post.latest_bookmarks or 0) * 0.25))
            eval_age = 30

        eval_age_mins[i] = float(eval_age)
        author_followers[i] = float(author.followers_count or 0)

        txt = post.text or ""
        char_len[i] = float(len(txt.strip()))
        line_breaks[i] = float(txt.count("\n"))
        has_url[i] = 1.0 if post.has_url else 0.0
        is_photo[i] = (
            1.0 if (post.has_media and post.media_type in ("photo", "image", None)) else 0.0
        )
        is_video[i] = (
            1.0 if (post.has_media and post.media_type in ("video", "animated_gif")) else 0.0
        )
        is_video_mp4[i] = 1.0 if (post.has_media and post.media_type == "video") else 0.0
        video_dur_ms[i] = float(post.video_duration_ms or 0)

        in_net_bool = bool(author.followed_by_me) if has_my_handle else False
        is_mut_bool = bool(has_my_handle and author.follows_me and author.followed_by_me)
        is_mutual[i] = 1.0 if is_mut_bool else 0.0
        in_network[i] = 1.0 if in_net_bool else 0.0
        is_reply[i] = 1.0 if post.is_reply else 0.0
        is_retweet[i] = 1.0 if post.is_retweet else 0.0

        # Stage-1 structural flags matching radar.scorer.evaluate_stage1_filters
        author_int_id = author.id or _stable_int_id(author.x_user_id)
        tweet_int_id = post.id or _stable_int_id(post.x_post_id)
        in_reply_to_id = _stable_int_id(post.in_reply_to_post_id) if post.is_reply else None
        if post.is_reply and in_reply_to_id == 0:
            in_reply_to_id = 1
        conv_id = _stable_int_id(post.conversation_id) if post.conversation_id else in_reply_to_id
        ancestor_users = [_stable_int_id(u) for u in (post.ancestor_author_ids_json or []) if u]
        ancestors = (
            [in_reply_to_id]
            if (post.is_reply and post.in_reply_to_post_id)
            else ([1] if (post.is_reply and ancestor_users) else [])
        )
        reply_has_ancestors[i] = 1.0 if (not post.is_reply or len(ancestors) > 0) else 0.0
        replies_to_root[i] = (
            1.0 if (conv_id is not None and in_reply_to_id == conv_id) else 0.0
        )
        allowed_ids = list(followed_author_ids)
        if in_net_bool:
            allowed_ids.append(author_int_id)
        chain_filter = SelfReplyChainFilter(viewer_id=viewer_id, followed_user_ids=allowed_ids)
        chain_cand = FilterCandidate(
            tweet_id=tweet_int_id,
            author_id=author_int_id,
            in_reply_to_tweet_id=in_reply_to_id,
            ancestor_users=ancestor_users,
        )
        self_reply_passed[i] = 1.0 if len(chain_filter.filter([chain_cand]).kept) == 1 else 0.0

        # Strictly causal early-snapshot Stage 1 signals (zero 48h future leakage)
        vel_metrics = compute_velocity_metrics(
            views=views_at_eval[i],
            age_minutes=float(eval_age),
            baseline_curves=author.baseline_curves_json,
            followers_fallback=author.followers_count or 10_000,
        )
        vel_z[i] = float(vel_metrics.velocity_z_score)

        snaps_up_to_eval = [s for s in snaps if s.age_minutes <= eval_age]
        seis_est = estimate_seismic(
            age_minutes=float(eval_age),
            views=views_at_eval[i],
            likes=likes[i],
            replies=replies[i],
            reposts=reposts[i],
            quotes=quotes[i],
            bookmarks=bookmarks[i],
            snapshots=snaps_up_to_eval,
        )
        seismic[i] = float(seis_est.infectiousness)

        eval_cutoff = dt_utc + timedelta(minutes=eval_age)
        early_inters = [
            inter
            for inter in inters_by_post.get(post.id, ())
            if ensure_utc(inter.occurred_at) <= eval_cutoff
        ]
        cat_boost[i] = float(compute_catalysis_boost(post, early_inters, ws_accounts_by_id))
        dpp_mult[i] = float(
            post.dpp_novelty_multiplier if post.dpp_novelty_multiplier is not None else 1.0
        )

        med_48h = float(extract_author_median_48h(author))
        author_median_48h[i] = med_48h
        final_v = float(
            post.actual_48h_views if post.actual_48h_views is not None else post.latest_views
        )
        actual_48h_views[i] = final_v

        a_comms = acct_comms.get(author.id, [])
        floor_v = active_cfg.community_floor_views
        for c in a_comms:
            if c.slug in active_cfg.community_floors:
                floor_v = min(floor_v, active_cfg.community_floors[c.slug])
            elif str(c.id) in active_cfg.community_floors:
                floor_v = min(floor_v, active_cfg.community_floors[str(c.id)])
        comm_floors[i] = float(floor_v)

        if post.is_true_breakout_48h is not None:
            true_bo = bool(post.is_true_breakout_48h)
        else:
            true_bo = (final_v >= active_cfg.breakout_multiple * med_48h) and (
                final_v >= float(floor_v)
            )
        is_true_bo[i] = 1.0 if true_bo else 0.0

        post_ids[i] = int(post.id)
        author_ids[i] = int(author.id)
        created_ts[i] = float(dt_utc.timestamp())

        metas.append(
            TrajectoryMeta(
                post_id=post.id,
                x_post_id=post.x_post_id,
                author_id=author.id,
                author_handle=author.handle,
                author_display_name=author.display_name,
                author_bridge_role=author.bridge_role,
                communities=[
                    {"id": c.id, "name": c.name, "slug": c.slug, "color": c.color}
                    for c in a_comms
                ],
                text=txt,
                created_at_iso=dt_utc.isoformat(),
                alert_age_minutes=eval_age,
            )
        )

    return (
        TrajectoryMatrix(
            views_at_eval=views_at_eval,
            likes=likes,
            replies=replies,
            reposts=reposts,
            quotes=quotes,
            bookmarks=bookmarks,
            char_len=char_len,
            line_breaks=line_breaks,
            has_url=has_url,
            is_photo=is_photo,
            is_video=is_video,
            video_dur_ms=video_dur_ms,
            is_mutual=is_mutual,
            in_network=in_network,
            is_reply=is_reply,
            is_retweet=is_retweet,
            vel_z=vel_z,
            seismic=seismic,
            cat_boost=cat_boost,
            dpp_mult=dpp_mult,
            actual_48h_views=actual_48h_views,
            is_true_breakout_48h=is_true_bo,
            post_ids=post_ids,
            author_ids=author_ids,
            created_ts=created_ts,
            is_video_mp4=is_video_mp4,
            author_median_48h_views=author_median_48h,
            community_floor_views=comm_floors,
            eval_age_mins=eval_age_mins,
            author_followers=author_followers,
            reply_has_ancestors=reply_has_ancestors,
            replies_to_root=replies_to_root,
            self_reply_passed=self_reply_passed,
        ),
        metas,
    )


def synthesize_benchmark_trajectories(
    n_posts: int = 10_000,
    *,
    seed: int = 20261101,
    base_matrix: TrajectoryMatrix | None = None,
) -> TrajectoryMatrix:
    """Generates N=10,000 realistic contiguous float64 post trajectories for <2s speed benchmarking."""
    rng = np.random.default_rng(seed)
    n = max(1, int(n_posts))

    if base_matrix is not None and base_matrix.size > 0:
        idx = rng.integers(0, base_matrix.size, size=n)
        jitter = rng.uniform(0.88, 1.14, size=n)
        views_eval = np.maximum(40.0, np.round(base_matrix.views_at_eval[idx] * jitter))
        likes = np.round(base_matrix.likes[idx] * jitter)
        replies = np.round(base_matrix.replies[idx] * rng.uniform(0.8, 1.25, size=n))
        reposts = np.round(base_matrix.reposts[idx] * rng.uniform(0.8, 1.25, size=n))
        quotes = np.round(base_matrix.quotes[idx] * rng.uniform(0.75, 1.3, size=n))
        bookmarks = np.round(base_matrix.bookmarks[idx] * rng.uniform(0.8, 1.3, size=n))
        vel_z = base_matrix.vel_z[idx] + rng.normal(0.0, 0.22, size=n)
        seismic = np.clip(base_matrix.seismic[idx] + rng.normal(0.0, 0.04, size=n), 0.05, 0.99)
        cat_boost = np.maximum(0.0, base_matrix.cat_boost[idx] + rng.normal(0.0, 0.02, size=n))
        dpp_mult = np.clip(base_matrix.dpp_mult[idx] + rng.normal(0.0, 0.03, size=n), 0.15, 1.0)
        med_48h = base_matrix.author_median_48h_views[idx]
        floors = base_matrix.community_floor_views[idx]
        final_v = np.maximum(views_eval, np.round(base_matrix.actual_48h_views[idx] * jitter))
        y_true = ((final_v >= 3.0 * med_48h) & (final_v >= floors)).astype(np.float64)
        base_ts = float(np.min(base_matrix.created_ts))
        created_ts = base_ts + np.linspace(0.0, 90.0 * 86_400.0, n, dtype=np.float64)

        return TrajectoryMatrix(
            views_at_eval=np.ascontiguousarray(views_eval, dtype=np.float64),
            likes=np.ascontiguousarray(likes, dtype=np.float64),
            replies=np.ascontiguousarray(replies, dtype=np.float64),
            reposts=np.ascontiguousarray(reposts, dtype=np.float64),
            quotes=np.ascontiguousarray(quotes, dtype=np.float64),
            bookmarks=np.ascontiguousarray(bookmarks, dtype=np.float64),
            char_len=np.ascontiguousarray(base_matrix.char_len[idx], dtype=np.float64),
            line_breaks=np.ascontiguousarray(base_matrix.line_breaks[idx], dtype=np.float64),
            has_url=np.ascontiguousarray(base_matrix.has_url[idx], dtype=np.float64),
            is_photo=np.ascontiguousarray(base_matrix.is_photo[idx], dtype=np.float64),
            is_video=np.ascontiguousarray(base_matrix.is_video[idx], dtype=np.float64),
            video_dur_ms=np.ascontiguousarray(base_matrix.video_dur_ms[idx], dtype=np.float64),
            is_mutual=np.ascontiguousarray(base_matrix.is_mutual[idx], dtype=np.float64),
            in_network=np.ascontiguousarray(base_matrix.in_network[idx], dtype=np.float64),
            is_reply=np.ascontiguousarray(base_matrix.is_reply[idx], dtype=np.float64),
            is_retweet=np.ascontiguousarray(base_matrix.is_retweet[idx], dtype=np.float64),
            vel_z=np.ascontiguousarray(vel_z, dtype=np.float64),
            seismic=np.ascontiguousarray(seismic, dtype=np.float64),
            cat_boost=np.ascontiguousarray(cat_boost, dtype=np.float64),
            dpp_mult=np.ascontiguousarray(dpp_mult, dtype=np.float64),
            actual_48h_views=np.ascontiguousarray(final_v, dtype=np.float64),
            is_true_breakout_48h=np.ascontiguousarray(y_true, dtype=np.float64),
            post_ids=np.arange(1, n + 1, dtype=np.int64),
            author_ids=np.ascontiguousarray(base_matrix.author_ids[idx], dtype=np.int64),
            created_ts=np.ascontiguousarray(created_ts, dtype=np.float64),
            is_video_mp4=np.ascontiguousarray(base_matrix.is_video_mp4[idx], dtype=np.float64),
            author_median_48h_views=np.ascontiguousarray(med_48h, dtype=np.float64),
            community_floor_views=np.ascontiguousarray(floors, dtype=np.float64),
        )

    # Pure standalone 10k trajectory synthesis across the 4 canonical lifecycle archetypes
    archetype = rng.choice(4, size=n, p=[0.26, 0.10, 0.10, 0.54])
    early_mult = np.select(
        [archetype == 0, archetype == 1, archetype == 2, archetype == 3],
        [
            rng.uniform(3.4, 5.2, size=n),
            rng.uniform(2.5, 3.2, size=n),
            rng.uniform(1.3, 1.8, size=n),
            rng.uniform(0.7, 1.4, size=n),
        ],
    )
    final_mult = np.select(
        [archetype == 0, archetype == 1, archetype == 2, archetype == 3],
        [
            rng.uniform(3.2, 6.5, size=n),
            rng.uniform(1.3, 2.2, size=n),
            rng.uniform(3.1, 4.6, size=n),
            rng.uniform(0.8, 1.6, size=n),
        ],
    )
    med_48h = rng.uniform(3_500.0, 28_000.0, size=n)
    views_eval = np.maximum(60.0, np.round(med_48h * 0.18 * early_mult))
    final_v = np.maximum(views_eval, np.round(med_48h * final_mult))

    is_deep = (archetype == 0) | (archetype == 2)
    likes = np.round(views_eval * np.where(is_deep, 0.022, 0.014))
    replies = np.round(views_eval * np.where(is_deep, 0.0038, 0.0014))
    reposts = np.round(views_eval * np.where(is_deep, 0.0030, 0.0015))
    quotes = np.round(views_eval * np.where(is_deep, 0.0019, 0.0005))
    bookmarks = np.round(views_eval * np.where(is_deep, 0.0075, 0.0018))

    media_draw = rng.random(n)
    is_photo = (media_draw < 0.28).astype(np.float64)
    is_video = ((media_draw >= 0.28) & (media_draw < 0.42)).astype(np.float64)
    video_dur_ms = np.where(is_video > 0.5, np.round(rng.uniform(8_000.0, 38_000.0, size=n)), 0.0)
    has_url = (rng.random(n) < np.where(archetype == 1, 0.45, 0.12)).astype(np.float64)
    in_network = (rng.random(n) < 0.65).astype(np.float64)
    is_mutual = ((in_network > 0.5) & (rng.random(n) < 0.55)).astype(np.float64)
    is_reply = (rng.random(n) < 0.06).astype(np.float64)
    is_retweet = np.zeros(n, dtype=np.float64)

    vel_z = (early_mult - 1.0) * 1.5
    seismic = np.clip(0.15 + 0.18 * early_mult, 0.05, 0.98)
    cat_boost = np.where(archetype == 0, 0.25, 0.04)
    dpp_mult = rng.uniform(0.76, 0.99, size=n)
    floors = np.full(n, 5_000.0, dtype=np.float64)
    y_true = ((final_v >= 3.0 * med_48h) & (final_v >= floors)).astype(np.float64)

    return TrajectoryMatrix(
        views_at_eval=np.ascontiguousarray(views_eval, dtype=np.float64),
        likes=np.ascontiguousarray(likes, dtype=np.float64),
        replies=np.ascontiguousarray(replies, dtype=np.float64),
        reposts=np.ascontiguousarray(reposts, dtype=np.float64),
        quotes=np.ascontiguousarray(quotes, dtype=np.float64),
        bookmarks=np.ascontiguousarray(bookmarks, dtype=np.float64),
        char_len=np.ascontiguousarray(np.round(rng.uniform(90.0, 290.0, size=n)), dtype=np.float64),
        line_breaks=np.ascontiguousarray(rng.integers(0, 6, size=n), dtype=np.float64),
        has_url=np.ascontiguousarray(has_url, dtype=np.float64),
        is_photo=np.ascontiguousarray(is_photo, dtype=np.float64),
        is_video=np.ascontiguousarray(is_video, dtype=np.float64),
        video_dur_ms=np.ascontiguousarray(video_dur_ms, dtype=np.float64),
        is_mutual=np.ascontiguousarray(is_mutual, dtype=np.float64),
        in_network=np.ascontiguousarray(in_network, dtype=np.float64),
        is_reply=np.ascontiguousarray(is_reply, dtype=np.float64),
        is_retweet=np.ascontiguousarray(is_retweet, dtype=np.float64),
        vel_z=np.ascontiguousarray(vel_z, dtype=np.float64),
        seismic=np.ascontiguousarray(seismic, dtype=np.float64),
        cat_boost=np.ascontiguousarray(cat_boost, dtype=np.float64),
        dpp_mult=np.ascontiguousarray(dpp_mult, dtype=np.float64),
        actual_48h_views=np.ascontiguousarray(final_v, dtype=np.float64),
        is_true_breakout_48h=np.ascontiguousarray(y_true, dtype=np.float64),
        post_ids=np.arange(1, n + 1, dtype=np.int64),
        author_ids=np.ascontiguousarray(rng.integers(1, 65, size=n), dtype=np.int64),
        created_ts=np.linspace(1_760_000_000.0, 1_767_776_000.0, n, dtype=np.float64),
        is_video_mp4=np.ascontiguousarray(is_video, dtype=np.float64),
        author_median_48h_views=np.ascontiguousarray(med_48h, dtype=np.float64),
        community_floor_views=floors,
    )


def compute_param_diffs(
    prev_cfg: ResolvedAlgoConfig,
    cand_cfg: ResolvedAlgoConfig,
) -> list[dict[str, Any]]:
    """Computes human-readable BEFORE -> AFTER parameter diffs across weights, surrogates & thresholds."""
    diffs: list[dict[str, Any]] = []

    def _record(section: str, key: str, before: Any, after: Any) -> None:
        if isinstance(before, bool) or isinstance(after, bool):
            if bool(before) != bool(after):
                diffs.append(
                    {
                        "section": section,
                        "param": key,
                        "const_name": key.upper(),
                        "before": bool(before),
                        "after": bool(after),
                        "pct_change": None,
                        "formatted": f"{key.upper()}: {before} -> {after}",
                    }
                )
            return
        b_f = float(before)
        a_f = float(after)
        if abs(a_f - b_f) <= 1e-6:
            return
        pct = ((a_f - b_f) / abs(b_f) * 100.0) if abs(b_f) > 1e-9 else None
        pct_str = f" ({pct:+.1f}%)" if pct is not None else ""
        diffs.append(
            {
                "section": section,
                "param": key,
                "const_name": key.upper(),
                "before": round(b_f, 5),
                "after": round(a_f, 5),
                "pct_change": round(pct, 2) if pct is not None else None,
                "formatted": f"{key.upper()}: {b_f:.4g} -> {a_f:.4g}{pct_str}",
            }
        )

    for k, b_val in prev_cfg.weights.to_dict().items():
        _record("weights", k, b_val, getattr(cand_cfg.weights, k))
    for k, b_val in prev_cfg.surrogate.to_dict().items():
        _record("surrogate", k, b_val, getattr(cand_cfg.surrogate, k))
    for k, b_val in prev_cfg.pipeline.to_dict().items():
        _record("pipeline", k, b_val, getattr(cand_cfg.pipeline, k))
    for k, b_val in prev_cfg.fusion.to_dict().items():
        _record("fusion", k, b_val, getattr(cand_cfg.fusion, k))

    _record("thresholds", "watch_threshold", prev_cfg.watch_threshold, cand_cfg.watch_threshold)
    _record("thresholds", "surge_threshold", prev_cfg.surge_threshold, cand_cfg.surge_threshold)
    _record(
        "thresholds", "breakout_multiple", prev_cfg.breakout_multiple, cand_cfg.breakout_multiple
    )
    _record(
        "thresholds",
        "community_floor_views",
        prev_cfg.community_floor_views,
        cand_cfg.community_floor_views,
    )
    return diffs


def _has_floor_override(overrides: Mapping[str, Any] | None) -> bool:
    if not overrides:
        return False
    if overrides.get("community_floor_views") is not None:
        return True
    th = overrides.get("thresholds")
    return isinstance(th, Mapping) and th.get("community_floor_views") is not None


async def run_backtest_replay(
    session: AsyncSession,
    workspace_id: int,
    overrides: Mapping[str, Any] | None = None,
    *,
    benchmark_10k: bool = False,
    benchmark_size: int = 10_000,
) -> dict[str, Any]:
    """Runs vectorized Live vs Candidate replay on workspace trajectories (or 10k benchmark)."""
    ws = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one()
    active_ver = await get_active_algo_version(session, workspace_id)
    live_cfg = config_from_version(active_ver, ws)
    cand_cfg = apply_candidate_overrides(live_cfg, overrides)

    floor_overridden = _has_floor_override(overrides)
    recompute_gt = (
        abs(cand_cfg.breakout_multiple - live_cfg.breakout_multiple) > 1e-6 or floor_overridden
    )

    ws_matrix, ws_metas = await pack_workspace_trajectories(
        session, workspace_id, matured_only=True
    )

    if benchmark_10k:
        eval_matrix = synthesize_benchmark_trajectories(
            benchmark_size, seed=20261101, base_matrix=ws_matrix if ws_matrix.size > 0 else None
        )
    else:
        eval_matrix = ws_matrix

    # Pure vectorized replay wall-clock measurement across Live + Candidate
    t0 = time.perf_counter()
    live_out = vectorized_score_trajectories(eval_matrix, live_cfg, recompute_ground_truth=False)
    cand_out = vectorized_score_trajectories(
        eval_matrix,
        cand_cfg,
        recompute_ground_truth=recompute_gt,
        override_community_floor=floor_overridden,
    )
    elapsed_secs = time.perf_counter() - t0
    elapsed_ms = round(elapsed_secs * 1000.0, 3)

    # Also compute post-level diffs on actual stored workspace trajectories
    if benchmark_10k and ws_matrix.size > 0:
        ws_live_out = vectorized_score_trajectories(ws_matrix, live_cfg, recompute_ground_truth=False)
        ws_cand_out = vectorized_score_trajectories(
            ws_matrix,
            cand_cfg,
            recompute_ground_truth=recompute_gt,
            override_community_floor=floor_overridden,
        )
    else:
        ws_live_out = live_out
        ws_cand_out = cand_out

    y_bool = ws_cand_out.y_true >= 0.5
    l_pos = ws_live_out.watch_mask
    c_pos = ws_cand_out.watch_mask

    fn_to_tp_mask = (~l_pos) & c_pos & y_bool
    fp_to_tn_mask = l_pos & (~c_pos) & (~y_bool)
    tn_to_fp_mask = (~l_pos) & c_pos & (~y_bool)
    tp_to_fn_mask = l_pos & (~c_pos) & y_bool

    post_diffs: list[dict[str, Any]] = []
    for idx, meta in enumerate(ws_metas):
        if fn_to_tp_mask[idx]:
            transition = "fn_to_tp"
        elif fp_to_tn_mask[idx]:
            transition = "fp_to_tn"
        elif tn_to_fp_mask[idx]:
            transition = "tn_to_fp"
        elif tp_to_fn_mask[idx]:
            transition = "tp_to_fn"
        else:
            continue

        actual_v = int(ws_matrix.actual_48h_views[idx])
        med_v = max(1.0, float(ws_matrix.author_median_48h_views[idx]))
        post_diffs.append(
            {
                "post_id": meta.post_id,
                "x_post_id": meta.x_post_id,
                "x_status_url": f"https://x.com/{meta.author_handle}/status/{meta.x_post_id}",
                "author_handle": meta.author_handle,
                "author_display_name": meta.author_display_name,
                "communities": meta.communities,
                "text": meta.text,
                "transition": transition,
                "live_score": round(float(ws_live_out.breakout_scores[idx]), 2),
                "candidate_score": round(float(ws_cand_out.breakout_scores[idx]), 2),
                "delta_score": round(
                    float(ws_cand_out.breakout_scores[idx] - ws_live_out.breakout_scores[idx]), 2
                ),
                "live_probability": round(float(ws_live_out.breakout_probs[idx]), 4),
                "candidate_probability": round(float(ws_cand_out.breakout_probs[idx]), 4),
                "actual_48h_views": actual_v,
                "actual_48h_multiple": round(actual_v / med_v, 2),
                "is_true_breakout_48h": bool(y_bool[idx]),
            }
        )

    priority_order = {"fn_to_tp": 0, "fp_to_tn": 1, "tn_to_fp": 2, "tp_to_fn": 3}
    post_diffs.sort(key=lambda r: (priority_order.get(r["transition"], 9), -abs(r["delta_score"])))

    # Scale flip summary on eval_matrix as well
    eval_y = cand_out.y_true >= 0.5
    eval_l = live_out.watch_mask
    eval_c = cand_out.watch_mask
    flips_summary = {
        "fn_to_tp": int(np.sum((~eval_l) & eval_c & eval_y)),
        "fp_to_tn": int(np.sum(eval_l & (~eval_c) & (~eval_y))),
        "tn_to_fp": int(np.sum((~eval_l) & eval_c & (~eval_y))),
        "tp_to_fn": int(np.sum(eval_l & (~eval_c) & eval_y)),
    }

    lm = live_out.metrics
    cm = cand_out.metrics
    deltas = {
        "delta_precision": round(cm["precision"] - lm["precision"], 4),
        "delta_recall": round(cm["recall"] - lm["recall"], 4),
        "delta_f05": round(cm["f05_score"] - lm["f05_score"], 4),
        "delta_brier": round(cm["brier_score"] - lm["brier_score"], 5),
        "delta_alerts_total": int(cm["alerts_total"] - lm["alerts_total"]),
        "delta_alerts_per_day": round(cm["alerts_per_day"] - lm["alerts_per_day"], 2),
    }

    throughput = (
        round(eval_matrix.size / max(elapsed_secs, 1e-9), 1) if eval_matrix.size > 0 else 0.0
    )

    return {
        "workspace_id": workspace_id,
        "benchmark_10k": benchmark_10k,
        "trajectories_evaluated": eval_matrix.size,
        "stored_trajectories_count": ws_matrix.size,
        "elapsed_ms": elapsed_ms,
        "elapsed_secs": round(elapsed_secs, 6),
        "throughput_posts_per_sec": throughput,
        "active_version": {
            "id": live_cfg.version_id,
            "version_number": live_cfg.version_number,
            "label": live_cfg.label,
            "source": live_cfg.source,
            "upstream_commit_sha": live_cfg.upstream_commit_sha,
        },
        "live_metrics": lm,
        "candidate_metrics": cm,
        "deltas": deltas,
        "flips_summary": flips_summary,
        "param_diffs": compute_param_diffs(live_cfg, cand_cfg),
        "post_diffs": post_diffs[:100],
    }


async def promote_candidate_params(
    session: AsyncSession,
    workspace_id: int,
    overrides: Mapping[str, Any],
    *,
    label: str | None = None,
    changelog_note: str | None = None,
) -> dict[str, Any]:
    """Promotes sandbox candidate parameters to a new active AlgoParamVersion(source='manual_sandbox')."""
    ws = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one()
    prev_ver = await get_active_algo_version(session, workspace_id)
    live_cfg = config_from_version(prev_ver, ws)
    cand_cfg = apply_candidate_overrides(live_cfg, overrides)

    ws_matrix, _ = await pack_workspace_trajectories(session, workspace_id, matured_only=True)
    cand_out = vectorized_score_trajectories(ws_matrix, cand_cfg, recompute_ground_truth=False)
    live_out = vectorized_score_trajectories(ws_matrix, live_cfg, recompute_ground_truth=False)

    param_diffs = compute_param_diffs(live_cfg, cand_cfg)

    all_versions = list(
        (
            await session.execute(
                select(AlgoParamVersion).where(
                    (AlgoParamVersion.workspace_id == workspace_id)
                    | (AlgoParamVersion.workspace_id.is_(None))
                )
            )
        )
        .scalars()
        .all()
    )
    next_num = max((v.version_number for v in all_versions), default=0) + 1

    # Deactivate prior workspace-scoped active versions while preserving global baseline for other workspaces
    for v in all_versions:
        if v.workspace_id == workspace_id and v.is_active:
            v.is_active = False

    diff_summary = ", ".join(d["formatted"] for d in param_diffs[:6])
    if len(param_diffs) > 6:
        diff_summary += f" (+{len(param_diffs) - 6} more)"
    auto_note = (
        f"Promoted from Backtest Sandbox ({diff_summary or 'no parameter delta'}). "
        f"Replay F0.5 {live_out.metrics['f05_score']:.3f} -> {cand_out.metrics['f05_score']:.3f}, "
        f"Brier {live_out.metrics['brier_score']:.4f} -> {cand_out.metrics['brier_score']:.4f}."
    )

    new_ver = AlgoParamVersion(
        workspace_id=workspace_id,
        version_number=next_num,
        label=(label or "").strip() or f"v{next_num} · Sandbox Promotion",
        source="manual_sandbox",
        upstream_commit_sha=cand_cfg.upstream_commit_sha,
        weights_json=cand_cfg.weights.to_dict(),
        surrogate_params_json=cand_cfg.surrogate.to_dict(),
        thresholds_json={
            "watch_threshold": cand_cfg.watch_threshold,
            "surge_threshold": cand_cfg.surge_threshold,
            "breakout_multiple": cand_cfg.breakout_multiple,
            "community_floor_views": cand_cfg.community_floor_views,
            "community_floors": cand_cfg.community_floors,
            "pipeline": cand_cfg.pipeline.to_dict(),
            "fusion": cand_cfg.fusion.to_dict(),
            "parent_version_id": prev_ver.id if prev_ver else None,
        },
        holdout_metrics_json={
            "precision": cand_out.metrics["precision"],
            "recall": cand_out.metrics["recall"],
            "f05": cand_out.metrics["f05_score"],
            "brier": cand_out.metrics["brier_score"],
            "matured_posts": ws_matrix.size,
            "param_diffs": param_diffs,
        },
        changelog_note=(changelog_note or "").strip() or auto_note,
        is_active=True,
        created_at=utc_now(),
    )
    session.add(new_ver)

    ws.watch_threshold = cand_cfg.watch_threshold
    ws.surge_threshold = cand_cfg.surge_threshold
    ws.updated_at = utc_now()

    await session.commit()
    await session.refresh(new_ver)

    return {
        "promoted": True,
        "version": {
            "id": new_ver.id,
            "workspace_id": new_ver.workspace_id,
            "version_number": new_ver.version_number,
            "label": new_ver.label,
            "source": new_ver.source,
            "upstream_commit_sha": new_ver.upstream_commit_sha,
            "is_active": new_ver.is_active,
            "changelog_note": new_ver.changelog_note,
            "holdout_metrics": new_ver.holdout_metrics_json,
            "created_at": ensure_utc(new_ver.created_at).isoformat(),
        },
        "param_diffs": param_diffs,
    }
