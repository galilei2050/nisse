"""Sub-agent management as tools — read the roster, write one config.

The roster is editable at runtime through these two tools, which is what lets the curator act on what
it learned overnight instead of only reporting it. `agents.yml` plus `make seed` remains the other
writer — the file is still the source of truth for the roster's shape.

**This is a trusted admin surface** (`app/subagents/CLAUDE.md`): a config decides which tools, which
model, and which prompt a child agent runs with. Only the curator gets these tools — they are
deliberately absent from `MAIN_TOOLS`, so nothing the owner types in chat reaches this write path.
Two guards make a bad write loud rather than silent: `tool_names` is checked against the live
registry (an unknown name would otherwise only explode at the next conversation build), and `model`
is checked against the models this project actually runs.
"""

import logging

from baski.agents.tool import Tool
from pydantic import BaseModel, Field

from app.shared import CoreDeps
from app.subagents.store import SubagentConfig, SubagentStore
from app.tools.registry import ToolRegistrar

logger = logging.getLogger(__name__)

# A child agent's model is a cost and quality decision, not a free-text field: a typo'd id fails at
# call time, and an expensive id silently multiplies the bill of every delegation.
ALLOWED_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001")

# The registry name this pair is registered under. Absence from `MAIN_TOOLS` keeps it away from the
# main agent; refusing it in `tool_names` keeps it away from sub-agents, which the main agent CAN
# reach. Both are needed — the registry resolves any registered name for a sub-agent config.
MANAGEMENT_TOOL_NAME = "subagents"


class SubagentListTool(Tool):
    """Reads the conversation's sub-agent roster in full. Lifecycle: per-conversation."""

    name = "subagent_list"
    one_line = "Show the configured sub-agents with their full prompts and tools"
    description = (
        "Read the conversation's sub-agents in full — name, description, model, tool_names, "
        "system_prompt, judge_prompt. Read before editing one: subagent_save replaces a config "
        "wholesale, so you need the current text to change one part of it."
    )

    class Input(BaseModel):
        """No arguments — the roster is always the whole conversation's."""

    def __init__(self, store: SubagentStore) -> None:
        """Bind to one conversation's sub-agent store."""
        self._store = store

    async def execute(self) -> str:
        """Render every configured sub-agent, full text — the curator edits from this, not from memory."""
        configs = await self._store.list()
        if not configs:
            return "No sub-agents are configured in this conversation."
        return "\n\n".join(_render(config) for config in configs)


class SubagentSaveTool(Tool):
    """Creates or replaces one sub-agent config. Lifecycle: per-conversation."""

    name = "subagent_save"
    one_line = "Create or update one sub-agent"
    description = (
        "Create a sub-agent, or replace an existing one by name. Every field is required and the "
        "config is replaced WHOLESALE — call subagent_list first and resend the fields you are not "
        "changing, or you will silently drop them. The previous version is kept in the change "
        "history. Create one only when a task class genuinely needs its own context and toolset; a "
        "single tool call is cheaper and more reliable than a delegation."
    )

    class Input(BaseModel):
        """One complete sub-agent config — every axis, because the save replaces the whole record."""

        name: str = Field(description="Agent-facing key, unique per conversation; becomes the tool name.")
        description: str = Field(
            description="What the parent reads to decide when to delegate. This is what actually routes work."
        )
        system_prompt: str = Field(
            description=(
                "The child's system prompt. It MUST demand a compressed, structured answer — the "
                "child's output re-enters the parent's limited context."
            )
        )
        model: str = Field(description=f"One of: {', '.join(ALLOWED_MODELS)}.")
        tool_names: list[str] = Field(
            description="Registry tool names, and/or sibling sub-agent names to delegate to (one level deep)."
        )
        context_tokens: int = Field(description="The child's history budget for one run, e.g. 32000.")
        max_turns: int = Field(description="Hard cap on the child's loop, e.g. 12.")
        judge_prompt: str = Field(description="The child's completeness rubric, graded by its own judge.")

    def __init__(self, store: SubagentStore, deps: CoreDeps, *, conversation_id: int) -> None:
        """Bind to the store, the deps whose registry validates `tool_names`, and the chat scope."""
        self._store = store
        self._deps = deps
        self._conversation_id = conversation_id

    async def execute(self, **fields: object) -> str:
        """Validate the config against the live registry, then save it, keeping the prior version."""
        config = SubagentConfig(conversation_id=self._conversation_id, **fields)  # type: ignore[arg-type]  # fields validated by Input
        rejection = await self._reject(config)
        if rejection:
            return rejection
        existing = await self._store.get(config.name)
        await self._store.save(config)
        logger.info("Sub-agent saved", extra={"subagent": config.name, "created": existing is None})
        verb = "Updated" if existing else "Created"
        return (
            f"{verb} sub-agent '{config.name}' ({config.model}, tools: {', '.join(config.tool_names)}). "
            "It takes effect on the next process start — the conversation's agent is built once and cached."
        )

    async def _reject(self, config: SubagentConfig) -> str | None:
        """The reason this config must not be saved, or None when it is sound.

        A `tool_names` entry is valid as a registered tool OR as a sibling sub-agent to delegate to;
        catching an unknown one here beats letting it raise at the next conversation build, where it
        would take the whole chat down rather than one tool call.
        """
        if config.model not in ALLOWED_MODELS:
            return f"Rejected: model '{config.model}' is not one of {', '.join(ALLOWED_MODELS)}."
        if MANAGEMENT_TOOL_NAME in config.tool_names:
            # Otherwise the curator could hand this write surface to a sub-agent, which the main
            # agent delegates to from ordinary chat — routing around "curator-only" entirely.
            return f"Rejected: '{MANAGEMENT_TOOL_NAME}' may not be given to a sub-agent."
        siblings = {existing.name for existing in await self._store.list()} | {config.name}
        unknown = [n for n in config.tool_names if self._deps.tools.get(n) is None and n not in siblings]
        if unknown:
            return (
                f"Rejected: tool_names {unknown} are neither registered tools nor sub-agents in this "
                "conversation. Call subagent_list to see what exists."
            )
        return None


def _render(config: SubagentConfig) -> str:
    """One config as readable text — full prompts, since the curator edits them verbatim."""
    return (
        f"### {config.name}\n"
        f"description: {config.description}\n"
        f"model: {config.model} · context_tokens: {config.context_tokens} · max_turns: {config.max_turns}\n"
        f"tool_names: {', '.join(config.tool_names)}\n"
        f"system_prompt:\n{config.system_prompt}\n"
        f"judge_prompt:\n{config.judge_prompt}"
    )


def build_subagent_tools(deps: CoreDeps, conversation_id: int) -> list[Tool]:
    """The read + write pair over one conversation's sub-agent roster (curator-only)."""
    store = SubagentStore(deps.database, conversation_id=conversation_id)
    return [SubagentListTool(store), SubagentSaveTool(store, deps, conversation_id=conversation_id)]


def register_management_tools(registrar: ToolRegistrar) -> None:
    """Register the management pair. Deliberately NOT in `MAIN_TOOLS`, and refused in `tool_names`."""
    registrar.register(MANAGEMENT_TOOL_NAME, build_subagent_tools)
