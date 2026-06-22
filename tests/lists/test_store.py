"""ListStore: case-folded names, de-duplicated items, in-place edits, soft-delete (no live DB).

The fake collection models only the ops ListStore uses (find / find_one / find_one_and_update /
update_one) keyed by (conversation_id, name). The point under test is that a list is never forked by
capitalization and an item is never added twice — the bug that put a duplicated shopping list into
long-term memory.
"""

from types import SimpleNamespace

from app.lists.store import ListStore
from app.lists.tools import ListEditTool


class _FakeCursor:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def __aiter__(self):  # noqa: ANN204
        return self._gen()

    async def _gen(self):  # noqa: ANN202
        for d in self._docs:
            yield d


class _FakeCollection:
    """In-memory stand-in keyed by (conversation_id, name)."""

    def __init__(self) -> None:
        self.docs: dict[tuple[int, str], dict] = {}

    @staticmethod
    def _match(doc: dict, flt: dict) -> bool:
        return all(doc.get(k) == v for k, v in flt.items())

    def find(self, flt: dict) -> _FakeCursor:
        return _FakeCursor([d for d in self.docs.values() if self._match(d, flt)])

    async def find_one(self, flt: dict) -> dict | None:
        return next((d for d in self.docs.values() if self._match(d, flt)), None)

    async def find_one_and_update(self, flt: dict, update: dict, upsert: bool = False, return_document: object = None):  # noqa: ANN201, ARG002, FBT002
        key = (flt["conversation_id"], flt["name"])
        doc = self.docs.get(key)
        if doc is None:
            assert upsert
            doc = {**update.get("$setOnInsert", {})}
            self.docs[key] = doc
        doc.update(update["$set"])
        return doc

    async def update_one(self, flt: dict, update: dict) -> SimpleNamespace:
        doc = next((d for d in self.docs.values() if self._match(d, flt)), None)
        if doc is None:
            return SimpleNamespace(modified_count=0)
        doc.update(update["$set"])
        return SimpleNamespace(modified_count=1)


class _FakeDatabase:
    def __init__(self, collection: _FakeCollection) -> None:
        self._collection = collection

    def __getitem__(self, _name: str) -> _FakeCollection:
        return self._collection


def _store(col: _FakeCollection, conversation_id: int = 1) -> ListStore:
    return ListStore(_FakeDatabase(col), conversation_id=conversation_id)


async def test_add_creates_then_dedupes_case_insensitively() -> None:
    col = _FakeCollection()
    store = _store(col)
    await store.add("shopping", ["Milk", "eggs"])
    result = await store.add("shopping", ["milk", "Bread"])  # 'milk' dup of 'Milk'
    assert result.items == ["Milk", "eggs", "Bread"]


async def test_same_name_different_case_is_one_list() -> None:
    col = _FakeCollection()
    store = _store(col)
    await store.add("Shopping", ["milk"])
    await store.add("shopping", ["bread"])  # 'Shopping' and 'shopping' must be the SAME list
    lists = await store.all()
    assert len(lists) == 1
    assert lists[0].items == ["milk", "bread"]


async def test_remove_exact_is_case_insensitive_and_missing_list() -> None:
    col = _FakeCollection()
    store = _store(col)
    await store.add("shopping", ["Milk", "eggs"])
    result = await store.remove("shopping", ["MILK"])
    assert result.updated is not None
    assert result.updated.items == ["eggs"]
    assert result.removed == ["Milk"]
    assert (await store.remove("todo", ["x"])).updated is None  # no such list


async def test_remove_by_unique_fragment_drops_it_but_ambiguous_is_left() -> None:
    col = _FakeCollection()
    store = _store(col)
    await store.add(
        "contradictions",
        ["Wants friends but loves coding alone", "Wants an independent yet available partner", "Likes acro"],
    )
    # 'Wants' occurs in two items → ambiguous, nothing removed, reported for retry (check while both present)
    amb = await store.remove("contradictions", ["Wants"])
    assert amb.removed == []
    assert amb.ambiguous == ["Wants"]
    assert len(amb.updated.items) == 3
    # a unique fragment removes the one item that contains it
    one = await store.remove("contradictions", ["coding alone"])
    assert one.removed == ["Wants friends but loves coding alone"]
    assert "Wants friends but loves coding alone" not in one.updated.items
    # a fragment matching nothing is reported missing
    miss = await store.remove("contradictions", ["pizza"])
    assert miss.missing == ["pizza"]


async def test_list_edit_tool_dispatches_add_remove_clear() -> None:
    col = _FakeCollection()
    tool = ListEditTool(_store(col))
    await tool.execute(name="shopping", add=["milk", "eggs"])
    out = await tool.execute(name="shopping", remove=["eggs"])
    assert "milk" in out
    assert "eggs" not in out
    cleared = await tool.execute(name="shopping", clear=True)
    assert "Cleared" in cleared
    assert await _store(col).get("shopping") is None


async def test_clear_soft_deletes_and_drops_from_index() -> None:
    col = _FakeCollection()
    store = _store(col)
    await store.add("shopping", ["milk"])
    assert await store.clear("shopping") is True
    assert await store.all() == []
    assert await store.get("shopping") is None
    assert await store.clear("shopping") is False  # already gone
