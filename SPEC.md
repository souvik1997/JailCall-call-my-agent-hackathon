# JailCall — Hackathon Spec

**Track:** The Fixer (multi-channel: voice + email + browser)
**Backup track:** Wildcard

**One-liner:** Call a number from the police station, an AI agent picks up, and Bay Area criminal defense attorneys are contacted on your behalf before you leave booking.

**Operator email:** Use `souvik@amlalabs.com` when signing up for every external account in this build (AgentPhone, AgentMail, Browser Use, Supermemory, Moss, Stripe, Sponge). Verification codes go there.

> **HACKATHON SCOPE.** This is a one-day hackathon demo. It is not designed to be general or production-ready. Specific load-bearing simplifications: jurisdiction is hardcoded to the Bay Area (no `classify_location`), no SMS confirmation, English-only callers, no caller memory across sessions. Prefer the tightest path to a working demo over breadth.

---

## Scope for today

Ship a working end-to-end demo: a real phone number you can call, a privilege-safe voice agent that collects minimal routing info, and a browser agent that finds and contacts Bay Area criminal defense attorneys.

**Hardcoded jurisdiction:** Bay Area. The agent does not ask the caller where they are and does not classify location. All dispatch goes to Bay Area criminal defense attorneys.

**Not in scope:** attorney roster/SLA system, payments, persistent caller memory across sessions, multi-metro number provisioning, collect-call acceptance, **non-English callers**, **non-Bay-Area callers** (location is hardcoded; we do not handle out-of-area dispatch), **SMS confirmation** (dropped — the inbound voice line is the only AgentPhone channel).

---

## Architecture

```
Caller (jail phone)
    │
    ▼
AgentPhone (inbound voice, webhook mode)
    │
    ▼
server.py (FastAPI)
    ├─ /webhook  HMAC-SHA256 verify, NDJSON streaming reply
    └─ Claude tool-call loop (haiku 4.5)
        ├─ moss_find_lawyers  → Moss (semantic lookup over pre-indexed Bay
        │                       Area attorney roster — sub-10 ms)
        ├─ contact_attorneys  → Browser Use (submit attorney intake/contact forms)
        └─ email_attorneys    → AgentMail (send intake email)
```

**Routing strategy.** Moss handles initial routing against a pre-indexed dataset of Bay Area
criminal defense firms (`law_firms/`), so the search step fits inside a voice turn (sub-10 ms).
Browser Use is reserved for the slow downstream action — actually filling out the chosen
firms' contact forms — and AgentMail handles the email path. The Moss index is built once
before the demo by `build_index.py` (see file structure below).

---

## Voice script (privilege-safe)

The agent MUST follow this script. No deviation. The call is potentially recorded by the facility.

### Opening (beginMessage)

> "You've reached JailCall. I am not a lawyer and this call may be recorded by the facility. Do not tell me what happened or any details about your case. I can help contact a criminal defense attorney on your behalf right now. Would you like me to do that?"

### If yes — collect routing info only

1. **"What is your full name?"** → store as `caller_name`
2. **"Do you know what you've been charged with? Just the general category — like DUI, assault, drug charge, or you can say you don't know."** → store as `charge_category`
3. **"Is there a phone number where an attorney can reach you or a family member? This could be the number you're calling from, or someone on the outside."** → store as `callback_number`

The agent does NOT ask about location. The jurisdiction is hardcoded to the Bay Area.

### Confirmation

> "I have your information. I'm contacting criminal defense attorneys in the Bay Area right now. They will reach out using the callback number you gave me. Remember — do not discuss your case with anyone except your attorney. You have the right to remain silent."

### Unsafe input handling

If the caller starts describing what happened, the agent MUST interrupt:

> "I need to stop you there. This call may be recorded and anything you say could be used against you. Please do not tell me what happened. Save that for your attorney. Can I get your name instead?"

---

## Tools

Three tools. Jurisdiction is hardcoded to the Bay Area, so there is no `classify_location` and no `county`/`state` arguments.

### 1. moss_find_lawyers

