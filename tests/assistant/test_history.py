"""MongoMessageHistory: write-once persistence, durable trimming, recoverable prunes (no live DB).

The fake collection models only the operations MongoMessageHistory uses. Turns are driven through
the real context manager so `__exit__` fires the fire-and-forget write; `flush()` awaits it. The
load-bearing test is `test_compaction_persists_so_dropped_turns_do_not_resurrect`: a turn dropped
by `compact()` must end up soft-deleted in Mongo, or it resurrects on the next `load()` (every Cloud
Run cold start).
"""

from types import SimpleNamespace

from anthropic.types import Usage
from baski.primitives import datetime as dt

from baski.agents.tools.delete_messages import DeleteMessagesTool

from app.assistant.history import MongoMessageHistory

_BIG_USAGE = Usage(input_tokens=60_000, output_tokens=0)  # over 0.9 * 32_000 → what compact() reads as over budget


class _FakeCursor:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    async def to_list(self, length: int | None = None) -> list[dict]:
        return list(self._docs)


class _FakeCollection:
    """In-memory stand-in keyed by (conversation_id, turn_id); records inserts to catch double-writes."""

    def __init__(self) -> None:
        self.docs: dict[tuple[int, int], dict] = {}
        self.inserted_ids: list[int] = []

    @staticmethod
    def _match(doc: dict, flt: dict) -> bool:
        for key, val in flt.items():
            if isinstance(val, dict) and "$in" in val:
                if doc.get(key) not in val["$in"]:
                    return False
            elif isinstance(val, dict) and "$nin" in val:
                if doc.get(key) in val["$nin"]:
                    return False
            elif doc.get(key) != val:
                return False
        return True

    @staticmethod
    def _sorted(docs: list[dict], sort: list[tuple[str, int]] | None) -> list[dict]:
        for key, direction in reversed(sort or []):
            docs = sorted(docs, key=lambda d: d[key], reverse=direction == -1)
        return docs

    def find(self, flt: dict, sort: list[tuple[str, int]] | None = None) -> _FakeCursor:
        return _FakeCursor(self._sorted([d for d in self.docs.values() if self._match(d, flt)], sort))

    async def find_one(self, flt: dict, sort: list[tuple[str, int]] | None = None) -> dict | None:
        matched = self._sorted([d for d in self.docs.values() if self._match(d, flt)], sort)
        return matched[0] if matched else None

    async def update_one(self, flt: dict, update: dict, upsert: bool = False) -> SimpleNamespace:
        key = (flt["conversation_id"], flt["turn_id"])
        doc = self.docs.get(key)
        if doc is None:
            assert upsert, "a turn should only ever be inserted"
            self.docs[key] = {**update.get("$setOnInsert", {}), **update["$set"]}
            self.inserted_ids.append(flt["turn_id"])
            return SimpleNamespace(modified_count=0)
        for field, value in update.get("$addToSet", {}).items():
            doc.setdefault(field, [])
            doc[field] += [v for v in value["$each"] if v not in doc[field]]
        doc.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=1)

    async def update_many(self, flt: dict, update: dict) -> SimpleNamespace:
        n = 0
        for doc in self.docs.values():
            if self._match(doc, flt):
                doc.update(update["$set"])
                n += 1
        return SimpleNamespace(modified_count=n)


class _FakeDatabase:
    def __init__(self, collection: _FakeCollection) -> None:
        self._collection = collection

    def __getitem__(self, _name: str) -> _FakeCollection:
        return self._collection


def _history(collection: _FakeCollection, conversation_id: int = 1) -> MongoMessageHistory:
    return MongoMessageHistory(database=_FakeDatabase(collection), conversation_id=conversation_id)


def _active_ids(collection: _FakeCollection) -> list[int]:
    return sorted(d["turn_id"] for d in collection.docs.values() if d["deleted_at"] is None)


