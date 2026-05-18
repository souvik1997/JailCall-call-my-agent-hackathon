# JailCall — Hackathon Spec (as shipped, 2026-05-17)

**Track:** The Fixer (voice + email). **Backup:** Wildcard.

**One-liner:** A real phone number you can call from a police station. An AI voice agent
picks up, takes a 2-field intake (name + charge), semantically routes the case to Bay Area
criminal defense firms, sends real intake emails on the caller's behalf — and remembers the
case across calls so the next time the caller dials in it greets them by name and surfaces
any attorney replies that came back.

**Operator email:** `souvik@amlalabs.com` for every external account signup
(AgentPhone, AgentMail, Browser Use, Supermemory, Moss, Gemini).

This spec documents what is **actually deployed today** for the hackathon demo. The build is
single-caller, single-jurisdiction (Bay Area), single-facility (San Francisco County Jail),
voice-only. Anything not listed under "Sponsor usage" is not in the build.

---

## Scope (shipped)

What the demo does:

- Real AgentPhone number routes inbound voice to FastAPI `/webhook`.
- Two-field intake: name + charge category. No callback number, no jurisdiction question
  (Bay Area is hardcoded), no location question (San Francisco County Jail is hardcoded).
- Moss-backed semantic routing over a pre-indexed roster of 57 Bay Area criminal defense
  firms returns top-3 candidates in sub-second.
- AgentMail sends real outbound intake emails to all 3 firms in parallel during the call.
- Supermemory captures every caller utterance, every dispatch, and every attorney reply
  under one container tag. On the next call, recall is injected as a prior-context turn so
  the agent greets the caller by name and refuses to re-dispatch.
- Inbound attorney replies hit `/webhook/agentmail` and land in Supermemory in real time.
- Local dashboard at `/` streams the live call, tool calls, and Moss results for demo
  observers.

What it deliberately does **not** do — see "Not in scope" below.

---

## Sponsor usage

Six sponsors are wired into the build. Each one has a concrete, load-bearing role.

| Sponsor | Role | Where in code |
|---|---|---|
| **AgentPhone (P26)** | The only voice channel. Real provisioned phone number, HMAC-SHA256 signed webhooks, NDJSON sentence-streamed response. | `server.py` (`/webhook`, `_voice_response_stream`, `_verify_signature`) |
| **Moss (F25)** | Semantic routing. Pre-indexed roster of 57 Bay Area criminal defense firms. Returns top-3 candidates per query with positional firm_ids that defeat URL/email hallucination by the LLM. | `moss.py`, `tools.py::moss_find_lawyers`, `build_index.py` |
| **AgentMail (S25)** | Outbound dispatch + inbound reply ingest. `inboxes.messages.send` for outbound intake emails; `/webhook/agentmail` ingests attorney replies straight into Supermemory. Single shared inbox `sb38318@agentmail.to` for both directions. | `tools.py::email_attorneys`, `server.py::agentmail_webhook` |
| **Supermemory** | Cross-call memory. Caller utterances, dispatch records, attorney replies all stored under one container tag. Recall on every webhook injects a prior-context agent turn so the model remembers the caller and refuses to re-dispatch. | `memory.py`, `server.py::_augment_history_with_recall` |
| **Browser Use (W25)** | **Offline data pipeline only — not in the runtime voice path.** Crawled all 57 firm websites to enrich the routing index with real contact emails. The demo story: "Browser Use made the live dispatch sub-second by enriching the index ahead of time." | `scrape_firm_emails.py` (one-shot, run before demo) |
| **Google DeepMind (Gemini 2.5 flash-lite)** | The LLM driving the agent. Sub-half-second TTFT, streamed token output, parallel tool calls in one iteration. Safety thresholds set to `BLOCK_NONE` so the agent answers legal questions about arraignment, bail, Miranda rights, ICE holds etc. directly. | `controller.py::_build_config`, `controller.py::generate` |

### Sponsor combination story

