"""Long-running operations as observable, cancellable tasks.

Centring a target, aligning on the pole, running autofocus and shooting a
sequence all take from seconds to hours. None of them can sit inside an HTTP
request, and all of them need to report progress and stop when asked. This
gives them one shape, so the API exposes them uniformly and the UI renders
any of them with the same component.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from astropi.core.errors import TaskCancelledError
from astropi.core.events import EventBus, Topic

logger = logging.getLogger(__name__)


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED)


@dataclass(slots=True)
class TaskProgress:
    """A snapshot the UI can render without knowing the task's type."""

    state: TaskState = TaskState.PENDING
    step: str = ""
    fraction: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)


class Task(ABC):
    """Base for every long-running operation.

    Subclasses implement `run` and call `report` as they go. Cancellation
    arrives as `asyncio.CancelledError` at the next await point, so
    subclasses get it for free as long as they do not swallow it.
    """

    #: Human-readable kind, used by the UI to choose a renderer.
    kind: str = "task"

    def __init__(self, *, name: str | None = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.name = name or self.kind
        self.progress = TaskProgress()
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.error: str | None = None
        self.result: Any = None
        self._events: EventBus | None = None
        self._forward: Callable[[str, str | None], None] | None = None

    @abstractmethod
    async def run(self) -> Any: ...

    def bind(self, events: EventBus) -> None:
        self._events = events

    def report_into(self, forward: Callable[[str, str | None], None]) -> None:
        """Send this task's progress to another task instead of the bus.

        A session plan runs centring and capture as ordinary tasks, but the
        operator is watching one job, not four. Without this each of them
        would publish under its own id and the dashboard would show a task
        appearing and vanishing every few minutes.
        """
        self._forward = forward

    def report(
        self,
        step: str,
        *,
        fraction: float | None = None,
        message: str | None = None,
        **detail: Any,
    ) -> None:
        """Record progress and push it to subscribers."""
        self.progress.step = step
        if fraction is not None:
            self.progress.fraction = max(0.0, min(1.0, fraction))
        if detail:
            self.progress.detail.update(detail)
        if message:
            self.progress.messages.append(message)
            logger.info("[%s] %s", self.name, message)
        if self._forward is not None:
            self._forward(step, message)
            return
        self._publish()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "state": str(self.progress.state),
            "step": self.progress.step,
            "fraction": self.progress.fraction,
            "detail": self.progress.detail,
            "messages": self.progress.messages[-20:],
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def _publish(self) -> None:
        if self._forward is not None:
            return
        if self._events is not None:
            self._events.publish(Topic.TASK_UPDATE, **self.as_dict())

    def _set_state(self, state: TaskState) -> None:
        self.progress.state = state
        self._publish()


class TaskEngine:
    """Runs tasks, keeps their history, and enforces one at a time.

    Serialised deliberately: every task here drives the same mount and the
    same camera, so two at once would fight over the hardware. Anything that
    genuinely needs to overlap - guiding during an imaging run - is a
    service with its own loop rather than a second task.
    """

    def __init__(self, events: EventBus, *, history: int = 50) -> None:
        self._events = events
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._current: Task | None = None
        self._runner: asyncio.Task[Any] | None = None
        self._history = history

    @property
    def current(self) -> Task | None:
        return self._current

    @property
    def busy(self) -> bool:
        return self._runner is not None and not self._runner.done()

    def submit(self, task: Task) -> Task:
        """Start a task, refusing if one is already running."""
        if self.busy:
            running = self._current.name if self._current else "another task"
            raise RuntimeError(f"{running} is already running; cancel it first")

        task.bind(self._events)
        self._tasks[task.id] = task
        self._order.append(task.id)
        self._trim()

        self._current = task
        self._runner = asyncio.create_task(self._run(task))
        return task

    async def _run(self, task: Task) -> None:
        task.started_at = time.time()
        task._set_state(TaskState.RUNNING)
        try:
            task.result = await task.run()
            task._set_state(TaskState.SUCCEEDED)
        except (asyncio.CancelledError, TaskCancelledError):
            task._set_state(TaskState.CANCELLED)
            task.report("cancelled", message="Cancelled by operator")
        except Exception as error:
            logger.exception("task %s failed", task.name)
            task.error = str(error)
            task._set_state(TaskState.FAILED)
            task.report("failed", message=str(error))
        finally:
            task.finished_at = time.time()
            if self._current is task:
                self._current = None

    async def cancel(self, task_id: str | None = None) -> bool:
        """Cancel the named task, or the running one."""
        if self._runner is None or self._runner.done():
            return False
        if task_id is not None and (self._current is None or self._current.id != task_id):
            return False
        self._runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._runner
        return True

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def recent(self, limit: int = 20) -> list[Task]:
        return [self._tasks[i] for i in reversed(self._order[-limit:]) if i in self._tasks]

    def _trim(self) -> None:
        while len(self._order) > self._history:
            self._tasks.pop(self._order.pop(0), None)
