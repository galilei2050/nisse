"""Scheduled self-invocations — durable tasks the bot fires at a future time.

Two access shapes (the runner has only a task id, no conversation_id):
- `ScheduleStore` — conversation-scoped CRUD for the agent's tools (mirrors MemoryStore).
- `FireStore` — global by public_id: claim / reschedule / mark_done, for the fire path.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum

from baski.primitives import datetime
from pydantic import model_validator
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.models import PublicIdModel
from app.shared.mongo import ensure_index

_COLLECTION = "scheduled_tasks"
# How a fired task's instruction enters the transcript. The runner writes it and the curator skips
# it: a reminder firing is the bot prompting itself, never the owner speaking.
SCHEDULED_PREFIX = "[Запланировано]"


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


class ScheduledTask(PublicIdModel):
    """One scheduled self-invocation, bound to the conversation it fires into.

    Lifecycle: a data record — one Mongo document, transient in memory. `fire_at` is the next (UTC)
    occurrence; for RECURRING it advances by `repeat_every_hours` after each fire. `instruction` is
    fed to the agent at fire time as a normal user turn.
    """

    conversation_id: int
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

    def is_overdue(self, now: datetime.datetime) -> bool:
        """True when this occurrence's moment has passed and it still has not fired.

        A task is armed by one queue message; a delivery that never lands leaves the row PENDING at a
        past `fire_at` forever. Reading that as "armed" is how the owner's morning routine kept
        showing "next 04.07" for 34 days after it last ran.
        """
        return self.status is ScheduleStatus.PENDING and self.fire_at < now


class ScheduleStore:
    """Conversation-scoped CRUD over `scheduled_tasks`, for the agent's tools.

    Lifecycle: per-conversation — built in `_build_scheduling_tools` and held by that chat's tools,
    and built per request by `chat/saved.py` (the read-only `/schedules` viewer).
    (The fire path uses `FireStore`, which addresses a task by id alone.)
    """

    def __init__(self, database: AsyncDatabase, *, conversation_id: int) -> None:
        """Bind to the collection for one conversation; every query is scoped to it."""
        self._collection = database[_COLLECTION]
        self._conversation_id = conversation_id

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """Unique index on public_id (the agent-facing key). Idempotent; call once at startup."""
        await ensure_index(database[_COLLECTION], "public_id", unique=True)

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
        """Live, still-armed tasks in this conversation, soonest occurrence first — the index injected each turn.

        The order is the query's, not each caller's: the agent (its injected schedule list) and the
        owner (`/schedules`) must not read the same tasks in two different orders, and Mongo's
        natural order is arbitrary.
        """
        query = {"conversation_id": self._conversation_id, "status": ScheduleStatus.PENDING, "deleted_at": None}
        cursor = self._collection.find(query).sort("fire_at", 1)
        return [ScheduledTask.model_validate(doc) async for doc in cursor]

    async def cancel(self, public_id: str) -> bool:
        """Soft-delete a task in this conversation; True if a live one was found.

        Sets CANCELLED + deleted_at, so an already-enqueued Cloud Task fires once more, fails the
        claim (no longer PENDING), and no-ops — a recurring chain then stops re-enqueuing itself.
        """
        now = datetime.now()
        result = await self._collection.update_one(
            {"conversation_id": self._conversation_id, "public_id": public_id, "deleted_at": None},
            {"$set": {"status": ScheduleStatus.CANCELLED, "deleted_at": now, "updated_at": now}},
        )
        return result.modified_count > 0


class FireStore:
    """The fire path's view of `scheduled_tasks`: global, by public_id — no conversation scope.

    Separate from `ScheduleStore` because the runner is handed only a task id and an occurrence: it
    fires whatever Cloud Tasks delivers, and scoping it to a conversation it doesn't know would be a
    filter it could only satisfy by looking the task up first.

    Lifecycle: long-lived — one per `ScheduleRunner`, serving every fire.
    """

    def __init__(self, database: AsyncDatabase) -> None:
        """Bind the tasks collection; every method addresses one task by its public_id."""
        self._collection = database[_COLLECTION]

    @asynccontextmanager
    async def claim(self, *, public_id: str, fire_at: datetime.datetime) -> AsyncIterator[ScheduledTask | None]:
        """Claim one occurrence for execution: PENDING→RUNNING for this public_id+fire_at.

        The single source of idempotency under Cloud Tasks' at-least-once delivery: only the first
        delivery of an occurrence flips PENDING→RUNNING and yields the task; every duplicate (already
        RUNNING/DONE, or advanced to a later fire_at) matches nothing and yields None. If the body
        raises, the claim is released back to PENDING (when still RUNNING for this occurrence), so a
        failed fire leaves a task that can be re-armed rather than one wedged in RUNNING. Nothing
        re-runs it on its own: the queue is at-most-once (`max_attempts=1`).
        """
        doc = await self._collection.find_one_and_update(
            {"public_id": public_id, "fire_at": fire_at, "status": ScheduleStatus.PENDING, "deleted_at": None},
            {"$set": {"status": ScheduleStatus.RUNNING, "updated_at": datetime.now()}},
            return_document=ReturnDocument.AFTER,
        )
        task = ScheduledTask.model_validate(doc) if doc else None
        try:
            yield task
        # Releases on cancellation as well as on error. A KILLED process (SIGKILL, OOM) runs nothing
        # here: a one-shot's occurrence stays RUNNING and never fires again — a recurring task has
        # already re-armed itself by this point. There is no reaper yet.
        except BaseException:
            if task is not None:
                await self._collection.update_one(
                    {"public_id": public_id, "fire_at": fire_at, "status": ScheduleStatus.RUNNING},
                    {"$set": {"status": ScheduleStatus.PENDING, "updated_at": datetime.now()}},
                )
            raise

    async def due(self, *, now: datetime.datetime, limit: int) -> list[ScheduledTask]:
        """Occurrences still PENDING after their moment passed, oldest first — what the sweep repairs.

        Capped: a long outage can strand many at once, and handling them all inside one request would
        hold the single instance for minutes while the owner's own messages wait behind them.
        """
        cursor = (
            self._collection.find({"status": ScheduleStatus.PENDING, "deleted_at": None, "fire_at": {"$lte": now}})
            .sort("fire_at", 1)
            .limit(limit)
        )
        return [ScheduledTask.model_validate(doc) async for doc in cursor]

    async def reschedule(self, *, public_id: str, fire_at: datetime.datetime) -> None:
        """Re-arm a recurring task for its next occurrence: RUNNING→PENDING with the new fire_at."""
        await self._collection.update_one(
            {"public_id": public_id},
            {"$set": {"fire_at": fire_at, "status": ScheduleStatus.PENDING, "updated_at": datetime.now()}},
        )

    async def mark_done(self, *, public_id: str) -> None:
        """Finish a one-shot task: RUNNING→DONE."""
        await self._collection.update_one(
            {"public_id": public_id},
            {"$set": {"status": ScheduleStatus.DONE, "updated_at": datetime.now()}},
        )