def _texts(messages: list[dict]) -> list[str]:
    """Every text block in a rendered payload, in order — turn markers included."""
    out: list[str] = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            out += [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return out


def _markers(hist: MongoMessageHistory) -> list[str]:
    """The `[Turn N …]` marker text rendered for each turn (one per turn, in order)."""
    return [text for text in _texts(hist.format_for_api()) if text.startswith("[Turn ")]


def _seed_turn(col: _FakeCollection, turn_id: int, created_at: object, conversation_id: int = 1) -> None:
    """Insert one active turn doc directly, to control its created_at (the recency-marker source)."""
    col.docs[(conversation_id, turn_id)] = {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "messages": [{"role": "user", "content": [{"type": "text", "text": f"m{turn_id}"}]}],
        "created_at": created_at,
        "deleted_at": None,
    }


def _add_user(hist: MongoMessageHistory, text: str = "hi") -> None:
    with hist:
        hist.add_user_text(text)


def _add_answer(hist: MongoMessageHistory, text: str = "answer") -> None:
    with hist:
        hist.add_assistant([{"type": "text", "text": text}])


def _add_tool_turn(hist: MongoMessageHistory, tool_id: str = "t1") -> None:
    with hist:
        hist.add_assistant([{"type": "tool_use", "id": tool_id, "name": "x", "input": {}}])
        hist.add_tool_results([{"type": "tool_result", "tool_use_id": tool_id, "content": "payload"}])


async def test_format_for_api_strips_thinking_from_completed_turns() -> None:
    """Settled turns' thinking blocks are omitted from the API payload (billed on Opus 4.5+), text kept."""
    col = _FakeCollection()
    hist = _history(col)
    await hist.load()
    _add_user(hist, "q")
    with hist:
        hist.add_assistant(
            [
                {"type": "thinking", "thinking": "private reasoning", "signature": "sig123"},
                {"type": "text", "text": "the answer"},
            ]
        )
    await hist.flush()

    blocks = [b for m in hist.format_for_api() if isinstance(m["content"], list) for b in m["content"]]
    types = [b.get("type") for b in blocks if isinstance(b, dict)]
    assert "thinking" not in types  # stripped from the completed turn
    assert "the answer" in [b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    assert col.docs[(1, 2)]["messages"]  # ...but Mongo keeps the full turn (thinking recoverable)


async def test_each_turn_written_exactly_once() -> None:
    """Turns are inserted once on commit — no rewrites, no double-writes (solves write amplification)."""
    col = _FakeCollection()
    hist = _history(col)
    await hist.load()

    _add_user(hist, "1")
    _add_answer(hist, "2")
    await hist.flush()
    _add_user(hist, "3")
    await hist.flush()

    assert sorted(col.inserted_ids) == [1, 2, 3]
    assert len(col.inserted_ids) == 3  # each turn inserted exactly once


async def test_reported_usage_never_moves_the_transcript() -> None:
    """The loop reports its context size after every call; none of that may drop a turn.

    A turn leaving mid-run moves the head of the message list, so the cached prefix stops matching and
    the whole transcript is re-written on every remaining turn — and the reply loses context it is
    still composing against.
    """
    col = _FakeCollection()
    hist = _history(col)
    await hist.load()
    _add_user(hist, "1")
    _add_answer(hist, "2")
    _add_answer(hist, "3")

    for _ in range(3):  # three over-budget calls inside one run
        hist.truncate(_BIG_USAGE)

    assert [t.id for t in hist.turns] == [1, 2, 3]


def _seed_old_search(col: _FakeCollection, turn_id: int, tool_id: str, said: str) -> None:
    """A narrated search from hours ago: the agent's words, its call, and the dump that came back."""
    col.docs[(1, turn_id)] = {
        "conversation_id": 1,
        "turn_id": turn_id,
        "created_at": dt.datetime.now() - dt.timedelta(hours=3),
        "deleted_at": None,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": said},
                    {"type": "tool_use", "id": tool_id, "name": "google_search", "input": {"q": "x"}},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "huge dump"}]},
        ],
    }


def _seed_old_photo(col: _FakeCollection, turn_id: int) -> None:
    """A photo the owner sent hours ago, with its caption."""
    col.docs[(1, turn_id)] = {
        "conversation_id": 1,
        "turn_id": turn_id,
        "created_at": dt.datetime.now() - dt.timedelta(hours=2),
        "deleted_at": None,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}},
                    {"type": "text", "text": "что на чеке?"},
                ],
            }
        ],
    }


def _add_narrated_tool_turn(hist: MongoMessageHistory, tool_id: str) -> None:
    """A tool round the model narrated — text plus the call, which is what Opus actually produces."""
    with hist:
        hist.add_assistant(
            [
                {"type": "text", "text": "сейчас гляну"},
                {"type": "tool_use", "id": tool_id, "name": "google_search", "input": {}},
            ]
        )
        hist.add_tool_results([{"type": "tool_result", "tool_use_id": tool_id, "content": "fresh dump"}])


def _kinds(hist: MongoMessageHistory) -> list[str]:
    """Every block type the API would receive, in order."""
    return [
        b["type"] for m in hist.format_for_api() if isinstance(m["content"], list) for b in m["content"] if isinstance(b, dict)
    ]


