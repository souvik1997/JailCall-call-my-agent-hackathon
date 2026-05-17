"""Tool schemas and handlers for JailCall's Gemini tool-call loop.

See SPEC.md for the three tools and their schemas:
``moss_find_lawyers``, ``contact_attorneys``, ``email_attorneys``.

Routing is Moss-backed: ``moss_find_lawyers`` queries the pre-built Moss index
populated by ``jailcall.build_index`` from ``law_firms/``. Browser Use is
reserved for the slow ``contact_attorneys`` form fill; AgentMail handles the
email path.

Jurisdiction is hardcoded to the Bay Area, so there is no ``classify_location``
tool and no ``county``/``state`` arguments. SMS confirmation is dropped — the
inbound voice line is the only AgentPhone channel.

The schemas are in google-genai ``FunctionDeclaration`` shape, ready to drop
into a ``Tool(function_declarations=[...])`` config. Handlers are async to
keep the door open for the real implementations (Moss client, Browser Use,
AgentMail) — today they return canned data so the controller end-to-end loop
is exercisable without the slow vendor calls. Milestones 5/6/7 swap in the
real impls without changing this file's public surface.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

from google.genai import types

from jailcall.call_context import current_call_id
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


# ─── Schemas (Gemini FunctionDeclaration shape) ─────────────────────────────


MOSS_FIND_LAWYERS_SCHEMA: Final[types.FunctionDeclaration] = types.FunctionDeclaration(
    name="moss_find_lawyers",
    description=(
        "Look up Bay Area criminal defense attorneys whose practice matches the "
        "caller's charge category. Returns up to 3 candidates as a JSON array of "
        "{firm_name, phone, email, form_url, summary}. Sub-second — safe to call "
        "inline mid-turn. Jurisdiction is hardcoded; do not pass any location."
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


CONTACT_ATTORNEYS_SCHEMA: Final[types.FunctionDeclaration] = types.FunctionDeclaration(
    name="contact_attorneys",
    description=(
        "Submit a contact/intake form on an attorney's website via Browser Use. "
        "Slow — 30 to 90 seconds. Call once per firm returned by moss_find_lawyers "
        "that has a form_url. ALWAYS speak an interim line to the caller before "
        "calling this so the line is not silent."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "form_url": types.Schema(
                type=types.Type.STRING,
                description="The attorney's contact-form URL, from moss_find_lawyers.",
            ),
            "caller_name": types.Schema(
                type=types.Type.STRING,
                description="The caller's name, exactly as they gave it.",
            ),
            "charge_category": types.Schema(
                type=types.Type.STRING,
                description='Charge category in plain English (e.g. "DUI").',
            ),
            "callback_number": types.Schema(
                type=types.Type.STRING,
                description=(
                    "E.164-formatted callback number the attorney should call. "
                    'Pass "" if the caller had no number.'
                ),
            ),
            "message": types.Schema(
                type=types.Type.STRING,
                description=(
                    "Short urgent message to the attorney. Should mention that the "
                    "person is in custody in the Bay Area and needs a callback ASAP."
                ),
            ),
        },
        required=[
            "form_url",
            "caller_name",
            "charge_category",
            "callback_number",
            "message",
        ],
    ),
)


EMAIL_ATTORNEYS_SCHEMA: Final[types.FunctionDeclaration] = types.FunctionDeclaration(
    name="email_attorneys",
    description=(
        "Send a structured intake email to an attorney whose email address was "
        "found by moss_find_lawyers. Faster than contact_attorneys (~1 second) "
        "so no interim line needed. Call once per firm with an email."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "to": types.Schema(
                type=types.Type.STRING,
                description="Attorney intake email address, from moss_find_lawyers.",
            ),
            "caller_name": types.Schema(
                type=types.Type.STRING,
                description="The caller's name.",
            ),
            "charge_category": types.Schema(
                type=types.Type.STRING,
                description='Charge category in plain English (e.g. "DUI").',
            ),
            "callback_number": types.Schema(
                type=types.Type.STRING,
                description=("E.164 callback number. Pass empty string if none was given."),
            ),
        },
        required=["to", "caller_name", "charge_category", "callback_number"],
    ),
)


TOOL_SCHEMAS: Final[list[types.FunctionDeclaration]] = [
    MOSS_FIND_LAWYERS_SCHEMA,
    CONTACT_ATTORNEYS_SCHEMA,
    EMAIL_ATTORNEYS_SCHEMA,
]


# ─── Stub handlers (Milestone 5/6/7 will replace these with real impls) ─────


async def moss_find_lawyers(args: dict[str, object]) -> str:
    """Query the Moss client for Bay Area attorneys matching ``charge_category``.

    Routes through ``jailcall.moss.get_moss_client()`` so the mock (today)
    and the real Moss SDK (Milestone 5) are swappable without touching this
    function. The return contract — JSON array of ``{firm_name, phone,
    email, form_url, summary}`` — is locked so the controller doesn't have
    to change.
    """
    charge = str(args.get("charge_category") or "criminal defense").strip()
    query_str = f"{charge} criminal defense attorney"
    index_name = os.environ.get("MOSS_INDEX_NAME", DEFAULT_MOSS_INDEX)

    client = get_moss_client()
    result = await client.query(index_name, query_str, top_k=3, alpha=0.6)

    candidates = [
        {
            "firm_name": doc.metadata.get("name"),
            "phone": doc.metadata.get("phone"),
            "email": doc.metadata.get("email"),
            "form_url": doc.metadata.get("intake_url") or doc.metadata.get("website"),
            "summary": doc.text,
        }
        for doc in result.docs
    ]
    logger.info(
        "moss_find_lawyers returned %d candidate(s) for charge=%r",
        len(candidates),
        charge,
    )
    return json.dumps(candidates)


async def contact_attorneys(args: dict[str, object]) -> str:
    """Stub: pretend the Browser Use form fill succeeded."""
    form_url = str(args.get("form_url") or "")
    name = str(args.get("caller_name") or "")
    logger.info("contact_attorneys stub submitting form for %r at %s", name, form_url)
    return json.dumps(
        {"status": "submitted", "form_url": form_url, "caller_name": name},
    )


async def email_attorneys(args: dict[str, object]) -> str:
    """Stub: pretend the AgentMail send succeeded."""
    to = str(args.get("to") or "")
    name = str(args.get("caller_name") or "")
    logger.info("email_attorneys stub sending intake email to %s for %r", to, name)
    return json.dumps({"status": "sent", "to": to, "caller_name": name})


TOOL_HANDLERS: Final[dict[str, ToolHandler]] = {
    "moss_find_lawyers": moss_find_lawyers,
    "contact_attorneys": contact_attorneys,
    "email_attorneys": email_attorneys,
}


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
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        result = json.dumps({"error": f"unknown tool: {name}"})
        _log_tool_call(name, args, result)
        return result
    try:
        result = await handler(args)
    except Exception as exc:
        logger.exception("tool %s failed", name)
        result = json.dumps({"error": f"tool {name} raised: {exc}"})
    _log_tool_call(name, args, result)
    return result
