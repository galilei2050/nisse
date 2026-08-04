"""Sub-agent management: the two guards that keep a bad config from reaching the build.

`subagents` is a trusted admin surface — a config decides which tools, which model and which prompt a
child agent runs with. A bad `tool_names` entry raises at the next conversation build, which takes
the whole chat down rather than one tool call; catching it at write time keeps the blast radius on
the curator that wrote it.
"""

from types import SimpleNamespace

from app.subagents.tools import ALLOWED_MODELS, SubagentSaveTool
from app.tools.registry import ToolRegistry

CONVERSATION = 42


class _FakeStore:
    """Records saves; `list()` is what the tool checks delegation targets against."""

    def __init__(self, existing: list[str] | None = None) -> None:
        self.saved: list[object] = []
        self._existing = existing or []

    async def list(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=name) for name in self._existing]

    async def get(self, name: str) -> None:
        return None

    async def save(self, config: object) -> object:
        self.saved.append(config)
        return config


def _fields(**overrides: object) -> dict:
    base = {
        "name": "retrieval",
        "description": "answers one self-contained lookup",
        "system_prompt": "Answer compactly.",
        "model": "claude-sonnet-5",
        "tool_names": ["google_search"],
        "context_tokens": 32000,
        "max_turns": 8,
        "judge_prompt": "Did it answer the question?",
    }
    return base | overrides


def _tool(store: _FakeStore) -> SubagentSaveTool:
    registry = ToolRegistry()
    registry.register("google_search", lambda _deps, _cid: [])
    deps = SimpleNamespace(tools=registry, database=None)
    return SubagentSaveTool(store, deps, conversation_id=CONVERSATION)  # type: ignore[arg-type]  # fake deps


async def test_an_unknown_tool_name_is_refused_before_it_can_break_the_next_build() -> None:
    store = _FakeStore()
    result = await _tool(store).execute(**_fields(tool_names=["google_search", "telepathy"]))

    assert "telepathy" in result
    assert store.saved == []  # refused, not saved-then-broken


async def test_the_management_pair_may_not_be_handed_to_a_subagent() -> None:
    """It is registered in the shared registry, so a sub-agent config could name it — and the main
    agent delegates to sub-agents from ordinary chat. Absence from `MAIN_TOOLS` alone would leave
    that route open, and this is the surface that decides which tools and prompts every agent runs."""
    store = _FakeStore()
    result = await _tool(store).execute(**_fields(name="helper", tool_names=["subagents"]))

    assert "subagents" in result
    assert store.saved == []


async def test_an_unlisted_model_is_refused() -> None:
    """A typo'd model id fails at call time, deep inside a delegation; an expensive one silently
    multiplies the cost of every delegation that follows."""
    store = _FakeStore()
    result = await _tool(store).execute(**_fields(model="gpt-5-turbo"))

    assert "gpt-5-turbo" in result
    assert store.saved == []
    assert all(model in result for model in ALLOWED_MODELS)


async def test_a_sound_config_is_saved_whole() -> None:
    """The tool's own contract is that a save replaces the record WHOLESALE, so every field has to
    arrive intact — a dropped or defaulted prompt would silently rewrite how that agent behaves.
    Delegating to a sibling is part of the same path: that is how the researcher reaches retrieval."""
    store = _FakeStore(existing=["retrieval"])
    fields = _fields(name="researcher", tool_names=["retrieval"])

    result = await _tool(store).execute(**fields)

    (saved,) = store.saved
    assert saved.model_dump(exclude={"id", "created_at", "updated_at", "deleted_at"}) == (  # type: ignore[attr-defined]
        fields | {"conversation_id": CONVERSATION}
    )
    assert "Created" in result
