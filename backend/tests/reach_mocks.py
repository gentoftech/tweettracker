"""TweetAPI mocks for reply-reach measurement in live-provider tests.

`timeline_handler` answers `/user/tweets-and-replies`: the owner's own timeline (empty by default)
or, for anyone else, one original post old enough to test (48h) with a few replies.
`details_handler` answers `/tweet/details` with early replies under that post from accounts in the
default follower band, so the candidate ends up with a measured reply reach.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

PARENT_PREFIX = "reach_parent_"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def reach_handlers(
    *,
    now: datetime | Callable[[], datetime],
    owner_user_ids: set[str] | frozenset[str] = frozenset({"999"}),
    owner_payload: dict[str, Any] | None = None,
    views_by_user_id: dict[str, int] | None = None,
    default_views: int = 2_400,
    replies_per_post: int = 6,
    replier_followers: int = 8_000,
) -> tuple[Callable[[httpx.Request], httpx.Response], Callable[[httpx.Request], httpx.Response]]:
    views_by_user_id = dict(views_by_user_id or {})
    parents: dict[str, tuple[str, datetime]] = {}

    def _now() -> datetime:
        return now() if callable(now) else now

    def timeline_handler(request: httpx.Request) -> httpx.Response:
        uid = request.url.params.get("userId") or ""
        if uid in owner_user_ids:
            return httpx.Response(200, json=owner_payload or {"data": []})
        created = _now() - timedelta(hours=48)
        pid = f"{PARENT_PREFIX}{uid}"
        parents[pid] = (uid, created)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": pid,
                        "text": "An original post to measure replies under",
                        "createdAt": _iso(created),
                        "author": {"id": uid, "username": f"user_{uid}", "followerCount": 50_000},
                        "viewCount": 40_000,
                        "replyCount": replies_per_post + 2,
                    }
                ]
            },
        )

    def details_handler(request: httpx.Request) -> httpx.Response:
        tid = request.url.params.get("tweetId") or ""
        if tid not in parents:
            return httpx.Response(200, json={"data": {"replies": []}})
        uid, created = parents[tid]
        views = views_by_user_id.get(uid, default_views)
        replies = [
            {
                "id": f"{tid}_r{i}",
                "text": "early reply",
                "createdAt": _iso(created + timedelta(minutes=1 + i * 5)),
                "author": {
                    "id": f"replier_{uid}_{i}",
                    "username": f"replier_{uid}_{i}",
                    "name": f"Replier {i}",
                    "followerCount": replier_followers,
                    "isBlueVerified": True,
                },
                "viewCount": views,
                "likeCount": 3,
            }
            for i in reversed(range(replies_per_post))
        ]
        return httpx.Response(200, json={"data": {"replies": replies}})

    return timeline_handler, details_handler
