"""SubagentTool — wraps one configured sub-agent as a delegating Tool (prompt in → result out)."""

from baski.agents import Agent, AgentConfig, GeminiJudge, InMemoryMessageHistory, Judge, ToolSet
from baski.agents.tool import Tool
from baski.env import get_env
from pydantic import BaseModel, Field

from app.shared import CoreDeps
from app.subagents.hypothesis_tree import HypothesisTreeTool
from app.subagents.registry import TOOL_REGISTRY
from app.subagents.store import SubagentConfig

_JUDGE_PROJECT = str(get_env("GOOGLE_CLOUD_PROJECT"))  # read at import — fail-fast if the secret is missing


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
        siblings: dict[str, SubagentConfig],
        can_delegate: bool,
    ) -> None:
        """Take name/description from the config (per-instance, shadowing the class defaults).

        `siblings` maps every config name in the conversation → its config, for resolving delegation
        targets. `can_delegate` gates whether this sub-agent may resolve a sibling name in its
        `tool_names` into a child sub-agent — top-level sub-agents get True, their children False, so
        nesting is capped at one level (children are also built with empty `siblings`).
        """
        self.name = config.name
        self.description = config.description
        self.one_line = config.description
        self._config = config
        self._deps = deps
        self._siblings = siblings
        self._can_delegate = can_delegate

    async def execute(self, *, prompt: str) -> str:
        """Run the isolated sub-agent once and return its answer."""
        agent = Agent(self._agent_config())
        agent.add_pinned_text(prompt)  # task mode — the prompt IS the child's whole request
        result = await agent.execute()
        if result.response is None:
            raise RuntimeError(f"subagent '{self.name}' produced no response (trace {result.trace_id})")
        return result.response

    def _agent_config(self) -> AgentConfig:
        """Assemble the child's AgentConfig from its stored config; fresh history per call (stateless)."""
        toolset = ToolSet()
        for name in self._config.tool_names:
            toolset.add(self._resolve_tool(name))
        return AgentConfig(
            toolset=toolset,
            message_history=InMemoryMessageHistory(max_tokens=self._config.context_tokens),
            anthropic_client=self._deps.anthropic,
            database=self._deps.database,
            bucket_name=self._deps.bucket_name,
            system_prompt=self._config.system_prompt,
            judge=self._judge(),
            model=self._config.model,
        )

    def _resolve_tool(self, name: str) -> Tool:
        """Map one `tool_names` entry to a live tool: web leaf, hypothesis tree, or a child sub-agent.

        A child sub-agent is built as a leaf — empty siblings + `can_delegate=False` — so nesting is
        capped at one level. A name that resolves to nothing (or a sibling this level can't delegate
        to) is a seed error: fail loud, like the registry whitelist.
        """
        if name in TOOL_REGISTRY:
            return TOOL_REGISTRY[name](self._deps)
        if name == "hypothesis_tree":
            return HypothesisTreeTool()  # fresh per run = ephemeral per investigation
        if self._can_delegate and name in self._siblings:
            return SubagentTool(self._siblings[name], self._deps, siblings={}, can_delegate=False)
        raise ValueError(
            f"subagent '{self.name}' references unknown tool '{name}' "
            f"(delegation {'allowed' if self._can_delegate else 'not allowed'} here)"
        )

    def _judge(self) -> Judge:
        """The child's own completeness judge, graded against its config's rubric."""
        return GeminiJudge(instructions=self._config.judge_prompt, project=_JUDGE_PROJECT)
