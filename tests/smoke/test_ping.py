"""Smoke — the deploy image serves /ping, i.e. the app finished startup and is listening.

Runs only when BACKEND_URL points at an HTTP backend (the image smoke sets it to
http://localhost:8080); the polling smoke has no HTTP port, so this skips there.
"""

import os

import httpx
import pytest

BACKEND_URL = os.environ.get("BACKEND_URL")


@pytest.mark.skipif(not BACKEND_URL, reason="no HTTP backend (polling smoke has no port)")
async def test_ping():
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BACKEND_URL}/ping")

    assert response.status_code == 200
