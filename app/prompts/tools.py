"""The owner-preference tool: injects the standing-instructions block and overwrites it.

Mirrors `RecallMemoryTool` (which owns the memory index): one tool both *injects* its content into
the system prompt every turn — via `system_prompt()`, now async/per-turn — and *edits* it. The
content is the owner's standing rules: how to behave, how to address them, conventions, identity
facts that shape behaviour. It is loaded into the system every turn, so the model always sees it
and can rewrite it wholesale.
"""

from baski.agents.tool import Tool
from pydantic import BaseModel, Field

from app.prompts.store import PromptStore, PromptType

_PREFERENCE_HEADER = (
    "OWNER PREFERENCES — standing instructions about who the owner is and how you must behave. "
    "Always follow them. When the owner states or corrects a durable preference, update them with "
    "update_preferences."
)


class PreferenceTool(Tool):
    """Maintain the owner-preference prompt injected into the system every turn. Lifecycle: per-conversation."""

    name = "update_preferences"
    one_line = "Record/adjust how the owner wants you to behave"
    description = (
        "Persist a standing instruction about how to behave — address form, formatting conventions, "
        "identity facts that shape behaviour ('don't do X', 'always do Y', 'call me Z'). The current "
        "preferences are shown to you each turn; pass the FULL updated text, which overwrites the "
        "previous version — preserve the existing rules and amend, never drop them. For a discrete "
        "fact or dated event use remember instead."
    )

    class Input(BaseModel):
        """Argument for overwriting the owner-preference prompt."""

        content: str = Field(description="The full owner-preference text; replaces the previous version wholesale")

    def __init__(self, store: PromptStore) -> None:
        """Hold the prompt store the preference is read from and written to."""
        self._store = store

    async def execute(self, *, content: str) -> str:
        """Overwrite the owner-preference prompt; it applies from the next turn on."""
        await self._store.set(PromptType.USER_PREFERENCE, content)
        return "Preferences updated."

    async def system_prompt(self) -> str:
        """The owner-preference block, read live and injected into the system prompt every turn."""
        content = await self._store.get(PromptType.USER_PREFERENCE)
        return f"{_PREFERENCE_HEADER}\n\n{content}" if content else ""
