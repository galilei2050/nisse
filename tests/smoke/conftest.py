import os

import httpx
import pytest_asyncio

# Where the live backend is. Provisioned by the runner (Makefile / CI), never by
# the tests: a local `python -m app.backend --cloud` boot, or the deployed
# Cloud Run URL for post-deploy smoke.
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8080")


@pytest_asyncio.fixture
async def smoke_client():
    """Async HTTP client targeting the live backend at BACKEND_URL."""
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=10.0) as client:
        yield client
