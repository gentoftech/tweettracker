"""Tests for UX Simplification & Zero-Touch Automation Overhaul:
1. Idempotent additive SQLite column upgrades on pre-existing tables.
2. Live ephemeral Hot Posts queue (`live_queue=true`), `acted` / `skipped` / `open` undo,
   plain-English `hover_summary`, and stale-fixture auto-rejuvenation.
3. Zero-touch `auto_sync_workspace_network`: authority-weighted Rising auto-promotion,
   1-click permanent `Mute`, and 30-day inactivity self-cleaning (never touching manual seeds).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile

import httpx
import pytest
from sqlalchemy import select

from tweettracker.api.main import create_app
from tweettracker.db import configure_database, dispose_database
from tweettracker.models import (
    Post,
    PostInteraction,
    RelationshipEdge,
    TrackedAccount,
)
from tweettracker.rising import auto_sync_workspace_network
from tweettracker.seed import bootstrap_database


@pytest.fixture
async def ux_api_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncGenerator[tuple[httpx.AsyncClient, object], None]:
    db_file = tmp_path / "ux_overhaul.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("AUTO_MIGRATE", "true")
    monkeypatch.setenv("SEED_FIXTURES", "true")
    monkeypatch.delenv("TWEETTRACKER_ADMIN_PASSWORD", raising=False)

    engine, session_factory = configure_database(db_url)
    await bootstrap_database(engine, session_factory, auto_migrate=True, seed_fixtures=True)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, session_factory

    await dispose_database()


@pytest.mark.asyncio
async def test_additive_sqlite_column_migration_upgrades_legacy_tables() -> None:
    """Verifies pre-existing SQLite DBs missing the 5 new UX/automation columns upgrade cleanly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "legacy.db"
        raw_conn = sqlite3.connect(db_path)
        try:
            raw_conn.execute(
                "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name VARCHAR(160), slug VARCHAR(160))"
            )
            raw_conn.execute(
                "CREATE TABLE tracked_accounts (id INTEGER PRIMARY KEY, workspace_id INTEGER, handle VARCHAR(64), is_tracked BOOLEAN NOT NULL DEFAULT 1)"
            )
            raw_conn.execute(
                "CREATE TABLE posts (id INTEGER PRIMARY KEY, workspace_id INTEGER, x_post_id VARCHAR(64))"
            )
            raw_conn.commit()
        finally:
            raw_conn.close()

        engine, session_factory = configure_database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
        try:
            await bootstrap_database(
                engine, session_factory, auto_migrate=True, seed_fixtures=False
            )
            # Idempotent second pass
            await bootstrap_database(
                engine, session_factory, auto_migrate=True, seed_fixtures=False
            )
        finally:
            await dispose_database()

        verify_conn = sqlite3.connect(db_path)
        try:
            ws_cols = {
                row[1]
                for row in verify_conn.execute("PRAGMA table_info(workspaces)").fetchall()
            }
            acct_cols = {
                row[1]
                for row in verify_conn.execute("PRAGMA table_info(tracked_accounts)").fetchall()
            }
            post_cols = {
                row[1] for row in verify_conn.execute("PRAGMA table_info(posts)").fetchall()
            }
            assert "x_list_ids_json" in ws_cols
            assert {"is_auto_tracked", "is_muted", "auto_tracked_at"}.issubset(acct_cols)
            assert {"queue_status", "acted_at"}.issubset(post_cols)
        finally:
            verify_conn.close()


