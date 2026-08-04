# nisse — Personal AI Assistant Telegram Bot

Backend deploys to Cloud Run on GCP project `nisse2050`. Built on `baski` (https://github.com/galilei2050/baski).

## Components

- `app/`        — FastAPI / TelegramServer backend. Entry point `python -m app.backend`.
- `infrastructure/` — Pulumi IaC for GCP (Cloud Run, IAM, secrets binding).

## Local vs prod — two separate bots

Local dev and prod are **different Telegram bots with different `TELEGRAM_TOKEN`s** (prod's is a GCP secret bound to Cloud Run; local's is in `.env`). Prod runs in **webhook** mode; local (`make backend-run` / `smoke-test`) runs in **polling**. Because they're distinct bot identities, running the local poller never disturbs prod — Telegram only forbids webhook+polling on the *same* bot.

## Decision principles

The bot is a **delegate** standing in for a personal assistant — design and evaluate it by the owner's outcome and trust, not per-request cost or latency. These follow from delegation/trust research (principal-agent: Holmström 1979, Townsend 1979, Diamond 1984; trust-in-automation: Lee & See 2004, Yang/Wickens 2016; algorithm aversion: Dietvorst et al. 2015):

- **Reliability beats peak quality.** A predictably good-enough answer is worth more than a higher-average but erratic one — the owner can't cheaply audit each output, and variance's low-quality tail is where undetected, trust-destroying misses hide.
- **An unverifiable miss is disproportionately costly.** Trust builds slowly, collapses fast (≈2× asymmetry) and one salient lapse generalizes to the whole agent; machine errors are punished harder than human ones. A couple of unexplained misses make the owner re-check everything — and once verification cost ≈ doing it himself, delegation is worthless. Weigh decisions by **stakes × how-unverifiable-by-the-owner**, not by frequency.
- **Honest self-signaling is the escape hatch.** When a reply was quick / unsure / not fully checked, say so — silent variable quality is the trust-killer; a flagged "I didn't verify X" lets the owner spot-check just that, preserving delegation's value. Pair any "I don't know" with a path forward. Composes with the "verify, don't guess" behaviour.
- **No cost/latency machinery without amortization.** Routers, classifiers, extra model calls pay off on high-volume heterogeneous traffic, not a single-user bot. Prefer the one-line option with no new failure mode over a mechanism that taxes every message.
- **Don't pile onto the system prompt.** It's already dense; more instructions cause over-think and diminishing returns. Add only with empirical evidence it changes behaviour.
- **Decide empirically, via `make probe` with repeats.** A single run is noise — quality/depth vary run-to-run at fixed settings; measure the distribution (especially the shallow-rate on hard tasks), not one lucky outcome.

## Conventions

- Foundation primitives (datetime, JSON, server bases) come from `baski`. Do NOT vendor a copy here.
- **`baski` is a LIBRARY — only change it as one.** Never edit baski to suit nisse alone. Prompt text is product policy and belongs on the calling side: `GeminiJudge`, `Agent` and friends are constructed here and take their text as an argument (`instructions=`, `system_prompt=`), so tune by passing nisse's own string — never by editing the library's default. Before touching baski ask "does every consumer need this, or only nisse?"; only nisse → change nisse. (A baski edit also costs a separate PR that blocks committing nisse until it merges.)
- `from baski.primitives import datetime` — always UTC-aware. Never `from datetime import datetime`.
- **Logging is plain stdlib.** Each module declares `logger = logging.getLogger(__name__)` at top scope and logs through it — no logger injection, no `CoreDeps.logger`. Per-call structured fields ride native `extra={...}` (e.g. `logger.info("Turns deleted", extra={"turnIds": ids})`), not `labels=`. Ambient context (rare; no HTTP scope here) is `baski.server.logger.{log_context, add_labels}`.
- `from baski.telegram.server import TelegramServer` — webhook + polling server base.
- Env vars are read at module-import time (fail-fast on missing secrets). Wrap reads in a `get_X()` helper for testability.
- Use Pydantic models for any data flowing between functions. Raw dicts only for pipeline intermediates and log `extra` fields.
- **A dependency is bound in a constructor, never passed per call.** A function that takes a client,
  a database, a bot or a store as an argument is a method on a class that holds it
  (`MessageClassifier(anthropic).classify(evidence)`, not `classify(anthropic, evidence)`). A
  function whose first argument is an entity and which derives a view of it is a method on that
  entity (`evidence.render()`) — but only when the view is built from the entity's own fields and
  bakes in no consumer's format. A rendering that exists for one consumer (Telegram markup, a tool's
  result contract) stays in that consumer's module; otherwise only dependency-free helpers over
  primitives stay free functions. Enforced, not just documented: `anon_lint.py`'s **ANON003** fails
  `make lint` on a module-level function that takes a client/database/bot/store. A genuine exception
  needs `# noqa: ANON003` naming why.
- Keep `__call__`/`run` methods as 3-5 line orchestrators. Push concerns into private methods.
- **Updating `CLAUDE.md` is part of every task.** When a change alters structure, conventions, or a documented fact, update the relevant `CLAUDE.md` in the same task — a task isn't done if the docs now lie. Keep it meaning + instructions, never a copy of discoverable code.
- **Throwaway scripts and scratch data go in `scratch/`** (git-ignored) — never `/tmp`, never the repo root. One-off analysis/verification scripts, downloaded traces, ad-hoc dumps live there; run them with `uv run --env-file .env python scratch/<x>.py`.

## Docs

Design notes and deeper docs live in `docs/`. Check there before implementing; filenames describe their topic. See `docs/CLAUDE.md` for what belongs in a doc.

## Commands

- `make setup`       — venv + install deps
- `make backend-run` — start backend in polling mode (background, logs to `~/Logs/backend.log`)
- `make test`        — lint + dry-run import check
- `make test-backend-image` — build the deploy image, boot it (webhook mode), wait for `/ping`, then `pytest tests/smoke/` against it; catches startup crashes dry-run misses. Runs as the CI `smoke` job (Docker + live secrets), not in `make ci`.
- `make cd`          — build + deploy to `nisse2050`
