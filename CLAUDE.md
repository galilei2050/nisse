# nisse — Personal AI Assistant Telegram Bot

Backend deploys to Cloud Run on GCP project `nisse2050`. Built on `baski` (https://github.com/galilei2050/baski).

## Components

- `app/`        — FastAPI / TelegramServer backend. Entry point `python -m app.backend`.
- `infrastructure/` — Pulumi IaC for GCP (Cloud Run, IAM, secrets binding).

## Local vs prod — two separate bots

Local dev and prod are **different Telegram bots with different `TELEGRAM_TOKEN`s** (prod's is a GCP secret bound to Cloud Run; local's is in `.env`). Prod runs in **webhook** mode; local (`make backend-run` / `smoke-test`) runs in **polling**. Because they're distinct bot identities, running the local poller never disturbs prod — Telegram only forbids webhook+polling on the *same* bot.

## Conventions

- Foundation primitives (datetime, JSON, logging, server bases) come from `baski`. Do NOT vendor a copy here.
- `from baski.primitives import datetime` — always UTC-aware. Never `from datetime import datetime`.
- `from baski.telegram.server import TelegramServer` — webhook + polling server base.
- Env vars are read at module-import time (fail-fast on missing secrets). Wrap reads in a `get_X()` helper for testability.
- Use Pydantic models for any data flowing between functions. Raw dicts only for pipeline intermediates and logger labels.
- Keep `__call__`/`run` methods as 3-5 line orchestrators. Push concerns into private methods.
- **Updating `CLAUDE.md` is part of every task.** When a change alters structure, conventions, or a documented fact, update the relevant `CLAUDE.md` in the same task — a task isn't done if the docs now lie. Keep it meaning + instructions, never a copy of discoverable code.

## Docs

Design notes and deeper docs live in `docs/`. Check there before implementing; filenames describe their topic.

## Commands

- `make setup`       — venv + install deps
- `make backend-run` — start backend in polling mode (background, logs to `~/Logs/backend.log`)
- `make test`        — lint + dry-run import check
- `make cd`          — build + deploy to `nisse2050`