@pytest.mark.asyncio
async def test_live_ephemeral_queue_act_skip_undo_and_hover_summary(
    ux_api_env: tuple[httpx.AsyncClient, object],
) -> None:
    client, _ = ux_api_env

    # Default API contract stays backward-compatible (returns all 14 unmatured seeded posts)
    raw_resp = await client.get("/api/workspaces/1/radar")
    assert raw_resp.status_code == 200
    assert raw_resp.json()["total_matching"] == 14

    # Live queue filters to active momentum & attaches plain-English hover_summary
    live_resp = await client.get("/api/workspaces/1/radar?live_queue=true")
    assert live_resp.status_code == 200
    live_body = live_resp.json()
    initial_live_count = live_body["total_matching"]
    assert initial_live_count >= 6
    assert "mutual_count" in live_body

    top_card = live_body["items"][0]
    assert top_card["queue_status"] == "open"
    assert "hover_summary" in top_card
    assert "usual views" in top_card["hover_summary"]["pace_label"]
    assert len(top_card["hover_summary"]["why_reasons"]) >= 2

    # Mark #1 post as acted ('Reply' / 'Quote') -> immediately clears from live_queue
    act_resp = await client.post(
        f"/api/workspaces/1/radar/posts/{top_card['id']}/queue",
        json={"status": "acted"},
    )
    assert act_resp.status_code == 200
    assert act_resp.json()["queue_status"] == "acted"
    assert act_resp.json()["acted_at"] is not None

    after_act = (await client.get("/api/workspaces/1/radar?live_queue=true")).json()
    assert after_act["total_matching"] == initial_live_count - 1
    assert all(it["id"] != top_card["id"] for it in after_act["items"])

    # Undo ('open') -> restores post into live_queue
    undo_resp = await client.post(
        f"/api/workspaces/1/radar/posts/{top_card['id']}/queue",
        json={"status": "open"},
    )
    assert undo_resp.status_code == 200
    assert undo_resp.json()["queue_status"] == "open"
    assert undo_resp.json()["acted_at"] is None

    after_undo = (await client.get("/api/workspaces/1/radar?live_queue=true")).json()
    assert after_undo["total_matching"] == initial_live_count
    assert any(it["id"] == top_card["id"] for it in after_undo["items"])

    # Mark #2 post as skipped -> clears from live_queue
    second_id = live_body["items"][1]["id"]
    skip_resp = await client.post(
        f"/api/workspaces/1/radar/posts/{second_id}/queue",
        json={"status": "skipped"},
    )
    assert skip_resp.status_code == 200
    after_skip = (await client.get("/api/workspaces/1/radar?live_queue=true")).json()
    assert after_skip["total_matching"] == initial_live_count - 1


