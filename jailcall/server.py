"""JailCall webhook server entry point.

Run with either:

* ``uv run python -m jailcall.server`` (uses the constants below), or
* ``uv run uvicorn jailcall.server:app --host 127.0.0.1 --port 5321 --reload``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, cast

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from jailcall.call_context import current_call_id
from jailcall.config import require_env
from jailcall.controller import generate as run_agent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_ = load_dotenv()

HOST: Final[str] = "127.0.0.1"
PORT: Final[int] = 5321
MAX_TIMESTAMP_SKEW_SECONDS: Final[int] = 300
CAPTURES_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "evals" / "captures"

logger = logging.getLogger("jailcall.webhook")

app = FastAPI(title="JailCall")


def _verify_signature(
    *,
    secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
) -> bool:
    """Verify the AgentPhone HMAC-SHA256 webhook signature.

    Recipe from ``TOOLS.md`` (AgentPhone → Webhook security):
    ``signed = f"{timestamp}.".encode() + body``,
    ``expected = hmac_sha256(secret, signed).hexdigest()``,
    compared in constant time against the ``sha256=<hex>`` header value.

    Args:
        secret: Shared webhook secret (``whsec_...``) from ``POST /v1/webhooks``.
        timestamp: ``X-Webhook-Timestamp`` header (Unix seconds, as string).
        body: Raw request body bytes (must be the exact bytes received).
        signature: ``X-Webhook-Signature`` header, formatted ``sha256=<hex>``.

    Returns:
        ``True`` if the signature matches.
    """
    signed = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def _ndjson(payload: dict[str, object]) -> bytes:
    """Encode a JSON object as one newline-terminated NDJSON record.

    Args:
        payload: JSON-serialisable mapping to emit.

    Returns:
        UTF-8 bytes ending with a newline, suitable for ``application/x-ndjson``.
    """
    return json.dumps(payload).encode() + b"\n"


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


def _write_capture(
    *,
    record: dict[str, object],
    delivery_id: str,
    event: str,
) -> None:
    """Persist a single webhook delivery (headers + body + response) for eval fixtures.

    Used as ground truth when authoring ``evals/replay.py`` — the replay adapter
    synthesizes payloads of the same shape AgentPhone actually sends. Failures
    are logged but never raised — capture must not break the live voice path.
    """
    try:
        CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in delivery_id)[:24]
        safe_event = event.replace(".", "_")
        path = CAPTURES_DIR / f"{int(time.time() * 1000)}-{safe_event}-{safe_id}.json"
        _ = path.write_text(json.dumps(record, indent=2, default=str))
    except OSError:
        logger.exception("capture write failed for delivery=%s", delivery_id)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Return health status for monitoring probes."""
    return {"status": "ok"}


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
    ``agent.message`` events streams an NDJSON reply (interim chunk first to
    avoid silence, then the spoken response).

    Args:
        request: FastAPI request — used for raw body access and JSON parsing.
        x_webhook_signature: ``sha256=<hex>`` HMAC-SHA256 of the body.
        x_webhook_timestamp: Unix-seconds timestamp of delivery.
        x_webhook_id: Unique delivery id (used for idempotency / logging).
        x_webhook_event: Event type (e.g. ``agent.message``, ``agent.call_ended``).

    Raises:
        HTTPException: 401 on bad signature, malformed timestamp, or
            timestamp older than ``MAX_TIMESTAMP_SKEW_SECONDS``.

    Returns:
        ``{"ok": true}`` for non-voice events, or a ``StreamingResponse`` of
        NDJSON records for voice ``agent.message`` events.
    """
    secret = require_env("AGENTPHONE_WEBHOOK_SECRET")

    body = await request.body()
    if not _verify_signature(
        secret=secret,
        timestamp=x_webhook_timestamp,
        body=body,
        signature=x_webhook_signature,
    ):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    try:
        delivered_at = int(x_webhook_timestamp)
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
    event = cast("dict[str, object]", parsed)

    channel = event.get("channel")
    logger.info(
        "delivery=%s event=%s channel=%s",
        x_webhook_id,
        x_webhook_event,
        channel,
    )

    capture: dict[str, object] = {
        "received_at": time.time(),
        "headers": {
            "X-Webhook-Signature": x_webhook_signature,
            "X-Webhook-Timestamp": x_webhook_timestamp,
            "X-Webhook-ID": x_webhook_id,
            "X-Webhook-Event": x_webhook_event,
        },
        "body": event,
        "response_chunks": [],
    }

    if x_webhook_event != "agent.message" or channel != "voice":
        response_payload: dict[str, object] = {"ok": True}
        cast("list[object]", capture["response_chunks"]).append(response_payload)
        _write_capture(record=capture, delivery_id=x_webhook_id, event=x_webhook_event)
        return {"ok": True}

    transcript = _extract_voice_transcript(event)
    recent_history = _extract_recent_history(event)
    call_id = _extract_call_id(event)
    logger.info(
        "voice transcript: %r call_id=%s history_len=%d",
        transcript,
        call_id,
        len(recent_history),
    )

    async def stream_reply() -> AsyncIterator[bytes]:
        """Speak interim, run the Gemini tool loop, speak final, persist capture."""
        # Stamp the call id into the asyncio context so tool calls in this
        # request log themselves under the correct call_id. ContextVar
        # automatically propagates across awaits within this task.
        token = current_call_id.set(call_id)
        try:
            interim: dict[str, object] = {"text": "One moment.", "interim": True}
            cast("list[object]", capture["response_chunks"]).append(interim)
            yield _ndjson(interim)

            text = await run_agent(recent_history) if recent_history else "I'm here. Go ahead."
            final: dict[str, object] = {"text": text}
            cast("list[object]", capture["response_chunks"]).append(final)
            yield _ndjson(final)

            _write_capture(record=capture, delivery_id=x_webhook_id, event=x_webhook_event)
        finally:
            current_call_id.reset(token)

    return StreamingResponse(stream_reply(), media_type="application/x-ndjson")


def main() -> None:
    """Run the dev server on ``HOST:PORT``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
