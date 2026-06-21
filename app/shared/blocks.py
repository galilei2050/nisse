"""Anthropic content-block helpers — the one place that reads a block's `type` discriminator."""


def block_type(block: object) -> str | None:
    """A content block's Anthropic `type` discriminator.

    Anthropic models content blocks as a discriminated union keyed on `type`; read that field
    uniformly, whether the block is a live SDK object (attribute) or a dict deserialized from Mongo
    or a trace (key).
    """
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
