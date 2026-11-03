"""Unit tests for TweetAPIProvider, spend cap guardrails, and LLMProvider router via respx."""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select

from tweettracker.db import build_async_engine
from tweettracker.models import ApiCreditLedger, Base, Workspace
from tweettracker.providers import (
    GeminiProvider,
    GrokProvider,
    ProviderNotConfiguredError,
    SpendCapExceededError,
    TWEETAPI_BASE_URL,
    TweetAPIProvider,
    build_list_query,
    get_llm_provider,
    get_x_data_provider,
    pack_from_queries,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture
async def db_session() -> AsyncSession:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


def test_pack_from_queries_strict_512_limit() -> None:
    handles = [f"@creator_account_num_{i:03d}" for i in range(60)]
    queries = pack_from_queries(
        handles,
        since_id="1895000123456789012",
        exclude_replies=True,
        max_chars=512,
    )
    assert len(queries) >= 3
    for q in queries:
        assert len(q) <= 512
        assert q.endswith(" -filter:replies since_id:1895000123456789012")
        assert "@" not in q

    # Verify all 60 handles appear across packed queries
    joined = " ".join(queries)
    for i in range(60):
        assert f"from:creator_account_num_{i:03d}" in joined


def test_build_list_query() -> None:
    q = build_list_query("184001001", since_id="999", exclude_replies=True)
    assert q == "list:184001001 -filter:replies since_id:999"


@respx.mock
async def test_tweetapi_search_and_list_polling(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWEETAPI_API_KEY", "unit-test-dummy-key")
    ws = Workspace(
        name="Test WS",
        slug="test-ws",
        x_data_provider="tweetapi_com",
        daily_spend_cap_usd=2.0,
        monthly_spend_cap_usd=25.0,
    )
    db_session.add(ws)
    await db_session.commit()

    search_route = respx.get(f"{TWEETAPI_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id_str": "1001",
                        "full_text": "Breakout thread on inference!",
                        "created_at": "2026-11-01T14:00:00Z",
                        "view_count": 12500,
                        "favorite_count": 310,
                        "reply_count": 42,
                        "quote_count": 19,
                        "retweet_count": 28,
                        "bookmark_count": 95,
                        "author": {
                            "id_str": "501",
                            "username": "tensor_architect",
                            "name": "Elena Vance",
                            "followers_count": 140000,
                        },
                    }
                ]
            },
        )
    )

    provider = TweetAPIProvider()
    posts_from = await provider.search_recent_posts(
        db_session, ws, handles=["@tensor_architect", "agentic_runtime"], since_id="900"
    )
    assert len(posts_from) == 1
    assert posts_from[0].x_post_id == "1001"
    assert posts_from[0].views == 12500
    assert posts_from[0].bookmarks == 95
    assert search_route.calls[0].request.headers["X-API-Key"] == "unit-test-dummy-key"

    posts_list = await provider.search_recent_posts(
        db_session, ws, list_id="184001001", since_id="900"
    )
    assert len(posts_list) == 1
    assert (
        search_route.calls[1].request.url.params["query"]
        == "list:184001001 -filter:replies since_id:900"
    )

    ledger_rows = list((await db_session.execute(select(ApiCreditLedger))).scalars().all())
    assert len(ledger_rows) == 2
    assert sum(r.units_consumed for r in ledger_rows) == 2.0


