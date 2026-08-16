"""Sub-agent management as tools — read the roster, write one config, retire one.

The roster is editable at runtime through these three tools, which is what lets the curator act on
what it learned overnight instead of only reporting it. `agents.yml` plus `make seed` remains the other
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

from app.prompts import JUDGE_RULES_TOOL_NAME
from app.shared import CoreDeps
from app.subagents.store import SubagentConfig, SubagentStore
from app.tools.registry import ToolRegistrar

logger = logging.getLogger(__name__)

# A child agent's model is a cost and quality decision, not a free-text field: a typo'd id fails at
# call time, and an expensive id silently multiplies the bill of every delegation.
ALLOWED_MODELS = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
    # Open weights through the gateway (app/subagents/gateway.py). Priced there, and only ids listed
    # in GATEWAY_PRICES are billable — an unlisted one would raise before the run spends anything.
    "moonshotai/kimi-k2-thinking",
)

# The registry name this pair is registered under. Absence from `MAIN_TOOLS` keeps it away from the
# main agent; refusing it in `tool_names` keeps it away from sub-agents, which the main agent CAN
# reach. Both are needed — the registry resolves any registered name for a sub-agent config.
MANAGEMENT_TOOL_NAME = "subagents"

# Every tool only the nightly pass may hold, and none of them may travel to a sub-agent, which
# ordinary chat reaches by delegation. Two of them write what the assistant does with EVERY later
# reply — its roster, or the rubric its answers are accepted by. The third reads: `transcript` hands
# back turns the window deliberately left out, which is a maintenance question, and a worker holding
# it could pull any past exchange into a delegated answer.
# `transcript` is spelled out rather than imported from `app.curator` for the same reason `ask_user`
# is below: `app.curator` imports this module's package, so importing it back cycles. Both are
# covered by `tests/subagents/test_registry.py` asserting every name here resolves in the registry.
CURATOR_ONLY_TOOLS = (MANAGEMENT_TOOL_NAME, JUDGE_RULES_TOOL_NAME, "transcript")

# Registry names that must never end up in a `tool_names`, read by BOTH the offer (`_catalogue`) and the
# refusal (`_reject`) so the shelf cannot advertise something the save then rejects — the curator's whole
# job here is picking names off that shelf.
# `ask_user` is here for a different reason than the curator-only pair: it blocks on the owner tapping a
# button (`app/chat/ask.py`, 300s). A worker holding it under a scheduled fire waits out the timeout with
# nobody looking at the screen, and answers "the user did not answer".
# `ask_user` is spelled out rather than imported from `app.chat.ask` (where `ASK_USER_TOOL_NAME` is
# declared): that import cycles — `app.chat` pulls `app.chat.curate`, which pulls `app.curator`, which
# reaches back here, so importing `app.curator` first fails outright. The drift risk is real and covered by
# a test instead: `tests/subagents/test_registry.py` asserts every name here resolves in the live registry.
NOT_GRANTABLE = (*CURATOR_ONLY_TOOLS, "ask_user")


class SubagentListTool(Tool):
    """Reads the conversation's sub-agent roster in full. Lifecycle: per-conversation."""

    name = "subagent_list"
    one_line = "Show the configured sub-agents in full, and the tools you may grant them"
    description = (
        "Read the conversation's sub-agents in full — name, description, model, tool_names, "
        "system_prompt, judge_prompt — then the tools you may grant. Read before editing one: "
        "subagent_save replaces a config wholesale, so you need the current text to change one part."
    )

    class Input(BaseModel):
        """No arguments — the roster is always the whole conversation's."""

    def __init__(self, store: SubagentStore, deps: CoreDeps, *, conversation_id: int) -> None:
        """Bind to one conversation's sub-agent store, plus the registry the catalogue is read from."""
        self._store = store
        self._deps = deps
        self._conversation_id = conversation_id

    async def execute(self) -> str:
        """The roster, then the catalogue — the curator edits from this, not from memory."""
        configs = await self._store.list()
        roster = (
            "\n\n".join(self._render(config) for config in configs)
            if configs
            else "No sub-agents are configured in this conversation."
        )
        return f"{roster}\n\n{self._catalogue()}"

    def _catalogue(self) -> str:
        """Every grantable tool name with what it does — so a missing capability is visible as a gap.

        Without this the roster shows only the names a worker ALREADY has, and a worker that cannot do
        something looks identical to one that can: the reader recognises `browse_website` as "the web"
        and concludes the capability was present. Naming what is on the shelf is what makes "this
        worker is missing a tool" a conclusion the evidence can support.
        """
        catalog = self._deps.tools.catalog(self._deps, self._conversation_id)
        lines = [
            "### Registered tools you may grant (any of these names may go in tool_names)",
            "A worker gets a capability ONLY if its tool_names lists the name. If the work the owner "
            "asked for needs something no listed tool of that worker does, the fix is this list, not "
            "the prompt. A tool_names entry may ALSO be the name of one of the workers above — that is "
            "how a worker delegates — so this list is what is registered, not everything you may name.",
        ]
        for name in sorted(catalog):
            if name in NOT_GRANTABLE:
                continue  # `_reject` refuses these; offering one would only invite a rejected save
            lines.append(f"- {name}: {' · '.join(catalog[name])}")
        return "\n".join(lines)

    @staticmethod
    def _render(config: SubagentConfig) -> str:
        """One config as this tool's result text — full prompts, since the curator edits them verbatim.

        Kept here rather than on `SubagentConfig`: the shape is this tool's output contract, not the
        record's, and a second reader would want a different one.
        """
        return (
            f"### {config.name}\n"
            f"description: {config.description}\n"
            f"model: {config.model} · context_tokens: {config.context_tokens} · max_turns: {config.max_turns}\n"
            f"tool_names: {', '.join(config.tool_names)}\n"
            f"system_prompt:\n{config.system_prompt}\n"
            f"judge_prompt:\n{config.judge_prompt}"
        )


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
        # NOT `created` — LogRecord owns that name, and the clash raises AFTER the config is written
        logger.info("Sub-agent saved", extra={"subagent": config.name, "isNew": existing is None})
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
        if self._deps.tools.get(config.name) is not None:
            # A worker's name becomes its delegating tool's name, and `Conversations._build` adds the
            # sub-agent tools into the ToolSet AFTER the registry ones — keyed by name, so a worker called
            # `ask_user` REPLACES the real tool and every clarifying question silently runs an agent
            # instead of reaching the owner. Refusing `tool_names` while leaving this field open closes one
            # door of the same room.
            return f"Rejected: '{config.name}' is already a registered tool name — a worker may not shadow one."
        refused = [name for name in NOT_GRANTABLE if name in config.tool_names]
        if refused:
            # Two reasons, one list (see NOT_GRANTABLE): a write surface handed to a sub-agent is
            # reachable from ordinary chat by delegation, routing around "curator-only" entirely; and a
            # tool that blocks on the owner tapping a button strands any worker a schedule drives.
            return f"Rejected: {refused} may not be given to a sub-agent."
        siblings = {existing.name for existing in await self._store.list()} | {config.name}
        unknown = [n for n in config.tool_names if self._deps.tools.get(n) is None and n not in siblings]
        if unknown:
            return (
                f"Rejected: tool_names {unknown} are neither registered tools nor sub-agents in this "
                "conversation. Call subagent_list to see what exists."
            )
        return None


