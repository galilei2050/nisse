"""Assistant module — the main agent composition root."""

from .assistant import Assistant
from .judge_prompt import NISSE_JUDGE_PROMPT

__all__ = ["NISSE_JUDGE_PROMPT", "Assistant"]
