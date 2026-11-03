"""Portable async SQLAlchemy 2.0 engine and session management (PostgreSQL & SQLite)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tweettracker.config import get_settings
from tweettracker.crypto import clear_runtime_secrets


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    if database_url.startswith("sqlite") and ":memory:" not in database_url:
        prefix_split = database_url.split(":///", 1)
        if len(prefix_split) == 2:
            raw_path = prefix_split[1].split("?", 1)[0]
            if raw_path:
                Path(raw_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def build_async_engine(database_url: str | None = None) -> AsyncEngine:
    url = database_url or get_settings().database_url
    _ensure_sqlite_parent_dir(url)

    engine_kwargs: dict[str, Any] = {"future": True}
    if url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 5.0}
    else:
        engine_kwargs["pool_pre_ping"] = True

    engine = create_async_engine(url, **engine_kwargs)

    if url.startswith("sqlite"):
        is_file_sqlite = ":memory:" not in url

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            if is_file_sqlite:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_workspace_locks: dict[int, asyncio.Lock] = {}


def get_workspace_lock(workspace_id: int) -> asyncio.Lock:
    """Returns an in-process per-workspace `asyncio.Lock` serializing concurrent
    radar polls, auto-calibrations, and version activations within a workspace.
    """
    ws_id = int(workspace_id)
    lock = _workspace_locks.get(ws_id)
    if lock is None:
        lock = asyncio.Lock()
        _workspace_locks[ws_id] = lock
    return lock


def configure_database(database_url: str | None = None) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    global _engine, _session_factory
    _workspace_locks.clear()
    _engine = build_async_engine(database_url)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine, _session_factory


def get_engine() -> AsyncEngine:
    if _engine is None:
        configure_database()
    assert _engine is not None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        configure_database()
    assert _session_factory is not None
    return _session_factory


async def dispose_database() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    _workspace_locks.clear()
    clear_runtime_secrets()


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session
