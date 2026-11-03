"""Dedicated security, math-parity, graph-isolation, and concurrency regression tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from copy import copy
from datetime import timedelta
import socket
import time

import httpx
import numpy as np
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tweettracker.algo import (
    DEFAULT_DPP_THETA,
    SurrogateCalibrationParams,
    ValueModelWeights,
    compute_text_embedding,
    dpp_novelty_multiplier,
)
from tweettracker.api.deps import create_sse_ticket
from tweettracker.api.main import create_app
from tweettracker.backtest import (
    get_active_algo_version,
    pack_workspace_trajectories,
    promote_candidate_params,
    config_from_version,
    vectorized_score_trajectories,
)

from tweettracker.db import configure_database, dispose_database
from tweettracker.graph import recompute_workspace_graph
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
from tweettracker.radar import (
    dispatch_signed_webhook,
    load_active_algo_bundle,
    score_single_post,
    validate_and_resolve_webhook_target,
    verify_hmac_signature,
)
from tweettracker.radar.scorer import _stable_int_id
from tweettracker.seed import bootstrap_database


@pytest.fixture
async def hardened_env(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient], None]:
    from pathlib import Path

    db_file = Path(str(tmp_path)) / "hardened.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.delenv("TWEETTRACKER_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("TWEETTRACKER_ALLOW_HTTP_WEBHOOKS", raising=False)
    monkeypatch.delenv("TWEETTRACKER_ALLOW_LOCAL_WEBHOOKS", raising=False)
    monkeypatch.setenv("TWEETTRACKER_WEBHOOK_SECRET", "hardening-regression-hmac-secret")

    engine, session_factory = configure_database(db_url)
    await bootstrap_database(engine, session_factory, auto_migrate=True, seed_fixtures=True)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield session_factory, client

    await dispose_database()



# =============================================================================
# 1. Outbound Webhook SSRF, Credential, & Redirect Hardening ([CRITICAL-01])
# =============================================================================


@respx.mock
async def test_webhook_ssrf_and_redirect_rejection(
    hardened_env: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory, client = hardened_env
    workspaces = (await client.get("/api/workspaces")).json()
    ws1_id = workspaces[0]["id"]

    blocked_urls = [
        "http://127.0.0.1:8080/hook",
        "http://169.254.169.254/latest/meta-data/",
        "https://10.0.0.1/internal-webhook",
        "https://user:pass@hooks.example.com/secret",
        "https://localhost:8443/hook",
        "https://hooks.example.com/hook#fragment",
    ]

    for bad_url in blocked_urls:
        patch_resp = await client.patch(
            f"/api/workspaces/{ws1_id}",
            json={"webhook_urls_json": [bad_url]},
        )
        assert patch_resp.status_code == 422, f"Expected 422 for {bad_url}, got {patch_resp.status_code}"

        test_resp = await client.post(
            f"/api/workspaces/{ws1_id}/webhooks/test",
            json={"target_url": bad_url},
        )
        assert test_resp.status_code == 422, f"Expected 422 for {bad_url}, got {test_resp.status_code}"

    # Exceeding max 10 webhooks per workspace is rejected
    too_many = [f"https://hooks.example.com/ep-{i}" for i in range(11)]
    many_resp = await client.patch(
        f"/api/workspaces/{ws1_id}",
        json={"webhook_urls_json": too_many},
    )
    assert many_resp.status_code == 422

    # DNS-rebinding defense: public hostname resolving to loopback / link-local IP is blocked
    def _fake_rebind_addrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_rebind_addrinfo)
    with pytest.raises(ValueError, match="disallowed private/internal IP"):
        await validate_and_resolve_webhook_target("https://rebind-attacker.com/hook")

    # Restore offline-safe RFC 2606 resolution for respx mock test
    def _offline_gaierror(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        raise socket.gaierror("offline test")

    monkeypatch.setattr(socket, "getaddrinfo", _offline_gaierror)

    # Personal single-operator escape hatch: TWEETTRACKER_ALLOW_LOCAL_WEBHOOKS=true allows
    # local n8n (localhost / 127.0.0.1) while STILL blocking cloud metadata (169.254.169.254)
    monkeypatch.setenv("TWEETTRACKER_ALLOW_LOCAL_WEBHOOKS", "true")
    local_ok = await client.patch(
        f"/api/workspaces/{ws1_id}",
        json={"webhook_urls_json": ["http://localhost:5678/webhook/n8n"]},
    )
    assert local_ok.status_code == 200
    meta_still_blocked = await client.patch(
        f"/api/workspaces/{ws1_id}",
        json={"webhook_urls_json": ["http://169.254.169.254/latest/meta-data/"]},
    )
    assert meta_still_blocked.status_code == 422
    monkeypatch.delenv("TWEETTRACKER_ALLOW_LOCAL_WEBHOOKS", raising=False)

    # Open redirect (302 -> metadata IP) must NOT be followed
    redirect_source = "https://hooks.example.com/redirect-trap"
    metadata_target = "http://169.254.169.254/latest/meta-data/"
    respx.post(redirect_source).mock(
        return_value=httpx.Response(302, headers={"Location": metadata_target})
    )
    meta_route = respx.route(url=metadata_target).mock(return_value=httpx.Response(200, text="secret"))

    async with session_factory() as session:
        ws1 = await session.get(Workspace, ws1_id)
        assert ws1 is not None
        outcome = await dispatch_signed_webhook(
            session,
            workspace_id=ws1.id,
            post_id=None,
            target_url=redirect_source,
            event_type="radar.webhook.test",
            alert_tier="surge",
            payload={"event": "radar.webhook.test"},
            max_attempts=1,
        )
        assert outcome.delivery.status == "failed"
        assert outcome.delivery.http_status == 302
        assert meta_route.call_count == 0

    # Empty webhook secret marks delivery unsigned and verify_hmac_signature returns False
    monkeypatch.setenv("TWEETTRACKER_WEBHOOK_SECRET", "")
    assert verify_hmac_signature(b'{"a":1}', "sha256=" + "0" * 64, secret="") is False
    ok_target = "https://hooks.example.com/unsigned-check"
    respx.post(ok_target).mock(return_value=httpx.Response(200, json={"ok": True}))
    unsigned_resp = await client.post(
        f"/api/workspaces/{ws1_id}/webhooks/test",
        json={"target_url": ok_target},
    )
    assert unsigned_resp.status_code == 200
    assert unsigned_resp.json()["signed"] is False


# =============================================================================
# 2. Exact Numerical Parity: score_single_post vs vectorized_score_trajectories
#    ([CRITICAL-02], [HIGH-02])
# =============================================================================


async def _assert_live_vs_vectorized_parity_for_workspace(
    session: AsyncSession,
    workspace_id: int,
) -> None:
    ws = await session.get(Workspace, workspace_id)
    assert ws is not None

    matrix, metas = await pack_workspace_trajectories(session, workspace_id, matured_only=True)
    assert matrix.size == 50

    accounts = list(
        (
            await session.execute(
                select(TrackedAccount).where(TrackedAccount.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    accounts_by_id = {a.id: a for a in accounts}
    has_my_handle = bool(ws.my_x_handle and ws.my_x_handle.strip())
    followed_author_ids = (
        [a.id or _stable_int_id(a.x_user_id) for a in accounts if a.followed_by_me]
        if has_my_handle
        else []
    )

    posts_by_id = {
        p.id: p
        for p in (
            await session.execute(select(Post).where(Post.workspace_id == workspace_id))
        )
        .scalars()
        .all()
    }
    snaps_all = list(
        (
            await session.execute(
                select(PostSnapshot)
                .where(PostSnapshot.post_id.in_(list(posts_by_id.keys())))
                .order_by(PostSnapshot.age_minutes.asc(), PostSnapshot.id.asc())
            )
        )
        .scalars()
        .all()
    )
    snaps_by_post: dict[int, list[PostSnapshot]] = {}
    for s in snaps_all:
        snaps_by_post.setdefault(s.post_id, []).append(s)

    inters_all = list(
        (
            await session.execute(
                select(PostInteraction)
                .where(PostInteraction.workspace_id == workspace_id)
                .order_by(PostInteraction.occurred_at.asc(), PostInteraction.id.asc())
            )
        )
        .scalars()
        .all()
    )
    inters_by_post: dict[int, list[PostInteraction]] = {}
    for inter in inters_all:
        inters_by_post.setdefault(inter.post_id, []).append(inter)

    comms_by_id = {
        c.id: c
        for c in (
            await session.execute(select(Community).where(Community.workspace_id == workspace_id))
        )
        .scalars()
        .all()
    }

    # Build a deterministic reference embedding to exercise non-trivial DPP rescaling (< 1.0)
    ref_emb = compute_text_embedding("distributed rust inference kernels and kv cache quantized routing")
    ref_embeddings = [ref_emb]
    for i, meta in enumerate(metas):
        post_obj = posts_by_id[meta.post_id]
        emb_i = (
            list(post_obj.embedding_json)
            if post_obj.embedding_json
            else compute_text_embedding(post_obj.text or "")
        )
        matrix.dpp_mult[i] = dpp_novelty_multiplier(
            emb_i, ref_embeddings, theta=DEFAULT_DPP_THETA
        )

    algo_bundle = await load_active_algo_bundle(session, workspace_id)
    vec_cfg = config_from_version(await get_active_algo_version(session, workspace_id), ws)
    vec_out = vectorized_score_trajectories(matrix, vec_cfg)

    for i, meta in enumerate(metas):
        orig_post = posts_by_id[meta.post_id]
        author = accounts_by_id[meta.author_id]
        eval_age = meta.alert_age_minutes
        created_utc = (
            orig_post.created_at_x
            if orig_post.created_at_x.tzinfo is not None
            else orig_post.created_at_x.replace(tzinfo=utc_now().tzinfo)
        )
        eval_now = created_utc + timedelta(minutes=eval_age)

        # Clone post state at exact causal evaluation age (matching pack_workspace_trajectories)
        eval_post = copy(orig_post)
        eval_post.latest_views = int(matrix.views_at_eval[i])
        eval_post.latest_likes = int(matrix.likes[i])
        eval_post.latest_replies = int(matrix.replies[i])
        eval_post.latest_reposts = int(matrix.reposts[i])
        eval_post.latest_quotes = int(matrix.quotes[i])
        eval_post.latest_bookmarks = int(matrix.bookmarks[i])

        early_snaps = [s for s in snaps_by_post.get(orig_post.id, []) if s.age_minutes <= eval_age]
        early_inters = [
            inter
            for inter in inters_by_post.get(orig_post.id, [])
            if (
                inter.occurred_at
                if inter.occurred_at.tzinfo is not None
                else inter.occurred_at.replace(tzinfo=utc_now().tzinfo)
            )
            <= eval_now
        ]
        author_comms = [comms_by_id[c["id"]] for c in meta.communities if c["id"] in comms_by_id]

        live_res = score_single_post(
            ws,
            eval_post,
            author,
            communities=author_comms,
            snapshots=early_snaps,
            interactions=early_inters,
            reference_embeddings=ref_embeddings,
            algo_bundle=algo_bundle,
            actor_accounts_by_id=accounts_by_id,
            followed_author_ids=followed_author_ids,
            now=eval_now,
            enforce_age_48h_drop=True,
        )

        assert abs(live_res.value_model_score - float(vec_out.vm_scores[i])) < 1e-6, (
            f"VM mismatch on post {meta.post_id}: {live_res.value_model_score} vs {vec_out.vm_scores[i]}"
        )
        assert abs(live_res.dpp_novelty_multiplier - float(vec_out.effective_dpp_mult[i])) < 1e-6
        assert abs(live_res.breakout_score - float(vec_out.breakout_scores[i])) < 1e-6, (
            f"Score mismatch on post {meta.post_id}: {live_res.breakout_score} vs {vec_out.breakout_scores[i]}"
        )
        assert abs(live_res.breakout_probability - float(vec_out.breakout_probs[i])) < 1e-6, (
            f"Prob mismatch on post {meta.post_id}: {live_res.breakout_probability} vs {vec_out.breakout_probs[i]}"
        )


async def test_exact_numerical_parity_live_scorer_vs_vectorized_replay_with_custom_fusion(
    hardened_env: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
) -> None:
    session_factory, _client = hardened_env
    async with session_factory() as session:
        ws1 = (
            await session.execute(select(Workspace).order_by(Workspace.id.asc()).limit(1))
        ).scalar_one()

        # 1. Exact < 1e-6 parity on default baseline across all 50 Workspace 1 posts
        await _assert_live_vs_vectorized_parity_for_workspace(session, ws1.id)

        # 2. Promote custom fusion + weights (including Head 20 user_video_continuation & pre-offset)
        #    + surrogate + pipeline (custom dpp_theta) parameters, then verify < 1e-6 parity again!
        await promote_candidate_params(
            session,
            ws1.id,
            overrides={
                "weights": {
                    "favorite": 0.85,
                    "reply": 6.2,
                    "user_video_continuation": 0.18,
                    "multiplier_pre_offset": True,
                },
                "surrogate": {
                    "dwell_max_secs": 34.0,
                    "prior_views_alpha0": 180.0,
                },
                "pipeline": {
                    "oon_weight_factor": 0.71,
                    "dpp_theta": 0.72,
                },
                "fusion": {
                    "vel_scale": 2.9,
                    "vel_weight": 34.0,
                    "seismic_weight": 21.5,
                    "vm_scale": 5.6,
                    "vm_weight": 26.5,
                    "cat_weight": 11.0,
                    "cold_start_bonus": 4.25,
                    "prob_center": 60.5,
                    "prob_scale": 10.2,
                },
                "watch_threshold": 63.0,
                "surge_threshold": 81.0,
            },
            label="Custom Fusion & DPP Parity Promotion",
        )

        await _assert_live_vs_vectorized_parity_for_workspace(session, ws1.id)


# =============================================================================
# 3. Graph Recomputation Community & Bridge Subset Purity ([HIGH-01])
# =============================================================================


async def test_graph_recompute_does_not_pollute_non_member_communities(
    hardened_env: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
) -> None:
    session_factory, client = hardened_env
    async with session_factory() as session:
        workspaces = list(
            (await session.execute(select(Workspace).order_by(Workspace.id.asc()))).scalars().all()
        )
        ws1 = workspaces[0]
        await recompute_workspace_graph(session, ws1.id)
        await session.commit()

        memberships = list(
            (
                await session.execute(
                    select(AccountCommunityMembership).where(
                        AccountCommunityMembership.workspace_id == ws1.id
                    )
                )
            )
            .scalars()
            .all()
        )
        # In K=3 workspace, every persisted membership must be manual or >= 0.25 NMF affinity
        for m in memberships:
            assert m.is_manual_tag or max(m.nmf_known_for, m.nmf_interested_in) >= 0.25

        ws1_comms = list(
            (
                await session.execute(
                    select(Community)
                    .where(Community.workspace_id == ws1.id)
                    .order_by(Community.sort_order.asc())
                )
            )
            .scalars()
            .all()
        )

    all_feed = (
        await client.get(f"/api/workspaces/{ws1.id}/radar?include_matured=true&limit=200")
    ).json()
    total_items = len(all_feed["items"])
    assert total_items > 10

    for comm in ws1_comms:
        comm_feed = (
            await client.get(
                f"/api/workspaces/{ws1.id}/radar?include_matured=true&limit=200&community_id={comm.id}"
            )
        ).json()
        comm_count = len(comm_feed["items"])
        assert 0 < comm_count < total_items, (
            f"Community {comm.slug} returned {comm_count}/{total_items} posts after graph recompute"
        )
        for item in comm_feed["items"]:
            comm_ids = {c["id"] for c in item["author"]["communities"]}
            assert comm.id in comm_ids

    bridges_feed = (
        await client.get(
            f"/api/workspaces/{ws1.id}/radar?include_matured=true&limit=200&bridges_only=true"
        )
    ).json()
    bridge_count = len(bridges_feed["items"])
    assert 0 < bridge_count < total_items
    for item in bridges_feed["items"]:
        assert item["author"]["is_bridge"] is True


# =============================================================================
# 4. Cross-Workspace Rollback / Activation Isolation ([HIGH-06])
# =============================================================================


async def test_rollback_to_inactive_global_version_isolates_workspaces(
    hardened_env: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
) -> None:
    session_factory, client = hardened_env
    async with session_factory() as session:
        workspaces = list(
            (await session.execute(select(Workspace).order_by(Workspace.id.asc()))).scalars().all()
        )
        ws1, ws2 = workspaces[0], workspaces[1]
        ws1_orig_watch = ws1.watch_threshold
        ws1_orig_surge = ws1.surge_threshold

        global_base = await get_active_algo_version(session, ws1.id)
        assert global_base is not None
        assert global_base.workspace_id is None
        assert global_base.is_active is True

        staged_global = AlgoParamVersion(
            workspace_id=None,
            version_number=99,
            label="Staged Global Upstream Candidate",
            source="upstream_algo_sync",
            upstream_commit_sha="deadbeef1234",
            weights_json=ValueModelWeights.home_mixer_defaults().to_dict(),
            surrogate_params_json=SurrogateCalibrationParams().to_dict(),
            thresholds_json={"watch_threshold": 61.0, "surge_threshold": 79.0},
            holdout_metrics_json={},
            changelog_note="Staged global candidate.",
            is_active=False,
        )
        session.add(staged_global)
        await session.commit()
        await session.refresh(staged_global)
        staged_id = staged_global.id

    act_resp = await client.post(
        f"/api/workspaces/{ws2.id}/changelog/{staged_id}/activate",
        json={"reason": "Activate staged candidate on WS2 only"},
    )
    assert act_resp.status_code == 200
    act_json = act_resp.json()
    cloned_id = act_json["active_version_id"]
    assert cloned_id != staged_id
    assert act_json["activated_version"]["workspace_id"] == ws2.id

    async with session_factory() as session:
        # Global baseline and staged global row must NOT have been mutated
        refreshed_base = await session.get(AlgoParamVersion, global_base.id)
        refreshed_staged = await session.get(AlgoParamVersion, staged_id)
        assert refreshed_base is not None and refreshed_base.is_active is True
        assert refreshed_staged is not None and refreshed_staged.is_active is False
        assert refreshed_staged.workspace_id is None

        # Workspace 1 remains on the original global baseline with untouched thresholds
        ws1_active = await get_active_algo_version(session, ws1.id)
        ws1_reloaded = await session.get(Workspace, ws1.id)
        assert ws1_active is not None and ws1_active.id == global_base.id
        assert ws1_reloaded is not None
        assert ws1_reloaded.watch_threshold == ws1_orig_watch
        assert ws1_reloaded.surge_threshold == ws1_orig_surge

        # Workspace 2 uses its workspace-scoped clone
        ws2_active = await get_active_algo_version(session, ws2.id)
        ws2_reloaded = await session.get(Workspace, ws2.id)
        assert ws2_active is not None and ws2_active.id == cloned_id
        assert ws2_active.workspace_id == ws2.id
        assert ws2_reloaded is not None
        assert ws2_reloaded.watch_threshold == 61.0
        assert ws2_reloaded.surge_threshold == 79.0


# =============================================================================
# 5. K >= 1 Community Deletion Guard & Threshold Validation ([MEDIUM-01])
# =============================================================================


async def test_last_community_deletion_guard_k_ge_1(
    hardened_env: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
) -> None:
    _session_factory, client = hardened_env

    # Creating a workspace with 0 initial communities is rejected (422)
    zero_comm_resp = await client.post(
        "/api/workspaces",
        json={"name": "Empty Circle WS", "initial_communities": []},
    )
    assert zero_comm_resp.status_code == 422

    # Create workspace with 2 communities, delete 1 (200), then block deleting the last 1 (400)
    create_resp = await client.post(
        "/api/workspaces",
        json={
            "name": "K1 Guard Workspace",
            "initial_communities": [
                {"name": "Alpha Circle", "color": "#0ea5e9"},
                {"name": "Beta Circle", "color": "#10b981"},
            ],
        },
    )
    assert create_resp.status_code == 201
    ws_data = create_resp.json()
    ws_id = ws_data["id"]
    c1_id, c2_id = ws_data["communities"][0]["id"], ws_data["communities"][1]["id"]

    del_first = await client.delete(f"/api/workspaces/{ws_id}/communities/{c1_id}")
    assert del_first.status_code == 204

    del_last = await client.delete(f"/api/workspaces/{ws_id}/communities/{c2_id}")
    assert del_last.status_code == 400
    assert "at least 1" in del_last.json()["detail"].lower()

    remaining = (await client.get(f"/api/workspaces/{ws_id}/communities")).json()
    assert len(remaining) == 1 and remaining[0]["id"] == c2_id


# =============================================================================
# 6. Backtest Numeric Validation: dwell_max_secs=0 & NaN/Inf Rejection ([MEDIUM-04])
# =============================================================================


async def test_backtest_replay_rejects_zero_dwell_and_non_finite_overrides(
    hardened_env: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
) -> None:
    _session_factory, client = hardened_env
    ws1_id = (await client.get("/api/workspaces")).json()[0]["id"]

    # dwell_max_secs=0 and negative values must be rejected with 422 (no ZeroDivisionError 500)
    zero_dwell = await client.post(
        f"/api/workspaces/{ws1_id}/backtest/replay",
        json={"overrides": {"surrogate": {"dwell_max_secs": 0}}},
    )
    assert zero_dwell.status_code == 422

    neg_dwell = await client.post(
        f"/api/workspaces/{ws1_id}/backtest/replay",
        json={"overrides": {"surrogate": {"dwell_max_secs": -2.5}}},
    )
    assert neg_dwell.status_code == 422

    # Inverted thresholds (watch >= surge) rejected with 422
    inverted_th = await client.post(
        f"/api/workspaces/{ws1_id}/backtest/replay",
        json={"overrides": {"watch_threshold": 88.0, "surge_threshold": 80.0}},
    )
    assert inverted_th.status_code == 422

    # Extreme / astronomical weight rejected with 422
    huge_weight = await client.post(
        f"/api/workspaces/{ws1_id}/backtest/replay",
        json={"overrides": {"weights": {"favorite": 1e9}}},
    )
    assert huge_weight.status_code == 422

    # Non-standard NaN JSON token rejected with 422
    nan_resp = await client.post(
        f"/api/workspaces/{ws1_id}/backtest/replay",
        content=b'{"overrides": {"weights": {"favorite": NaN}}}',
        headers={"Content-Type": "application/json"},
    )
    assert nan_resp.status_code == 422


# =============================================================================
# 7. SVD Rank-Revealing DPP Stability on Duplicate Embeddings ([MEDIUM-04])
# =============================================================================


def test_dpp_svd_stability_with_duplicate_and_collinear_embeddings() -> None:
    emb_a = compute_text_embedding("flashattention hopper fp8 tensor core kernel")
    emb_b = compute_text_embedding("venture seed fundraising valuation metrics")

    # Exact duplicate in reference set yields 0.0 novelty without NaN
    assert dpp_novelty_multiplier(emb_a, [emb_a], theta=0.65) == 0.0
    assert dpp_novelty_multiplier(emb_a, [emb_a, emb_a, emb_a], theta=0.65) == 0.0

    # Repeating a distinct reference embedding (or its negation) does not distort subspace projection
    single_ref_mult = dpp_novelty_multiplier(emb_b, [emb_a], theta=0.65)
    neg_a = [-x for x in emb_a]
    repeated_ref_mult = dpp_novelty_multiplier(
        emb_b, [emb_a, emb_a, neg_a, emb_a], theta=0.65
    )
    assert 0.0 < single_ref_mult <= 1.0
    assert abs(single_ref_mult - repeated_ref_mult) < 1e-9


# =============================================================================
# 8. Short-Lived SSE Ticket Auth & Unauthenticated Health Redaction ([HIGH-04], [MEDIUM-07])
# =============================================================================


async def test_sse_short_lived_ticket_auth_and_health_redaction(
    hardened_env: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session_factory, client = hardened_env
    admin_pw = "hardened-sse-ticket-password-2026"
    monkeypatch.setenv("TWEETTRACKER_ADMIN_PASSWORD", admin_pw)


    # 1. Unauthenticated /api/health omits providers_configured when auth_required=True
    unauth_health = await client.get("/api/health")
    assert unauth_health.status_code == 200
    unauth_body = unauth_health.json()
    assert unauth_body["auth_required"] is True
    assert "providers_configured" not in unauth_body

    # Authenticated /api/health includes providers_configured
    auth_health = await client.get(
        "/api/health", headers={"X-Admin-Password": admin_pw}
    )
    assert auth_health.status_code == 200
    assert "providers_configured" in auth_health.json()

    ws1_id = (
        await client.get("/api/workspaces", headers={"X-Admin-Password": admin_pw})
    ).json()[0]["id"]

    # 2. Unauthenticated SSE ticket request & unauthenticated SSE stream fail with 401
    assert (await client.post("/api/auth/sse-ticket")).status_code == 401
    assert (await client.get(f"/api/workspaces/{ws1_id}/events?max_events=1")).status_code == 401

    # Raw admin password passed in ?ticket= query param is rejected (never accepted as a ticket)
    raw_pw_in_query = await client.get(
        f"/api/workspaces/{ws1_id}/events?max_events=1&ticket={admin_pw}"
    )
    assert raw_pw_in_query.status_code == 401

    # 3. Mint short-lived HMAC ticket via POST /api/auth/sse-ticket and stream SSE without header
    ticket_resp = await client.post(
        "/api/auth/sse-ticket", headers={"X-Admin-Password": admin_pw}
    )
    assert ticket_resp.status_code == 200
    ticket = ticket_resp.json()["ticket"]
    assert admin_pw not in ticket

    sse_ok = await client.get(
        f"/api/workspaces/{ws1_id}/events?max_events=1&ticket={ticket}"
    )
    assert sse_ok.status_code == 200
    assert "event: connected" in sse_ok.text

    # 4. Tampered or expired SSE tickets are rejected with 401
    tampered_ticket = ticket[:-4] + ("0000" if not ticket.endswith("0000") else "ffff")
    assert (
        await client.get(
            f"/api/workspaces/{ws1_id}/events?max_events=1&ticket={tampered_ticket}"
        )
    ).status_code == 401

    expired_ticket = create_sse_ticket(ttl_seconds=60, now_ts=int(time.time()) - 120)["ticket"]
    assert (
        await client.get(
            f"/api/workspaces/{ws1_id}/events?max_events=1&ticket={expired_ticket}"
        )
    ).status_code == 401


# =============================================================================
# 9. Live-Provider Zero Fabrication, Git-Ref Hardening, & Docs Watch ([HIGH-03], [MEDIUM-07], [LOW-01])
# =============================================================================


@respx.mock
async def test_live_provider_zero_fabrication_and_git_ref_validation(
    hardened_env: tuple[async_sessionmaker[AsyncSession], httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tweettracker.algo_sync import is_watched_ranking_path

    _session_factory, client = hardened_env
    monkeypatch.setenv("TWEETAPI_API_KEY", "mock-tweetapi-key")


    # Create a live-provider workspace (tweetapi_com)
    create_resp = await client.post(
        "/api/workspaces",
        json={
            "name": "Live Provider Guard WS",
            "x_data_provider": "tweetapi_com",
            "my_x_handle": "operator_live",
        },
    )
    assert create_resp.status_code == 201
    live_ws_id = create_resp.json()["id"]

    # Mock TweetAPI returning empty profile list -> import must 400 instead of fabricating profiles
    respx.get(url__regex=r"https://api\.tweetapi\.com/.*").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    imp_empty = await client.post(
        f"/api/workspaces/{live_ws_id}/network/accounts/import",
        json={"handles": ["nonexistent_live_handle"]},
    )
    assert imp_empty.status_code == 400

    # Git ref traversal / query injection on /api/algo-sync/check must 422
    for bad_ref in ["main?per_page=1", "../secret", "/leading-slash", "branch..two", "-flag"]:
        bad_sync = await client.post(
            "/api/algo-sync/check",
            json={"target_ref": bad_ref},
        )
        assert bad_sync.status_code == 422, f"Expected 422 for target_ref={bad_ref}"

    # docs/ is watched while root README.md and ads/ remain ignored
    assert is_watched_ranking_path("docs/architecture.md") is True
    assert is_watched_ranking_path("README.md") is False
    assert is_watched_ranking_path("ads/pacing/budget.rs") is False
