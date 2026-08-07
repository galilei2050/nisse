"""HTTP fire endpoint — Cloud Tasks POSTs here when a scheduled task is due."""

from http import HTTPStatus

from baski.primitives import datetime
from fastapi import FastAPI, Request, Response

from app.scheduling.runner import ScheduleRunner


def build_fire_route(app: FastAPI, runner: ScheduleRunner) -> None:
    """Mount the two fire paths: one occurrence from the queue, and the sweep that repairs the rest.

    `/schedule/fire` is Cloud Tasks delivering one due occurrence; `/schedule/sweep` is Cloud
    Scheduler asking for everything the queue never delivered.

    No app-level OIDC check: these routes sit behind the same Cloud Tasks OIDC + Cloud Run ingress
    protection as baski's `/tasks/update` worker (matched convention). Verifying the token in-app
    would be a separate hardening pass for all of them.
    """

    @app.post("/schedule/fire")
    async def fire(request: Request) -> Response:
        body = await request.json()
        await runner.fire(public_id=body["public_id"], fire_at=datetime.datetime.fromisoformat(body["fire_at"]))
        return Response(status_code=HTTPStatus.OK)

    @app.post("/schedule/sweep")
    async def sweep() -> Response:
        # Inline, like `fire`: Cloud Run bills and schedules CPU per request, so work handed to a
        # background task can be frozen the moment the response returns.
        await runner.sweep()
        return Response(status_code=HTTPStatus.OK)
