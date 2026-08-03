"""Record the owner's emoji reactions — the `message_reaction` update, written straight to Mongo.

Telegram delivers this update only when `message_reaction` is in the webhook's `allowed_updates`;
both modes derive that list from the registered handlers (aiogram's `resolve_used_update_types`,
which baski also uses to set the webhook), so registering the handler below is the whole wiring.

The Bot API says reactions need the bot to be a chat administrator, which a 1:1 chat has no concept
of — but private chats do deliver the update; the admin rule is a groups-and-channels rule.
"""

import logging

from aiogram import Router
from aiogram.types import (
    MessageReactionUpdated,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    ReactionTypeUnion,
)
from pymongo.asynchronous.database import AsyncDatabase

from app.access import is_allowed
from app.reactions import ReactionStore

logger = logging.getLogger(__name__)


def _labels(reactions: list[ReactionTypeUnion]) -> list[str]:
    """Reactions as plain strings: the emoji itself, or `custom:<id>` for a premium custom emoji.

    Anything else Telegram introduces (paid reactions today) keeps its API type name rather than
    being dropped — this store is a raw record, so an unknown kind must still leave a trace.
    """
    labels = []
    for reaction in reactions:
        if isinstance(reaction, ReactionTypeEmoji):
            labels.append(reaction.emoji)
        elif isinstance(reaction, ReactionTypeCustomEmoji):
            labels.append(f"custom:{reaction.custom_emoji_id}")
        else:
            labels.append(reaction.type)
    return labels


class ReactionRecorder:
    """Records the owner's reaction changes and drops everyone else's.

    Lifecycle: long-lived (one per bot); the chat comes from each update, not from construction.
    """

    def __init__(self, database: AsyncDatabase) -> None:
        """Hold the database the per-chat store is opened on, per update."""
        self._database = database

    def register(self, router: Router) -> None:
        """Wire the reaction handler; this registration is what puts `message_reaction` on the wire."""
        router.message_reaction.register(self.record)

    async def record(self, reaction: MessageReactionUpdated) -> None:
        """Append one reaction change, ignoring anyone but the owner.

        `AllowlistMiddleware` guards `message` only, so this update type reaches the handler
        ungated — and anyone can DM the bot and react to its refusal. `user` is absent when the
        reactor is anonymous (a channel or an anonymous group admin), which is not the owner either.
        """
        user = reaction.user
        username = user.username if user else None
        if user is None or username is None or not is_allowed(username):
            return
        current = _labels(reaction.new_reaction)
        store = ReactionStore(self._database, conversation_id=reaction.chat.id)
        await store.record(
            message_id=reaction.message_id,
            user_id=user.id,
            username=username,
            previous=_labels(reaction.old_reaction),
            current=current,
            reacted_at=reaction.date,
        )
        logger.info("Reaction recorded", extra={"messageId": reaction.message_id, "current": current})
