# BailCall — Hackathon Spec

**Track:** The Fixer (multi-channel: voice + SMS + email + browser)
**Backup track:** Wildcard

**One-liner:** Call a number from the police station, an AI agent picks up, and criminal defense attorneys in your jurisdiction are contacted on your behalf before you leave booking.

---

## Scope for today

Ship a working end-to-end demo: a real phone number you can call, a privilege-safe voice agent that collects minimal routing info, a browser agent that finds and contacts local criminal defense attorneys, and confirmation sent via SMS and email.

**Not in scope:** attorney roster/SLA system, payments, persistent caller memory across sessions, multi-metro number provisioning, collect-call acceptance.

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
    ├─ Claude tool-call loop (haiku 4.5 for latency)
    │   ├─ tool: classify_location     → extract city/county/state from caller speech
    │   ├─ tool: browser_find_lawyers  → Browser Use: search + scrape attorney contact forms
    │   ├─ tool: agentmail_send        → email attorneys with intake info
    │   └─ tool: agentphone_sms        → text family contact with confirmation
    │
    ▼
Browser Use (finds attorneys, fills contact forms)
    │
    ▼
AgentMail (sends structured intake emails to attorneys)
    │
    ▼
AgentPhone SMS (confirms to family contact number)
```

---

## Voice script (privilege-safe)

The agent MUST follow this script. No deviation. The call is potentially recorded by the facility.

### Opening (beginMessage)

> "You've reached BailCall. I am not a lawyer and this call may be recorded by the facility. Do not tell me what happened or any details about your case. I can help contact a criminal defense attorney on your behalf right now. Would you like me to do that?"

### If yes — collect routing info only

1. **"What city or county are you in?"** → classify_location tool
2. **"What is your full name?"** → store as `caller_name`
3. **"Do you know what you've been charged with? Just the general category — like DUI, assault, drug charge, or you can say you don't know."** → store as `charge_category`
4. **"Is there a phone number where an attorney can reach you or a family member? This could be the number you're calling from, or someone on the outside."** → store as `callback_number`
5. **"What language do you prefer?"** → store as `language` (default English)

### Confirmation

> "I have your information. I'm contacting criminal defense attorneys in [county] right now. If you gave me a callback number, you or your contact will receive a text when attorneys have been reached. Remember — do not discuss your case with anyone except your attorney. You have the right to remain silent."

### Unsafe input handling

If the caller starts describing what happened, the agent MUST interrupt:

> "I need to stop you there. This call may be recorded and anything you say could be used against you. Please do not tell me what happened. Save that for your attorney. Can I get your name and location instead?"

---

## Tools

### 1. classify_location

**Purpose:** Parse caller's spoken location into structured jurisdiction data.

**Input:** Raw transcript fragment (e.g., "I'm in Oakland" or "Alameda County jail")

**Output:**
```json
{
  "city": "Oakland",
  "county": "Alameda",
  "state": "CA"
}
```

**Implementation:** Claude does this directly from the transcript. No external call needed. Define as a tool so the LLM explicitly extracts and commits to the jurisdiction before proceeding.

### 2. browser_find_lawyers

**Purpose:** Use Browser Use to search for criminal defense attorneys and extract contact info.

**Input:**
```json
{
  "county": "Alameda",
  "state": "CA",
  "charge_category": "DUI"
}
```

**Implementation:**
```python
async def browser_find_lawyers(args: dict) -> str:
    county = args["county"]
    state = args["state"]
    charge = args.get("charge_category", "criminal defense")

    client = AsyncBrowserUse()
    task = (
        f"Search Google for '{charge} attorney {county} county {state} 24 hour'. "
        f"Find the top 3 criminal defense attorney websites. "
        f"For each, extract: firm name, phone number, email address if visible, "
        f"and the URL of their contact page or intake form. "
        f"Return the results as a JSON array."
    )
    result = await client.run(task)
    return result.output