> Caller dials an **AgentPhone** number. Webhook delivery is verified, every caller turn is
> fire-and-forget written to **Supermemory**. **Gemini 2.5 flash-lite** drives the
> conversation, streaming sentences back over NDJSON. After two intake questions, Gemini
> emits a **Moss** query and three parallel **AgentMail** sends in one iteration. Every
> dispatch is logged to **Supermemory**. If an attorney replies, **AgentMail**'s inbound
> webhook writes the reply to **Supermemory**, so on call 2 recall surfaces it. **Browser
> Use** ran before the demo to enrich the **Moss** index with real intake emails.

---

## Architecture

```
Caller (jail phone)
    │
    ▼
AgentPhone (inbound voice, webhook mode, HMAC-SHA256 signed)
    │
    ▼
FastAPI (jailcall.server)
    ├─ /                       local live dashboard (jailcall/static/index.html)
    ├─ /api/dashboard          in-memory transcript / tools / events snapshot
    ├─ /api/reset-memory       wipe Supermemory + tool-call log between takes
    ├─ /healthz                liveness
    ├─ /webhook                AgentPhone signed inbound; NDJSON streamed reply
    └─ /webhook/agentmail      attorney email reply → Supermemory ingest
        │
        ├─ Supermemory write (caller utterance, fire-and-forget)
        ├─ Supermemory recall (last 40 memories → "[Prior context]" agent turn)
        └─ Gemini 2.5 flash-lite tool-call loop (jailcall.controller)
              ├─ moss_find_lawyers  → Moss (sub-second, sub-10ms typical)
              └─ email_attorneys    → AgentMail send + Supermemory dispatch record

Offline (before the demo):
    Browser Use → scrape_firm_emails.py → firm.txt → firm_profiles.tsv → build_index.py → Moss
```

### Voice flow per webhook delivery

1. AgentPhone POSTs an `agent.message` webhook on `voice` channel.
2. `_authenticate_and_parse` verifies HMAC-SHA256 + freshness (5-min skew), parses body.
3. `_build_webhook_context` extracts `call_id`, transcript, `recentHistory`.
4. Fire-and-forget `record_caller_utterance(call_id, transcript)` writes to Supermemory.
5. `_voice_response_stream` yields:
   - Immediate `{"text": "One moment.", "interim": true}` to cover Gemini TTFT.
   - `_augment_history_with_recall` pulls last 40 memories under `jailcall:demo` and
     prepends a `[Prior context for this caller …]` agent turn. If any prior DISPATCH
     record is present, the header strongly warns the model not to re-dispatch.
   - `current_call_id` and `current_turn_started_at` ContextVars set so the tool layer can
     stamp the tool-call log and distinguish "same turn" vs "prior turn" dispatches.
   - Iterates `controller.generate(augmented_history)`, streaming sentences from Gemini.
     One-sentence lookahead so only the FINAL sentence in a turn is unflagged — every
     prior sentence carries `interim: true`.
6. Non-voice events (e.g. `agent.call_ended`) return `{"ok": true}` and update the
   dashboard's call-status row.

### Controller loop

`jailcall.controller.generate` is the tool-call loop:

- Builds a Gemini client (`gemini-2.5-flash-lite` by default, `GEMINI_MODEL` overridable).
- `_build_config` sets the system prompt, `TOOL_SCHEMAS`, `thinking_budget=0`,
  `max_output_tokens=400`, and `BLOCK_NONE` on every safety category.
- Iteration loop capped at `MAX_TOOL_ITERATIONS = 10`.
- `_run_iter_with_retry`: 2 retries (100ms / 300ms backoff) on
  exception-before-first-yield OR completely-empty iteration. **No mid-stream retries** —
  if Gemini fails after a sentence has been spoken, we don't re-stream and duplicate it.
- `_IterState.tool_names_invoked` tracks every tool fired across iterations. If Gemini
  ends a turn with no spoken text, the **silent-tail backstop** picks:
  - `POST_DISPATCH_CONFIRMATION` if a dispatch tool actually fired (real send happened),
  - `FALLBACK_TEXT` otherwise ("I'm having trouble right now…").

### Tool layer

Two tools, both in `jailcall.tools`. Schemas are Gemini `FunctionDeclaration` objects in
`TOOL_SCHEMAS`. The handler is `run_tool(name, args)`.

#### `moss_find_lawyers(charge_category)`

