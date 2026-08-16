"""The record of each curator pass — what it reviewed, what it changed, what it told the owner.

Separate from `revisions` on purpose: a revision says a record's text changed, this says why the
night's work happened at all and carries the reasoning the owner reads. Together they answer both
questions an owner asks about an unattended agent — "what did it change" and "what was it thinking".
"""

from baski.primitives import datetime
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.models import NisseDbModel
from app.shared.mongo import ensure_index

_COLLECTION = "curator_runs"


class CuratorRun(NisseDbModel):
    """One maintenance pass. Lifecycle: a data record — written once when the pass ends.

    A pass skipped for want of anything to learn from is still recorded, with an empty report:
    "ran and found nothing" must be distinguishable from "never ran". Its counts are the window's
    real ones — a night of scheduled check-ins has exchanges and no owner in them, which reads
    differently from a night with no traffic at all.
    """

    conversation_id: int
    run_id: str  # what every revision this pass wrote is stamped with
    since: datetime.datetime  # start of the reviewed window
    exchanges_reviewed: int  # owner-message-to-answer pairs, not raw API turns
    owner_messages: int  # of those, the ones a human opened (the rest are schedules firing)
    reactions_reviewed: int
    signals: list[str]  # the classification, flattened — kind + subject per owner message
    changes: int  # revisions written during the pass
    report: str  # the owner-facing summary the pass produced
    cost: float


class CuratorRunStore:
    """Append-only writes and reads over `curator_runs`. Lifecycle: long-lived, one per bot."""

    def __init__(self, database: AsyncDatabase) -> None:
        """Bind to the runs collection; the conversation is a field, not a scope — runs span chats."""
        self._collection = database[_COLLECTION]

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """By conversation and recency — how the owner and the next pass both read it. Idempotent."""
        await ensure_index(database[_COLLECTION], [("conversation_id", 1), ("created_at", -1)])

    async def record(self, run: CuratorRun) -> CuratorRun:
        """Persist one finished pass; Mongo assigns `_id`."""
        result = await self._collection.insert_one(run.model_dump(exclude={"id"}))
        run.id = str(result.inserted_id)
        return run
