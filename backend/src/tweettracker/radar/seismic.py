"""Multi-Signal SEISMIC Hawkes Self-Exciting Cascade Infectiousness Estimator."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any

# Multi-signal Hawkes cascade event weights per Epic 2 specification:
QUOTE_CASCADE_WEIGHT: float = 3.0
BOOKMARK_CASCADE_WEIGHT: float = 2.5
REPLY_CASCADE_WEIGHT: float = 2.0
REPOST_CASCADE_WEIGHT: float = 1.5
LIKE_CASCADE_WEIGHT: float = 0.5

# Exponential memory kernel half-life (minutes) across early 15-60m snapshots
DEFAULT_KERNEL_HALF_LIFE_MINUTES: float = 30.0

# Empirical-Bayes regularization prior so low-impression noise cannot saturate p_t
DEFAULT_PRIOR_EXPOSURE_VIEWS: float = 400.0
DEFAULT_PRIOR_CASCADE_RATE: float = 0.0175
CRITICAL_BRANCHING_RATE: float = 0.055


@dataclass(slots=True, frozen=True)
class CascadeSnapshotPoint:
    """Single cumulative observation of post exposure and multi-signal cascade counts."""

    age_minutes: float
    views: float
    likes: float = 0.0
    replies: float = 0.0
    reposts: float = 0.0
    quotes: float = 0.0
    bookmarks: float = 0.0


@dataclass(slots=True, frozen=True)
class SeismicEstimate:
    """Detailed SEISMIC Hawkes cascade estimate (`infectiousness` p_t in [0, 1])."""

    infectiousness: float
    weighted_cascade_events: float
    decayed_cascade_events: float
    decayed_exposure_views: float
    regularized_rate_per_view: float
    cascade_velocity_per_min: float
    acceleration_factor: float
    signal_breakdown: dict[str, float]


def compute_weighted_cascade_events(
    *,
    likes: int | float = 0,
    replies: int | float = 0,
    reposts: int | float = 0,
    quotes: int | float = 0,
    bookmarks: int | float = 0,
) -> tuple[float, dict[str, float]]:
    """Computes `quotes*3.0 + bookmarks*2.5 + replies*2.0 + reposts*1.5 + likes*0.5`."""
    q_term = max(float(quotes), 0.0) * QUOTE_CASCADE_WEIGHT
    b_term = max(float(bookmarks), 0.0) * BOOKMARK_CASCADE_WEIGHT
    r_term = max(float(replies), 0.0) * REPLY_CASCADE_WEIGHT
    rt_term = max(float(reposts), 0.0) * REPOST_CASCADE_WEIGHT
    l_term = max(float(likes), 0.0) * LIKE_CASCADE_WEIGHT
    total = q_term + b_term + r_term + rt_term + l_term
    return total, {
        "quotes": round(q_term, 3),
        "bookmarks": round(b_term, 3),
        "replies": round(r_term, 3),
        "reposts": round(rt_term, 3),
        "likes": round(l_term, 3),
    }


def _coerce_snapshot_point(item: Any) -> CascadeSnapshotPoint | None:
    if isinstance(item, CascadeSnapshotPoint):
        return item
    if isinstance(item, Mapping):
        if "age_minutes" not in item:
            return None
        return CascadeSnapshotPoint(
            age_minutes=max(float(item.get("age_minutes", 1.0)), 0.5),
            views=max(float(item.get("views", 0.0)), 0.0),
            likes=max(float(item.get("likes", 0.0)), 0.0),
            replies=max(float(item.get("replies", 0.0)), 0.0),
            reposts=max(float(item.get("reposts", 0.0)), 0.0),
            quotes=max(float(item.get("quotes", 0.0)), 0.0),
            bookmarks=max(float(item.get("bookmarks", 0.0)), 0.0),
        )
    if hasattr(item, "age_minutes") and hasattr(item, "views"):
        return CascadeSnapshotPoint(
            age_minutes=max(float(item.age_minutes), 0.5),
            views=max(float(item.views), 0.0),
            likes=max(float(getattr(item, "likes", 0) or 0), 0.0),
            replies=max(float(getattr(item, "replies", 0) or 0), 0.0),
            reposts=max(float(getattr(item, "reposts", 0) or 0), 0.0),
            quotes=max(float(getattr(item, "quotes", 0) or 0), 0.0),
            bookmarks=max(float(getattr(item, "bookmarks", 0) or 0), 0.0),
        )
    return None


def estimate_seismic(
    *,
    age_minutes: float,
    views: int | float,
    likes: int | float = 0,
    replies: int | float = 0,
    reposts: int | float = 0,
    quotes: int | float = 0,
    bookmarks: int | float = 0,
    snapshots: Sequence[Any] | None = None,
    kernel_half_life_minutes: float = DEFAULT_KERNEL_HALF_LIFE_MINUTES,
    prior_exposure_views: float = DEFAULT_PRIOR_EXPOSURE_VIEWS,
    prior_cascade_rate: float = DEFAULT_PRIOR_CASCADE_RATE,
) -> SeismicEstimate:
    """Estimates time-decayed Hawkes/SEISMIC self-exciting cascade infectiousness `p_t in [0, 1]`
    across early trajectory snapshots (`15m, 30m, 45m, 60m`) and cumulative exposure rate,
    regularized with a conjugate Empirical-Bayes exposure prior so tiny-view noise does not
    saturate `p_t`.
    """
    eval_age = max(float(age_minutes), 1.0)
    cur_views = max(float(views), 0.0)
    total_weighted, breakdown = compute_weighted_cascade_events(
        likes=likes,
        replies=replies,
        reposts=reposts,
        quotes=quotes,
        bookmarks=bookmarks,
    )

    points: list[CascadeSnapshotPoint] = []
    if snapshots:
        for raw_s in snapshots:
            pt = _coerce_snapshot_point(raw_s)
            if pt is not None and pt.age_minutes <= eval_age + 1e-6:
                points.append(pt)
    points.sort(key=lambda p: p.age_minutes)

    # Deduplicate same-age snapshots keeping the latest/largest observation
    deduped: list[CascadeSnapshotPoint] = []
    for pt in points:
        if deduped and abs(deduped[-1].age_minutes - pt.age_minutes) < 0.5:
            deduped[-1] = pt
        else:
            deduped.append(pt)

    current_point = CascadeSnapshotPoint(
        age_minutes=eval_age,
        views=cur_views,
        likes=max(float(likes), 0.0),
        replies=max(float(replies), 0.0),
        reposts=max(float(reposts), 0.0),
        quotes=max(float(quotes), 0.0),
        bookmarks=max(float(bookmarks), 0.0),
    )
    if not deduped:
        deduped.append(current_point)
    elif eval_age - deduped[-1].age_minutes >= 0.5:
        deduped.append(current_point)
    else:
        # Merge latest state at current age
        last = deduped[-1]
        deduped[-1] = CascadeSnapshotPoint(
            age_minutes=max(last.age_minutes, eval_age),
            views=max(last.views, cur_views),
            likes=max(last.likes, current_point.likes),
            replies=max(last.replies, current_point.replies),
            reposts=max(last.reposts, current_point.reposts),
            quotes=max(last.quotes, current_point.quotes),
            bookmarks=max(last.bookmarks, current_point.bookmarks),
        )

    decay_lambda = math.log(2.0) / max(float(kernel_half_life_minutes), 1.0)

    prev_t = 0.0
    prev_views = 0.0
    prev_mass = 0.0

    decayed_events = 0.0
    decayed_views = 0.0
    decayed_minutes = 0.0
    interval_velocities: list[float] = []

    for pt in deduped:
        dt = max(pt.age_minutes - prev_t, 0.5)
        pt_mass, _ = compute_weighted_cascade_events(
            likes=pt.likes,
            replies=pt.replies,
            reposts=pt.reposts,
            quotes=pt.quotes,
            bookmarks=pt.bookmarks,
        )
        # Ensure cumulative monotonicity across snapshot intervals
        eff_views = max(pt.views, prev_views)
        eff_mass = max(pt_mass, prev_mass)

        d_views = max(0.0, eff_views - prev_views)
        d_mass = max(0.0, eff_mass - prev_mass)

        t_mid = 0.5 * (prev_t + pt.age_minutes)
        lag = max(0.0, eval_age - t_mid)
        kernel_w = math.exp(-decay_lambda * lag)

        decayed_events += kernel_w * d_mass
        decayed_views += kernel_w * d_views
        decayed_minutes += kernel_w * dt
        interval_velocities.append(d_mass / dt)

        prev_t = pt.age_minutes
        prev_views = eff_views
        prev_mass = eff_mass

    # Bayesian-shrunk cascade mass per impression
    prior_v = max(float(prior_exposure_views), 1.0)
    prior_r = min(max(float(prior_cascade_rate), 1e-5), 0.5)
    reg_rate = (decayed_events + prior_v * prior_r) / (decayed_views + prior_v)

    # Regularized arrival velocity (weighted cascade events per minute)
    cascade_vel = decayed_events / (decayed_minutes + 20.0)

    # Trajectory acceleration across early intervals (e.g. 15m -> 30m -> 45m -> 60m)
    accel_factor = 1.0
    if len(interval_velocities) >= 2:
        mid_idx = max(1, len(interval_velocities) // 2)
        early_v = sum(interval_velocities[:mid_idx]) / float(mid_idx)
        recent_v = sum(interval_velocities[mid_idx:]) / float(len(interval_velocities) - mid_idx)
        accel_factor = 1.0 + 0.20 * math.tanh((recent_v - early_v) / max(early_v + 1.0, 1.0))

    # Effective Hawkes reproduction index mapped smoothly into [0, 1]
    intensity_factor = 0.65 + 0.35 * (cascade_vel / (cascade_vel + 2.5))
    r_eff = (reg_rate / CRITICAL_BRANCHING_RATE) * intensity_factor * accel_factor
    p_t = min(max(r_eff / (0.85 + r_eff), 0.0), 1.0)

    return SeismicEstimate(
        infectiousness=round(p_t, 4),
        weighted_cascade_events=round(max(total_weighted, prev_mass), 3),
        decayed_cascade_events=round(decayed_events, 3),
        decayed_exposure_views=round(decayed_views, 3),
        regularized_rate_per_view=round(reg_rate, 6),
        cascade_velocity_per_min=round(cascade_vel, 4),
        acceleration_factor=round(accel_factor, 4),
        signal_breakdown=breakdown,
    )


def estimate_seismic_infectiousness(
    *,
    age_minutes: float,
    views: int | float,
    likes: int | float = 0,
    replies: int | float = 0,
    reposts: int | float = 0,
    quotes: int | float = 0,
    bookmarks: int | float = 0,
    snapshots: Sequence[Any] | None = None,
) -> float:
    """Convenience wrapper returning `p_t in [0, 1]`."""
    return estimate_seismic(
        age_minutes=age_minutes,
        views=views,
        likes=likes,
        replies=replies,
        reposts=reposts,
        quotes=quotes,
        bookmarks=bookmarks,
        snapshots=snapshots,
    ).infectiousness
