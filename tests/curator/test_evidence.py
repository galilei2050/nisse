"""Evidence: the digest the curator reasons from — how turns become exchanges, and what a reaction
currently says.

Two properties are load-bearing. The transcript stores one turn per API call, so an owner question
that took three tool rounds looks like one message and two monologues unless the turns are folded
back into an exchange. And the reaction log is append-only with the WHOLE set per record, so "what
is on this message now" is the last record — a curator reading a retracted 👍 as still standing would
learn from approval the owner explicitly took back.
"""

from baski.primitives import datetime

from app.curator.evidence import collect, render

CONVERSATION = 42
SINCE = datetime.as_utc(datetime.datetime(2026, 8, 3, 0, 0))


def _turn(turn_id: int, owner: str, answer: str, *, at_hour: int = 9) -> dict:
    return {
        "conversation_id": CONVERSATION,
        "turn_id": turn_id,
        "created_at": datetime.as_utc(datetime.datetime(2026, 8, 3, at_hour, 0)),
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": owner}]},
            {"role": "assistant", "content": [{"type": "text", "text": answer}]},
        ],
    }


def _reaction(turn_id: int, current: list[str], *, at_hour: int) -> dict:
    return {
        "conversation_id": CONVERSATION,
        "turn_id": turn_id,
        "current": current,
        "reacted_at": datetime.as_utc(datetime.datetime(2026, 8, 3, at_hour, 0)),
    }


class _FakeCursor:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    async def to_list(self, length: object = None) -> list[dict]:  # noqa: ARG002 — pymongo's signature
        return self._docs


class _FakeCollection:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def find(self, flt: dict, sort: object = None) -> _FakeCursor:  # noqa: ARG002 — order is fixed by the fixture
        return _FakeCursor([d for d in self._docs if self._match(d, flt)])

    @staticmethod
    def _match(doc: dict, flt: dict) -> bool:
        for key, condition in flt.items():
            if isinstance(condition, dict):  # {"$gte": ...} / {"$ne": ...} — the only operators used here
                if "$gte" in condition and doc[key] < condition["$gte"]:
                    return False
                if "$ne" in condition and doc.get(key) == condition["$ne"]:
                    return False
            elif doc.get(key) != condition:
                return False
        return True


class _FakeDatabase:
    def __init__(self, *, turns: list[dict], reactions: list[dict]) -> None:
        self._collections = {
            "conversation_turns": _FakeCollection(turns),
            "reactions": _FakeCollection(reactions),
        }

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._collections[name]


async def test_a_retracted_reaction_is_not_reported_as_standing() -> None:
    """Telegram sends the whole set on every change, so an empty `current` means it was taken back.
    Reading the earlier record would hand the curator approval that no longer exists."""
    db = _FakeDatabase(
        turns=[_turn(1, "как дела", "нормально")],
        reactions=[_reaction(1, ["👍"], at_hour=10), _reaction(1, [], at_hour=11)],
    )

    evidence = await collect(db, conversation_id=CONVERSATION, since=SINCE)

    assert evidence.exchanges[0].reactions == []
    assert evidence.reaction_count == 0


async def test_the_latest_reaction_set_reaches_the_digest() -> None:
    db = _FakeDatabase(
        turns=[_turn(1, "сравни варианты", "вот сравнение")],
        reactions=[_reaction(1, ["👍"], at_hour=10), _reaction(1, ["🔥", "❤"], at_hour=12)],
    )

    evidence = await collect(db, conversation_id=CONVERSATION, since=SINCE)

    assert evidence.exchanges[0].reactions == ["🔥", "❤"]
    assert evidence.reaction_count == 1
    assert "🔥 ❤" in render(evidence)  # the curator must see WHICH emoji, not just that one exists


async def test_a_multi_turn_answer_folds_into_one_exchange_ending_in_the_real_answer() -> None:
    """One question that took two tool rounds is ONE thing the owner said. Left as raw turns it reads
    as an owner message followed by two unprompted monologues, and the middle one — live progress
    narration — would be graded as if it were the answer."""
    narration = _turn(2, "", "Ищу рейсы.", at_hour=9)
    final = _turn(3, "", "Нашла три рейса, дешевле всего в среду.", at_hour=9)
    db = _FakeDatabase(
        turns=[_turn(1, "найди рейс в Лиссабон", ""), narration, final],
        reactions=[_reaction(3, ["👍"], at_hour=10)],  # the tap landed on the LAST message
    )

    evidence = await collect(db, conversation_id=CONVERSATION, since=SINCE)

    assert len(evidence.exchanges) == 1
    exchange = evidence.exchanges[0]
    assert exchange.turn_id == 1  # named by the turn the owner opened
    assert exchange.owner_text == "найди рейс в Лиссабон"
    assert exchange.answer_text == "Нашла три рейса, дешевле всего в среду."  # not the narration
    assert exchange.reactions == ["👍"]  # a reaction anywhere inside the exchange belongs to it


async def test_a_scheduled_self_prompt_is_not_counted_as_the_owner_speaking() -> None:
    """A reminder firing enters the transcript as a user message. Classified as owner input, the bot
    would be learning from its own scheduled prompts — the self-confirming loop the design forbids."""
    db = _FakeDatabase(
        turns=[_turn(1, "[Запланировано] Вечерний чек-ин", "Как прошёл день?"), _turn(2, "нормально", "Хорошо")],
        reactions=[],
    )

    evidence = await collect(db, conversation_id=CONVERSATION, since=SINCE)

    scheduled, real = evidence.exchanges
    assert scheduled.scheduled and not scheduled.has_owner_input
    assert real.has_owner_input
    assert evidence.owner_message_count == 1
    assert "NOT the owner" in render(evidence)  # the classifier is told which block to skip
