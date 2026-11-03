"""Author Age-Matched Velocity Z-Score & Quantile Curve Interpolation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

BASELINE_AGES: tuple[int, ...] = (15, 30, 45, 60, 120, 360, 1440, 2880)

DEFAULT_AGE_FRACTIONS: dict[int, float] = {
    15: 0.09,
    30: 0.17,
    45: 0.25,
    60: 0.33,
    120: 0.50,
    360: 0.72,
    1440: 0.91,
    2880: 1.00,
}

# Normal distribution interquartile range in standard deviations: Phi^-1(0.75) - Phi^-1(0.25)
IQR_TO_SIGMA: float = 1.3489795


@dataclass(slots=True, frozen=True)
class InterpolatedBaseline:
    """Interpolated author view quantiles at exact post age `t` (minutes)."""

    age_minutes: float
    p25: float
    p50: float
    p75: float
    p90: float


@dataclass(slots=True, frozen=True)
class VelocityMetrics:
    """Age-matched velocity multiple and robust IQR Z-score against an author's baseline."""

    age_minutes: float
    views: float
    expected_p25: float
    expected_p50: float
    expected_p75: float
    expected_p90: float
    velocity_multiple: float
    iqr_sigma: float
    velocity_z_score: float


def default_baseline_curves(median_48h_views: int = 2_000) -> dict[str, dict[str, int]]:
    """Constructs fallback P25/P50/P75/P90 baseline curves across 15m..48h."""
    anchor = max(120, int(median_48h_views))
    curves: dict[str, dict[str, int]] = {"p25": {}, "p50": {}, "p75": {}, "p90": {}}
    for age, frac in DEFAULT_AGE_FRACTIONS.items():
        key = str(age)
        p50 = max(40, int(round(anchor * frac)))
        curves["p25"][key] = max(20, int(round(p50 * 0.58)))
        curves["p50"][key] = p50
        curves["p75"][key] = max(p50 + 1, int(round(p50 * 1.55)))
        curves["p90"][key] = max(curves["p75"][key] + 1, int(round(p50 * 2.45)))
    return curves


def _extract_quantile_knots(
    baseline_curves: Mapping[str, Any] | None,
    quantile_key: str,
    fallback_curves: Mapping[str, Mapping[str, int]],
) -> list[float]:
    raw_q = baseline_curves.get(quantile_key) if isinstance(baseline_curves, Mapping) else None
    knots: list[float] = []
    for age in BASELINE_AGES:
        val: Any = None
        if isinstance(raw_q, Mapping):
            val = raw_q.get(str(age))
            if val is None:
                val = raw_q.get(age)  # type: ignore[arg-type]
        if val is None:
            val = fallback_curves[quantile_key][str(age)]
        knots.append(max(1.0, float(val)))
    return knots


def _interpolate_piecewise(age_minutes: float, knots: Sequence[float]) -> float:
    t = max(float(age_minutes), 1.0)
    if t <= float(BASELINE_AGES[0]):
        if t == float(BASELINE_AGES[0]):
            return float(knots[0])
        return max(1.0, float(knots[0]) * (t / float(BASELINE_AGES[0])))

    if t >= float(BASELINE_AGES[-1]):
        return float(knots[-1])

    for idx in range(len(BASELINE_AGES) - 1):
        t0 = float(BASELINE_AGES[idx])
        t1 = float(BASELINE_AGES[idx + 1])
        if t0 <= t <= t1:
            if t == t0:
                return float(knots[idx])
            if t == t1:
                return float(knots[idx + 1])
            alpha = (t - t0) / (t1 - t0)
            return float(knots[idx]) + alpha * (float(knots[idx + 1]) - float(knots[idx]))

    return float(knots[-1])


