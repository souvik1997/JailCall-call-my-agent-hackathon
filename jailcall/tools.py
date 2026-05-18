"""Tool schemas and handlers for JailCall's Gemini tool-call loop.

Two tools today: ``moss_find_lawyers`` (semantic routing over the Bay
Area criminal-defense roster) and ``email_attorneys`` (real outbound
AgentMail send). Browser Use is intentionally NOT in the runtime path —
it's used by ``jailcall.scrape_firm_emails`` to enrich the corpus
offline, which means every firm in the index has an email and the live
agent can reach all of them in ~1 second per send.

Jurisdiction is hardcoded to the Bay Area, so there is no ``classify_location``
tool and no ``county``/``state`` arguments. SMS confirmation is dropped — the
inbound voice line is the only AgentPhone channel.

The schemas are in google-genai ``FunctionDeclaration`` shape, ready to drop
into a ``Tool(function_declarations=[...])`` config. Handlers are async:
Moss uses the real SDK when credentials are configured, Browser Use runs
form submissions in the background, and AgentMail still uses a fast stub
for the current demo path.
"""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final, cast

from agentmail import AgentMail
from google.genai import types

from jailcall.call_context import current_call_id, current_turn_started_at
from jailcall.config import require_env
from jailcall.dashboard import record_moss_search, record_tool_status
from jailcall.facility import current_facility
from jailcall.memory import record_dispatch_attempt
from jailcall.moss import get_moss_client

logger = logging.getLogger("jailcall.tools")

ToolHandler = Callable[[dict[str, object]], Awaitable[str]]

# Tool-call log path — the eval harness reads this per ``call_id`` to grade
# ``expected_dispatch`` assertions. One JSONL line per call; never trimmed
# at runtime (the harness truncates between suite runs).
TOOL_CALL_LOG_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "evals" / "last_run" / "tool_calls.jsonl"
)

# Default Moss index name. Real one is set via env in M5.
DEFAULT_MOSS_INDEX: Final[str] = "jailcall-lawyers"
MOSS_RETRIEVAL_TOP_K: Final[int] = 12
MOSS_FIRM_LIMIT: Final[int] = 3

# Hackathon demo lever: this firm is force-prioritised to firm_id="0" in
# every moss_find_lawyers result regardless of charge. Real firm with a
# real intake address (help@veralawoffice.com) — picked over Nieves
# because "Vera Law" is easier to pronounce in the spoken confirmation.
# The user manually impersonates this firm in the AgentMail inbox during
# the demo.
DEMO_PRIORITY_FIRM_SHORT_NAME: Final[str] = "vera-law"

# Per-call cache of the most recent ``moss_find_lawyers`` result, keyed
# by call_id → {firm_id: candidate_dict}. ``email_attorneys`` looks up
# ``firm_id`` here to get the real email address instead of trusting
# whatever the model passes — Gemini was confidently hallucinating URLs
# and emails before this.
_moss_results_by_call: dict[str, dict[str, dict[str, object]]] = {}


def clear_moss_result_cache() -> None:
    """Drop every cached moss result. Called from server lifespan / reset."""
    _moss_results_by_call.clear()


# ─── Schemas (Gemini FunctionDeclaration shape) ─────────────────────────────


MOSS_FIND_LAWYERS_SCHEMA: Final[types.FunctionDeclaration] = types.FunctionDeclaration(
    name="moss_find_lawyers",
    description=(
        "Look up Bay Area criminal defense attorneys whose practice matches the "
        "caller's charge category. Returns up to 3 candidates as a JSON array of "
        "{firm_id, firm_name, phone, email, form_url, summary}. The firm_id field "
        "is a small positional string — '0', '1', or '2' — that you MUST pass "
        "back to email_attorneys verbatim. Do not invent "
        "firm_ids and do not pass URLs/emails yourself. Sub-second — safe to "
        "call inline mid-turn. Jurisdiction is hardcoded; do not pass any location."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "charge_category": types.Schema(
                type=types.Type.STRING,
                description=(
                    'The caller\'s charge category in plain English (e.g. "DUI", '
                    '"drug charge", "assault"). Pass "unknown" if the caller did not know.'
                ),
            ),
        },
        required=["charge_category"],
    ),
)


