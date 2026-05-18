"""In-memory observability state for the local JailCall dashboard."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Final

from jailcall.call_context import current_call_id

JsonObject = dict[str, object]

MAX_EVENTS: Final[int] = 300
MAX_TURNS: Final[int] = 120
MAX_TOOL_RECORDS: Final[int] = 120
MAX_AGENTPHONE_CHUNKS: Final[int] = 120
MAX_INBOUND_REPLIES: Final[int] = 50
REPLY_PREVIEW_CHARS: Final[int] = 1200


@dataclass
class TranscriptTurn:
    """One caller or agent utterance shown in the dashboard transcript."""

    role: str
    text: str
    ts: float
    interim: bool = False
    source: str = "webhook"

    def to_json(self) -> JsonObject:
        """Return a JSON-serialisable representation."""
        return {
            "role": self.role,
            "text": self.text,
            "ts": self.ts,
            "interim": self.interim,
            "source": self.source,
        }


@dataclass
class ToolRecord:
    """One tool action or dispatch event."""

    name: str
    status: str
    ts: float
    args: JsonObject = field(default_factory=dict)
    result: JsonObject | str = ""
    duration_ms: float | None = None

    def to_json(self) -> JsonObject:
        """Return a JSON-serialisable representation."""
        payload: JsonObject = {
            "name": self.name,
            "status": self.status,
            "ts": self.ts,
            "args": self.args,
            "result": self.result,
        }
        if self.duration_ms is not None:
            payload["duration_ms"] = self.duration_ms
        return payload


@dataclass
class DashboardEvent:
    """A chronological event for the dashboard activity feed."""

    seq: int
    call_id: str
    kind: str
    title: str
    detail: str
    ts: float
    payload: JsonObject = field(default_factory=dict)

    def to_json(self) -> JsonObject:
        """Return a JSON-serialisable representation."""
        return {
            "seq": self.seq,
            "call_id": self.call_id,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "ts": self.ts,
            "payload": self.payload,
        }


@dataclass
class ConversationState:
    """Dashboard state for one AgentPhone call."""

    call_id: str
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    status: str = "active"
    latest_delivery_id: str = ""
    latest_event: str = ""
    latest_transcript: str = ""
    transcript: list[TranscriptTurn] = field(default_factory=list)
    tools: list[ToolRecord] = field(default_factory=list)
    agentphone_chunks: list[JsonObject] = field(default_factory=list)

    def to_json(self) -> JsonObject:
        """Return a JSON-serialisable representation."""
        return {
            "call_id": self.call_id,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "latest_delivery_id": self.latest_delivery_id,
            "latest_event": self.latest_event,
            "latest_transcript": self.latest_transcript,
            "transcript": [turn.to_json() for turn in self.transcript],
            "tools": [tool.to_json() for tool in self.tools],
            "agentphone_chunks": list(self.agentphone_chunks),
        }


_lock: Final[Lock] = Lock()
_conversations: dict[str, ConversationState] = {}
_events: list[DashboardEvent] = []
_inbound_replies: list[JsonObject] = []
_active_call_id = ""
_seq = 0


def _now() -> float:
    return time.time()


def _coerce_call_id(call_id: str | None = None) -> str:
    explicit = (call_id or "").strip()
    if explicit:
        return explicit
    contextual = current_call_id.get().strip()
    return contextual or "unknown-call"


def _normalize_transcript(text: str) -> str:
    """Lowercase + collapse whitespace + strip trailing punctuation.

    Used to dedup caller turns where AgentPhone delivers slight variations
    (partial vs final transcripts, punctuation drift) of the same utterance
    so they don't show as two separate rows in the dashboard transcript.
    """
    return " ".join(text.lower().strip(" .,!?\"'").split())


def _conversation_locked(call_id: str) -> ConversationState:
    conversation = _conversations.get(call_id)
    if conversation is None:
        conversation = ConversationState(call_id=call_id)
        _conversations[call_id] = conversation
    conversation.updated_at = _now()
    return conversation


def _append_event_locked(
    *,
    call_id: str,
    kind: str,
    title: str,
    detail: str,
    payload: JsonObject | None = None,
) -> None:
    global _seq  # noqa: PLW0603 - process-local sequence for live dashboard ordering.
    _seq += 1
    _events.append(
        DashboardEvent(
            seq=_seq,
            call_id=call_id,
            kind=kind,
            title=title,
            detail=detail,
            ts=_now(),
            payload=payload or {},
        ),
    )
    del _events[:-MAX_EVENTS]


def _trim_conversation(conversation: ConversationState) -> None:
    del conversation.transcript[:-MAX_TURNS]
    del conversation.tools[:-MAX_TOOL_RECORDS]
    del conversation.agentphone_chunks[:-MAX_AGENTPHONE_CHUNKS]


def record_webhook_delivery(
    *,
    call_id: str,
    delivery_id: str,
    event_name: str,
    channel: object,
    transcript: str,
    recent_history: list[dict[str, str]],
) -> None:
    """Record an inbound AgentPhone webhook delivery and caller transcript."""
    global _active_call_id  # noqa: PLW0603 - the dashboard has one visible active call.
    normalized_call_id = _coerce_call_id(call_id)
    now = _now()
    with _lock:
        conversation = _conversation_locked(normalized_call_id)
        conversation.status = "active"
        conversation.latest_delivery_id = delivery_id
        conversation.latest_event = event_name
        conversation.latest_transcript = transcript
        if recent_history:
            conversation.transcript = [
                TranscriptTurn(
                    role="agent" if entry["role"] == "agent" else "caller",
                    text=entry["content"],
                    ts=now,
                    source="recentHistory",
                )
                for entry in recent_history
                if entry.get("content")
            ]
        if transcript and (
            not conversation.transcript
            or conversation.transcript[-1].role != "caller"
            or _normalize_transcript(conversation.transcript[-1].text)
            != _normalize_transcript(transcript)
        ):
            conversation.transcript.append(
                TranscriptTurn(role="caller", text=transcript, ts=now),
            )
        _active_call_id = normalized_call_id
        _trim_conversation(conversation)
        _append_event_locked(
            call_id=normalized_call_id,
            kind="agentphone-inbound",
            title="AgentPhone webhook",
            detail=f"{event_name} on {channel or 'unknown'}",
            payload={"delivery_id": delivery_id, "transcript": transcript},
        )


def record_agentphone_chunk(
    *,
    call_id: str | None = None,
    text: str,
    interim: bool = False,
    payload: JsonObject | None = None,
) -> None:
    """Record a response chunk being streamed back to AgentPhone."""
    normalized_call_id = _coerce_call_id(call_id)
    chunk = dict(payload or {})
    chunk["text"] = text
    chunk["interim"] = interim
    chunk["ts"] = _now()
    with _lock:
        conversation = _conversation_locked(normalized_call_id)
        conversation.agentphone_chunks.append(chunk)
        conversation.transcript.append(
            TranscriptTurn(
                role="agent",
                text=text,
                ts=chunk["ts"] if isinstance(chunk["ts"], float) else _now(),
                interim=interim,
                source="agentphone-ndjson",
            ),
        )
        _trim_conversation(conversation)
        _append_event_locked(
            call_id=normalized_call_id,
            kind="agentphone-outbound",
            title="Streamed response",
            detail=text,
            payload=chunk,
        )


def record_call_status(
    *,
    call_id: str,
    status: str,
    event_name: str,
    detail: str = "",
) -> None:
    """Record a call lifecycle status change."""
    normalized_call_id = _coerce_call_id(call_id)
    with _lock:
        conversation = _conversation_locked(normalized_call_id)
        conversation.status = status
        conversation.latest_event = event_name
        _append_event_locked(
            call_id=normalized_call_id,
            kind="call-status",
            title=event_name,
            detail=detail or status,
        )


def record_tool_status(
    *,
    name: str,
    status: str,
    args: JsonObject | None = None,
    result: JsonObject | str = "",
    duration_ms: float | None = None,
    call_id: str | None = None,
) -> None:
    """Record a tool-call status update for the active call."""
    normalized_call_id = _coerce_call_id(call_id)
    record = ToolRecord(
        name=name,
        status=status,
        ts=_now(),
        args=args or {},
        result=result,
        duration_ms=duration_ms,
    )
    result_preview = result if isinstance(result, str) else json.dumps(result)[:220]
    with _lock:
        conversation = _conversation_locked(normalized_call_id)
        conversation.tools.append(record)
        _trim_conversation(conversation)
        _append_event_locked(
            call_id=normalized_call_id,
            kind="tool",
            title=f"{name}: {status}",
            detail=result_preview,
            payload=record.to_json(),
        )


def record_moss_search(
    *,
    query: str,
    index_name: str,
    candidates: list[JsonObject],
    duration_ms: float,
    call_id: str | None = None,
) -> None:
    """Record the Moss search query and candidate firms returned."""
    normalized_call_id = _coerce_call_id(call_id)
    payload: JsonObject = {
        "query": query,
        "index_name": index_name,
        "candidates": candidates,
        "duration_ms": duration_ms,
    }
    with _lock:
        conversation = _conversation_locked(normalized_call_id)
        conversation.tools.append(
            ToolRecord(
                name="moss_find_lawyers",
                status="results",
                ts=_now(),
                args={"query": query, "index_name": index_name},
                result={"candidates": candidates},
                duration_ms=duration_ms,
            ),
        )
        _trim_conversation(conversation)
        _append_event_locked(
            call_id=normalized_call_id,
            kind="moss",
            title="Moss returned attorney matches",
            detail=f"{len(candidates)} firms for {query}",
            payload=payload,
        )


def record_inbound_reply(
    *,
    sender: str,
    subject: str,
    text: str,
) -> None:
    """Record an inbound AgentMail reply for the dashboard's reply panel.

    Written alongside the Supermemory ingest in ``server.agentmail_webhook``.
    Sender + subject are kept for display only — the demo trusts the body
    text for firm identification, not the headers.
    """
    with _lock:
        _inbound_replies.append(
            {
                "ts": _now(),
                "sender": sender.strip() or "unknown",
                "subject": subject.strip() or "(no subject)",
                "text": text.strip()[:REPLY_PREVIEW_CHARS],
                "call_id": _active_call_id,
            },
        )
        del _inbound_replies[:-MAX_INBOUND_REPLIES]
        _append_event_locked(
            call_id=_active_call_id or "inbox",
            kind="agentmail-inbound",
            title="AgentMail reply received",
            detail=f"from {sender or '?'} — {subject or '(no subject)'}",
            payload={"sender": sender, "subject": subject},
        )


def snapshot() -> JsonObject:
    """Return the full dashboard state as JSON."""
    with _lock:
        conversations = sorted(
            _conversations.values(),
            key=lambda item: item.updated_at,
            reverse=True,
        )
        return {
            "generated_at": _now(),
            "active_call_id": _active_call_id,
            "dispatch_inbox": os.environ.get("AGENTMAIL_DISPATCH_INBOX", ""),
            "conversations": [conversation.to_json() for conversation in conversations],
            "events": [event.to_json() for event in _events],
            "inbound_replies": list(reversed(_inbound_replies)),
        }
