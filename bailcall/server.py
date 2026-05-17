"""BailCall webhook server entry point.

Run with either:

* ``uv run python -m bailcall.server`` (uses the constants below), or
* ``uv run uvicorn bailcall.server:app --host 127.0.0.1 --port 5321 --reload``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import TYPE_CHECKING, Annotated, Final, cast

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from bailcall.config import require_env

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_ = load_dotenv()

HOST: Final[str] = "127.0.0.1"
PORT: Final[int] = 5321
MAX_TIMESTAMP_SKEW_SECONDS: Final[int] = 300

logger = logging.getLogger("bailcall.webhook")

app = FastAPI(title="BailCall")


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

    parsed = cast("object", json.loads(body))
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

    if x_webhook_event != "agent.message" or channel != "voice":
        return {"ok": True}

    transcript = ""
    data_obj = event.get("data")
    if isinstance(data_obj, dict):
        data = cast("dict[str, object]", data_obj)
        transcript_raw = data.get("transcript")
        if isinstance(transcript_raw, str):
            transcript = transcript_raw.strip()
    logger.info("voice transcript: %r", transcript)

    async def generate() -> AsyncIterator[bytes]:
        """Yield interim chunk first, then the scripted reply."""
        yield _ndjson({"text": "One moment.", "interim": True})
        reply = (
            f"You said: {transcript}. The full agent is still being wired up."
            if transcript
            else "I'm here. Go ahead."
        )
        yield _ndjson({"text": reply})

    return StreamingResponse(generate(), media_type="application/x-ndjson")


def main() -> None:
    """Run the dev server on ``HOST:PORT``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