**Purpose:** Query the pre-built Moss index of Bay Area criminal defense firms for the top
candidates matching the caller's charge category. The Moss index is built once from
`law_firms/` by `build_index.py` (see file structure) and loaded into memory on server
startup, so this tool returns in sub-10 ms — comfortably inside a voice turn.

**Input:**
```json
{
  "charge_category": "DUI"
}
```

**Implementation:**
```python
async def moss_find_lawyers(args: dict) -> str:
    charge = (args.get("charge_category") or "criminal defense").strip()

    # Built query — biased toward charge category, kept Bay Area implicit because every
    # document in the index is already a Bay Area firm. Hybrid retrieval (alpha=0.6) so
    # exact-term hits ("DUI", "drug") still beat pure semantic neighbours.
    query = f"{charge} criminal defense attorney"

    result = await moss_client.query(
        os.environ["MOSS_INDEX_NAME"],
        query,
        QueryOptions(top_k=3, alpha=0.6),
    )

    # Each Moss doc carries the firm's contact info in metadata (see build_index.py).
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
    return json.dumps(candidates)
```

**Latency note:** Moss is the fast step (sub-10 ms once the index is loaded). The slow step
in this workflow is now `contact_attorneys` (Browser Use form fill — 30-90 s). The interim
NDJSON chunk lives before `contact_attorneys`, not here.

**Index prerequisite:** `build_index.py` must have populated the `MOSS_INDEX_NAME` index
from `law_firms/` before the server starts, and `server.py`'s startup hook must call
`client.load_index(MOSS_INDEX_NAME)` so queries run from the loaded local copy.

### 2. contact_attorneys

**Purpose:** Use Browser Use to fill out contact/intake forms on attorney websites. Called
once per firm returned by `moss_find_lawyers` that has a `form_url`.

**Input:**
```json
{
  "form_url": "https://smithlaw.com/contact",
  "caller_name": "John Doe",
  "charge_category": "DUI",
  "callback_number": "+15551234567",
  "message": "URGENT: Person in custody in the Bay Area needs criminal defense representation. Please call back ASAP."
}
```

**Implementation:**
```python
async def contact_attorneys(args: dict) -> str:
    client = AsyncBrowserUse()
    task = (
        f"Go to {args['form_url']}. Fill out the contact form with: "
        f"Name: {args['caller_name']}, "
        f"Phone: {args['callback_number']}, "
        f"Message: {args['message']}. "
        f"Submit the form. Confirm submission."
    )
    result = await client.run(task)
    return result.output
```

**Latency note:** This is the slow step (30-90 s per form). Stream an interim NDJSON chunk
before kicking it off:
> "I'm reaching out to attorneys in the Bay Area now. This will take a moment."

### 3. email_attorneys

**Purpose:** Send structured intake emails to attorneys whose email addresses were found.

**Input:**
```json
{
  "to": "intake@smithlaw.com",
  "caller_name": "John Doe",
  "charge_category": "DUI",
  "callback_number": "+15551234567"
}
```

**Implementation:**
```python
async def email_attorneys(args: dict) -> str:
    inbox = agentmail_client.inboxes.create(client_id="jailcall-dispatch")

    subject = "URGENT: Person in custody — Bay Area"
    body = (
        f"A person currently in custody in the Bay Area has requested legal representation.\n\n"
        f"Name: {args['caller_name']}\n"
        f"Charge category: {args['charge_category']}\n"
        f"Callback: {args['callback_number']}\n\n"
        f"Please call back as soon as possible.\n\n"
        f"— JailCall (automated legal access service)"
    )

    agentmail_client.inboxes.messages.send(
        inbox.inbox_id,
        to=args["to"],
        subject=subject,
        text=body,
    )
    return f"Email sent to {args['to']}"
```

---

## System prompt

```python
SYSTEM_PROMPT = """You are JailCall, an emergency legal access agent on a live phone call.

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
```

---

## File structure

