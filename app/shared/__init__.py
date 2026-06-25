"""Shared primitives reused across domains — no domain logic of its own."""

from .blocks import block_type
from .browser import browser_state_path
from .deps import CoreDeps

__all__ = ["CoreDeps", "block_type", "browser_state_path"]
