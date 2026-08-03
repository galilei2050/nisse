"""ReactionRecorder: only the owner's taps are recorded, and every reaction kind leaves a trace.

The store is a raw signal log, so the test asserts the whole document it writes — a dropped field
here is a signal silently lost, which no later reader can notice.
"""

from aiogram import types
from baski.primitives import datetime

from app.chat.reactions import ReactionRecorder, _labels

OWNER = "galilei"
CHAT_ID = 42
REACTED_AT = datetime.as_utc(datetime.datetime(2026, 8, 2, 18, 30))


class _FakeResult:
    inserted_id = "6890f0c0c0de5eed00000001"


class _FakeCollection:
    """Records the documents insert_one was handed."""

    def __init__(self) -> None:
        self.inserted: list[dict] = []

    async def insert_one(self, doc: dict) -> _FakeResult:
        self.inserted.append(doc)
        return _FakeResult()


class _FakeDatabase(dict):
    def __missing__(self, name: str) -> _FakeCollection:
        collection = _FakeCollection()
        self[name] = collection
        return collection


def _update(*, username: str | None, new: list, old: list | None = None) -> types.MessageReactionUpdated:  # noqa: ANN001 — aiogram reaction union
    user = (
        None if username is None else types.User.model_construct(id=7, is_bot=False, first_name="V", username=username)
    )
    return types.MessageReactionUpdated.model_construct(
        chat=types.Chat.model_construct(id=CHAT_ID, type="private"),
        message_id=96,
        user=user,
        date=REACTED_AT,
        old_reaction=old or [],
        new_reaction=new,
    )


def test_emoji_custom_and_unknown_kinds_all_leave_a_label() -> None:
    """A raw log must not silently drop a reaction kind it doesn't recognise."""
    reactions = [
        types.ReactionTypeEmoji(emoji="❤"),
        types.ReactionTypeCustomEmoji(custom_emoji_id="5411", type="custom_emoji"),
        types.ReactionTypePaid(type="paid"),
    ]
    assert _labels(reactions) == ["❤", "custom:5411", "paid"]


async def test_owner_reaction_is_recorded_in_full() -> None:
    db = _FakeDatabase()
    await ReactionRecorder(db).record(_update(username=OWNER, new=[types.ReactionTypeEmoji(emoji="❤")]))

    (doc,) = db["reactions"].inserted
    assert doc["conversation_id"] == CHAT_ID
    assert doc["message_id"] == 96
    assert doc["user_id"] == 7
    assert doc["username"] == OWNER
    assert doc["previous"] == []
    assert doc["current"] == ["❤"]
    assert doc["reacted_at"] == REACTED_AT


async def test_taking_a_reaction_back_is_recorded_as_an_empty_current() -> None:
    """Telegram sends the whole new set, so a removal arrives as new_reaction: [] — not as a delete."""
    db = _FakeDatabase()
    await ReactionRecorder(db).record(_update(username=OWNER, new=[], old=[types.ReactionTypeEmoji(emoji="👍")]))

    (doc,) = db["reactions"].inserted
    assert doc["previous"] == ["👍"]
    assert doc["current"] == []


async def test_a_stranger_is_not_recorded() -> None:
    """message_reaction bypasses AllowlistMiddleware, which guards `message` only."""
    db = _FakeDatabase()
    await ReactionRecorder(db).record(_update(username="someone-else", new=[types.ReactionTypeEmoji(emoji="❤")]))
    assert db["reactions"].inserted == []


async def test_an_anonymous_reactor_is_not_recorded() -> None:
    db = _FakeDatabase()
    await ReactionRecorder(db).record(_update(username=None, new=[types.ReactionTypeEmoji(emoji="❤")]))
    assert db["reactions"].inserted == []
