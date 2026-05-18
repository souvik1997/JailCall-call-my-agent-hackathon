"""JailCall webhook server.

Receives AgentPhone webhooks, runs the Gemini tool-call loop in
``jailcall.controller``, and streams sentence-by-sentence NDJSON back to
AgentPhone. Also hosts the local live-call dashboard at ``/`` (HTML in
``jailcall/static/``, in-memory state from ``jailcall.dashboard``).

Run with either:

* ``uv run python -m jailcall.server`` (uses the constants below), or
* ``uv run uvicorn jailcall.server:app --host 127.0.0.1 --port 5321 --reload``.

Layout of this module (top to bottom):

1. Imports + module constants + ``logger`` + ``lifespan``
2. FastAPI ``app`` + static mount
3. Tiny helpers: signature verify, NDJSON encode, capture write
4. Body-parse helpers: ``_extract_*`` + ``_authenticate_and_parse``
5. ``_WebhookContext`` dataclass + ``_build_webhook_context`` + ``_record_response_chunk``
6. Event handlers: non-voice, voice (streamed)
7. Routes: ``/healthz``, ``/``, ``/api/dashboard``, ``/webhook``
8. ``main()`` for ``python -m`` invocation
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, cast

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from jailcall.call_context import current_call_id, current_turn_started_at
from jailcall.config import require_env
from jailcall.controller import generate as run_agent
from jailcall.dashboard import (
    record_agentphone_chunk,
    record_call_status,
    record_inbound_reply,
    record_webhook_delivery,
    snapshot,
)
from jailcall.memory import (
    clear_memory,
    recall_context,
    record_attorney_reply,
    record_caller_utterance,
)
from jailcall.moss import get_moss_client
from jailcall.tools import (
    clear_agentmail_inbox,
    clear_inbox_sync_state,
    clear_moss_result_cache,
    clear_tool_call_log,
    sync_inbox_into_dashboard,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

_ = load_dotenv()

HOST: Final[str] = "127.0.0.1"
PORT: Final[int] = 5321
MAX_TIMESTAMP_SKEW_SECONDS: Final[int] = 300
DEFAULT_MOSS_INDEX: Final[str] = "jailcall-lawyers"
ROOT: Final[Path] = Path(__file__).resolve().parent.parent
CAPTURES_DIR: Final[Path] = ROOT / "evals" / "captures"
STATIC_DIR: Final[Path] = Path(__file__).resolve().parent / "static"

# Hold strong references to fire-and-forget tasks so the GC doesn't drop
# them mid-flight. See the asyncio.create_task docs (RUF006) — tasks that
# nobody references can be collected before they complete.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

# Call_ids we've already polled the AgentMail inbox for. AgentPhone's
# webhook fires once per *turn* but we only want to sync the inbox once
# per *call* — the rest of the call's turns reuse whatever Supermemory
# captured at call start (plus any in-call replies the webhook pushes).
_synced_call_ids: set[str] = set()

logger = logging.getLogger("jailcall.webhook")


# ─── lifespan ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Startup/shutdown hooks. Loads Moss + clears Supermemory + tool log."""
    index_name = os.environ.get("MOSS_INDEX_NAME", DEFAULT_MOSS_INDEX)
    await get_moss_client().load_index(index_name)
    logger.info("loaded Moss index %s", index_name)
    # Single-caller demo — start every run from a blank slate so previous
    # calls don't bleed into this one. Wipe both stores: Supermemory (used
    # for caller recall) and the local tool-call log (used by the hard
    # guard against re-dispatch).
    await clear_memory()
    await clear_agentmail_inbox()
    clear_tool_call_log()
    clear_moss_result_cache()
    clear_inbox_sync_state()
    _synced_call_ids.clear()
    yield
    # No shutdown work today.


# StaticFiles fails at startup if the directory doesn't exist — make sure it does.
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="JailCall", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ─── tiny helpers ──────────────────────────────────────────────────────────


