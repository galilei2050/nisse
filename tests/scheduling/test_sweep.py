"""The sweep is what makes the durable row the trigger of record.

A task is armed by one at-most-once Cloud Tasks message. The owner's morning routine lost its single
dispatch on 2026-07-04 and nothing noticed for 34 days, because the next occurrence is only queued
inside a successful fire. The sweep re-reads what is overdue on a clock the queue cannot lose.
"""

from typing import Any

from baski.primitives import datetime

from app.scheduling.runner import ScheduleRunner
from app.scheduling.store import ScheduleKind, ScheduleStatus

_PAST = datetime.now() - datetime.timedelta(days=34)


def _doc(*, public_id: str, kind: ScheduleKind, fire_at: datetime.datetime, every: int | None = None) -> dict:
    return {
        "public_id": public_id,
        "conversation_id": 42,
        "kind": kind,
        "instruction": "Позвонить в клинику" if kind is ScheduleKind.ONCE else "Утренний чек-ин",
        "fire_at": fire_at,
        "repeat_every_hours": every,
        "status": ScheduleStatus.PENDING,
        "deleted_at": None,
    }


class _FakeCursor:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def sort(self, *_args: Any) -> "_FakeCursor":
        return _FakeCursor(sorted(self._docs, key=lambda d: d["fire_at"]))

    def limit(self, count: int) -> "_FakeCursor":
        return _FakeCursor(self._docs[:count])

    async def __aiter__(self):
        for doc in self._docs:
            yield doc


class _FakeCollection:
    """Just the four operations the fire path uses, over a list of documents."""

    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs

    def find(self, query: dict) -> _FakeCursor:
        matched = [
            d
            for d in self.docs
            if d["status"] == query["status"] and d["deleted_at"] is None and d["fire_at"] <= query["fire_at"]["$lte"]
        ]
        return _FakeCursor(matched)

    async def find_one_and_update(self, query: dict, update: dict, **_kwargs: Any) -> dict | None:
        for doc in self.docs:
            if doc["public_id"] == query["public_id"] and doc["fire_at"] == query["fire_at"]:
                doc.update(update["$set"])
                return dict(doc)
        return None

    async def update_one(self, query: dict, update: dict) -> None:
        for doc in self.docs:
            if doc["public_id"] == query["public_id"]:
                doc.update(update["$set"])


class _FakeDatabase:
    def __init__(self, collection: _FakeCollection) -> None:
        self._collection = collection

    def __getitem__(self, _name: str) -> _FakeCollection:
        return self._collection


class _FakeAssistant:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def reply(self, *, conversation_id: int, text: str, **_kwargs: Any) -> Any:
        self.asked.append(text)
        return type("Reply", (), {"result": object(), "turn_id": 1})()

    async def flush(self, *, conversation_id: int) -> None:
        pass


class _FakeSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, *, chat_id: int, text: str) -> None:
        self.sent.append(text)


class _FakeScheduling:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, datetime.datetime]] = []

    async def enqueue_fire(self, *, public_id: str, fire_at: datetime.datetime) -> None:
        self.enqueued.append((public_id, fire_at))


class _DeadChatSender:
    """Telegram refusing a chat that no longer exists — the failure the sweep must not loop on."""

    async def send(self, *, chat_id: int, text: str) -> None:
        raise RuntimeError("Bad Request: chat not found")


def _runner(
    docs: list[dict], sender: Any = None
) -> tuple[ScheduleRunner, _FakeCollection, _FakeAssistant, Any, _FakeScheduling]:
    collection = _FakeCollection(docs)
    assistant, scheduling = _FakeAssistant(), _FakeScheduling()
    sender = sender or _FakeSender()
    runner = ScheduleRunner(
        assistant=assistant,  # type: ignore[arg-type]
        sender=sender,  # type: ignore[arg-type]
        database=_FakeDatabase(collection),  # type: ignore[arg-type]
        scheduling=scheduling,  # type: ignore[arg-type]
        format_answer=lambda _result: "готово",
    )
    return runner, collection, assistant, sender, scheduling


async def test_a_stranded_routine_is_re_armed_and_not_replayed():
    """34 missed mornings must not arrive at once, and the next one must actually be queued."""
    docs = [_doc(public_id="6ca1cff154", kind=ScheduleKind.RECURRING, fire_at=_PAST, every=24)]
    runner, collection, assistant, sender, scheduling = _runner(docs)

    assert await runner.sweep() == 1

    assert collection.docs[0]["fire_at"] > datetime.now()  # pointed at a future occurrence
    assert collection.docs[0]["status"] == ScheduleStatus.PENDING
    assert scheduling.enqueued == [("6ca1cff154", collection.docs[0]["fire_at"])]  # and actually queued
    assert assistant.asked == []  # the missed mornings are not replayed
    assert sender.sent == []


async def test_a_stranded_reminder_is_delivered_and_says_it_is_late():
    """A one-shot is the whole point of the reminder — dropping it loses what the owner asked for.
    Arriving days late with no explanation reads as the bot being confused, not catching up."""
    docs = [_doc(public_id="d81014603c", kind=ScheduleKind.ONCE, fire_at=_PAST)]
    runner, collection, assistant, sender, _scheduling = _runner(docs)

    assert await runner.sweep() == 1

    assert len(assistant.asked) == 1
    assert "опоздание" in assistant.asked[0]
    assert f"{_PAST:%d.%m %H:%M}" in assistant.asked[0]  # names the moment it should have arrived
    assert "Позвонить в клинику" in assistant.asked[0]
    assert sender.sent == ["готово"]
    assert collection.docs[0]["status"] == ScheduleStatus.DONE  # and it does not fire twice


async def test_the_sweep_leaves_future_occurrences_alone():
    docs = [
        _doc(
            public_id="3cfb35b0b1",
            kind=ScheduleKind.RECURRING,
            fire_at=datetime.now() + datetime.timedelta(hours=8),
            every=24,
        )
    ]
    runner, _collection, assistant, _sender, scheduling = _runner(docs)

    assert await runner.sweep() == 0
    assert scheduling.enqueued == []
    assert assistant.asked == []


async def test_an_undeliverable_reminder_is_closed_instead_of_retried_forever():
    """A failed fire releases its claim back to PENDING, so an unclosed one comes back to every
    later sweep — and the usual cause is a chat that no longer exists, which no repeat can fix.
    Each attempt is a paid agent run, so the loop would bill every 15 minutes, forever."""
    docs = [_doc(public_id="d81014603c", kind=ScheduleKind.ONCE, fire_at=_PAST)]
    runner, collection, assistant, _sender, _scheduling = _runner(docs, sender=_DeadChatSender())

    assert await runner.sweep() == 1

    assert collection.docs[0]["status"] == ScheduleStatus.DONE
    assert len(assistant.asked) == 1
    assert await runner.sweep() == 0  # and it is gone from the next sweep's view


async def test_one_sweep_handles_a_bounded_batch():
    """Each stranded one-shot costs a full agent run on a single-instance service; an unbounded
    batch would park the owner's own messages behind minutes of catch-up."""
    docs = [
        _doc(public_id=f"task{n:02d}", kind=ScheduleKind.ONCE, fire_at=_PAST + datetime.timedelta(minutes=n))
        for n in range(25)
    ]
    runner, _collection, assistant, _sender, _scheduling = _runner(docs)

    handled = await runner.sweep()

    assert handled == 10
    assert len(assistant.asked) == 10
