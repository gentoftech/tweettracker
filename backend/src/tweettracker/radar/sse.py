"""Real-Time Async Pub/Sub Server-Sent Events (SSE) Broadcaster per Workspace."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any
import uuid

MAX_SUBSCRIBERS_PER_WORKSPACE = 64


@dataclass(slots=True, frozen=True)
class SSEMessage:
    """Structured SSE frame fanned out to workspace subscribers."""

    event_id: str
    workspace_id: int
    event_type: str
    data: dict[str, Any]
    created_at: str

    def encode_sse(self) -> str:
        payload = json.dumps(self.data, separators=(",", ":"), default=str)
        return f"id: {self.event_id}\nevent: {self.event_type}\ndata: {payload}\n\n"


class RadarSSEHub:
    """In-process per-workspace `asyncio.Queue` fan-out hub supporting subscribe/unsubscribe,
    heartbeats, and structured JSON SSE events (`breakout.watch`, `breakout.surge`,
    `radar.polled`, `webhook.tested`, `algo.upstream_drift_detected`).
    """

    def __init__(
        self,
        *,
        history_limit: int = 30,
        max_subscribers_per_workspace: int = MAX_SUBSCRIBERS_PER_WORKSPACE,
    ) -> None:
        self._subscribers: dict[int, set[asyncio.Queue[SSEMessage]]] = {}
        self._recent_events: dict[int, deque[SSEMessage]] = {}
        self._history_limit = max(1, history_limit)
        self._max_subscribers = max(1, int(max_subscribers_per_workspace))

    def subscribe(
        self,
        workspace_id: int,
        *,
        max_queue_size: int = 200,
    ) -> asyncio.Queue[SSEMessage]:
        ws_id = int(workspace_id)
        bucket = self._subscribers.setdefault(ws_id, set())
        if len(bucket) >= self._max_subscribers:
            raise OverflowError(
                f"Workspace {ws_id} reached maximum SSE subscribers ({self._max_subscribers})."
            )
        queue: asyncio.Queue[SSEMessage] = asyncio.Queue(maxsize=max(8, max_queue_size))
        bucket.add(queue)
        return queue

    def unsubscribe(
        self,
        workspace_id: int,
        queue: asyncio.Queue[SSEMessage],
    ) -> None:
        ws_id = int(workspace_id)
        bucket = self._subscribers.get(ws_id)
        if bucket is not None:
            bucket.discard(queue)
            if not bucket:
                self._subscribers.pop(ws_id, None)

    def subscriber_count(self, workspace_id: int | None = None) -> int:
        if workspace_id is not None:
            return len(self._subscribers.get(int(workspace_id), set()))
        return sum(len(qs) for qs in self._subscribers.values())

    def recent_events(self, workspace_id: int) -> list[SSEMessage]:
        return list(self._recent_events.get(int(workspace_id), ()))

    def publish(
        self,
        workspace_id: int,
        event_type: str,
        data: dict[str, Any],
        *,
        event_id: str | None = None,
    ) -> SSEMessage:
        ws_id = int(workspace_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        enriched = {
            "workspace_id": ws_id,
            "event_type": event_type,
            "timestamp": now_iso,
            **data,
        }
        msg = SSEMessage(
            event_id=event_id or uuid.uuid4().hex[:16],
            workspace_id=ws_id,
            event_type=event_type,
            data=enriched,
            created_at=now_iso,
        )

        hist = self._recent_events.setdefault(ws_id, deque(maxlen=self._history_limit))
        hist.append(msg)

        for queue in list(self._subscribers.get(ws_id, ())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass

        return msg

    async def stream_events(
        self,
        workspace_id: int,
        *,
        heartbeat_interval_seconds: float = 15.0,
        max_events: int | None = None,
        stop_event: asyncio.Event | None = None,
        request: Any = None,
        pre_subscribed_queue: asyncio.Queue[SSEMessage] | None = None,
    ) -> AsyncIterator[str]:
        """Yields SSE wire frames starting with an immediate `connected` handshake frame,
        followed by live published events and periodic `heartbeat` frames.
        """
        ws_id = int(workspace_id)
        queue = (
            pre_subscribed_queue
            if pre_subscribed_queue is not None
            else self.subscribe(ws_id)
        )
        emitted = 0
        try:
            connected_msg = SSEMessage(
                event_id=uuid.uuid4().hex[:16],
                workspace_id=ws_id,
                event_type="connected",
                data={
                    "workspace_id": ws_id,
                    "event_type": "connected",
                    "status": "live",
                    "subscribers": self.subscriber_count(ws_id),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            yield connected_msg.encode_sse()
            emitted += 1
            if max_events is not None and emitted >= max_events:
                return

            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                if request is not None and await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(
                        queue.get(),
                        timeout=max(0.05, float(heartbeat_interval_seconds)),
                    )
                    yield msg.encode_sse()
                    emitted += 1
                except TimeoutError:
                    if request is not None and await request.is_disconnected():
                        break
                    now_iso = datetime.now(timezone.utc).isoformat()
                    hb = SSEMessage(
                        event_id=uuid.uuid4().hex[:16],
                        workspace_id=ws_id,
                        event_type="heartbeat",
                        data={
                            "workspace_id": ws_id,
                            "event_type": "heartbeat",
                            "timestamp": now_iso,
                        },
                        created_at=now_iso,
                    )
                    yield ": keep-alive\n" + hb.encode_sse()
                    emitted += 1

                if max_events is not None and emitted >= max_events:
                    break
        finally:
            self.unsubscribe(ws_id, queue)


radar_sse_hub = RadarSSEHub()