@pytest.mark.asyncio
async def test_auto_sync_network_promotes_rising_respects_mute_and_prunes_30d_inactive(
    ux_api_env: tuple[httpx.AsyncClient, object],
) -> None:
    client, session_factory = ux_api_env

    # 1. Zero-touch auto-sync promotes qualifying <50k Rising voices into TrackedAccounts
    sync_resp = await client.post("/api/workspaces/1/network/auto-sync")
    assert sync_resp.status_code == 200
    sync_data = sync_resp.json()
    assert sync_data["auto_added_count"] >= 2
    assert len(sync_data["auto_added_handles"]) == sync_data["auto_added_count"]

    rising_accts = (
        await client.get("/api/workspaces/1/network/accounts?rising_only=true")
    ).json()
    assert len(rising_accts) == sync_data["auto_added_count"]
    assert all(a["is_auto_tracked"] is True for a in rising_accts)

    # 2. 1-click Mute permanently excludes an auto-tracked account across future syncs
    muted_target = rising_accts[0]
    mute_resp = await client.patch(
        f"/api/workspaces/1/network/accounts/{muted_target['id']}",
        json={"is_muted": True},
    )
    assert mute_resp.status_code == 200
    assert mute_resp.json()["is_muted"] is True

    rising_after_mute = (
        await client.get("/api/workspaces/1/network/accounts?rising_only=true")
    ).json()
    assert all(a["id"] != muted_target["id"] for a in rising_after_mute)

    sync_again = (await client.post("/api/workspaces/1/network/auto-sync")).json()
    assert muted_target["handle"] not in sync_again["auto_added_handles"]

    # 3. 30-day self-cleaning auto-prune drops inactive auto-tracked accounts ONLY,
    #    while manual seeds (`is_auto_tracked == False`) are NEVER dropped
    stale_auto = rising_after_mute[0]
    now_utc = datetime.now(timezone.utc)
    forty_days_ago = now_utc - timedelta(days=40)

    async with session_factory() as session:  # type: ignore[operator]
        manual_seed = (
            await session.execute(
                select(TrackedAccount)
                .where(
                    TrackedAccount.workspace_id == 1,
                    TrackedAccount.is_auto_tracked.is_(False),
                    TrackedAccount.is_tracked.is_(True),
                )
                .limit(1)
            )
        ).scalar_one()
        manual_seed_id = manual_seed.id

        auto_row = await session.get(TrackedAccount, stale_auto["id"])
        assert auto_row is not None
        auto_row.auto_tracked_at = forty_days_ago

        # Backdate all posts, interactions, and edges for BOTH stale_auto and manual_seed to 40d ago
        for target_id in (stale_auto["id"], manual_seed_id):
            posts = (
                await session.execute(
                    select(Post).where(Post.author_account_id == target_id)
                )
            ).scalars().all()
            for p in posts:
                p.created_at_x = forty_days_ago

            inters = (
                await session.execute(
                    select(PostInteraction).where(PostInteraction.actor_account_id == target_id)
                )
            ).scalars().all()
            for it in inters:
                it.occurred_at = forty_days_ago

            edges = (
                await session.execute(
                    select(RelationshipEdge).where(
                        (RelationshipEdge.source_account_id == target_id)
                        | (RelationshipEdge.target_account_id == target_id)
                    )
                )
            ).scalars().all()
            for ed in edges:
                ed.last_event_at = forty_days_ago

        await session.commit()

        prune_summary = await auto_sync_workspace_network(session, 1, now=now_utc)
        assert stale_auto["handle"] in prune_summary["auto_pruned_handles"]
        assert prune_summary["auto_pruned_count"] >= 1

        refreshed_auto = await session.get(TrackedAccount, stale_auto["id"])
        refreshed_manual = await session.get(TrackedAccount, manual_seed_id)
        assert refreshed_auto is not None and refreshed_auto.is_tracked is False
        assert refreshed_manual is not None and refreshed_manual.is_tracked is True


