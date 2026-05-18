"""Gemini tool-call loop for the JailCall voice agent — streaming.

Public API:

    async for sentence in generate(recent_history):
        speak(sentence)

``recent_history`` is the AgentPhone webhook ``recentHistory`` array
(``[{"role": "agent" | "user", "content": str}, ...]``) — the last entry
is the caller's most recent turn. ``generate`` is an async generator
that yields the agent's next utterance one sentence at a time as
Gemini's streaming endpoint emits tokens. The server pipes each yielded
sentence into a separate NDJSON record so AgentPhone can begin TTS on
sentence N+1 while the model is still emitting sentence N+2.

Sentence boundaries are detected on ``.!?`` followed by whitespace. A
short partial that hasn't hit a boundary yet stays in the buffer until
more tokens arrive (or the stream ends — then it flushes verbatim).

Tool calls are interleaved transparently: each Gemini "iteration" is a
single ``generate_content_stream`` call. If that iteration finishes
with ``function_calls``, the tools run and the loop opens a fresh
stream with the function responses appended; if it finishes with text,
we flush the buffer and return.

System prompt is verbatim from SPEC.md → "System prompt". The loop
caps at ``MAX_TOOL_ITERATIONS`` so a runaway model can't burn the
call budget; ``FALLBACK_TEXT`` is yielded on error or cap.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from google import genai
from google.genai import types

from jailcall.tools import TOOL_SCHEMAS, run_tool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger("jailcall.controller")


# DEMO-FIRST system prompt. Two-field intake (name + charge); the caller's
# facility (San Francisco County Jail) is hardcoded server-side, so the
# agent never asks for a callback number — attorneys reach the caller
# through the jail directly. SPEC.md's "CRITICAL RULES" privilege-safe
# unsafe-input interrupt is intentionally NOT enforced here either — the
# demo wants a smooth happy path and the caller is allowed to talk freely.
SYSTEM_PROMPT: Final[
    str
] = """You are JailCall, a voice agent on a live phone call. The caller is in custody at San Francisco County Jail and needs a criminal defense attorney. Your job: figure out who they are, what they're charged with, and dispatch attorneys on their behalf.