class SubagentForgetTool(Tool):
    """Retires one sub-agent so it stops being a routing target. Lifecycle: per-conversation."""

    name = "subagent_forget"
    one_line = "Retire one sub-agent the owner has rejected"
    description = (
        "Retire a sub-agent by name so the parent stops seeing it. Use it when the owner has "
        "rejected a worker outright, or when the job it was built for turned out not to be a job — "
        "a worker nobody should route to is a wrong routing target, and rewording its description "
        "leaves it in the roster. Refused while another worker delegates to it. Saving the same "
        "name again brings the worker back, but you cannot re-read the retired config: its text "
        "survives only in the change history, which the owner reads and you have no tool for."
    )

    class Input(BaseModel):
        """The worker to retire, by the name `subagent_list` prints."""

        name: str = Field(description="The sub-agent's name, exactly as subagent_list shows it.")

    def __init__(self, store: SubagentStore) -> None:
        """Bind to one conversation's sub-agent store — retiring needs no registry lookup."""
        self._store = store

    async def execute(self, name: str) -> str:
        """Retire it, or say why not — a silent no-op would read as a change in the report."""
        dependents = [config.name for config in await self._store.list() if name in config.tool_names]
        if dependents:
            # The mirror of SubagentSaveTool._reject: a `tool_names` entry naming a worker that is
            # gone raises in `_resolve_tools` — and that runs inside `execute`, so it surfaces as a
            # failed delegation in a live turn hours later, not at the next build.
            return f"Rejected: {dependents} delegate to '{name}' — re-save them without it first."
        if not await self._store.soft_delete(name):
            return f"No live sub-agent named '{name}' in this conversation — nothing retired."
        logger.info("Sub-agent retired", extra={"subagent": name})
        return (
            f"Retired sub-agent '{name}'. It takes effect on the next process start — the "
            "conversation's agent is built once and cached."
        )


def build_subagent_tools(deps: CoreDeps, conversation_id: int) -> list[Tool]:
    """The read + write trio over one conversation's sub-agent roster (curator-only)."""
    store = SubagentStore(deps.database, conversation_id=conversation_id)
    return [
        SubagentListTool(store, deps, conversation_id=conversation_id),
        SubagentSaveTool(store, deps, conversation_id=conversation_id),
        SubagentForgetTool(store),
    ]


def register_management_tools(registrar: ToolRegistrar) -> None:
    """Register the management trio. Deliberately NOT in `MAIN_TOOLS`, and refused in `tool_names`."""
    registrar.register(MANAGEMENT_TOOL_NAME, build_subagent_tools)
