"""Change history — an append-only record of every mutation to the bot's durable stores.

The stores keep only the CURRENT state: core memory is overwritten in place, a memory body is
replaced, list items are dropped. Once a nightly curator edits them unattended, "what changed and
who changed it" stops being reconstructable — and an owner who can't audit a change can't trust the
agent that made it. So every content-losing write also appends a `Revision` here, with the text that
is about to disappear.

**Why a separate collection rather than a second version in the same one.** `memories` and `lists`
carry unique indexes that deliberately span soft-deleted documents: re-adding a cleared list REVIVES
its document, and a soft-deleted memory keeps its `public_id` reserved so it can never be reused.
A superseded copy living beside the live one would collide with both rules.

Who is writing is ambient, not a parameter: a store is built the same way for the live assistant and
for the curator, so the actor rides a context variable (`acting_as`) instead of being threaded
through every tool factory. Outside a curator run the actor is the assistant — that is simply true.
"""

import contextlib
from collections.abc import Generator
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

from pymongo.asynchronous.database import AsyncDatabase

from app.shared.models import NisseDbModel
from app.shared.mongo import ensure_index

_COLLECTION = "revisions"


class Actor(StrEnum):
    """Who performed a change — the axis the owner reads the history along."""

    ASSISTANT = "assistant"  # the live agent, mid-conversation, with the owner present
    CURATOR = "curator"  # the nightly consolidation pass, unattended


class ChangeKind(StrEnum):
    """What happened to the target record."""

    CREATE = "create"
    REPLACE = "replace"  # content swapped for new content — `before` is the only surviving copy
    DELETE = "delete"  # soft-deleted; the record survives, but it left the live set


@dataclass(frozen=True)
class Attribution:
    """Who is writing right now, and under which curator run (none when the assistant writes)."""

    actor: Actor
    run_id: str | None


_ASSISTANT = Attribution(actor=Actor.ASSISTANT, run_id=None)
_CURRENT: ContextVar[Attribution] = ContextVar("attribution", default=_ASSISTANT)


@contextlib.contextmanager
def acting_as(actor: Actor, *, run_id: str) -> Generator[None]:
    """Attribute every store write inside this block to `actor` and its run."""
    token = _CURRENT.set(Attribution(actor=actor, run_id=run_id))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_attribution() -> Attribution:
    """Who is writing in this context — the assistant unless a curator run wrapped the call."""
    return _CURRENT.get()


class Revision(NisseDbModel):
    """One recorded change to one record. Lifecycle: a data record — append-only, never edited.

    `before` holds the content the change destroyed and is what a restore reads; it is None only on
    a create, where nothing was lost. `target` is the record's agent-facing key within its store
    (a memory's `public_id`, a list's name, a prompt's type, a sub-agent's name).
    """

    conversation_id: int
    collection: str
    target: str
    kind: ChangeKind
    before: str | None
    after: str | None
    actor: Actor
    run_id: str | None


class RevisionLog:
    """Appends change records for one conversation. Lifecycle: per-conversation, built by each store.

    A store builds its own log from the database it already holds, so recording a change costs no
    plumbing at the call sites that construct tools.
    """

    def __init__(self, database: AsyncDatabase, *, conversation_id: int) -> None:
        """Bind to the revisions collection for one conversation."""
        self._collection = database[_COLLECTION]
        self._conversation_id = conversation_id

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """By run (what one curator pass did) and by target (one record's history). Idempotent."""
        await ensure_index(database[_COLLECTION], [("conversation_id", 1), ("run_id", 1)])
        await ensure_index(database[_COLLECTION], [("conversation_id", 1), ("collection", 1), ("target", 1)])

    async def record(  # noqa: PLR0913 — one revision's fields, minus the conversation scope the log owns
        self, *, collection: str, target: str, kind: ChangeKind, before: str | None, after: str | None
    ) -> None:
        """Append one change, attributed to whoever the ambient context says is writing."""
        attribution = current_attribution()
        revision = Revision(
            conversation_id=self._conversation_id,
            collection=collection,
            target=target,
            kind=kind,
            before=before,
            after=after,
            actor=attribution.actor,
            run_id=attribution.run_id,
        )
        await self._collection.insert_one(revision.model_dump(exclude={"id"}))

    async def for_run(self, run_id: str) -> list[Revision]:
        """Every change made during one curator run — the owner-facing "what it did last night"."""
        query = {"conversation_id": self._conversation_id, "run_id": run_id}
        return [Revision.model_validate(doc) async for doc in self._collection.find(query).sort("created_at", 1)]

    async def history(self, *, collection: str, target: str) -> list[Revision]:
        """One record's changes, oldest first — how a memory or the core block got to its current text."""
        query = {"conversation_id": self._conversation_id, "collection": collection, "target": target}
        return [Revision.model_validate(doc) async for doc in self._collection.find(query).sort("created_at", 1)]
