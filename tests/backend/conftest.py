import sys

import pytest

from app.backend import NisseBot


@pytest.fixture
def bot():
    """Construct NisseBot with a clean argv so argparse ignores pytest's flags.

    Env (TELEGRAM_TOKEN, WEBHOOK_URL) is provisioned by the runner — the CI job or
    the local Makefile/.env — never by the tests themselves.
    """
    old_argv = sys.argv
    sys.argv = ["app.backend"]
    try:
        yield NisseBot()
    finally:
        sys.argv = old_argv
