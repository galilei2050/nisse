"""Walk one trace and every sub-agent trace it spawned, printing the nested tool-call tree.

Follows `tool_results[].sub_trace_ids` (baski) to recurse: main → researcher → retrieval → leaf tools.
Reads `scratch/traces/<id>.json` (probe's dir); accepts trace ids or file paths.

Usage: uv run python .claude/skills/analyze-traces/trace_tree.py <trace_id|file> ...
"""

import json
import sys
from pathlib import Path

TRACES_DIR = Path("scratch/traces")


def _load(ref: str) -> dict | None:
    """Resolve a trace id or path to its JSON; None (with a note) if the file isn't local."""
    path = Path(ref) if ref.endswith(".json") else TRACES_DIR / f"{ref}.json"
    if not path.exists():
        print(f"  <trace {ref} not found at {path}>")
        return None
    return json.loads(path.read_text())


def _walk(trace: dict, depth: int) -> None:
    """Print this agent's tool calls in order; recurse into each sub-agent trace it spawned."""
    pad = "  " * depth
    result = trace.get("result") or {}
    verdicts = result.get("judge_verdicts") or []
    passed = sum(1 for v in verdicts if v["finished"])
    print(f"{pad}▸ {trace['model']}  req={trace['user_request'][:80]!r}  turns={len(trace['turns'])}  judge={passed}/{len(verdicts)}")

    for turn in trace["turns"]:
        subs = {tr["tool_id"]: tr.get("sub_trace_ids", []) for tr in turn.get("tool_results", [])}
        for tc in turn.get("tool_calls", []):
            inp = json.dumps(tc.get("input", {}), ensure_ascii=False)
            print(f"{pad}  • {tc['name']}({inp[:120]})")
            for child_id in subs.get(tc["id"], []):
                child = _load(child_id)
                if child is not None:
                    _walk(child, depth + 2)


for ref in sys.argv[1:]:
    trace = _load(ref)
    if trace is not None:
        _walk(trace, 0)