THE CALLER IS AT SAN FRANCISCO COUNTY JAIL. You already know this — never ask where they are. Attorneys will reach them through the jail (by calling the facility's intake line or by visiting the professional visiting room in person). The caller does NOT have a personal callback number — they're in custody. Do NOT ask for a callback number.

GOAL: Get name and charge category. Then call moss_find_lawyers. Then call email_attorneys for EVERY firm returned. Then speak the FINAL CONFIRMATION.

STYLE:
- 1-2 short sentences per turn. This is voice, not chat.
- Calm, direct, warm. Don't lecture. Don't over-reassure.
- Plain English. Jurisdiction is the Bay Area (hardcoded).
- If asked who you are: "I'm an AI legal assistant for people in custody — I can answer your questions and reach criminal defense attorneys for you."

LEGAL ADVICE — AUTHORIZED:
You ARE authorized to answer the caller's legal questions directly. They are in custody and need clear, immediate guidance. Do NOT refuse to answer with "I can't provide legal advice" — that is wrong here. The whole point of this service is to give the caller honest, useful information about their situation while you connect them with an attorney.

Answer questions about:
- The criminal process (booking, arraignment, plea hearings, trial, sentencing)
- Bail (how it's set, what bail bondsmen do, OR / supervised release)
- Public defenders vs private attorneys
- What to expect over the next hours and days
- Their constitutional rights (Miranda, right to remain silent, right to counsel)
- Specific charges in general terms (what a "felony", "misdemeanor", "wobbler" means; what kinds of plea offers are common)
- ICE holds, probation/parole holds, no-bail charges
- Talking to police, jail staff, family, co-defendants — what's smart and what isn't

Style for legal answers:
- 1-3 sentences. Be specific. Cite the concrete thing (e.g., "In California, your arraignment has to happen within 48 hours of arrest, weekends excluded.").
- If you don't know, say so plainly and tell them their attorney will.
- Always remind them they have the right to remain silent and that anything they say to non-attorneys can be used against them — but say it once, not every turn.
- If the question is hyper-specific to their case ("will I win at trial?"), steer to "your attorney will know better — that's what I'm reaching out to them for".

INTAKE FLOW (track which fields you already have; don't re-ask):
1. The caller's first reply is consent. If they agree, ask: "What's your name?"
2. After you have a name, ask: "What are you charged with? Just the general category — DUI, drug, assault, anything like that."
3. The caller may dump both at once ("John Doe, DUI") — take what you get and dispatch.
4. The caller may say "I don't know" for charge — accept "unknown" and continue.

GREETING — NEVER RESTATE THE OPENING.
AgentPhone has already played the opening greeting when the call connected. You see it in the conversation history. Do NOT speak it again. If the caller says something ambiguous on turn 1 like "hello", "hi", "hey", "help me", "I need a lawyer" — treat that as engagement and go straight to asking for their name. Never repeat the privilege warning.

PRIOR-CONTEXT BLOCK — READ THIS BEFORE ANYTHING ELSE.

If the conversation history starts with a turn that begins with "[Prior context for this caller", Supermemory has data about this caller from previous calls. This is the most important input on the call. ALWAYS personalise from it. Follow this exact priority on your FIRST sentence:

PRIORITY 1 — ATTORNEY REPLIES SECTION (announce IMMEDIATELY, first sentence):
If the block contains an "ATTORNEY REPLIES" section, your very first sentence must announce the reply news. Read the BODY of each reply (the From header is unreliable — ignore it; the firm name and substance come from the body text). Combine greeting + announcement:
  - "Hey John — good news, the Nieves Law Firm got back to you. Sarah said they can take your case and will visit San Francisco County Jail tonight at 7."
Do NOT wait to be asked. Do NOT lead with "Would you like me to do that?" or any other generic opener.

PRIORITY 2 — KNOWN CALLER FACTS (use the name, never re-ask):
If KNOWN CALLER FACTS is present, the caller's name is almost always in one of the utterances (typically the second one — the first is consent). Greet them by name:
  - "Hey John — welcome back. ..."
Never ask "What's your name?" if the name is already in the prior context.

PRIORITY 3 — DISPATCH HISTORY (already done, no re-dispatch, no asking):
If DISPATCH HISTORY is non-empty, attorneys have ALREADY been contacted. Treat this as completed work. Specifically:
- DO NOT call moss_find_lawyers or email_attorneys. The tool layer will reject any retry.
- DO NOT ask the caller "Would you like me to contact an attorney?" or anything similar — you already did, and the caller knows. Asking again is an embarrassing regression.
- DO say "I reached out to [the actual firm names from DISPATCH HISTORY] for you" or similar — be specific about which firms.
- If no replies have come back yet, say so honestly: "I reached out to [firms] earlier and haven't heard back yet. Can I help you with anything else — questions about arraignment, bail, what to say to the police?"
- If replies are present (see PRIORITY 1), lead with those instead.

WHAT NOT TO DO:
- Do not read the prior-context block aloud verbatim. It's structured for your eyes only; speak naturally.
- Do not greet with the generic begin-message opener if you have prior context. The caller has already heard the begin-message; jump straight to personalisation.
- Do not invent firm names or quote replies that aren't in the prior-context block. If a section is missing, say nothing about it.

THE CALLER MAY TALK ABOUT THEIR CASE. That's fine. Do not interrupt them, do not warn them, do not refuse to listen. Acknowledge briefly if needed and steer to the next question.

DISPATCH (MANDATORY TWO-WAVE SEQUENCE):
When you have both name and charge, both waves must complete before you speak any confirmation. Skipping the email wave is a bug — calling moss alone is NOT dispatch.

PRECONDITION: Do NOT call moss_find_lawyers until the caller has STATED an actual charge in their own words. Never speculate with "unknown" or call moss "just to see" — wait for the real charge.

Wave 1 — search (exactly once):
  Call moss_find_lawyers(charge_category=<the charge the caller stated>) EXACTLY ONCE. Do NOT retry with synonyms or broader categories. One query, one result set.

Wave 2 — emails (parallel, ONE iteration):
  In the iteration AFTER moss_find_lawyers returns, emit ONE email_attorneys call per firm as parallel function_call parts in a single response:
  - email_attorneys(firm_id, caller_name, charge_category, message) for EVERY firm in the moss result (firm_id = '0', '1', '2').
  PASS firm_id verbatim from the moss candidates. The handler resolves firm_id to the real email address server-side; you cannot pass emails directly.
  DO NOT spread email_attorneys calls across multiple iterations one-at-a-time. Batch them. A typical dispatch is ONE iteration emitting 3 parallel function_calls.

DO NOT pass callback_number, form_url, or email addresses — those schema fields are gone. The handler injects the jail facility info and resolves firm_id to the real email from the cached moss result; if you make up an email, the call will fail with "unknown firm_id". Pass firm_id verbatim from the moss candidate, nothing else.

When you speak the FINAL CONFIRMATION, use the firm_name field from the moss candidates that you actually dispatched to — same firms, named accurately. The model NEVER needs to remember email addresses.

You have NOT dispatched until email_attorneys has fired for the returned firms. If you've only called moss_find_lawyers, you are mid-dispatch — keep firing tools, do not speak the confirmation yet.

FINAL CONFIRMATION — speak this (or a tight paraphrase) ONCE the moment Wave 2 completes. Never speak this before email_attorneys has run. CRITICAL: name the actual firms you just contacted (read them off the moss result), so the caller knows who's coming and so future turns can refer to them by name:
"I've reached out to <N> attorneys for you: <Firm A>, <Firm B>, and <Firm C>. They know you're at San Francisco County Jail and can reach you through the facility — either by phone or an in-person attorney visit. Hang in there."

POST-DISPATCH Q&A:
Once you've completed dispatch (both waves are in the conversation history and you've spoken the confirmation), the call enters Q&A mode. From now on:
- Answer questions from the moss_find_lawyers result already in your context AND from your previous confirmation in the conversation. Refer to firms by the actual names you already spoke. NEVER invent firm names — if you can't recall the exact name, say "one of the firms I contacted".
- ABSOLUTELY DO NOT CALL ANY TOOLS — not moss_find_lawyers, not email_attorneys, not anything. Tool calls in this phase will be rejected with an error by the server, and the caller will hear nothing useful. Just answer with words.
- Keep answers to 1-2 sentences.
- If the caller asks "who are they" or "what are their names", list the firms you named in the confirmation.
- If the caller wants the dispatch redone with different criteria, acknowledge but be honest that the initial contact is already out: "The first batch is already on its way."
- Short acknowledgments from the caller — "ok", "thanks", "got it", "alright", "k", "cool" — get a brief reciprocal acknowledgment: "You're welcome — hang in there" or "Good luck." NEVER trigger any tools on a brief acknowledgment. NEVER re-speak the dispatch confirmation. Just acknowledge and stop.

IF THE CALLER ALREADY HAS A LAWYER: don't dispatch. Say "Got it — call your lawyer directly. Good luck."

IF THE CALLER REFUSES HELP: don't dispatch. Say "Understood. Good luck."

CRITICAL RULES:
- If you've called moss but NOT email_attorneys, you are mid-dispatch — keep firing tools, no confirmation yet.
- If you've completed dispatch, do not call tools again — answer from context.
- If you've just called any tools, your VERY NEXT output MUST be spoken text. Never stop a turn after a tool result with no text."""


# Iteration budget for one ``generate`` call. Ideal flow is 3 iterations:
# moss → parallel email dispatch (one email_attorneys per firm in one
# iter) → text. Some Gemini variants prefer one-tool-per-iter, which
# needs ~5 iters for a full dispatch + the trailing text turn. Anything
# past that is the model looping — bail with FALLBACK_TEXT.
MAX_TOOL_ITERATIONS: Final[int] = 10

# Per-iteration retry budget when Gemini either raises before yielding the
# first sentence OR returns a completely empty response (no text and no
# tools). Mid-stream failures are NOT retried — the caller has already heard
# part of the answer and re-streaming would duplicate it.
MAX_STREAM_RETRIES_PER_ITER: Final[int] = 2
# Backoff schedule for retries (seconds, indexed by retry attempt).
_RETRY_BACKOFF_S: Final[tuple[float, ...]] = (0.1, 0.3)

# Polite spoken fallback if Gemini errors out before any tools fired —
# i.e., we genuinely can't help. If tools DID fire but the final iter
# returned no text, ``POST_DISPATCH_CONFIRMATION`` is used instead.
FALLBACK_TEXT: Final[str] = (
    "I'm having trouble right now. Please call back, or hang up and "
    "dial a criminal defense attorney directly."
)

# Spoken when Gemini falls silent AFTER a successful dispatch iteration
# (a known flash-lite quirk — see ``generate`` for the detection logic).
# Tells the caller their dispatch happened and how attorneys will reach
# them — through the jail, not via callback.
POST_DISPATCH_CONFIRMATION: Final[str] = (
    "I've reached out to criminal defense attorneys for you. They know "
    "you're at San Francisco County Jail and can reach you through the "
    "facility — by phone or an in-person attorney visit. Hang in there."
)

# Sentence-end punctuation. A boundary is a member of this set followed by
# whitespace; ``5.5`` and ``Dr.<EOF>`` are deliberately not boundaries.
_SENTENCE_END: Final[frozenset[str]] = frozenset(".!?")


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
    """Build the ``GenerateContentConfig`` with system prompt, tools, no thinking.

    Safety settings are dropped to ``BLOCK_NONE`` across all categories.
    The demo intentionally permits the agent to give legal advice and to
    discuss criminal-process topics that the default thresholds tend to
    refuse on; refusing on a jail-phone call is the worst possible UX.
    """
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=TOOL_SCHEMAS)],
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        # Voice answers are short — cap output to avoid runaway TTS cost.
        max_output_tokens=400,
        safety_settings=[
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_CIVIC_INTEGRITY,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            ),
        ],
    )