@pytest.mark.asyncio
async def test_circle_free_workspace_x_lists_auto_clusters_and_organic_layout(
    ux_api_env: tuple[httpx.AsyncClient, object],
) -> None:
    """Verifies circle-free workspaces normalize workspace X Lists, color graph nodes by
    emergent Louvain interaction clusters, run organic force layout on K=1, and never emit 'connector'.
    """
    client, session_factory = ux_api_env

    # 1. Create workspace without initial_communities + URL & numeric X List IDs
    create_resp = await client.post(
        "/api/workspaces",
        json={
            "name": "Circle-Free Founders",
            "my_x_handle": "@founder_me",
            "x_data_provider": "fixture",
            "use_x_lists": True,
            "x_list_ids_json": [
                "https://x.com/i/lists/18492019283",
                "9988776655",
                "18492019283",
            ],
        },
    )
    assert create_resp.status_code == 201
    ws = create_resp.json()
    ws_id = ws["id"]
    assert ws["x_list_ids_json"] == ["18492019283", "9988776655"]

    # 2. Import accounts with zero community_ids and also accept full X List URL in import payload
    imp_resp = await client.post(
        f"/api/workspaces/{ws_id}/network/accounts/import",
        json={
            "handles_text": "@alice_ai\n@bob_ai\n@carol_sec\n@dave_sec\n@lone_seed",
        },
    )
    assert imp_resp.status_code == 201
    assert imp_resp.json()["imported_count"] == 5

    # In fixture mode, import creates zero synthetic RelationshipEdges when fresh or let's inspect graph
    now_utc = datetime.now(timezone.utc)
    async with session_factory() as session:  # type: ignore[operator]
        accts = list(
            (
                await session.execute(
                    select(TrackedAccount).where(TrackedAccount.workspace_id == ws_id)
                )
            ).scalars().all()
        )
        by_handle = {a.handle: a for a in accts}
        # Clear any synthetic fixture edges first to verify isolated default color (#38bdf8)
        existing_edges = list(
            (
                await session.execute(
                    select(RelationshipEdge).where(RelationshipEdge.workspace_id == ws_id)
                )
            ).scalars().all()
        )
        for ed in existing_edges:
            await session.delete(ed)
        await session.commit()

    iso_graph = (await client.get(f"/api/workspaces/{ws_id}/network/graph?mode=community")).json()
    assert len(iso_graph["nodes"]) == 5
    for node in iso_graph["nodes"]:
        assert node["color"] == "#38bdf8"
        assert node["status_badge"] != "connector"

    # 3. Add two distinct interaction clusters (alice<->bob and carol<->dave), leaving lone_seed isolated
    async with session_factory() as session:  # type: ignore[operator]
        accts = list(
            (
                await session.execute(
                    select(TrackedAccount).where(TrackedAccount.workspace_id == ws_id)
                )
            ).scalars().all()
        )
        by_handle = {a.handle: a for a in accts}
        pairs = [
            (by_handle["alice_ai"].id, by_handle["bob_ai"].id),
            (by_handle["bob_ai"].id, by_handle["alice_ai"].id),
            (by_handle["carol_sec"].id, by_handle["dave_sec"].id),
            (by_handle["dave_sec"].id, by_handle["carol_sec"].id),
        ]
        for src_id, dst_id in pairs:
            session.add(
                RelationshipEdge(
                    workspace_id=ws_id,
                    source_account_id=src_id,
                    target_account_id=dst_id,
                    edge_type="reply",
                    interaction_count=6,
                    decayed_weight=5.5,
                    last_event_at=now_utc - timedelta(hours=2),
                )
            )
        await session.commit()

    clustered_graph = (
        await client.get(f"/api/workspaces/{ws_id}/network/graph?mode=community")
    ).json()
    node_by_handle = {n["handle"]: n for n in clustered_graph["nodes"]}

    # Connected pockets share auto-cluster colors; distinct pockets get distinct colors; isolated stays #38bdf8
    assert node_by_handle["alice_ai"]["color"] == node_by_handle["bob_ai"]["color"]
    assert node_by_handle["carol_sec"]["color"] == node_by_handle["dave_sec"]["color"]
    assert node_by_handle["alice_ai"]["color"] != node_by_handle["carol_sec"]["color"]
    assert node_by_handle["lone_seed"]["color"] == "#38bdf8"

    # Organic force layout places intra-cluster pair closer than cross-cluster pair
    def _dist(h1: str, h2: str) -> float:
        p1 = node_by_handle[h1]["single_community_xy"]
        p2 = node_by_handle[h2]["single_community_xy"]
        return float(((p1["x"] - p2["x"]) ** 2 + (p1["y"] - p2["y"]) ** 2) ** 0.5)

    assert _dist("alice_ai", "bob_ai") < _dist("alice_ai", "carol_sec")

    # Poll radar using workspace-level x_list_ids_json with zero errors
    poll_resp = await client.post(f"/api/workspaces/{ws_id}/radar/poll")
    assert poll_resp.status_code == 200

    # Also verify seeded multi-community workspace never emits status_badge == 'connector'
    ws1_accts = (await client.get("/api/workspaces/1/network/accounts")).json()
    assert all(a["status_badge"] != "connector" for a in ws1_accts)