def _verify_signature(*, secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """Verify the AgentPhone HMAC-SHA256 webhook signature.

    Recipe from ``TOOLS.md`` (AgentPhone → Webhook security):
    ``signed = f"{timestamp}.".encode() + body``,
    ``expected = hmac_sha256(secret, signed).hexdigest()``,
    compared in constant time against the ``sha256=<hex>`` header value.
    """
    signed = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def _ndjson(payload: dict[str, object]) -> bytes:
    """Encode a JSON object as one newline-terminated NDJSON record."""
    return json.dumps(payload).encode() + b"\n"


def _write_capture(*, record: dict[str, object], delivery_id: str, event: str) -> None:
    """Persist one webhook delivery (headers + body + response) to disk.

    Useful as a payload-shape reference for the interactive REPL
    (``evals/interactive.py``) and for offline inspection after live calls.
    Failures are logged but never raised — capture must not break the
    live voice path.
    """
    try:
        CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in delivery_id)[:24]
        safe_event = event.replace(".", "_")
        path = CAPTURES_DIR / f"{int(time.time() * 1000)}-{safe_event}-{safe_id}.json"
        _ = path.write_text(json.dumps(record, indent=2, default=str))
    except OSError:
        logger.exception("capture write failed for delivery=%s", delivery_id)


# ─── body-parse helpers ────────────────────────────────────────────────────


def _extract_call_id(event: dict[str, object]) -> str:
    """Pull ``data.callId`` out of a voice webhook payload."""
    data_obj = event.get("data")
    if not isinstance(data_obj, dict):
        return ""
    data = cast("dict[str, object]", data_obj)
    call_id = data.get("callId")
    return call_id if isinstance(call_id, str) else ""


def _extract_voice_transcript(event: dict[str, object]) -> str:
    """Pull ``data.transcript`` out of an ``agent.message`` voice payload."""
    data_obj = event.get("data")
    if not isinstance(data_obj, dict):
        return ""
    data = cast("dict[str, object]", data_obj)
    transcript_raw = data.get("transcript")
    if not isinstance(transcript_raw, str):
        return ""
    return transcript_raw.strip()


def _extract_recent_history(event: dict[str, object]) -> list[dict[str, str]]:
    """Pull ``recentHistory[]`` out of the webhook payload, defensively.

    AgentPhone sends ``[{"role": "agent" | "user", "content": str}, ...]``.
    Entries with non-string role/content are dropped.
    """
    raw = event.get("recentHistory")
    if not isinstance(raw, list):
        return []
    entries = cast("list[object]", raw)
    out: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_dict = cast("dict[str, object]", entry)
        role = entry_dict.get("role")
        content = entry_dict.get("content")
        if isinstance(role, str) and isinstance(content, str):
            out.append({"role": role, "content": content})
    return out


def _authenticate_and_parse(
    *,
    body: bytes,
    signature: str,
    timestamp: str,
    secret: str,
) -> dict[str, object]:
    """Verify HMAC + freshness, parse JSON body. Raises ``HTTPException`` on failure.

    Returns the parsed event payload as a ``dict[str, object]``.
    """
    if not _verify_signature(secret=secret, timestamp=timestamp, body=body, signature=signature):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    try:
        delivered_at = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid timestamp") from exc
    if abs(time.time() - delivered_at) > MAX_TIMESTAMP_SKEW_SECONDS:
        raise HTTPException(status_code=401, detail="stale webhook")

    try:
        parsed = cast("object", json.loads(body))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="webhook body must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="webhook body must be a JSON object")
    return cast("dict[str, object]", parsed)


# ─── webhook context + chunk recording ─────────────────────────────────────


@dataclass
class _WebhookContext:
    """Everything the webhook handler needs to thread to downstream helpers."""

    event_name: str
    delivery_id: str
    channel: object
    event: dict[str, object]
    call_id: str
    transcript: str
    recent_history: list[dict[str, str]]
    capture: dict[str, object] = field(default_factory=dict)


