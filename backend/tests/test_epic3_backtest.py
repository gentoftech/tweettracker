"""Comprehensive unit, parity, benchmark (<2s 10k) & FastAPI route tests for Epic 3."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import timedelta
import time

import httpx
import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tweettracker.algo import (
    ObservablePostMetrics,
    PipelineParams,
    QueryScoringContext,
    SurrogateCalibrationParams,
    ValueModelWeights,
    build_candidate_scoring_inputs,
    compute_text_embedding,
    compute_value_scores,
)
from tweettracker.api.main import create_app
from tweettracker.backtest import (
    FusionCalibrationParams,
    ResolvedAlgoConfig,
    clamp_with_drift_cap,
    compute_workspace_scorecard,
    get_active_algo_version,
    grade_workspace_outcomes,
    pack_workspace_trajectories,
    rollback_to_version,
    run_auto_calibration,
    synthesize_benchmark_trajectories,
    vectorized_score_trajectories,
)
from tweettracker.db import configure_database, dispose_database
from tweettracker.models import (
    AlgoParamVersion,
    BreakoutPrediction,
    Post,
    PostSnapshot,
    TrackedAccount,
    Workspace,
    utc_now,
)
from tweettracker.seed import _build_baseline_curves, bootstrap_database


@pytest.fixture
async def db_and_client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient], None]:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.delenv("TWEETTRACKER_ADMIN_PASSWORD", raising=False)

    engine, session_factory = configure_database("sqlite+aiosqlite:///:memory:")
    await bootstrap_database(engine, session_factory, auto_migrate=True, seed_fixtures=True)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield session_factory, client

    await dispose_database()


async def test_48h_outcome_grader_and_scorecard_ws1_and_ws2(
    db_and_client: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
) -> None:
    session_factory, _ = db_and_client
    now = utc_now()

    async with session_factory() as session:
        workspaces = list(
            (await session.execute(select(Workspace).order_by(Workspace.id.asc()))).scalars().all()
        )
        ws1, ws2 = workspaces[0], workspaces[1]

        # 1. Verify Workspace 1 (50 seeded matured posts) grading & scorecard invariants
        grade_ws1 = await grade_workspace_outcomes(session, ws1.id)
        assert grade_ws1["matured_posts_graded"] == 50
        assert grade_ws1["tp"] + grade_ws1["fp"] + grade_ws1["fn"] + grade_ws1["tn"] == 50
        assert grade_ws1["tp"] > 0
        assert grade_ws1["fp"] > 0
        assert grade_ws1["fn"] > 0
        assert grade_ws1["tn"] > 0

        # Ensure every graded BreakoutPrediction has outcome_class strictly in {tp, fp, fn, tn}
        ws1_matured_preds = list(
            (
                await session.execute(
                    select(BreakoutPrediction, Post)
                    .join(Post, BreakoutPrediction.post_id == Post.id)
                    .where(
                        BreakoutPrediction.workspace_id == ws1.id,
                        Post.matured_48h.is_(True),
                    )
                )
            ).all()
        )
        assert len(ws1_matured_preds) == 50
        for pred, post in ws1_matured_preds:
            assert pred.outcome_class in {"tp", "fp", "fn", "tn"}
            assert pred.evaluated_at_48h is not None
            y = 1.0 if post.is_true_breakout_48h else 0.0
            assert pred.brier_error == pytest.approx((pred.breakout_probability - y) ** 2, abs=1e-5)

        sc1 = await compute_workspace_scorecard(session, ws1.id, auto_grade=False)
        ov1 = sc1["overall"]
        assert ov1["matured_posts_count"] == 50
        expected_p = ov1["tp"] / (ov1["tp"] + ov1["fp"])
        expected_r = ov1["tp"] / (ov1["tp"] + ov1["fn"])
        expected_f05 = 1.25 * expected_p * expected_r / (0.25 * expected_p + expected_r)
        assert ov1["precision"] == pytest.approx(expected_p, abs=1e-3)
        assert ov1["recall"] == pytest.approx(expected_r, abs=1e-3)
        assert ov1["f05_score"] == pytest.approx(expected_f05, abs=1e-3)
        assert 0.0 <= ov1["brier_score"] <= 1.0
        assert 0.5 < ov1["roc_auc"] <= 1.0
        assert ov1["median_lead_time_growth_share"] > 0.0
        assert ov1["median_lead_time_multiple"] > 1.0

        # 10-bin calibration curve invariants
        assert len(sc1["calibration_curve"]) == 10
        assert sum(b["count"] for b in sc1["calibration_curve"]) == 50
        assert len(sc1["communities"]) == 3
        assert len(sc1["false_alarms_fp"]) == ov1["fp"]
        assert len(sc1["missed_breakouts_fn"]) == ov1["fn"]
        assert all(row["diagnostic_tag"] for row in sc1["false_alarms_fp"])
        assert all(row["diagnostic_tag"] for row in sc1["missed_breakouts_fn"])

        # 2. Top up Workspace 2 from 16 seeded matured posts to 40 matured posts
        #    including explicit boundary cases (>= 3.0x median AND >= 5,000 floor)
        ws2_accts = list(
            (
                await session.execute(
                    select(TrackedAccount)
                    .where(TrackedAccount.workspace_id == ws2.id)
                    .order_by(TrackedAccount.id.asc())
                )
            )
            .scalars()
            .all()
        )
        # Create a small-baseline account (median_48h = 1,200) to test the 5,000-view floor rule
        small_acct = TrackedAccount(
            workspace_id=ws2.id,
            x_user_id="299999001",
            handle="niche_rust_dev",
            display_name="Niche Rust Dev",
            followers_count=6_000,
            is_tracked=True,
            baseline_curves_json=_build_baseline_curves(1_200),
        )
        session.add(small_acct)
        await session.flush()

        # Add 24 matured posts to Workspace 2 -> 16 + 24 = 40 matured posts total:
        #   i == 0: 4,200 views on median 1,200 (3.5x >= 3.0x, BUT < 5,000 floor -> NOT breakout!)
        #   i == 1: 12,000 views on large author (e.g. < 3.0x median, >= 5,000 floor -> NOT breakout!)
        for extra_i in range(24):
            if extra_i == 0:
                author = small_acct
                final_v = 4_200  # 3.5x median (1,200), but < 5,000 floor => False
                b_score = 74.0   # Predicted positive => FP
            elif extra_i == 1:
                author = ws2_accts[0]
                med = int(author.baseline_curves_json["p50"]["2880"])
                final_v = max(6_000, int(med * 2.4))  # >= 5,000 floor, but 2.4x < 3.0x => False
                b_score = 45.0   # Predicted negative => TN
            elif extra_i % 4 == 0:
                author = ws2_accts[extra_i % len(ws2_accts)]
                med = int(author.baseline_curves_json["p50"]["2880"])
                final_v = max(6_500, int(med * 3.8))  # >= 3.0x AND >= 5,000 => True
                b_score = 52.0   # Slow burn (<64 watch) => FN
            else:
                author = ws2_accts[extra_i % len(ws2_accts)]
                med = int(author.baseline_curves_json["p50"]["2880"])
                is_hit = extra_i % 3 == 0
                final_v = max(7_000, int(med * 4.1)) if is_hit else int(med * 1.2)
                b_score = 81.0 if is_hit else 44.0

            c_at = now - timedelta(hours=54 + extra_i * 2)
            p_new = Post(
                workspace_id=ws2.id,
                x_post_id=str(199910000 + extra_i),
                author_account_id=author.id,
                text=f"WS2 historical trajectory #{extra_i} on distributed ledgers & mTLS.",
                created_at_x=c_at,
                embedding_json=compute_text_embedding(f"ws2 post {extra_i}"),
                latest_views=final_v,
                latest_likes=int(final_v * 0.02),
                latest_replies=int(final_v * 0.003),
                latest_quotes=int(final_v * 0.0015),
                latest_reposts=int(final_v * 0.002),
                latest_bookmarks=int(final_v * 0.005),
                breakout_score=b_score,
                velocity_z_score=2.5 if b_score >= 64.0 else 0.4,
                seismic_infectiousness=0.68 if b_score >= 64.0 else 0.28,
                value_model_score=0.85,
                dpp_novelty_multiplier=0.92,
                catalysis_boost=0.22 if b_score >= 75.0 else 0.04,
                matured_48h=False,  # age >= 54h so grader must mature & label it automatically!
                actual_48h_views=None,
            )
            session.add(p_new)
            await session.flush()
            session.add(
                PostSnapshot(
                    post_id=p_new.id,
                    age_minutes=30,
                    captured_at=c_at + timedelta(minutes=30),
                    views=max(80, int(final_v * 0.25)),
                    likes=int(final_v * 0.005),
                    replies=int(final_v * 0.0008),
                    quotes=int(final_v * 0.0003),
                    reposts=int(final_v * 0.0005),
                    bookmarks=int(final_v * 0.0015),
                    breakout_score_at_capture=b_score,
                )
            )
        await session.commit()

        sc2 = await compute_workspace_scorecard(session, ws2.id, auto_grade=True)
        assert sc2["overall"]["matured_posts_count"] == 40
        assert len(sc2["communities"]) == 4
        assert sum(b["count"] for b in sc2["calibration_curve"]) == 40

        # Check exact floor & multiple boundary cases
        floor_blocked = (
            await session.execute(select(Post).where(Post.x_post_id == "199910000"))
        ).scalar_one()
        assert floor_blocked.matured_48h is True
        assert floor_blocked.actual_48h_multiple >= 3.0
        assert floor_blocked.actual_48h_views == 4_200
        assert floor_blocked.is_true_breakout_48h is False

        mult_blocked = (
            await session.execute(select(Post).where(Post.x_post_id == "199910001"))
        ).scalar_one()
        assert mult_blocked.actual_48h_views >= 5_000
        assert mult_blocked.actual_48h_multiple < 3.0
        assert mult_blocked.is_true_breakout_48h is False


async def test_auto_tuner_chronological_holdout_drift_cap_refusal_and_rollback(
    db_and_client: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
) -> None:
    session_factory, _ = db_and_client

    # Unit check on sign-safe +-20% drift clamp helper (positive & negative weights)
    assert clamp_with_drift_cap(25.0, 15.0, 0.20) == pytest.approx(18.0)
    assert clamp_with_drift_cap(5.0, 15.0, 0.20) == pytest.approx(12.0)
    assert clamp_with_drift_cap(-100.0, -47.52, 0.20) == pytest.approx(-57.024)
    assert clamp_with_drift_cap(-10.0, -47.52, 0.20) == pytest.approx(-38.016)

    async with session_factory() as session:
        workspaces = list(
            (await session.execute(select(Workspace).order_by(Workspace.id.asc()))).scalars().all()
        )
        ws1, ws2 = workspaces[0], workspaces[1]

        initial_active = await get_active_algo_version(session, ws1.id)
        assert initial_active is not None
        initial_id = initial_active.id

        # 1. Refusal when matured posts < min_matured_posts (seeded WS2 has 16 < 30)
        refused_ws2 = await run_auto_calibration(session, ws2.id, min_matured_posts=30)
        assert refused_ws2["status"] == "insufficient_data"
        assert refused_ws2["promoted"] is False
        assert refused_ws2["version"] is None
        assert refused_ws2["matured_posts_count"] == 16

        # Also test refusal on WS1 when min_matured_posts=80 (> 50)
        refused_ws1 = await run_auto_calibration(session, ws1.id, min_matured_posts=80)
        assert refused_ws1["status"] == "insufficient_data"
        assert refused_ws1["version"] is None

        # 2. Run auto-tuner on WS1 (50 matured posts: 40 oldest train / 10 newest holdout)
        tuned = await run_auto_calibration(
            session, ws1.id, min_matured_posts=30, l2_reg=0.15, max_drift_pct=0.20
        )
        assert tuned["status"] in ("promoted", "staged")
        assert tuned["matured_posts_count"] == 50
        split = tuned["split"]
        assert split["train_count"] == 40
        assert split["holdout_count"] == 10
        assert split["zero_future_leakage"] is True
        assert split["train_end_at"] <= split["holdout_start_at"]

        # Verify strict +-20% single-run parameter drift cap across EVERY diff
        assert tuned["max_observed_drift_pct"] <= 20.0 + 1e-6
        for diff in tuned["param_diffs"]:
            if diff["pct_change"] is not None:
                assert abs(diff["pct_change"]) <= 20.0 + 1e-3

        created_ver = tuned["version"]
        assert created_ver is not None
        assert created_ver["source"] == "auto_tuner"
        new_ver_id = created_ver["id"]

        # Ensure created version can be activated (if staged) and verify WS2 remains isolated
        if not tuned["promoted"]:
            await rollback_to_version(session, ws1.id, new_ver_id, reason="Test promote staged")

        active_after_tune = await get_active_algo_version(session, ws1.id)
        assert active_after_tune is not None
        assert active_after_tune.id == new_ver_id

        ws2_active = await get_active_algo_version(session, ws2.id)
        assert ws2_active is not None
        assert ws2_active.id == initial_id  # WS2 unaffected by WS1 tuning!

        # 3. 1-Click Rollback restores exact previous AlgoParamVersion (9106e9f baseline)
        rb_res = await rollback_to_version(
            session, ws1.id, initial_id, reason="Rollback to 9106e9f baseline"
        )
        assert rb_res["previous_version_id"] == new_ver_id
        assert rb_res["active_version_id"] == initial_id

        restored_active = await get_active_algo_version(session, ws1.id)
        assert restored_active is not None
        assert restored_active.id == initial_id


def test_vectorized_10k_benchmark_under_2s_and_single_candidate_parity() -> None:
    """Explicit performance benchmark (<2.0s for 10,000 trajectories) + exact xai-value-model parity."""
    # 1. Single-candidate parity check across diverse post structures, media, URLs, mutuals, OON, & flags
    rng = np.random.default_rng(42)
    n_parity = 120
    mat = synthesize_benchmark_trajectories(n_parity, seed=42)
    # Exercise replies, retweets, non-MP4 GIFs, short vs long videos, and zero-view edge case
    mat.is_reply[:15] = 1.0
    mat.is_retweet[15:25] = 1.0
    mat.views_at_eval[0] = 0.0
    mat.video_dur_ms[25:40] = 6_000.0  # < 10,000ms min_video_duration_ms

    for weights_variant in (
        ValueModelWeights.vm_ranker_defaults(),
        ValueModelWeights.home_mixer_defaults(),
        ValueModelWeights(
            **{
                **ValueModelWeights.vm_ranker_defaults().to_dict(),
                "vqv": 1.75,
                "post_video": 0.25,
                "profile_visit_secs": 0.12,
                "video_continuation": 0.08,
                "bidirectional_follow_dwell_weight_boost": 0.4,
                "multiplier_pre_offset": True,
            }
        ),
        ValueModelWeights(
            **{
                **ValueModelWeights.vm_ranker_defaults().to_dict(),
                "not_interested": -450.0,
                "not_dwelled": -2.5,
            }
        ),
    ):
        surrogate = SurrogateCalibrationParams(
            prior_views_alpha0=180.0,
            copy_link_from_bookmark_mult=0.44,
            url_dwell_discount=0.68,
        )
        pipe = PipelineParams(oon_weight_factor=0.75, dpp_theta=0.65)
        cfg = ResolvedAlgoConfig(
            version_id=1,
            version_number=1,
            label="Parity Check",
            source="test",
            upstream_commit_sha="9106e9f",
            weights=weights_variant,
            surrogate=surrogate,
            pipeline=pipe,
            fusion=FusionCalibrationParams(),
        )

        vec_out = vectorized_score_trajectories(mat, cfg, batch_author_diversity=False)

        ctx = QueryScoringContext(effective_oon_weight=pipe.oon_weight_factor)
        scalar_vm_scores = np.empty(n_parity, dtype=np.float64)
        for i in range(n_parity):
            has_photo = bool(mat.is_photo[i] > 0.5)
            has_vid = bool(mat.is_video[i] > 0.5)
            has_media = has_photo or has_vid
            m_type = "video" if bool(mat.is_video_mp4[i] > 0.5) else ("photo" if has_photo else None)
            vid_ms = int(mat.video_dur_ms[i]) if mat.video_dur_ms[i] > 0 else None

            # Reconstruct exact text with matching char_len and line_breaks
            target_len = max(5, int(mat.char_len[i]))
            n_breaks = min(int(mat.line_breaks[i]), target_len - 2)
            synth_text = "a" + ("\n" * n_breaks) + ("b" * (target_len - 1 - n_breaks))
            assert len(synth_text.strip()) == target_len
            assert synth_text.count("\n") == n_breaks

            obs = ObservablePostMetrics(
                views=int(mat.views_at_eval[i]),
                likes=int(mat.likes[i]),
                replies=int(mat.replies[i]),
                reposts=int(mat.reposts[i]),
                quotes=int(mat.quotes[i]),
                bookmarks=int(mat.bookmarks[i]),
                text=synth_text,
                has_url=bool(mat.has_url[i] > 0.5),
                has_media=has_media,
                media_type=m_type,
                video_duration_ms=vid_ms,
            )
            cand_in = build_candidate_scoring_inputs(
                obs,
                author_id=int(mat.author_ids[i]),
                in_network=bool(mat.in_network[i] > 0.5),
                is_reply=bool(mat.is_reply[i] > 0.5),
                is_retweet=bool(mat.is_retweet[i] > 0.5),
                is_mutual_follow_author=bool(mat.is_mutual[i] > 0.5),
                min_video_duration_ms=weights_variant.min_video_duration_ms,
                params=surrogate,
            )
            res = compute_value_scores(weights_variant, ctx, [cand_in])
            scalar_vm_scores[i] = res.scores[0]

        np.testing.assert_allclose(vec_out.vm_scores, scalar_vm_scores, rtol=1e-12, atol=1e-12)

    # 2. Explicit 10,000-trajectory wall-clock benchmark (< 2.0 seconds)
    matrix_10k = synthesize_benchmark_trajectories(10_000, seed=20261101)
    assert matrix_10k.size == 10_000
    assert matrix_10k.views_at_eval.dtype == np.float64
    assert matrix_10k.views_at_eval.flags["C_CONTIGUOUS"]

    live_cfg = ResolvedAlgoConfig(
        version_id=1,
        version_number=1,
        label="Live 9106e9f",
        source="upstream_xalgo",
        upstream_commit_sha="9106e9f",
        weights=ValueModelWeights.vm_ranker_defaults(),
        surrogate=SurrogateCalibrationParams(),
        pipeline=PipelineParams(),
        fusion=FusionCalibrationParams(),
    )
    cand_cfg = ResolvedAlgoConfig(
        version_id=2,
        version_number=2,
        label="Candidate",
        source="manual_sandbox",
        upstream_commit_sha="9106e9f",
        weights=ValueModelWeights.home_mixer_defaults(),
        surrogate=SurrogateCalibrationParams(copy_link_from_bookmark_mult=0.48),
        pipeline=PipelineParams(dpp_theta=0.55),
        fusion=FusionCalibrationParams(),
        watch_threshold=61.0,
    )

    t0 = time.perf_counter()
    live_10k = vectorized_score_trajectories(matrix_10k, live_cfg)
    cand_10k = vectorized_score_trajectories(matrix_10k, cand_cfg)
    elapsed_secs = time.perf_counter() - t0

    assert elapsed_secs < 2.0, f"10k replay took {elapsed_secs:.3f}s, exceeding 2.0s budget!"
    assert live_10k.metrics["matured_posts_count"] == 10_000
    assert cand_10k.metrics["matured_posts_count"] == 10_000
    assert cand_10k.metrics["alerts_total"] > live_10k.metrics["alerts_total"]


async def test_epic3_fastapi_routes_and_auth(
    db_and_client: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, client = db_and_client

    ws_list = (await client.get("/api/workspaces")).json()
    ws1_id = ws_list[0]["id"]

    # 1. GET /scorecard & POST /scorecard/grade
    sc_resp = await client.get(f"/api/workspaces/{ws1_id}/scorecard")
    assert sc_resp.status_code == 200
    sc_data = sc_resp.json()
    assert sc_data["overall"]["matured_posts_count"] == 50
    assert len(sc_data["communities"]) == 3
    assert len(sc_data["calibration_curve"]) == 10

    grade_resp = await client.post(
        f"/api/workspaces/{ws1_id}/scorecard/grade",
        json={"breakout_multiple": 3.0, "community_floor_views": 5000},
    )
    assert grade_resp.status_code == 200
    assert grade_resp.json()["grade_summary"]["matured_posts_graded"] == 50

    # 2. GET /changelog (initial 9106e9f baseline)
    cl_init = await client.get(f"/api/workspaces/{ws1_id}/changelog")
    assert cl_init.status_code == 200
    cl_versions = cl_init.json()["versions"]
    assert len(cl_versions) >= 1
    baseline_id = cl_versions[0]["id"]

    # 3. POST /backtest/replay (candidate threshold lowering catches slow burns + 10k benchmark)
    replay_resp = await client.post(
        f"/api/workspaces/{ws1_id}/backtest/replay",
        json={
            "watch_threshold": 53.0,
            "share_via_copy_link": 26.0,
            "copy_link_from_bookmark_mult": 0.46,
            "dpp_theta": 0.60,
        },
    )
    assert replay_resp.status_code == 200
    rep = replay_resp.json()
    assert rep["trajectories_evaluated"] == 50
    assert rep["flips_summary"]["fn_to_tp"] > 0
    assert any(row["transition"] == "fn_to_tp" for row in rep["post_diffs"])

    bench_resp = await client.post(
        f"/api/workspaces/{ws1_id}/backtest/replay",
        json={"watch_threshold": 62.0, "benchmark_10k": True},
    )
    assert bench_resp.status_code == 200
    bench = bench_resp.json()
    assert bench["benchmark_10k"] is True
    assert bench["trajectories_evaluated"] == 10_000
    assert bench["elapsed_secs"] < 2.0

    # 4. POST /backtest/promote
    prom_resp = await client.post(
        f"/api/workspaces/{ws1_id}/backtest/promote",
        json={
            "watch_threshold": 63.0,
            "share_via_copy_link": 24.0,
            "label": "High Copy-Link Sensitivity",
        },
    )
    assert prom_resp.status_code == 200
    prom = prom_resp.json()
    assert prom["promoted"] is True
    assert prom["version"]["source"] == "manual_sandbox"
    sandbox_ver_id = prom["version"]["id"]

    # 5. POST /calibrate
    cal_resp = await client.post(
        f"/api/workspaces/{ws1_id}/calibrate",
        json={"min_matured_posts": 30, "max_drift_pct": 0.20},
    )
    assert cal_resp.status_code == 200
    cal_data = cal_resp.json()
    assert cal_data["status"] in ("promoted", "staged")
    assert cal_data["max_observed_drift_pct"] <= 20.0 + 1e-6

    # 6. POST /changelog/{version_id}/activate (1-click rollback to baseline)
    act_resp = await client.post(
        f"/api/workspaces/{ws1_id}/changelog/{baseline_id}/activate",
        json={"reason": "Restore baseline after test"},
    )
    assert act_resp.status_code == 200
    assert act_resp.json()["active_version_id"] == baseline_id

    # Re-activate sandbox version & verify changelog listing reflects effective active
    act_sb = await client.post(f"/api/workspaces/{ws1_id}/changelog/{sandbox_ver_id}/activate")
    assert act_sb.status_code == 200
    cl_after = (await client.get(f"/api/workspaces/{ws1_id}/changelog")).json()
    assert cl_after["active_version_id"] == sandbox_ver_id

    # 7. Admin auth protection check on Epic 3 endpoints
    monkeypatch.setenv("TWEETTRACKER_ADMIN_PASSWORD", "epic3-lock-secret")
    unauth = await client.get(f"/api/workspaces/{ws1_id}/scorecard")
    assert unauth.status_code == 401
    authed = await client.get(
        f"/api/workspaces/{ws1_id}/scorecard",
        headers={"Authorization": "Bearer epic3-lock-secret"},
    )
    assert authed.status_code == 200


async def test_stage_only_upstream_sync_activation_and_noise_cut_flips(
    db_and_client: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
) -> None:
    session_factory, client = db_and_client
    ws_list = (await client.get("/api/workspaces")).json()
    ws1_id = ws_list[0]["id"]

    # 1. Raising watch_threshold in replay eliminates false alarms (fp -> tn)
    cut_resp = await client.post(
        f"/api/workspaces/{ws1_id}/backtest/replay",
        json={"watch_threshold": 86.0, "surge_threshold": 94.0},
    )
    assert cut_resp.status_code == 200
    cut_data = cut_resp.json()
    assert cut_data["flips_summary"]["fp_to_tn"] > 0
    assert any(r["transition"] == "fp_to_tn" for r in cut_data["post_diffs"])

    # 2. force_stage_only=True creates staged AlgoParamVersion without replacing active version
    before_cl = (await client.get(f"/api/workspaces/{ws1_id}/changelog")).json()
    prev_active_id = before_cl["active_version_id"]

    staged_cal = await client.post(
        f"/api/workspaces/{ws1_id}/calibrate",
        json={"min_matured_posts": 30, "force_stage_only": True},
    )
    assert staged_cal.status_code == 200
    sc_json = staged_cal.json()
    assert sc_json["status"] == "staged"
    assert sc_json["promoted"] is False
    assert sc_json["version"]["is_active"] is False

    after_cl = (await client.get(f"/api/workspaces/{ws1_id}/changelog")).json()
    assert after_cl["active_version_id"] == prev_active_id

    # 3. Stage an upstream algo-sync global version and 1-click activate it
    async with session_factory() as session:
        upstream_staged = AlgoParamVersion(
            workspace_id=None,
            version_number=99,
            label="xai-org/x-algorithm @ a1b2c3d (staged sync)",
            source="upstream_xalgo",
            upstream_commit_sha="a1b2c3d",
            weights_json=ValueModelWeights.home_mixer_defaults().to_dict(),
            surrogate_params_json=SurrogateCalibrationParams().to_dict(),
            thresholds_json={"watch_threshold": 66.0, "surge_threshold": 83.0},
            holdout_metrics_json={"precision": 0.80, "f05": 0.78, "brier": 0.13},
            changelog_note="Upstream nightly sync staged for operator review.",
            is_active=False,
        )
        session.add(upstream_staged)
        await session.commit()
        await session.refresh(upstream_staged)
        up_id = upstream_staged.id

    act_up = await client.post(f"/api/workspaces/{ws1_id}/changelog/{up_id}/activate")
    assert act_up.status_code == 200
    act_up_json = act_up.json()
    assert act_up_json["activated_version"]["workspace_id"] == ws1_id
    assert act_up_json["activated_version"]["upstream_commit_sha"] == "a1b2c3d"
    assert act_up_json["active_version_id"] == act_up_json["activated_version"]["id"]

    # 4. 404 checks for missing workspace and missing version
    assert (await client.get("/api/workspaces/99999/scorecard")).status_code == 404
    assert (
        await client.post(f"/api/workspaces/{ws1_id}/changelog/99999/activate")
    ).status_code == 404



def test_cap_alert_thresholds_keeps_alerts_reachable() -> None:
    from tweettracker.backtest.tuner import cap_alert_thresholds

    # 2026-11-03 production values: surge 118.15 could never fire on a 0-100 score
    assert cap_alert_thresholds(93.65, 118.15) == (93.65, 99.0)
    assert cap_alert_thresholds(97.0, 98.0) == (95.0, 98.0)
    assert cap_alert_thresholds(99.0, 100.0) == (95.0, 99.0)
    assert cap_alert_thresholds(65.0, 82.0) == (65.0, 82.0)


async def test_auto_tuner_refuses_without_enough_true_breakouts(
    db_and_client: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
) -> None:
    """Seeded WS2 has 16 matured posts but only 5 true breakouts: no tuning, thresholds untouched."""
    session_factory, _ = db_and_client
    async with session_factory() as session:
        ws2 = (await session.execute(select(Workspace).order_by(Workspace.id.asc()))).scalars().all()[1]
        before = (ws2.watch_threshold, ws2.surge_threshold)
        res = await run_auto_calibration(session, ws2.id, min_matured_posts=10)
        assert res["status"] == "insufficient_breakouts"
        assert res["true_breakouts_count"] == 5
        assert res["promoted"] is False
        assert res["version"] is None
        await session.refresh(ws2)
        assert (ws2.watch_threshold, ws2.surge_threshold) == before


async def test_auto_tuner_moves_thresholds_only_on_alert_evidence_and_under_ceiling(
    db_and_client: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
) -> None:
    """Starting from the runaway production thresholds, a tuning run may only move the alert
    thresholds when holdout F0.5 improves, and never above the 95 / 99 ceilings."""
    session_factory, _ = db_and_client
    async with session_factory() as session:
        ws1 = (await session.execute(select(Workspace).order_by(Workspace.id.asc()))).scalars().all()[0]
        active = await get_active_algo_version(session, ws1.id)
        active.thresholds_json = {
            **(active.thresholds_json or {}),
            "watch_threshold": 93.65,
            "surge_threshold": 118.15,
        }
        ws1.watch_threshold, ws1.surge_threshold = 93.65, 118.15
        await session.commit()

        res = await run_auto_calibration(session, ws1.id, min_matured_posts=30, max_drift_pct=0.20)
        assert res["status"] in ("promoted", "staged")
        ver_thresholds = res["version"]["thresholds"]
        assert ver_thresholds["watch_threshold"] <= 95.0
        assert ver_thresholds["surge_threshold"] <= 99.0
        assert ver_thresholds["surge_threshold"] > ver_thresholds["watch_threshold"]

        f05_improved = (
            res["candidate_holdout_metrics"]["f05_score"]
            > res["live_holdout_metrics"]["f05_score"] + 1e-4
        )
        if not f05_improved:
            # Held at the live values, only clamped under the ceiling
            assert ver_thresholds["watch_threshold"] == 93.65
            assert ver_thresholds["surge_threshold"] == 99.0

        await session.refresh(ws1)
        assert ws1.watch_threshold <= 95.0
        if res["promoted"]:
            assert ws1.surge_threshold <= 99.0
