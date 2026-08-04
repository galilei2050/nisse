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
    """Applies the sort it is handed. Both behaviours under test — "the last reaction record wins"
    and "turns fold in order" — are correct only because of the `sort=` the queries pass, so a fake
    that ignored it would keep these tests green while production read a retracted 👍 as standing."""

    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def find(self, flt: dict, sort: list[tuple[str, int]] | None = None) -> _FakeCursor:
        found = [d for d in self._docs if self._match(d, flt)]
        for key, direction in reversed(sort or []):
            found.sort(key=lambda d, k=key: d[k], reverse=direction < 0)  # type: ignore[misc]  # bound per iteration
        return _FakeCursor(found)

    @staticmethod
    def _match(doc: dict, flt: dict) -> bool:
        for key, condition in flt.items():
            if isinstance(condition, dict):  # {"$gte": ...} / {"$in": [...]} — the operators used here
                if "$gte" in condition and doc[key] < condition["$gte"]:
                    return False
                if "$in" in condition and doc.get(key) not in condition["$in"]:
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
    Reading the earlier record would hand the curator approval that no longer exists.

    The records are supplied newest-first so the assertion depends on the query's sort, not on the
    order the fixture happens to list them in."""
    db = _FakeDatabase(
        turns=[_turn(1, "как дела", "нормально")],
        reactions=[_reaction(1, [], at_hour=11), _reaction(1, ["👍"], at_hour=10)],
    )

    evidence = await collect(db, conversation_id=CONVERSATION, since=SINCE)

    assert evidence.exchanges[0].reactions == []
    assert evidence.reaction_count == 0


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


async def test_a_judge_retry_is_not_read_as_the_owner_correcting_the_bot() -> None:
    """baski's completeness judge feeds its verdict back as a USER message, and the transcript keeps
    it. Read as owner input it is the perfect fake correction — it literally says the answer isn't
    finished and what is missing — so the curator would promote its own judge's words into a standing
    rule, the self-confirming loop its prompt forbids. It must extend the owner's exchange instead,
    so the redone answer is what hangs off the owner's question."""
    retry = _turn(2, "[Completeness check] Your answer isn't finished. Убери фразу «Ты прав».", "", at_hour=9)
    redone = _turn(3, "", "Итог: 830 евро.", at_hour=9)
    db = _FakeDatabase(turns=[_turn(1, "посчитай бюджет", "Ты прав в главном…"), retry, redone], reactions=[])

    evidence = await collect(db, conversation_id=CONVERSATION, since=SINCE)

    assert len(evidence.exchanges) == 1
    exchange = evidence.exchanges[0]
    assert exchange.owner_text == "посчитай бюджет"
    assert exchange.answer_text == "Итог: 830 евро."  # the redone answer, not the draft the judge rejected
    assert evidence.owner_message_count == 1
    assert "Completeness check" not in render(evidence)


async def test_a_reaction_left_the_next_morning_still_reaches_the_curator() -> None:
    """The owner reads an answer over coffee and taps it hours later, so the turn and the tap fall in
    different nights. Windowing both would lose it forever: the run holding the turn predates the tap,
    and the run holding the tap no longer collects the turn."""
    late = _reaction(1, ["👍"], at_hour=9)
    late["reacted_at"] = datetime.as_utc(datetime.datetime(2026, 8, 5, 9, 0))  # two days after the turn
    db = _FakeDatabase(turns=[_turn(1, "сравни отели", "вот сравнение")], reactions=[late])

    evidence = await collect(db, conversation_id=CONVERSATION, since=SINCE)

    assert evidence.exchanges[0].reactions == ["👍"]


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
