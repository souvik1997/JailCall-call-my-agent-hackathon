"""Moss client abstraction with an in-process mock.

This module fronts the Moss SDK so the controller and tool layer don't
have to care whether they're talking to the real service or a stub.
Milestone 5 will introduce ``RealMossClient`` (wrapping
``from moss import MossClient``); until then ``MockMossClient`` is the
default and returns three canned Bay Area criminal defense firms.

The mock's surface mirrors what the real SDK gives us — ``query`` returns
a result object with ``.docs``, each doc has ``.text`` and ``.metadata`` —
so swapping implementations is one line in ``set_moss_client``.

The eval harness drives the mock via two affordances:

* **Per-test response override** — ``MockMossClient.set_response_for(
  query_substring, docs)`` swaps the canned docs for queries that match
  a substring (case-insensitive).
* **Call inspection** — ``MockMossClient.call_log`` records every
  ``query`` / ``load_index`` invocation. The harness uses this together
  with the broader tool-call log (``evals/last_run/tool_calls.jsonl``)
  to grade ``expected_dispatch``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Final, override


@dataclass
class MossDoc:
    """One document returned by ``MossClient.query``.

    Mirrors the public surface of the real SDK's result docs.
    """

    id: str
    text: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class MossQueryResult:
    """Wrapper around the docs list, matching the real SDK's result shape."""

    docs: list[MossDoc]
    time_taken_ms: float = 0.0


class MossClient(ABC):
    """The narrow interface ``jailcall.tools`` actually uses.

    Real and mock impls both implement these methods.
    """

    @abstractmethod
    async def load_index(self, name: str) -> None:
        """Pre-load an index so subsequent ``query`` calls are local."""

    @abstractmethod
    async def query(
        self,
        index_name: str,
        query_str: str,
        *,
        top_k: int = 3,
        alpha: float = 0.6,
    ) -> MossQueryResult:
        """Run a hybrid semantic+keyword query against ``index_name``."""


# Canned data the mock returns when no scenario-specific override matches.
# Three Bay Area criminal defense firms — enough for the controller to
# fan out contact_attorneys / email_attorneys without obvious data gaps.
_DEFAULT_DOCS: Final[list[MossDoc]] = [
    MossDoc(
        id="bay-area-defense",
        text=(
            "San Francisco criminal defense firm. Practice areas include "
            "DUI, drug offenses, and assault. 24/7 emergency intake."
        ),
        metadata={
            "name": "Bay Area Defense Group",
            "phone": "+14155550100",
            "email": "intake@bayareadefense.example",
            "intake_url": "https://bayareadefense.example/contact",
            "website": "https://bayareadefense.example",
        },
    ),
    MossDoc(
        id="oakland-criminal",
        text=(
            "Oakland-based criminal defense practice handling DUI, drug, "
            "and assault cases. Takes after-hours calls."
        ),
        metadata={
            "name": "Oakland Criminal Defense LLP",
            "phone": "+15105550200",
            "email": "intake@oaklanddefense.example",
            "intake_url": "https://oaklanddefense.example/contact",
            "website": "https://oaklanddefense.example",
        },
    ),
    MossDoc(
        id="peninsula-trial",
        text=(
            "Palo Alto / South Bay criminal defense; emphasis on first-time "
            "offenders and pre-charge negotiation."
        ),
        metadata={
            "name": "Peninsula Trial Counsel",
            "phone": "+16505550300",
            "email": "intake@peninsulacounsel.example",
            "intake_url": "https://peninsulacounsel.example/intake",
            "website": "https://peninsulacounsel.example",
        },
    ),
]


class MockMossClient(MossClient):
    """In-process Moss stub for development and eval runs.

    Records every method call into ``self.call_log`` and allows scenario-
    specific response overrides keyed by query substring. The same instance
    serves the whole server process, so the eval harness can read the
    ``call_log`` after each scenario to verify what was searched for.
    """

    def __init__(self) -> None:
        """Start with no overrides and an empty call log."""
        self._overrides: dict[str, list[MossDoc]] = {}
        self.call_log: list[dict[str, object]] = []

    def set_response_for(self, query_substring: str, docs: list[MossDoc]) -> None:
        """Override the canned docs when ``query_substring`` appears in the query.

        Substring match is case-insensitive. Last-set wins on conflict.
        """
        self._overrides[query_substring.lower()] = docs

    def clear_overrides(self) -> None:
        """Reset all per-scenario response overrides."""
        self._overrides.clear()

    def clear_call_log(self) -> None:
        """Reset the recorded call log (e.g., between scenarios)."""
        self.call_log.clear()

    @override
    async def load_index(self, name: str) -> None:
        """No-op; record the call."""
        self.call_log.append({"method": "load_index", "name": name})

    @override
    async def query(
        self,
        index_name: str,
        query_str: str,
        *,
        top_k: int = 3,
        alpha: float = 0.6,
    ) -> MossQueryResult:
        """Return overridden docs if any pattern matches, else the canned set."""
        self.call_log.append(
            {
                "method": "query",
                "index": index_name,
                "query": query_str,
                "top_k": top_k,
                "alpha": alpha,
            },
        )
        q = query_str.lower()
        for pattern, docs in self._overrides.items():
            if pattern in q:
                return MossQueryResult(docs=list(docs[:top_k]))
        return MossQueryResult(docs=list(_DEFAULT_DOCS[:top_k]))


# ── Singleton wiring ─────────────────────────────────────────────────────────
# The controller and tool layer get the client via ``get_moss_client``. The
# eval harness can swap in a custom instance (or stash an override) via
# ``set_moss_client``. Milestone 5 will swap to ``RealMossClient`` here.

_client: MossClient = MockMossClient()


def get_moss_client() -> MossClient:
    """Return the process-wide Moss client."""
    return _client


def set_moss_client(client: MossClient) -> None:
    """Replace the process-wide Moss client (for tests / M5 swap)."""
    global _client  # noqa: PLW0603 — process-wide singleton, intentional
    _client = client