def _build_webhook_context(
    *,
    event: dict[str, object],
    delivery_id: str,
    event_name: str,
    headers: dict[str, str],
) -> _WebhookContext:
    """Pull every field we need off the parsed event into one bundle."""
    transcript = _extract_voice_transcript(event)
    recent_history = _extract_recent_history(event)
    call_id = _extract_call_id(event) or delivery_id
    capture: dict[str, object] = {
        "received_at": time.time(),
        "headers": dict(headers),
        "body": event,
        "response_chunks": [],
    }
    return _WebhookContext(
        event_name=event_name,
        delivery_id=delivery_id,
        channel=event.get("channel"),
        event=event,
        call_id=call_id,
        transcript=transcript,
        recent_history=recent_history,
        capture=capture,
    )


def _record_response_chunk(
    ctx: _WebhookContext,
    chunk: dict[str, object],
    *,
    interim: bool = False,
) -> None:
    """Append a chunk to the capture AND record it for the dashboard.

    Collapses the three-step pattern (append to capture / record to
    dashboard / [yield is done by caller]) so the streaming generator
    stays readable.
    """
    cast("list[object]", ctx.capture["response_chunks"]).append(chunk)
    text = chunk.get("text") if isinstance(chunk.get("text"), str) else ""
    record_agentphone_chunk(
        call_id=ctx.call_id,
        text=cast("str", text),
        interim=interim,
        payload=chunk,
    )


# ─── event handlers ────────────────────────────────────────────────────────


def _handle_non_voice_event(ctx: _WebhookContext) -> dict[str, bool]:
    """Acknowledge any non-voice or non-message event with ``{"ok": true}``.

    Side effects: dashboard state for ``agent.call_ended``, capture write.
    """
    response_payload: dict[str, object] = {"ok": True}
    cast("list[object]", ctx.capture["response_chunks"]).append(response_payload)
    if ctx.event_name == "agent.call_ended":
        record_call_status(
            call_id=ctx.call_id,
            status="ended",
            event_name=ctx.event_name,
            detail="AgentPhone reported the call ended.",
        )
    _write_capture(record=ctx.capture, delivery_id=ctx.delivery_id, event=ctx.event_name)
    return {"ok": True}


async def _voice_response_stream(ctx: _WebhookContext) -> AsyncIterator[bytes]:
    """Yield NDJSON-encoded sentence chunks for the voice response.

    Opens with an immediate ``"One moment."`` interim chunk (covers
    Gemini's TTFT + any leading tool-call latency), then streams the
    controller's sentences. Every emitted sentence is also flagged
    ``interim: true`` *except the last* — that gives AgentPhone a clean
    "this is the final answer" signal at the end of the turn.

    The ``current_call_id`` ContextVar is set/reset around the controller
    invocation so ``jailcall.tools.run_tool`` can stamp the tool-call log
    with the right ``call_id``.
    """
    call_token = current_call_id.set(ctx.call_id)
    turn_token = current_turn_started_at.set(time.time())
    try:
        interim: dict[str, object] = {"text": "One moment.", "interim": True}
        _record_response_chunk(ctx, interim, interim=True)
        yield _ndjson(interim)

        if not ctx.recent_history:
            fallback: dict[str, object] = {"text": "I'm here. Go ahead."}
            _record_response_chunk(ctx, fallback)
            yield _ndjson(fallback)
        else:
            # Prepend a "prior context" agent turn so the model recognises
            # a returning caller, remembers their charge, and can surface
            # attorney replies that landed between calls. Failures here
            # never block the voice path — fall back to the raw history.
            augmented_history = await _augment_history_with_recall(ctx.recent_history)
            # One-sentence lookahead so the *last* sentence isn't marked interim.
            pending: str | None = None
            async for sentence in run_agent(augmented_history):
                if pending is not None:
                    chunk: dict[str, object] = {"text": pending, "interim": True}
                    _record_response_chunk(ctx, chunk, interim=True)
                    yield _ndjson(chunk)
                pending = sentence
            if pending is not None:
                final: dict[str, object] = {"text": pending}
                _record_response_chunk(ctx, final)
                yield _ndjson(final)

        _write_capture(record=ctx.capture, delivery_id=ctx.delivery_id, event=ctx.event_name)
    finally:
        current_turn_started_at.reset(turn_token)
        current_call_id.reset(call_token)


