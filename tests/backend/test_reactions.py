"""Reactions: the handler is actually on the wire, and only the owner's taps are written.

Registering the handler is the whole wiring — Telegram sends `message_reaction` only because the
observer exists — so the first test drives the router aiogram would build, not the recorder alone.
"""

from types import SimpleNamespace

from aiogram import types
from baski.primitives import datetime

from app.chat.reactions import ReactionRecorder
from app.chat.router import build_router
from app.chat.saved import SavedViewer

OWNER = "galilei"
CHAT_ID = 42
REACTED_AT = datetime.as_utc(datetime.datetime(2026, 8, 2, 18, 30))


class _FakeCollection:
    """Records the documents insert_one was handed."""

    def __init__(self) -> None:
        self.inserted: list[dict] = []

    async def insert_one(self, doc: dict) -> SimpleNamespace:
        self.inserted.append(doc)
        return SimpleNamespace(inserted_id="6890f0c0c0de5eed00000001")


class _FakeDatabase(dict):
    def __missing__(self, name: str) -> _FakeCollection:
        collection = _FakeCollection()
        self[name] = collection
        return collection


class _FakeTurns:
    """Stands in for TurnLookup: the message→turn link the chat layer wrote when it sent the answer."""

    def __init__(self, links: dict[int, int] | None = None) -> None:
        self._links = links or {}

    async def turn_for_message(self, *, conversation_id: int, message_id: int) -> int | None:
        assert conversation_id == CHAT_ID
        return self._links.get(message_id)


def _recorder(db: _FakeDatabase, links: dict[int, int] | None = None) -> ReactionRecorder:
    return ReactionRecorder(db, turns=_FakeTurns(links))


def _update(
    *,
    username: str | None,
    new: list[types.ReactionTypeUnion],
    old: list[types.ReactionTypeUnion] | None = None,
) -> types.MessageReactionUpdated:
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


def test_the_router_subscribes_to_reaction_updates() -> None:
    """Telegram sends message_reaction only for a registered observer — drop the wiring and the
    signal disappears with no error anywhere."""
    router = build_router(
        assistant=SimpleNamespace(),
        transcriber=SimpleNamespace(),
        speaker=SimpleNamespace(),
        saved=SavedViewer(_FakeDatabase()),
        reactions=_recorder(_FakeDatabase()),
    )
    assert "message_reaction" in router.resolve_used_update_types()


async def test_owner_reaction_is_written_whole_every_kind_kept() -> None:
    """The store is a raw log: a dropped field or an unrecognised emoji kind is signal lost for good."""
    db = _FakeDatabase()
    reactions = [
        types.ReactionTypeEmoji(emoji="❤"),
        types.ReactionTypeCustomEmoji(custom_emoji_id="5411", type="custom_emoji"),
        types.ReactionTypePaid(type="paid"),
    ]
    await _recorder(db, {96: 7}).record(_update(username=OWNER, new=reactions))

    (doc,) = db["reactions"].inserted
    audit = {doc.pop(field) for field in ("created_at", "updated_at")}
    assert audit  # stamped by NisseDbModel; their values are wall-clock, not part of the contract
    assert doc == {
        "conversation_id": CHAT_ID,
        "message_id": 96,
        "turn_id": 7,
        "user_id": 7,
        "username": OWNER,
        "previous": [],
        "current": ["❤", "custom:5411", "paid"],
        "reacted_at": REACTED_AT,
        "deleted_at": None,
    }


async def test_changing_one_of_two_reactions_keeps_both_sides_whole() -> None:
    """A Premium account can hold up to three reactions at once, and Telegram reports the WHOLE set
    on each side — not the one that changed. Treating either side as a delta would lose the reaction
    that stayed put."""
    db = _FakeDatabase()
    await _recorder(db).record(
        _update(
            username=OWNER,
            old=[types.ReactionTypeEmoji(emoji="👍"), types.ReactionTypeEmoji(emoji="❤")],
            new=[types.ReactionTypeEmoji(emoji="👍"), types.ReactionTypeEmoji(emoji="🔥")],
        )
    )

    (doc,) = db["reactions"].inserted
    assert doc["previous"] == ["👍", "❤"]
    assert doc["current"] == ["👍", "🔥"]  # 👍 survives the change, 🔥 replaces ❤


async def test_taking_a_reaction_back_is_recorded_as_an_empty_current() -> None:
    """Telegram sends the whole new set, so a removal arrives as new_reaction: [] — not as a delete."""
    db = _FakeDatabase()
    await _recorder(db).record(_update(username=OWNER, new=[], old=[types.ReactionTypeEmoji(emoji="👍")]))

    (doc,) = db["reactions"].inserted
    assert doc["previous"] == ["👍"]
    assert doc["current"] == []


async def test_a_reaction_on_a_message_no_turn_produced_is_still_recorded() -> None:
    """Plenty of messages are not an agent answer — a transcript echo, a `/lists` view, an error
    notice. The tap is still real signal, so it is written with no turn rather than dropped."""
    db = _FakeDatabase()
    await _recorder(db, {12: 3}).record(_update(username=OWNER, new=[types.ReactionTypeEmoji(emoji="❤")]))

    (doc,) = db["reactions"].inserted
    assert doc["message_id"] == 96  # not the linked 12
    assert doc["turn_id"] is None


async def test_a_stranger_is_not_recorded() -> None:
    """message_reaction bypasses AllowlistMiddleware, which guards `message` only."""
    db = _FakeDatabase()
    await _recorder(db).record(_update(username="someone-else", new=[types.ReactionTypeEmoji(emoji="❤")]))
    assert db["reactions"].inserted == []


async def test_an_anonymous_reactor_is_not_recorded() -> None:
    db = _FakeDatabase()
    await _recorder(db).record(_update(username=None, new=[types.ReactionTypeEmoji(emoji="❤")]))
    assert db["reactions"].inserted == []
