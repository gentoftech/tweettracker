"""Tests for Live Graph, Encrypted In-App Keys & Cloud Hosting Prep:
1. Clean cloud boot (`seed_fixtures=False`) creates 0 demo workspaces while seeding Algo v1 (`9106e9f`).
2. Encrypted-at-rest `app_secrets` vault (`GET/PUT /api/settings/secrets`), masked hints, and env fallback.
3. Live `TweetAPIProvider` seed-to-graph crawler (`crawl_live_seed_network`):
   - `@my_x_handle` mutual resolution (`followed_by_me` + `follows_me` -> `is_mutual_20x`)
   - Seed-to-seed `follow` + `quote` edges
   - Unlimited-size 2-hop snowball auto-add (`140k` niche peer auto-added; `5M` passive celebrity rejected)
   - `rising_only=true` filter remains scoped strictly to `<50k` accounts
4. Organic force-directed graph layout (`compute_bridges_and_layout`) produces bounded, non-overlapping coordinates.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
import math
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from reach_mocks import reach_handlers
from tweettracker.api.main import create_app
from tweettracker.crypto import clear_runtime_secrets, decrypt_secret
from tweettracker.db import configure_database, dispose_database
from tweettracker.models import AppSecret, RelationshipEdge, TrackedAccount
from tweettracker.seed import bootstrap_database


@pytest.fixture
async def clean_cloud_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncGenerator[tuple[httpx.AsyncClient, object], None]:
    db_file = tmp_path / "clean_cloud.db"
    vault_file = tmp_path / ".test_vault.key"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("AUTO_MIGRATE", "true")
    monkeypatch.setenv("SEED_FIXTURES", "false")
    monkeypatch.setenv("TWEETTRACKER_VAULT_FILE", str(vault_file))
    monkeypatch.delenv("TWEETTRACKER_SECRET_KEY", raising=False)
    monkeypatch.delenv("TWEETTRACKER_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("TWEETAPI_API_KEY", raising=False)

    clear_runtime_secrets()
    engine, session_factory = configure_database(db_url)
    await bootstrap_database(engine, session_factory, auto_migrate=True, seed_fixtures=False)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, session_factory

    clear_runtime_secrets()
    await dispose_database()


@pytest.mark.asyncio
async def test_clean_cloud_boot_seeds_algo_v1_without_demo_workspaces(
    clean_cloud_env: tuple[httpx.AsyncClient, object],
) -> None:
    client, _ = clean_cloud_env

    ws_resp = await client.get("/api/workspaces")
    assert ws_resp.status_code == 200
    assert ws_resp.json() == []

    algo_resp = await client.get("/api/algo/params")
    assert algo_resp.status_code == 200
    algo_body = algo_resp.json()
    assert algo_body["upstream_commit_sha"] == "9106e9f"
    assert algo_body["active_version"] is not None
    assert algo_body["active_version"]["version_number"] == 1


@pytest.mark.asyncio
async def test_encrypted_in_app_secrets_vault_masking_and_env_fallback(
    clean_cloud_env: tuple[httpx.AsyncClient, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session_factory = clean_cloud_env
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "test-env-placeholder-1111")

    # 1. Initial GET reflects env fallback without exposing raw secret
    get_resp = await client.get("/api/settings/secrets")
    assert get_resp.status_code == 200
    by_key = {item["key_name"]: item for item in get_resp.json()["secrets"]}

    assert by_key["TWEETAPI_API_KEY"]["configured"] is False
    assert by_key["TWEETAPI_API_KEY"]["source"] == "none"

    assert by_key["GOOGLE_AI_API_KEY"]["configured"] is True
    assert by_key["GOOGLE_AI_API_KEY"]["source"] == "env"
    assert by_key["GOOGLE_AI_API_KEY"]["masked_hint"] == "••••••••1111"
    assert "test-env-placeholder-1111" not in get_resp.text

    # 2. Save in-app encrypted secrets
    put_resp = await client.put(
        "/api/settings/secrets",
        json={
            "secrets": {
                "TWEETAPI_API_KEY": "test-tweetapi-key-9876",
                "GOOGLE_AI_API_KEY": "test-gemini-override-4321",
            }
        },
    )
    assert put_resp.status_code == 200
    updated_by_key = {item["key_name"]: item for item in put_resp.json()["secrets"]}
    assert updated_by_key["TWEETAPI_API_KEY"]["configured"] is True
    assert updated_by_key["TWEETAPI_API_KEY"]["source"] == "app"
    assert updated_by_key["TWEETAPI_API_KEY"]["masked_hint"] == "••••••••9876"
    assert updated_by_key["GOOGLE_AI_API_KEY"]["source"] == "app"
    assert updated_by_key["GOOGLE_AI_API_KEY"]["masked_hint"] == "••••••••4321"
    assert "test-tweetapi-key-9876" not in put_resp.text

    # Verify ciphertext at rest in SQLite is genuinely Fernet-encrypted
    async with session_factory() as session:
        row = await session.get(AppSecret, "TWEETAPI_API_KEY")
        assert row is not None
        assert row.encrypted_value != "test-tweetapi-key-9876"
        assert "9876" not in row.encrypted_value
        assert decrypt_secret(row.encrypted_value) == "test-tweetapi-key-9876"

    # Verify /api/health immediately sees tweetapi_com as configured
    health_resp = await client.get("/api/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["providers_configured"]["tweetapi_com"] is True

    # 3. Clearing GOOGLE_AI_API_KEY reverts cleanly to server env fallback
    clear_resp = await client.put(
        "/api/settings/secrets",
        json={"secrets": {"GOOGLE_AI_API_KEY": ""}},
    )
    assert clear_resp.status_code == 200
    reverted = {item["key_name"]: item for item in clear_resp.json()["secrets"]}
    assert reverted["GOOGLE_AI_API_KEY"]["configured"] is True
    assert reverted["GOOGLE_AI_API_KEY"]["source"] == "env"
    assert reverted["GOOGLE_AI_API_KEY"]["masked_hint"] == "••••••••1111"


@pytest.mark.asyncio
async def test_live_seed_crawler_mutuals_edges_and_unlimited_size_snowball(
    clean_cloud_env: tuple[httpx.AsyncClient, object],
) -> None:
    client, session_factory = clean_cloud_env

    # Configure live TweetAPI key via encrypted in-app vault
    await client.put(
        "/api/settings/secrets",
        json={"secrets": {"TWEETAPI_API_KEY": "test-live-crawler-key-5555"}},
    )

    # Create workspace with @gentoftech handle and live tweetapi_com provider
    create_ws = await client.post(
        "/api/workspaces",
        json={
            "name": "GentOfTech Live",
            "my_x_handle": "gentoftech",
            "x_data_provider": "tweetapi_com",
            "llm_provider": "google_gemini",
            "initial_communities": [
                {"name": "Applied AI", "color": "#38bdf8"},
                {"name": "GTM Builders", "color": "#a78bfa"},
            ],
        },
    )
    assert create_ws.status_code == 201
    ws = create_ws.json()
    ws_id = ws["id"]
    ai_comm_id = ws["communities"][0]["id"]

    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.tweetapi.com/tw-v2/user/by-usernames").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "101",
                            "username": "seed_alpha",
                            "name": "Seed Alpha",
                            "description": "AI agent infra",
                            "followers_count": 32_000,
                            "following_count": 410,
                        },
                        {
                            "id": "102",
                            "username": "seed_beta",
                            "name": "Seed Beta",
                            "description": "GTM & evals",
                            "followers_count": 44_000,
                            "following_count": 520,
                        },
                    ]
                },
            )
        )

        user_ids = {"gentoftech": "999", "seed_alpha": "101", "seed_beta": "102"}

        def _by_username_handler(request: httpx.Request) -> httpx.Response:
            uname = (request.url.params.get("username") or "").lower()
            if uname not in user_ids:
                return httpx.Response(404, json={"statusCode": 404, "message": "Not found"})
            return httpx.Response(
                200, json={"data": {"id": user_ids[uname], "username": uname}}
            )

        router.get("https://api.tweetapi.com/tw-v2/user/by-username").mock(
            side_effect=_by_username_handler
        )
        # Reply reach under each candidate: early replies under the 5M celebrity go unseen
        timeline_handler, details_handler = reach_handlers(
            now=lambda: datetime.now(timezone.utc), views_by_user_id={"901": 15}
        )
        router.get("https://api.tweetapi.com/tw-v2/user/tweets-and-replies").mock(
            side_effect=timeline_handler
        )
        router.get("https://api.tweetapi.com/tw-v2/tweet/details").mock(side_effect=details_handler)

        def _following_handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("X-API-Key") == "test-live-crawler-key-5555"
            # following-list only accepts a numeric userId (never a username)
            assert "username" not in request.url.params
            names_by_id = {v: k for k, v in user_ids.items()}
            uname = names_by_id.get(request.url.params.get("userId") or "", "")
            if uname == "gentoftech":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "101",
                                "username": "seed_alpha",
                                "name": "Seed Alpha",
                                "followers_count": 32_000,
                            }
                        ]
                    },
                )
            if uname == "seed_alpha":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "999",
                                "username": "gentoftech",
                                "name": "GentOfTech",
                                "followers_count": 12_000,
                            },
                            {
                                "id": "102",
                                "username": "seed_beta",
                                "name": "Seed Beta",
                                "followers_count": 44_000,
                            },
                            {
                                "id": "201",
                                "username": "niche_heavy_140k",
                                "name": "Niche Heavy 140k",
                                "description": "140k follower niche AI founder",
                                "followers_count": 140_000,
                                "following_count": 600,
                            },
                            {
                                "id": "901",
                                "username": "mega_celeb_5m",
                                "name": "Mega Celebrity",
                                "followers_count": 5_000_000,
                                "following_count": 100,
                            },
                        ]
                    },
                )
            if uname == "seed_beta":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "201",
                                "username": "niche_heavy_140k",
                                "name": "Niche Heavy 140k",
                                "description": "140k follower niche AI founder",
                                "followers_count": 140_000,
                                "following_count": 600,
                            },
                            {
                                "id": "901",
                                "username": "mega_celeb_5m",
                                "name": "Mega Celebrity",
                                "followers_count": 5_000_000,
                                "following_count": 100,
                            },
                        ]
                    },
                )
            return httpx.Response(200, json={"data": []})

        router.get("https://api.tweetapi.com/tw-v2/user/following-list").mock(
            side_effect=_following_handler
        )

        router.get("https://api.tweetapi.com/tw-v2/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "7001",
                            "text": "Spot-on breakdown by @seed_alpha and @niche_heavy_140k on agent evals",
                            "created_at": "2026-11-02T14:00:00Z",
                            "is_quote_status": True,
                            "author": {
                                "id": "102",
                                "username": "seed_beta",
                                "name": "Seed Beta",
                                "followers_count": 44_000,
                            },
                            "views_count": 18_500,
                            "favorite_count": 210,
                            "reply_count": 24,
                            "retweet_count": 19,
                            "quote_count": 8,
                        }
                    ]
                },
            )
        )

        imp_resp = await client.post(
            f"/api/workspaces/{ws_id}/network/accounts/import",
            json={
                "handles_text": "@seed_alpha, @seed_beta",
                "community_ids": [ai_comm_id],
                "resolve_via_provider": True,
            },
        )
        assert imp_resp.status_code == 201, imp_resp.text
        imp_json = imp_resp.json()
        # Manual import is strictly pure: imports only requested seeds and stages 2-hop peers
        assert imp_json["imported_count"] == 2
        assert imp_json["total_tracked_accounts"] == 2

        sync_resp = await client.post(f"/api/workspaces/{ws_id}/network/auto-sync")
        assert sync_resp.status_code == 200, sync_resp.text
        sync_json = sync_resp.json()
        assert "niche_heavy_140k" in sync_json["auto_added_handles"]
        assert "mega_celeb_5m" not in sync_json["auto_added_handles"]

    # Verify accounts, mutual detection, and SimClusters core-affinity auto-add (no 50k or 2M size cliff)
    accts_resp = await client.get(f"/api/workspaces/{ws_id}/network/accounts")
    assert accts_resp.status_code == 200
    accts_by_h = {a["handle"]: a for a in accts_resp.json()}

    assert "seed_alpha" in accts_by_h
    assert accts_by_h["seed_alpha"]["followed_by_me"] is True
    assert accts_by_h["seed_alpha"]["follows_me"] is True
    assert accts_by_h["seed_alpha"]["is_mutual_20x"] is True

    # >50k niche peer engaged + co-followed by core seeds IS auto-added via core affinity >= 0.55
    assert "niche_heavy_140k" in accts_by_h
    assert accts_by_h["niche_heavy_140k"]["is_auto_tracked"] is True
    assert accts_by_h["niche_heavy_140k"]["followers_count"] == 140_000
    assert accts_by_h["niche_heavy_140k"]["core_affinity_score"] >= 0.55

    # Passive celebrity with 0 conversational ties scores < 0.55 and is NOT auto-added
    assert "mega_celeb_5m" not in accts_by_h

    # `rising_only=true` filter remains strictly scoped to <50k emerging accounts
    rising_resp = await client.get(
        f"/api/workspaces/{ws_id}/network/accounts?rising_only=true"
    )
    assert rising_resp.status_code == 200
    rising_handles = {a["handle"] for a in rising_resp.json()}
    assert "niche_heavy_140k" not in rising_handles

    # Verify seed-to-seed follow + quote edges exist in DB
    async with session_factory() as session:
        edges = list(
            (
                await session.execute(
                    select(RelationshipEdge).where(RelationshipEdge.workspace_id == ws_id)
                )
            )
            .scalars()
            .all()
        )
        edge_types = {e.edge_type for e in edges}
        assert "follow" in edge_types
        assert "quote" in edge_types
        assert len(edges) >= 4


@pytest.mark.asyncio
async def test_organic_force_layout_produces_bounded_non_overlapping_positions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "layout_test.db"
    vault_file = tmp_path / ".layout_vault.key"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("AUTO_MIGRATE", "true")
    monkeypatch.setenv("SEED_FIXTURES", "true")
    monkeypatch.setenv("TWEETTRACKER_VAULT_FILE", str(vault_file))
    monkeypatch.delenv("TWEETTRACKER_ADMIN_PASSWORD", raising=False)

    engine, session_factory = configure_database(db_url)
    try:
        await bootstrap_database(
            engine, session_factory, auto_migrate=True, seed_fixtures=True
        )
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            graph_resp = await client.get("/api/workspaces/1/network/graph?mode=bridges")
            assert graph_resp.status_code == 200
            nodes = graph_resp.json()["nodes"]
            assert len(nodes) >= 15

            xs = [float(n["n_pole_xy"]["x"]) for n in nodes]
            ys = [float(n["n_pole_xy"]["y"]) for n in nodes]
            assert all(-1.05 <= x <= 1.05 for x in xs)
            assert all(-1.05 <= y <= 1.05 for y in ys)
            assert (max(xs) - min(xs)) > 0.5
            assert (max(ys) - min(ys)) > 0.5

            # Ensure collision relaxation prevents exact node overlap
            min_pair_dist = min(
                math.hypot(xs[i] - xs[j], ys[i] - ys[j])
                for i in range(len(nodes))
                for j in range(i + 1, len(nodes))
            )
            assert min_pair_dist > 0.01
    finally:
        clear_runtime_secrets()
        await dispose_database()
