"""In-process event bus.

Everything that changes state publishes here; the WebSocket layer is just
one more subscriber. That keeps domain code free of any knowledge of
transports - a guide loop emits `guiding.sample` without caring whether a
browser, a log file or nothing at all is listening.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class Topic(StrEnum):
    """Every event topic in the system.

    An enum rather than free-form strings so that a typo in a subscriber
    fails at import time instead of silently never matching.
    """

    DEVICE_STATE = "device.state"
    MOUNT_POSITION = "mount.position"
    CAMERA_STATE = "camera.state"
    CAMERA_FRAME = "camera.frame"
    GUIDING_SAMPLE = "guiding.sample"
    GUIDING_STATE = "guiding.state"
    SOLVE_RESULT = "solve.result"
    POLAR_ALIGN = "polar.align"
    TASK_UPDATE = "task.update"
    LOG = "log"


@dataclass(frozen=True, slots=True)
class Event:
    topic: Topic
    payload: dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def as_json(self) -> dict[str, Any]:
        return {"topic": str(self.topic), "timestamp": self.timestamp, "payload": self.payload}


class EventBus:
    """Fan-out to any number of subscribers, each with its own queue.

    Subscriber queues are bounded and drop their oldest entry when full.
    A browser on a slow phone connection must never be able to stall the
    guide loop by failing to drain its socket, and for telemetry the newest
    sample is the one that matters - stale ones are not worth blocking for.
    """

    def __init__(self, *, history: int = 200, queue_size: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._history: deque[Event] = deque(maxlen=history)
        self._queue_size = queue_size

    def publish(self, topic: Topic, **payload: Any) -> Event:
        """Publish an event. Safe to call from synchronous code."""
        event = Event(topic=topic, payload=payload)
        self._history.append(event)
        for queue in self._subscribers:
            if queue.full():
                # Bounded queue: shed the oldest rather than block a producer.
                with contextlib.suppress(asyncio.QueueEmpty):  # racing a drain
                    queue.get_nowait()
            queue.put_nowait(event)
        return event

    def recent(self, topics: Iterable[Topic] | None = None) -> list[Event]:
        """Replay buffered events so a new client can render immediately.

        Without this a freshly opened dashboard shows empty panels until the
        next event happens to fire, which for an idle rig could be a while.
        """
        if topics is None:
            return list(self._history)
        wanted = set(topics)
        return [event for event in self._history if event.topic in wanted]

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[Event]]:
        """Yield an async iterator of events for the caller's lifetime."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        try:
            yield self._drain(queue)
        finally:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def _drain(self, queue: asyncio.Queue[Event]) -> AsyncIterator[Event]:
        while True:
            yield await queue.get()
