"""In-process background jobs so long operations (sync, labeling, reports) don't block pages."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from echo_voc.errors import EchoError
from echo_voc.timeutil import utcnow

log = logging.getLogger(__name__)

Progress = Callable[[str], None]
MAX_JOBS = 100

# Jobs in the same lane run one at a time: platform requests are rate limited and LLM labeling
# costs money, so running two in parallel only adds risk.
LANES = {"sync": "network", "embed": "embed", "label": "llm"}


@dataclass
class Job:
    id: int
    kind: str
    title: str
    status: str = "queued"  # queued | running | done | failed
    messages: deque[str] = field(default_factory=lambda: deque(maxlen=30))
    result: Any = None
    error: str | None = None
    context: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None

    @property
    def finished(self) -> bool:
        return self.status in {"done", "failed"}

    @property
    def last_message(self) -> str | None:
        return self.messages[-1] if self.messages else None


class JobManager:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._jobs: OrderedDict[int, Job] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    def submit(
        self,
        kind: str,
        title: str,
        fn: Callable[[Progress], Awaitable[Any]],
        **context: Any,
    ) -> Job:
        job = Job(id=next(self._ids), kind=kind, title=title, context=context)
        self._jobs[job.id] = job
        while len(self._jobs) > MAX_JOBS:
            oldest = next(iter(self._jobs.values()))
            if not oldest.finished:
                break
            self._jobs.popitem(last=False)
        task = asyncio.create_task(self._run(job, fn))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job, fn: Callable[[Progress], Awaitable[Any]]) -> None:
        lane = LANES.get(job.kind)
        lock = self._locks.setdefault(lane, asyncio.Lock()) if lane else None
        try:
            if lock is not None:
                if lock.locked():
                    job.messages.append("Waiting for another job of this kind to finish…")
                await lock.acquire()
            job.status = "running"
            try:
                job.result = await fn(job.messages.append)
                job.status = "done"
            finally:
                if lock is not None:
                    lock.release()
        except EchoError as e:
            job.status, job.error = "failed", str(e)
        except asyncio.CancelledError:
            job.status, job.error = "failed", "cancelled"
            raise
        except Exception as e:  # surface unexpected errors in the UI instead of losing them
            log.exception("Job %s (%s) crashed", job.id, job.kind)
            job.status, job.error = "failed", f"{type(e).__name__}: {e}"
        finally:
            job.finished_at = utcnow()

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self, limit: int = 10) -> list[Job]:
        return list(reversed(self._jobs.values()))[:limit]

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