EMAIL_ATTORNEYS_SCHEMA: Final[types.FunctionDeclaration] = types.FunctionDeclaration(
    name="email_attorneys",
    description=(
        "Send a structured intake email to an attorney via AgentMail. Sub-"
        "second send. Call once per firm returned by moss_find_lawyers (one "
        "call per firm_id). The handler looks up the real email address and "
        "facility info from the cached moss result — you do NOT pass an "
        "email address."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "firm_id": types.Schema(
                type=types.Type.STRING,
                description=(
                    "The firm_id from one of the moss_find_lawyers candidates "
                    "— a small positional string, '0' / '1' / '2'. Copy "
                    "verbatim from the moss output. Do NOT invent cuid-style "
                    "ids or substitute anything else. The handler resolves "
                    "firm_id to the firm's real email."
                ),
            ),
            "caller_name": types.Schema(
                type=types.Type.STRING,
                description="The caller's name.",
            ),
            "charge_category": types.Schema(
                type=types.Type.STRING,
                description='Charge category in plain English (e.g. "DUI").',
            ),
            "message": types.Schema(
                type=types.Type.STRING,
                description=(
                    "Short free-form note specific to this caller. Leave empty "
                    "if none. Do NOT include the jail facility info."
                ),
            ),
        },
        required=["firm_id", "caller_name", "charge_category", "message"],
    ),
)


TOOL_SCHEMAS: Final[list[types.FunctionDeclaration]] = [
    MOSS_FIND_LAWYERS_SCHEMA,
    EMAIL_ATTORNEYS_SCHEMA,
]


# ─── Tool handlers ──────────────────────────────────────────────────────────


def _candidate_from_doc(doc: object, firm_short: str) -> dict[str, object]:
    """Build the model-visible candidate dict from one Moss doc.

    ``firm_id`` is intentionally NOT set here — it gets assigned by
    position after the priority-firm reordering in ``moss_find_lawyers``.
    """
    metadata = cast("dict[str, object]", getattr(doc, "metadata", {}) or {})
    text = str(getattr(doc, "text", "") or "")
    return {
        "firm_short_name": firm_short,
        "firm_name": metadata.get("name"),
        "phone": metadata.get("phone"),
        "email": metadata.get("email"),
        "form_url": metadata.get("intake_url") or metadata.get("website"),
        "summary": text[:1200],
    }


async def _prioritise_demo_firm(
    candidates: list[dict[str, object]],
    index_name: str,
) -> list[dict[str, object]]:
    """Return ``candidates`` with the demo-priority firm at index 0.

    If the priority firm is already in ``candidates``, just move it to
    the front. Otherwise, do a targeted Moss query for it and prepend
    the result. Failures fall back to the original list (the demo
    still works, the firm just isn't force-prioritised).
    """
    priority = DEMO_PRIORITY_FIRM_SHORT_NAME
    in_list = [c for c in candidates if c.get("firm_short_name") == priority]
    rest = [c for c in candidates if c.get("firm_short_name") != priority]
    if in_list:
        return [*in_list, *rest]

    try:
        fetched = await get_moss_client().query(
            index_name,
            f"{priority} criminal defense Bay Area",
            top_k=MOSS_RETRIEVAL_TOP_K,
            alpha=0.6,
        )
    except Exception:
        logger.exception("priority-firm lookup for %r failed; serving Moss order", priority)
        return candidates
    for doc in fetched.docs:
        metadata = cast("dict[str, object]", getattr(doc, "metadata", {}) or {})
        short = str(
            metadata.get("firm_id")
            or metadata.get("short_name")
            or str(getattr(doc, "id", "")).split(":", 1)[0],
        )
        if short == priority:
            return [_candidate_from_doc(doc, short), *rest]
    logger.warning("priority firm %r not found in Moss index; serving Moss order", priority)
    return candidates


