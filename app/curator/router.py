"""HTTP trigger — Cloud Scheduler POSTs here nightly to run the maintenance pass.

Same protection as the scheduling worker (`app/scheduling/router.py`): Cloud Tasks/Scheduler OIDC
plus Cloud Run ingress, no app-level token check — matched convention, not an oversight.
"""

from http import HTTPStatus

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from app.curator.curator import Curator


class CurateRequest(BaseModel):
    """The POST body.

    Cloud Scheduler sends none, which means "every active conversation"; a manual call may name one
    chat to review.
    """

    conversation_id: int | None = None


def build_curate_route(app: FastAPI, curator: Curator) -> None:
    """Mount POST /curate — every active conversation, or the one named in the body."""

    @app.post("/curate")
    async def curate(request: Request) -> Response:
        body = await _request(request)
        conversation_ids = (
            [body.conversation_id] if body.conversation_id is not None else await curator.active_conversations()
        )
        for conversation_id in conversation_ids:
            await curator.curate(conversation_id=conversation_id)
        return Response(status_code=HTTPStatus.OK)


async def _request(request: Request) -> CurateRequest:
    """Parse the body, treating an empty one as "sweep everything" (what Cloud Scheduler sends)."""
    raw = await request.body()
    return CurateRequest.model_validate(await request.json()) if raw else CurateRequest()
