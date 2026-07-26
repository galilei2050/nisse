"""Anthropic content-block helpers — read a block's `type`; carry a user attachment (photo/PDF)."""

from typing import NamedTuple


class Media(NamedTuple):
    """A photo or PDF the user attached: base64-encoded data and its media type."""

    data: str
    media_type: str


def block_type(block: object) -> str | None:
    """A content block's Anthropic `type` discriminator.

    Anthropic models content blocks as a discriminated union keyed on `type`; read that field
    uniformly, whether the block is a live SDK object (attribute) or a dict deserialized from Mongo
    or a trace (key).
    """
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
