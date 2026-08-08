"""Two updates for one chat must share one Conversation — or the owner's message is destroyed.

`_build` awaits (it loads the transcript from Mongo). Without a lock, two concurrent updates both
miss the cache and each get their own Conversation: separate reply locks, so replies stop being
serialized, and separate histories minting turn ids from the same start. `_write_turn` upserts on
`(conversation_id, turn_id)`, so the second writer replaces the first turn's messages.

One Cloud Run instance is no protection — containerConcurrency is 80, so both requests run
concurrently in one process.
"""

import asyncio

from app.assistant.conversations import Conversations


class _SlowRegistry(Conversations):
    """The real get-or-create, with `_build` replaced by something that awaits like Mongo does."""

    def __init__(self) -> None:
        self._conversations = {}
        self._building = asyncio.Lock()
        self.builds = 0

    async def _build(self, conversation_id: int) -> object:  # type: ignore[override]
        self.builds += 1
        await asyncio.sleep(0)  # the load() round-trip: any await at all opens the race
        return object()


async def test_concurrent_first_messages_share_one_conversation():
    registry = _SlowRegistry()

    first, second = await asyncio.gather(registry.get(112991176), registry.get(112991176))

    assert first is second, "two Conversation objects for one chat: separate locks, separate turn ids"
    assert registry.builds == 1


async def test_different_chats_still_get_their_own():
    registry = _SlowRegistry()

    owner, other = await asyncio.gather(registry.get(112991176), registry.get(990041))

    assert owner is not other
    assert registry.builds == 2
