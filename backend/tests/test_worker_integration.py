"""End-to-end integration test for the unified TweetTracker async worker loop."""

from __future__ import annotations

from pathlib import Path
import tempfile

import pytest

from tweettracker.db import configure_database, dispose_database
from tweettracker.seed import bootstrap_database
from tweettracker.worker.main import WorkerScheduleState, run_worker_cycle


@pytest.mark.asyncio
async def test_worker_cycle_runs_all_epic_pipelines_cleanly() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "worker_test.db"
        engine, session_factory = configure_database(f"sqlite+aiosqlite:///{db_file.as_posix()}")
        try:
            await bootstrap_database(
                engine,
                session_factory,
                auto_migrate=True,
                seed_fixtures=True,
            )
            state = WorkerScheduleState()

            # First tick runs radar poll + hourly jobs across both seeded workspaces (WS1 & WS2)
            first = await run_worker_cycle(
                session_factory,
                state=state,
                run_upstream_sync=False,
            )
            assert first["workspaces_total"] == 2
            assert first["polled_workspaces"] == 2
            assert first["hourly_refreshed"] == 2

            # Immediate second tick respects per-workspace poll & hourly cooldowns
            second = await run_worker_cycle(
                session_factory,
                state=state,
                run_upstream_sync=False,
            )
            assert second["workspaces_total"] == 2
            assert second["polled_workspaces"] == 0
            assert second["hourly_refreshed"] == 0
        finally:
            await dispose_database()
