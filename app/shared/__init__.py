"""Shared primitives reused across domains — no domain logic of its own."""

from .blocks import block_type
from .deps import CoreDeps
from .sending import MessageSender

__all__ = ["CoreDeps", "MessageSender", "block_type"]