async def moss_find_lawyers(args: dict[str, object]) -> str:
    """Query the Moss client for Bay Area attorneys matching ``charge_category``.

    Routes through ``jailcall.moss.get_moss_client()`` so the mock (today)
    and the real Moss SDK (Milestone 5) are swappable without touching this
    function. The return contract — JSON array of ``{firm_name, phone,
    email, form_url, summary}`` — is locked so the controller doesn't have
    to change.
    """
    charge = str(args.get("charge_category") or "criminal defense").strip()
    query_str = (
        f"Caller needs a Bay Area criminal defense attorney for: {charge}. "
        f"Match charge category, offense facts, and reachable intake path."
    )
    index_name = os.environ.get("MOSS_INDEX_NAME", DEFAULT_MOSS_INDEX)

    client = get_moss_client()
    started = time.perf_counter()
    result = await client.query(index_name, query_str, top_k=MOSS_RETRIEVAL_TOP_K, alpha=0.6)
    duration_ms = (time.perf_counter() - started) * 1000

    # Dedupe by firm short_name, capturing top hits up to MOSS_FIRM_LIMIT + 1
    # extras (the +1 covers the case where we have to evict a slot for
    # the demo-priority firm).
    pre_candidates: list[dict[str, object]] = []
    seen_firms: set[str] = set()
    for doc in result.docs:
        firm_short = str(
            doc.metadata.get("firm_id") or doc.metadata.get("short_name") or doc.id.split(":", 1)[0]
        )
        if not firm_short or firm_short in seen_firms:
            continue
        seen_firms.add(firm_short)
        pre_candidates.append(_candidate_from_doc(doc, firm_short))
        if len(pre_candidates) >= MOSS_FIRM_LIMIT + 1:
            break

    # Force the demo-priority firm (bay-area-lawyers) to firm_id="0" no
    # matter what charge the caller named. If Moss didn't naturally
    # return it, do a targeted lookup. The cache + Gemini dispatch flow
    # is unchanged: firm_id "0" still resolves to whatever firm sits in
    # slot 0, but slot 0 is now stable across queries.
    ordered = await _prioritise_demo_firm(pre_candidates, index_name)
    candidates: list[dict[str, object]] = []
    for slot, raw in enumerate(ordered[:MOSS_FIRM_LIMIT]):
        raw["firm_id"] = str(slot)
        candidates.append(raw)

    logger.info(
        "moss_find_lawyers returned %d candidate(s) for charge=%r firm_ids=%s",
        len(candidates),
        charge,
        [(c["firm_id"], c["firm_short_name"]) for c in candidates],
    )

    # Cache the candidates so email_attorneys can
    # resolve firm_id → real form_url / email (defeats hallucination of
    # URLs and emails by the model).
    call_id = current_call_id.get()
    if call_id:
        _moss_results_by_call[call_id] = {str(c["firm_id"]): c for c in candidates}

    record_moss_search(
        query=query_str,
        index_name=index_name,
        candidates=candidates,
        duration_ms=duration_ms,
    )
    return json.dumps(candidates)


def _build_intake_message(
    *,
    caller_name: str,
    charge: str,
    extra: str,
    reply_email: str = "",
) -> str:
    """Compose the urgent intake note attorneys actually see.

    Facility info (where the caller is held, how to reach them) is
    injected here so the model never has to know those values — the
    caller's facility is server-side state. ``reply_email``, when set,
    is the AgentMail inbox attorneys should reply to so both the
    form-fill and email channels converge on the same /webhook/agentmail
    ingest pipeline.
    """
    facility = current_facility()
    blocks = [
        (
            f"URGENT: {caller_name} is in custody at {facility.name} and is "
            f"requesting criminal defense representation for a {charge} charge."
        ),
        (
            f"How to reach them:\n"
            f"  - Facility intake line: {facility.phone}\n"
            f"  - Address: {facility.address}\n"
            f"  - {facility.visit_info}"
        ),
        "Please respond as soon as possible — every hour matters.",
    ]
    if reply_email:
        blocks.append(
            f"Reply to: {reply_email} (JailCall dispatch inbox —"
            + " replies route back to the caller's case file).",
        )
    if extra:
        blocks.append(f"From the caller: {extra}")
    return "\n\n".join(blocks)


