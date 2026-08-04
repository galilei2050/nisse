"""Index creation that tolerates a Mongo role lacking createIndex rights."""

import logging
from typing import Any

from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import OperationFailure

logger = logging.getLogger(__name__)


async def ensure_index(collection: AsyncCollection[Any], keys: Any, **kwargs: Any) -> None:  # noqa: ANN401, ANON003 — forwards pymongo's polymorphic create_index signature; a wrapper over one pymongo call, called from each store's own ensure_indexes
    """Create an index, but don't let a Mongo user without createIndex rights block startup.

    Exception to the fail-fast rule: indexes are a query optimization, not a correctness
    requirement. A restricted role (e.g. a CI or read-mostly user) that can't create them
    should still boot and serve — so an authorization failure on createIndex is logged and
    swallowed. Any other OperationFailure still raises.
    """
    try:
        await collection.create_index(keys, **kwargs)
    except OperationFailure as exc:
        if "not allowed to do action [createIndex]" not in str(exc):
            raise
        logger.warning("createIndex not permitted; skipping index", extra={"collection": collection.name})
