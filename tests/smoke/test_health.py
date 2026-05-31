"""Smoke tests — hit a real, running backend (webhook mode exposes GET /ping, /).

These FAIL unless a backend is actually serving at BACKEND_URL. Run with
`make smoke-test` against a local `--cloud` boot or the deployed Cloud Run URL.
"""


async def test_ping_returns_ok(smoke_client):
    resp = await smoke_client.get("/ping")

    assert resp.status_code == 200
    assert resp.json() == "OK"


async def test_root_returns_ok(smoke_client):
    resp = await smoke_client.get("/")

    assert resp.status_code == 200
    assert resp.json() == "OK"
