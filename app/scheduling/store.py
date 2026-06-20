"""Scheduled self-invocations — durable tasks the bot fires at a future time.

Two access shapes (the runner has only a task id, no conversation_id):
- `ScheduleStore` — conversation-scoped CRUD for the agent's tools (mirrors MemoryStore).
- module-level `claim` / `reschedule` / `mark_done` — global by public_id, for the fire path.
"""

import secrets
from enum import StrEnum

from baski.primitives import datetime
from pydantic import Field, model_validator
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.models import NisseDbModel

_COLLECTION = "scheduled_tasks"


class ScheduleKind(StrEnum):
    """One-shot reminder vs a repeating routine."""

    ONCE = "once"
    RECURRING = "recurring"


class ScheduleStatus(StrEnum):
    """Lifecycle of one task. RUNNING is the claimed-for-execution state (idempotency)."""

    PENDING = "pending"  # armed, waiting for its fire_at
    RUNNING = "running"  # claimed by a fire delivery; in-flight
    DONE = "done"  # one-shot fired and finished
    CANCELLED = "cancelled"  # owner cancelled it


def _new_public_id() -> str:
    """A short, LLM-friendly id the agent echoes back to list/cancel."""
    return secrets.token_hex(5)


class ScheduledTask(NisseDbModel):
    """One scheduled self-invocation, bound to the conversation it fires into.

    `fire_at` is the next (UTC) occurrence; for RECURRING it advances by `repeat_every_hours`
    after each fire. `instruction` is fed to the agent at fire time as a normal user turn.
    """

    conversation_id: int
    public_id: str = Field(default_factory=_new_public_id)
    kind: ScheduleKind
    instruction: str
    fire_at: datetime.datetime
    repeat_every_hours: int | None = None  # set iff kind is RECURRING
    status: ScheduleStatus = ScheduleStatus.PENDING

    @model_validator(mode="after")
    def _recurring_needs_period(self) -> "ScheduledTask":
        """A recurring task must say how often; a one-shot must not."""
        if self.kind is ScheduleKind.RECURRING and not self.repeat_every_hours:
            raise ValueError("repeat_every_hours is required (and > 0) when kind is 'recurring'")
        if self.kind is ScheduleKind.ONCE and self.repeat_every_hours is not None:
            raise ValueError("repeat_every_hours must be omitted when kind is 'once'")
        return self


class ScheduleStore:
    """Conversation-scoped CRUD over `scheduled_tasks`, for the agent's tools."""

    def __init__(self, database: AsyncDatabase, *, conversation_id: int) -> None:
        """Bind to the collection for one conversation; every query is scoped to it."""
        self._collection = database[_COLLECTION]
        self._conversation_id = conversation_id

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """Unique index on public_id (the agent-facing key). Idempotent; call once at startup."""
        await database[_COLLECTION].create_index("public_id", unique=True)

    async def add(
        self,
        *,
        kind: ScheduleKind,
        instruction: str,
        fire_at: datetime.datetime,
        repeat_every_hours: int | None = None,
    ) -> ScheduledTask:
        """Store a new task in this conversation; Mongo assigns `_id`, we keep the public_id."""
        task = ScheduledTask(
            conversation_id=self._conversation_id,
            kind=kind,
            instruction=instruction,
            fire_at=fire_at,
            repeat_every_hours=repeat_every_hours,
        )
        result = await self._collection.insert_one(task.model_dump(exclude={"id"}))
        task.id = str(result.inserted_id)
        return task

    async def list(self) -> list[ScheduledTask]:
        """Live, still-armed tasks in this conversation (for a future list/cancel tool)."""
        query = {"conversation_id": self._conversation_id, "status": ScheduleStatus.PENDING, "deleted_at": None}
        return [ScheduledTask.model_validate(doc) async for doc in self._collection.find(query)]


# ── Fire path (global, trusted): the runner has only a public_id + the occurrence's fire_at. ──


async def claim(database: AsyncDatabase, *, public_id: str, fire_at: datetime.datetime) -> ScheduledTask | None:
    """Atomically claim one occurrence for execution: PENDING→RUNNING for this public_id+fire_at.

    The single source of idempotency under Cloud Tasks' at-least-once delivery: only the first
    delivery of an occurrence flips PENDING→RUNNING and gets the task back; every duplicate
    (already RUNNING/DONE, or advanced to a later fire_at) matches nothing and gets None.
    """
    doc = await database[_COLLECTION].find_one_and_update(
        {"public_id": public_id, "fire_at": fire_at, "status": ScheduleStatus.PENDING, "deleted_at": None},
        {"$set": {"status": ScheduleStatus.RUNNING, "updated_at": datetime.now()}},
        return_document=ReturnDocument.AFTER,
    )
    return ScheduledTask.model_validate(doc) if doc else None


async def reschedule(database: AsyncDatabase, *, public_id: str, fire_at: datetime.datetime) -> None:
    """Re-arm a recurring task for its next occurrence: RUNNING→PENDING with the new fire_at."""
    await database[_COLLECTION].update_one(
        {"public_id": public_id},
        {"$set": {"fire_at": fire_at, "status": ScheduleStatus.PENDING, "updated_at": datetime.now()}},
    )


async def mark_done(database: AsyncDatabase, *, public_id: str) -> None:
    """Finish a one-shot task: RUNNING→DONE."""
    await database[_COLLECTION].update_one(
        {"public_id": public_id},
        {"$set": {"status": ScheduleStatus.DONE, "updated_at": datetime.now()}},
    )
