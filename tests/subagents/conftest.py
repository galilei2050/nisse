"""No fixtures here on purpose: these tests read secrets from the real environment, not from fakes.

`make` loads `.env` (via `include .env`); CI provides them through the job's `env:`. Run as
`make test-backend` or `uv run --env-file .env pytest tests/subagents/`. Never hardcode secret values
here — that tests a vacuum, not reality. See tests/CLAUDE.md.
"""
