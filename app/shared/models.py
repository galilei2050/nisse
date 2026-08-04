"""Pydantic bases for data flowing between functions and into Mongo."""

import secrets

from baski.primitives import datetime
from bson import ObjectId
from pydantic import AliasChoices, BaseModel, Field, field_validator


class NisseDbModel(BaseModel):
    """Base for top-level Mongo documents: `_id` ↔ `id` mapping plus audit timestamps.

    `id` is the durable DB key (Mongo `ObjectId` as str), distinct from any agent-facing
    id a subclass adds. `created_at`/`updated_at` are set on creation; `deleted_at` is the
    soft-delete marker — `None` means live. Records are never hard-deleted: set `deleted_at`
    and filter `{"deleted_at": None}` on read. On insert, `model_dump(exclude={"id"})` so
    Mongo assigns `_id`; then set `id = str(result.inserted_id)`.
    """

    id: str | None = Field(default=None, validation_alias=AliasChoices("_id", "id"))  # None before insert
    created_at: datetime.datetime = Field(default_factory=datetime.now)
    updated_at: datetime.datetime = Field(default_factory=datetime.now)
    deleted_at: datetime.datetime | None = None  # None => live; a timestamp once soft-deleted

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_object_id(cls, v: object) -> object:
        """Coerce a BSON ObjectId to str before validation."""
        if isinstance(v, ObjectId):
            return str(v)
        return v


class PublicIdModel(NisseDbModel):
    """A record the agent addresses by a short id of its own, not by the Mongo `_id`.

    Lifecycle: a data record — one Mongo document. Ten hex characters: short enough that the model
    copies it back without a typo, wide enough that the unique index never has to reject a collision.
    `public_id` is never reused — a forgotten record keeps its id, so a stale reference resolves to
    nothing rather than to someone else's row.
    """

    public_id: str = Field(default_factory=lambda: secrets.token_hex(5))
