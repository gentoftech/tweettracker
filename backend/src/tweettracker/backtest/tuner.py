"""Holdout-Gated L2-Regularized Auto-Tuner, Changelog & 1-Click Rollback (Story 3.2)."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

import numpy as np
from scipy.optimize import minimize
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.algo import PipelineParams, SurrogateCalibrationParams, ValueModelWeights
from tweettracker.backtest.grader import grade_workspace_outcomes
from tweettracker.backtest.replay import (
    FusionCalibrationParams,
    ResolvedAlgoConfig,
    compute_param_diffs,
    config_from_version,
    ensure_utc,
    get_active_algo_version,
    pack_workspace_trajectories,
    vectorized_score_trajectories,
)
from tweettracker.models import AlgoParamVersion, Workspace, utc_now

# Tunable parameter specs: (section, field_name, domain_min, domain_max)
TUNABLE_SPECS: tuple[tuple[str, str, float, float], ...] = (
    ("surrogate", "copy_link_from_bookmark_mult", 0.05, 1.0),
    ("surrogate", "copy_link_from_quote_mult", 0.05, 1.0),
    ("surrogate", "dm_share_from_bookmark_mult", 0.05, 1.0),
    ("surrogate", "share_from_bookmark_mult", 0.05, 1.0),
    ("surrogate", "url_dwell_discount", 0.20, 1.0),
    ("surrogate", "prior_views_alpha0", 25.0, 2_000.0),
    ("weights", "share_via_copy_link", 1.0, 100.0),
    ("weights", "bidirectional_follow_reply_weight_boost", 1.0, 60.0),
    ("weights", "quote", 0.5, 40.0),
    ("weights", "reply", 0.5, 40.0),
    ("weights", "not_interested", -500.0, -1.0),
    ("fusion", "vel_weight", 2.0, 35.0),
    ("fusion", "catalysis_weight", 2.0, 45.0),
    ("fusion", "vm_weight", 4.0, 60.0),
    ("fusion", "prob_midpoint", 25.0, 92.0),
    ("fusion", "prob_scale", 2.0, 25.0),
)

# Breakout scores live in [0, 100]: a watch/surge threshold above these can never fire, so the
# tuner clamps to them no matter how far the drift-capped search wanders.
MAX_WATCH_THRESHOLD: float = 95.0
MAX_SURGE_THRESHOLD: float = 99.0
# Fewer true breakouts than this gives the watch-threshold search almost no positives to learn
# from (2026-11-03: 4 of 38 matured posts; the bar ratcheted 65 -> 93.65 at 0.0 precision/recall).
MIN_TRUE_BREAKOUTS_FOR_TUNING: int = 10


def cap_alert_thresholds(watch: float, surge: float) -> tuple[float, float]:
    """Clamps thresholds under the score ceiling while keeping surge strictly above watch."""
    capped_watch = round(min(float(watch), MAX_WATCH_THRESHOLD), 2)
    capped_surge = round(min(max(float(surge), capped_watch + 1.0), MAX_SURGE_THRESHOLD), 2)
    return capped_watch, capped_surge


def clamp_with_drift_cap(
    theta_new: float,
    theta_prev: float,
    max_drift_pct: float = 0.20,
) -> float:
    """Clamps theta_new strictly inside [1 - drift, 1 + drift] * theta_prev (sign-safe)."""
    cap = max(0.0, float(max_drift_pct))
    b1 = (1.0 - cap) * float(theta_prev)
    b2 = (1.0 + cap) * float(theta_prev)
    lo = min(b1, b2)
    hi = max(b1, b2)
    return float(np.clip(float(theta_new), lo, hi))


def _get_param_val(cfg: ResolvedAlgoConfig, section: str, name: str) -> float:
    if section == "weights":
        return float(getattr(cfg.weights, name))
    if section == "surrogate":
        return float(getattr(cfg.surrogate, name))
    if section == "fusion":
        return float(getattr(cfg.fusion, name))
    if section == "pipeline":
        return float(getattr(cfg.pipeline, name))
    raise ValueError(f"Unknown parameter section: {section}")


def _build_cfg_from_vector(
    base_cfg: ResolvedAlgoConfig,
    u_vec: np.ndarray,
    max_drift_pct: float,
) -> ResolvedAlgoConfig:
    w_dict = base_cfg.weights.to_dict()
    s_dict = base_cfg.surrogate.to_dict()
    f_dict = base_cfg.fusion.to_dict()

    for j, (section, name, dom_lo, dom_hi) in enumerate(TUNABLE_SPECS):
        prev_v = _get_param_val(base_cfg, section, name)
        raw_v = prev_v + float(u_vec[j]) * abs(prev_v)
        drift_clamped = clamp_with_drift_cap(raw_v, prev_v, max_drift_pct)
        # Intersect with physical domain bounds without ever violating drift bounds
        b1 = (1.0 - max_drift_pct) * prev_v
        b2 = (1.0 + max_drift_pct) * prev_v
        d_lo, d_hi = min(b1, b2), max(b1, b2)
        eff_lo = max(dom_lo, d_lo)
        eff_hi = min(dom_hi, d_hi)
        if eff_lo <= eff_hi:
            final_v = float(np.clip(drift_clamped, eff_lo, eff_hi))
        else:
            final_v = drift_clamped

        if section == "weights":
            w_dict[name] = final_v
        elif section == "surrogate":
            s_dict[name] = final_v
        elif section == "fusion":
            f_dict[name] = final_v

    return ResolvedAlgoConfig(
        version_id=None,
        version_number=base_cfg.version_number + 1,
        label=base_cfg.label,
        source="auto_tuner",
        upstream_commit_sha=base_cfg.upstream_commit_sha,
        weights=ValueModelWeights.from_dict(w_dict),
        surrogate=SurrogateCalibrationParams.from_dict(s_dict),
        pipeline=PipelineParams.from_dict(base_cfg.pipeline.to_dict()),
        fusion=FusionCalibrationParams.from_dict(f_dict),
        watch_threshold=base_cfg.watch_threshold,
        surge_threshold=base_cfg.surge_threshold,
        breakout_multiple=base_cfg.breakout_multiple,
        community_floor_views=base_cfg.community_floor_views,
        community_floors=dict(base_cfg.community_floors),
    )


async def run_auto_calibration(
    session: AsyncSession,
    workspace_id: int,
    *,
    min_matured_posts: int = 30,
    l2_reg: float = 0.25,
    max_drift_pct: float = 0.20,
    force_stage_only: bool = False,
) -> dict[str, Any]:
    """Fits chronological-holdout-gated L2-regularized calibration with strict +-20% drift cap."""
    await grade_workspace_outcomes(session, workspace_id)

    ws = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one()
    prev_ver = await get_active_algo_version(session, workspace_id)
    live_cfg = config_from_version(prev_ver, ws)
    prior_cfg = config_from_version(None, ws)

    matrix, _ = await pack_workspace_trajectories(session, workspace_id, matured_only=True)
    n_total = matrix.size
    req_min = max(5, int(min_matured_posts))
    drift_cap = min(max(float(max_drift_pct), 0.01), 0.20)

    if n_total < req_min:
        return {
            "status": "insufficient_data",
            "promoted": False,
            "gate_passed": False,
            "reason": (
                f"Workspace has {n_total} matured 48h trajectories; at least "
                f"{req_min} are required before running auto-calibration."
            ),
            "matured_posts_count": n_total,
            "min_matured_posts": req_min,
            "max_drift_pct": drift_cap,
            "split": None,
            "live_holdout_metrics": None,
            "candidate_holdout_metrics": None,
            "param_diffs": [],
            "changelog_note": "",
            "version": None,
        }

    n_true_breakouts = int(np.sum(matrix.is_true_breakout_48h >= 0.5))
    if n_true_breakouts < MIN_TRUE_BREAKOUTS_FOR_TUNING:
        return {
            "status": "insufficient_breakouts",
            "promoted": False,
            "gate_passed": False,
            "reason": (
                f"Only {n_true_breakouts} of {n_total} matured 48h trajectories are true "
                f"breakouts; at least {MIN_TRUE_BREAKOUTS_FOR_TUNING} are required before "
                "auto-calibration can learn alert thresholds."
            ),
            "matured_posts_count": n_total,
            "true_breakouts_count": n_true_breakouts,
            "min_matured_posts": req_min,
            "max_drift_pct": drift_cap,
            "split": None,
            "live_holdout_metrics": None,
            "candidate_holdout_metrics": None,
            "param_diffs": [],
            "changelog_note": "",
            "version": None,
        }

    # Strict chronological 80% oldest train / 20% newest holdout split (zero future leakage)
    chron_order = np.argsort(matrix.created_ts, kind="mergesort")
    sorted_matrix = matrix.slice_indices(chron_order)
    n_train = max(1, min(n_total - 1, int(math.floor(n_total * 0.80))))
    train_matrix = sorted_matrix.slice_indices(slice(0, n_train))
    holdout_matrix = sorted_matrix.slice_indices(slice(n_train, n_total))

    train_max_ts = float(np.max(train_matrix.created_ts))
    holdout_min_ts = float(np.min(holdout_matrix.created_ts))
    zero_leakage = train_max_ts <= holdout_min_ts + 1e-6

    dim = len(TUNABLE_SPECS)
    prior_vals = np.array(
        [_get_param_val(prior_cfg, sec, nm) for sec, nm, _, _ in TUNABLE_SPECS],
        dtype=np.float64,
    )
    prev_vals = np.array(
        [_get_param_val(live_cfg, sec, nm) for sec, nm, _, _ in TUNABLE_SPECS],
        dtype=np.float64,
    )
    eff_lambda = max(0.01, float(l2_reg)) * (30.0 / max(float(n_train), 30.0))

    def _train_objective(u_vec: np.ndarray) -> float:
        cand = _build_cfg_from_vector(live_cfg, u_vec, drift_cap)
        scored = vectorized_score_trajectories(train_matrix, cand)
        brier = float(np.mean(np.square(scored.breakout_probs - train_matrix.is_true_breakout_48h)))

        cur_vals = np.array(
            [_get_param_val(cand, sec, nm) for sec, nm, _, _ in TUNABLE_SPECS],
            dtype=np.float64,
        )
        prior_rel = (cur_vals - prior_vals) / np.maximum(np.abs(prior_vals), 1e-6)
        step_rel = (cur_vals - prev_vals) / np.maximum(np.abs(prev_vals), 1e-6)
        reg_term = eff_lambda * float(np.mean(np.square(prior_rel))) + (
            0.20 * eff_lambda
        ) * float(np.mean(np.square(step_rel)))
        return brier + reg_term

    opt_res = minimize(
        _train_objective,
        x0=np.zeros(dim, dtype=np.float64),
        method="L-BFGS-B",
        bounds=[(-drift_cap, drift_cap)] * dim,
        options={"maxiter": 60, "ftol": 1e-8},
    )

    cand_cfg = _build_cfg_from_vector(live_cfg, opt_res.x, drift_cap)

    # Optimize watch_threshold on chronological 80% TRAIN split only within +-drift_cap
    train_scored = vectorized_score_trajectories(train_matrix, cand_cfg)
    w_lo = clamp_with_drift_cap(live_cfg.watch_threshold * (1.0 - drift_cap), live_cfg.watch_threshold, drift_cap)
    w_hi = clamp_with_drift_cap(live_cfg.watch_threshold * (1.0 + drift_cap), live_cfg.watch_threshold, drift_cap)
    best_watch = live_cfg.watch_threshold
    best_watch_obj = -1e9
    y_train_bool = train_matrix.is_true_breakout_48h >= 0.5

    for w_cand in np.linspace(w_lo, w_hi, 41):
        p_mask = train_scored.breakout_scores >= float(w_cand)
        tp = int(np.sum(p_mask & y_train_bool))
        fp = int(np.sum(p_mask & (~y_train_bool)))
        fn = int(np.sum((~p_mask) & y_train_bool))
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        denom = 0.25 * prec + rec
        f05_tr = (1.25 * prec * rec / denom) if denom > 0.0 else 0.0
        rel_w = (float(w_cand) - live_cfg.watch_threshold) / max(live_cfg.watch_threshold, 1.0)
        obj_w = f05_tr - (0.25 * eff_lambda) * (rel_w * rel_w)
        if obj_w > best_watch_obj + 1e-6:
            best_watch_obj = obj_w
            best_watch = round(float(w_cand), 2)

    cand_cfg.watch_threshold = clamp_with_drift_cap(
        best_watch, live_cfg.watch_threshold, drift_cap
    )
    scaled_surge = live_cfg.surge_threshold * (
        cand_cfg.watch_threshold / max(live_cfg.watch_threshold, 1.0)
    )
    s_lo = min((1.0 - drift_cap) * live_cfg.surge_threshold, (1.0 + drift_cap) * live_cfg.surge_threshold)
    s_hi = max((1.0 - drift_cap) * live_cfg.surge_threshold, (1.0 + drift_cap) * live_cfg.surge_threshold)
    cand_cfg.surge_threshold = round(
        float(np.clip(max(scaled_surge, cand_cfg.watch_threshold + 4.0), s_lo, s_hi)), 2
    )
    if cand_cfg.surge_threshold <= cand_cfg.watch_threshold:
        cand_cfg.surge_threshold = round(cand_cfg.watch_threshold + 1.0, 2)
    cand_cfg.watch_threshold, cand_cfg.surge_threshold = cap_alert_thresholds(
        cand_cfg.watch_threshold, cand_cfg.surge_threshold
    )

    # Final defensive drift-cap enforcement pass on every tunable parameter
    for sec, nm, _, _ in TUNABLE_SPECS:
        prev_v = _get_param_val(live_cfg, sec, nm)
        cur_v = _get_param_val(cand_cfg, sec, nm)
        clamped_v = round(clamp_with_drift_cap(cur_v, prev_v, drift_cap), 6)
        if sec == "weights":
            setattr(cand_cfg.weights, nm, clamped_v)
        elif sec == "surrogate":
            setattr(cand_cfg.surrogate, nm, clamped_v)
        elif sec == "fusion":
            setattr(cand_cfg.fusion, nm, clamped_v)

    # Evaluate Live vs Candidate on chronological 20% holdout & overall set
    live_train_out = vectorized_score_trajectories(train_matrix, live_cfg)
    cand_train_out = vectorized_score_trajectories(train_matrix, cand_cfg)
    live_hold_out = vectorized_score_trajectories(holdout_matrix, live_cfg)
    cand_hold_out = vectorized_score_trajectories(holdout_matrix, cand_cfg)
    live_all_out = vectorized_score_trajectories(sorted_matrix, live_cfg)
    cand_all_out = vectorized_score_trajectories(sorted_matrix, cand_cfg)

    lm_h = live_hold_out.metrics
    cm_h = cand_hold_out.metrics

    brier_improved = cm_h["brier_score"] < lm_h["brier_score"] - 1e-5
    f05_improved = (cm_h["f05_score"] > lm_h["f05_score"] + 1e-4) and (
        cm_h["brier_score"] <= lm_h["brier_score"] + 0.005
    )
    gate_passed = bool(brier_improved or f05_improved)
    should_promote = bool(gate_passed and (not force_stage_only))

    # Brier only measures probability calibration; it says nothing about where alerts should
    # fire. Thresholds move only when holdout F0.5 (alert quality) improves; otherwise the
    # candidate keeps the live thresholds (still clamped under the score ceiling).
    thresholds_held = not f05_improved
    if thresholds_held:
        cand_cfg.watch_threshold, cand_cfg.surge_threshold = cap_alert_thresholds(
            live_cfg.watch_threshold, live_cfg.surge_threshold
        )

    param_diffs = compute_param_diffs(live_cfg, cand_cfg)
    max_observed_drift_pct = max(
        (abs(float(d["pct_change"])) for d in param_diffs if d["pct_change"] is not None),
        default=0.0,
    )

    diff_fragments = [d["formatted"] for d in param_diffs[:6]]
    if len(param_diffs) > 6:
        diff_fragments.append(f"+{len(param_diffs) - 6} more")
    diff_text = "; ".join(diff_fragments) if diff_fragments else "all weights at stationary optimum"

    if force_stage_only:
        gate_label = "STAGED (force_stage_only=True)"
    elif should_promote:
        gate_label = "PROMOTED (Holdout Gate PASSED)"
    else:
        gate_label = "STAGED ONLY (Holdout Gate did not beat active baseline)"

    changelog_note = (
        f"[{gate_label}] Holdout Brier {lm_h['brier_score']:.4f} -> {cm_h['brier_score']:.4f}, "
        f"Holdout F0.5 {lm_h['f05_score']:.3f} -> {cm_h['f05_score']:.3f} "
        f"(80/20 chronological split: {n_train} train / {holdout_matrix.size} holdout, "
        f"max drift {max_observed_drift_pct:.1f}% <= {drift_cap * 100:.0f}% cap). "
        f"Changes: {diff_text}."
        + (" Alert thresholds held (holdout F0.5 did not improve)." if thresholds_held else "")
    )

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

    if should_promote:
        for v in all_versions:
            if v.workspace_id == workspace_id and v.is_active:
                v.is_active = False
        ws.watch_threshold = cand_cfg.watch_threshold
        ws.surge_threshold = cand_cfg.surge_threshold
        ws.updated_at = utc_now()

    split_meta = {
        "train_count": n_train,
        "holdout_count": holdout_matrix.size,
        "train_start_at": datetime.fromtimestamp(
            float(np.min(train_matrix.created_ts)), tz=timezone.utc
        ).isoformat(),
        "train_end_at": datetime.fromtimestamp(train_max_ts, tz=timezone.utc).isoformat(),
        "holdout_start_at": datetime.fromtimestamp(holdout_min_ts, tz=timezone.utc).isoformat(),
        "holdout_end_at": datetime.fromtimestamp(
            float(np.max(holdout_matrix.created_ts)), tz=timezone.utc
        ).isoformat(),
        "zero_future_leakage": zero_leakage,
    }

    holdout_metrics_payload = {
        "precision": cm_h["precision"],
        "recall": cm_h["recall"],
        "f05": cm_h["f05_score"],
        "brier": cm_h["brier_score"],
        "live_brier": lm_h["brier_score"],
        "live_f05": lm_h["f05_score"],
        "overall_brier": cand_all_out.metrics["brier_score"],
        "overall_f05": cand_all_out.metrics["f05_score"],
        "live_overall_brier": live_all_out.metrics["brier_score"],
        "matured_posts": n_total,
        "gate_passed": gate_passed,
        "promoted": should_promote,
        "max_observed_drift_pct": round(max_observed_drift_pct, 3),
        "split": split_meta,
        "param_diffs": param_diffs,
    }

    new_ver = AlgoParamVersion(
        workspace_id=workspace_id,
        version_number=next_num,
        label=f"v{next_num} · L2 Auto-Tuner ({'Active' if should_promote else 'Staged'})",
        source="auto_tuner",
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
        holdout_metrics_json=holdout_metrics_payload,
        changelog_note=changelog_note,
        is_active=should_promote,
        created_at=utc_now(),
    )
    session.add(new_ver)
    await session.commit()
    await session.refresh(new_ver)

    return {
        "status": "promoted" if should_promote else "staged",
        "promoted": should_promote,
        "gate_passed": gate_passed,
        "reason": changelog_note,
        "matured_posts_count": n_total,
        "min_matured_posts": req_min,
        "max_drift_pct": drift_cap,
        "max_observed_drift_pct": round(max_observed_drift_pct, 3),
        "split": split_meta,
        "live_train_metrics": live_train_out.metrics,
        "candidate_train_metrics": cand_train_out.metrics,
        "live_holdout_metrics": lm_h,
        "candidate_holdout_metrics": cm_h,
        "live_overall_metrics": live_all_out.metrics,
        "candidate_overall_metrics": cand_all_out.metrics,
        "param_diffs": param_diffs,
        "changelog_note": changelog_note,
        "version": serialize_param_version(
            new_ver,
            effective_active_id=new_ver.id if should_promote else (prev_ver.id if prev_ver else None),
            param_diffs=param_diffs,
        ),
    }


def serialize_param_version(
    ver: AlgoParamVersion,
    *,
    effective_active_id: int | None,
    param_diffs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Serializes an AlgoParamVersion ORM row with effective workspace status and diffs."""
    hm = dict(ver.holdout_metrics_json or {})
    diffs = param_diffs if param_diffs is not None else list(hm.get("param_diffs", []))
    return {
        "id": ver.id,
        "workspace_id": ver.workspace_id,
        "version_number": ver.version_number,
        "label": ver.label,
        "source": ver.source,
        "upstream_commit_sha": ver.upstream_commit_sha,
        "is_active": bool(ver.is_active),
        "is_effective_active": (ver.id == effective_active_id),
        "weights": ver.weights_json,
        "surrogate_params": ver.surrogate_params_json,
        "thresholds": ver.thresholds_json,
        "holdout_metrics": hm,
        "param_diffs": diffs,
        "changelog_note": ver.changelog_note,
        "created_at": ensure_utc(ver.created_at).isoformat(),
    }