@pytest.mark.asyncio
async def test_live_queue_read_does_not_wait_on_workspace_lock_for_live_provider(
    ux_api_env: tuple[httpx.AsyncClient, object],
) -> None:
    """A long network sync holding the workspace lock must not block the live Hot Posts read."""
    import asyncio

    from tweettracker.db import get_workspace_lock
    from tweettracker.models import Workspace

    client, session_factory = ux_api_env
    async with session_factory() as session:  # type: ignore[operator]
        ws = await session.get(Workspace, 1)
        ws.x_data_provider = "tweetapi_com"
        await session.commit()

    async with get_workspace_lock(1):
        resp = await asyncio.wait_for(
            client.get("/api/workspaces/1/radar?live_queue=true"), timeout=5.0
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_save_for_later_leaves_live_queue_and_persists_past_maturity(
    ux_api_env: tuple[httpx.AsyncClient, object],
) -> None:
    from tweettracker.models import Post

    client, session_factory = ux_api_env
    live = (await client.get("/api/workspaces/1/radar?live_queue=true")).json()
    post_id = live["items"][0]["id"]

    saved = await client.post(
        f"/api/workspaces/1/radar/posts/{post_id}/queue", json={"status": "saved"}
    )
    assert saved.status_code == 200
    assert saved.json()["queue_status"] == "saved"
    assert saved.json()["acted_at"] is not None

    after = (await client.get("/api/workspaces/1/radar?live_queue=true")).json()
    assert all(it["id"] != post_id for it in after["items"])

    # Saved posts stay listed even after they mature past the 48h window
    async with session_factory() as session:  # type: ignore[operator]
        post = await session.get(Post, post_id)
        post.matured_48h = True
        await session.commit()

    saved_list = (await client.get("/api/workspaces/1/radar?saved_only=true")).json()
    assert [it["id"] for it in saved_list["items"]] == [post_id]
    assert saved_list["items"][0]["queue_status"] == "saved"

    unsave = await client.post(
        f"/api/workspaces/1/radar/posts/{post_id}/queue", json={"status": "open"}
    )
    assert unsave.json()["queue_status"] == "open"
    assert (await client.get("/api/workspaces/1/radar?saved_only=true")).json()["items"] == []


@pytest.mark.asyncio
async def test_hot_posts_rank_by_projected_reach_and_hide_small_posts(
    ux_api_env: tuple[httpx.AsyncClient, object],
) -> None:
    """A 1,400-view post running ~24x its tiny author's usual is too small to reply to and is
    hidden; a 40,000-view post at ~2.6x a big author's usual stays, and outranks the small one
    when the minimum is off despite a much lower breakout score."""
    from tweettracker.models import Workspace
    from tweettracker.radar import compute_velocity_metrics, project_48h_views
    from tweettracker.seed import _build_baseline_curves

    client, session_factory = ux_api_env
    now = datetime.now(timezone.utc)
    small_curves = _build_baseline_curves(150)
    big_curves = _build_baseline_curves(40_000)

    async with session_factory() as session:  # type: ignore[operator]
        ws = await session.get(Workspace, 1)
        assert ws.hot_posts_min_projected_views == 5_000
        small_author = TrackedAccount(
            workspace_id=1, x_user_id="u_small_reach", handle="small_reach_author",
            followers_count=3_000, baseline_curves_json=small_curves,
        )
        big_author = TrackedAccount(
            workspace_id=1, x_user_id="u_big_reach", handle="big_reach_author",
            followers_count=400_000, baseline_curves_json=big_curves,
        )
        session.add_all([small_author, big_author])
        await session.flush()
        created = now - timedelta(minutes=80)
        small_post = Post(
            workspace_id=1, x_post_id="p_small_reach", author_account_id=small_author.id,
            text="Van #4.", created_at_x=created, latest_views=1_400, breakout_score=95.0,
        )
        big_post = Post(
            workspace_id=1, x_post_id="p_big_reach", author_account_id=big_author.id,
            text="Big thread.", created_at_x=created, latest_views=40_000, breakout_score=70.0,
        )
        session.add_all([small_post, big_post])
        await session.commit()
        small_id, big_id = small_post.id, big_post.id

    # Preconditions: the small post is the bigger breakout but projects under the 5k floor
    small_vel = compute_velocity_metrics(1_400, 80.0, small_curves)
    big_vel = compute_velocity_metrics(40_000, 80.0, big_curves)
    assert small_vel.velocity_multiple > 20.0
    assert 1.25 < big_vel.velocity_multiple < 3.0
    assert project_48h_views(1_400, 80.0, small_curves) < 5_000
    assert project_48h_views(40_000, 80.0, big_curves) > 50_000

    body = (await client.get("/api/workspaces/1/radar?live_queue=true&limit=100")).json()
    ids = [it["id"] for it in body["items"]]
    assert small_id not in ids
    assert big_id in ids
    big_card = next(it for it in body["items"] if it["id"] == big_id)
    assert big_card["projected_48h_views"] > 50_000
    assert all(it["projected_48h_views"] >= 5_000 for it in body["items"])

    # Setting the minimum to 0 shows small posts again
    resp = await client.patch("/api/workspaces/1", json={"hot_posts_min_projected_views": 0})
    assert resp.status_code == 200
    assert resp.json()["hot_posts_min_projected_views"] == 0
    ids_all = [
        it["id"]
        for it in (await client.get("/api/workspaces/1/radar?live_queue=true&limit=100")).json()["items"]
    ]
    assert small_id in ids_all
    assert ids_all.index(big_id) < ids_all.index(small_id)


@pytest.mark.asyncio
async def test_hot_posts_show_big_ordinary_posts_and_prefer_earlier_ones(
    ux_api_env: tuple[httpx.AsyncClient, object],
) -> None:
    """No "beating its author's usual" gate: a big account's ordinary post (0.2x usual, low
    breakout score) is worth replying to; posts past the 90-minute window are not; and between
    two otherwise identical posts, the earlier one ranks higher."""
    from tweettracker.seed import _build_baseline_curves

    client, session_factory = ux_api_env
    now = datetime.now(timezone.utc)
    big_curves = _build_baseline_curves(40_000)

    async with session_factory() as session:  # type: ignore[operator]
        author = TrackedAccount(
            workspace_id=1, x_user_id="u_big_ordinary", handle="big_ordinary_author",
            followers_count=300_000, baseline_curves_json=big_curves,
        )
        session.add(author)
        await session.flush()
        # p50 at 40m ~ 0.27 * 40k = 10.8k, so 2,200 views is ~0.2x usual
        ordinary_fresh = Post(
            workspace_id=1, x_post_id="p_ordinary_fresh", author_account_id=author.id,
            text="Ordinary post.", created_at_x=now - timedelta(minutes=40),
            latest_views=2_200, breakout_score=14.0,
        )
        ordinary_later = Post(
            workspace_id=1, x_post_id="p_ordinary_later", author_account_id=author.id,
            text="Same pace, later.", created_at_x=now - timedelta(minutes=75),
            latest_views=3_600, breakout_score=14.0,
        )
        too_old = Post(
            workspace_id=1, x_post_id="p_ordinary_old", author_account_id=author.id,
            text="Past the window.", created_at_x=now - timedelta(minutes=120),
            latest_views=20_000, breakout_score=90.0,
        )
        session.add_all([ordinary_fresh, ordinary_later, too_old])
        await session.commit()
        fresh_id, later_id, old_id = ordinary_fresh.id, ordinary_later.id, too_old.id

    body = (await client.get("/api/workspaces/1/radar?live_queue=true&limit=100")).json()
    by_id = {it["id"]: it for it in body["items"]}
    assert fresh_id in by_id, "big account's ordinary post must show despite 0.2x pace"
    assert by_id[fresh_id]["velocity_multiple"] < 0.5
    assert by_id[fresh_id]["breakout_score"] < 48.0
    assert old_id not in by_id
    assert all(it["age_minutes"] <= 90 for it in body["items"])
    assert later_id in by_id
    assert (
        by_id[fresh_id]["expected_reply_reach_score"]
        > by_id[later_id]["expected_reply_reach_score"]
    )