- Queries Moss against `MOSS_INDEX_NAME` (default `jailcall-lawyers`) with
  `top_k=12, alpha=0.6`.
- Dedupes by firm short_name, keeps the first 3 distinct firms.
- Returns a JSON array of up to 3 candidates, each:
  ```json
  {
    "firm_id": "0",
    "firm_short_name": "lamano-law-office",
    "firm_name": "Lamano Law Office",
    "phone": "+14155551234",
    "email": "intake@lamanolaw.com",
    "form_url": "https://lamanolaw.com/contact",
    "summary": "…"
  }
  ```
- **`firm_id` is positional** — `"0"`, `"1"`, `"2"`. The model echoes it back verbatim to
  `email_attorneys`. The smallest possible identifier makes hallucination effectively
  impossible.
- Caches the candidates per `call_id` in `_moss_results_by_call[call_id]` so the dispatch
  tool can resolve `firm_id` → real metadata server-side.

#### `email_attorneys(firm_id, caller_name, charge_category, message)`

- Resolves `firm_id` from the cache. Hallucinated firm_id → returns
  `{"error": "unknown firm_id", "valid_firm_ids": [...]}`.
- Builds the intake body via `_build_intake_message` which injects the hardcoded facility
  block (name / phone / address / visit_info) and a `Reply to: sb38318@agentmail.to` line.
  The model never has to know facility info.
- **`noreply@` short-circuit:** if the resolved email starts with `noreply@`, the send is
  skipped (returns `{status: "sent", placeholder: true, ...}`) but the dispatch record is
  still written to Supermemory and the dashboard. These addresses are synthetic
  placeholders we wrote into the corpus for firms whose websites didn't list a public
  email — sending there would bounce or hit an unrelated inbox.
- Otherwise calls `client.inboxes.messages.send(inbox_id, to, subject, text)` via
  `asyncio.to_thread`. Inbox id is resolved once from `AGENTMAIL_DISPATCH_INBOX` and
  cached for the process lifetime.
- Writes a `DISPATCH (email): contacted <to> for caller '<name>' on the '<charge>' matter`
  memory to Supermemory.

#### Hard guard against re-dispatch

`run_tool` reads `evals/last_run/tool_calls.jsonl` and short-circuits any
`email_attorneys` call where a prior-turn dispatch entry exists (filtered by
`ts < current_turn_started_at` so parallel sends inside the same turn aren't false
positives). Returns `{"error": "dispatch_already_completed", ...}` and the model answers
from prior context instead.

### Memory layer

`jailcall.memory` is the Supermemory wrapper. One container tag for the whole service:
`DEMO_CONTAINER_TAG` (default `jailcall:demo`, overridable via `JAILCALL_MEMORY_TAG`).
Supermemory's Python SDK is sync; every call is wrapped in `asyncio.to_thread`. Failures
log but never raise — Supermemory hiccups must not break the live voice path.

Entry points:

| Function | Writes / reads | Used by |
|---|---|---|
| `record_caller_utterance(call_id, text)` | One memory per caller turn | `server.webhook` fire-and-forget |
| `record_dispatch_attempt(channel, target, caller_name, charge)` | `DISPATCH (email): …` memory | `email_attorneys` (both real and placeholder sends) |
| `record_attorney_reply(sender, subject, text)` | `Attorney reply from X — subject: …` memory | `/webhook/agentmail` |
| `recall_context(limit=40)` | Returns recent memories oldest-first | `_augment_history_with_recall` per turn |
| `clear_memory()` | Bulk-delete via `documents.delete_bulk(container_tags=[...])` | `server.lifespan` startup + `/api/reset-memory` |

### Lifespan (startup)

`server.lifespan` runs three things on startup, in order:

1. `get_moss_client().load_index(MOSS_INDEX_NAME)` so subsequent queries are local.
2. `clear_memory()` — wipe Supermemory under the demo tag.
3. `clear_tool_call_log()` — wipe `evals/last_run/tool_calls.jsonl`.
4. `clear_moss_result_cache()` — drop the per-call firm-id cache.

Single-caller demo always starts blank. The same wipe is exposed at `POST /api/reset-memory`
so the demo can be reset between takes without restarting the process.