```
jailcall/
├── __init__.py
├── server.py              # FastAPI webhook handler + tool-call loop; loads Moss index on startup
├── tools.py               # Tool schemas + handler implementations (Moss, Browser Use, AgentMail)
├── config.py              # Env vars, constants
├── setup_agent.py         # One-time: create AgentPhone agent + webhook
└── build_index.py         # One-time: parse law_firms/*/firm.txt and create the Moss index
law_firms/                 # Pre-scraped Bay Area criminal defense firms (Codex-generated;
                           #   one directory per firm, each with firm.txt + optional case
                           #   subdirectories). Source data for build_index.py — not loaded
                           #   at runtime by server.py.
pyproject.toml             # Deps + lint config (source of truth — no requirements.txt)
uv.lock                    # Locked dependency versions
.env                       # API keys (gitignored)
.env.example               # Template for .env
CLAUDE.md                  # Project guidance for Claude Code / Codex sessions
SPEC.md                    # This file
TOOLS.md                   # Vendor SDK/API reference
evals/transcripts.jsonl    # Scenario eval set (controller-shape, not AgentPhone-shape)
```

---

## Dependencies

```
fastapi>=0.115.0
uvicorn>=0.30.0
anthropic>=0.39.0
python-dotenv>=1.0.0
httpx>=0.28.0
moss>=1.0.0
browser-use-sdk
agentmail
agentphone
```

> Source of truth is `pyproject.toml`. The list here is informational; do not maintain a
> separate `requirements.txt`.

---

## Env vars

```
AGENTPHONE_API_KEY=sk_live_...
AGENTPHONE_WEBHOOK_SECRET=whsec_...
AGENTPHONE_NUMBER_ID=...
AGENTPHONE_NUMBER=+15551234567
AGENTPHONE_AGENT_ID=...
AGENTPHONE_VOICE_ID=...
PUBLIC_WEBHOOK_BASE_URL=https://your-server.example.com
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
MOSS_PROJECT_ID=...
MOSS_PROJECT_KEY=moss_...
MOSS_INDEX_NAME=jailcall-lawyers
BROWSER_USE_API_KEY=bu_...
AGENTMAIL_API_KEY=...
PORT=5321
```

---

## AgentPhone voice setup playbook

How AgentPhone handles speech, and the exact ordered steps to go from no account to "the number speaks our `beginMessage`." AgentPhone owns the entire voice stack — STT, TTS, codec, barge-in. We choose a voice once and return `{"text": "..."}` from the webhook; AgentPhone synthesizes and streams it to the caller. **No external TTS to integrate.**

**Voice mode:** `webhook` (we control the LLM). `hosted` is a non-goal — we need the Claude tool-call loop.

### Steps

1. **Sign up.** `POST /v0/agent/sign-up` with `{"email": "souvik@amlalabs.com"}` mails a code. `POST /v0/agent/verify` with `{"email": "souvik@amlalabs.com", "code": "..."}` returns `{"api_key": "sk_live_..."}`. Save as `AGENTPHONE_API_KEY`.

2. **Provision a number.** `POST /v1/numbers` with empty body. Response includes `id` (the number id) and `phoneNumber` (E.164). Save `id` as `AGENTPHONE_NUMBER_ID`.

3. **Pick a voice.** `GET /v1/agents/voices` returns objects with `voice_id`, `voice_name`, `provider`, `gender`, `accent`, `preview_audio_url`. For JailCall, pick a calm, lower-register, professional voice — avoid bright/sales-y voices. Listen to `preview_audio_url` before committing. The caller is at a jail phone and needs to trust the voice in the first 5 seconds. Save the chosen `voice_id`.

4. **Public tunnel.** AgentPhone needs to POST webhooks to a public HTTPS URL, but our FastAPI server runs on `localhost:5321`. Pick one of:
   - `cloudflared tunnel --url localhost:5321/` — no signup, prints a `https://*.trycloudflare.com` URL to stdout. Install: `sudo pacman -S cloudflared` (or grab the binary from Cloudflare).
   - `ngrok http 5321` — requires a free account + authtoken; ships a request inspector at `http://localhost:4040` that's invaluable for replaying webhook payloads while debugging.

   Free-tier URLs from either tool rotate on restart, so Step 5 must re-run each session — roll Steps 5–7 into `setup_agent.py`. **Demo phase:** swap the tunnel for a real deploy (Railway, Fly, Render) so a sleeping laptop or flaky Wi-Fi can't kill the demo.

