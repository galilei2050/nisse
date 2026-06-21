"""Memory tools: index rendering and the remember/read/forget tool logic (no DB)."""

from baski.primitives import datetime

from app.memory.store import Memory, MemorySource
from app.memory.tools import EditMemoryTool, ForgetTool, RecallMemoryTool, RememberTool


def _memory(public_id: str, *, category: str, source: dict, title: str, body: str = "body text") -> Memory:
    return Memory.model_validate(
        {
            "_id": "deadbeefdeadbeefdeadbeef",
            "conversation_id": 1,
            "public_id": public_id,
            "title": title,
            "category": category,
            "source": source,
            "created_at": "2026-06-18T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",  # differs from created_at so the index test proves it uses updated_at
            "body": body,
        }
    )


class FakeStore:
    """In-memory stand-in for the scoped MemoryStore — the tools touch list/add/get/soft_delete."""

    def __init__(self, memories: list[Memory] | None = None) -> None:
        self._m = {m.public_id: m for m in (memories or [])}
        self.added: list[Memory] = []

    async def list(self) -> list[Memory]:
        return [m for m in self._m.values() if m.deleted_at is None]

    async def add(self, *, title, category, source, body) -> Memory:  # noqa: ANN001
        mem = _memory("p1", category=category, source=source.model_dump(), title=title, body=body)
        self._m[mem.public_id] = mem
        self.added.append(mem)
        return mem

    async def get(self, public_id: str) -> Memory | None:
        mem = self._m.get(public_id)
        return mem if (mem and mem.deleted_at is None) else None

    async def overwrite(self, public_id, *, title, category, source, body) -> Memory:  # noqa: ANN001, PLR0913
        mem = self._m.get(public_id)
        if mem is None or mem.deleted_at is not None:
            return await self.add(title=title, category=category, source=source, body=body)
        mem.title, mem.category, mem.source, mem.body = title, category, source, body
        return mem

    async def set_body(self, public_id: str, *, body) -> None:  # noqa: ANN001
        self._m[public_id].body = body

    async def soft_delete(self, public_id: str) -> bool:
        mem = self._m.get(public_id)
        if mem and mem.deleted_at is None:
            mem.deleted_at = datetime.now()
            return True
        return False


async def test_index_renders_pointers_grouped_by_category() -> None:
    memories = [
        _memory("a1", category="fact", source={"kind": "user"}, title="Drives a BMW Z4", body="SECRET"),
        _memory("b2", category="event", source={"kind": "external", "ref": "https://x.test"}, title="Booked flight"),
    ]
    msg = await RecallMemoryTool(FakeStore(memories)).user_message()
    assert msg is not None
    text = msg["content"][0]["text"]

    assert "YOUR LONG-TERM MEMORY" in text
    assert "FACT" in text  # category is the group header, not part of the pointer line
    assert "- [a1] user · 2026-06-20 — Drives a BMW Z4" in text  # updated_at, not created_at (2026-06-18)
    assert "EVENT" in text
    assert "- [b2] external · 2026-06-20 — Booked flight" in text  # kind only — the url is not in the index
    assert "https://x.test" not in text  # the external ref/url lives in the full read, not the index
    assert "SECRET" not in text  # bodies are fetched on demand, never in the index


async def test_empty_index_injects_nothing() -> None:
    assert await RecallMemoryTool(FakeStore()).user_message() is None


async def test_remember_saves_with_source() -> None:
    store = FakeStore()
    result = await RememberTool(store).execute(
        title="Wife is Lena", category="fact", source={"kind": "user", "ref": None}, body="Owner's wife is Lena."
    )
    assert result == "Saved memory p1."
    assert store.added[0].source == MemorySource(kind="user", ref=None)


async def test_read_returns_body_or_not_found() -> None:
    mem = _memory("a1", category="fact", source={"kind": "user"}, title="t", body="the body")
    tool = RecallMemoryTool(FakeStore([mem]))
    assert await tool.execute(public_id="a1") == "the body"  # no ref for a user source
    assert await tool.execute(public_id="zz") == "No memory zz."


async def test_read_appends_source_link_for_external() -> None:
    mem = _memory("a1", category="fact", source={"kind": "external", "ref": "https://x.test"}, title="t", body="the body")
    out = await RecallMemoryTool(FakeStore([mem])).execute(public_id="a1")
    assert "the body" in out
    assert "https://x.test" in out  # the link surfaces in the full read, not the index


async def test_forget_soft_deletes_then_reports_missing() -> None:
    mem = _memory("a1", category="fact", source={"kind": "user"}, title="t")
    tool = ForgetTool(FakeStore([mem]))
    assert await tool.execute(public_id="a1") == "Forgot memory a1."
    assert await tool.execute(public_id="a1") == "No memory a1."  # already soft-deleted


async def test_remember_with_public_id_overwrites_in_place() -> None:
    mem = _memory("a1", category="fact", source={"kind": "user"}, title="Loves chocolate", body="Loves chocolate.")
    store = FakeStore([mem])
    result = await RememberTool(store).execute(
        public_id="a1",
        title="Loves dark chocolate",
        category="fact",
        source={"kind": "user", "ref": None},
        body="Loves dark chocolate, 70%+.",
    )
    assert result == "Updated memory a1."  # same id, no churn
    assert store.added == []  # overwrote in place, did not add a second doc
    assert (await store.get("a1")).body == "Loves dark chocolate, 70%+."


async def test_remember_overwrite_of_gone_id_creates_fresh() -> None:
    store = FakeStore()  # the id was soft-deleted / never existed
    result = await RememberTool(store).execute(
        public_id="gone", title="t", category="fact", source={"kind": "user", "ref": None}, body="b"
    )
    assert result == "Saved new memory p1."  # a fresh id, not the gone one
    assert len(store.added) == 1


async def test_edit_memory_appends_when_old_empty() -> None:
    mem = _memory("a1", category="fact", source={"kind": "user"}, title="t", body="line one")
    store = FakeStore([mem])
    assert await EditMemoryTool(store).execute(public_id="a1", old="", new="line two") == "Edited memory a1."
    assert (await store.get("a1")).body == "line one\nline two"


async def test_edit_memory_replaces_only_the_fragment() -> None:
    mem = _memory("a1", category="fact", source={"kind": "user"}, title="t", body="two extra shots, no sugar")
    store = FakeStore([mem])
    result = await EditMemoryTool(store).execute(public_id="a1", old="two extra shots", new="three extra shots")
    assert result == "Edited memory a1."
    assert (await store.get("a1")).body == "three extra shots, no sugar"  # rest untouched


async def test_edit_memory_no_match_hands_back_body_unchanged() -> None:
    mem = _memory("a1", category="fact", source={"kind": "user"}, title="t", body="the real body")
    store = FakeStore([mem])
    result = await EditMemoryTool(store).execute(public_id="a1", old="not present", new="x")
    assert "not found verbatim" in result
    assert "the real body" in result  # current body returned for a grounded retry
    assert (await store.get("a1")).body == "the real body"  # never a silent change


async def test_edit_memory_unknown_id_reports_missing() -> None:
    assert await EditMemoryTool(FakeStore()).execute(public_id="zz", old="", new="x") == "No memory zz."