### Facility (hardcoded)

`jailcall.facility.current_facility()` always returns:

```
San Francisco County Jail (Jail #2)
+1 (415) 555-0100
425 7th Street, San Francisco, CA 94103
Attorney visits 24/7 in the professional visiting room with a valid bar card.
```

The caller never tells the agent where they are. Attorneys reach the caller through the
facility's intake line or by attorney visit — not via a personal callback number, because
the caller is in custody.

### Offline data pipeline (Browser Use)

`law_firms/` has 57 firm directories. Each has a `firm.txt` (curated metadata) and a
`site_text/` subdir of normalized website chunks. `firm_profiles.tsv` is the routing-table
view that `build_index.py` reads.

`jailcall.scrape_firm_emails` is a one-shot Browser Use crawler:

1. Walks `law_firms/`, finds firms with empty `Email:` (or with `--retry-noreply`,
   placeholder emails too).
2. For each firm, runs `AsyncBrowserUse().run(...)` against the firm's intake URL or
   homepage with a prompt that finds the single best contact email or returns `NONE`.
3. Writes the result back into `firm.txt` and syncs into `firm_profiles.tsv` so
   `build_index.py` picks it up on the next build.

`jailcall.build_index`:

- Reads `firm_profiles.tsv`.
- Builds three doc types per firm with full contact metadata on every chunk:
  - `firm_profile` (one per firm)
  - `site_text` (chunked website text)
  - `representative_case` (when present in `cases.tsv`)
- `client.create_index(MOSS_INDEX_NAME, docs, "moss-minilm")` then `load_index`.
- Idempotent — drops and recreates.

### Final email coverage (after Browser Use + manual curation)

- **57 / 57** firms have an email in the index.
- **~6** real attorney emails scraped by Browser Use (Tayac, Hoorfar, Tully & Weiss,
  Gorelick, Nieves, Perani).
- **22** `noreply@<domain>` placeholders — handled at runtime by the short-circuit in
  `email_attorneys`; never actually sent to.
- **29** originally-curated emails from the corpus seed pass.

---

## Voice script (as shipped)

The opening greeting is configured server-side in the **AgentPhone portal** as
`beginMessage` — it plays automatically when the call connects, before the first webhook
delivery. The controller's system prompt explicitly tells the model: never re-speak the
opening.

### Intake (two fields, no callback)

After the caller's first reply (treated as consent to engage), the agent asks:

1. "What's your name?"
2. After name: "What are you charged with? Just the general category — DUI, drug,
   assault, anything like that."

The caller may dump both at once ("John Doe, DUI") — the agent takes what it gets and
dispatches. The caller may say "I don't know" for charge — agent accepts `"unknown"` and
continues.

The agent **does not** ask where the caller is (facility hardcoded), does not ask for a
callback number (the caller is in custody), and does not ask about jurisdiction (Bay Area
hardcoded).

### Dispatch (mandatory two-wave sequence)

Once name + charge are present, the model fires:

- **Wave 1 — Moss search (one iteration):** `moss_find_lawyers(charge_category=…)`,
  exactly once. No retries with synonyms.
- **Wave 2 — AgentMail sends (one iteration, parallel):** one `email_attorneys` call per
  firm returned, emitted as parallel function-call parts in a single iteration. The model
  passes `firm_id="0"`, `firm_id="1"`, `firm_id="2"` verbatim from the Moss output. The
  handler resolves `firm_id` → real email server-side.

### Final confirmation (spoken once Wave 2 completes)

> "I've reached out to N attorneys for you: <Firm A>, <Firm B>, and <Firm C>. They know
> you're at San Francisco County Jail and can reach you through the facility — either by
> phone or an in-person attorney visit. Hang in there."

If Gemini falls silent after a successful dispatch iteration (a known flash-lite quirk),
the silent-tail backstop in `controller.generate` speaks `POST_DISPATCH_CONFIRMATION`,
which carries the same content.

### Post-dispatch Q&A