5. **Register the webhook.** `POST /v1/webhooks` with `{"url": "https://…/webhook", "timeout": 90}`. Returns `{"secret": "whsec_..."}`. Save as `AGENTPHONE_WEBHOOK_SECRET`. The 90-second timeout is non-default — `contact_attorneys` runs Browser Use form fills at 30–90s each and the default 30s will sever the call mid-dispatch.

6. **Create the agent.** `POST /v1/agents` with `voiceMode: "webhook"`, the **verbatim** `beginMessage` from the Voice script section, the chosen `voice_id` as `voice`, and `modelTier: "turbo"`. The agent's `systemPrompt` field is hosted-mode only — our system prompt lives in the Anthropic call, not here.

7. **Attach the number.** `POST /v1/agents/{agent_id}/numbers` with `{"numberId": "<NUMBER_ID>"}`. Any call to the number now routes to our webhook.

8. **Webhook handler (`server.py`).** Verify HMAC-SHA256 signature (headers `X-Webhook-Signature`, `X-Webhook-Timestamp`, `X-Webhook-ID`, `X-Webhook-Event`; reject timestamps older than 5 min). For `event: "agent.message"` + `channel: "voice"`, read `data.transcript`, return a JSON object with a `text` field. Start with a dumb echo response to verify the round-trip before wiring Claude. **Non-object responses are silently ignored** — first thing to check if the caller hears nothing.

9. **Test by dialing the real number.** `/v1/calls/web` exists but returns an access token for the `agentphone-web-sdk` (not a clickable URL), so a host page is needed to use it. For hackathon speed, dial the provisioned number from your phone — at $0.13/min, ten test calls is $1.30 against the $5 signup credit. Listen for voice quality, pronunciation of "JailCall" and "DUI", and end-pointing on short replies like "yes."

10. **Switch to NDJSON streaming.** Return `StreamingResponse(generate(), media_type="application/x-ndjson")` where `generate()` yields `{"text": "...", "interim": true}` *before* any slow tool, then `{"text": answer}` after. Silence on a jail phone is a product killer; the interim chunk is what hides Browser Use's 30–90s form fill behind speech. (Moss lookup is sub-10 ms — no interim chunk needed for `moss_find_lawyers`.)

### Things that bite people

- Webhook response must be a JSON **object**. Lists, strings, bare numbers → caller hears silence.
- Default voice webhook timeout is 30s; raise to 90 at registration for Browser Use tools.
- `recentHistory` (on the event payload) must be threaded into Anthropic `messages` or each turn is amnesiac. See the `to_anthropic_history` helper in the TOOLS.md cookbook.
- The agent's `systemPrompt` field only fires in hosted mode. Don't waste effort populating it.
- Emergency / N11 numbers (911, 211, 411, …) are blocked from provisioning/dialing.
- Free ngrok / cloudflared tunnels rotate URLs on restart — re-run webhook registration each session.
- `interim: true` is NDJSON-only; it does nothing in a plain JSON response.

---

## Delivery architecture and milestones

The build should move in vertical slices. Each milestone must leave the repo in a runnable
state, with one concrete thing that can be dialed, invoked, or verified from the command line.
Do not spend time polishing downstream channels until the upstream voice path is working.

### System slices

| Slice | Files | Responsibility |
|---|---|---|
| Runtime config | `config.py`, `.env.example` | Load env vars, choose model, configure host/port, centralize service constants. |
| AgentPhone setup | `setup_agent.py` | Register webhook, create webhook-mode agent, attach number, print IDs/secrets to store in `.env`. |
| Moss index build | `build_index.py`, `law_firms/` | Parse `law_firms/*/firm.txt`, construct `DocumentInfo`s with contact metadata, `create_index` on Moss. One-time per dataset refresh. |
| Voice webhook | `server.py` | Verify AgentPhone signatures, parse voice events, stream NDJSON replies, map `recentHistory` to Claude messages, `load_index` on startup. |
| Conversation controller | `server.py` | Keep per-call in-memory intake state, enforce the locked script order, decide when dispatch begins. |
| Claude tool loop | `server.py`, `tools.py` | Run bounded Anthropic tool-use loop with the exact tool names and schemas from this spec. |
| External tools | `tools.py` | Implement Moss attorney lookup, Browser Use form submission, and AgentMail email. |
| Demo observability | `server.py` | Log call id, field collection, attorneys found, and emails/forms submitted. |

