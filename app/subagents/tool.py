"""SubagentTool — wraps one configured sub-agent as a delegating Tool (prompt in → result out)."""

from baski.agents import Agent, AgentConfig, GeminiJudge, InMemoryMessageHistory, Judge, ToolSet
from baski.agents.tool import Tool
from baski.env import get_env
from pydantic import BaseModel, Field

from app.shared import CoreDeps
from app.subagents.registry import build_tools
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

    def __init__(self, config: SubagentConfig, deps: CoreDeps) -> None:
        """Take name/description from the config (per-instance, shadowing the class defaults)."""
        self.name = config.name
        self.description = config.description
        self.one_line = config.description
        self._config = config
        self._deps = deps

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
        for tool in build_tools(self._config.tool_names, self._deps):
            toolset.add(tool)
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

    def _judge(self) -> Judge:
        """The child's own completeness judge, graded against its config's rubric."""
        return GeminiJudge(instructions=self._config.judge_prompt, project=_JUDGE_PROJECT)