After dispatch completes, the call enters Q&A mode. The agent answers legal questions
about the criminal process directly using the Gemini context — bail, arraignment, Miranda,
public defenders vs private counsel, ICE holds, what to expect over the next few days.
Safety thresholds are `BLOCK_NONE`. Brief caller acknowledgements ("ok", "thanks") get a
brief reciprocal acknowledgement and never trigger tools. Any attempt to re-dispatch is
hard-blocked by `run_tool`.

### Returning caller (call 2+)

`_augment_history_with_recall` injects a prior-context agent turn at the front of the
conversation. The system prompt teaches the model to:

- Greet by name if recall has it.
- Skip re-asking name and charge.
- Refuse to re-dispatch if a `DISPATCH STATUS` header is present.
- Surface attorney replies proactively if any `Attorney reply` memory exists.

---

## System prompt

Defined verbatim in `jailcall.controller.SYSTEM_PROMPT`. Key rules (do not paraphrase
without checking the source):

- The caller is at San Francisco County Jail; never ask where they are.
- The caller is in custody; do not ask for a callback number.
- Legal advice is **authorized** — the agent answers questions directly. Refusal is wrong
  here.
- Intake is 2 fields: name + charge. Don't re-ask fields already in history or prior
  context.
- AgentPhone has already played the opening; never re-speak it. Treat "hello"/"hi"/"help
  me" on turn 1 as engagement and go straight to asking for the name.
- Dispatch is mandatory two-wave: Moss once, then parallel `email_attorneys` per firm in
  one iteration. Pass `firm_id` verbatim. Don't pass emails, URLs, or callback numbers —
  those schema fields don't exist anymore.
- Post-dispatch: ABSOLUTELY DO NOT CALL ANY TOOLS. Answer from context.
- If prior context says dispatch is done, don't dispatch again.

---

## File structure

```
jailcall/
├── __init__.py
├── server.py              # FastAPI app; /webhook, /webhook/agentmail, /api/*, /
├── controller.py          # Gemini 2.5 flash-lite tool-call loop + system prompt
├── tools.py               # TOOL_SCHEMAS, moss_find_lawyers, email_attorneys, run_tool
├── memory.py              # Supermemory wrapper (record_*, recall_context, clear_memory)
├── moss.py                # RealMossClient (no fallback; requires MOSS_PROJECT_*)
├── facility.py            # Hardcoded DEMO_FACILITY (SF County Jail Jail #2)
├── call_context.py        # current_call_id, current_turn_started_at ContextVars
├── dashboard.py           # In-memory state for the live dashboard
├── config.py              # require_env + small constants
├── setup_agent.py         # One-time AgentPhone provisioning script
├── build_index.py         # One-time Moss index builder from law_firms/
├── scrape_firm_emails.py  # One-time Browser Use email enrichment (OFFLINE)
└── static/
    ├── index.html         # Live demo dashboard
    ├── dashboard.css
    └── dashboard.js

law_firms/                 # 57 firm directories + firm_profiles.tsv (curated)
evals/
├── interactive.py         # Type-at-the-agent REPL — signs an AgentPhone-shape webhook
├── captures/              # Raw webhook deliveries persisted at runtime (gitignored)
└── last_run/              # tool_calls.jsonl + Moss preview JSONL (gitignored)
pyproject.toml             # Deps + lint config (no requirements.txt)
uv.lock                    # Locked dependency versions
.env                       # API keys (gitignored)
.env.example               # Template
SPEC.md                    # This file
TOOLS.md                   # Vendor SDK/API reference
CLAUDE.md                  # Project guidance for Claude / Codex sessions
```

---

## Env vars

```
# Voice + LLM
AGENTPHONE_API_KEY=sk_live_...
AGENTPHONE_WEBHOOK_SECRET=whsec_...
AGENTPHONE_NUMBER_ID=...
AGENTPHONE_NUMBER=+15551234567
AGENTPHONE_AGENT_ID=...
AGENTPHONE_VOICE_ID=...
PUBLIC_WEBHOOK_BASE_URL=https://your-server.example.com
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash-lite

# Routing
MOSS_PROJECT_ID=...
MOSS_PROJECT_KEY=moss_...
MOSS_INDEX_NAME=jailcall-lawyers

# Dispatch
AGENTMAIL_API_KEY=...
AGENTMAIL_DISPATCH_INBOX=sb38318@agentmail.to

# Memory
SUPERMEMORY_API_KEY=sm_...
JAILCALL_MEMORY_TAG=jailcall:demo

# Offline pipeline only
BROWSER_USE_API_KEY=bu_...
BROWSER_USE_SCRAPE_MAX_COST_USD=1.00

# App
PORT=5321
LOG_LEVEL=INFO
```