def _categorise_memories(
    memories: list[str],
) -> dict[str, list[str]]:
    """Bucket raw Supermemory entries by kind so recall is easy to read."""
    out: dict[str, list[str]] = {
        "utterances": [],
        "dispatches": [],
        "replies": [],
        "other": [],
    }
    for raw in memories:
        first = raw.split("\n", 1)[0]
        if first.startswith("[call=") and "caller said:" in first:
            out["utterances"].append(first.split("caller said:", 1)[1].strip())
        elif first.startswith("DISPATCH ("):
            out["dispatches"].append(first.strip())
        elif "Inbound attorney reply" in first or first.startswith("Attorney reply"):
            out["replies"].append(raw.strip())
        else:
            out["other"].append(raw.strip())
    return out


_NAME_UTTERANCE_INDEX: Final[int] = 1  # 2nd utterance (0=consent, 1=name)
_MAX_PLAUSIBLE_NAME_LEN: Final[int] = 60


def _guess_caller_name(utterances: list[str]) -> str:
    """Heuristic: the caller's 2nd utterance is almost always their name.

    First utterance is consent ("yes" / "yeah" / "please help"). Second
    is the name when the agent's first follow-up is "what's your name?".
    Returns empty string if there's no plausible candidate.
    """
    if len(utterances) <= _NAME_UTTERANCE_INDEX:
        return ""
    candidate = utterances[_NAME_UTTERANCE_INDEX].strip().rstrip(".").strip()
    # Skip if it looks like a charge or a refusal rather than a name.
    if not candidate or len(candidate) > _MAX_PLAUSIBLE_NAME_LEN:
        return ""
    return candidate


def _build_prior_context_block(memories: list[str]) -> str:
    """Structure raw Supermemory entries into a single readable agent turn.

    Sections (only included when non-empty):
      * Header with imperative instructions for the model.
      * SUGGESTED OPENER: a pre-baked first sentence the model can copy.
      * KNOWN CALLER FACTS: the caller's utterances across prior calls.
      * DISPATCH HISTORY: every attorney we've contacted, oldest first.
      * ATTORNEY REPLIES: full body of each inbound reply.
      * OTHER NOTES: anything we couldn't categorise.
    """
    cats = _categorise_memories(memories)
    has_dispatch = bool(cats["dispatches"])
    has_reply = bool(cats["replies"])
    caller_name = _guess_caller_name(cats["utterances"])
    name_token = caller_name or "there"

    header: list[str] = [
        "[Prior context for this caller — pulled from Supermemory at call start.]",
        "",
        "ON THIS TURN you MUST personalise the call from this context. Specifically:",
        " 1. If KNOWN CALLER FACTS has a name, greet the caller by name in your FIRST"
        + " sentence.",
        " 2. If ATTORNEY REPLIES is non-empty, your FIRST sentence must announce the"
        + " reply news — read the BODY to identify the firm and the substance."
        + " Do NOT trust the From header.",
        " 3. If DISPATCH HISTORY is non-empty, you have already dispatched —"
        + " DO NOT call moss_find_lawyers or email_attorneys again, and DO NOT ask"
        + " the caller if they want you to contact an attorney (you already did).",
        " 4. Never read this block aloud verbatim. Use it to speak naturally.",
    ]

    # Pre-bake the model's first sentence so it just has to copy/paraphrase.
    if has_reply:
        header.append("")
        header.append(
            "SUGGESTED OPENER (use this exact structure as your VERY FIRST sentence,"
            + " substituting the firm + substance from the ATTORNEY REPLIES section):",
        )
        header.append(
            f'  "Hey {name_token} — good news, [firm name from the reply body] got back'
            + ' to you. They said [paraphrase the substance from the body]."',
        )
    elif has_dispatch:
        header.append("")
        header.append(
            "SUGGESTED OPENER (use this exact structure as your VERY FIRST sentence,"
            + " substituting the firm names from DISPATCH HISTORY):",
        )
        header.append(
            f'  "Hey {name_token} — back again. Earlier I reached out to'
            + " [list the firms from DISPATCH HISTORY] for you. Haven't heard back yet."
            + " Anything I can help you with — questions about arraignment, bail, or"
            + ' what to say to police?"',
        )
    elif caller_name:
        header.append("")
        header.append(
            f'SUGGESTED OPENER: "Hey {name_token} — welcome back. How can I help?"',
        )

    sections: list[str] = []
    if cats["utterances"]:
        sections.append("KNOWN CALLER FACTS (caller utterances across prior calls):")
        sections.extend(f'  - "{u}"' for u in cats["utterances"][-12:])
        sections.append("")
    if cats["dispatches"]:
        sections.append("DISPATCH HISTORY (attorneys already contacted — DO NOT re-dispatch):")
        sections.extend(f"  - {r}" for r in cats["dispatches"])
        sections.append("")
    if cats["replies"]:
        sections.append("ATTORNEY REPLIES (read the BODY to identify firm + substance):")
        for reply in cats["replies"]:
            sections.append(reply)
            sections.append("")
    if cats["other"]:
        sections.append("OTHER NOTES:")
        sections.extend(f"  - {entry}" for entry in cats["other"])

    return "\n".join([*header, "", *sections]).rstrip() + "\n"


