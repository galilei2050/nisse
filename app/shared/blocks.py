"""Anthropic content-block helpers — read a block's `type`; carry a user attachment (photo/PDF)."""

from enum import StrEnum
from typing import NamedTuple


class MediaType(StrEnum):
    """A media type Anthropic reads natively as a user attachment (images + PDF)."""

    JPEG = "image/jpeg"
    PNG = "image/png"
    GIF = "image/gif"
    WEBP = "image/webp"
    PDF = "application/pdf"


class Media(NamedTuple):
    """A photo or PDF the user attached: base64-encoded data and its media type."""

    data: str
    media_type: MediaType


def block_field(block: object, name: str) -> object | None:
    """One field of a content block, read the same way whichever shape the block is in.

    A block is a live SDK object while the reply runs and a plain dict once it has been through Mongo
    or a trace file, so every read has to work on both.
    """
    return block.get(name) if isinstance(block, dict) else getattr(block, name, None)


def block_type(block: object) -> str | None:
    """A content block's Anthropic `type` discriminator — the union's key."""
    value = block_field(block, "type")
    return value if isinstance(value, str) else None
