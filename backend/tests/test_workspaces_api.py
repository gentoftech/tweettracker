"""Integration tests for FastAPI multi-workspace CRUD, communities, spend, algo params & auth."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import httpx
import pytest

from tweettracker.api.main import create_app
from tweettracker.db import configure_database, dispose_database
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


async def test_health_and_seeded_multi_workspaces(api_client: httpx.AsyncClient) -> None:
    health = (await api_client.get("/api/health")).json()
    assert health["status"] == "ok"
    assert health["upstream_commit_sha"] == "9106e9f"
    assert health["auth_required"] is False

    ws_resp = await api_client.get("/api/workspaces")
    assert ws_resp.status_code == 200
    workspaces = ws_resp.json()
    assert len(workspaces) == 2

    ws1, ws2 = workspaces[0], workspaces[1]
    assert ws1["name"] == "AI, Startups & SMB Ops"
    assert ws1["my_x_handle"] == "operator_hq"
    assert len(ws1["communities"]) == 3
    assert ws1["tracked_accounts_count"] == 36
    assert ws1["posts_count"] >= 60

    assert ws2["name"] == "FinTech & DevTools Research"
    assert ws2["my_x_handle"] is None
    assert len(ws2["communities"]) == 4
    assert ws2["tracked_accounts_count"] == 24

    ov1 = (await api_client.get(f"/api/workspaces/{ws1['id']}/overview")).json()
    assert ov1["communities_count"] == 3
    assert ov1["tracked_accounts_count"] == 36
    assert ov1["mutual_accounts_count"] > 0
    assert ov1["rising_candidates_count"] == 8
    assert len(ov1["top_live_posts"]) > 0

    ov2 = (await api_client.get(f"/api/workspaces/{ws2['id']}/overview")).json()
    assert ov2["communities_count"] == 4
    assert ov2["mutual_accounts_count"] == 0


async def test_workspace_and_community_crud_plus_spend_and_algo(
    api_client: httpx.AsyncClient,
) -> None:
    create_resp = await api_client.post(
        "/api/workspaces",
        json={
            "name": "Robotics & Embodied AI",
            "my_x_handle": "@robo_founder",
            "x_data_provider": "fixture",
            "llm_provider": "google_gemini",
            "daily_spend_cap_usd": 4.5,
            "monthly_spend_cap_usd": 40.0,
            "initial_communities": [
                {"name": "Humanoid Hardware", "color": "#f97316"},
                {"name": "World Models", "color": "#06b6d4"},
            ],
        },
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    ws_id = created["id"]
    assert created["my_x_handle"] == "robo_founder"
    assert len(created["communities"]) == 2

    # Update workspace including clearing my_x_handle to None and adding webhook URL
    patch_resp = await api_client.patch(
        f"/api/workspaces/{ws_id}",
        json={
            "my_x_handle": "",
            "radar_poll_minutes": 15,
            "webhook_urls_json": ["https://hooks.example.com/tt"],
        },
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["my_x_handle"] is None
    assert patched["radar_poll_minutes"] == 15
    assert patched["webhook_urls_json"] == ["https://hooks.example.com/tt"]

    # Add a 3rd community
    comm_resp = await api_client.post(
        f"/api/workspaces/{ws_id}/communities",
        json={
            "name": "Industrial Automation",
            "color": "#10b981",
            "x_list_id": "199888777",
        },
    )
    assert comm_resp.status_code == 201
    comm = comm_resp.json()
    assert comm["x_list_id"] == "199888777"

    # Update community
    comm_patch = await api_client.patch(
        f"/api/workspaces/{ws_id}/communities/{comm['id']}",
        json={"name": "Factory Robotics", "watch_threshold_override": 70.0},
    )
    assert comm_patch.status_code == 200
    assert comm_patch.json()["name"] == "Factory Robotics"
    assert comm_patch.json()["watch_threshold_override"] == 70.0

    # Spend summary
    spend_resp = await api_client.get(f"/api/workspaces/{ws_id}/spend")
    assert spend_resp.status_code == 200
    spend = spend_resp.json()
    assert spend["daily_spend_cap_usd"] == 4.5
    assert spend["monthly_spend_cap_usd"] == 40.0

    # Algo params endpoint
    algo_resp = await api_client.get("/api/algo/params")
    assert algo_resp.status_code == 200
    algo_data = algo_resp.json()
    assert algo_data["upstream_commit_sha"] == "9106e9f"
    assert algo_data["vm_ranker_weights"]["share_via_copy_link"] == 20.0
    assert algo_data["vm_ranker_weights"]["bidirectional_follow_reply_weight_boost"] == 15.0
    assert algo_data["home_mixer_weights"]["favorite"] == 0.66
    assert algo_data["pipeline_params"]["dpp_theta"] == 0.65

    # Delete community & workspace
    del_comm = await api_client.delete(f"/api/workspaces/{ws_id}/communities/{comm['id']}")
    assert del_comm.status_code == 204

    del_ws = await api_client.delete(f"/api/workspaces/{ws_id}")
    assert del_ws.status_code == 204

    get_deleted = await api_client.get(f"/api/workspaces/{ws_id}")
    assert get_deleted.status_code == 404


async def test_optional_admin_password_enforcement(
    api_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWEETTRACKER_ADMIN_PASSWORD", "test-only-lock-token")

    # Health remains public and signals auth_required = True
    health = (await api_client.get("/api/health")).json()
    assert health["auth_required"] is True

    # Unauthenticated workspace call returns 401
    unauth = await api_client.get("/api/workspaces")
    assert unauth.status_code == 401

    # Wrong password returns 401
    wrong_verify = await api_client.post("/api/auth/verify", json={"password": "wrong"})
    assert wrong_verify.status_code == 401

    # Valid Bearer token & X-Admin-Password header both succeed
    ok_bearer = await api_client.get(
        "/api/workspaces",
        headers={"Authorization": "Bearer test-only-lock-token"},
    )
    assert ok_bearer.status_code == 200

    ok_header = await api_client.get(
        "/api/workspaces",
        headers={"X-Admin-Password": "test-only-lock-token"},
    )
    assert ok_header.status_code == 200
