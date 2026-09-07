"""Where the money went: who spent it, on what, and which tool filled the context.

Reads the `traces` summary alone — no trace bodies from GCS. The per-agent, per-bucket and daily
totals are recomputed from the stored token counts at each model's own price, so they are
explainable rather than trusted. The dearest-answers list and the per-tool `$` column are the
trace's own `cost` field — it covers what a run delegated to, which the recomputed buckets
deliberately do not.

    make cost              # last 30 days
    make cost DAYS=7

Rows with no `agent_name` are runs recorded before the spend fields existed: counted and reported
separately, never folded in, because a total mixing measured rows with assumed ones is the kind of
number that took a month to catch the last time. Runs older still — from before `created_at` became
a date rather than a string — fall outside a date-typed window query entirely and are not counted at
all; nothing could have broken them down either.
"""

import asyncio
import math
import os
import sys
from collections import defaultdict

from anthropic.types import Usage
from baski.agents.pricing import calculate_cost
from baski.primitives import datetime
from pymongo import AsyncMongoClient


def _buckets(run: dict) -> dict[str, float]:
    """One run's spend split into the four things that are priced differently.

    Each bucket is priced by the same `calculate_cost` that charged the run — one bucket filled at a
    time — rather than by rates restated here. The two rates differ 12.5x and are not fixed forever
    (a 1-hour cache prices a write at 2x, not 1.25x), so a second copy of them would drift from what
    the database actually holds, and the report would disagree with it with nothing to show why.
    """
    model = run["model"]
    return {
        "fresh input": calculate_cost(model, Usage(input_tokens=run["input_tokens"], output_tokens=0)),
        "output": calculate_cost(model, Usage(input_tokens=0, output_tokens=run["output_tokens"])),
        "cache write": calculate_cost(
            model, Usage(input_tokens=0, output_tokens=0, cache_creation_input_tokens=run["cache_write_tokens"])
        ),
        "cache read": calculate_cost(
            model, Usage(input_tokens=0, output_tokens=0, cache_read_input_tokens=run["cache_read_tokens"])
        ),
    }


def _percentile(values: list[float], share: float) -> float:
    """Nearest-rank percentile. `int(n * share)` lands one rank high and returns the single dearest
    run as the p90 of every agent whose run count is a multiple of ten — and the tail is the number
    the whole report is read for."""
    return sorted(values)[max(math.ceil(len(values) * share) - 1, 0)]


def report(measured: list[dict], days: int) -> None:
    """Print the breakdown for runs that carry the spend fields."""
    per_agent: dict[str, list[float]] = defaultdict(list)
    per_bucket: dict[str, float] = defaultdict(float)
    per_tool: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for run in measured:
        spend = _buckets(run)
        per_agent[run["agent_name"]].append(sum(spend.values()))
        for name, amount in spend.items():
            per_bucket[name] += amount
        for tool in run["tools"]:
            row = per_tool[tool["name"]]
            for field in ("calls", "errors", "cost", "output_chars"):
                row[field] += tool[field]

    total = sum(per_bucket.values())
    if not total:
        # A run that dies on its first API call still writes a summary row, with every token count
        # at zero. A window holding only those has nothing to take a share of.
        print(f"{len(measured)} runs, none of which spent anything — every one failed before its first call.")
        return
    print(f"${total:.2f} total · ${total / days:.2f}/day\n")

    print(f"WHO SPENT IT\n{'agent':16} {'runs':>5} {'own $':>8} {'share':>6} {'median':>8} {'p90':>8}")
    for agent, spends in sorted(per_agent.items(), key=lambda kv: -sum(kv[1])):
        own = sum(spends)
        print(
            f"{agent:16} {len(spends):5} {own:8.2f} {own / total * 100:5.0f}% "
            f"{_percentile(spends, 0.5):8.3f} {_percentile(spends, 0.9):8.3f}"
        )

    print("\nWHAT IT WENT ON")
    for name, amount in sorted(per_bucket.items(), key=lambda kv: -kv[1]):
        print(f"  {name:14} {amount:8.2f} {amount / total * 100:5.0f}%")

    print(f"\nTOOLS\n{'tool':22} {'calls':>6} {'errors':>7} {'its own $':>10} {'chars into context':>19}")
    for name, row in sorted(per_tool.items(), key=lambda kv: -kv[1]["output_chars"])[:15]:
        print(
            f"{name:22} {int(row['calls']):6} {int(row['errors']):7} {row['cost']:10.2f} "
            f"{int(row['output_chars']):19,}"
        )

    delegated = {sub for run in measured for sub in run["sub_trace_ids"]}
    roots = sorted((r for r in measured if r["_id"] not in delegated), key=lambda r: -r["cost"])
    print("\nDEAREST ANSWERS (the run and everything it delegated to)")
    for run in roots[:10]:
        print(f"  ${run['cost']:6.2f}  {run['agent_name']:10} {run['user_request'][:60]}")


async def main(days: int) -> None:
    db = AsyncMongoClient(os.environ["MONGODB_URI"], tz_aware=True).get_default_database()
    since = datetime.now() - datetime.timedelta(days=days)
    rows = await db["traces"].find({"created_at": {"$gte": since}}).to_list(None)

    measured = [r for r in rows if "agent_name" in r]
    print(f"=== {db.name}: last {days} days ===")
    print(f"{len(measured)} runs measured", end="")
    if older := len(rows) - len(measured):
        print(f" · {older} recorded before the spend fields existed — not broken down below", end="")
    print("\n")
    if measured:
        report(measured, days)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