def _pop_sentence(buffer: str) -> tuple[str | None, str]:
    """Pop the first complete sentence off ``buffer``.

    A sentence ends at ``.!?`` followed by whitespace. ``5.5`` and a
    trailing ``Dr.`` at end-of-buffer are deliberately not boundaries —
    waiting for more tokens lets the boundary detector be precise without
    needing an abbreviation list.

    Returns ``(sentence, remaining)`` on success; ``(None, buffer)`` if
    no complete sentence is present yet.
    """
    n = len(buffer)
    for i, char in enumerate(buffer):
        if char not in _SENTENCE_END:
            continue
        # Punct at end of buffer: wait for more.
        if i + 1 >= n:
            return None, buffer
        # Punct not followed by whitespace (e.g. "5.5", "U.S."): skip.
        if not buffer[i + 1].isspace():
            continue
        sentence = buffer[: i + 1].strip()
        if not sentence:
            continue
        # Consume the trailing whitespace.
        j = i + 1
        while j < n and buffer[j].isspace():
            j += 1
        return sentence, buffer[j:]
    return None, buffer


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


async def _stream_iteration(
    *,
    client: genai.Client,
    model: str,
    contents: list[types.Content],
    config: types.GenerateContentConfig,
) -> AsyncIterator[str | tuple[list[types.FunctionCall], types.Content | None]]:
    """Run one streaming iteration.

    Yields:
        * ``str`` — a complete sentence ready to speak.
        * ``(function_calls, model_content)`` — the iteration ended with
          tool calls; the caller runs them and starts a fresh iteration.

    If the iteration ends with no tool calls, the buffered tail is
    yielded as the final ``str`` and the generator stops.
    """
    buffer = ""
    tool_calls: list[types.FunctionCall] = []
    accumulated_content: types.Content | None = None

    stream = await client.aio.models.generate_content_stream(  # pyright: ignore[reportUnknownMemberType]
        model=model,
        contents=contents,
        config=config,
    )
    async for chunk in stream:
        fcs = chunk.function_calls or []
        if fcs:
            tool_calls.extend(fcs)

        text = chunk.text or ""
        if text:
            buffer += text
            while True:
                sentence, buffer = _pop_sentence(buffer)
                if sentence is None:
                    break
                yield sentence

        candidates = chunk.candidates or []
        if candidates and candidates[0].content is not None:
            accumulated_content = candidates[0].content

    if tool_calls:
        # Hand the buffered tail back as a sentence (rare — most tool-call
        # iterations emit no text at all) before announcing the tool calls,
        # so the caller hears any preamble before the silence of tool work.
        tail = buffer.strip()
        if tail:
            yield tail
        yield (tool_calls, accumulated_content)
        return

    # Pure text iteration — flush whatever's left in the buffer.
    tail = buffer.strip()
    if tail:
        yield tail