async def list_workspace_changelog(
    session: AsyncSession,
    workspace_id: int,
) -> dict[str, Any]:
    """Lists all AlgoParamVersion rows applicable to the workspace with parent parameter diffs."""
    ws = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one()
    active_ver = await get_active_algo_version(session, workspace_id)
    effective_id = active_ver.id if active_ver is not None else None

    versions = list(
        (
            await session.execute(
                select(AlgoParamVersion)
                .where(
                    (AlgoParamVersion.workspace_id == workspace_id)
                    | (AlgoParamVersion.workspace_id.is_(None))
                )
                .order_by(AlgoParamVersion.version_number.desc(), AlgoParamVersion.id.desc())
            )
        )
        .scalars()
        .all()
    )

    by_id = {v.id: v for v in versions}
    baseline_cfg = config_from_version(None, ws)

    entries: list[dict[str, Any]] = []
    for v in versions:
        hm = dict(v.holdout_metrics_json or {})
        stored_diffs = hm.get("param_diffs")
        if isinstance(stored_diffs, list) and stored_diffs:
            diffs = stored_diffs
        else:
            parent_id = (v.thresholds_json or {}).get("parent_version_id")
            parent_ver = by_id.get(int(parent_id)) if parent_id is not None else None
            parent_cfg = config_from_version(parent_ver, ws) if parent_ver else baseline_cfg
            cur_cfg = config_from_version(v, ws)
            diffs = compute_param_diffs(parent_cfg, cur_cfg)

        entries.append(
            serialize_param_version(v, effective_active_id=effective_id, param_diffs=diffs)
        )

    return {
        "workspace_id": workspace_id,
        "active_version_id": effective_id,
        "versions": entries,
    }


