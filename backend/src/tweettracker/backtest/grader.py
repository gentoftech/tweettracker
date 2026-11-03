"""48h Lifecycle Outcome Grader & Workspace/Community Scorecard Calculator (Story 3.1)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
import math
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.backtest.replay import (
    compute_calibration_metrics_vec,
    config_from_version,
    ensure_utc,
    extract_author_median_48h,
    get_active_algo_version,
)
from tweettracker.models import (
    AccountCommunityMembership,
    AlgoParamVersion,
    BreakoutPrediction,
    Community,
    Post,
    PostSnapshot,
    TrackedAccount,
    Workspace,
    utc_now,
)

VALID_SHORT_OUTCOMES = frozenset({"tp", "fp", "fn", "tn"})
ACTIVE_COMMUNITY_AFFINITY_FLOOR = 0.25


def _is_active_membership(m: AccountCommunityMembership) -> bool:
    return bool(m.is_manual_tag) or (
        max(float(m.nmf_interested_in or 0.0), float(m.nmf_known_for or 0.0))
        >= ACTIVE_COMMUNITY_AFFINITY_FLOOR
    )


def _diagnose_false_alarm(
    post: Post,
    early_views: int,
    early_likes: int,
    early_bookmarks: int,
    early_quotes: int,
) -> tuple[str, str, str]:
    """Generates causal postmortem attribution for an Early Fizzle (FP)."""
    bmk_like_ratio = early_bookmarks / max(early_likes, 1)
    quote_rate = early_quotes / max(early_views, 1)

    if post.has_url:
        return (
            "external_link_dwell_leak",
            "External URL Dwell Leak",
            "Outbound link discounted post-click dwell time (0.72x) and throttled OON expansion after initial follower burst.",
        )
    if bmk_like_ratio < 0.22 and quote_rate < 0.0012:
        return (
            "shallow_like_spike",
            "Shallow Like Spike (Low Bookmarks/Quotes)",
            "High 15-30m like velocity lacked bookmarks and quotes, starving the 20x share_via_copy_link head.",
        )
    if post.dpp_novelty_multiplier < 0.86:
        return (
            "dpp_semantic_crowding",
            "DPP Semantic Crowding Discount",
            f"Overlapping topic cluster reduced DPP novelty multiplier ({post.dpp_novelty_multiplier:.2f}x) in downstream Home Mixer batches.",
        )
    if post.catalysis_boost < 0.10:
        return (
            "no_heavy_hitter_relay",
            "No Heavy-Hitter Relay Past 60m",
            "Strong opening cohort engagement plateaued without Heavy-Hitter quote or mutual reply catalysis.",
        )
    return (
        "in_network_saturation",
        "In-Network Pool Saturation",
        "Fast follower engagement saturated the in-network graph without crossing SimClusters OON velocity thresholds.",
    )


def _diagnose_missed_breakout(
    post: Post,
    author: TrackedAccount,
    early_views: int,
    early_bookmarks: int,
    final_views: int,
) -> tuple[str, str, str]:
    """Generates causal postmortem attribution for a Slow Burn (FN)."""
    growth_mult = final_views / max(early_views, 1)
    bmk_rate = early_bookmarks / max(early_views, 1)

    if growth_mult >= 6.0:
        return (
            "late_heavy_hitter_catalysis",
            "Late Heavy-Hitter Quote Catalysis (>60m)",
            f"Modest 30m velocity surged {growth_mult:.1f}x after post-60m Heavy-Hitter quote/reply amplification.",
        )
    if bmk_rate >= 0.0040:
        return (
            "silent_bookmark_compounding",
            "Silent Bookmark & Copy-Link Compounding",
            "Quiet early view count masked elevated bookmark propensity driving delayed 20x share_via_copy_link distribution.",
        )
    if author.bridge_role != "provincial":
        return (
            "simclusters_secondary_wave",
            "Delayed Out-of-Network SimClusters Wave",
            "Post picked up secondary Out-of-Network SimClusters distribution in hours 2–24.",
        )
    return (
        "sustained_dwell_retention",
        "Sustained Dwell Retention Curve",
        "Steady reader dwell time compounded across multi-hour SimClusters retrieval waves up to the 48h ceiling.",
    )


async def _ensure_baseline_param_version(session: AsyncSession) -> AlgoParamVersion:
    """Fetches an active/existing AlgoParamVersion or creates the 9106e9f baseline if DB was bare."""
    existing = (
        await session.execute(select(AlgoParamVersion).order_by(AlgoParamVersion.id.asc()).limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    cfg = config_from_version(None, None)
    ver = AlgoParamVersion(
        workspace_id=None,
        version_number=1,
        label=cfg.label,
        source="upstream_xalgo",
        upstream_commit_sha=cfg.upstream_commit_sha,
        weights_json=cfg.weights.to_dict(),
        surrogate_params_json=cfg.surrogate.to_dict(),
        thresholds_json={
            "watch_threshold": cfg.watch_threshold,
            "surge_threshold": cfg.surge_threshold,
            "pipeline": cfg.pipeline.to_dict(),
            "fusion": cfg.fusion.to_dict(),
        },
        holdout_metrics_json={},
        changelog_note="Auto-initialized 9106e9f baseline during 48h outcome grading.",
        is_active=True,
    )
    session.add(ver)
    await session.flush()
    return ver


async def grade_workspace_outcomes(
    session: AsyncSession,
    workspace_id: int,
    *,
    breakout_multiple: float | None = None,
    community_floor_views: int | None = None,
    community_floors: Mapping[str, int] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Grades all matured (>=48h) posts and evaluates BreakoutPrediction rows (tp/fp/fn/tn + Brier)."""
    ref_now = ensure_utc(now)
    ws = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one()

    active_ver = await get_active_algo_version(session, workspace_id)
    if active_ver is None:
        active_ver = await _ensure_baseline_param_version(session)
    active_cfg = config_from_version(active_ver, ws)

    eff_mult = float(breakout_multiple) if breakout_multiple is not None else active_cfg.breakout_multiple
    eff_floor = (
        int(community_floor_views)
        if community_floor_views is not None
        else active_cfg.community_floor_views
    )
    eff_comm_floors = dict(active_cfg.community_floors)
    if community_floors:
        for k, v in community_floors.items():
            eff_comm_floors[str(k)] = int(v)

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

    post_rows = list(
        (
            await session.execute(
                select(Post, TrackedAccount)
                .join(TrackedAccount, Post.author_account_id == TrackedAccount.id)
                .where(Post.workspace_id == workspace_id)
                .order_by(Post.created_at_x.asc(), Post.id.asc())
            )
        ).all()
    )

    matured_pairs: list[tuple[Post, TrackedAccount]] = []
    for post, author in post_rows:
        age_secs = (ref_now - ensure_utc(post.created_at_x)).total_seconds()
        if bool(post.matured_48h) or age_secs >= 48.0 * 3600.0:
            matured_pairs.append((post, author))

    if not matured_pairs:
        return {
            "workspace_id": workspace_id,
            "matured_posts_graded": 0,
            "predictions_evaluated": 0,
            "true_breakouts_count": 0,
            "tp": 0,
            "fp": 0,
            "fn": 0,
            "tn": 0,
            "breakout_multiple": eff_mult,
            "community_floor_views": eff_floor,
        }

    matured_ids = [p.id for p, _ in matured_pairs]
    snaps = list(
        (
            await session.execute(
                select(PostSnapshot)
                .where(PostSnapshot.post_id.in_(matured_ids))
                .order_by(PostSnapshot.post_id.asc(), PostSnapshot.age_minutes.asc())
            )
        )
        .scalars()
        .all()
    )
    snaps_by_post: dict[int, list[PostSnapshot]] = {}
    for s in snaps:
        snaps_by_post.setdefault(s.post_id, []).append(s)

    preds = list(
        (
            await session.execute(
                select(BreakoutPrediction)
                .where(
                    BreakoutPrediction.workspace_id == workspace_id,
                    BreakoutPrediction.post_id.in_(matured_ids),
                )
                .order_by(BreakoutPrediction.id.asc())
            )
        )
        .scalars()
        .all()
    )
    preds_by_post: dict[int, list[BreakoutPrediction]] = {}
    for pr in preds:
        preds_by_post.setdefault(pr.post_id, []).append(pr)

    true_bo_count = 0
    preds_evaluated = 0
    counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}

    for post, author in matured_pairs:
        p_snaps = snaps_by_post.get(post.id, [])
        med_48h = extract_author_median_48h(author)

        if post.actual_48h_views is not None and post.actual_48h_views > 0:
            actual_v = int(post.actual_48h_views)
        else:
            late_snaps = [s for s in p_snaps if s.age_minutes >= 1440]
            if late_snaps:
                snap_48 = min(late_snaps, key=lambda s: abs(s.age_minutes - 2880))
                actual_v = int(snap_48.views)
            else:
                actual_v = int(post.latest_views)

        a_comms = acct_comms.get(author.id, [])
        floor_v = eff_floor
        watch_overrides: list[float] = []
        surge_overrides: list[float] = []
        for c in a_comms:
            if c.slug in eff_comm_floors:
                floor_v = min(floor_v, eff_comm_floors[c.slug])
            elif str(c.id) in eff_comm_floors:
                floor_v = min(floor_v, eff_comm_floors[str(c.id)])
            if c.watch_threshold_override is not None:
                watch_overrides.append(float(c.watch_threshold_override))
            if c.surge_threshold_override is not None:
                surge_overrides.append(float(c.surge_threshold_override))

        watch_t = min(watch_overrides) if watch_overrides else active_cfg.watch_threshold
        raw_surge = min(surge_overrides) if surge_overrides else active_cfg.surge_threshold
        surge_t = max(watch_t + 1.0, raw_surge)

        actual_mult = round(actual_v / max(float(med_48h), 1.0), 3)
        is_true_bo = (actual_v >= eff_mult * float(med_48h)) and (actual_v >= floor_v)

        post.matured_48h = True
        post.actual_48h_views = actual_v
        post.actual_48h_multiple = actual_mult
        post.is_true_breakout_48h = bool(is_true_bo)
        if is_true_bo:
            true_bo_count += 1

        p_preds = preds_by_post.get(post.id, [])
        if not p_preds:
            early_snaps = [s for s in p_snaps if s.age_minutes <= 60]
            early_snap = (
                min(early_snaps, key=lambda s: abs(s.age_minutes - 30))
                if early_snaps
                else (p_snaps[0] if p_snaps else None)
            )
            b_score = (
                float(early_snap.breakout_score_at_capture)
                if (early_snap is not None and early_snap.breakout_score_at_capture > 0)
                else float(post.breakout_score)
            )
            pred_age = int(early_snap.age_minutes) if early_snap is not None else 30
            prob_scale = max(active_cfg.fusion.prob_scale, 0.5)
            logit = max(-40.0, min(40.0, -(b_score - active_cfg.fusion.prob_midpoint) / prob_scale))
            prob = round(1.0 / (1.0 + math.exp(logit)), 4)
            tier = "surge" if b_score >= surge_t else ("watch" if b_score >= watch_t else "none")
            created_utc = ensure_utc(post.created_at_x)
            synth_pred = BreakoutPrediction(
                workspace_id=workspace_id,
                post_id=post.id,
                param_version_id=active_ver.id,
                predicted_at=created_utc + timedelta(minutes=pred_age),
                post_age_minutes=pred_age,
                breakout_probability=prob,
                breakout_score=b_score,
                alert_tier=tier,
            )
            session.add(synth_pred)
            p_preds = [synth_pred]

        y_true = 1.0 if is_true_bo else 0.0
        eval_ts = ensure_utc(post.created_at_x) + timedelta(hours=48)
        if eval_ts > ref_now:
            eval_ts = ref_now

        for pr in p_preds:
            pred_pos = pr.alert_tier in ("watch", "surge") or float(pr.breakout_score) >= watch_t
            if pred_pos and is_true_bo:
                out_cls = "tp"
            elif pred_pos and (not is_true_bo):
                out_cls = "fp"
            elif (not pred_pos) and is_true_bo:
                out_cls = "fn"
            else:
                out_cls = "tn"

            pr.outcome_class = out_cls
            pr.brier_error = round((float(pr.breakout_probability) - y_true) ** 2, 6)
            if pr.evaluated_at_48h is None:
                pr.evaluated_at_48h = eval_ts
            preds_evaluated += 1

        # Count primary (latest) prediction per post for summary totals
        counts[p_preds[-1].outcome_class or "tn"] += 1

    await session.commit()

    return {
        "workspace_id": workspace_id,
        "matured_posts_graded": len(matured_pairs),
        "predictions_evaluated": preds_evaluated,
        "true_breakouts_count": true_bo_count,
        "tp": counts["tp"],
        "fp": counts["fp"],
        "fn": counts["fn"],
        "tn": counts["tn"],
        "breakout_multiple": eff_mult,
        "community_floor_views": eff_floor,
    }


