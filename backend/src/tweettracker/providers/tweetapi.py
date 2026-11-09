"""TweetAPI.com (https://api.tweetapi.com/tw-v2) Build 1 adapter with batching & spend caps."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import os
from typing import Any
import weakref

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.crypto import resolve_secret
from tweettracker.models import Workspace
from tweettracker.providers.base import (
    PostRecord,
    ProfileRecord,
    ReplyRecord,
    ProviderNotConfiguredError,
    ProviderUsageInfo,
)
from tweettracker.providers.spend import enforce_spend_caps, log_api_credit

TWEETAPI_BASE_URL = "https://api.tweetapi.com/tw-v2"
DEFAULT_USD_PER_UNIT = 0.00025  # 100k monthly plan parity ($25 / 100,000 units)

# TweetAPI rate-limits per account per rolling second (Free plan: 2 req/s) across all keys,
# and the api + worker processes share one account, so each process paces itself to 1 req/s.
DEFAULT_MIN_INTERVAL_SECS = 1.0
MAX_RATE_LIMIT_RETRIES = 2
MAX_RETRY_AFTER_SECS = 30.0
# 429 bodies carrying these codes are not transient; retrying only burns time.
NON_RETRYABLE_429_CODES = ("QUOTA_EXHAUSTED", "SUBSCRIPTION_INACTIVE")

# Per-event-loop request pacing state: loop -> (lock, [last_request_monotonic])
_PACING: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[asyncio.Lock, list[float]]] = (
    weakref.WeakKeyDictionary()
)
# Process-local `handle -> X user id` cache; following-list only accepts numeric `userId`.
_USER_ID_CACHE: dict[str, str] = {}


def _clean_handle(raw: str) -> str:
    return raw.strip().lstrip("@").strip()


async def _pace_request(min_interval_secs: float) -> None:
    if min_interval_secs <= 0:
        return
    loop = asyncio.get_running_loop()
    state = _PACING.get(loop)
    if state is None:
        state = (asyncio.Lock(), [0.0])
        _PACING[loop] = state
    lock, last = state
    async with lock:
        wait = last[0] + min_interval_secs - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        last[0] = loop.time()


def _retry_after_secs(resp: httpx.Response) -> float:
    try:
        secs = float(resp.headers.get("Retry-After", "2"))
    except ValueError:
        secs = 2.0
    return min(max(secs, 1.0), MAX_RETRY_AFTER_SECS)


def pack_from_queries(
    handles: Sequence[str],
    *,
    since_id: str | None = None,
    exclude_replies: bool = True,
    max_chars: int = 512,
) -> list[str]:
    """Packs X handles into `<=512`-character `(from:h1 OR from:h2) -filter:replies since_id:...` queries."""
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in handles:
        h = _clean_handle(raw)
        if not h:
            continue
        key = h.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(h)

    if not deduped:
        return []

    suffix_parts: list[str] = []
    if exclude_replies:
        suffix_parts.append("-filter:replies")
    if since_id and since_id.strip():
        suffix_parts.append(f"since_id:{since_id.strip()}")
    suffix = (" " + " ".join(suffix_parts)) if suffix_parts else ""

    queries: list[str] = []
    current_terms: list[str] = []

    for handle in deduped:
        term = f"from:{handle}"
        candidate_single = f"({term}){suffix}"
        if len(candidate_single) > max_chars:
            raise ValueError(
                f"Handle '{handle}' with query suffix exceeds max_chars={max_chars}"
            )
        tentative_terms = current_terms + [term]
        tentative_query = f"({' OR '.join(tentative_terms)}){suffix}"
        if len(tentative_query) <= max_chars:
            current_terms = tentative_terms
        else:
            queries.append(f"({' OR '.join(current_terms)}){suffix}")
            current_terms = [term]

    if current_terms:
        queries.append(f"({' OR '.join(current_terms)}){suffix}")

    return queries


def build_list_query(
    list_id: str,
    *,
    since_id: str | None = None,
    exclude_replies: bool = True,
) -> str:
    cleaned_id = list_id.strip()
    if not cleaned_id:
        raise ValueError("list_id must not be empty")
    parts = [f"list:{cleaned_id}"]
    if exclude_replies:
        parts.append("-filter:replies")
    if since_id and since_id.strip():
        parts.append(f"since_id:{since_id.strip()}")
    return " ".join(parts)


def _parse_x_datetime(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        try:
            dt = parsedate_to_datetime(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _extract_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            val = payload.get(key)
            if isinstance(val, list):
                return [item for item in val if isinstance(item, dict)]
            if isinstance(val, dict):
                for sub_key in ("tweets", "users", "items", "list", "results"):
                    sub_val = val.get(sub_key)
                    if isinstance(sub_val, list):
                        return [item for item in sub_val if isinstance(item, dict)]
    return []


def _normalize_profile(raw: dict[str, Any]) -> ProfileRecord:
    legacy = raw.get("legacy") if isinstance(raw.get("legacy"), dict) else {}
    x_uid = str(
        raw.get("id_str")
        or raw.get("rest_id")
        or raw.get("id")
        or legacy.get("id_str")
        or ""
    )
    handle = _clean_handle(
        str(
            raw.get("username")
            or raw.get("screen_name")
            or legacy.get("screen_name")
            or ""
        )
    )
    display_name = str(
        raw.get("name") or raw.get("display_name") or legacy.get("name") or handle
    )
    bio = str(raw.get("description") or raw.get("bio") or legacy.get("description") or "")
    avatar = (
        raw.get("profile_image_url_https")
        or raw.get("avatar_url")
        or raw.get("avatar")
        or legacy.get("profile_image_url_https")
    )
    followers = int(
        raw.get("followers_count")
        or raw.get("followersCount")
        or raw.get("followerCount")
        or legacy.get("followers_count")
        or 0
    )
    following = int(
        raw.get("friends_count")
        or raw.get("following_count")
        or raw.get("followingCount")
        or legacy.get("friends_count")
        or 0
    )
    return ProfileRecord(
        x_user_id=x_uid,
        handle=handle,
        display_name=display_name,
        bio=bio,
        avatar_url=str(avatar) if avatar else None,
        followers_count=followers,
        following_count=following,
        is_premium=bool(raw.get("isBlueVerified") or raw.get("is_blue_verified")),
    )


def _normalize_post(raw: dict[str, Any]) -> PostRecord:
    author_obj = raw.get("author") or raw.get("user") or {}
    if not isinstance(author_obj, dict):
        author_obj = {}
    author_prof = _normalize_profile(author_obj)

    post_id = str(raw.get("id_str") or raw.get("rest_id") or raw.get("id") or "")
    author_uid = author_prof.x_user_id or str(raw.get("author_id") or "")
    author_handle = author_prof.handle or _clean_handle(str(raw.get("author_handle") or ""))
    text = str(raw.get("full_text") or raw.get("text") or "")
    # TweetAPI v2 labels every post `type: tweet | reply | quote | retweet`
    post_type = str(raw.get("type") or "").lower()

    # TweetAPI v2 shape: `replyTo: {tweetId, userId, username}`
    reply_obj = raw.get("replyTo") if isinstance(raw.get("replyTo"), dict) else {}
    in_reply_to = (
        raw.get("in_reply_to_status_id_str")
        or raw.get("in_reply_to_post_id")
        or reply_obj.get("tweetId")
    )
    retweet_obj = next(
        (
            raw.get(k)
            for k in ("retweeted_tweet", "retweetedTweet")
            if isinstance(raw.get(k), dict)
        ),
        None,
    )
    retweeted_id = raw.get("retweeted_status_id_str") or (
        retweet_obj.get("id") if retweet_obj else None
    )
    quoted_obj = next(
        (
            raw.get(k)
            for k in ("quoted_tweet", "quotedTweet", "quoted_status")
            if isinstance(raw.get(k), dict)
        ),
        None,
    )
    quoted_id = raw.get("quoted_status_id_str") or raw.get("quoted_post_id") or (
        quoted_obj.get("id_str") or quoted_obj.get("id") if quoted_obj else None
    )
    quoted_author_handle = ""
    if quoted_obj:
        q_author = quoted_obj.get("author") or quoted_obj.get("user") or {}
        if isinstance(q_author, dict):
            quoted_author_handle = _normalize_profile(q_author).handle
    reply_to_handle = _clean_handle(
        str(
            raw.get("in_reply_to_screen_name")
            or raw.get("in_reply_to_username")
            or reply_obj.get("username")
            or ""
        )
    )
    target_author_handle = quoted_author_handle or reply_to_handle or None
    conv_id = (
        raw.get("conversation_id_str") or raw.get("conversation_id") or raw.get("conversationId")
    )

    # TweetAPI v2 puts `urls` / `media` at the top level instead of under `entities`
    entities = raw.get("entities") if isinstance(raw.get("entities"), dict) else {}
    urls = next(
        (src.get("urls") for src in (entities, raw) if isinstance(src.get("urls"), list)), []
    )
    media_list = next(
        (src.get("media") for src in (entities, raw) if isinstance(src.get("media"), list)), []
    )

    has_url = bool(raw.get("has_url")) or len(urls) > 0
    has_media = bool(raw.get("has_media")) or len(media_list) > 0
    media_type = raw.get("media_type")
    if not media_type and media_list and isinstance(media_list[0], dict):
        media_type = media_list[0].get("type")
    video_duration_ms = raw.get("video_duration_ms")

    views_raw = (
        raw.get("view_count")
        or raw.get("views_count")
        or raw.get("viewCount")
        or raw.get("views")
        or 0
    )
    if isinstance(views_raw, dict):
        views_raw = views_raw.get("count") or 0

    return PostRecord(
        x_post_id=post_id,
        author_x_user_id=author_uid,
        author_handle=author_handle,
        author_followers=author_prof.followers_count,
        text=text,
        created_at_x=_parse_x_datetime(raw.get("created_at") or raw.get("createdAt")),
        is_reply=bool(raw.get("is_reply"))
        or post_type == "reply"
        or (in_reply_to is not None)
        or bool(reply_to_handle),
        is_retweet=bool(raw.get("is_retweet"))
        or post_type == "retweet"
        or (retweeted_id is not None),
        is_quote=bool(raw.get("is_quote") or raw.get("is_quote_status"))
        or post_type == "quote"
        or (quoted_id is not None),
        in_reply_to_post_id=str(in_reply_to) if in_reply_to else None,
        conversation_id=str(conv_id) if conv_id else None,
        quoted_post_id=str(quoted_id) if quoted_id else None,
        target_author_handle=target_author_handle,
        has_url=has_url,
        has_media=has_media,
        media_type=str(media_type) if media_type else None,
        video_duration_ms=int(video_duration_ms) if video_duration_ms is not None else None,
        ancestor_author_ids=[
            str(uid) for uid in (raw.get("ancestor_author_ids") or [])
        ],
        views=int(views_raw or 0),
        likes=int(
            raw.get("favorite_count")
            or raw.get("like_count")
            or raw.get("likeCount")
            or raw.get("likes")
            or 0
        ),
        replies=int(raw.get("reply_count") or raw.get("replyCount") or raw.get("replies") or 0),
        quotes=int(raw.get("quote_count") or raw.get("quoteCount") or raw.get("quotes") or 0),
        reposts=int(
            raw.get("retweet_count") or raw.get("retweetCount") or raw.get("reposts") or 0
        ),
        bookmarks=int(
            raw.get("bookmark_count") or raw.get("bookmarkCount") or raw.get("bookmarks") or 0
        ),
    )


class TweetAPIProvider:
    """TweetAPI.com REST v2 adapter (`1 API unit / HTTP request`, `0` units for `/account/usage`)."""

    provider_name = "tweetapi_com"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = TWEETAPI_BASE_URL,
        usd_per_unit: float = DEFAULT_USD_PER_UNIT,
        timeout_secs: float = 20.0,
        client: httpx.AsyncClient | None = None,
        min_interval_secs: float | None = None,
    ) -> None:
        self._explicit_api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.usd_per_unit = float(usd_per_unit)
        self.timeout_secs = timeout_secs
        self._client = client
        self.min_interval_secs = (
            float(min_interval_secs)
            if min_interval_secs is not None
            else float(
                os.environ.get("TWEETAPI_MIN_INTERVAL_SECS", "").strip()
                or DEFAULT_MIN_INTERVAL_SECS
            )
        )

    def _resolve_api_key(self) -> str:
        key = (
            self._explicit_api_key
            if self._explicit_api_key is not None
            else resolve_secret("TWEETAPI_API_KEY")
        ).strip()
        if not key:
            raise ProviderNotConfiguredError(
                "TWEETAPI_API_KEY is not set in Settings or environment variables."
            )
        return key

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {
            "X-API-Key": self._resolve_api_key(),
            "Accept": "application/json",
        }
        url = f"{self.base_url}{path}"
        if self._client is not None:
            return await self._send_paced(self._client, url, params, headers)

        async with httpx.AsyncClient(timeout=self.timeout_secs) as client:
            return await self._send_paced(client, url, params, headers)

    async def _send_paced(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """Paces requests under TweetAPI's per-second limit and retries transient 429s."""
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            await _pace_request(self.min_interval_secs)
            resp = await client.get(url, params=params, headers=headers)
            if (
                resp.status_code == 429
                and attempt < MAX_RATE_LIMIT_RETRIES
                and not any(code in resp.text for code in NON_RETRYABLE_429_CODES)
            ):
                await asyncio.sleep(_retry_after_secs(resp))
                continue
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {"data": data}
        raise AssertionError("unreachable")  # pragma: no cover

    async def _resolve_user_id(
        self, session: AsyncSession, workspace: Workspace, handle: str
    ) -> str | None:
        """Maps an X handle to its numeric user id (cached; 1 unit per uncached lookup)."""
        key = handle.lower()
        cached = _USER_ID_CACHE.get(key)
        if cached:
            return cached
        await enforce_spend_caps(session, workspace, projected_usd=self.usd_per_unit)
        payload = await self._get_json("/user/by-username", params={"username": handle})
        await log_api_credit(
            session,
            workspace_id=workspace.id,
            provider=self.provider_name,
            endpoint="/tw-v2/user/by-username",
            units_consumed=1.0,
            estimated_usd=self.usd_per_unit,
        )
        data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else None
        candidates = [data_obj] if data_obj else _extract_list(payload, "data", "users")
        for item in candidates:
            prof = _normalize_profile(item)
            if prof.x_user_id and prof.handle.lower() == key:
                _USER_ID_CACHE[key] = prof.x_user_id
                return prof.x_user_id
        return None

    async def search_recent_posts(
        self,
        session: AsyncSession,
        workspace: Workspace,
        *,
        handles: list[str] | None = None,
        list_id: str | None = None,
        since_id: str | None = None,
        exclude_replies: bool = True,
    ) -> list[PostRecord]:
        if list_id and list_id.strip():
            queries = [
                build_list_query(list_id, since_id=since_id, exclude_replies=exclude_replies)
            ]
        elif handles:
            queries = pack_from_queries(
                handles,
                since_id=since_id,
                exclude_replies=exclude_replies,
                max_chars=512,
            )
        else:
            return []

        results: list[PostRecord] = []
        seen_post_ids: set[str] = set()
        endpoint = "/tw-v2/search"

        for query_str in queries:
            await enforce_spend_caps(session, workspace, projected_usd=self.usd_per_unit)
            payload = await self._get_json(
                "/search",
                params={"query": query_str, "type": "Latest"},
            )
            await log_api_credit(
                session,
                workspace_id=workspace.id,
                provider=self.provider_name,
                endpoint=endpoint,
                units_consumed=1.0,
                estimated_usd=self.usd_per_unit,
            )
            for item in _extract_list(payload, "data", "tweets", "results"):
                rec = _normalize_post(item)
                if rec.x_post_id and rec.x_post_id not in seen_post_ids:
                    seen_post_ids.add(rec.x_post_id)
                    results.append(rec)

        return results

    async def get_posts_by_ids(
        self,
        session: AsyncSession,
        workspace: Workspace,
        post_ids: list[str],
        *,
        ledger_label: str | None = None,
    ) -> list[PostRecord]:
        cleaned = [pid.strip() for pid in post_ids if pid and pid.strip()]
        if not cleaned:
            return []

        results: list[PostRecord] = []
        endpoint = ledger_label or "/tw-v2/tweet/details-by-ids"

        # Up to 200 post IDs per 1-unit batch call
        for start in range(0, len(cleaned), 200):
            batch = cleaned[start : start + 200]
            await enforce_spend_caps(session, workspace, projected_usd=self.usd_per_unit)
            payload = await self._get_json(
                "/tweet/details-by-ids",
                params={"ids": ",".join(batch)},
            )
            await log_api_credit(
                session,
                workspace_id=workspace.id,
                provider=self.provider_name,
                endpoint=endpoint,
                units_consumed=1.0,
                estimated_usd=self.usd_per_unit,
            )
            for item in _extract_list(payload, "data", "tweets", "results"):
                rec = _normalize_post(item)
                if rec.x_post_id:
                    results.append(rec)

        return results

    async def get_profiles_by_handles(
        self,
        session: AsyncSession,
        workspace: Workspace,
        handles: list[str],
    ) -> list[ProfileRecord]:
        deduped: list[str] = []
        seen: set[str] = set()
        for raw in handles:
            h = _clean_handle(raw)
            if h and h.lower() not in seen:
                seen.add(h.lower())
                deduped.append(h)

        if not deduped:
            return []

        results: list[ProfileRecord] = []
        endpoint = "/tw-v2/user/by-usernames"

        # Up to 100 handles per 1-unit batch lookup
        for start in range(0, len(deduped), 100):
            batch = deduped[start : start + 100]
            await enforce_spend_caps(session, workspace, projected_usd=self.usd_per_unit)
            payload = await self._get_json(
                "/user/by-usernames",
                params={"usernames": ",".join(batch)},
            )
            await log_api_credit(
                session,
                workspace_id=workspace.id,
                provider=self.provider_name,
                endpoint=endpoint,
                units_consumed=1.0,
                estimated_usd=self.usd_per_unit,
            )
            for item in _extract_list(payload, "data", "users", "results"):
                prof = _normalize_profile(item)
                if prof.handle or prof.x_user_id:
                    results.append(prof)

        return results

    async def get_following_list(
        self,
        session: AsyncSession,
        workspace: Workspace,
        username_or_id: str,
        *,
        max_pages: int = 5,
        count: int = 200,
    ) -> list[ProfileRecord]:
        target = _clean_handle(username_or_id)
        if not target:
            return []
        # following-list only accepts a numeric `userId`, never a username
        user_id = target if target.isdigit() else await self._resolve_user_id(
            session, workspace, target
        )
        if not user_id:
            return []

        page_size = min(max(int(count), 1), 200)
        cursor: str | None = None
        results: list[ProfileRecord] = []
        endpoint = "/tw-v2/user/following-list"

        for _ in range(max(1, max_pages)):
            await enforce_spend_caps(session, workspace, projected_usd=self.usd_per_unit)
            params: dict[str, Any] = {"userId": user_id, "count": page_size}
            if cursor:
                params["cursor"] = cursor

            payload = await self._get_json("/user/following-list", params=params)
            await log_api_credit(
                session,
                workspace_id=workspace.id,
                provider=self.provider_name,
                endpoint=endpoint,
                units_consumed=1.0,
                estimated_usd=self.usd_per_unit,
            )

            items = _extract_list(payload, "data", "users", "following", "results")
            for item in items:
                prof = _normalize_profile(item)
                if prof.handle or prof.x_user_id:
                    results.append(prof)

            pagination = (
                payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
            )
            next_cursor = (
                pagination.get("nextCursor")
                or payload.get("next_cursor")
                or (
                    payload.get("data", {}).get("next_cursor")
                    if isinstance(payload.get("data"), dict)
                    else None
                )
            )
            if not next_cursor or str(next_cursor) in ("0", cursor or ""):
                break
            cursor = str(next_cursor)

        return results

    async def get_user_tweets_and_replies(
        self,
        session: AsyncSession,
        workspace: Workspace,
        username_or_id: str,
        *,
        count: int = 40,
        ledger_label: str | None = None,
    ) -> list[PostRecord]:
        """Fetches recent timeline posts + replies for `username_or_id` via `/tw-v2/user/tweets-and-replies` (1 unit)."""
        target = _clean_handle(username_or_id)
        if not target:
            return []
        user_id = (
            target
            if target.isdigit()
            else await self._resolve_user_id(session, workspace, target)
        )
        if not user_id:
            return []

        page_size = min(max(int(count), 1), 100)
        endpoint = ledger_label or "/tw-v2/user/tweets-and-replies"
        await enforce_spend_caps(session, workspace, projected_usd=self.usd_per_unit)
        payload = await self._get_json(
            "/user/tweets-and-replies",
            params={"userId": user_id, "count": page_size},
        )
        await log_api_credit(
            session,
            workspace_id=workspace.id,
            provider=self.provider_name,
            endpoint=endpoint,
            units_consumed=1.0,
            estimated_usd=self.usd_per_unit,
        )

        results: list[PostRecord] = []
        seen_post_ids: set[str] = set()
        for item in _extract_list(payload, "data", "tweets", "items", "results"):
            rec = _normalize_post(item)
            if rec.x_post_id and rec.x_post_id not in seen_post_ids:
                seen_post_ids.add(rec.x_post_id)
                results.append(rec)
        return results

    async def get_tweet_replies(
        self,
        session: AsyncSession,
        workspace: Workspace,
        tweet_id: str,
        *,
        sort_by: str = "Recency",
        cursor: str | None = None,
    ) -> tuple[list[ReplyRecord], str | None]:
        """One page of replies under `tweet_id` via `/tw-v2/tweet/details` (1 unit per page)."""
        tid = (tweet_id or "").strip()
        if not tid:
            return [], None
        params: dict[str, Any] = {"tweetId": tid, "sortBy": sort_by}
        if cursor:
            params["cursor"] = cursor
        await enforce_spend_caps(session, workspace, projected_usd=self.usd_per_unit)
        payload = await self._get_json("/tweet/details", params=params)
        await log_api_credit(
            session,
            workspace_id=workspace.id,
            provider=self.provider_name,
            endpoint="/tw-v2/tweet/details",
            units_consumed=1.0,
            estimated_usd=self.usd_per_unit,
        )
        data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        replies: list[ReplyRecord] = []
        for item in data_obj.get("replies") or []:
            if not isinstance(item, dict):
                continue
            author = _normalize_profile(item.get("author") or {})
            post_id = str(item.get("id") or item.get("id_str") or "")
            if not post_id:
                continue
            replies.append(
                ReplyRecord(
                    x_post_id=post_id,
                    author_x_user_id=author.x_user_id,
                    author_handle=author.handle,
                    author_display_name=author.display_name,
                    author_followers=author.followers_count,
                    author_premium=author.is_premium,
                    created_at_x=_parse_x_datetime(item.get("createdAt") or item.get("created_at")),
                    views=int(item.get("viewCount") or item.get("view_count") or 0),
                    likes=int(item.get("likeCount") or item.get("favorite_count") or 0),
                )
            )
        next_cursor = data_obj.get("nextCursor") or data_obj.get("next_cursor")
        if not next_cursor:
            pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
            next_cursor = pagination.get("nextCursor")
        return replies, (str(next_cursor) if next_cursor else None)

    async def get_account_usage(
        self,
        session: AsyncSession,
        workspace_id: int | None = None,
    ) -> ProviderUsageInfo:
        """Calls free 0-unit `/tw-v2/account/usage` endpoint to reconcile wallet balance."""
        payload = await self._get_json("/account/usage")
        data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        raw_plan = data_obj.get("plan")
        plan_obj = raw_plan if isinstance(raw_plan, dict) else {}

        def _first_present(*vals: Any) -> Any:
            for v in vals:
                if v is not None and not isinstance(v, (dict, list)):
                    return v
            return None

        remaining = _first_present(
            data_obj.get("availableUnits"),
            plan_obj.get("remainingUnits"),
            plan_obj.get("availableUnits"),
            data_obj.get("remainingUnits"),
            data_obj.get("remaining_units"),
            plan_obj.get("remaining_units"),
            data_obj.get("remaining_credits"),
            data_obj.get("balance"),
        )
        used = _first_present(
            data_obj.get("usedUnits"),
            plan_obj.get("usedUnits"),
            data_obj.get("used_units"),
            plan_obj.get("used_units"),
            data_obj.get("used_credits"),
        )
        if isinstance(raw_plan, str) and raw_plan.strip():
            plan_name: Any = raw_plan.strip()
        else:
            plan_name = _first_present(
                plan_obj.get("name"),
                plan_obj.get("planName"),
                plan_obj.get("tier"),
                data_obj.get("plan_name"),
                data_obj.get("planName"),
            )

        remaining_f = float(remaining) if remaining is not None else None
        used_f = float(used) if used is not None else None

        await log_api_credit(
            session,
            workspace_id=workspace_id,
            provider=self.provider_name,
            endpoint="/tw-v2/account/usage",
            units_consumed=0.0,
            estimated_usd=0.0,
            reconciled_balance_units=remaining_f,
        )

        return ProviderUsageInfo(
            provider=self.provider_name,
            remaining_units=remaining_f,
            used_units=used_f,
            plan_name=str(plan_name) if plan_name else None,
            raw=payload,
        )