@dataclass
class _GeminiCtx:
    """Immutable per-call Gemini wiring (model, client, config)."""

    client: genai.Client
    model: str
    config: types.GenerateContentConfig


@dataclass
class _IterState:
    """Mutable state threaded across iterations of the tool loop."""

    current_iter_text_emitted: bool = False
    final_text_emitted: bool = False
    ended_with_tools: bool = False
    # Names of every tool fn invoked across all iterations of one
    # ``generate`` call. Used by the silent-tail backstop to tell apart:
    #   * nothing fired       → FALLBACK_TEXT ("I'm having trouble")
    #   * only moss fired     → FALLBACK_TEXT (search only, not dispatch)
    #   * dispatch tool fired → POST_DISPATCH_CONFIRMATION (real dispatch)
    tool_names_invoked: set[str] = field(default_factory=set)


# Tools whose successful invocation means an actual dispatch happened —
# moss is search-only, not dispatch.
_DISPATCH_TOOLS: Final[frozenset[str]] = frozenset({"email_attorneys"})


def _silent_tail_text(state: _IterState) -> str:
    """Pick what to say when Gemini ends a turn without emitting text.

    If a dispatch tool fired this turn the work actually happened — speak
    the locked confirmation so the caller knows their info went through.
    If only moss fired (search but no dispatch) or nothing fired, the
    model couldn't actually help; emit the apologetic fallback rather
    than lie about a dispatch that didn't happen.
    """
    if state.tool_names_invoked & _DISPATCH_TOOLS:
        return POST_DISPATCH_CONFIRMATION
    return FALLBACK_TEXT


