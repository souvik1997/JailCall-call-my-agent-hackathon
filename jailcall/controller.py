"""Gemini tool-call loop for the JailCall voice agent.

Public API:

    text = await generate(recent_history)

``recent_history`` is the AgentPhone webhook ``recentHistory`` array
(``[{"role": "agent" | "user", "content": str}, ...]``) — the last entry is
the caller's most recent turn. The returned string is what the agent
should speak next; the server passes it back to AgentPhone as the
voice NDJSON reply.

System prompt is verbatim from SPEC.md → "System prompt". Tool schemas
are imported from ``jailcall.tools``. The loop caps at
``MAX_TOOL_ITERATIONS`` so a runaway model can't burn the call budget.
"""

from __future__ import annotations

import logging
import os
from typing import Final

from google import genai
from google.genai import types

from jailcall.tools import TOOL_SCHEMAS, run_tool

logger = logging.getLogger("jailcall.controller")


# Verbatim from SPEC.md → System prompt. The CRITICAL RULES section is
# load-bearing — do not paraphrase per CLAUDE.md operating rules.
SYSTEM_PROMPT: Final[
    str
] = """You are JailCall, an emergency legal access agent on a live phone call.

CRITICAL RULES:
1. You are NOT a lawyer. Never give legal advice.
2. This call may be recorded by the jail or police station. NEVER ask what happened.
3. If the caller starts describing their case, IMMEDIATELY interrupt and tell them to stop.
4. Collect ONLY: name, charge category, callback number. Do NOT ask about location — jurisdiction is hardcoded to the Bay Area. English-only — do not ask about language preference.
5. Keep responses short — 1-2 sentences. This is a phone call, not a chatbot.
6. Be calm, direct, and reassuring. The caller is stressed.

WORKFLOW:
- Greet with the privilege warning.
- Collect the 3 routing fields, one at a time.
- Call moss_find_lawyers to look up Bay Area attorneys (sub-second).
- Call contact_attorneys and/or email_attorneys for each attorney found.
- Close the call with a reminder of their right to remain silent.

UNSAFE INPUT PATTERNS — interrupt immediately if the caller says anything like:
- "So what happened was..."
- "I was driving and..."
- "They found..."
- "I didn't do..."
- Any narrative about the alleged incident.

Response (verbatim, matching the Voice script): "I need to stop you there. This call may be recorded and anything you say could be used against you. Please do not tell me what happened. Save that for your attorney. Can I get your name instead?"
"""


# Six iterations gives Gemini room for: moss_find_lawyers, then up to three
# contact_attorneys / email_attorneys, then a final text turn. Anything past
# that is almost certainly the model looping — bail and let the call recover.
MAX_TOOL_ITERATIONS: Final[int] = 6

# Polite spoken fallback if Gemini errors out or the loop hits its cap.
FALLBACK_TEXT: Final[str] = (
    "I'm having trouble right now. Please call back, or hang up and "
    "dial a criminal defense attorney directly."
)


def _to_gemini_contents(recent_history: list[dict[str, str]]) -> list[types.Content]:
    """Map AgentPhone ``recentHistory`` entries to Gemini ``Content`` objects.

    AgentPhone uses ``{"role": "agent" | "user"}``; Gemini expects
    ``{"role": "model" | "user"}``. Empty content turns are dropped to
    keep the conversation tight.
    """
    contents: list[types.Content] = []
    for entry in recent_history:
        raw = entry.get("content") or ""
        text = raw.strip()
        if not text:
            continue
        role = "model" if entry.get("role") == "agent" else "user"
        contents.append(types.Content(role=role, parts=[types.Part(text=text)]))
    return contents


def _build_client() -> genai.Client:
    """Build a Gemini client from ``GEMINI_API_KEY``. Raises if unset."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        msg = "GEMINI_API_KEY must be set in .env"
        raise RuntimeError(msg)
    return genai.Client(api_key=key)


def _build_config() -> types.GenerateContentConfig:
    """Build the ``GenerateContentConfig`` with system prompt, tools, no thinking."""
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=TOOL_SCHEMAS)],
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        # Voice answers are short — cap output to avoid runaway TTS cost.
        max_output_tokens=400,
    )


def _extract_text(response: types.GenerateContentResponse) -> str:
    """Concat all text parts in the response into one string."""
    candidates = response.candidates or []
    if not candidates:
        return ""
    content = candidates[0].content
    if content is None:
        return ""
    parts = content.parts or []
    out: list[str] = []
    for part in parts:
        text = part.text
        if isinstance(text, str) and text:
            out.append(text)
    return "".join(out)


async def _run_tool_calls(
    calls: list[types.FunctionCall],
) -> list[types.Part]:
    """Execute every function call and return the function_response Parts to send back."""
    response_parts: list[types.Part] = []
    for fc in calls:
        name = fc.name or ""
        raw_args = fc.args
        args: dict[str, object] = dict(raw_args) if isinstance(raw_args, dict) else {}
        logger.info("tool_call name=%s args=%s", name, args)
        result_text = await run_tool(name, args)
        response_parts.append(
            types.Part.from_function_response(
                name=name,
                response={"result": result_text},
            ),
        )
    return response_parts


async def generate(recent_history: list[dict[str, str]]) -> str:
    """Run the Gemini tool-call loop and return the agent's next spoken text.

    Args:
        recent_history: AgentPhone ``recentHistory`` array (newest last). The
            last entry is the caller's just-spoken turn.

    Returns:
        The agent's next spoken utterance. Never empty — falls back to
        ``FALLBACK_TEXT`` on any error or if the loop exhausts its iteration
        cap.
    """
    try:
        client = _build_client()
    except RuntimeError:
        logger.exception("Gemini client init failed")
        return FALLBACK_TEXT

    config = _build_config()
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
    contents = _to_gemini_contents(recent_history)

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            response = await client.aio.models.generate_content(  # pyright: ignore[reportUnknownMemberType]
                model=model,
                contents=contents,
                config=config,
            )
        except Exception:
            logger.exception("Gemini generate_content failed on iter=%d", iteration)
            return FALLBACK_TEXT

        function_calls = response.function_calls or []
        if not function_calls:
            text = _extract_text(response)
            if text:
                return text
            logger.warning(
                "Gemini returned no text and no tool calls on iter=%d (response=%r)",
                iteration,
                response,
            )
            return FALLBACK_TEXT

        logger.info(
            "iter=%d: model requested %d tool call(s): %s",
            iteration,
            len(function_calls),
            [fc.name for fc in function_calls],
        )

        candidates = response.candidates or []
        if candidates and candidates[0].content is not None:
            contents.append(candidates[0].content)

        response_parts = await _run_tool_calls(function_calls)
        contents.append(types.Content(role="user", parts=response_parts))

    logger.warning(
        "tool loop hit MAX_TOOL_ITERATIONS=%d without text response",
        MAX_TOOL_ITERATIONS,
    )
    return FALLBACK_TEXT