### Milestone 0 - Repo baseline and guardrails (15 min)

**Goal:** Make the current scaffold predictable before touching external services.

**Build:**
- Confirm `uv sync` works and the app imports.
- Keep `SPEC.md` as product truth and `TOOLS.md` as vendor truth.
- Add missing env slots if implementation needs them, but never commit `.env`.
- Decide that demo state is in-memory only. Persistent caller memory is out of scope.

**Acceptance:**
- `uv run ruff check .` passes.
- `uv run basedpyright` passes.
- `uv run python -m jailcall.server` starts and `/healthz` returns `{"status":"ok"}`.

### Milestone 1 - AgentPhone provisioning path (30-45 min)

**Goal:** One command can create or update the real phone entry points.

**Build:**
- Implement `setup_agent.py` using `httpx` and AgentPhone REST endpoints from `TOOLS.md`.
- Inputs: `AGENTPHONE_API_KEY`, `AGENTPHONE_NUMBER_ID`, public webhook base URL, optional voice id.
- Register `/webhook` with `timeout: 90`.
- Create a webhook-mode agent with the verbatim `beginMessage`.
- Attach the configured number to the agent.
- Print the webhook secret, agent id, number id, and phone number in a copyable summary.

**Acceptance:**
- Running setup returns a webhook secret to place in `.env`.
- Dialing the number reaches AgentPhone and speaks the exact opening message.
- The agent is in `webhook` mode, not hosted mode.

### Milestone 2 - Signed webhook echo with NDJSON (45 min)

**Goal:** Prove that AgentPhone can call our server and hear a response.

**Build:**
- Add `POST /webhook` in `server.py`.
- Verify `X-Webhook-Signature`, `X-Webhook-Timestamp`, `X-Webhook-ID`, and `X-Webhook-Event`.
- Reject invalid signatures and timestamps older than 5 minutes.
- For non-voice or non-`agent.message` events, return `{"ok": true}`.
- For voice messages, stream NDJSON with an immediate interim chunk and then a simple scripted reply.

**Acceptance:**
- Invalid signature returns 401.
- Valid mock request returns `application/x-ndjson`.
- A real call can say one phrase and hear a response.
- Caller never hears silence before slow work.

### Milestone 3 - Scripted intake controller (60 min)

**Goal:** Collect the three routing fields in the locked order before any attorney dispatch.

**Build:**
- Add an in-memory call state keyed by AgentPhone conversation id or call id.
- Track `consent`, `caller_name`, `charge_category`, and `callback_number`.
- Ask exactly one routing question at a time, using the wording in the Voice script section.
- Detect obvious unsafe narrative phrases before sending text to the model and return the locked interrupt.
- After all fields are present, return the exact confirmation template and start dispatch.

**Acceptance:**
- A local mock sequence collects all three fields in order.
- Unsafe input receives the required interrupt text.
- The controller never asks "what happened" and never asks for location.
- A caller who says "I don't know" for charge still progresses.

### Milestone 4 - Claude tool loop (45-60 min)

**Goal:** Add the Anthropic loop wiring before introducing external slow tools.

**Build:**
- Define the three tool schemas (`moss_find_lawyers`, `contact_attorneys`, `email_attorneys`) in `tools.py`, matching this spec.
- Implement `run_tool_call` in `server.py` using the bounded loop pattern from the cookbook.
- Convert AgentPhone `recentHistory` into Anthropic `messages`.
- Keep model default at `claude-haiku-4-5-20251001`.