@respx.mock
async def test_tweetapi_batch_200_ids_100_handles_200_following_and_0_unit_usage(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWEETAPI_API_KEY", "unit-test-dummy-key")
    ws = Workspace(
        name="Batch WS",
        slug="batch-ws",
        x_data_provider="tweetapi_com",
        daily_spend_cap_usd=5.0,
        monthly_spend_cap_usd=50.0,
    )
    db_session.add(ws)
    await db_session.commit()

    details_route = respx.get(f"{TWEETAPI_BASE_URL}/tweet/details-by-ids").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "data": [
                    {"id_str": pid, "text": f"Post {pid}", "view_count": 500}
                    for pid in req.url.params["ids"].split(",")
                ]
            },
        )
    )
    users_route = respx.get(f"{TWEETAPI_BASE_URL}/user/by-usernames").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "data": [
                    {"id_str": f"u_{u}", "username": u, "followers_count": 1000}
                    for u in req.url.params["usernames"].split(",")
                ]
            },
        )
    )
    by_username_route = respx.get(f"{TWEETAPI_BASE_URL}/user/by-username").mock(
        return_value=httpx.Response(
            200, json={"data": {"id": "777", "username": "operator_hq", "followerCount": 900}}
        )
    )
    following_calls = 0

    def _following_responder(req: httpx.Request) -> httpx.Response:
        nonlocal following_calls
        following_calls += 1
        assert req.url.params["count"] == "200"
        assert req.url.params["userId"] == "777"
        assert "username" not in req.url.params
        if following_calls == 1:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "f1", "username": "peer_one", "followerCount": 5000}],
                    "pagination": {"nextCursor": "cur_page_2", "prevCursor": "0"},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [{"id_str": "f2", "username": "peer_two", "followers_count": 8000}],
                "next_cursor": "0",
            },
        )

    respx.get(f"{TWEETAPI_BASE_URL}/user/following-list").mock(side_effect=_following_responder)
    respx.get(f"{TWEETAPI_BASE_URL}/account/usage").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"remaining_units": 94320, "used_units": 5680, "plan": "Pro"}},
        )
    )

    provider = TweetAPIProvider()

    # 450 post IDs -> 3 batches (200 + 200 + 50)
    post_ids = [str(10_000 + i) for i in range(450)]
    hydrated_posts = await provider.get_posts_by_ids(db_session, ws, post_ids)
    assert len(hydrated_posts) == 450
    assert details_route.call_count == 3

    # 230 handles -> 3 batches (100 + 100 + 30)
    handles = [f"user_h_{i}" for i in range(230)]
    profiles = await provider.get_profiles_by_handles(db_session, ws, handles)
    assert len(profiles) == 230
    assert users_route.call_count == 3

    # 1 handle -> userId lookup + 2 cursor pages of following-list
    follows = await provider.get_following_list(db_session, ws, "operator_hq", max_pages=5, count=200)
    assert len(follows) == 2
    assert follows[0].followers_count == 5000
    assert following_calls == 2
    assert by_username_route.call_count == 1

    # Repeat crawl reuses the cached userId (no second lookup)
    following_calls = 0
    await provider.get_following_list(db_session, ws, "@Operator_HQ", max_pages=1, count=200)
    assert by_username_route.call_count == 1

    # 0-unit account usage reconciliation
    usage = await provider.get_account_usage(db_session, workspace_id=ws.id)
    assert usage.remaining_units == 94320.0
    assert usage.plan_name == "Pro"

    rows = list((await db_session.execute(select(ApiCreditLedger))).scalars().all())
    assert len(rows) == 11  # 3 + 3 + 1 lookup + 2 + 1 paid + 1 zero-unit usage check
    assert sum(r.units_consumed for r in rows) == 10.0
    assert rows[-1].units_consumed == 0.0
    assert rows[-1].estimated_usd == 0.0
    assert rows[-1].reconciled_balance_units == 94320.0


