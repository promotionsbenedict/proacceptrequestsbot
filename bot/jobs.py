"""In-memory background-job registry for bulk approval runs.

A single approval job runs per chat at a time. Jobs live only in memory: the
authoritative queue is Telegram itself, so "recovery" after a restart simply
means the owner taps *Approve All* again and the run resumes from whatever is
still pending on Telegram. The local mirror in the database is preserved either
way, so nothing is lost.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ApprovalJob:
    """State for one running bulk-approval task."""

    chat_id: int
    channel_id: int
    title: str
    mode: str  # "all" | "amount"
    target: int = 0  # requested amount ("amount" mode); 0 when approving all
    approved: int = 0
    total: int = 0  # best-known size of the queue (moving target for "all")
    status: str = "running"  # running | done | cancelled | error
    task: Optional[asyncio.Task] = None
    _cancel: asyncio.Event = field(default_factory=asyncio.Event)

    def request_cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()


class JobManager:
    """Tracks at most one active :class:`ApprovalJob` per chat_id."""

    def __init__(self) -> None:
        self._jobs: dict[int, ApprovalJob] = {}

    def running(self, chat_id: int) -> Optional[ApprovalJob]:
        job = self._jobs.get(chat_id)
        return job if job is not None and job.status == "running" else None

    def register(self, job: ApprovalJob) -> None:
        self._jobs[job.chat_id] = job

    def finish(self, chat_id: int) -> None:
        self._jobs.pop(chat_id, None)
