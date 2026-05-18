"""Supermemory wiring — caller utterances, attorney replies, recall, reset.

Demo design (single-caller, per SPEC.md):

* One hardcoded container tag for the whole service — ``DEMO_CONTAINER_TAG``,
  overridable via ``JAILCALL_MEMORY_TAG`` env var, default ``jailcall:demo``.
* ``record_caller_utterance(call_id, text)`` writes one memory per caller
  turn. Fire-and-forget — voice latency must not depend on Supermemory.
* ``record_attorney_reply(sender, subject, text)`` writes one memory per
  inbound attorney email landed via the AgentMail webhook.
* ``recall_context(limit)`` pulls recent memories under the demo tag so
  the agent can personalize the conversation across calls (remember the
  caller's charge, surface attorney replies that arrived between calls).
* ``clear_memory()`` bulk-deletes every doc under the demo tag. Server
  lifespan calls it on startup so each run starts from a blank slate, and
  ``POST /api/reset-memory`` exposes the same hook for manual reset.

The Supermemory Python SDK is synchronous; we wrap calls in
``asyncio.to_thread`` so they don't block the FastAPI event loop. All
network failures are logged but never raised — Supermemory hiccups must
not break the live voice path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Final

from supermemory import Supermemory

from jailcall.config import require_env

logger = logging.getLogger("jailcall.memory")

DEMO_CONTAINER_TAG: Final[str] = os.environ.get("JAILCALL_MEMORY_TAG", "jailcall:demo")


def _client() -> Supermemory:
    """Build a Supermemory client. Raises if ``SUPERMEMORY_API_KEY`` is unset."""
    return Supermemory(api_key=require_env("SUPERMEMORY_API_KEY"))


def _add_sync(content: str) -> None:
    """Sync write of one memory under the demo container tag."""
    try:
        _ = _client().add(content=content, container_tag=DEMO_CONTAINER_TAG)
    except Exception:
        logger.exception("supermemory add failed (content=%r)", content[:120])


def _clear_sync() -> None:
    """Sync bulk-delete of every doc under the demo container tag."""
    try:
        _ = _client().documents.delete_bulk(container_tags=[DEMO_CONTAINER_TAG])
        logger.info("supermemory cleared for tag=%s", DEMO_CONTAINER_TAG)
    except Exception:
        logger.exception("supermemory clear failed for tag=%s", DEMO_CONTAINER_TAG)


def _recall_sync(limit: int) -> list[str]:
    """Sync read of recent memories under the demo container tag.

    Returns the memories chronologically (oldest first) so the prepended
    context reads like a timeline.
    """
    try:
        result = _client().documents.list(
            container_tags=[DEMO_CONTAINER_TAG],
            limit=limit,
            sort="createdAt",
            order="desc",
            include_content=True,
        )
    except Exception:
        logger.exception("supermemory recall failed")
        return []
    out: list[str] = []
    for mem in result.memories:
        content = (mem.content or mem.summary or mem.title or "").strip()
        if content:
            out.append(content)
    return list(reversed(out))


async def record_caller_utterance(call_id: str, text: str) -> None:
    """Best-effort async write of one caller turn to Supermemory."""
    if not text.strip():
        return
    content = f"[call={call_id}] caller said: {text}"
    await asyncio.to_thread(_add_sync, content)


async def record_dispatch_attempt(
    *,
    channel: str,
    target: str,
    caller_name: str,
    charge: str,
    firm_name: str = "",
) -> None:
    """Write one dispatch record (form-fill or email) to Supermemory.

    Used by ``email_attorneys`` so the recall flow on a follow-up call
    can surface that dispatch already happened — the agent reads this
    in the prior-context block and avoids re-dispatching. The
    ``firm_name`` is included verbatim so the recall block has the
    human-readable firm name, not just the contact email (which the
    model would otherwise mis-paraphrase).
    """
    firm_str = f"{firm_name} ({target})" if firm_name else target
    content = (
        f"DISPATCH ({channel}): contacted {firm_str} for caller "
        f"{caller_name!r} on the {charge!r} matter."
    )
    await asyncio.to_thread(_add_sync, content)


def build_attorney_reply_content(*, sender: str, subject: str, text: str) -> str:
    """Compose the body-first Supermemory entry for one attorney reply.

    Shared by ``record_attorney_reply`` (async webhook path) and the
    synchronous inbox-polling path in ``jailcall.tools`` so both write
    the same content format — recall surfaces them identically.
    """
    body_excerpt = text.strip()[:1000] or "(empty body)"
    return (
        "Inbound attorney reply landed in the dispatch inbox.\n"
        "TRUST THE BODY, not the From/Subject headers — identify the firm and "
        "the substance of the reply from the body text below.\n\n"
        f"Body:\n{body_excerpt}\n\n"
        f"(Headers — informational only — from={sender.strip() or 'unknown'}; "
        f"subject={subject.strip() or '(no subject)'})"
    )


def record_attorney_reply_sync(*, sender: str, subject: str, text: str) -> None:
    """Synchronous variant for callers already in a worker thread."""
    _add_sync(build_attorney_reply_content(sender=sender, subject=subject, text=text))


async def record_attorney_reply(*, sender: str, subject: str, text: str) -> None:
    """Best-effort async write of one inbound attorney email to Supermemory.

    Body-first format. The demo deliberately ignores the ``from``/``to``
    addresses for routing — anyone can write to the dispatch inbox and
    claim to be a firm, and the agent reads the body text to identify
    which firm replied. Sender + subject are recorded as metadata at
    the end of the entry, not as the authoritative source of truth.
    """
    content = build_attorney_reply_content(sender=sender, subject=subject, text=text)
    await asyncio.to_thread(_add_sync, content)


async def recall_context(limit: int = 20) -> list[str]:
    """Return recent memories for this demo caller (oldest first).

    Used to prepend a "prior context" agent turn into ``recent_history``
    so the model can recognise a returning caller, remember their charge,
    and surface attorney replies that landed between calls.
    """
    return await asyncio.to_thread(_recall_sync, limit)


async def clear_memory() -> None:
    """Bulk-delete every doc under the demo container tag."""
    await asyncio.to_thread(_clear_sync)
