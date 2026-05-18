"""Moss client.

Thin async adapter around the Moss Python SDK. Used by
``jailcall.tools.moss_find_lawyers`` to retrieve top-K attorney
candidates from the pre-indexed Bay Area roster (built by
``jailcall.build_index``).

``MOSS_PROJECT_ID`` and ``MOSS_PROJECT_KEY`` must be set; the module
fails to import otherwise (no silent fallback).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, override

from dotenv import load_dotenv
from moss import MossClient as SdkMossClient
from moss import QueryOptions

from jailcall.config import require_env

_ = load_dotenv()


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
    """The narrow interface ``jailcall.tools`` uses.

    Kept as an ABC so future test doubles or alternate backends can drop
    in without touching the tool layer.
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


class RealMossClient(MossClient):
    """Thin async adapter around the Moss Python SDK."""

    def __init__(self, project_id: str, project_key: str) -> None:
        """Create a real Moss SDK client."""
        self._client: Any = SdkMossClient(project_id, project_key)
        self._query_options: Any = QueryOptions
        self._loaded_indexes: set[str] = set()

    @override
    async def load_index(self, name: str) -> None:
        """Load an index into the local Moss runtime (idempotent per name)."""
        if name in self._loaded_indexes:
            return
        await self._client.load_index(name)
        self._loaded_indexes.add(name)

    @override
    async def query(
        self,
        index_name: str,
        query_str: str,
        *,
        top_k: int = 3,
        alpha: float = 0.6,
    ) -> MossQueryResult:
        """Run a Moss query and normalize SDK docs into our local shape."""
        await self.load_index(index_name)
        result = await self._client.query(
            index_name,
            query_str,
            self._query_options(top_k=top_k, alpha=alpha),
        )
        docs: list[MossDoc] = []
        for raw_doc in getattr(result, "docs", []):
            metadata = getattr(raw_doc, "metadata", {}) or {}
            docs.append(
                MossDoc(
                    id=str(getattr(raw_doc, "id", "")),
                    text=str(getattr(raw_doc, "text", "")),
                    metadata=dict(metadata),
                ),
            )
        return MossQueryResult(
            docs=docs,
            time_taken_ms=float(getattr(result, "time_taken_ms", 0.0) or 0.0),
        )


# Singleton wiring. ``require_env`` raises a clear error at import time if
# the Moss credentials are missing — that's the intended behaviour.
_client: MossClient = RealMossClient(
    project_id=require_env("MOSS_PROJECT_ID"),
    project_key=require_env("MOSS_PROJECT_KEY"),
)


def get_moss_client() -> MossClient:
    """Return the process-wide Moss client."""
    return _client
