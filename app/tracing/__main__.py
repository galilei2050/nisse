"""Inspect a saved agent trace without re-running the probe — so you don't burn tokens to see what happened.

    uv run python -m app.tracing <trace_id|path> [--results] [--grep TEXT] [--system] [--full] [--answer]

`app.probe` saves each run to `scratch/traces/<trace_id>.json`; pass that id (or any path). Default prints
tool calls + answer + stats (cheap); `--results` adds what the agent actually saw after each call,
`--grep` narrows those results to matching lines (e.g. `--grep "Order Placed"`, `--grep Subtotal`).
"""

import argparse
from pathlib import Path

from baski.agents.trace import TraceRecord

from app.tracing.view import print_trace

_TRACES = Path(__file__).resolve().parent.parent.parent / "scratch" / "traces"


def _resolve(arg: str) -> Path:
    """Find the trace json: an explicit path, else `scratch/traces/<id>.json`."""
    direct = Path(arg)
    if direct.is_file():
        return direct
    candidate = _TRACES / (arg if arg.endswith(".json") else f"{arg}.json")
    if candidate.is_file():
        return candidate
    recent = sorted(_TRACES.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    listing = "\n".join(f"  {p.stem}" for p in recent) or "  (none yet — run `make probe` first)"
    raise SystemExit(f"trace not found: {arg}\nrecent traces in {_TRACES}:\n{listing}")


def main() -> None:
    """Parse args and print the requested view of a saved trace."""
    parser = argparse.ArgumentParser(description="Inspect a saved agent trace (no re-run).")
    parser.add_argument("trace", help="trace id (in scratch/traces/) or a path to a trace json")
    parser.add_argument("--results", action="store_true", help="show each tool result (what the agent saw)")
    parser.add_argument("--grep", help="show only result lines containing this text (implies --results)")
    parser.add_argument("--system", action="store_true", help="also print the system prompt + first-turn messages")
    parser.add_argument("--full", action="store_true", help="do not truncate tool results")
    parser.add_argument("--answer", action="store_true", help="print only the final answer")
    args = parser.parse_args()
    trace = TraceRecord.model_validate_json(_resolve(args.trace).read_text())
    print_trace(
        trace, system=args.system, results=args.results, grep=args.grep, full=args.full, answer_only=args.answer
    )


if __name__ == "__main__":
    main()
