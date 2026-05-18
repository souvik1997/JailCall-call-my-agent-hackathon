"""Interactive REPL — type as if you're speaking to the agent on the phone.

POSTs each line you type as a signed AgentPhone-shape ``agent.message``
webhook against the running server. The NDJSON reply streams back
sentence-by-sentence and prints to your terminal as it lands — same code
path the live phone call exercises.

Usage::

    uv run python -m evals.interactive
    uv run python -m evals.interactive --url http://127.0.0.1:5321/webhook

Slash commands during the session:

* ``/quit``    or  Ctrl-D   — hang up (POSTs ``agent.call_ended``)
* ``/reset``               — start a fresh call (new ``call_id``)
* ``/tools``               — list every tool call fired on this call
* ``/history``             — print the accumulated ``recentHistory``
* ``/help``                — show this list
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
TOOL_CALL_LOG: Final[Path] = ROOT / "evals" / "last_run" / "tool_calls.jsonl"
DEFAULT_URL: Final[str] = "http://127.0.0.1:5321/webhook"

# Locked beginMessage — must match SPEC.md → Voice script → Opening verbatim
# (and the agent's beginMessage configured in the AgentPhone portal).
BEGIN_MESSAGE: Final[str] = (
    "You've reached JailCall. I can help connect you with a criminal "
    "defense attorney in the Bay Area and walk you through what happens "
    "next. What's going on?"
)

# Fake-but-realistic ids for synthesized payloads. Real captures use
# the same cuid-ish format; nothing on our server checks them.
AGENT_ID: Final[str] = "cmp1rq9gf04ocdex677zw30m0"
NUMBER_ID: Final[str] = "cmpa0373x01vujz00iuwg82a9"
FROM_NUMBER: Final[str] = "+15551234567"
TO_NUMBER: Final[str] = "+15707347169"

# ANSI styling — kept minimal so output is still readable when piped.
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
MAGENTA = "\x1b[35m"
RED = "\x1b[31m"


def _new_call_id() -> str:
    """Mint a cuid-shaped synthetic call id."""
    return f"cmpa{uuid.uuid4().hex[:20]}"


def _sign(secret: bytes, timestamp: str, body: bytes) -> str:
    """Reproduce the AgentPhone signature recipe used by jailcall.server."""
    mac = hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _agent_message_payload(
    *,
    call_id: str,
    transcript: str,
    recent_history: list[dict[str, str]],
) -> dict[str, object]:
    """Build a payload identical in shape to a real ``agent.message`` delivery."""
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "event": "agent.message",
        "channel": "voice",
        "timestamp": now_iso,
        "agentId": AGENT_ID,
        "data": {
            "callId": call_id,
            "numberId": NUMBER_ID,
            "from": FROM_NUMBER,
            "to": TO_NUMBER,
            "contact": None,
            "status": "in-progress",
            "transcript": transcript,
            "direction": "inbound",
        },
        "conversationState": None,
        "recentHistory": recent_history,
    }


def _call_ended_payload(
    *,
    call_id: str,
    full_transcript: list[dict[str, str]],
    started_at: float,
    ended_at: float,
) -> dict[str, object]:
    """Build a payload identical in shape to a real ``agent.call_ended`` delivery."""

    def to_iso(t: float) -> str:
        return datetime.fromtimestamp(t, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    return {
        "event": "agent.call_ended",
        "channel": "voice",
        "timestamp": to_iso(ended_at),
        "agentId": AGENT_ID,
        "data": {
            "callId": call_id,
            "numberId": NUMBER_ID,
            "from": FROM_NUMBER,
            "to": TO_NUMBER,
            "contact": None,
            "direction": "inbound",
            "status": "completed",
            "startedAt": to_iso(started_at),
            "endedAt": to_iso(ended_at),
            "durationSeconds": round(ended_at - started_at, 2),
            "disconnectionReason": "user_hangup",
            "transcript": full_transcript,
            "summary": None,
            "userSentiment": None,
            "callSuccessful": None,
        },
    }


def _stream_post(
    *,
    client: httpx.Client,
    url: str,
    secret: bytes,
    delivery_id: str,
    event_type: str,
    payload: dict[str, object],
    show_chunks: bool,
) -> tuple[int, list[dict[str, object]]]:
    """POST a signed delivery and (optionally) print each NDJSON record as it lands.

    Returns the final HTTP status and the ordered list of decoded chunks
    so the caller can update ``recent_history``.
    """
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": _sign(secret, ts, body),
        "X-Webhook-Timestamp": ts,
        "X-Webhook-ID": delivery_id,
        "X-Webhook-Event": event_type,
    }

    chunks: list[dict[str, object]] = []
    printed_prefix = False
    with client.stream("POST", url, content=body, headers=headers, timeout=120.0) as resp:
        for raw in resp.iter_lines():
            line = raw.strip()
            if not line:
                continue
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(decoded, dict):
                continue
            chunks.append(cast("dict[str, object]", decoded))
            if not show_chunks:
                continue
            text = decoded.get("text")
            if not isinstance(text, str) or not text:
                continue
            if not printed_prefix:
                print(f"{GREEN}{BOLD}AGENT{RESET}  ", end="", flush=True)
                printed_prefix = True
            tag = f"{DIM}(interim){RESET} " if decoded.get("interim") else ""
            print(f"{tag}{text} ", end="", flush=True)
        status = resp.status_code
    if show_chunks and printed_prefix:
        print()  # newline after the agent's full reply
    return status, chunks


def _read_tool_calls(call_id: str) -> list[dict[str, object]]:
    """All tool-call log entries stamped with this ``call_id``, in order."""
    if not TOOL_CALL_LOG.exists():
        return []
    out: list[dict[str, object]] = []
    with TOOL_CALL_LOG.open() as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("call_id") == call_id:
                out.append(cast("dict[str, object]", entry))
    return out


def _print_tools(tools: list[dict[str, object]]) -> None:
    """Print each tool call as ``↳ tool name {args}``."""
    if not tools:
        print(f"  {DIM}(no tool calls yet){RESET}")
        return
    for t in tools:
        args = t.get("args")
        args_str = json.dumps(args, default=str) if args else "{}"
        print(f"  {MAGENTA}↳ {t.get('tool')}{RESET} {DIM}{args_str}{RESET}")


def _print_history(history: list[dict[str, str]]) -> None:
    """Pretty-print the accumulated recent_history."""
    if not history:
        print(f"  {DIM}(empty){RESET}")
        return
    for entry in history:
        role = entry.get("role", "")
        content = entry.get("content", "")
        tag = f"{GREEN}AGENT{RESET}" if role == "agent" else f"{YELLOW}USER {RESET}"
        print(f"  {tag}  {content}")


def _print_banner(call_id: str) -> None:
    print(f"{CYAN}{BOLD}JailCall interactive{RESET}  {DIM}call_id={call_id}{RESET}")
    print(f"{DIM}Slash commands: /quit /reset /tools /history /help{RESET}")
    print()
    print(f"{GREEN}{BOLD}AGENT{RESET}  {BEGIN_MESSAGE}")
    print()


def _send_call_ended(
    *,
    client: httpx.Client,
    url: str,
    secret: bytes,
    call_id: str,
    history: list[dict[str, str]],
    started_at: float,
) -> None:
    """Best-effort agent.call_ended POST so server-side state closes cleanly."""
    payload = _call_ended_payload(
        call_id=call_id,
        full_transcript=list(history),
        started_at=started_at,
        ended_at=time.time(),
    )
    with contextlib.suppress(httpx.HTTPError):
        _, _ = _stream_post(
            client=client,
            url=url,
            secret=secret,
            delivery_id=f"call_ended_{call_id}_{int(time.time())}",
            event_type="agent.call_ended",
            payload=payload,
            show_chunks=False,
        )


def _handle_slash(
    cmd: str,
    *,
    call_id: str,
    history: list[dict[str, str]],
) -> bool:
    """Process a non-quit ``/...`` command. Returns True if a command ran."""
    if cmd == "/help":
        print(f"{DIM}commands: /quit /reset /tools /history /help{RESET}")
        return True
    if cmd == "/tools":
        _print_tools(_read_tool_calls(call_id))
        return True
    if cmd == "/history":
        _print_history(history)
        return True
    print(f"{RED}unknown command:{RESET} {cmd}  {DIM}(try /help){RESET}")
    return True


def _new_session_state() -> tuple[str, list[dict[str, str]], float]:
    """Fresh call_id, history seeded with the locked beginMessage, start ts."""
    call_id = _new_call_id()
    history: list[dict[str, str]] = [{"role": "agent", "content": BEGIN_MESSAGE}]
    return call_id, history, time.time()


def _prompt() -> str:
    """Read one caller line. Raises EOFError on Ctrl-D."""
    return input(f"{YELLOW}{BOLD}YOU  {RESET}").strip()


def run(*, url: str, secret: bytes) -> int:
    """Drive the REPL until the user quits. Returns process exit code."""
    call_id, history, started_at = _new_session_state()
    _print_banner(call_id)
    turn_idx = 0

    with httpx.Client() as client:
        while True:
            try:
                text = _prompt()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text:
                continue

            if text.startswith("/"):
                if text in ("/quit", "/exit", "/q"):
                    break
                if text == "/reset":
                    _send_call_ended(
                        client=client,
                        url=url,
                        secret=secret,
                        call_id=call_id,
                        history=history,
                        started_at=started_at,
                    )
                    call_id, history, started_at = _new_session_state()
                    turn_idx = 0
                    print()
                    _print_banner(call_id)
                    continue
                _handle_slash(text, call_id=call_id, history=history)
                continue

            history.append({"role": "user", "content": text})
            payload = _agent_message_payload(
                call_id=call_id,
                transcript=text,
                recent_history=list(history),
            )
            delivery_id = f"voice_{call_id}_{int(time.time())}_{turn_idx}"

            tools_before = len(_read_tool_calls(call_id))
            try:
                status, chunks = _stream_post(
                    client=client,
                    url=url,
                    secret=secret,
                    delivery_id=delivery_id,
                    event_type="agent.message",
                    payload=payload,
                    show_chunks=True,
                )
            except httpx.HTTPError as exc:
                print(f"\n{RED}request failed:{RESET} {exc}")
                history.pop()  # roll back the user turn we just appended
                continue

            if status != 200:
                print(f"{RED}HTTP {status}{RESET}")
                history.pop()
                continue

            agent_text = "".join(
                c.get("text", "") for c in chunks if isinstance(c.get("text"), str)
            )
            history.append({"role": "agent", "content": agent_text})

            new_tools = _read_tool_calls(call_id)[tools_before:]
            if new_tools:
                _print_tools(new_tools)

            turn_idx += 1

        _send_call_ended(
            client=client,
            url=url,
            secret=secret,
            call_id=call_id,
            history=history,
            started_at=started_at,
        )

    print(f"{DIM}goodbye.{RESET}")
    return 0


def main() -> int:
    """Entry point for ``python -m evals.interactive``."""
    parser = argparse.ArgumentParser(description="JailCall interactive REPL.")
    _ = parser.add_argument("--url", default=DEFAULT_URL, help="Webhook URL")
    args = parser.parse_args()
    url: str = args.url

    _ = load_dotenv(ROOT / ".env")
    secret = os.environ.get("AGENTPHONE_WEBHOOK_SECRET")
    if not secret:
        print("AGENTPHONE_WEBHOOK_SECRET not set — load .env first", file=sys.stderr)
        return 2

    return run(url=url, secret=secret.encode())


if __name__ == "__main__":
    raise SystemExit(main())