async def test_an_old_turn_is_sent_as_its_words_alone() -> None:
    """Past the window the API stops seeing the calls, dumps and attachments — the words stay."""
    col = _FakeCollection()
    _seed_old_search(col, 1, "t1", "сейчас поищу")
    _seed_old_search(col, 2, "t2", "уточню ещё раз")
    _seed_old_photo(col, 3)
    hist = _history(col)
    await hist.load()
    _add_user(hist, "и что там?")
    _add_narrated_tool_turn(hist, "fresh")

    sent = hist.format_for_api()
    texts = [b["text"] for m in sent if isinstance(m["content"], list) for b in m["content"] if isinstance(b, dict) and b["type"] == "text"]
    payloads = [b for m in sent if isinstance(m["content"], list) for b in m["content"] if isinstance(b, dict) and b["type"] in ("tool_use", "tool_result")]

    assert "сейчас поищу" in texts and "уточню ещё раз" in texts and "что на чеке?" in texts
    assert "image" not in _kinds(hist)  # the hours-old attachment is not sent
    assert [b.get("id") or b.get("tool_use_id") for b in payloads] == ["fresh", "fresh"]  # only this reply's pair


async def test_a_fresh_turn_is_sent_whole_so_a_follow_up_can_reach_its_output() -> None:
    """"Show me the second one you found" needs the dump of the exchange it follows."""
    col = _FakeCollection()
    hist = _history(col)
    await hist.load()
    _add_user(hist, "поищи автосервисы")
    _add_narrated_tool_turn(hist, "fresh")

    kinds = _kinds(hist)

    assert "tool_use" in kinds and "tool_result" in kinds


async def test_rendering_leaves_the_transcript_and_mongo_untouched() -> None:
    """Hiding is a view. Ask for the same transcript twice and nothing has been lost in between."""
    col = _FakeCollection()
    _seed_old_search(col, 1, "t1", "сейчас поищу")
    hist = _history(col)
    await hist.load()

    hist.format_for_api()

    assert [len(t.messages) for t in hist.turns] == [2]  # the turn still holds both messages
    assert len(col.docs[(1, 1)]["messages"][0]["content"]) == 2  # ...text AND the call
    assert col.docs[(1, 1)]["messages"][1]["content"][0]["content"] == "huge dump"
    assert "tool_use" not in _kinds(hist)  # ...while the API still doesn't see them


async def test_an_old_turn_that_was_only_machinery_is_sent_as_nothing() -> None:
    """No words, nothing to say: an old pure tool round costs not even its `[Turn N]` marker."""
    col = _FakeCollection()
    col.docs[(1, 1)] = {
        "conversation_id": 1,
        "turn_id": 1,
        "created_at": dt.datetime.now() - dt.timedelta(hours=3),
        "deleted_at": None,
        "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "dump"}]},
        ],
    }
    hist = _history(col)
    await hist.load()

    assert hist.format_for_api() == []


async def test_delete_turns_persists_and_keeps_the_deleted_turn_readable() -> None:
    """The load-bearing case: what the agent deletes is soft-deleted in Mongo, never resurrected."""
    col = _FakeCollection()
    hist = _history(col)
    await hist.load()
    _add_user(hist, "1")
    _add_answer(hist, "2")
    _add_answer(hist, "3")
    await hist.flush()
    assert _active_ids(col) == [1, 2, 3]

    removed = await hist.delete_turns([2])
    assert removed == 1
    await hist.flush()
    assert _active_ids(col) == [1, 3]
    assert col.docs[(1, 2)]["messages"]  # content intact — recoverable

    cold = _history(col)  # simulate a Cloud Run cold start
    await cold.load()
    assert [t.id for t in cold.turns] == [1, 3]  # turn 2 does NOT come back


async def test_prune_transcript_keep_last_drops_older_turns_durably() -> None:
    """prune_transcript(keep_last=N) drops all but the last N turns; the prune persists across reload."""
    col = _FakeCollection()
    hist = _history(col)
    await hist.load()
    for i in range(1, 6):
        _add_answer(hist, str(i))
    await hist.flush()
    assert _active_ids(col) == [1, 2, 3, 4, 5]

    result = await DeleteMessagesTool(hist).execute(keep_last=2)
    assert "Deleted 3" in result  # turns 1,2,3 dropped
    await hist.flush()
    assert _active_ids(col) == [4, 5]

    cold = _history(col)
    await cold.load()
    assert [t.id for t in cold.turns] == [4, 5]


