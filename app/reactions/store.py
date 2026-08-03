"""Emoji reactions the owner puts on messages, recorded raw in Mongo.

A reaction is the cheapest signal the owner can give — one tap, no message — so it is captured as
it happened and interpreted nowhere: this store only appends what Telegram reported. Telegram sends
the WHOLE new reaction set on every change, not a delta, so a record keeps both sides (`previous` →
`current`); an empty `current` is a reaction taken back.
"""

from baski.primitives import datetime
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.models import NisseDbModel
from app.shared.mongo import ensure_index

_COLLECTION = "reactions"


class Reaction(NisseDbModel):
    """One reaction change on one message. Lifecycle: a data record — append-only, never edited.

    `reacted_at` is Telegram's own timestamp for the tap; `created_at` (NisseDbModel) is when we
    wrote it down.
    """

    conversation_id: int
    message_id: int
    user_id: int
    username: str
    previous: list[str]
    current: list[str]  # empty => the owner removed their reaction
    reacted_at: datetime.datetime


class ReactionStore:
    """Append-only writes to the `reactions` collection, scoped to one conversation.

    Lifecycle: per-conversation — built per update by the chat layer's reaction handler.
    """

    def __init__(self, database: AsyncDatabase, *, conversation_id: int) -> None:
        """Bind to the reactions collection for one conversation; every query is scoped to it."""
        self._collection = database[_COLLECTION]
        self._conversation_id = conversation_id

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """(conversation_id, message_id) — the key a future reader will look reactions up by. Idempotent."""
        await ensure_index(database[_COLLECTION], [("conversation_id", 1), ("message_id", 1)])

    async def record(  # noqa: PLR0913 — one document's fields, minus the conversation scope the store owns
        self,
        *,
        message_id: int,
        user_id: int,
        username: str,
        previous: list[str],
        current: list[str],
        reacted_at: datetime.datetime,
    ) -> None:
        """Append one reaction change; Mongo assigns `_id`."""
        reaction = Reaction(
            conversation_id=self._conversation_id,
            message_id=message_id,
            user_id=user_id,
            username=username,
            previous=previous,
            current=current,
            reacted_at=reacted_at,
        )
        await self._collection.insert_one(reaction.model_dump(exclude={"id"}))
