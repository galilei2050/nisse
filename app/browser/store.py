"""Where each chat's logged-in browser session lives — one Mongo document per conversation.

A Playwright storage-state (cookies + localStorage, ~35KB, the owner's saved logins) is small
per-user data read on each browser action, so it lives in Mongo beside the chat's other
per-conversation stores (lists, memories, prompts) — not in object storage. `make startbrowser`
writes it here after you log in; `BrowserSession` reads it to act with your logins. Cloud Run is
stateless, so Mongo is also what makes the session survive in production. One document per
`conversation_id`, overwritten in place.
"""

from baski.primitives import datetime
from playwright.async_api import StorageState
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.mongo import ensure_index

_COLLECTION = "browser_sessions"


class BrowserSessionStore:
    """Read/write one conversation's browser storage-state. Lifecycle: per-conversation."""

    def __init__(self, database: AsyncDatabase, *, conversation_id: int) -> None:
        """Bind to the browser_sessions collection for one conversation; every query is scoped to it."""
        self._collection = database[_COLLECTION]
        self._conversation_id = conversation_id

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """Unique index on conversation_id — one session document per chat. Idempotent.

        No caller yet: `backend.py`'s startup hook builds the indexes of the stores that are wired, and
        this one isn't. Until it is registered there, `save`'s upsert has nothing enforcing
        one-document-per-chat, so two concurrent saves can leave `load` picking an arbitrary one.
        """
        await ensure_index(database[_COLLECTION], [("conversation_id", 1)], unique=True)

    async def load(self) -> StorageState | None:
        """This chat's saved storage-state (cookies + origins), or None if it has never logged in."""
        doc = await self._collection.find_one({"conversation_id": self._conversation_id})
        return doc["storage_state"] if doc else None

    async def save(self, storage_state: StorageState) -> None:
        """Overwrite this chat's storage-state in place (upsert) — written by `make startbrowser`."""
        now = datetime.now()
        await self._collection.update_one(
            {"conversation_id": self._conversation_id},
            {
                "$set": {"storage_state": storage_state, "updated_at": now},
                "$setOnInsert": {"conversation_id": self._conversation_id, "created_at": now},
            },
            upsert=True,
        )