**Acceptance:**
- The Claude loop runs end-to-end on a mock turn, returning a final assistant message.
- Tool-loop max iterations prevents runaway calls.
- The voice path can complete intake and trigger at least one tool call without timing out.

### Milestone 5 - Moss attorney routing (60-90 min)

**Goal:** Build a Moss index over `law_firms/` and wire the `moss_find_lawyers` tool so the
search step fits inside a voice turn.

**Build:**
- Implement `build_index.py`:
  - Walk `law_firms/*/firm.txt` and parse the structured key-value lines (`Name`, `Short name`,
    `Website`, `Phone`, `Email` if present, `Address`, `Areas of practice`, `Attorneys observed`,
    etc. — the dataset is Codex-generated, so tolerate missing/extra fields).
  - For each firm, build a `DocumentInfo` with `id = short_name`, `text` containing the
    semantically-searchable surface (firm name + practice areas + attorney names + free-text
    notes), and `metadata` containing the contact fields (`name`, `phone`, `email`, `website`,
    `intake_url`).
  - Call `client.create_index(MOSS_INDEX_NAME, docs, "moss-minilm")`. Idempotent: drop/recreate
    is fine for a one-time build.
- Add a FastAPI `lifespan` handler in `server.py` that calls `client.load_index(MOSS_INDEX_NAME)`
  once at startup so subsequent `query` calls run from the local loaded copy.
- Implement `moss_find_lawyers(args)` taking `charge_category` only; Bay Area is hardcoded.
  Query against the loaded index with `QueryOptions(top_k=3, alpha=0.6)`. Return a JSON
  array of firm objects (firm_name, phone, email, form_url, summary).

**Acceptance:**
- `uv run python -m jailcall.build_index` populates the Moss index from `law_firms/`.
- Standalone invocation of `moss_find_lawyers` returns up to 3 attorney candidates in
  under ~50 ms.
- At least one candidate has either an email address or a form URL.
- No interim NDJSON chunk is required before this tool — Moss is fast enough to call
  inline in the voice turn.

### Milestone 6 - Attorney contact forms (60 min)

**Goal:** Submit web intake forms where Browser Use found contact URLs.

**Build:**
- Implement `contact_attorneys(args)` using Browser Use.
- Generate a privilege-safe message that includes only routing info:
  `URGENT: Person in custody in the Bay Area needs criminal defense representation. Please call back ASAP.`
- Do not include incident facts, confessions, or narrative details.
- Return a structured status per form: submitted, failed, skipped, and reason.

**Acceptance:**
- Standalone invocation can submit or clearly fail on one real contact page.
- Failures do not block email paths for other attorneys.
- Logs show firm, URL, and result without leaking secrets.

### Milestone 7 - AgentMail dispatch (45 min)

**Goal:** Email every attorney candidate with an email address.

**Build:**
- Implement `email_attorneys(args)` with AgentMail.
- Reuse a stable inbox client id such as `jailcall-dispatch`.
- Use the subject/body from this spec.
- Return sent status and recipient address.

**Acceptance:**
- Standalone invocation sends a test email to a controlled address.
- End-to-end dispatch emails all candidates with email addresses.
- Email body contains only name, charge category, callback, and a callback request — no incident details.

### Milestone 8 - Dispatch orchestrator (60 min)

**Goal:** Wire search, forms, and email into one post-intake workflow.

**Build:**
- After the three intake fields are complete, run `moss_find_lawyers` (sub-second).
- For each candidate, call `contact_attorneys` when a form URL exists and `email_attorneys` when an email exists.
- Accumulate contacted firm names from successful form or email results.
- Cap total dispatch time so the webhook does not exceed the 90-second AgentPhone timeout — the binding constraint is Browser Use form fills, not the Moss lookup.

**Acceptance:**
- One real call can complete intake and trigger all downstream channels.
- Browser Use work begins only after the confirmation line is spoken.
- Partial success is still useful: one contacted attorney is enough for the demo path.

### Milestone 9 - Error handling and edge cases (45-60 min)

**Goal:** Make the demo survivable under realistic caller and vendor failures.

