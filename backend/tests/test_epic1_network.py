"""Comprehensive Epic 1 tests: Seeded NMF, Heavy Hitters, N-Pole Bridges, Mutual CRM & <1.5s Perf."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
import math
import time

import httpx
import networkx as nx
import numpy as np
import pytest

from tweettracker.api.main import create_app
from tweettracker.db import configure_database, dispose_database
from tweettracker.graph import (
    AccountNodeInput,
    CommunityAnchorInput,
    PostInteractionInput,
    PostTrajectoryInput,
    RelationshipEdgeInput,
    build_behavioral_graph,
    classify_bridge_role,
    compute_bipartite_salsa,
    compute_burt_brokerage,
    compute_heavy_hitters,
    compute_mini_simclusters,
    compute_n_pole_anchors,
    compute_relationships_and_roi,
    compute_weighted_s_core,
    compute_workspace_analytics_in_memory,
    guimera_amaral_participation,
)
from tweettracker.seed import bootstrap_database


@pytest.fixture
async def api_client(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[httpx.AsyncClient, None]:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.delenv("TWEETTRACKER_ADMIN_PASSWORD", raising=False)

    engine, session_factory = configure_database("sqlite+aiosqlite:///:memory:")
    await bootstrap_database(engine, session_factory, auto_migrate=True, seed_fixtures=True)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    await dispose_database()


def test_story_1_1_seeded_nmf_discovers_cross_community_overlap_and_k1_safety() -> None:
    """Verifies Seeded NMF discovers cross-circle overlap from co-engagement and handles K=1 safely."""
    now = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)
    accts = [AccountNodeInput(id=i + 1, handle=f"u_{i + 1}", followers_count=10_000) for i in range(9)]
    comms_k2 = [
        CommunityAnchorInput(id=10, name="AI Infra", slug="ai-infra", color="#38bdf8"),
        CommunityAnchorInput(id=20, name="Venture", slug="venture", color="#a78bfa"),
    ]
    # Nodes 1..4 tagged AI (10), Nodes 5..8 tagged Venture (20), Node 9 manually tagged ONLY AI (10)
    # but heavily engaged by Venture accounts 5, 6, 7, 8!
    manual_tags = {
        1: {10}, 2: {10}, 3: {10}, 4: {10},
        5: {20}, 6: {20}, 7: {20}, 8: {20},
        9: {10},
    }
    edges: list[RelationshipEdgeInput] = []
    for u in (1, 2, 3, 4):
        for v in (1, 2, 3, 4):
            if u != v:
                edges.append(RelationshipEdgeInput(u, v, "reply", 3, now))
    for u in (5, 6, 7, 8):
        for v in (5, 6, 7, 8):
            if u != v:
                edges.append(RelationshipEdgeInput(u, v, "reply", 3, now))
        # Venture accounts heavily quote & reply to Node 9 (cross-community behavioral pull)
        edges.append(RelationshipEdgeInput(u, 9, "quote", 5, now))
        edges.append(RelationshipEdgeInput(9, u, "reply", 4, now))
    edges.append(RelationshipEdgeInput(1, 9, "reply", 3, now))

    bundle = build_behavioral_graph(accts, edges, reference_time=now)
    sc_k2 = compute_mini_simclusters(bundle, comms_k2, manual_tags)

    for u in bundle.node_ids:
        tot = sum(sc_k2.interested_in[u].values())
        assert abs(tot - 1.0) < 1e-3
        assert u in sc_k2.sub_pocket_labels

    # Node 9 (manually tagged only 10) acquires significant Venture (20) affinity vs pure AI Node 1
    assert sc_k2.interested_in[9][20] > sc_k2.interested_in[1][20] + 0.15

    # K=1 graceful path: exact 1.0 affinity, zero division-by-zero
    sc_k1 = compute_mini_simclusters(bundle, comms_k2[:1], {u: {10} for u in bundle.node_ids})
    for u in bundle.node_ids:
        assert sc_k1.known_for[u] == {10: 1.0}
        assert sc_k1.interested_in[u] == {10: 1.0}


def test_story_1_2_heavy_hitter_core_catalyst_beats_high_follower_ghost() -> None:
    """Verifies a smaller core catalyst account decisively outranks a 500k-follower ghost."""
    now = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)
    accounts = [
        AccountNodeInput(
            id=1,
            handle="core_catalyst",
            followers_count=14_500,
            baseline_p50={15: 250, 30: 480, 60: 900, 2880: 2500},
        ),
        AccountNodeInput(
            id=2,
            handle="peer_alpha",
            followers_count=32_000,
            baseline_p50={15: 500, 30: 950, 60: 1800, 2880: 5000},
        ),
        AccountNodeInput(
            id=3,
            handle="peer_beta",
            followers_count=28_000,
            baseline_p50={15: 420, 30: 800, 60: 1500, 2880: 4400},
        ),
        AccountNodeInput(
            id=4,
            handle="peer_gamma",
            followers_count=19_000,
            baseline_p50={15: 300, 30: 560, 60: 1050, 2880: 3000},
        ),
        # 500k-follower ghost with zero behavioral edges and stagnant posts
        AccountNodeInput(
            id=99,
            handle="dormant_megaghost",
            followers_count=550_000,
            baseline_p50={15: 8000, 30: 15000, 60: 28000, 2880: 85000},
        ),
    ]

    # Dense mutual & reply/quote core among nodes 1, 2, 3, 4; node 99 is completely isolated
    edges: list[RelationshipEdgeInput] = []
    for u in (1, 2, 3, 4):
        for v in (1, 2, 3, 4):
            if u != v:
                edges.append(
                    RelationshipEdgeInput(
                        source_account_id=u,
                        target_account_id=v,
                        edge_type="follow",
                        last_event_at=now - timedelta(days=5),
                    )
                )
        if u != 1:
            edges.append(
                RelationshipEdgeInput(
                    source_account_id=u,
                    target_account_id=1,
                    edge_type="reply",
                    interaction_count=4,
                    last_event_at=now - timedelta(days=1),
                )
            )
            edges.append(
                RelationshipEdgeInput(
                    source_account_id=1,
                    target_account_id=u,
                    edge_type="quote",
                    interaction_count=3,
                    last_event_at=now - timedelta(days=2),
                )
            )

    posts = [
        # Post by peer_alpha where core_catalyst replies at 15m -> 4.5x velocity surge by 60m!
        PostTrajectoryInput(
            id=101,
            x_post_id="p101",
            author_account_id=2,
            created_at_x=now - timedelta(minutes=75),
            latest_views=6200,
            value_model_score=1.35,
            snapshots=[(15, 520), (60, 6100)],
        ),
        # Own post by core_catalyst performing strongly
        PostTrajectoryInput(
            id=102,
            x_post_id="p102",
            author_account_id=1,
            created_at_x=now - timedelta(minutes=45),
            latest_views=9400,
            value_model_score=1.48,
            snapshots=[(15, 3100), (45, 9400)],
        ),
        # Dead post by dormant_megaghost
        PostTrajectoryInput(
            id=103,
            x_post_id="p103",
            author_account_id=99,
            created_at_x=now - timedelta(minutes=120),
            latest_views=310,
            value_model_score=0.02,
            snapshots=[(60, 240), (120, 310)],
        ),
    ]

    interactions = [
        PostInteractionInput(
            post_id=101,
            author_account_id=2,
            actor_account_id=1,
            actor_x_user_id="u1",
            actor_handle="core_catalyst",
            interaction_type="reply",
            occurred_at=now - timedelta(minutes=60),
        )
    ]

    bundle = build_behavioral_graph(accounts, edges, interactions, reference_time=now)
    # Mutual reply weight (20.0) + mutual follow (2.0 + 4.0) verified on 2 -> 1
    assert bundle.directed[2][1]["is_mutual_follow"] is True
    assert bundle.directed[2][1]["weight"] > 25.0

    s_cores = compute_weighted_s_core(bundle.undirected)
    assert s_cores[1] > 0.0
    assert s_cores[99] == 0.0

    salsa_auth, salsa_scout = compute_bipartite_salsa(bundle.directed, bundle.node_ids)
    for uid in bundle.node_ids:
        assert 0.0 <= salsa_auth[uid] <= 1.0
        assert 0.0 <= salsa_scout[uid] <= 1.0
    assert salsa_auth[1] > salsa_auth[99] == 0.0
    assert salsa_scout[1] > salsa_scout[99] == 0.0

    hh = compute_heavy_hitters(
        accounts, bundle, posts=posts, post_interactions=interactions, reference_time=now
    )
    cat_metrics = hh.by_account[1]
    ghost_metrics = hh.by_account[99]
    peer_no_cat = hh.by_account[3]

    # Neutral 1.0x prior when 0 catalysis events observed
    assert peer_no_cat.catalysis_lift == 1.0
    assert cat_metrics.catalysis_lift > 2.5
    assert 0.0 <= cat_metrics.user_cred_v2 <= 100.0
    assert 0.0 <= ghost_metrics.user_cred_v2 <= 100.0
    assert cat_metrics.heavy_hitter_score > 70.0
    assert ghost_metrics.heavy_hitter_score < 10.0
    assert cat_metrics.heavy_hitter_score > ghost_metrics.heavy_hitter_score
    assert cat_metrics.follower_efficiency > ghost_metrics.follower_efficiency


def test_story_1_3_guimera_amaral_burt_parity_and_n_pole_geometry() -> None:
    """Verifies K-normalized participation, exact Burt brokerage parity, and N-pole layouts for K=1..4."""
    # K=1 -> 0.0; single-circle -> 0.0; even split -> 1.0 for all K >= 2
    assert guimera_amaral_participation([10.0]) == 0.0
    for k in (2, 3, 4, 5):
        assert guimera_amaral_participation([1.0] + [0.0] * (k - 1)) == 0.0
        assert abs(guimera_amaral_participation([2.5] * k) - 1.0) < 1e-12

    # Role classification
    assert classify_bridge_role(0.18, [0.92, 0.08, 0.0]) == "provincial"
    assert classify_bridge_role(0.72, [0.52, 0.44, 0.04]) == "two_way_bridge"
    assert classify_bridge_role(0.95, [0.36, 0.34, 0.30]) == "multi_circle_connector"

    # Exact parity against networkx.constraint on weighted undirected graph + isolated/degree-1 safety
    g = nx.Graph()
    g.add_nodes_from(range(10))  # node 8 = degree 1, node 9 = isolated
    for u, v, w in (
        (0, 1, 4.0),
        (1, 2, 3.0),
        (0, 2, 5.0),
        (3, 4, 4.0),
        (4, 5, 3.5),
        (3, 5, 4.5),
        # Node 6 bridges clique (0,1,2) and clique (3,4,5) across a structural hole
        (6, 0, 3.0),
        (6, 3, 3.0),
        (6, 7, 2.5),
        # Node 8 is degree-1 pendant attached to 7
        (7, 8, 2.0),
    ):
        g.add_edge(u, v, weight=w)

    nx_c = nx.constraint(g, weight="weight")
    brok = compute_burt_brokerage(g, list(range(10)))

    assert brok[9] == 0.0  # isolated
    assert brok[8] == 0.0  # degree-1
    for u in range(8):
        expected_brok = round(min(1.0, max(0.0, 1.0 - float(nx_c[u]))), 4)
        assert abs(brok[u] - expected_brok) < 1e-4
    # Structural hole broker (6) has much higher brokerage than closed-triad member (1)
    assert brok[6] > brok[1]

    # N-Pole Anchor Geometry for K = 1, 2, 3, 4
    c_list = [
        CommunityAnchorInput(id=i + 1, name=f"C{i + 1}", slug=f"c{i + 1}", color="#38bdf8")
        for i in range(4)
    ]
    p1 = compute_n_pole_anchors(c_list[:1])
    assert len(p1) == 1 and p1[0].x == 0.0 and p1[0].y == 0.0

    # K=2: horizontal axis (1, 0) and (-1, 0)
    p2 = compute_n_pole_anchors(c_list[:2])
    assert (p2[0].x, p2[0].y) == (1.0, 0.0)
    assert abs(p2[1].x - (-1.0)) < 1e-5 and abs(p2[1].y - 0.0) < 1e-5

    # K=3: equilateral triangle
    p3 = compute_n_pole_anchors(c_list[:3])
    assert (p3[0].x, p3[0].y) == (1.0, 0.0)
    assert abs(p3[1].x - (-0.5)) < 1e-5 and abs(p3[1].y - (math.sqrt(3.0) / 2.0)) < 1e-5
    assert abs(p3[2].x - (-0.5)) < 1e-5 and abs(p3[2].y - (-math.sqrt(3.0) / 2.0)) < 1e-5

    # K=4: unit square / diamond (1, 0), (0, 1), (-1, 0), (0, -1)
    p4 = compute_n_pole_anchors(c_list[:4])
    assert (p4[0].x, p4[0].y) == (1.0, 0.0)
    assert abs(p4[1].x - 0.0) < 1e-5 and abs(p4[1].y - 1.0) < 1e-5
    assert abs(p4[2].x - (-1.0)) < 1e-5 and abs(p4[2].y - 0.0) < 1e-5
    assert abs(p4[3].x - 0.0) < 1e-5 and abs(p4[3].y - (-1.0)) < 1e-5


def test_story_1_4_mutual_crm_5_states_and_optional_handle() -> None:
    """Verifies 5-state classification + ROI queue ordering vs clean none mode when handle is blank."""
    now = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)
    accts = [
        AccountNodeInput(id=1, handle="mut_live", follows_me=True, followed_by_me=True, mutual_interaction_count_30d=6),
        AccountNodeInput(id=2, handle="mut_sleep", follows_me=True, followed_by_me=True, mutual_interaction_count_30d=0, last_interaction_at=now - timedelta(days=38)),
        AccountNodeInput(id=3, handle="warm_target", follows_me=False, followed_by_me=True, mutual_interaction_count_30d=3),
        AccountNodeInput(id=4, handle="cold_target", follows_me=False, followed_by_me=True, mutual_interaction_count_30d=0),
        AccountNodeInput(id=5, handle="inbound_vip", follows_me=True, followed_by_me=False, mutual_interaction_count_30d=0),
    ]
    hh = {1: 80.0, 2: 75.0, 3: 78.0, 4: 70.0, 5: 82.0}
    bs = {1: 40.0, 2: 30.0, 3: 50.0, 4: 20.0, 5: 60.0}

    # With my_x_handle configured
    res = compute_relationships_and_roi("operator_hq", accts, hh, bs, reference_time=now)
    assert res.has_own_handle is True
    assert res.by_account[1].relationship_state == "mutual_active"
    assert res.by_account[1].is_mutual_20x is True
    assert res.by_account[2].relationship_state == "mutual_dormant"
    assert res.by_account[3].relationship_state == "outbound_warming"
    assert res.by_account[4].relationship_state == "outbound_cold"
    assert res.by_account[5].relationship_state == "inbound_unreciprocated"
    assert res.by_account[5].unlocks_20x_on_follow_back is True

    # ROI queue includes strictly inbound_unreciprocated, outbound_warming, mutual_dormant in descending ROI order
    assert [item.account_id for item in res.roi_queue] == [5, 3, 2]
    assert res.roi_queue[0].roi_score > res.roi_queue[1].roi_score > res.roi_queue[2].roi_score

    # With my_x_handle = None -> clean Ecosystem Mode
    res_none = compute_relationships_and_roi(None, accts, hh, bs, reference_time=now)
    assert res_none.has_own_handle is False
    assert res_none.roi_queue == []
    assert all(m.relationship_state == "none" for m in res_none.by_account.values())


async def test_api_seeded_k3_and_k4_plus_k1_edge_case_and_bulk_import(
    api_client: httpx.AsyncClient,
) -> None:
    """End-to-end API verification across WS1 (K=3, handle), WS2 (K=4, no handle), and K=1 workspace."""
    workspaces = (await api_client.get("/api/workspaces")).json()
    ws1, ws2 = workspaces[0], workspaces[1]

    # 1. Verify WS1 (K=3, my_x_handle="operator_hq")
    g1 = (await api_client.get(f"/api/workspaces/{ws1['id']}/network/graph?mode=bridges")).json()
    assert g1["k_communities"] == 3
    assert g1["bridge_map_available"] is True
    assert g1["has_own_handle"] is True
    assert len(g1["poles"]) == 3
    assert len(g1["nodes"]) == 36
    assert len(g1["edges"]) > 20

    # Check deterministic golden-angle anti-stacking: all 36 (x, y) pairs are distinct!
    xy_pairs = {(n["n_pole_xy"]["x"], n["n_pole_xy"]["y"]) for n in g1["nodes"]}
    assert len(xy_pairs) == 36

    # Check Heavy Hitters on WS1: core catalyst outranks dormant ghost cuda_craft (119k followers)
    accts1 = (
        await api_client.get(
            f"/api/workspaces/{ws1['id']}/network/accounts?sort_by=heavy_hitter_score&sort_dir=desc"
        )
    ).json()
    by_h1 = {a["handle"]: a for a in accts1}
    assert by_h1["tensor_architect"]["heavy_hitter_score"] > by_h1["cuda_craft"]["heavy_hitter_score"]
    assert by_h1["cuda_craft"]["followers_count"] == 119_000
    assert by_h1["cuda_craft"]["heavy_hitter_score"] < 15.0

    # Every account's K=3 community_splits sum to ~100%
    for a in accts1:
        total_pct = sum(s["pct"] for s in a["community_splits"])
        assert 99.5 <= total_pct <= 100.5

    # Drill-down detail endpoint returns catalyzed conversations & community breakdown
    cat_target = next(a for a in accts1 if a["catalyzed_posts_count"] > 0)
    detail = (
        await api_client.get(
            f"/api/workspaces/{ws1['id']}/network/accounts/{cat_target['id']}/detail"
        )
    ).json()
    assert len(detail["catalyzed_conversations"]) >= 1
    assert detail["account"]["x_profile_url"].startswith("https://x.com/")

    # Relationships endpoint on WS1 + 1-click follow-back unlock
    rel1 = (await api_client.get(f"/api/workspaces/{ws1['id']}/network/relationships")).json()
    assert rel1["has_own_handle"] is True
    assert rel1["counts"]["inbound_unreciprocated"] >= 1
    assert len(rel1["roi_queue"]) >= 5

    quick_win_id = next(
        item["account"]["id"]
        for item in rel1["roi_queue"]
        if item["unlocks_20x_on_follow_back"] is True
    )
    patched_qw = (
        await api_client.patch(
            f"/api/workspaces/{ws1['id']}/network/accounts/{quick_win_id}",
            json={"followed_by_me": True},
        )
    ).json()
    assert patched_qw["is_mutual_20x"] is True
    assert patched_qw["relationship_state"] in ("mutual_active", "mutual_dormant")

    # 2. Verify WS2 (K=4, my_x_handle=None)
    g2 = (await api_client.get(f"/api/workspaces/{ws2['id']}/network/graph?mode=bridges")).json()
    assert g2["k_communities"] == 4
    assert g2["bridge_map_available"] is True
    assert g2["has_own_handle"] is False
    assert len(g2["poles"]) == 4

    rel2 = (await api_client.get(f"/api/workspaces/{ws2['id']}/network/relationships")).json()
    assert rel2["has_own_handle"] is False
    assert rel2["roi_queue"] == []
    assert rel2["counts"]["none"] == 24

    # 3. Verify K=1 Single-Community Workspace + Bulk URL / CSV / X List Import + Deduplication
    ws_k1 = (
        await api_client.post(
            "/api/workspaces",
            json={
                "name": "Single Circle BioTech",
                "my_x_handle": "@biotech_op",
                "x_data_provider": "fixture",
                "initial_communities": [{"name": "Protein Design", "color": "#10b981"}],
            },
        )
    ).json()
    k1_id = ws_k1["id"]
    c0_id = ws_k1["communities"][0]["id"]

    imp_res = await api_client.post(
        f"/api/workspaces/{k1_id}/network/accounts/import",
        json={
            "handles_text": "@Alpha_Fold_Dev, https://x.com/alpha_fold_dev?s=20\nhttps://twitter.com/WetLab_Auto",
            "csv_text": "handle,communities\ncrispr_foundry,Protein Design\nwetlab_auto,Protein Design",
            "community_ids": [c0_id],
            "x_list_id": "777888999",
        },
    )
    assert imp_res.status_code == 201
    imp_json = imp_res.json()
    # 3 unique from handles/CSV + 3 synthesized from X List 777888999 = 6 unique accounts, 2 duplicates collapsed
    assert imp_json["imported_count"] == 6
    assert imp_json["deduplicated_count"] == 2

    g_k1 = (await api_client.get(f"/api/workspaces/{k1_id}/network/graph?mode=bridges")).json()
    # Automatically falls back to single-community mode when K=1
    assert g_k1["k_communities"] == 1
    assert g_k1["bridge_map_available"] is False
    assert g_k1["mode"] == "community"
    for node in g_k1["nodes"]:
        assert node["bridge_score"] == 0.0
        assert node["bridge_role"] == "provincial"
        assert node["community_mix"][0]["nmf_interested_in"] == 1.0
        assert node["community_mix"][0]["pct"] == 100.0

    # Delete an account and verify graph recomputes cleanly
    victim_id = g_k1["nodes"][0]["id"]
    del_resp = await api_client.delete(
        f"/api/workspaces/{k1_id}/network/accounts/{victim_id}"
    )
    assert del_resp.status_code == 204
    accts_after_del = (
        await api_client.get(f"/api/workspaces/{k1_id}/network/accounts")
    ).json()
    assert len(accts_after_del) == 5


def _synthetic_analytics_inputs(n_nodes: int, n_edges: int) -> dict:
    """Synthetic 3-community workspace at a fixed edge density (5 edges per node)."""
    rng = np.random.default_rng(2026)
    now = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)

    communities = [
        CommunityAnchorInput(id=1, name="Circle Alpha", slug="alpha", color="#38bdf8"),
        CommunityAnchorInput(id=2, name="Circle Beta", slug="beta", color="#a78bfa"),
        CommunityAnchorInput(id=3, name="Circle Gamma", slug="gamma", color="#34d399"),
    ]
    accounts = [
        AccountNodeInput(
            id=i + 1,
            handle=f"voice_{i + 1}",
            followers_count=int(1_000 + (i * 97) % 150_000),
            follows_me=(i % 9 == 0),
            followed_by_me=(i % 4 == 0),
            mutual_interaction_count_30d=(2 if i % 11 == 0 else 0),
        )
        for i in range(n_nodes)
    ]

    manual_tags: dict[int, set[int]] = {}
    for i in range(n_nodes):
        prim = (i % 3) + 1
        if i % 15 == 0:
            manual_tags[i + 1] = {1, 2, 3}
        elif i % 7 == 0:
            manual_tags[i + 1] = {prim, (prim % 3) + 1}
        else:
            manual_tags[i + 1] = {prim}

    src_arr = rng.integers(1, n_nodes + 1, size=n_edges)
    tgt_arr = rng.integers(1, n_nodes + 1, size=n_edges)
    etypes = ("follow", "reply", "quote", "repost")
    edges = [
        RelationshipEdgeInput(
            source_account_id=int(s),
            target_account_id=int(t),
            edge_type=etypes[idx % 4],
            interaction_count=1 + (idx % 3),
            last_event_at=now - timedelta(days=int(idx % 12)),
        )
        for idx, (s, t) in enumerate(zip(src_arr, tgt_arr, strict=False))
        if s != t
    ]
    return {
        "my_x_handle": "operator_hq",
        "communities": communities,
        "accounts": accounts,
        "manual_tags": manual_tags,
        "relationship_edges": edges,
        "reference_time": now,
    }


def _best_of_runs_secs(inputs: dict, runs: int = 3) -> tuple[float, object]:
    """Fastest of `runs` timings, so a busy machine inflates both sides of the ratio less."""
    best = math.inf
    res = None
    for _ in range(runs):
        t0 = time.perf_counter()
        res = compute_workspace_analytics_in_memory(**inputs)
        best = min(best, time.perf_counter() - t0)
    return best, res


def test_graph_engine_scales_near_linearly_to_5000_nodes() -> None:
    """The full Epic 1 graph engine must scale near-linearly with workspace size.

    Asserts a ratio (5x the nodes and edges costs at most 8x the time) instead of wall-clock
    seconds, so it catches an accidental quadratic step on any machine without failing just
    because the machine running the tests is busy.
    """
    small_secs, _ = _best_of_runs_secs(_synthetic_analytics_inputs(1_000, 5_000))
    large_secs, res = _best_of_runs_secs(_synthetic_analytics_inputs(5_000, 25_000))

    assert len(res.heavy_hitters.by_account) == 5_000
    assert len(res.bridges.by_account) == 5_000
    assert len(res.relationships.by_account) == 5_000

    ratio = large_secs / max(small_secs, 1e-6)
    assert ratio <= 8.0, (
        f"5x nodes took {ratio:.1f}x as long ({small_secs:.3f}s -> {large_secs:.3f}s); "
        "expected near-linear scaling (<= 8x)"
    )