def _agentmail_dispatch_inbox() -> str:
    """Return the AgentMail inbox attorneys should reply to."""
    return require_env("AGENTMAIL_DISPATCH_INBOX")


# Lazy-init AgentMail client + inbox-id cache. The list-inboxes lookup
# fires once per process; subsequent sends use the cached id. Lowercased
# names so basedpyright doesn't treat them as immutable constants.
_agentmail_client_singleton: AgentMail | None = None
_agentmail_inbox_id_cache: str | None = None


def _agentmail_client_sync() -> AgentMail:
    """Lazy-construct + cache the AgentMail SDK client."""
    global _agentmail_client_singleton  # noqa: PLW0603 — process-wide singleton
    if _agentmail_client_singleton is None:
        _agentmail_client_singleton = AgentMail(api_key=require_env("AGENTMAIL_API_KEY"))
    return _agentmail_client_singleton


def _resolve_dispatch_inbox_id_sync(email: str) -> str:
    """Look up the inbox_id for ``email``, cache it for the process lifetime."""
    global _agentmail_inbox_id_cache  # noqa: PLW0603 — process-wide singleton
    if _agentmail_inbox_id_cache is not None:
        return _agentmail_inbox_id_cache
    client = _agentmail_client_sync()
    response = client.inboxes.list()
    for inbox in response.inboxes:
        if inbox.email == email:
            _agentmail_inbox_id_cache = inbox.inbox_id
            return _agentmail_inbox_id_cache
    msg = f"AgentMail inbox {email!r} not found among {len(response.inboxes)} inboxes"
    raise RuntimeError(msg)


# Tracks ``(thread_id, last_message_id)`` pairs we've already ingested via
# the synchronous inbox sync below, so each turn only writes truly new
# messages into the dashboard + Supermemory.
_seen_inbox_thread_messages: set[tuple[str, str]] = set()


def clear_inbox_sync_state() -> None:
    """Reset the inbox-sync dedup set. Called from lifespan / reset."""
    _seen_inbox_thread_messages.clear()


def _ingest_inbox_thread(
    *,
    sender: str,
    subject: str,
    text: str,
) -> None:
    """Record one thread's latest content into dashboard + Supermemory.

    Uses ``memory.record_attorney_reply_sync`` so the recall block format
    is identical regardless of whether the reply arrived via the webhook
    push or this synchronous pull.
    """
    from jailcall.dashboard import record_inbound_reply  # noqa: PLC0415
    from jailcall.memory import record_attorney_reply_sync  # noqa: PLC0415

    record_inbound_reply(sender=sender, subject=subject, text=text)
    record_attorney_reply_sync(sender=sender, subject=subject, text=text)


def _sync_inbox_sync(dispatch_inbox: str) -> int:
    """Pull threads from the dispatch inbox, ingest new ones. Returns count."""
    inbox_id = _resolve_dispatch_inbox_id_sync(dispatch_inbox)
    client = _agentmail_client_sync()
    response = client.inboxes.threads.list(
        inbox_id,
        include_spam=True,
        include_trash=True,
    )
    new_count = 0
    for thread in response.threads:
        key = (thread.thread_id, thread.last_message_id or "")
        if key in _seen_inbox_thread_messages:
            continue
        _seen_inbox_thread_messages.add(key)
        sender = thread.senders[0] if thread.senders else "unknown"
        _ingest_inbox_thread(
            sender=sender,
            subject=thread.subject or "(no subject)",
            text=thread.preview or "",
        )
        new_count += 1
    return new_count


