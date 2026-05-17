"""Replay JailCall eval scenarios against the live webhook server.

For each scenario in ``evals/transcripts.jsonl`` this harness synthesizes
AgentPhone-shaped signed webhook POSTs, threads ``recentHistory`` across
turns the way real deliveries do, parses the NDJSON reply, and runs the
per-turn assertions.

Payload shape is verbatim from real captures in ``evals/captures/`` taken
on 2026-05-17 — see ``TOOLS.md`` for the schema.

Run with::

    uv run python -m evals.replay
    uv run python -m evals.replay --scenario happy_path_dui
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from dotenv import load_dotenv

if TYPE_CHECKING:
    from collections.abc import Iterable

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = ROOT / "evals" / "transcripts.jsonl"
TOOL_CALL_LOG = ROOT / "evals" / "last_run" / "tool_calls.jsonl"
DEFAULT_URL = "http://127.0.0.1:5321/webhook"

# Locked beginMessage — must match SPEC.md "Voice script" section verbatim.
# Source of truth: SPEC.md → Voice script → Opening (beginMessage).
BEGIN_MESSAGE = (
    "You've reached JailCall. I am not a lawyer and this call may be "
    "recorded by the facility. Do not tell me what happened or any "
    "details about your case. I can help contact a criminal defense "
    "attorney on your behalf right now. Would you like me to do that?"
)

# Fake-but-realistic ids for synthesized payloads. Real captures use
# the same cuid-ish format; nothing on our server checks them.
AGENT_ID = "cmp1rq9gf04ocdex677zw30m0"
NUMBER_ID = "cmpa0373x01vujz00iuwg82a9"
FROM_NUMBER = "+15551234567"
TO_NUMBER = "+15707347169"


@dataclass
class Assertion:
    """One assertion ran against one turn's response (or a final dispatch)."""

    label: str
    passed: bool
    detail: str = ""


@dataclass
class TurnResult:
    """Per-turn outcome: HTTP status, parsed agent text, assertions."""

    turn_index: int
    caller: str
    agent_text: str
    http_status: int
    assertions: list[Assertion] = field(default_factory=list)


@dataclass
class ScenarioResult:
    """Full per-scenario result, including dispatch-level checks."""

    scenario_id: str
    turns: list[TurnResult] = field(default_factory=list)
    dispatch_assertions: list[Assertion] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def failed_count(self) -> int:
        """Number of assertions that ran and failed."""
        turn_fails = sum(1 for t in self.turns for a in t.assertions if not a.passed)
        dispatch_fails = sum(1 for a in self.dispatch_assertions if not a.passed)
        return turn_fails + dispatch_fails

    @property
    def assertion_count(self) -> int:
        """Total assertions actually run (not skipped)."""
        turn_n = sum(len(t.assertions) for t in self.turns)
        return turn_n + len(self.dispatch_assertions)


def _new_call_id() -> str:
    """Mint a cuid-shaped synthetic call id."""
    return f"cmpa{uuid.uuid4().hex[:20]}"


def _sign(secret: bytes, timestamp: str, body: bytes) -> str:
    """Reproduce the AgentPhone signature recipe used by server.py."""
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


