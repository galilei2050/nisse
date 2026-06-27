"""Trace inspection tooling: render a saved agent `TraceRecord` for humans.

`print_trace` is the shared renderer (used by `app.probe` after a run and by the `python -m app.tracing`
CLI to re-view any saved trace). See `view.py` for the renderer and `__main__.py` for the CLI.
"""

from app.tracing.view import print_trace

__all__ = ["print_trace"]
