"""Render an agent `TraceRecord` for humans — the ground truth of what the agent saw and did.

Shared by `app/probe.py` (the run it just executed) and `python -m app.tracing` (re-view any saved
trace without re-running — don't burn tokens re-running to inspect). The flags keep the output cheap:
by default print only tool calls + answer + stats; opt into the system prompt, tool results, or a grep
filter when needed.

This is a CLI rendering, not a view of the record itself: it prints nisse's own section headers, caps
results at a length that suits a terminal, and answers to command-line flags. That is why it lives
here as its own type rather than as a method on baski's `TraceRecord`.
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


class TraceView:
    """One saved run, printed for a human. Short-lived: built per probe run or per CLI invocation.

    The flags are display knobs, held here so each section can read them instead of taking them as
    arguments: `grep` implies `results`, and `full` lifts the per-result length cap.
    """

    def __init__(
        self,
        trace: TraceRecord,
        *,
        system: bool = False,
        results: bool = False,
        grep: str | None = None,
        full: bool = False,
    ) -> None:
        """Hold the trace and how much of it to show."""
        self._trace = trace
        self._system = system
        self._results = results or grep is not None
        self._grep = grep
        self._full = full

    def print_answer(self) -> None:
        """The final answer alone — the whole output of `--answer`."""
        print(self._answer())

    def print_report(self) -> None:
        """The full report: the system prompt when asked, then tool calls, answer, cache and stats."""
        if self._system:
            self._print_system()
        self._print_tool_calls()
        print("\n=== ANSWER ===\n" + self._answer())
        self._print_cache_usage()
        self._print_stats()

    def _answer(self) -> str:
        """The final answer, or a placeholder when the run produced none."""
        result = self._trace.result
        return (result and result.response) or "<no answer>"

    def _print_system(self) -> None:
        """What was injected: the system prompt and the messages of the first turn."""
        print("\n=== SYSTEM PROMPT ===\n" + self._trace.system_prompt)
        print("\n=== MESSAGES (first turn) ===")
        for msg in self._trace.turns[0].messages:  # SkipValidation kept these as raw {role, content} dicts
            print(f"\n[{msg['role']}]\n{_render_content(msg['content'])}")

    def _print_tool_calls(self) -> None:
        """Every tool call in order, each with the result the agent read back when asked for results."""
        header = "TOOL CALLS + RESULTS (what the agent saw)" if self._results else "TOOL CALLS"
        print(f"\n=== {header} ===")
        for turn in self._trace.turns:
            by_id = {r.tool_id: r for r in turn.tool_results}
            for tc in turn.tool_calls:
                print(f"\n→ {tc.name}({json.dumps(tc.input, ensure_ascii=False)})")
                result = by_id.get(tc.id)
                if self._results and result:
                    tag = " [ERROR]" if result.is_error else ""
                    print(f"  ⤷ result{tag} ({result.duration_ms}ms):\n{self._filtered(result.output)}")

    def _print_cache_usage(self) -> None:
        """Per-turn cache hits — how much of the prompt prefix the cache actually served."""
        print("\n=== PROMPT CACHE (per turn) ===")
        for turn in self._trace.turns:
            print(
                f"turn {turn.turn_number}: input={turn.input_tokens} "
                f"cache_read={turn.cache_read_tokens} cache_write={turn.cache_creation_tokens}"
            )

    def _print_stats(self) -> None:
        """Turns, tool calls, tokens and what the run cost."""
        result = self._trace.result
        if result:
            print(
                f"\n=== STATS ===\nturns={result.turn_count} tool_calls={result.tool_call_count} "
                f"in={result.total_input_tokens} out={result.total_output_tokens} cost=${result.total_cost:.4f}"
            )

    def _filtered(self, output: str) -> str:
        """A tool result trimmed for display: grep to matching lines, else cap length (unless full)."""
        if self._grep:
            hits = [line for line in output.splitlines() if self._grep.lower() in line.lower()]
            return "\n".join(hits) if hits else "(no lines match grep)"
        if self._full or len(output) <= _RESULT_CAP:
            return output
        return f"{output[:_RESULT_CAP]}… [+{len(output) - _RESULT_CAP} more chars]"
