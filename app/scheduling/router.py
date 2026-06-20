"""HTTP fire endpoint — Cloud Tasks POSTs here when a scheduled task is due."""

from http import HTTPStatus

from baski.primitives import datetime
from fastapi import FastAPI, Request, Response

from app.scheduling.runner import ScheduleRunner


def build_fire_route(app: FastAPI, runner: ScheduleRunner) -> None:
    """Mount POST /schedule/fire → runner.fire({public_id, fire_at}).

    No app-level OIDC check: this route sits behind the same Cloud Tasks OIDC + Cloud Run ingress
    protection as baski's `/tasks/update` worker (matched convention). Verifying the token in-app
    would be a separate hardening pass for both routes.
    """

    @app.post("/schedule/fire")
    async def fire(request: Request) -> Response:
        body = await request.json()
        await runner.fire(public_id=body["public_id"], fire_at=datetime.datetime.fromisoformat(body["fire_at"]))
        return Response(status_code=HTTPStatus.OK)