async def sync_inbox_into_dashboard() -> int:
    """Synchronously pull new AgentMail messages, ingest into dashboard+memory.

    Called at the start of every voice turn so the agent's prior-context
    block is always fresh, even if the AgentMail webhook never fires
    (e.g., tunnel down, webhook misconfigured). Errors are swallowed —
    a failed sync must not block the voice path.
    """
    try:
        dispatch_inbox = _agentmail_dispatch_inbox()
    except RuntimeError:
        return 0
    try:
        return await asyncio.to_thread(_sync_inbox_sync, dispatch_inbox)
    except Exception:
        logger.exception("agentmail inbox sync failed")
        return 0


def _clear_agentmail_inbox_sync(dispatch_inbox: str) -> int:
    """Permanently delete every thread in the dispatch inbox. Returns count.

    AgentMail deletes are thread-based (not per-message). Pages through
    ``inboxes.threads.list`` with spam/trash included so a fresh demo run
    starts with a truly empty inbox — same intent as ``memory.clear_memory``
    for Supermemory. ``permanent=True`` skips the trash bucket so threads
    don't resurrect into recall on the next run.
    """
    inbox_id = _resolve_dispatch_inbox_id_sync(dispatch_inbox)
    client = _agentmail_client_sync()
    deleted = 0
    page_token: str | None = None
    while True:
        response = client.inboxes.threads.list(
            inbox_id,
            include_spam=True,
            include_trash=True,
            page_token=page_token,
        )
        for thread in response.threads:
            try:
                client.inboxes.threads.delete(inbox_id, thread.thread_id, permanent=True)
                deleted += 1
            except Exception:
                logger.exception("agentmail thread delete failed id=%s", thread.thread_id)
        page_token = response.next_page_token
        if not page_token:
            break
    return deleted


async def clear_agentmail_inbox() -> None:
    """Bulk-delete every message in the AgentMail dispatch inbox.

    Failure-tolerant: AgentMail outages must not block server startup.
    """
    try:
        dispatch_inbox = _agentmail_dispatch_inbox()
    except Exception:
        logger.exception("agentmail inbox clear skipped — env not configured")
        return
    try:
        deleted = await asyncio.to_thread(_clear_agentmail_inbox_sync, dispatch_inbox)
    except Exception:
        logger.exception("agentmail inbox clear failed for %s", dispatch_inbox)
        return
    logger.info("agentmail inbox cleared (%d message(s)) for %s", deleted, dispatch_inbox)


def _send_intake_email_sync(
    *,
    to: str,
    subject: str,
    text: str,
    dispatch_inbox: str,
) -> dict[str, object]:
    """Synchronous AgentMail send. Returns a small dict summarising the result."""
    inbox_id = _resolve_dispatch_inbox_id_sync(dispatch_inbox)
    client = _agentmail_client_sync()
    response = client.inboxes.messages.send(
        inbox_id,
        to=to,
        subject=subject,
        text=text,
    )
    message_id = getattr(response, "message_id", None) or getattr(response, "id", "")
    return {
        "inbox_id": inbox_id,
        "message_id": str(message_id) if message_id else "",
    }


