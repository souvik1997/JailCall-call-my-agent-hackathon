"""Per-call context propagated via ``contextvars``.

The tool handlers in ``jailcall.tools`` need to know which call they're
serving so they can stamp the tool-call log (``evals/last_run/
tool_calls.jsonl``) with the right ``call_id`` and the eval harness can
grade ``expected_dispatch`` per scenario.

``contextvars.ContextVar`` propagates automatically across ``await``
boundaries within the same asyncio task, so the server-side webhook
handler only has to ``.set(...)`` once before invoking the controller —
every downstream tool call sees the same id.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Final

current_call_id: Final[ContextVar[str]] = ContextVar("current_call_id", default="")
