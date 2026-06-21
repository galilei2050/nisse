"""Shared primitives reused across domains — no domain logic of its own."""

from .blocks import block_type
from .deps import CoreDeps

__all__ = ["CoreDeps", "block_type"]