async def test_turn_marker_carries_utc_send_time_on_first_turn_and_after_gap() -> None:
    """A marker shows the UTC send-time on the first turn and after a >1h gap; bare otherwise.

    Drives the cold-start `load()` path so created_at comes from Mongo, like production.
    """
    col = _FakeCollection()
    base = dt.as_utc(dt.datetime(2026, 6, 21, 10, 0))
    _seed_turn(col, 1, base)  # first turn → always stamped
    _seed_turn(col, 2, base + dt.timedelta(minutes=5))  # +5min, no gap → bare
    _seed_turn(col, 3, base + dt.timedelta(hours=2))  # +2h gap → stamped
    hist = _history(col)
    await hist.load()

    assert _markers(hist) == [
        "[Turn 1 · 2026-06-21 10:00 UTC]",
        "[Turn 2]",
        "[Turn 3 · 2026-06-21 12:00 UTC]",
    ]


async def test_turn_marker_handles_naive_created_at_from_mongo() -> None:
    """A naive created_at (pymongo without tz_aware) is treated as UTC, not crashed on or misread."""
    col = _FakeCollection()
    _seed_turn(col, 1, dt.datetime(2026, 6, 21, 9, 30))  # naive — no tzinfo
    hist = _history(col)
    await hist.load()

    assert _markers(hist) == ["[Turn 1 · 2026-06-21 09:30 UTC]"]


async def test_pure_tool_turn_written_soft_deleted_but_recoverable() -> None:
    """A pure tool turn is written already soft-deleted: this session still uses it, a later one won't.

    It stays in the live transcript, because a follow-up in the next few minutes may reach into its
    output. The soft-delete is what keeps it from being restored hours later, when only the words are
    still worth sending.
    """
    col = _FakeCollection()
    hist = _history(col)
    await hist.load()
    _add_user(hist, "question")
    _add_tool_turn(hist)
    _add_answer(hist, "answer")
    await hist.flush()

    assert _active_ids(col) == [1, 3]
    assert col.docs[(1, 2)]["deleted_at"] is not None  # tool turn soft-deleted
    assert col.docs[(1, 2)]["messages"]  # but recoverable
    assert [t.id for t in hist.turns] == [1, 2, 3]  # and still usable while this process lives

    cold = _history(col)
    await cold.load()
    assert [t.id for t in cold.turns] == [1, 3]


async def test_a_delivered_message_reaches_the_model_on_the_very_next_turn() -> None:
    """What the owner types mid-reply must be in the payload the running loop builds next, not wait
    for a whole new reply — that is the point of handing it to the history instead of starting one."""
    col = _FakeCollection()
    hist = _history(col)
    await hist.load()
    _add_user(hist, "сколько стоит")
    _add_tool_turn(hist)  # the agent is mid-work: a tool round with its results already in

    hist.deliver("в евро, не в долларах")

    assert hist.has_incoming
    assert "в евро, не в долларах" in _texts(hist.format_for_api())
    assert not hist.has_incoming  # taken, so the turn after this one does not repeat it


async def test_a_delivered_message_lands_as_its_own_turn_after_the_tool_results() -> None:
    """Appending it into the turn the agent has open would put user text between its `tool_use`
    blocks and the results that must follow them — a payload the API rejects outright."""
    col = _FakeCollection()
    hist = _history(col)
    await hist.load()
    _add_user(hist, "сколько стоит")
    _add_tool_turn(hist)

    hist.deliver("в евро")
    payload = hist.format_for_api()
    await hist.flush()

    assert payload[-1]["role"] == "user"
    assert [t.id for t in hist.turns] == [1, 2, 3]
    assert col.docs[(1, 3)]["messages"] == [{"role": "user", "content": [{"type": "text", "text": "в евро"}]}]


async def test_link_messages_stamps_the_turn_it_is_given_not_the_newest() -> None:
    """The ids are linked after the answer is sent, by when another reply can already have added
    turns. Taking "the newest" then hangs one answer's messages on a turn that never produced them,
    and a reaction on that answer resolves to the wrong exchange."""
    col = _FakeCollection()
    hist = _history(col)
    await hist.load()
    _add_user(hist, "вопрос")
    _add_answer(hist, "ответ")
    answered = hist.last_turn_id
    _add_user(hist, "следующий вопрос")  # the reply that started while the answer was still being sent
    await hist.flush()

    await hist.link_messages(turn_id=answered, message_ids=[1771])

    assert col.docs[(1, 2)]["message_ids"] == [1771]
    assert "message_ids" not in col.docs[(1, 3)]