async def email_attorneys(args: dict[str, object]) -> str:
    """Send an attorney intake email via AgentMail.

    The body is built from the caller's name + charge + the hardcoded
    facility context, so the attorney knows where the caller is held and
    how to reach them. Sent FROM the AgentMail dispatch inbox — replies
    land back in the same inbox and route into Supermemory via
    ``/webhook/agentmail`` so the next call's recall surfaces them.
    """
    firm_id = str(args.get("firm_id") or "")
    caller_name = str(args.get("caller_name") or "")
    charge = str(args.get("charge_category") or "")
    extra = str(args.get("message") or "")
    facility = current_facility()
    call_id = current_call_id.get() or "unknown-call"

    if not firm_id:
        return json.dumps({"error": "email_attorneys requires firm_id"})
    firm = _moss_results_by_call.get(call_id, {}).get(firm_id)
    if firm is None:
        valid = sorted(_moss_results_by_call.get(call_id, {}).keys())
        return json.dumps(
            {
                "error": "unknown firm_id",
                "passed": firm_id,
                "valid_firm_ids": valid,
                "note": (
                    "Call moss_find_lawyers first, then pass one of its "
                    "firm_id values verbatim. Do not invent firm_ids."
                ),
            },
        )
    to = str(firm.get("email") or "")
    if not to:
        return json.dumps(
            {
                "error": "firm has no email",
                "firm_id": firm_id,
                "firm_name": firm.get("firm_name"),
            },
        )

    dispatch_inbox = _agentmail_dispatch_inbox()
    intake_message = _build_intake_message(
        caller_name=caller_name,
        charge=charge,
        extra=extra,
        reply_email=dispatch_inbox,
    )

    # Hackathon-only short-circuit: a ``noreply@<domain>`` address is a
    # placeholder we generated from the firm's website domain when the
    # scraper couldn't find a real intake email. Sending there would
    # either bounce or hit an unrelated inbox (e.g. the website host's
    # default mailbox), so we record the dispatch (for memory + guard +
    # dashboard) but skip the actual AgentMail send.
    if to.lower().startswith("noreply@"):
        logger.info(
            "email_attorneys SKIPPING placeholder send to %s for %r (facility=%s)",
            to,
            caller_name,
            facility.name,
        )
        await record_dispatch_attempt(
            channel="email",
            target=to,
            caller_name=caller_name,
            charge=charge,
            firm_name=str(firm.get("firm_name") or ""),
        )
        return json.dumps(
            {
                "status": "sent",
                "placeholder": True,
                "firm_id": firm_id,
                "firm_name": firm.get("firm_name"),
                "to": to,
                "caller_name": caller_name,
                "facility": facility.name,
                "facility_phone": facility.phone,
                "reply_email": dispatch_inbox,
                "message_preview": "(placeholder address — no real email sent)",
            },
        )

    subject = (
        f"Urgent criminal defense request — {caller_name or 'caller in custody'} "
        f"({charge or 'charge unknown'})"
    )

    logger.info(
        "email_attorneys sending intake email to %s for %r (facility=%s reply=%s)",
        to,
        caller_name,
        facility.name,
        dispatch_inbox,
    )
    try:
        send_result = await asyncio.to_thread(
            _send_intake_email_sync,
            to=to,
            subject=subject,
            text=intake_message,
            dispatch_inbox=dispatch_inbox,
        )
    except Exception as exc:
        logger.exception("email_attorneys send failed (to=%s)", to)
        return json.dumps(
            {
                "status": "send_failed",
                "to": to,
                "error": str(exc),
            },
        )

    await record_dispatch_attempt(
        channel="email",
        target=to,
        caller_name=caller_name,
        charge=charge,
        firm_name=str(firm.get("firm_name") or ""),
    )
    return json.dumps(
        {
            "status": "sent",
            "firm_id": firm_id,
            "firm_name": firm.get("firm_name"),
            "to": to,
            "caller_name": caller_name,
            "facility": facility.name,
            "facility_phone": facility.phone,
            "reply_email": dispatch_inbox,
            "message_preview": intake_message[:200],
            **send_result,
        },
    )


TOOL_HANDLERS: Final[dict[str, ToolHandler]] = {
    "moss_find_lawyers": moss_find_lawyers,
    "email_attorneys": email_attorneys,
}

# Tool whose successful invocation means an actual dispatch happened.
# Calling it a second time on the same caller is a model regression
# (post-dispatch turn should answer from context, not re-fire) and gets
# short-circuited by ``_dispatch_already_done``.
_DISPATCH_TOOL_NAMES: Final[frozenset[str]] = frozenset({"email_attorneys"})