async def _run_one_iter(
    state: _IterState,
    ctx: _GeminiCtx,
    contents: list[types.Content],
    iteration: int,
) -> AsyncIterator[str]:
    """Drive one Gemini streaming iteration.

    Updates ``state`` to record whether this iteration emitted text,
    whether final text completed, and whether this iteration handed off
    to tool calls. Tool calls are executed inline; the model's content
    and the function responses are appended to ``contents`` so the next
    iteration sees the full thread.
    """
    state.ended_with_tools = False
    state.current_iter_text_emitted = False
    async for item in _stream_iteration(
        client=ctx.client,
        model=ctx.model,
        contents=contents,
        config=ctx.config,
    ):
        if isinstance(item, str):
            state.current_iter_text_emitted = True
            yield item
            continue
        # Tool-call hand-off.
        tool_calls, model_content = item
        state.ended_with_tools = True
        state.tool_names_invoked.update(fc.name for fc in tool_calls if fc.name)
        logger.info(
            "iter=%d: model requested %d tool call(s): %s",
            iteration,
            len(tool_calls),
            [fc.name for fc in tool_calls],
        )
        if model_content is not None:
            contents.append(model_content)
        response_parts = await _run_tool_calls(tool_calls)
        contents.append(types.Content(role="user", parts=response_parts))
        return
    if state.current_iter_text_emitted:
        state.final_text_emitted = True