def interpolate_baseline_quantiles(
    baseline_curves: Mapping[str, Any] | None,
    age_minutes: float,
    *,
    followers_fallback: int = 10_000,
) -> InterpolatedBaseline:
    """Interpolates an author's P25/P50/P75/P90 curves at exact age `t` (minutes)."""
    fallback_48h = max(400, int(max(followers_fallback, 100) * 0.18))
    fallback = default_baseline_curves(fallback_48h)

    k_p25 = _extract_quantile_knots(baseline_curves, "p25", fallback)
    k_p50 = _extract_quantile_knots(baseline_curves, "p50", fallback)
    k_p75 = _extract_quantile_knots(baseline_curves, "p75", fallback)
    k_p90 = _extract_quantile_knots(baseline_curves, "p90", fallback)

    p25 = _interpolate_piecewise(age_minutes, k_p25)
    p50 = max(p25, _interpolate_piecewise(age_minutes, k_p50))
    p75 = max(p50, _interpolate_piecewise(age_minutes, k_p75))
    p90 = max(p75, _interpolate_piecewise(age_minutes, k_p90))

    return InterpolatedBaseline(
        age_minutes=max(float(age_minutes), 1.0),
        p25=p25,
        p50=p50,
        p75=p75,
        p90=p90,
    )


def compute_velocity_metrics(
    views: int | float,
    age_minutes: float,
    baseline_curves: Mapping[str, Any] | None = None,
    *,
    followers_fallback: int = 10_000,
) -> VelocityMetrics:
    """Computes author age-matched velocity multiple (`views / p50(t)`) and robust IQR Z-score
    `(views - p50(t)) / max(iqr_sigma(t), 1.0)` so early breakouts are normalized to that
    specific author's empirical baseline trajectory rather than raw follower count.
    """
    interp = interpolate_baseline_quantiles(
        baseline_curves,
        age_minutes,
        followers_fallback=followers_fallback,
    )
    clean_views = max(float(views), 0.0)
    expected_p50 = max(interp.p50, 1.0)
    velocity_multiple = clean_views / expected_p50

    iqr = max(interp.p75 - interp.p25, 0.0)
    iqr_sigma = max(iqr / IQR_TO_SIGMA, expected_p50 * 0.15, 1.0)
    raw_z = (clean_views - expected_p50) / max(iqr_sigma, 1.0)
    clamped_z = min(max(raw_z, -5.0), 25.0)

    return VelocityMetrics(
        age_minutes=interp.age_minutes,
        views=clean_views,
        expected_p25=round(interp.p25, 2),
        expected_p50=round(expected_p50, 2),
        expected_p75=round(interp.p75, 2),
        expected_p90=round(interp.p90, 2),
        velocity_multiple=round(velocity_multiple, 4),
        iqr_sigma=round(iqr_sigma, 4),
        velocity_z_score=round(clamped_z, 4),
    )


def _nearest_baseline_bucket(age_minutes: float) -> int:
    return min(BASELINE_AGES, key=lambda b: abs(float(b) - float(age_minutes)))