async def rollback_to_version(
    session: AsyncSession,
    workspace_id: int,
    version_id: int,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Atomically activates/restores a workspace or global AlgoParamVersion for THIS workspace only.

    Global (`workspace_id=None`) rows are strictly immutable across workspace rollbacks so
    Workspace B never mutates Workspace A's active parameters:
    - If `target` is workspace-owned (`target.workspace_id == workspace_id`), toggles `is_active`
      among `workspace_id`'s versions only.
    - If `target` is the active global baseline (`target.workspace_id is None and target.is_active`),
      deactivates `workspace_id` overrides so this workspace falls back to the immutable global baseline.
    - If `target` is an inactive/staged global version (`target.workspace_id is None and not target.is_active`),
      clones `target` into a workspace-scoped `AlgoParamVersion(workspace_id=workspace_id, is_active=True)`
      without mutating the global row or any sibling workspace.
    """
    ws = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one()

    target = (
        await session.execute(
            select(AlgoParamVersion).where(
                AlgoParamVersion.id == version_id,
                (AlgoParamVersion.workspace_id == workspace_id)
                | (AlgoParamVersion.workspace_id.is_(None)),
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise LookupError(
            f"AlgoParamVersion {version_id} not found or not applicable to workspace {workspace_id}."
        )

    prev_active = await get_active_algo_version(session, workspace_id)

    ws_versions = list(
        (
            await session.execute(
                select(AlgoParamVersion).where(AlgoParamVersion.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )

    ts_iso = utc_now().strftime("%Y-%m-%d %H:%M UTC")
    audit_suffix = f" [Activated for workspace #{workspace_id} at {ts_iso}"
    if reason and reason.strip():
        audit_suffix += f": {reason.strip()}"
    audit_suffix += "]"

    target_cfg = config_from_version(target, ws)
    prev_cfg = config_from_version(prev_active, ws) if prev_active else config_from_version(None, ws)
    diffs = compute_param_diffs(prev_cfg, target_cfg)

    activated_ver: AlgoParamVersion
    if target.workspace_id == workspace_id:
        for v in ws_versions:
            v.is_active = v.id == target.id
        base_note = (target.changelog_note or "").split(" [Activated for workspace")[0]
        target.changelog_note = f"{base_note}{audit_suffix}".strip()
        hm = dict(target.holdout_metrics_json or {})
        hm["last_activated_at"] = utc_now().isoformat()
        hm["last_activated_workspace_id"] = workspace_id
        if prev_active is not None and prev_active.id != target.id:
            hm["rolled_back_from_version_id"] = prev_active.id
        target.holdout_metrics_json = hm
        activated_ver = target
    elif target.is_active:
        # Target is the active global baseline: clear workspace overrides ONLY without mutating global row
        for v in ws_versions:
            v.is_active = False
        activated_ver = target
    else:
        # Target is an inactive/staged global version: clone into workspace_id without mutating global row
        for v in ws_versions:
            v.is_active = False

        max_ver = int(
            (
                await session.execute(
                    select(func.max(AlgoParamVersion.version_number)).where(
                        (AlgoParamVersion.workspace_id == workspace_id)
                        | (AlgoParamVersion.workspace_id.is_(None))
                    )
                )
            ).scalar_one()
            or 1
        )
        hm = dict(target.holdout_metrics_json or {})
        hm["last_activated_at"] = utc_now().isoformat()
        hm["last_activated_workspace_id"] = workspace_id
        hm["cloned_from_version_id"] = target.id
        if prev_active is not None:
            hm["rolled_back_from_version_id"] = prev_active.id
        if diffs and not hm.get("param_diffs"):
            hm["param_diffs"] = diffs

        cloned_thresholds = dict(target.thresholds_json or {})
        cloned_thresholds["cloned_from_version_id"] = target.id
        cloned_thresholds["parent_version_id"] = prev_active.id if prev_active else target.id

        base_note = (target.changelog_note or "").strip()
        activated_ver = AlgoParamVersion(
            workspace_id=workspace_id,
            version_number=max_ver + 1,
            label=f"{target.label} (Workspace #{workspace_id} Live)",
            source=target.source,
            upstream_commit_sha=target.upstream_commit_sha,
            weights_json=dict(target.weights_json or {}),
            surrogate_params_json=dict(target.surrogate_params_json or {}),
            thresholds_json=cloned_thresholds,
            holdout_metrics_json=hm,
            changelog_note=f"{base_note}{audit_suffix}".strip(),
            is_active=True,
            created_at=utc_now(),
        )
        session.add(activated_ver)
        await session.flush()

    ws.watch_threshold = target_cfg.watch_threshold
    ws.surge_threshold = target_cfg.surge_threshold
    ws.updated_at = utc_now()

    await session.commit()
    await session.refresh(activated_ver)

    return {
        "workspace_id": workspace_id,
        "previous_version_id": prev_active.id if prev_active else None,
        "active_version_id": activated_ver.id,
        "activated_version": serialize_param_version(
            activated_ver, effective_active_id=activated_ver.id, param_diffs=diffs
        ),
    }
