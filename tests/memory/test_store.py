"""Memory tools: index rendering and the remember/read/forget tool logic (no DB)."""

from baski.primitives import datetime

from app.memory.store import Memory, MemorySource
from app.memory.tools import ForgetTool, RecallMemoryTool, RememberTool


def _memory(public_id: str, *, category: str, source: dict, title: str, body: str = "body text") -> Memory:
    return Memory.model_validate(
        {
            "_id": "deadbeefdeadbeefdeadbeef",
            "public_id": public_id,
            "title": title,
            "category": category,
            "source": source,
            "created_at": "2026-06-18T00:00:00Z",
            "body": body,
        }
    )


class FakeStore:
    """In-memory stand-in for MemoryStore — the tools only touch add/get/soft_delete."""

    def __init__(self, memories: list[Memory] | None = None) -> None:
        self._m = {m.public_id: m for m in (memories or [])}
        self.added: list[Memory] = []

    async def add(self, *, title, category, source, body) -> Memory:  # noqa: ANN001
        mem = _memory("p1", category=category, source=source.model_dump(), title=title, body=body)
        self._m[mem.public_id] = mem
        self.added.append(mem)
        return mem

    async def get(self, public_id: str) -> Memory | None:
        mem = self._m.get(public_id)
        return mem if (mem and mem.deleted_at is None) else None

    async def soft_delete(self, public_id: str) -> bool:
        mem = self._m.get(public_id)
        if mem and mem.deleted_at is None:
            mem.deleted_at = datetime.now()
            return True
        return False


def test_index_renders_pointers_not_bodies() -> None:
    memories = [
        _memory("a1", category="preference", source={"kind": "user"}, title="Prefers metric units", body="SECRET"),
        _memory("b2", category="event", source={"kind": "external", "ref": "https://x.test"}, title="Booked flight"),
    ]
    msg = RecallMemoryTool(FakeStore(), memories).user_message()
    assert msg is not None
    text = msg["content"][0]["text"]

    assert "YOUR LONG-TERM MEMORY" in text
    assert "[a1] preference · user · 2026-06-18 — Prefers metric units" in text
    assert "[b2] event · external:https://x.test · 2026-06-18 — Booked flight" in text
    assert "SECRET" not in text  # bodies are fetched on demand, never in the index


def test_empty_index_injects_nothing() -> None:
    assert RecallMemoryTool(FakeStore(), []).user_message() is None


async def test_remember_saves_with_source() -> None:
    store = FakeStore()
    result = await RememberTool(store).execute(
        title="Wife is Lena", category="fact", source={"kind": "user", "ref": None}, body="Owner's wife is Lena."
    )
    assert result == "Saved memory p1."
    assert store.added[0].source == MemorySource(kind="user", ref=None)


async def test_read_returns_body_or_not_found() -> None:
    mem = _memory("a1", category="fact", source={"kind": "user"}, title="t", body="the body")
    tool = RecallMemoryTool(FakeStore([mem]), [mem])
    assert await tool.execute(public_id="a1") == "the body"
    assert await tool.execute(public_id="zz") == "No memory zz."


async def test_forget_soft_deletes_then_reports_missing() -> None:
    mem = _memory("a1", category="fact", source={"kind": "user"}, title="t")
    tool = ForgetTool(FakeStore([mem]))
    assert await tool.execute(public_id="a1") == "Forgot memory a1."
    assert await tool.execute(public_id="a1") == "No memory a1."  # already soft-deleted