def _dispatch_already_done() -> bool:
    """Return True if a dispatch tool fired in a *prior* turn.

    Single-caller demo — we deliberately ignore the ``call_id`` boundary.
    Any prior dispatch (from this call OR an earlier call) means dispatch
    is already done; on a follow-up call the agent should answer from
    recalled context, not re-fire attorneys.

    Filters by ``ts < current_turn_started_at`` so parallel dispatch tools
    within the SAME turn don't false-positive each other.
    """
    if not TOOL_CALL_LOG_PATH.exists():
        return False
    turn_start = current_turn_started_at.get()
    try:
        with TOOL_CALL_LOG_PATH.open() as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = cast("object", json.loads(line))
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                row = cast("dict[str, object]", entry)
                if row.get("tool") not in _DISPATCH_TOOL_NAMES:
                    continue
                ts = row.get("ts")
                if isinstance(ts, (int, float)) and ts < turn_start:
                    return True
    except OSError:
        return False
    return False


def clear_tool_call_log() -> None:
    """Delete the tool-call log so a fresh demo run starts clean.

    Called from ``server.lifespan`` on startup and from the
    ``/api/reset-memory`` endpoint, alongside ``memory.clear_memory``.
    Failures are logged but never raised.
    """
    try:
        TOOL_CALL_LOG_PATH.unlink(missing_ok=True)
        logger.info("tool-call log cleared")
    except OSError:
        logger.exception("tool-call log clear failed")


def _log_tool_call(name: str, args: dict[str, object], result: str) -> None:
    """Append one tool-call record to ``TOOL_CALL_LOG_PATH``, keyed by call_id.

    Skipped silently if no call_id is set (e.g., standalone import / unit
    test). Failures are logged but never re-raised — observability must not
    break the live voice path.
    """
    call_id = current_call_id.get()
    if not call_id:
        return
    record = {
        "call_id": call_id,
        "tool": name,
        "args": args,
        "result_chars": len(result),
        "ts": time.time(),
    }
    try:
        TOOL_CALL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with TOOL_CALL_LOG_PATH.open("a") as fh:
            _ = fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        logger.exception("tool_call log write failed for tool=%s", name)


async def run_tool(name: str, args: dict[str, object]) -> str:
    """Dispatch one tool call by name and stamp the tool-call log.

    Unknown tool names return a structured error string the model can read
    and recover from, rather than raising. Every call (success, error, or
    unknown-tool) is logged so the eval harness can grade ``expected_dispatch``.
    """
    started = time.perf_counter()
    record_tool_status(name=name, status="started", args=args)
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        result = json.dumps({"error": f"unknown tool: {name}"})
        _log_tool_call(name, args, result)
        record_tool_status(
            name=name,
            status="unknown",
            args=args,
            result=result,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return result

    # Hard guard: refuse a second dispatch on the same call. Gemini
    # sometimes tries to re-fire email_attorneys on
    # benign caller turns ("ok", "thanks"); the prompt says don't but the
    # model isn't always faithful. Short-circuit here so the call really
    # can't double-dispatch even if the model misbehaves.
    if name in _DISPATCH_TOOL_NAMES and _dispatch_already_done():
        result = json.dumps(
            {
                "error": "dispatch_already_completed",
                "note": (
                    "Dispatch already fired for this call. Do not call any "
                    "dispatch tools again — answer from the previous moss "
                    "result and your prior confirmation."
                ),
            },
        )
        logger.warning("run_tool short-circuited %s — dispatch already done", name)
        _log_tool_call(name, args, result)
        record_tool_status(
            name=name,
            status="rejected_already_dispatched",
            args=args,
            result=result,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return result

    try:
        result = await handler(args)
    except Exception as exc:
        logger.exception("tool %s failed", name)
        result = json.dumps({"error": f"tool {name} raised: {exc}"})
    _log_tool_call(name, args, result)
    record_tool_status(
        name=name,
        status="completed",
        args=args,
        result=result,
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return result