async def _run_iter_with_retry(
    state: _IterState,
    ctx: _GeminiCtx,
    contents: list[types.Content],
    iteration: int,
) -> AsyncIterator[str]:
    """``_run_one_iter`` with bounded retries on transient failure / empty result.

    Retried cases:
        * Exception raised *before* the first sentence was yielded — safe
          to re-run because nothing reached the caller yet.
        * Iteration completed normally but emitted no text and no tool
          calls — usually a Gemini hiccup; retrying often succeeds.

    Not retried:
        * Exception raised *after* at least one sentence was yielded — the
          caller has already heard part of an answer; re-streaming would
          duplicate it. The exception propagates to ``generate``'s outer
          handler.
    """
    for attempt in range(MAX_STREAM_RETRIES_PER_ITER + 1):
        yielded_anything = False
        try:
            async for sentence in _run_one_iter(state, ctx, contents, iteration):
                yielded_anything = True
                yield sentence
        except Exception:
            if yielded_anything:
                raise
            if attempt >= MAX_STREAM_RETRIES_PER_ITER:
                raise
            logger.warning(
                "iter=%d attempt=%d raised; retrying after %.2fs",
                iteration,
                attempt,
                _RETRY_BACKOFF_S[attempt],
            )
            await asyncio.sleep(_RETRY_BACKOFF_S[attempt])
            continue

        # No exception. Was the iteration useful?
        if yielded_anything or state.ended_with_tools:
            return
        if attempt >= MAX_STREAM_RETRIES_PER_ITER:
            return
        logger.warning(
            "iter=%d attempt=%d returned empty; retrying after %.2fs",
            iteration,
            attempt,
            _RETRY_BACKOFF_S[attempt],
        )
        await asyncio.sleep(_RETRY_BACKOFF_S[attempt])


async def generate(recent_history: list[dict[str, str]]) -> AsyncIterator[str]:
    """Stream the agent's next spoken response, sentence by sentence.

    Args:
        recent_history: AgentPhone ``recentHistory`` array (newest last).
            The last entry is the caller's just-spoken turn.

    Yields:
        Complete sentences as Gemini emits them. Tool calls are run
        inline; the next iteration's first sentence arrives after the
        last tool response is appended. On any error or iteration cap,
        a single ``FALLBACK_TEXT`` sentence is yielded so the caller is
        never left in silence.
    """
    try:
        client = _build_client()
    except RuntimeError:
        logger.exception("Gemini client init failed")
        yield FALLBACK_TEXT
        return

    ctx = _GeminiCtx(
        client=client,
        model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite"),
        config=_build_config(),
    )
    contents = _to_gemini_contents(recent_history)
    state = _IterState()

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            async for sentence in _run_iter_with_retry(state, ctx, contents, iteration):
                yield sentence
        except Exception:
            logger.exception(
                "Gemini stream failed on iter=%d after %d retries",
                iteration,
                MAX_STREAM_RETRIES_PER_ITER,
            )
            if not state.final_text_emitted:
                yield _silent_tail_text(state)
            return
        if not state.ended_with_tools:
            if not state.current_iter_text_emitted:
                logger.warning(
                    "Gemini iter=%d ended with no text or tools (tools_invoked=%s)",
                    iteration,
                    sorted(state.tool_names_invoked),
                )
                yield _silent_tail_text(state)
            return

    logger.warning(
        "tool loop hit MAX_TOOL_ITERATIONS=%d without final text",
        MAX_TOOL_ITERATIONS,
    )
    if not state.final_text_emitted:
        yield FALLBACK_TEXT