**Build:**
- If `moss_find_lawyers` returns no hits (low-recall charge category), retry once with a relaxed query like `"criminal defense attorney Bay Area"` and `alpha` shifted toward keyword (e.g. 0.3).
- If email or form submission fails, continue to the next candidate.
- If the callback number is malformed, still proceed with email/form dispatch — attorneys can use the contact info embedded in the dispatch.

**Acceptance:**
- No single external-service failure crashes `/webhook`.
- The caller receives a short useful response in every failure case.
- Logs make it obvious which service failed.

### Milestone 10 - Deployment and demo runbook (45 min)

**Goal:** Remove laptop/tunnel fragility before judging.

**Build:**
- Prefer Railway, Fly, or Render for the final webhook URL.
- Set production env vars in the host dashboard.
- Register the deployed `/webhook` URL with AgentPhone.
- Keep local tunnel setup documented as fallback.
- Add a short README with run, setup, test call, and demo commands.

**Acceptance:**
- Deployed `/healthz` is reachable over HTTPS.
- AgentPhone webhook points to the deployed URL.
- Dialing the real number reaches the deployed app, not a local laptop.

### Milestone 11 - Full rehearsal and recording (60 min)

**Goal:** Produce the exact judging demo with proof across channels.

**Build:**
- Run the judge script: John Doe, DUI, real callback number.
- Capture server logs showing intake, Moss lookup results, and form/email status.
- Show the Browser Use session filling the form if available.
- Show the email that was sent to the attorney.
- Record one clean backup video in case live network conditions fail.

**Acceptance:**
- Full path completes in one take.
- Backup video shows real phone call, browser action, and email delivery.
- Any known flaky step has a documented fallback for the live demo.

### Critical path

1. Phone call answers with exact `beginMessage`.
2. `/webhook` verifies signatures and returns spoken NDJSON.
3. Intake controller collects the three fields without unsafe legal questioning.
4. `moss_find_lawyers` returns at least one Bay Area attorney candidate from the pre-built index.
5. Email or form submission reaches at least one attorney.
6. Deployed URL survives a real call during judging.

### Time budget

| Phase | Budget | Dependency |
|---|---:|---|
| Milestones 0-2 | 1.5-2 h | Required before real voice testing. |
| Milestones 3-4 | 1.5-2 h | Required before safe intake. |
| Milestones 5-8 | 2.5-3 h | Required for the multi-channel demo. |
| Milestones 9-10 | 1.5 h | Required before judging. |
| Milestone 11 | 1 h | Required for backup demo evidence. |
| Buffer | 1 h | Vendor auth, tunnel, or Browser Use variance. |

---

## Demo script (for judges)

1. "Imagine you've just been arrested. You're at the police station. You don't have a lawyer's number memorized. You don't have anyone to call."
2. Call the JailCall number live on speakerphone.
3. Go through the intake: "John Doe", "DUI", give a real callback number. (No location question — jurisdiction is hardcoded to the Bay Area.)
4. Show the browser agent searching and filling forms in real time on a projector/screen.
5. Show the email that was sent to the attorney.
6. "The law says you get a phone call. JailCall is the number that always answers."

---

## Judging alignment

Per the hackathon page: "things a human could pay for tomorrow over slide demos."

This works end-to-end with a real phone number, a real call, real Bay Area attorney websites being contacted, and real emails sent. No slides. No mocks. A person in jail could use this today.

**Tracks hit:**
- **The Fixer** — voice + email + browser in one workflow
- **Wildcard** — agent touches the real world in a new way (criminal justice)
- **Web Wranglers** — Browser Use filling real attorney intake forms
- **The Doer** — booking an appointment (with a lawyer) in the physical world

**Sponsor integrations used:**
- **AgentPhone** — inbound voice (number, webhook agent, NDJSON streaming).
- **Anthropic** — Claude Haiku 4.5 in the tool-call loop.
- **Moss** — sub-10 ms semantic lookup over the pre-indexed Bay Area attorney roster.
- **Browser Use** — autonomous form fill on real attorney intake pages.
- **AgentMail** — structured intake emails to attorneys with discoverable inboxes.