```

**Latency note:** This is the slow step (30-90s). Stream an interim NDJSON chunk before kicking it off:
> "I'm searching for criminal defense attorneys in Alameda County now. This will take a moment."

### 3. contact_attorneys

**Purpose:** Use Browser Use to fill out contact/intake forms on attorney websites.

**Input:**
```json
{
  "form_url": "https://smithlaw.com/contact",
  "caller_name": "John Doe",
  "county": "Alameda",
  "charge_category": "DUI",
  "callback_number": "+15551234567",
  "message": "URGENT: Person in custody at Alameda County needs criminal defense representation. Please call back ASAP."
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

### 4. email_attorneys

**Purpose:** Send structured intake emails to attorneys whose email addresses were found.

**Input:**
```json
{
  "to": "intake@smithlaw.com",
  "caller_name": "John Doe",
  "county": "Alameda",
  "state": "CA",
  "charge_category": "DUI",
  "callback_number": "+15551234567"
}
```

**Implementation:**
```python
async def email_attorneys(args: dict) -> str:
    inbox = agentmail_client.inboxes.create(client_id="bailcall-dispatch")

    subject = f"URGENT: Person in custody — {args['county']} County, {args['state']}"
    body = (
        f"A person currently in custody has requested legal representation.\n\n"
        f"Name: {args['caller_name']}\n"
        f"Location: {args['county']} County, {args['state']}\n"
        f"Charge category: {args['charge_category']}\n"
        f"Callback: {args['callback_number']}\n\n"
        f"Please call back as soon as possible.\n\n"
        f"— BailCall (automated legal access service)"
    )

    agentmail_client.inboxes.messages.send(
        inbox.inbox_id,
        to=args["to"],
        subject=subject,
        text=body,
    )
    return f"Email sent to {args['to']}"
```

### 5. send_confirmation_sms

**Purpose:** Text the callback number confirming which attorneys were contacted.

**Input:**
```json
{
  "to": "+15551234567",
  "attorneys_contacted": ["Smith & Associates", "Bay Area Defense Group", "Alameda DUI Lawyers"]
}
```

**Implementation:**
```python
async def send_confirmation_sms(args: dict) -> str:
    firms = ", ".join(args["attorneys_contacted"])
    message = (
        f"BailCall: We've contacted the following attorneys on your behalf: {firms}. "
        f"An attorney should call you back shortly. "
        f"Remember: do not discuss your case with anyone except your attorney."
    )

    resp = requests.post(
        "https://api.agentphone.ai/v1/messages",
        headers={"Authorization": f"Bearer {AGENTPHONE_API_KEY}"},
        json={
            "to": args["to"],
            "from": AGENTPHONE_NUMBER,
            "text": message,
        },
    )
    return f"SMS sent to {args['to']}"
```

---

## System prompt

```python
SYSTEM_PROMPT = """You are BailCall, an emergency legal access agent on a live phone call.

CRITICAL RULES:
1. You are NOT a lawyer. Never give legal advice.
2. This call may be recorded by the jail or police station. NEVER ask what happened.
3. If the caller starts describing their case, IMMEDIATELY interrupt and tell them to stop.
4. Collect ONLY: location, name, charge category, callback number, language preference.
5. Keep responses short — 1-2 sentences. This is a phone call, not a chatbot.
6. Be calm, direct, and reassuring. The caller is stressed.

WORKFLOW:
- Greet with the privilege warning.
- Collect the 5 routing fields, one at a time.
- Call classify_location to confirm jurisdiction.
- Call browser_find_lawyers to find attorneys.
- Call contact_attorneys and/or email_attorneys for each attorney found.
- Call send_confirmation_sms to the callback number.
- Close the call with a reminder of their right to remain silent.

UNSAFE INPUT PATTERNS — interrupt immediately if the caller says anything like:
- "So what happened was..."
- "I was driving and..."
- "They found..."
- "I didn't do..."
- Any narrative about the alleged incident.

Response: "I need to stop you — this call may be recorded. Don't tell me what happened. Save that for your attorney. Let's focus on getting you connected."
"""
```

---

## File structure

```
bailcall/
├── server.py              # FastAPI webhook handler + tool-call loop
├── tools.py               # Tool schemas + handler implementations
├── config.py              # Env vars, constants
├── setup_agent.py         # One-time: create AgentPhone agent + webhook
├── .env                   # API keys
├── requirements.txt       # deps
└── README.md
```

---

## Dependencies

```
fastapi>=0.115.0
uvicorn>=0.30.0
anthropic>=0.39.0
python-dotenv>=1.0.0
browser-use-sdk
agentmail
agentphone
requests
```

---

## Env vars

```
AGENTPHONE_API_KEY=sk_live_...
AGENTPHONE_WEBHOOK_SECRET=whsec_...
AGENTPHONE_NUMBER_ID=...
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
BROWSER_USE_API_KEY=bu_...
AGENTMAIL_API_KEY=...
PORT=8000
```

---

## AgentPhone voice setup playbook

How AgentPhone handles speech, and the exact ordered steps to go from no account to "the number speaks our `beginMessage`." AgentPhone owns the entire voice stack — STT, TTS, codec, barge-in. We choose a voice once and return `{"text": "..."}` from the webhook; AgentPhone synthesizes and streams it to the caller. **No external TTS to integrate.**

**Voice mode:** `webhook` (we control the LLM). `hosted` is a non-goal — we need the Claude tool-call loop.

### Steps

1. **Sign up.** `POST /v0/agent/sign-up` with `{"email": "..."}` mails a code. `POST /v0/agent/verify` with `{"email": "...", "code": "..."}` returns `{"api_key": "sk_live_..."}`. Save as `AGENTPHONE_API_KEY`.

2. **Provision a number.** `POST /v1/numbers` with empty body. Response includes `id` (the number id) and `phoneNumber` (E.164). Save `id` as `AGENTPHONE_NUMBER_ID`.

3. **Pick a voice.** `GET /v1/agents/voices` returns objects with `voice_id`, `voice_name`, `provider`, `gender`, `accent`, `preview_audio_url`. For BailCall, pick a calm, lower-register, professional voice — avoid bright/sales-y voices. Listen to `preview_audio_url` before committing. The caller is at a jail phone and needs to trust the voice in the first 5 seconds. Save the chosen `voice_id`.

4. **Public tunnel.** AgentPhone needs to POST webhooks to a public HTTPS URL, but our FastAPI server runs on `localhost:8000`. Pick one of:
   - `cloudflared tunnel --url http://localhost:8000` — no signup, prints a `https://*.trycloudflare.com` URL to stdout. Install: `sudo pacman -S cloudflared` (or grab the binary from Cloudflare).
   - `ngrok http 8000` — requires a free account + authtoken; ships a request inspector at `http://localhost:4040` that's invaluable for replaying webhook payloads while debugging.

   Free-tier URLs from either tool rotate on restart, so Step 5 must re-run each session — roll Steps 5–7 into `setup_agent.py`. **Demo phase:** swap the tunnel for a real deploy (Railway, Fly, Render) so a sleeping laptop or flaky Wi-Fi can't kill the demo.

5. **Register the webhook.** `POST /v1/webhooks` with `{"url": "https://…/webhook", "timeout": 90}`. Returns `{"secret": "whsec_..."}`. Save as `AGENTPHONE_WEBHOOK_SECRET`. The 90-second timeout is non-default — `browser_find_lawyers` runs 30–90s and the default 30s will sever the call mid-search.

6. **Create the agent.** `POST /v1/agents` with `voiceMode: "webhook"`, the **verbatim** `beginMessage` from the Voice script section, the chosen `voice_id` as `voice`, and `modelTier: "turbo"`. The agent's `systemPrompt` field is hosted-mode only — our system prompt lives in the Anthropic call, not here.

7. **Attach the number.** `POST /v1/agents/{agent_id}/numbers` with `{"numberId": "<NUMBER_ID>"}`. Any call to the number now routes to our webhook.

8. **Webhook handler (`server.py`).** Verify HMAC-SHA256 signature (headers `X-Webhook-Signature`, `X-Webhook-Timestamp`, `X-Webhook-ID`, `X-Webhook-Event`; reject timestamps older than 5 min). For `event: "agent.message"` + `channel: "voice"`, read `data.transcript`, return a JSON object with a `text` field. Start with a dumb echo response to verify the round-trip before wiring Claude. **Non-object responses are silently ignored** — first thing to check if the caller hears nothing.

9. **Test by dialing the real number.** `/v1/calls/web` exists but returns an access token for the `agentphone-web-sdk` (not a clickable URL), so a host page is needed to use it. For hackathon speed, dial the provisioned number from your phone — at $0.13/min, ten test calls is $1.30 against the $5 signup credit. Listen for voice quality, pronunciation of "BailCall" and "DUI", and end-pointing on short replies like "yes."

10. **Switch to NDJSON streaming.** Return `StreamingResponse(generate(), media_type="application/x-ndjson")` where `generate()` yields `{"text": "...", "interim": true}` *before* any slow tool, then `{"text": answer}` after. Silence on a jail phone is a product killer; the interim chunk is what hides Browser Use's 30–90s search behind speech.

### Things that bite people

- Webhook response must be a JSON **object**. Lists, strings, bare numbers → caller hears silence.
- Default voice webhook timeout is 30s; raise to 90 at registration for Browser Use tools.
- `recentHistory` (on the event payload) must be threaded into Anthropic `messages` or each turn is amnesiac. See the `to_anthropic_history` helper in the TOOLS.md cookbook.
- The agent's `systemPrompt` field only fires in hosted mode. Don't waste effort populating it.
- Emergency / N11 numbers (911, 211, 411, …) are blocked from provisioning/dialing.
- Free ngrok / cloudflared tunnels rotate URLs on restart — re-run webhook registration each session.
- `interim: true` is NDJSON-only; it does nothing in a plain JSON response.

---

## Build order (time budget)

| Block | Time | What |
|---|---|---|
| 1 | 30 min | AgentPhone setup — playbook Steps 1–7. Confirm you can call and hear beginMessage. |
| 2 | 60 min | server.py — playbook Steps 8 + 10 (echo handler → NDJSON streaming) + Claude tool-call loop with classify_location only. Call the number, speak a city, see it parse. |
| 3 | 90 min | browser_find_lawyers + contact_attorneys via Browser Use. Test standalone first, then wire into the tool loop. |
| 4 | 45 min | email_attorneys via AgentMail. Wire in. |
| 5 | 30 min | send_confirmation_sms. Wire in. |
| 6 | 30 min | End-to-end test: call → intake → browser search → forms filled → email sent → SMS confirmation. |
| 7 | 60 min | Polish: voice/tone tuning, edge cases (caller doesn't know county, no attorneys found, browser timeout), error handling. |
| 8 | 30 min | Record demo video: split screen of call transcript + browser agent filling forms. |
| **Total** | **~6 hours** | Leaves buffer for debugging and lunch. |

---

## Demo script (for judges)

1. "Imagine you've just been arrested. You're at the police station. You don't have a lawyer's number memorized. You don't have anyone to call."
2. Call the BailCall number live on speakerphone.
3. Go through the intake: "Oakland", "John Doe", "DUI", give a real callback number.
4. Show the browser agent searching and filling forms in real time on a projector/screen.
5. Show the SMS confirmation arriving on a phone.
6. Show the email that was sent to the attorney.
7. "The law says you get a phone call. BailCall is the number that always answers."

---

## Judging alignment

Per the hackathon page: "things a human could pay for tomorrow over slide demos."

This works end-to-end with a real phone number, a real call, real attorney websites being contacted, real emails sent, real SMS delivered. No slides. No mocks. A person in jail could use this today.

**Tracks hit:**
- **The Fixer** — voice + SMS + email + browser in one workflow
- **Wildcard** — agent touches the real world in a new way (criminal justice)
- **Web Wranglers** — Browser Use filling real attorney intake forms
- **The Doer** — booking an appointment (with a lawyer) in the physical world