async def compute_workspace_scorecard(
    session: AsyncSession,
    workspace_id: int,
    *,
    auto_grade: bool = True,
    breakout_multiple: float | None = None,
    community_floor_views: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Computes full 48h Prediction Scorecard globally + per community + reliability curve + postmortems."""
    ref_now = ensure_utc(now)
    if auto_grade:
        await grade_workspace_outcomes(
            session,
            workspace_id,
            breakout_multiple=breakout_multiple,
            community_floor_views=community_floor_views,
            now=ref_now,
        )

    ws = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one()
    active_ver = await get_active_algo_version(session, workspace_id)
    active_cfg = config_from_version(active_ver, ws)
    eff_mult = float(breakout_multiple) if breakout_multiple is not None else active_cfg.breakout_multiple
    eff_floor = (
        int(community_floor_views)
        if community_floor_views is not None
        else active_cfg.community_floor_views
    )

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

    matured_rows = list(
        (
            await session.execute(
                select(Post, TrackedAccount)
                .join(TrackedAccount, Post.author_account_id == TrackedAccount.id)
                .where(Post.workspace_id == workspace_id, Post.matured_48h.is_(True))
                .order_by(Post.created_at_x.asc(), Post.id.asc())
            )
        ).all()
    )

    matured_ids = [p.id for p, _ in matured_rows]
    preds_by_post: dict[int, BreakoutPrediction] = {}
    snaps_by_post: dict[int, list[PostSnapshot]] = {}

    if matured_ids:
        pred_rows = list(
            (
                await session.execute(
                    select(BreakoutPrediction)
                    .where(
                        BreakoutPrediction.workspace_id == workspace_id,
                        BreakoutPrediction.post_id.in_(matured_ids),
                    )
                    .order_by(BreakoutPrediction.id.asc())
                )
            )
            .scalars()
            .all()
        )
        for pr in pred_rows:
            preds_by_post[pr.post_id] = pr

        snap_rows = list(
            (
                await session.execute(
                    select(PostSnapshot)
                    .where(PostSnapshot.post_id.in_(matured_ids))
                    .order_by(PostSnapshot.post_id.asc(), PostSnapshot.age_minutes.asc())
                )
            )
            .scalars()
            .all()
        )
        for s in snap_rows:
            snaps_by_post.setdefault(s.post_id, []).append(s)

    n = len(matured_rows)
    probs = np.zeros(n, dtype=np.float64)
    scores = np.zeros(n, dtype=np.float64)
    pred_pos = np.zeros(n, dtype=bool)
    y_true = np.zeros(n, dtype=np.float64)
    views_alert = np.zeros(n, dtype=np.float64)
    views_48h = np.zeros(n, dtype=np.float64)
    actual_mults = np.zeros(n, dtype=np.float64)
    created_ts = np.zeros(n, dtype=np.float64)
    alert_ages = np.zeros(n, dtype=np.float64)

    comm_indices: dict[int, list[int]] = {c.id: [] for c in comms}
    false_alarms: list[dict[str, Any]] = []
    missed_breakouts: list[dict[str, Any]] = []

    norm_map = {
        "tp": "tp",
        "true_positive": "tp",
        "fp": "fp",
        "false_positive": "fp",
        "fn": "fn",
        "false_negative": "fn",
        "tn": "tn",
        "true_negative": "tn",
    }

    for i, (post, author) in enumerate(matured_rows):
        pr = preds_by_post.get(post.id)
        p_snaps = snaps_by_post.get(post.id, [])
        target_age = pr.post_age_minutes if pr is not None else 30
        early_snaps = [s for s in p_snaps if s.age_minutes <= max(60, target_age)]
        early_snap = (
            min(early_snaps, key=lambda s: abs(s.age_minutes - target_age))
            if early_snaps
            else (p_snaps[0] if p_snaps else None)
        )

        v_early = (
            int(early_snap.views)
            if early_snap is not None
            else max(1, int((post.actual_48h_views or post.latest_views) * 0.25))
        )
        l_early = int(early_snap.likes) if early_snap is not None else int(v_early * 0.015)
        b_early = int(early_snap.bookmarks) if early_snap is not None else int(v_early * 0.003)
        q_early = int(early_snap.quotes) if early_snap is not None else int(v_early * 0.001)

        med_48h = extract_author_median_48h(author)
        v_final = int(post.actual_48h_views if post.actual_48h_views is not None else post.latest_views)
        mult_48h = float(
            post.actual_48h_multiple
            if post.actual_48h_multiple is not None
            else round(v_final / max(med_48h, 1), 3)
        )
        is_bo = bool(post.is_true_breakout_48h)

        if pr is not None:
            prob_val = float(pr.breakout_probability)
            score_val = float(pr.breakout_score)
            out_cls = norm_map.get((pr.outcome_class or "").lower(), "")
            if not out_cls:
                is_pos = pr.alert_tier in ("watch", "surge") or score_val >= active_cfg.watch_threshold
                out_cls = (
                    "tp"
                    if (is_pos and is_bo)
                    else ("fp" if is_pos else ("fn" if is_bo else "tn"))
                )
            is_pos = out_cls in ("tp", "fp")
            age_m = int(pr.post_age_minutes)
        else:
            score_val = float(post.breakout_score)
            prob_scale = max(active_cfg.fusion.prob_scale, 0.5)
            logit = max(-40.0, min(40.0, -(score_val - active_cfg.fusion.prob_midpoint) / prob_scale))
            prob_val = 1.0 / (1.0 + math.exp(logit))
            is_pos = score_val >= active_cfg.watch_threshold
            out_cls = (
                "tp" if (is_pos and is_bo) else ("fp" if is_pos else ("fn" if is_bo else "tn"))
            )
            age_m = int(early_snap.age_minutes) if early_snap is not None else 30

        probs[i] = prob_val
        scores[i] = score_val
        pred_pos[i] = is_pos
        y_true[i] = 1.0 if is_bo else 0.0
        views_alert[i] = float(v_early)
        views_48h[i] = float(v_final)
        actual_mults[i] = mult_48h
        created_ts[i] = float(ensure_utc(post.created_at_x).timestamp())
        alert_ages[i] = float(age_m)

        a_comms = acct_comms.get(author.id, [])
        comm_badges = [
            {"id": c.id, "name": c.name, "slug": c.slug, "color": c.color} for c in a_comms
        ]
        for c in a_comms:
            comm_indices[c.id].append(i)

        growth_share = round(max(0.0, (v_final - v_early) / max(v_final, 1)), 4)
        growth_mult = round(v_final / max(v_early, 1), 2)

        if out_cls == "fp":
            d_tag, d_label, d_reason = _diagnose_false_alarm(
                post, v_early, l_early, b_early, q_early
            )
            false_alarms.append(
                {
                    "post_id": post.id,
                    "x_post_id": post.x_post_id,
                    "x_status_url": f"https://x.com/{author.handle}/status/{post.x_post_id}",
                    "author_handle": author.handle,
                    "author_display_name": author.display_name,
                    "communities": comm_badges,
                    "text": post.text,
                    "outcome_class": "fp",
                    "breakout_score": round(score_val, 2),
                    "breakout_probability": round(prob_val, 4),
                    "alert_age_minutes": age_m,
                    "views_at_alert": v_early,
                    "actual_48h_views": v_final,
                    "author_median_48h_views": med_48h,
                    "actual_48h_multiple": round(mult_48h, 2),
                    "lead_time_growth_share": growth_share,
                    "lead_time_multiple": growth_mult,
                    "bookmark_to_like_ratio": round(b_early / max(l_early, 1), 3),
                    "quote_rate": round(q_early / max(v_early, 1), 5),
                    "has_url": bool(post.has_url),
                    "dpp_novelty_multiplier": round(float(post.dpp_novelty_multiplier), 3),
                    "diagnostic_tag": d_tag,
                    "diagnostic_label": d_label,
                    "diagnostic_reason": d_reason,
                }
            )
        elif out_cls == "fn":
            d_tag, d_label, d_reason = _diagnose_missed_breakout(
                post, author, v_early, b_early, v_final
            )
            missed_breakouts.append(
                {
                    "post_id": post.id,
                    "x_post_id": post.x_post_id,
                    "x_status_url": f"https://x.com/{author.handle}/status/{post.x_post_id}",
                    "author_handle": author.handle,
                    "author_display_name": author.display_name,
                    "communities": comm_badges,
                    "text": post.text,
                    "outcome_class": "fn",
                    "breakout_score": round(score_val, 2),
                    "breakout_probability": round(prob_val, 4),
                    "alert_age_minutes": age_m,
                    "views_at_alert": v_early,
                    "actual_48h_views": v_final,
                    "author_median_48h_views": med_48h,
                    "actual_48h_multiple": round(mult_48h, 2),
                    "lead_time_growth_share": growth_share,
                    "lead_time_multiple": growth_mult,
                    "bookmark_to_like_ratio": round(b_early / max(l_early, 1), 3),
                    "quote_rate": round(q_early / max(v_early, 1), 5),
                    "has_url": bool(post.has_url),
                    "dpp_novelty_multiplier": round(float(post.dpp_novelty_multiplier), 3),
                    "diagnostic_tag": d_tag,
                    "diagnostic_label": d_label,
                    "diagnostic_reason": d_reason,
                }
            )

    false_alarms.sort(key=lambda r: -r["breakout_score"])
    missed_breakouts.sort(key=lambda r: -r["actual_48h_multiple"])

    overall_metrics = compute_calibration_metrics_vec(
        probs=probs,
        pred_pos_mask=pred_pos,
        y_true_float=y_true,
        views_at_eval=views_alert,
        actual_48h_views=views_48h,
        created_ts=created_ts,
        actual_multiples=actual_mults,
    )
    overall_metrics["median_alert_age_minutes"] = (
        round(float(np.median(alert_ages[pred_pos])), 1)
        if int(np.sum(pred_pos)) > 0
        else (round(float(np.median(alert_ages)), 1) if n > 0 else 30.0)
    )
    overall_metrics["true_breakouts_count"] = int(np.sum(y_true >= 0.5))

    community_scorecards: list[dict[str, Any]] = []
    for c in comms:
        idxs = np.asarray(comm_indices[c.id], dtype=np.int64)
        c_floor = active_cfg.community_floors.get(
            c.slug, active_cfg.community_floors.get(str(c.id), eff_floor)
        )
        c_watch = (
            float(c.watch_threshold_override)
            if c.watch_threshold_override is not None
            else active_cfg.watch_threshold
        )
        if idxs.size > 0:
            c_met = compute_calibration_metrics_vec(
                probs=probs[idxs],
                pred_pos_mask=pred_pos[idxs],
                y_true_float=y_true[idxs],
                views_at_eval=views_alert[idxs],
                actual_48h_views=views_48h[idxs],
                created_ts=created_ts[idxs],
                actual_multiples=actual_mults[idxs],
            )
            c_true_bo = int(np.sum(y_true[idxs] >= 0.5))
        else:
            c_met = compute_calibration_metrics_vec(
                probs=np.empty(0, dtype=np.float64),
                pred_pos_mask=np.empty(0, dtype=bool),
                y_true_float=np.empty(0, dtype=np.float64),
                views_at_eval=np.empty(0, dtype=np.float64),
                actual_48h_views=np.empty(0, dtype=np.float64),
            )
            c_true_bo = 0

        community_scorecards.append(
            {
                "community_id": c.id,
                "name": c.name,
                "slug": c.slug,
                "color": c.color,
                "effective_watch_threshold": c_watch,
                "community_floor_views": c_floor,
                "matured_posts_count": c_met["matured_posts_count"],
                "true_breakouts_count": c_true_bo,
                "tp": c_met["tp"],
                "fp": c_met["fp"],
                "fn": c_met["fn"],
                "tn": c_met["tn"],
                "precision": c_met["precision"],
                "recall": c_met["recall"],
                "f05_score": c_met["f05_score"],
                "brier_score": c_met["brier_score"],
                "roc_auc": c_met["roc_auc"],
                "median_lead_time_growth_share": c_met["median_lead_time_growth_share"],
                "median_lead_time_multiple": c_met["median_lead_time_multiple"],
            }
        )

    return {
        "workspace_id": workspace_id,
        "workspace_name": ws.name,
        "evaluated_at": utc_now().isoformat(),
        "outcome_definition": {
            "breakout_multiple": eff_mult,
            "community_floor_views": eff_floor,
            "lifecycle_hours": 48,
        },
        "active_version": {
            "id": active_cfg.version_id,
            "version_number": active_cfg.version_number,
            "label": active_cfg.label,
            "source": active_cfg.source,
            "upstream_commit_sha": active_cfg.upstream_commit_sha,
            "watch_threshold": active_cfg.watch_threshold,
            "surge_threshold": active_cfg.surge_threshold,
        },
        "overall": overall_metrics,
        "communities": community_scorecards,
        "calibration_curve": overall_metrics["calibration_bins"],
        "false_alarms_fp": false_alarms[:25],
        "missed_breakouts_fn": missed_breakouts[:25],
    }
