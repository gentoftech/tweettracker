"""Offline deterministic FixtureProvider ($0 cost) and XDataProvider router."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.models import AccountCommunityMembership, Community, Post, TrackedAccount, Workspace
from tweettracker.providers.base import (
    PostRecord,
    ProfileRecord,
    ProviderNotConfiguredError,
    ProviderUsageInfo,
    XDataProvider,
)
from tweettracker.providers.spend import log_api_credit
from tweettracker.providers.tweetapi import TweetAPIProvider


def _snowflake_gt(post_id: str, since_id: str) -> bool:
    a = post_id.strip()
    b = since_id.strip()
    if a.isdigit() and b.isdigit():
        return int(a) > int(b)
    return a > b


class FixtureProvider:
    """Zero-key local dev & test provider backed by deterministic workspace fixtures."""

    provider_name = "fixture"

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
        stmt = (
            select(Post, TrackedAccount)
            .join(TrackedAccount, Post.author_account_id == TrackedAccount.id)
            .where(Post.workspace_id == workspace.id)
            .order_by(Post.created_at_x.desc())
            .limit(40)
        )
        if handles:
            cleaned = {h.strip().lstrip("@").lower() for h in handles if h.strip()}
            stmt = stmt.where(TrackedAccount.handle.in_(cleaned))
        elif list_id and list_id.strip():
            comm_ids = list(
                (
                    await session.execute(
                        select(Community.id).where(
                            Community.workspace_id == workspace.id,
                            Community.x_list_id == list_id.strip(),
                        )
                    )
                ).scalars().all()
            )
            if comm_ids:
                stmt = stmt.join(
                    AccountCommunityMembership,
                    AccountCommunityMembership.account_id == TrackedAccount.id,
                ).where(
                    AccountCommunityMembership.community_id.in_(comm_ids),
                    or_(
                        AccountCommunityMembership.is_manual_tag.is_(True),
                        AccountCommunityMembership.nmf_interested_in >= 0.25,
                        AccountCommunityMembership.nmf_known_for >= 0.25,
                    ),
                )
        if exclude_replies:
            stmt = stmt.where(Post.is_reply.is_(False))

        rows = (await session.execute(stmt)).all()
        await log_api_credit(
            session,
            workspace_id=workspace.id,
            provider=self.provider_name,
            endpoint="fixture://search_recent_posts",
            units_consumed=0.0,
            estimated_usd=0.0,
        )

        # Synthesize deterministic list posts/voices if syncing an arbitrary X List ID in fixture mode
        if list_id and list_id.strip() and not rows:
            lid = list_id.strip()
            now = datetime.now(timezone.utc)
            synth: list[PostRecord] = []
            for idx in range(1, 4):
                seed_key = f"{lid}:{idx}".encode("utf-8")
                digest = int.from_bytes(hashlib.blake2b(seed_key, digest_size=4).digest(), "little")
                short_lid = "".join(ch for ch in lid if ch.isalnum())[-4:] or "list"
                handle = f"list_{short_lid.lower()}_voice_{idx}"
                views = 4_500 + (digest % 22_000)
                synth.append(
                    PostRecord(
                        x_post_id=str(1999100000 + (digest % 8_000_000)),
                        author_x_user_id=str(910_000_000 + (digest % 80_000_000)),
                        author_handle=handle,
                        author_followers=6_000 + (digest % 38_000),
                        text=f"Operational deep-dive from X List {lid} member @{handle} on execution velocity.",
                        created_at_x=now - timedelta(minutes=18 + idx * 7),
                        views=views,
                        likes=int(views * 0.022),
                        replies=int(views * 0.004),
                        quotes=int(views * 0.002),
                        reposts=int(views * 0.003),
                        bookmarks=int(views * 0.007),
                    )
                )
            return synth
        records: list[PostRecord] = []
        for post, acct in rows:
            if since_id and not _snowflake_gt(post.x_post_id, since_id):
                continue
            records.append(
                PostRecord(
                    x_post_id=post.x_post_id,
                    author_x_user_id=acct.x_user_id,
                    author_handle=acct.handle,
                    author_followers=acct.followers_count,
                    text=post.text,
                    created_at_x=post.created_at_x,
                    is_reply=post.is_reply,
                    is_retweet=post.is_retweet,
                    is_quote=post.is_quote,
                    in_reply_to_post_id=post.in_reply_to_post_id,
                    conversation_id=post.conversation_id,
                    quoted_post_id=post.quoted_post_id,
                    has_url=post.has_url,
                    has_media=post.has_media,
                    media_type=post.media_type,
                    video_duration_ms=post.video_duration_ms,
                    ancestor_author_ids=list(post.ancestor_author_ids_json or []),
                    views=post.latest_views,
                    likes=post.latest_likes,
                    replies=post.latest_replies,
                    quotes=post.latest_quotes,
                    reposts=post.latest_reposts,
                    bookmarks=post.latest_bookmarks,
                )
            )
        return records

    async def get_posts_by_ids(
        self,
        session: AsyncSession,
        workspace: Workspace,
        post_ids: list[str],
    ) -> list[PostRecord]:
        if not post_ids:
            return []
        stmt = (
            select(Post, TrackedAccount)
            .join(TrackedAccount, Post.author_account_id == TrackedAccount.id)
            .where(Post.workspace_id == workspace.id, Post.x_post_id.in_(post_ids))
        )
        rows = (await session.execute(stmt)).all()
        return [
            PostRecord(
                x_post_id=post.x_post_id,
                author_x_user_id=acct.x_user_id,
                author_handle=acct.handle,
                author_followers=acct.followers_count,
                text=post.text,
                created_at_x=post.created_at_x,
                is_reply=post.is_reply,
                is_retweet=post.is_retweet,
                is_quote=post.is_quote,
                views=post.latest_views,
                likes=post.latest_likes,
                replies=post.latest_replies,
                quotes=post.latest_quotes,
                reposts=post.latest_reposts,
                bookmarks=post.latest_bookmarks,
            )
            for post, acct in rows
        ]

    async def get_profiles_by_handles(
        self,
        session: AsyncSession,
        workspace: Workspace,
        handles: list[str],
    ) -> list[ProfileRecord]:
        cleaned = [h.strip().lstrip("@") for h in handles if h.strip()]
        if not cleaned:
            return []
        lower_map = {h.lower(): h for h in cleaned}
        stmt = select(TrackedAccount).where(
            TrackedAccount.workspace_id == workspace.id,
            TrackedAccount.handle.in_(list(lower_map.keys())),
        )
        existing = {a.handle.lower(): a for a in (await session.execute(stmt)).scalars().all()}

        out: list[ProfileRecord] = []
        for key, orig_h in lower_map.items():
            if key in existing:
                acct = existing[key]
                out.append(
                    ProfileRecord(
                        x_user_id=acct.x_user_id,
                        handle=acct.handle,
                        display_name=acct.display_name,
                        bio=acct.bio,
                        avatar_url=acct.avatar_url,
                        followers_count=acct.followers_count,
                        following_count=acct.following_count,
                    )
                )
            else:
                digest = int.from_bytes(hashlib.blake2b(key.encode("utf-8"), digest_size=4).digest(), "little")
                out.append(
                    ProfileRecord(
                        x_user_id=str(900_000_000 + (digest % 90_000_000)),
                        handle=orig_h.lower(),
                        display_name=orig_h.replace("_", " ").title(),
                        bio=f"Tracked ecosystem voice @{orig_h.lower()} (offline fixture hydration).",
                        followers_count=2_500 + (digest % 45_000),
                        following_count=300 + (digest % 1_200),
                    )
                )
        return out

    async def get_following_list(
        self,
        session: AsyncSession,
        workspace: Workspace,
        username_or_id: str,
        *,
        max_pages: int = 5,
        count: int = 200,
    ) -> list[ProfileRecord]:
        stmt = (
            select(TrackedAccount)
            .where(TrackedAccount.workspace_id == workspace.id)
            .limit(min(count, 50))
        )
        accts = (await session.execute(stmt)).scalars().all()
        return [
            ProfileRecord(
                x_user_id=a.x_user_id,
                handle=a.handle,
                display_name=a.display_name,
                bio=a.bio,
                avatar_url=a.avatar_url,
                followers_count=a.followers_count,
                following_count=a.following_count,
            )
            for a in accts
        ]

    async def get_user_tweets_and_replies(
        self,
        session: AsyncSession,
        workspace: Workspace,
        username_or_id: str,
        *,
        count: int = 40,
    ) -> list[PostRecord]:
        _ = (username_or_id, count)
        await log_api_credit(
            session,
            workspace_id=workspace.id,
            provider=self.provider_name,
            endpoint="fixture://get_user_tweets_and_replies",
            units_consumed=0.0,
            estimated_usd=0.0,
        )
        return []

    async def get_account_usage(
        self,
        session: AsyncSession,
        workspace_id: int | None = None,
    ) -> ProviderUsageInfo:
        return ProviderUsageInfo(
            provider="fixture",
            remaining_units=None,
            used_units=0.0,
            plan_name="Offline Fixture Mode ($0)",
            raw={"mode": "fixture"},
        )


class StubXDataProvider:
    """Typed Build-2 failover slot for socialdata, twitterapi_io, and official_x."""

    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name

    def _raise(self) -> None:
        raise ProviderNotConfiguredError(
            f"XDataProvider '{self.provider_name}' is a reserved failover slot in Build 1; "
            f"select 'fixture' or 'tweetapi_com' (per docs/decisions/0003)."
        )

    async def search_recent_posts(self, *args: object, **kwargs: object) -> list[PostRecord]:
        self._raise()
        return []

    async def get_posts_by_ids(self, *args: object, **kwargs: object) -> list[PostRecord]:
        self._raise()
        return []

    async def get_profiles_by_handles(self, *args: object, **kwargs: object) -> list[ProfileRecord]:
        self._raise()
        return []

    async def get_following_list(self, *args: object, **kwargs: object) -> list[ProfileRecord]:
        self._raise()
        return []

    async def get_user_tweets_and_replies(
        self, *args: object, **kwargs: object
    ) -> list[PostRecord]:
        self._raise()
        return []

    async def get_account_usage(self, *args: object, **kwargs: object) -> ProviderUsageInfo:
        self._raise()
        return ProviderUsageInfo(provider=self.provider_name)


def get_x_data_provider(provider_name: str) -> XDataProvider:
    normalized = provider_name.strip().lower()
    if normalized == "fixture":
        return FixtureProvider()
    if normalized == "tweetapi_com":
        return TweetAPIProvider()
    if normalized in ("socialdata", "twitterapi_io", "official_x"):
        return StubXDataProvider(normalized)
    raise ProviderNotConfiguredError(f"Unknown XDataProvider '{provider_name}'.")