def _post_webhook(
    *,
    client: httpx.Client,
    url: str,
    secret: bytes,
    event_type: str,
    delivery_id: str,
    payload: dict[str, object],
) -> tuple[int, list[dict[str, object]]]:
    """POST a signed webhook delivery and parse the NDJSON response.

    Returns the HTTP status and the list of decoded NDJSON records (one
    JSON object per newline-separated chunk).
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
    resp = client.post(url, content=body, headers=headers, timeout=120.0)
    chunks: list[dict[str, object]] = []
    for raw in resp.text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            chunks.append(decoded)
    return resp.status_code, chunks


def _spoken_text(chunks: list[dict[str, object]]) -> str:
    """Concat all ``text`` fields across chunks — including interims.

    Live AgentPhone behavior (verified from captures): interim chunks ARE
    spoken to the caller and ARE recorded into recentHistory. So the
    "what did the agent say" view is the full concat.
    """
    parts: list[str] = []
    for chunk in chunks:
        text = chunk.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _check_includes(agent_text: str, must_include: Iterable[Any]) -> list[Assertion]:
    """Each ``must_include`` substring should be a case-insensitive substring of agent_text."""
    out: list[Assertion] = []
    haystack = agent_text.lower()
    for needle in must_include:
        if not isinstance(needle, str):
            continue
        ok = needle.lower() in haystack
        out.append(
            Assertion(
                label=f'agent_must_include "{needle}"',
                passed=ok,
                detail="" if ok else f"agent text was: {agent_text!r}",
            ),
        )
    return out


def _tool_calls_for(call_id: str) -> list[str]:
    """Read the tool-call log for the entries stamped with ``call_id``.

    Returns the ordered list of tool names that fired during this call.
    Missing or unreadable lines are silently skipped — the log is a
    one-way append from the server, never edited.
    """
    if not TOOL_CALL_LOG.exists():
        return []
    out: list[str] = []
    with TOOL_CALL_LOG.open() as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("call_id") != call_id:
                continue
            tool = entry.get("tool")
            if isinstance(tool, str):
                out.append(tool)
    return out


def _grade_required(required: object, tools_called: list[str], detail: str) -> list[Assertion]:
    """Each ``required`` tool name must appear at least once in tools_called."""
    if not isinstance(required, list):
        return []
    out: list[Assertion] = []
    for needed in required:
        if not isinstance(needed, str):
            continue
        ok = needed in tools_called
        out.append(
            Assertion(
                label=f"dispatch required: {needed}",
                passed=ok,
                detail="" if ok else detail,
            ),
        )
    return out


def _grade_any_of(any_of: object, tools_called: list[str], detail: str) -> list[Assertion]:
    """Each ``any_of`` group needs at least one of its names called."""
    if not isinstance(any_of, list):
        return []
    out: list[Assertion] = []
    for group in any_of:
        if not isinstance(group, list):
            continue
        names = [str(x) for x in group if isinstance(x, str)]
        if not names:
            continue
        ok = any(n in tools_called for n in names)
        out.append(
            Assertion(
                label=f"dispatch any_of: {' OR '.join(names)}",
                passed=ok,
                detail="" if ok else detail,
            ),
        )
    return out


def _grade_forbidden(forbidden: object, tools_called: list[str], call_id: str) -> list[Assertion]:
    """No ``forbidden`` tool name may appear in tools_called."""
    if not isinstance(forbidden, list):
        return []
    out: list[Assertion] = []
    for banned in forbidden:
        if not isinstance(banned, str):
            continue
        ok = banned not in tools_called
        out.append(
            Assertion(
                label=f"dispatch forbidden: {banned}",
                passed=ok,
                detail="" if ok else f"tool {banned} was called for {call_id}",
            ),
        )
    return out


def _grade_dispatch(call_id: str, expected: object, tools_called: list[str]) -> list[Assertion]:
    """Run required / any_of / forbidden assertions against the recorded tool calls.

    ``expected = None`` means the scenario should not dispatch anything;
    that becomes a single assertion that the tool-call list is empty.
    """
    detail = f"tools called for {call_id}: {tools_called}"

    if expected is None:
        return [
            Assertion(
                label="no dispatch expected (zero tool calls)",
                passed=len(tools_called) == 0,
                detail="" if not tools_called else detail,
            ),
        ]

    if not isinstance(expected, dict):
        return []

    return [
        *_grade_required(expected.get("required"), tools_called, detail),
        *_grade_any_of(expected.get("any_of"), tools_called, detail),
        *_grade_forbidden(expected.get("forbidden"), tools_called, call_id),
    ]


def _check_excludes(agent_text: str, must_not_include: Iterable[Any]) -> list[Assertion]:
    """Each ``must_not_include`` substring should NOT appear in agent_text."""
    out: list[Assertion] = []
    haystack = agent_text.lower()
    for needle in must_not_include:
        if not isinstance(needle, str):
            continue
        ok = needle.lower() not in haystack
        out.append(
            Assertion(
                label=f'agent_must_not_include "{needle}"',
                passed=ok,
                detail="" if ok else f"agent text was: {agent_text!r}",
            ),
        )
    return out


def _run_one_turn(
    *,
    turn: dict[str, Any],
    turn_index: int,
    call_id: str,
    recent_history: list[dict[str, str]],
    client: httpx.Client,
    url: str,
    secret: bytes,
) -> TurnResult:
    """Send one ``agent.message`` for this turn, parse the reply, run assertions.

    Mutates ``recent_history`` in place — appends the user turn before
    POSTing and the agent's spoken response after.
    """
    caller = str(turn.get("caller", ""))
    recent_history.append({"role": "user", "content": caller})

    payload = _agent_message_payload(
        call_id=call_id,
        transcript=caller,
        recent_history=list(recent_history),
    )
    delivery_id = f"voice_{call_id}_{int(time.time())}_{turn_index}"
    status, chunks = _post_webhook(
        client=client,
        url=url,
        secret=secret,
        event_type="agent.message",
        delivery_id=delivery_id,
        payload=payload,
    )
    agent_text = _spoken_text(chunks)
    recent_history.append({"role": "agent", "content": agent_text})

    tr = TurnResult(
        turn_index=turn_index,
        caller=caller,
        agent_text=agent_text,
        http_status=status,
    )
    tr.assertions.append(
        Assertion(
            label="http_status == 200",
            passed=status == 200,
            detail="" if status == 200 else f"got {status}",
        ),
    )

    expect = turn.get("expect")
    if isinstance(expect, dict):
        includes = expect.get("agent_must_include")
        if isinstance(includes, list):
            tr.assertions.extend(_check_includes(agent_text, includes))
        excludes = expect.get("agent_must_not_include")
        if isinstance(excludes, list):
            tr.assertions.extend(_check_excludes(agent_text, excludes))
        # tool_calls / state_after intentionally not observable yet.

    return tr


def _run_scenario(
    *,
    scenario: dict[str, Any],
    client: httpx.Client,
    url: str,
    secret: bytes,
) -> ScenarioResult:
    """Drive one scenario end to end: each turn, then a final agent.call_ended."""
    scenario_id = str(scenario.get("id", "<unknown>"))
    result = ScenarioResult(scenario_id=scenario_id)

    call_id = _new_call_id()
    started_at = time.time()
    recent_history: list[dict[str, str]] = [
        {"role": "agent", "content": BEGIN_MESSAGE},
    ]

    turns = scenario.get("turns")
    if not isinstance(turns, list):
        result.skipped.append("scenario has no turns array")
        return result

    for i, turn_obj in enumerate(turns):
        if not isinstance(turn_obj, dict):
            continue
        result.turns.append(
            _run_one_turn(
                turn=turn_obj,
                turn_index=i,
                call_id=call_id,
                recent_history=recent_history,
                client=client,
                url=url,
                secret=secret,
            ),
        )

    ended_at = time.time()
    end_payload = _call_ended_payload(
        call_id=call_id,
        full_transcript=list(recent_history),
        started_at=started_at,
        ended_at=ended_at,
    )
    end_status, _ = _post_webhook(
        client=client,
        url=url,
        secret=secret,
        event_type="agent.call_ended",
        delivery_id=f"call_ended_{call_id}_{int(time.time())}",
        payload=end_payload,
    )
    if end_status != 200:
        result.skipped.append(f"agent.call_ended POST returned {end_status}")

    tools_called = _tool_calls_for(call_id)
    result.dispatch_assertions = _grade_dispatch(
        call_id=call_id,
        expected=scenario.get("expected_dispatch"),
        tools_called=tools_called,
    )

    if any(isinstance(t, dict) and t.get("expect", {}).get("state_after") for t in turns):
        result.skipped.append(
            "state_after assertions skipped: requires controller state observability",
        )

    return result


def _load_scenarios(path: Path) -> list[dict[str, Any]]:
    """Parse evals/transcripts.jsonl into a list of scenario dicts."""
    out: list[dict[str, Any]] = []
    with path.open() as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"skipping {path}:{line_no} — invalid JSON: {exc}", file=sys.stderr)
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _print_summary(results: list[ScenarioResult]) -> int:
    """Pretty-print results; return process exit code (non-zero on any fail)."""
    total_assert = 0
    total_fail = 0
    total_skipped = 0
    for r in results:
        total_assert += r.assertion_count
        total_fail += r.failed_count
        total_skipped += len(r.skipped)
        status = "PASS" if r.failed_count == 0 else "FAIL"
        print(f"\n[{status}] {r.scenario_id}  ({r.assertion_count} ran, {r.failed_count} failed)")
        for t in r.turns:
            head = f"  turn {t.turn_index} (HTTP {t.http_status}) caller={t.caller!r}"
            print(head)
            print(f"    agent_text: {t.agent_text!r}")
            for a in t.assertions:
                mark = "✓" if a.passed else "✗"
                line = f"      {mark} {a.label}"
                if a.detail:
                    line += f"  — {a.detail}"
                print(line)
        if r.dispatch_assertions:
            print("  dispatch:")
            for a in r.dispatch_assertions:
                mark = "✓" if a.passed else "✗"
                line = f"    {mark} {a.label}"
                if a.detail:
                    line += f"  — {a.detail}"
                print(line)
        for skip in r.skipped:
            print(f"  ⤳ skipped: {skip}")

    print(
        f"\n=== {len(results)} scenarios, {total_assert} assertions, "
        f"{total_fail} failed, {total_skipped} skip-notes ===",
    )
    return 0 if total_fail == 0 else 1


def main() -> int:
    """Entry point for ``python -m evals.replay``."""
    parser = argparse.ArgumentParser(description="Replay JailCall eval scenarios.")
    _ = parser.add_argument("--url", default=DEFAULT_URL, help="Webhook URL")
    _ = parser.add_argument(
        "--scenario",
        default=None,
        help="Run only this scenario id (otherwise all)",
    )
    args = parser.parse_args()
    url: str = args.url
    only: str | None = args.scenario

    _ = load_dotenv(ROOT / ".env")
    secret = os.environ.get("AGENTPHONE_WEBHOOK_SECRET")
    if not secret:
        print("AGENTPHONE_WEBHOOK_SECRET not set — load .env first", file=sys.stderr)
        return 2

    # Truncate the tool-call log once per suite so a stale prior run can't
    # leak dispatch grades into this one. Per-scenario filtering by call_id
    # already isolates results within a single suite run.
    TOOL_CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
    _ = TOOL_CALL_LOG.write_text("")

    scenarios = _load_scenarios(SCENARIOS_PATH)
    if only:
        scenarios = [s for s in scenarios if s.get("id") == only]
        if not scenarios:
            print(f"no scenario with id={only!r}", file=sys.stderr)
            return 2

    with httpx.Client() as client:
        results = [
            _run_scenario(scenario=s, client=client, url=url, secret=secret.encode())
            for s in scenarios
        ]

    return _print_summary(results)


if __name__ == "__main__":
    raise SystemExit(main())