def rebuild_author_baseline_curves(
    posts_or_snapshots: Sequence[Any],
    *,
    fallback_median_48h: int = 2_000,
) -> dict[str, dict[str, int]]:
    """Rebuilds empirical P25/P50/P75/P90 view curves across 15m..48h from stored
    `PostSnapshot`s, `Post` objects, dicts (`{age_minutes, views}`), or `(age, views)` pairs.
    """
    bucket_samples: dict[int, list[float]] = {age: [] for age in BASELINE_AGES}
    implied_48h_samples: list[float] = []

    def _record_observation(age_val: float, views_val: float) -> None:
        if views_val <= 0:
            return
        bucket = _nearest_baseline_bucket(age_val)
        bucket_samples[bucket].append(float(views_val))
        frac = DEFAULT_AGE_FRACTIONS[bucket]
        if frac > 0:
            implied_48h_samples.append(float(views_val) / frac)

    for item in posts_or_snapshots:
        if item is None:
            continue
        if isinstance(item, tuple) and len(item) >= 2:
            _record_observation(float(item[0]), float(item[1]))
            continue
        if isinstance(item, Mapping):
            if "age_minutes" in item and "views" in item:
                _record_observation(float(item["age_minutes"]), float(item["views"]))
            elif "actual_48h_views" in item and item["actual_48h_views"] is not None:
                _record_observation(2880.0, float(item["actual_48h_views"]))
            elif "latest_views" in item:
                _record_observation(float(item.get("age_minutes", 2880.0)), float(item["latest_views"]))
            continue

        # PostSnapshot-like object
        if hasattr(item, "age_minutes") and hasattr(item, "views"):
            _record_observation(float(item.age_minutes), float(item.views))
            continue

        # Post-like object
        snaps = getattr(item, "snapshots", None)
        if snaps:
            for snap in snaps:
                if hasattr(snap, "age_minutes") and hasattr(snap, "views"):
                    _record_observation(float(snap.age_minutes), float(snap.views))
            continue

        actual_48h = getattr(item, "actual_48h_views", None)
        if actual_48h is not None:
            _record_observation(2880.0, float(actual_48h))
        elif hasattr(item, "latest_views"):
            _record_observation(2880.0, float(item.latest_views))

    if implied_48h_samples:
        anchor_48h = max(120, int(round(float(np.median(implied_48h_samples)))))
    else:
        anchor_48h = max(120, int(fallback_median_48h))

    base = default_baseline_curves(anchor_48h)
    curves: dict[str, dict[str, int]] = {"p25": {}, "p50": {}, "p75": {}, "p90": {}}

    prev_p25, prev_p50, prev_p75, prev_p90 = 1, 1, 2, 3
    for age in BASELINE_AGES:
        key = str(age)
        samples = bucket_samples[age]
        if len(samples) >= 3:
            q25, q50, q75, q90 = np.percentile(np.asarray(samples, dtype=np.float64), [25, 50, 75, 90])
            p25 = max(prev_p25, int(round(float(q25))))
            p50 = max(prev_p50, p25, int(round(float(q50))))
            p75 = max(prev_p75, p50 + 1, int(round(float(q75))))
            p90 = max(prev_p90, p75 + 1, int(round(float(q90))))
        elif len(samples) > 0:
            emp_med = float(np.median(samples))
            blended_p50 = 0.5 * emp_med + 0.5 * float(base["p50"][key])
            p50 = max(prev_p50, 20, int(round(blended_p50)))
            p25 = max(prev_p25, int(round(p50 * 0.58)))
            p25 = min(p25, p50)
            p75 = max(prev_p75, p50 + 1, int(round(p50 * 1.55)))
            p90 = max(prev_p90, p75 + 1, int(round(p50 * 2.45)))
        else:
            p25 = max(prev_p25, base["p25"][key])
            p50 = max(prev_p50, p25, base["p50"][key])
            p75 = max(prev_p75, p50 + 1, base["p75"][key])
            p90 = max(prev_p90, p75 + 1, base["p90"][key])

        curves["p25"][key] = p25
        curves["p50"][key] = p50
        curves["p75"][key] = p75
        curves["p90"][key] = p90
        prev_p25, prev_p50, prev_p75, prev_p90 = p25, p50, p75, p90

    return curves


def project_48h_views(
    views: int | float,
    age_minutes: float,
    baseline_curves: Mapping[str, Any] | None = None,
    *,
    followers_fallback: int = 10_000,
) -> float:
    """Projects a post's 48h views as current views x the author's typical remaining growth
    (`p50(48h) / p50(t)`): a post running at any pace keeps that pace along the author's own
    curve. Absolute reach, unlike the velocity multiple, says how many people a reply can reach.
    """
    clean_views = max(float(views), 0.0)
    now_p50 = max(
        interpolate_baseline_quantiles(
            baseline_curves, age_minutes, followers_fallback=followers_fallback
        ).p50,
        1.0,
    )
    p50_48h = interpolate_baseline_quantiles(
        baseline_curves, 2880.0, followers_fallback=followers_fallback
    ).p50
    return max(clean_views, clean_views * p50_48h / now_p50)
