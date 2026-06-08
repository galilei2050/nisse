"""Assistant — composition root that turns a user message into an agent reply."""

from baski.agents import Agent, AgentConfig

NISSE_SYSTEM_PROMPT = (
    "You are Nisse, a personal AI assistant for a single owner. Be concise, direct, and "
    "helpful. When a question needs current or external information, use your tools to look "
    "it up, then answer in plain language."
)

_NO_ANSWER = "I couldn't produce a response — please try rephrasing."


class Assistant:
    """Turns a text message into a reply by running a fresh, stateless agent per call."""

    def __init__(self, *, config: AgentConfig, system_prompt: str = NISSE_SYSTEM_PROMPT) -> None:
        """Store the agent config and system prompt reused for every reply."""
        self._config = config
        self._system_prompt = system_prompt

    async def reply(self, *, text: str) -> str:
        """Run the agent on one message and return its final text (stateless per call)."""
        agent = Agent(config=self._config, system=self._system_prompt)
        result = await agent.execute(text)
        return result.response or _NO_ANSWER
