# tests/ — how tests get their environment, and what to test

## The environment provides the config — never fake it

Tests read secrets and config from the **real environment**, exactly like the app does. They do NOT
hardcode dummy values in a `conftest.py` or `monkeypatch.setenv(...)` — a test running against
`GOOGLE_CLOUD_PROJECT="test-project"` proves nothing about reality.

Where the env comes from:
- **Locally:** the `Makefile` does `include .env` (top of the file), so every `make` target's
  `uv run …` inherits the real `.env`. Run tests with **`make test-backend`** (or the full
  `make test`).
- **Directly:** `uv run --env-file .env pytest tests/…`. A bare `uv run pytest` has **no** `.env`, so
  any module that reads env **at import** (fail-fast, e.g. `app.subagents` reads
  `GOOGLE_CLOUD_PROJECT`) will fail to import — that's the convention working, not a bug. Load `.env`.
- **CI:** each job supplies what its code reads at import through the job's `env:` block, sourced from
  repo `vars`/`secrets` (see `.github/workflows/test-app.yml`). If you add tests that import an
  env-reading module, add the vars that module needs to that job's `env:`.

## Test close to reality, not in a vacuum

Prefer exercising the real path: real env, real deps, the end-to-end **probe** (`app/probe.py`,
`make probe`) which drives the actual agent against real API + Mongo. Unit tests are for **pure
logic** that has a real, faking-free contract — a validation guard, a renderer, an index formatter
(see `tests/memory`, `tests/lists`). Do NOT stand up a stub of a live client just to assert against
it; that tests the stub. When the thing under test needs a browser / search key / model, verify it
through the probe, not a mock.

Example: `tests/subagents/test_registry.py` unit-tests only the "unknown tool name → loud raise"
guard (pure, no deps). Whether a sub-agent actually runs with its configured tools is checked by the
probe (`docs/subagents-test-cases.md`), against real deps.

## Layout

Unit tests mirror the `app/` module they cover (`tests/memory`, `tests/lists`, `tests/assistant`,
`tests/backend`, `tests/subagents`, `tests/curator`, `tests/scheduling`). `tests/smoke/` boots the
real deploy image and checks it serves. The CI `test` job runs everything but `tests/smoke/`
(`make test-backend`); the `smoke` job runs `tests/smoke/` with live secrets. A new unit dir needs no
wiring — collection is by discovery, since an enumerated list is a directory silently never run.

**Expectation-first (`app/CLAUDE.md`):** each feature has a `docs/*-test-cases.md`; write the
expectation before you run, and re-run related cases to catch regressions. A task isn't done until the
new scenarios are written AND run.
