"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from tweettracker.providers import tweetapi


@pytest.fixture(autouse=True)
def _fast_tweetapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disables real-time TweetAPI request pacing and resets the handle -> user id cache."""
    monkeypatch.setenv("TWEETAPI_MIN_INTERVAL_SECS", "0")
    tweetapi._USER_ID_CACHE.clear()