@respx.mock
async def test_tweetapi_v2_camelcase_post_shape_is_parsed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matches the live `GET /tw-v2/search` response shape (camelCase, top-level media/urls)."""
    monkeypatch.setenv("TWEETAPI_API_KEY", "unit-test-dummy-key")
    ws = Workspace(name="Shape WS", slug="shape-ws", x_data_provider="tweetapi_com")
    db_session.add(ws)
    await db_session.commit()

    author = {"id": "501", "username": "tensor_architect", "followerCount": 140000}
    respx.get(f"{TWEETAPI_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "2001",
                        "conversationId": "2001",
                        "text": "Original post",
                        "type": "tweet",
                        "author": author,
                        "replyTo": None,
                        "quotedTweet": None,
                        "retweetedTweet": None,
                        "likeCount": 296,
                        "retweetCount": 10,
                        "replyCount": 192,
                        "quoteCount": 15,
                        "bookmarkCount": 118,
                        "viewCount": 26496,
                        "media": [{"id": "m1", "type": "video"}],
                        "urls": [],
                        "createdAt": "2026-11-02T18:43:02.000Z",
                    },
                    {
                        "id": "2002",
                        "conversationId": "1999",
                        "text": "Reply",
                        "type": "reply",
                        "author": author,
                        "replyTo": {"tweetId": "1999", "userId": "278", "username": "ajh3_"},
                        "viewCount": 49,
                        "createdAt": "2026-11-02T18:50:00.000Z",
                    },
                    {
                        "id": "2003",
                        "text": "Quote",
                        "type": "quote",
                        "author": author,
                        "quotedTweet": {
                            "id": "1888",
                            "text": "Quoted",
                            "author": {"id": "9", "username": "quoted_person"},
                        },
                        "createdAt": "2026-11-02T18:55:00.000Z",
                    },
                ],
                "pagination": {"nextCursor": "abc", "prevCursor": "def"},
            },
        )
    )

    posts = await TweetAPIProvider().search_recent_posts(db_session, ws, handles=["tensor_architect"])
    by_id = {p.x_post_id: p for p in posts}

    original = by_id["2001"]
    assert (original.views, original.likes, original.replies) == (26496, 296, 192)
    assert (original.quotes, original.reposts, original.bookmarks) == (15, 10, 118)
    assert original.author_followers == 140000
    assert original.has_media and original.media_type == "video"
    assert original.created_at_x.isoformat() == "2026-11-02T18:43:02+00:00"
    assert not (original.is_reply or original.is_quote or original.is_retweet)

    reply = by_id["2002"]
    assert reply.is_reply and reply.in_reply_to_post_id == "1999"
    assert reply.target_author_handle == "ajh3_"
    assert reply.conversation_id == "1999"

    quote = by_id["2003"]
    assert quote.is_quote and quote.quoted_post_id == "1888"
    assert quote.target_author_handle == "quoted_person"


@respx.mock
async def test_tweetapi_retries_rate_limited_429_but_not_quota_exhausted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWEETAPI_API_KEY", "unit-test-dummy-key")
    sleeps: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    monkeypatch.setattr("tweettracker.providers.tweetapi.asyncio.sleep", _fake_sleep)
    ws = Workspace(name="429 WS", slug="ws-429", x_data_provider="tweetapi_com")
    db_session.add(ws)
    await db_session.commit()

    route = respx.get(f"{TWEETAPI_BASE_URL}/tweet/details-by-ids").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}, json={"code": "RATE_LIMITED"}),
            httpx.Response(200, json={"data": [{"id": "1", "text": "ok"}]}),
        ]
    )
    posts = await TweetAPIProvider().get_posts_by_ids(db_session, ws, ["1"])
    assert [p.x_post_id for p in posts] == ["1"]
    assert route.call_count == 2
    assert sleeps == [3.0]

    quota_route = respx.get(f"{TWEETAPI_BASE_URL}/user/by-usernames").mock(
        return_value=httpx.Response(429, json={"code": "QUOTA_EXHAUSTED"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await TweetAPIProvider().get_profiles_by_handles(db_session, ws, ["someone"])
    assert quota_route.call_count == 1


@respx.mock
async def test_spend_cap_blocks_before_http_call(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWEETAPI_API_KEY", "unit-test-dummy-key")
    ws = Workspace(
        name="Tight Cap WS",
        slug="tight-cap-ws",
        x_data_provider="tweetapi_com",
        daily_spend_cap_usd=0.001,  # Only 2 calls allowed at $0.0005/unit
        monthly_spend_cap_usd=10.0,
    )
    db_session.add(ws)
    await db_session.commit()

    route = respx.get(f"{TWEETAPI_BASE_URL}/tweet/details-by-ids").mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    provider = TweetAPIProvider(usd_per_unit=0.0005)
    await provider.get_posts_by_ids(db_session, ws, ["1"])
    await provider.get_posts_by_ids(db_session, ws, ["2"])
    assert route.call_count == 2

    with pytest.raises(SpendCapExceededError):
        await provider.get_posts_by_ids(db_session, ws, ["3"])

    # Verify 3rd HTTP request was NEVER sent!
    assert route.call_count == 2


@respx.mock
async def test_llm_router_gemini_grok_and_stub_slots(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "dummy-gemini-key")
    monkeypatch.setenv("XAI_API_KEY", "dummy-xai-key")

    ws = Workspace(name="LLM WS", slug="llm-ws", daily_spend_cap_usd=2.0, monthly_spend_cap_usd=20.0)
    db_session.add(ws)
    await db_session.commit()

    respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "Updated OonWeightFactor diff summary."}]}}],
                "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 40},
            },
        )
    )
    respx.post("https://api.x.ai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Grok Rust diff summary."}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 35},
            },
        )
    )

    gem = get_llm_provider("google_gemini")
    assert isinstance(gem, GeminiProvider)
    gem_res = await gem.complete("Summarize diff", session=db_session, workspace=ws)
    assert "OonWeightFactor" in gem_res.text

    grok = get_llm_provider("xai_grok")
    assert isinstance(grok, GrokProvider)
    grok_res = await grok.complete("Summarize diff", session=db_session, workspace=ws)
    assert "Grok" in grok_res.text

    for slot in ("openai", "anthropic", "deepinfra"):
        stub = get_llm_provider(slot)
        with pytest.raises(ProviderNotConfiguredError):
            await stub.complete("test")

    for x_slot in ("socialdata", "twitterapi_io", "official_x"):
        x_stub = get_x_data_provider(x_slot)
        with pytest.raises(ProviderNotConfiguredError):
            await x_stub.get_posts_by_ids(db_session, ws, ["1"])


@respx.mock
async def test_get_user_tweets_and_replies_resolves_user_id_and_logs_one_unit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies `/tw-v2/user/tweets-and-replies` resolves handle -> userId, caches it, and logs 1.0 unit."""
    monkeypatch.setenv("TWEETAPI_API_KEY", "unit-test-dummy-key")
    ws = Workspace(
        name="Reply Yield WS",
        slug="reply-yield-ws",
        my_x_handle="gentoftech",
        x_data_provider="tweetapi_com",
    )
    db_session.add(ws)
    await db_session.commit()

    lookup_route = respx.get(f"{TWEETAPI_BASE_URL}/user/by-username").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"id": "888001", "username": "gentoftech", "followerCount": 4250}},
        )
    )
    replies_route = respx.get(f"{TWEETAPI_BASE_URL}/user/tweets-and-replies").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "9001",
                        "type": "reply",
                        "text": "@seed_alpha super clean breakdown on SimClusters!",
                        "author": {
                            "id": req.url.params["userId"],
                            "username": "gentoftech",
                            "followerCount": 4250,
                        },
                        "replyTo": {
                            "tweetId": "7001",
                            "userId": "101",
                            "username": "seed_alpha",
                        },
                        "viewCount": 6840,
                        "likeCount": 42,
                        "replyCount": 7,
                        "createdAt": "2026-11-02T18:00:00.000Z",
                    },
                    {
                        "id": "9002",
                        "type": "quote",
                        "text": "Worth reading closely:",
                        "author": {
                            "id": req.url.params["userId"],
                            "username": "gentoftech",
                            "followerCount": 4250,
                        },
                        "quotedTweet": {
                            "id": "7002",
                            "text": "Original thread",
                            "author": {"id": "102", "username": "seed_beta"},
                        },
                        "viewCount": 3120,
                        "createdAt": "2026-11-02T18:15:00.000Z",
                    },
                ]
            },
        )
    )

    provider = TweetAPIProvider()
    records = await provider.get_user_tweets_and_replies(db_session, ws, "@gentoftech", count=40)
    assert len(records) == 2
    assert records[0].is_reply is True
    assert records[0].in_reply_to_post_id == "7001"
    assert records[0].target_author_handle == "seed_alpha"
    assert records[0].views == 6840
    assert records[0].author_followers == 4250
    assert records[1].is_quote is True
    assert records[1].quoted_post_id == "7002"
    assert records[1].target_author_handle == "seed_beta"

    # Second call reuses cached userId -> still only 1 lookup call
    await provider.get_user_tweets_and_replies(db_session, ws, "gentoftech", count=20)
    assert lookup_route.call_count == 1
    assert replies_route.call_count == 2

    ledger_rows = list(
        (
            await db_session.execute(
                select(ApiCreditLedger).where(
                    ApiCreditLedger.endpoint == "/tw-v2/user/tweets-and-replies"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(ledger_rows) == 2
    assert all(r.units_consumed == 1.0 for r in ledger_rows)


@respx.mock
async def test_get_account_usage_parses_live_camel_case_shape_and_reconciles_throttle(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies `/tw-v2/account/usage` parses live `data.availableUnits` + `data.plan` object
    (including `0` remaining units) so `reconciled_balance` throttle/pause rules fire."""
    from tweettracker.providers import estimate_workspace_monthly_units

    monkeypatch.setenv("TWEETAPI_API_KEY", "unit-test-dummy-key")
    ws = Workspace(
        name="Wallet Reconcile WS",
        slug="wallet-reconcile-ws",
        x_data_provider="tweetapi_com",
        monthly_unit_budget=100_000,
    )
    db_session.add(ws)
    await db_session.commit()

    usage_route = respx.get(f"{TWEETAPI_BASE_URL}/account/usage").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": {
                        "availableUnits": 14_000,
                        "usedUnits": 86_000,
                        "plan": {
                            "name": "Pro 100k",
                            "remainingUnits": 14_000,
                            "usedUnits": 86_000,
                        },
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": {
                        "availableUnits": 0,
                        "plan": {
                            "name": "Pro 100k",
                            "remainingUnits": 0,
                            "usedUnits": 100_000,
                        },
                    }
                },
            ),
        ]
    )

    provider = TweetAPIProvider()

    # 14,000 remaining units (14% of 100k budget) -> throttled even with 0 local logged units
    usage_low = await provider.get_account_usage(db_session, workspace_id=ws.id)
    await db_session.commit()
    assert usage_low.remaining_units == 14_000.0
    assert usage_low.used_units == 86_000.0
    assert usage_low.plan_name == "Pro 100k"

    proj_low = await estimate_workspace_monthly_units(db_session, ws)
    assert proj_low["reconciled_balance_units"] == 14_000.0
    assert proj_low["discovery_throttle_mode"] == "throttled"

    # 0 remaining units (0% of 100k budget) -> preserved as 0.0 (not dropped by truthy `or`) -> paused
    usage_zero = await provider.get_account_usage(db_session, workspace_id=ws.id)
    await db_session.commit()
    assert usage_zero.remaining_units == 0.0
    assert usage_zero.used_units == 100_000.0
    assert usage_route.call_count == 2

    proj_zero = await estimate_workspace_monthly_units(db_session, ws)
    assert proj_zero["reconciled_balance_units"] == 0.0
    assert proj_zero["discovery_throttle_mode"] == "paused"
