# nisse — Personal AI Assistant Telegram Bot

Backend deploys to Cloud Run on GCP project `nisse2050`. Built on `baski` (https://github.com/galilei2050/baski).

## Components

- `app/`        — FastAPI / TelegramServer backend. Entry point `python -m app.backend`.
- `infrastructure/` — Pulumi IaC for GCP (Cloud Run, IAM, secrets binding).

## Conventions

- Foundation primitives (datetime, JSON, logging, server bases) come from `baski`. Do NOT vendor a copy here.
- `from baski.primitives import datetime` — always UTC-aware. Never `from datetime import datetime`.
- `from baski.telegram.server import TelegramServer` — webhook + polling server base.
- Env vars are read at module-import time (fail-fast on missing secrets). Wrap reads in a `get_X()` helper for testability.
- Use Pydantic models for any data flowing between functions. Raw dicts only for pipeline intermediates and logger labels.
- Keep `__call__`/`run` methods as 3-5 line orchestrators. Push concerns into private methods.

## Commands

- `make setup`       — venv + install deps
- `make backend-run` — start backend in polling mode (background, logs to `~/Logs/backend.log`)
- `make test`        — lint + dry-run import check
- `make cd`          — build + deploy to `nisse2050`
