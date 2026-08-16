"""SubagentStore's write paths: what a retirement records, and what it takes to undo one (no live DB).

Retiring is the one roster write with no validation in front of it, and the revision it appends is
the ONLY surviving copy of a retired worker's prompt — the tool promises the owner exactly that. The
fake models just the ops the store uses, keyed by (conversation_id, name).
"""

import asyncio
from types import SimpleNamespace
from typing import Any

from app.shared.revisions import ChangeKind
from app.subagents.store import SubagentConfig, SubagentStore

CONVERSATION = 7


class _FakeCursor:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def __aiter__(self):  # noqa: ANN204
        return self._gen()

    async def _gen(self):  # noqa: ANN202
        for doc in self._docs:
            yield doc


class _FakeCollection:
    """In-memory stand-in keyed by (conversation_id, name); understands the one `$ne` the store uses."""

    def __init__(self) -> None:
        self.docs: dict[tuple[int, str], dict] = {}
        self.inserted: list[dict] = []  # documents with no (conversation_id, name) key — revisions

    @classmethod
    def _match(cls, doc: dict, flt: dict) -> bool:
        return all(cls._matches_one(doc.get(key), term) for key, term in flt.items())

    @staticmethod
    def _matches_one(value: object, term: object) -> bool:
        if isinstance(term, dict) and "$ne" in term:
            return value != term["$ne"]
        return value == term

    def find(self, flt: dict, _projection: dict | None = None) -> _FakeCursor:
        return _FakeCursor([d for d in self.docs.values() if self._match(d, flt)])

    async def find_one(self, flt: dict) -> dict | None:
        # Matched first, yielded after — a real query is answered from the state at the server when
        # it ran, and only the reply comes back later. Yielding BEFORE the match would instead model
        # a reader that sees every write racing it, and no interleaving could be reproduced at all.
        found = next((d for d in self.docs.values() if self._match(d, flt)), None)
        await asyncio.sleep(0)
        return found

    async def find_one_and_replace(self, flt: dict, replacement: dict, **_kwargs: Any) -> dict:
        key = (flt["conversation_id"], flt["name"])
        self.docs[key] = dict(replacement)
        return self.docs[key]

    async def update_one(self, flt: dict, update: dict) -> SimpleNamespace:
        doc = next((d for d in self.docs.values() if self._match(d, flt)), None)
        if doc is None:
            return SimpleNamespace(modified_count=0)
        doc.update(update["$set"])
        return SimpleNamespace(modified_count=1)

    async def insert_one(self, doc: dict) -> SimpleNamespace:
        self.inserted.append(doc)
        return SimpleNamespace(inserted_id="000000000000000000000001")


class _FakeDatabase:
    """One collection per name, so the store's writes to `revisions` never land among the configs."""

    def __init__(self) -> None:
        self.collections: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        return self.collections.setdefault(name, _FakeCollection())

    def deletions(self) -> list[dict]:
        """The DELETE revisions recorded so far."""
        return [r for r in self["revisions"].inserted if r["kind"] == ChangeKind.DELETE]


def _config(name: str, *, prompt: str = "Answer compactly.") -> SubagentConfig:
    return SubagentConfig(
        conversation_id=CONVERSATION,
        name=name,
        description="answers one self-contained lookup",
        system_prompt=prompt,
        model="claude-sonnet-5",
        tool_names=["google_search"],
        context_tokens=32000,
        max_turns=8,
        judge_prompt="Did it answer the question?",
    )


async def _seeded(*names: str) -> tuple[SubagentStore, _FakeDatabase]:
    database = _FakeDatabase()
    store = SubagentStore(database, conversation_id=CONVERSATION)  # type: ignore[arg-type]  # fake db
    for name in names:
        await store.save(_config(name))
    return store, database


async def test_retiring_hides_the_worker_and_keeps_its_config_in_the_history() -> None:
    """The revision is the only surviving copy of a retired worker's prompt — the tool tells the
    owner so, and `subagent_list` can no longer show it."""
    store, database = await _seeded("maps_list_reader")

    assert await store.soft_delete("maps_list_reader") is True

    assert await store.list() == []
    assert await store.get("maps_list_reader") is None
    (deletion,) = database.deletions()
    assert deletion["target"] == "maps_list_reader"
    assert "Answer compactly." in deletion["before"]  # the whole config, not just its name
    assert deletion["after"] is None


async def test_retiring_twice_reports_no_change() -> None:
    """The second call must not claim a retirement — the curator reports what the tool told it."""
    store, _ = await _seeded("maps_list_reader")
    await store.soft_delete("maps_list_reader")

    assert await store.soft_delete("maps_list_reader") is False


async def test_two_concurrent_retirements_record_one_deletion() -> None:
    """baski runs a turn's tool calls with `asyncio.gather`, so two retirements of one name can both
    read a live config before either writes. Recording off that read would put a change in the
    owner's report that the losing call never made."""
    store, database = await _seeded("maps_list_reader")

    outcomes = await asyncio.gather(
        store.soft_delete("maps_list_reader"),
        store.soft_delete("maps_list_reader"),
    )

    assert sorted(outcomes) == [False, True]
    assert len(database.deletions()) == 1


async def test_saving_the_name_again_revives_a_retired_worker() -> None:
    """The documented undo. It works because `save` replaces the document whole and does not filter
    on `deleted_at` — a refactor to `$set` of named fields would break it with nothing else red."""
    store, _ = await _seeded("maps_list_reader")
    await store.soft_delete("maps_list_reader")

    await store.save(_config("maps_list_reader", prompt="Rebuilt from the change history."))

    live = await store.list()
    assert [config.name for config in live] == ["maps_list_reader"]
    assert live[0].system_prompt == "Rebuilt from the change history."
    assert await store.retired_names() == set()


async def test_retired_names_is_what_the_seed_script_must_not_revive() -> None:
    """`make seed` re-saves every definition in agents.yml; without this read it would silently undo
    a retirement the curator made on the owner's evidence."""
    store, _ = await _seeded("retrieval", "maps_list_reader")
    await store.soft_delete("maps_list_reader")

    assert await store.retired_names() == {"maps_list_reader"}
