"""SubagentTool — wraps one configured sub-agent as a delegating Tool (prompt in → result out)."""

import logging

from baski.agents import Agent, AgentConfig, GeminiJudge, InMemoryMessageHistory, Judge, Jury, ToolResult, ToolSet
from baski.agents.tool import Tool
from baski.server.logger import log_context
from pydantic import BaseModel, Field

from app.shared import CoreDeps
from app.subagents.citations import CitedJudge, OpenedPages
from app.subagents.gateway import gateway_client, is_gateway_model, price_for
from app.subagents.store import SubagentConfig

logger = logging.getLogger(__name__)


class SubagentTool(Tool):
    """Wraps one SubagentConfig as a delegating tool. Lifecycle: per-conversation, one per config.

    Each call runs a fresh, isolated Agent (its own context window, tools, model, judge) on the
    given prompt and returns its final answer — no state kept between calls.
    """

    class Input(BaseModel):
        """The task for the sub-agent, as one self-contained string."""

        prompt: str = Field(
            description=(
                "The sub-agent's task as one self-contained brief. State the GOAL, the desired "
                "OUTPUT FORMAT, and the BOUNDARIES. The sub-agent shares none of this chat's "
                "context — include every fact it needs."
            )
        )

    def __init__(
        self,
        config: SubagentConfig,
        deps: CoreDeps,
        *,
        conversation_id: int,
        siblings: dict[str, SubagentConfig],
    ) -> None:
        """Take name/description from the config (per-instance, shadowing the class defaults).

        Builds its tools through the same registry the main agent uses (`deps.tools`), scoped to
        `conversation_id`. `siblings` maps every config name in the conversation → its config, for
        resolving delegation targets — a sub-agent may delegate exactly when it HAS siblings: top-level
        tools get all configs, their children get none, so nesting is capped at one level.
        """
        self.name = config.name
        self.description = config.description
        self.one_line = config.description
        self._config = config
        self._deps = deps
        self._conversation_id = conversation_id
        self._siblings = siblings

    async def execute(self, *, prompt: str) -> ToolResult:
        """Run the isolated sub-agent once; return its answer plus its cost and trace for the parent.

        Returning a `ToolResult` (not a bare str) is what folds this run's spend into the caller's
        turn cost and links its trace under the parent's — so `total_cost` and the trace tree cover
        the whole delegation, at any depth.
        """
        opened: list[OpenedPages] = []
        with log_context(agent=self.name):  # tag every log this sub-agent (and its children) emits
            agent = Agent(self._agent_config(opened))
            agent.add_pinned_text(prompt)  # task mode — the prompt IS the child's whole request
            result = await agent.execute()
        if result.response is None:
            raise RuntimeError(f"subagent '{self.name}' produced no response (trace {result.trace_id})")
        return ToolResult(content=result.response, cost=result.total_cost, sub_trace_ids=[result.trace_id])

    def _agent_config(self, opened: list[OpenedPages]) -> AgentConfig:
        """Assemble the child's AgentConfig from its stored config; fresh history per call (stateless).

        `browse_website` is handed over wrapped, so the run's own list of opened pages exists to check
        its citations against — the tool call's arguments are the only place that set is knowable.
        """
        toolset = ToolSet()
        for name in self._config.tool_names:
            for tool in self._resolve_tools(name):
                if tool.name == "browse_website":
                    recorder = OpenedPages(tool)
                    opened.append(recorder)
                    toolset.add(recorder)
                    continue
                toolset.add(tool)
        return AgentConfig(
            toolset=toolset,
            name=self._config.name,  # `researcher`/`retrieval`/… — how this run is labelled in `traces`
            message_history=InMemoryMessageHistory(max_tokens=self._config.context_tokens),
            # An open model behind the gateway speaks the same API; only the client differs.
            anthropic_client=gateway_client() if is_gateway_model(self._config.model) else self._deps.anthropic,
            database=self._deps.database,
            bucket_name=self._deps.bucket_name,
            system_prompt=self._config.system_prompt,
            judge=self._judge(opened),
            model=self._config.model,
            price=price_for(self._config.model),
            max_turns=self._config.max_turns,
            await_trace=self._deps.await_trace,
            local_traces_dir=self._deps.local_traces_dir,
        )

    def _resolve_tools(self, name: str) -> list[Tool]:
        """Map one `tool_names` entry to live tool(s): a registry tool, or a child sub-agent to delegate to.

        A registered name is built through the shared registry `deps.tools` (e.g. `hypothesis_tree`
        yields the granular add/update pair). Otherwise it may be a sibling to delegate to — allowed
        only when this sub-agent has siblings (children have none, capping nesting at one level). A name
        that is neither a registered tool nor a delegable sibling is a seed error: fail loud.
        """
        factory = self._deps.tools.get(name)
        if factory is not None:
            return factory(self._deps, self._conversation_id)
        if self._siblings and name in self._siblings:
            return [self._child(self._siblings[name])]
        raise ValueError(
            f"subagent '{self.name}' references '{name}', which is neither a registered tool nor a "
            f"delegable sibling (delegation {'allowed' if self._siblings else 'not allowed'} here)"
        )

    def _child(self, config: SubagentConfig) -> "SubagentTool":
        """A delegated child sub-agent, built with no siblings so nesting is capped at one level."""
        return SubagentTool(config, self._deps, conversation_id=self._conversation_id, siblings={})

    def _judge(self, opened: list[OpenedPages]) -> Judge:
        """The child's completeness judge — joined by the citation check only if it can browse.

        "Cite only what you opened" is a fair demand of a web researcher and a nonsensical one for a
        worker without `browse_website`: it opens nothing, so every url it mentions would count as
        unread and every answer would be sent back. The roster decides, not the type — a worker the
        curator invents tomorrow gets this machinery exactly when it gets the tool.
        """
        graded = GeminiJudge(instructions=self._config.judge_prompt, project=self._deps.judge_project)
        return Jury([graded, CitedJudge(opened)]) if opened else graded