`ANTHROPIC_API_KEY` exists in `.env.example` for completeness but is not used by the
shipped controller — the active brain is Gemini. Same for `STRIPE_SECRET_KEY` and
`SPONGE_API_KEY` — present in the env template, not wired into the build.

---

## Build & run

```bash
# 1. Install deps.
uv sync

# 2. Fill .env from .env.example.
cp .env.example .env  # then edit

# 3. Build the Moss index from the law_firms/ corpus.
uv run python -m jailcall.build_index --push

# 4. (Optional, before demo) refresh attorney emails via Browser Use.
uv run python -m jailcall.scrape_firm_emails --retry-noreply
uv run python -m jailcall.build_index --push  # rebuild after scrape

# 5. Provision AgentPhone (one-time per session if using a fresh tunnel).
uv run python -m jailcall.setup_agent

# 6. Run the server.
uv run python -m jailcall.server   # listens on 127.0.0.1:5321

# 7. Either dial the AgentPhone number, or test in-terminal:
uv run python -m evals.interactive
```

Reset between demo takes (wipes Supermemory + tool-call log + Moss firm-id cache):

```bash
curl -X POST http://127.0.0.1:5321/api/reset-memory
```

---

## Not in scope (deliberately, for the demo)

Things judges sometimes look for that are **not** in the build:

- **Stripe Agent Toolkit, Sponge** — payments are out of scope.
- **SMS / iMessage** — voice-only. AgentPhone is inbound voice only.
- **`classify_location` / jurisdiction routing** — Bay Area is hardcoded.
- **`contact_attorneys` (Browser Use form fill at call time)** — removed from the runtime
  path. Browser Use is offline-only (corpus enrichment). Email is the only dispatch
  channel. This change is what made the live demo feel sub-second per dispatch instead of
  30–90s per firm.
- **Multi-caller support** — single container tag, single demo persona. Cross-call recall
  always pulls from the same tag.
- **Callback-number intake** — the caller is in custody; attorneys reach them through the
  facility. Schema field removed.
- **Privilege-safe "unsafe input" interrupt** — disabled. The demo lets the caller talk
  freely about their case.
- **Legal-advice refusals / "I am not a lawyer" caveats** — explicitly disabled. The
  agent answers legal questions directly because that is the most useful thing it can do
  for someone in custody. Gemini safety settings are `BLOCK_NONE`.
- **`recall_case_context` / `update_case_memory` as Gemini-visible tools** — these are
  server-side functions (`memory.recall_context`, `memory.record_*`), not callable tools.
  Recall happens automatically per webhook; writes happen automatically inside other
  tools. The model never sees them in `TOOL_SCHEMAS`.

---

## Demo script (for judges)

1. **Frame:** "Imagine you've just been arrested. You're at the police station. You don't
   have a lawyer's number memorized."
2. **Call 1.** Dial the real JailCall number live. AgentPhone plays the opening. Walk
   through: "John Doe", "DUI". On the projector show the dashboard:
   - Moss returning 3 candidates in sub-second.
   - Three parallel AgentMail sends fired in one iteration.
   - One real intake email landing in the AgentMail inbox.
3. **Reply lands.** Send a fake attorney reply to `sb38318@agentmail.to`. AgentMail's
   webhook fires `/webhook/agentmail` → Supermemory captures it.
4. **Reset framing:** "Now you're moved to a different facility. You get another phone
   call."
5. **Call 2.** Dial again. The agent opens with a personalized greeting referencing John's
   name and DUI charge (from recall). Ask "did anyone get back to me?" — the agent
   surfaces the attorney reply by name. Ask "what happens at arraignment?" — the agent
   answers using case context (DUI, SF County, Bay Area).
6. **Close:** "The law says you get a phone call. JailCall is the number that always
   answers — and the only one that remembers what's going on with your case."
