"""Render an agent `TraceRecord` for humans — the ground truth of what the agent saw and did.

Shared by `app/probe.py` (the run it just executed) and `app/trace.py` (re-view any saved trace without
re-running — don't burn tokens re-running to inspect). Flags keep the output cheap: by default print
only tool calls + answer + stats; opt into system prompt, tool results, or a grep filter when needed.
"""

import json

from baski.agents.trace import TraceRecord

from app.shared import block_type

_RESULT_CAP = 4000  # chars of each tool result printed with --results (unless --full)


def _render_content(content: object) -> str:
    """Flatten a serialized message's content (text rendered verbatim, other blocks tagged)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for block in content:
        btype = block_type(block)
        if btype == "text":
            parts.append(str(block["text"]))
        elif btype is not None:
            parts.append(f"<{btype}>")
        else:
            parts.append(str(block))
    return "\n".join(parts)


def _filtered(output: str, grep: str | None, *, full: bool) -> str:
    """A tool result trimmed for display: grep to matching lines, else cap length (unless full)."""
    if grep:
        hits = [line for line in output.splitlines() if grep.lower() in line.lower()]
        return "\n".join(hits) if hits else "(no lines match grep)"
    if full or len(output) <= _RESULT_CAP:
        return output
    return f"{output[:_RESULT_CAP]}… [+{len(output) - _RESULT_CAP} more chars]"


def print_trace(
    trace: TraceRecord,
    *,
    system: bool = False,
    results: bool = False,
    grep: str | None = None,
    full: bool = False,
    answer_only: bool = False,
) -> None:
    """Print a trace. Default: tool calls + answer + stats. Flags add system prompt / results / grep."""
    if answer_only:
        print((trace.result and trace.result.response) or "<no answer>")
        return

    if system:
        print("\n=== SYSTEM PROMPT ===\n" + trace.system_prompt)
        print("\n=== MESSAGES (first turn) ===")
        for msg in trace.turns[0].messages:  # SkipValidation kept these as raw {role, content} dicts
            print(f"\n[{msg['role']}]\n{_render_content(msg['content'])}")

    show_results = results or grep is not None
    header = "TOOL CALLS + RESULTS (what the agent saw)" if show_results else "TOOL CALLS"
    print(f"\n=== {header} ===")
    for turn in trace.turns:
        by_id = {r.tool_id: r for r in turn.tool_results}
        for tc in turn.tool_calls:
            print(f"\n→ {tc.name}({json.dumps(tc.input, ensure_ascii=False)})")
            result = by_id.get(tc.id)
            if show_results and result:
                tag = " [ERROR]" if result.is_error else ""
                print(f"  ⤷ result{tag} ({result.duration_ms}ms):\n{_filtered(result.output, grep, full=full)}")

    result = trace.result
    print("\n=== ANSWER ===\n" + ((result and result.response) or "<no answer>"))
    if result:
        print(
            f"\n=== STATS ===\nturns={result.turn_count} tool_calls={result.tool_call_count} "
            f"in={result.total_input_tokens} out={result.total_output_tokens} cost=${result.total_cost:.4f}"
        )
