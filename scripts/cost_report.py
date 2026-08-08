"""Where the money went: who spent it, on what, and which tool filled the context.

Reads the `traces` summary alone — no trace bodies from GCS. Every number is recomputed from the
stored token counts at each model's own price, so a run's cost is explainable rather than trusted.

    make cost              # last 30 days
    make cost DAYS=7

Runs recorded before the spend fields existed (`agent_name`, `tools`, the cache buckets) are counted
and reported separately, never folded in: a total that mixes measured rows with assumed ones is the
kind of number that took a month to catch the last time.
"""

import asyncio
import datetime as dt
import os
import sys
from collections import defaultdict

from baski.agents.pricing import MODEL_PRICING
from pymongo import AsyncMongoClient

_CACHE_WRITE = 1.25  # a written cache token costs this much of the base input price
_CACHE_READ = 0.10  # a read one, this much — the 12.5x gap is why the buckets are stored apart


def _buckets(run: dict) -> dict[str, float]:
    """One run's spend split into the four things that are priced differently."""
    price = MODEL_PRICING[run["model"]]
    return {
        "fresh input": run["input_tokens"] / 1e6 * price["input"],
        "cache write": run["cache_write_tokens"] / 1e6 * price["input"] * _CACHE_WRITE,
        "cache read": run["cache_read_tokens"] / 1e6 * price["input"] * _CACHE_READ,
        "output": run["output_tokens"] / 1e6 * price["output"],
    }


def _percentile(values: list[float], share: float) -> float:
    return sorted(values)[min(int(len(values) * share), len(values) - 1)]


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
        for tool in run.get("tools", []):
            row = per_tool[tool["name"]]
            for field in ("calls", "errors", "cost", "output_chars"):
                row[field] += tool[field]

    total = sum(per_bucket.values())
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

    delegated = {sub for run in measured for sub in run.get("sub_trace_ids", [])}
    roots = sorted((r for r in measured if r["_id"] not in delegated), key=lambda r: -r["cost"])
    print("\nDEAREST ANSWERS (the run and everything it delegated to)")
    for run in roots[:10]:
        print(f"  ${run['cost']:6.2f}  {run['agent_name']:10} {run['user_request'][:60]}")


async def main(days: int) -> None:
    """Read the window from `traces` and hand the usable rows to the report."""
    db = AsyncMongoClient(os.environ["MONGODB_URI"], tz_aware=True).get_default_database()
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat()
    rows = await db["traces"].find({"created_at": {"$gte": since}}).to_list(None)

    measured = [r for r in rows if "agent_name" in r and r["model"] in MODEL_PRICING]
    print(f"=== {db.name}: last {days} days ===")
    print(f"{len(measured)} runs measured", end="")
    if older := len(rows) - len(measured):
        print(f" · {older} recorded before the spend fields existed — not broken down below", end="")
    print("\n")
    if measured:
        report(measured, days)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