async def _augment_history_with_recall(
    recent_history: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Prepend a structured "prior context" agent turn from Supermemory.

    Pulls recent memories under the demo container tag and groups them
    into KNOWN CALLER FACTS / DISPATCH HISTORY / ATTORNEY REPLIES so the
    model doesn't have to parse a flat bullet list — it reads section
    headers and acts. Returns ``recent_history`` unchanged if recall
    fails or nothing's in memory.
    """
    try:
        prior = await recall_context(limit=40)
    except Exception:
        logger.exception("recall_context failed; serving turn without prior context")
        return recent_history
    if not prior:
        return recent_history
    context_turn: dict[str, str] = {
        "role": "agent",
        "content": _build_prior_context_block(prior),
    }
    return [context_turn, *recent_history]


# ─── routes ────────────────────────────────────────────────────────────────


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Return health status for monitoring probes."""
    return {"status": "ok"}


@app.get("/", response_class=FileResponse)
def dashboard() -> FileResponse:
    """Serve the local live-call dashboard."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/dashboard")
def dashboard_state() -> dict[str, object]:
    """Return current in-memory call, transcript, and dispatch state."""
    return snapshot()


@app.post("/api/reset-memory")
async def reset_memory() -> dict[str, object]:
    """Bulk-delete every memory + clear the tool-call log.

    Same effect as the startup clear in ``lifespan`` — exposed as an
    endpoint so the demo can be reset between takes without restarting
    the server.
    """
    await clear_memory()
    await clear_agentmail_inbox()
    clear_tool_call_log()
    clear_moss_result_cache()
    clear_inbox_sync_state()
    _synced_call_ids.clear()
    return {"ok": True}


@app.post("/webhook/agentmail")
async def agentmail_webhook(request: Request) -> dict[str, bool]:
    """Ingest one inbound attorney email into Supermemory.

    AgentMail's exact webhook payload shape isn't documented in TOOLS.md
    yet, so this handler accepts a flexible JSON object with the common
    fields (``from``, ``subject``, ``text``/``body``/``snippet``). Real
    signing/verification should be wired in once the AgentMail portal
    is configured — until then this is unauthenticated.
    """
    try:
        parsed = cast("object", await request.json())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="expected JSON object")
    body = cast("dict[str, object]", parsed)

    def _pick(*keys: str) -> str:
        for key in keys:
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    sender = _pick("from", "sender", "from_address", "fromAddress")
    subject = _pick("subject")
    text = _pick("text", "body", "snippet", "extracted_text", "extractedText")

    logger.info("agentmail webhook ingested from=%s subject=%r", sender or "?", subject)

    # Surface the reply on the live dashboard immediately (sync, in-memory).
    record_inbound_reply(sender=sender, subject=subject, text=text)

    # Fire-and-forget the durable Supermemory write so it shows up in recall
    # on the caller's next call.
    task = asyncio.create_task(
        record_attorney_reply(sender=sender, subject=subject, text=text),
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return {"ok": True}


@app.post("/webhook", response_model=None)
async def webhook(
    request: Request,
    x_webhook_signature: Annotated[str, Header()],
    x_webhook_timestamp: Annotated[str, Header()],
    x_webhook_id: Annotated[str, Header()],
    x_webhook_event: Annotated[str, Header()],
) -> StreamingResponse | dict[str, bool]:
    """Receive an AgentPhone webhook delivery.

    Verifies HMAC-SHA256 signature, drops stale deliveries, and for voice
    ``agent.message`` events streams an NDJSON reply (interim chunk first
    to avoid silence, then sentence-by-sentence streaming from the
    Gemini tool-call loop).

    Returns ``{"ok": true}`` for non-voice events; ``StreamingResponse``
    (NDJSON) for voice events.
    """
    secret = require_env("AGENTPHONE_WEBHOOK_SECRET")
    body = await request.body()
    event = _authenticate_and_parse(
        body=body,
        signature=x_webhook_signature,
        timestamp=x_webhook_timestamp,
        secret=secret,
    )

    ctx = _build_webhook_context(
        event=event,
        delivery_id=x_webhook_id,
        event_name=x_webhook_event,
        headers={
            "X-Webhook-Signature": x_webhook_signature,
            "X-Webhook-Timestamp": x_webhook_timestamp,
            "X-Webhook-ID": x_webhook_id,
            "X-Webhook-Event": x_webhook_event,
        },
    )
    logger.info(
        "delivery=%s event=%s channel=%s call_id=%s transcript=%r",
        ctx.delivery_id,
        ctx.event_name,
        ctx.channel,
        ctx.call_id,
        ctx.transcript,
    )
    record_webhook_delivery(
        call_id=ctx.call_id,
        delivery_id=ctx.delivery_id,
        event_name=ctx.event_name,
        channel=ctx.channel,
        transcript=ctx.transcript,
        recent_history=ctx.recent_history,
    )

    if ctx.event_name != "agent.message" or ctx.channel != "voice":
        return _handle_non_voice_event(ctx)

    # First turn of a new call: synchronously pull the AgentMail inbox so
    # any attorney replies that landed between calls are in Supermemory
    # before the controller's recall fires. The webhook push is the
    # primary path; this synchronous sync is the safety net for when
    # the webhook hasn't fired (tunnel down, webhook unregistered, etc.).
    if ctx.call_id and ctx.call_id not in _synced_call_ids:
        _synced_call_ids.add(ctx.call_id)
        try:
            new_replies = await sync_inbox_into_dashboard()
            if new_replies:
                logger.info(
                    "synced %d inbox message(s) at call start (call_id=%s)",
                    new_replies,
                    ctx.call_id,
                )
        except Exception:
            logger.exception("inbox sync failed at call start (call_id=%s)", ctx.call_id)

    # Fire-and-forget the Supermemory write so voice latency doesn't
    # depend on Supermemory's API. The single-caller demo writes every
    # caller utterance under the hardcoded container tag.
    if ctx.transcript:
        task = asyncio.create_task(record_caller_utterance(ctx.call_id, ctx.transcript))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    return StreamingResponse(_voice_response_stream(ctx), media_type="application/x-ndjson")


# ─── entry point ───────────────────────────────────────────────────────────


def main() -> None:
    """Run the dev server on ``HOST:PORT``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
