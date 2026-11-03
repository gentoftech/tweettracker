"""Async background scheduler worker entrypoint (`python -m tweettracker.worker.main`)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import signal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tweettracker.algo_sync import check_upstream_algo_sync
from tweettracker.backtest import grade_workspace_outcomes, run_auto_calibration
from tweettracker.config import get_settings
from tweettracker.crypto import refresh_runtime_secrets
from tweettracker.db import configure_database, dispose_database, get_workspace_lock
from tweettracker.graph import recompute_workspace_graph
from tweettracker.models import Post, Workspace
from tweettracker.radar import poll_workspace_radar
from tweettracker.rising import auto_sync_workspace_network
from tweettracker.seed import bootstrap_database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [tweettracker.worker] %(message)s",
)
logger = logging.getLogger("tweettracker.worker")

HOURLY_INTERVAL_SECS = 3600.0
DISCOVERY_SYNC_INTERVAL_SECS = 21600.0
DAILY_ALGO_SYNC_INTERVAL_SECS = 86400.0
AUTO_CALIBRATE_MIN_MATURED_POSTS = 30


@dataclass
class WorkerScheduleState:
    """Tracks per-workspace & global job execution timestamps across worker ticks."""

    last_radar_poll_at: dict[int, datetime] = field(default_factory=dict)
    last_hourly_jobs_at: dict[int, datetime] = field(default_factory=dict)
    last_discovery_sync_at: dict[int, datetime] = field(default_factory=dict)
    last_algo_sync_at: datetime | None = None


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def run_worker_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    state: WorkerScheduleState | None = None,
    run_upstream_sync: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Executes one paced scheduler pass across all configured workspaces."""
    sched = state if state is not None else WorkerScheduleState()
    now_utc = _ensure_utc(now) or datetime.now(timezone.utc)

    polled_workspaces = 0
    hourly_refreshed = 0
    discovery_synced = 0
    upstream_status: str | None = None

    async with session_factory() as session:
        await refresh_runtime_secrets(session)
        workspaces = list(
            (await session.execute(select(Workspace).order_by(Workspace.id.asc())))
            .scalars()
            .all()
        )

    for ws_stub in workspaces:
        ws_id = ws_stub.id
        poll_interval_secs = max(60.0, float(ws_stub.radar_poll_minutes) * 60.0)
        last_poll = sched.last_radar_poll_at.get(ws_id)
        due_poll = (
            last_poll is None
            or (now_utc - last_poll).total_seconds() >= poll_interval_secs
        )

        if due_poll:
            try:
                async with get_workspace_lock(ws_id):
                    async with session_factory() as session:
                        ws = await session.get(Workspace, ws_id)
                        if ws is not None:
                            summary = await poll_workspace_radar(session, ws, now=now_utc)
                            await session.commit()
                            sched.last_radar_poll_at[ws_id] = now_utc
                            polled_workspaces += 1
                            logger.info(
                                "Polled radar for workspace=%s (%s): active=%d, watch=%d, surge=%d, new_alerts=%d",
                                ws.slug,
                                ws.x_data_provider,
                                summary.active_posts_count,
                                summary.watch_count,
                                summary.surge_count,
                                summary.new_alerts_count,
                            )
            except Exception:
                logger.exception("Radar poll failed for workspace_id=%d", ws_id)

        last_hourly = sched.last_hourly_jobs_at.get(ws_id)
        due_hourly = (
            last_hourly is None
            or (now_utc - last_hourly).total_seconds() >= HOURLY_INTERVAL_SECS
        )
        if due_hourly:
            try:
                async with get_workspace_lock(ws_id):
                    async with session_factory() as session:
                        await grade_workspace_outcomes(session, ws_id, now=now_utc)

                        ws = await session.get(Workspace, ws_id)
                        mem_last_disc = _ensure_utc(sched.last_discovery_sync_at.get(ws_id))
                        db_last_disc = (
                            _ensure_utc(ws.last_discovery_sync_at) if ws is not None else None
                        )
                        if mem_last_disc is not None and db_last_disc is not None:
                            last_disc: datetime | None = max(mem_last_disc, db_last_disc)
                        else:
                            last_disc = mem_last_disc or db_last_disc

                        due_discovery = (
                            last_disc is None
                            or (now_utc - last_disc).total_seconds() >= DISCOVERY_SYNC_INTERVAL_SECS
                        )
                        if due_discovery:
                            await auto_sync_workspace_network(session, ws_id, now=now_utc)
                            sched.last_discovery_sync_at[ws_id] = now_utc
                            discovery_synced += 1
                        else:
                            await recompute_workspace_graph(
                                session, ws_id, commit=False, reference_time=now_utc
                            )

                        matured_count = int(
                            (
                                await session.execute(
                                    select(func.count(Post.id)).where(
                                        Post.workspace_id == ws_id,
                                        Post.matured_48h.is_(True),
                                    )
                                )
                            ).scalar_one()
                            or 0
                        )
                        if matured_count >= AUTO_CALIBRATE_MIN_MATURED_POSTS:
                            await run_auto_calibration(
                                session,
                                ws_id,
                                min_matured_posts=AUTO_CALIBRATE_MIN_MATURED_POSTS,
                            )

                        await session.commit()
                        sched.last_hourly_jobs_at[ws_id] = now_utc
                        hourly_refreshed += 1
                        logger.info(
                            "Completed 48h grading, graph recompute, and <50k discovery for workspace_id=%d",
                            ws_id,
                        )
            except Exception:
                logger.exception("Hourly maintenance failed for workspace_id=%d", ws_id)

    due_sync = run_upstream_sync and (
        sched.last_algo_sync_at is None
        or (now_utc - sched.last_algo_sync_at).total_seconds()
        >= DAILY_ALGO_SYNC_INTERVAL_SECS
    )
    if due_sync:
        try:
            async with session_factory() as session:
                first_ws = (
                    await session.execute(select(Workspace).order_by(Workspace.id.asc()).limit(1))
                ).scalar_one_or_none()
                outcome = await check_upstream_algo_sync(
                    session,
                    allow_gh_cli_fallback=True,
                    llm_provider_name=(first_ws.llm_provider if first_ws else "google_gemini"),
                    workspace=first_ws,
                )
                sched.last_algo_sync_at = now_utc
                upstream_status = outcome.event.status
                logger.info(
                    "Upstream xai-org/x-algorithm sync check: status=%s head=%s",
                    outcome.event.status,
                    outcome.event.head_sha[:7],
                )
        except Exception:
            logger.exception("Daily upstream x-algorithm sync check failed")

    return {
        "workspaces_total": len(workspaces),
        "polled_workspaces": polled_workspaces,
        "hourly_refreshed": hourly_refreshed,
        "discovery_synced_workspaces": discovery_synced,
        "upstream_status": upstream_status,
    }


async def main() -> None:
    settings = get_settings()
    engine, session_factory = configure_database(settings.database_url)
    await bootstrap_database(
        engine,
        session_factory,
        auto_migrate=settings.auto_migrate,
        seed_fixtures=settings.seed_fixtures,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    state = WorkerScheduleState()
    logger.info(
        "TweetTracker worker started (AUTO_MIGRATE=%s, SEED_FIXTURES=%s).",
        settings.auto_migrate,
        settings.seed_fixtures,
    )
    try:
        while not stop_event.is_set():
            await run_worker_cycle(
                session_factory,
                state=state,
                run_upstream_sync=True,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except TimeoutError:
                continue
    finally:
        await dispose_database()
        logger.info("TweetTracker worker stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
