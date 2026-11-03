"""Provider-agnostic XDataProvider interfaces, records, and error types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.models import Workspace

VALID_X_DATA_PROVIDERS = (
    "fixture",
    "tweetapi_com",
    "socialdata",
    "twitterapi_io",
    "official_x",
)

VALID_LLM_PROVIDERS = (
    "google_gemini",
    "xai_grok",
    "openai",
    "anthropic",
    "deepinfra",
)


class SpendCapExceededError(RuntimeError):
    """Raised before making a paid external API call when daily/monthly USD caps are reached."""


class ProviderNotConfiguredError(RuntimeError):
    """Raised when an unconfigured or Build-2 stubbed provider slot is invoked."""


@dataclass(slots=True)
class ProfileRecord:
    x_user_id: str
    handle: str
    display_name: str = ""
    bio: str = ""
    avatar_url: str | None = None
    followers_count: int = 0
    following_count: int = 0


@dataclass(slots=True)
class PostRecord:
    x_post_id: str
    author_x_user_id: str
    author_handle: str
    text: str
    created_at_x: datetime
    author_followers: int = 0
    is_reply: bool = False
    is_retweet: bool = False
    is_quote: bool = False
    in_reply_to_post_id: str | None = None
    conversation_id: str | None = None
    quoted_post_id: str | None = None
    target_author_handle: str | None = None
    has_url: bool = False
    has_media: bool = False
    media_type: str | None = None
    video_duration_ms: int | None = None
    ancestor_author_ids: list[str] = field(default_factory=list)
    views: int = 0
    likes: int = 0
    replies: int = 0
    quotes: int = 0
    reposts: int = 0
    bookmarks: int = 0


@dataclass(slots=True)
class InteractionRecord:
    post_x_id: str
    actor_x_user_id: str
    actor_handle: str
    actor_followers: int
    interaction_type: str
    occurred_at: datetime
    source_x_post_id: str | None = None


@dataclass(slots=True)
class ProviderUsageInfo:
    provider: str
    remaining_units: float | None = None
    used_units: float | None = None
    plan_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class XDataProvider(Protocol):
    """Unified async interface across FixtureProvider, TweetAPI.com, and failover slots."""

    provider_name: str

    async def search_recent_posts(
        self,
        session: AsyncSession,
        workspace: Workspace,
        *,
        handles: list[str] | None = None,
        list_id: str | None = None,
        since_id: str | None = None,
        exclude_replies: bool = True,
    ) -> list[PostRecord]: ...

    async def get_posts_by_ids(
        self,
        session: AsyncSession,
        workspace: Workspace,
        post_ids: list[str],
    ) -> list[PostRecord]: ...

    async def get_profiles_by_handles(
        self,
        session: AsyncSession,
        workspace: Workspace,
        handles: list[str],
    ) -> list[ProfileRecord]: ...

    async def get_following_list(
        self,
        session: AsyncSession,
        workspace: Workspace,
        username_or_id: str,
        *,
        max_pages: int = 5,
        count: int = 200,
    ) -> list[ProfileRecord]: ...

    async def get_user_tweets_and_replies(
        self,
        session: AsyncSession,
        workspace: Workspace,
        username_or_id: str,
        *,
        count: int = 40,
    ) -> list[PostRecord]: ...

    async def get_account_usage(
        self,
        session: AsyncSession,
        workspace_id: int | None = None,
    ) -> ProviderUsageInfo: ...
