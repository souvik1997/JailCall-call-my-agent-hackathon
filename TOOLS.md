# Tools Index — YC Call My Agent Hackathon

Quick reference for everything you'll touch during the build.
Compiled 2026-05-17. Verify endpoint shapes against vendor docs before relying on details below.

**Stack at a glance (sponsors):**
- **AgentPhone** (P26, host) — phone numbers + voice/SMS ingress and egress for agents.
- **Moss** (F25) — sub-10 ms semantic search; retrieval layer that fits inside a voice turn.
- **Supermemory** — persistent memory graph per user/org; survives across calls.
- **Browser Use** (W25) — browser automation for agents; the "agent that uses the web" layer.
- **AgentMail** (S25) — email inbox API; create inboxes, send/receive, threads, webhooks.
- **Stripe** (S09) — agentic payments toolkit; give an agent the ability to charge / get paid.
- **Sponge** (W26) — wallets + payment gateway for autonomous agent-to-agent transactions.
- **Google DeepMind** — sponsor; bring your own Gemini integration if you go that way.
- **Anthropic Claude** — the brain. Default model `claude-haiku-4-5-20251001` for voice latency.

**Reference example:** [Moss × AgentPhone cookbook](https://github.com/usemoss/moss/tree/main/examples/cookbook/agentphone).

---

## Table of contents

1. [Hackathon context](#hackathon-context)
2. [Decision matrix](#decision-matrix)
3. [AgentPhone](#agentphone) — phone / voice / SMS
4. [Moss](#moss) — semantic search
5. [Supermemory](#supermemory) — long-term memory
6. [Browser Use](#browser-use) — web automation
7. [AgentMail](#agentmail) — email inbox API
8. [Stripe Agent Toolkit](#stripe-agent-toolkit) — agentic payments
9. [Sponge](#sponge) — autonomous wallets + agent payment gateway
10. [Cookbook: Moss × AgentPhone (full source)](#cookbook-moss--agentphone)
11. [Combination patterns](#combination-patterns)
12. [Anthropic model selection](#anthropic-model-selection)
13. [Env-var cheat sheet](#env-var-cheat-sheet)
14. [Useful links](#useful-links-one-stop)

---

## Hackathon context

- **Event:** Call My Agent Hackathon — hosted by **AgentPhone (P26)** at **Y Combinator, San Francisco**
- **Date:** **May 17, 2026** (today)
- **URL:** https://events.ycombinator.com/CallMyAgentHackathon
- **Registration:** closed

### Prizes
- **First place:** guaranteed YC interview
- Cash prizes
- Sponsor credits
- Phones for winners
- Bonus prizes across multiple tracks
- Swag for every attendee

### Schedule (all PT, May 17 2026)

| Time | Event |
|---|---|
| 8:00 AM | Doors open + breakfast |
| 9:00 AM | Kickoff + sponsor intros |
| 9:30 AM | **Hacking begins** |
| 12:30 PM | Lunch |
| 6:30 PM | Dinner |
| **8:00 PM** | **Submissions due + judging begins** |
| 9:30 PM | Closing ceremony |
| 10:00 PM | Event ends |

> **Time-on-clock:** ~10.5 hours of hacking from 9:30 AM to 8:00 PM. Ship something that
> works end-to-end early; polish in the last 90 minutes.

### Build tracks

| Track | What it means |
|---|---|
| **Cold callers that close** | Voice agents that pitch, qualify, book meetings — AgentPhone outbound + Moss for product KB |
| **Inbox warriors** | Email triage / reply / follow-up — AgentMail + Supermemory |
| **The negotiator** | Agents that haggle / book / buy on your behalf — Browser Use + Stripe/Sponge |
| **Group chat chaos** | SMS + iMessage agents that text humans and other agents — AgentPhone messaging |
| **Web wranglers** | Navigate sites, fill forms, get past logins — Browser Use core |
| **The fixer** | Multi-channel: voice + SMS + email + browsing in one workflow |
| **The doer** | Book appointments, place orders, schedule deliveries — anything in the physical world |
| **Wildcard** | Anything where an agent touches the real world in a new way |

### Theme (from the page)

> "For years, AI agents were stuck in chat windows. That's changed. Voice models are cheap and
> fast, infrastructure for SMS, iMessage, email, and browsing has matured … pick up the phone
> and call your agent."

Judging implicitly favors things a human could pay for tomorrow over slide demos.

---

## Decision matrix

| You need… | Reach for |
|---|---|
| A real phone number an agent can answer | AgentPhone — `POST /v1/numbers` + attach to agent |
| Place an outbound call from code | AgentPhone — `POST /v1/calls` (autonomous) |
| Stream voice → text → your LLM → speech | AgentPhone webhook mode (NDJSON response) |
| Built-in LLM handles the call end-to-end | AgentPhone hosted mode (`systemPrompt` on agent) |
| Send / receive SMS or iMessage | AgentPhone — `POST /v1/messages` + `agent.message` webhooks |
| Browser-based test call (no phone) | AgentPhone — `POST /v1/calls/web` returns a Web SDK access token (not a clickable URL); consume via `agentphone-web-sdk` in a host page. For hackathon speed, just dial the provisioned number ($0.13/min). |
| Stream live transcript to a dashboard | AgentPhone — `GET /v1/calls/{id}/transcript/stream` (SSE) |
| Look up KB content during a voice turn | Moss `client.query(...)` — sub-10 ms |
| Index PDFs, docs, raw text for semantic search | Moss `createIndex` / `addDocs` |
| Remember "who is this caller" across calls | Supermemory User Profiles + container tags |
| Long-term episodic memory of past conversations | Supermemory `client.add(...)` + `client.profile(...)` |
| Agent that browses websites, fills forms, logs in | Browser Use — `client.run("…")` |
| Stealth/CAPTCHA-resistant scraping | Browser Use Stealth Browsers + residential proxies |
| Give an agent its own email inbox | AgentMail — `client.inboxes.create(...)` |
| Agent sends/replies to email | AgentMail `inboxes.messages.send(...)` + webhooks |
| Agent accepts payments from users | Stripe Agent Toolkit — Payment Links tool |
| Agent pays for things (other agents, services) | Sponge Wallet |
| Sell your service to other agents | Sponge Gateway |

---

## AgentPhone

Provisions real US/Canada phone numbers, transcribes calls in real time, and routes voice/SMS
events through a single unified webhook format. The phone-ingress sponsor.

- **Marketing:** https://agentphone.ai/
- **API docs:** https://docs.agentphone.ai
- **LLM-friendly skills doc:** https://agentphone.ai/skills.md
- **OpenAPI:** linked from docs (JSON/YAML)
- **API base URL:** `https://api.agentphone.ai/v1`
- **Native integrations:** Claude Code, Cursor, Windsurf, Zed, OpenClaw (MCP server),
  LangChain, Google ADK, Hermes Agent Framework
- **Adopters:** Google, LangChain, Y Combinator-backed companies (per marketing site)

### Pricing (pay-as-you-go)

| Item | Cost |
|---|---|
| Phone number | $3.00 / month |
| Voice (webhook mode) | $0.13 / min |
| Voice (hosted mode) | $0.22 / min |
| SMS / iMessage | $0.02 / message |
| Free credit on signup | $5.00 |

Default 10-number account limit. Emergency numbers (911, N11) blocked.

### Onboarding (no dashboard needed)

```bash
# 1. Sign up
curl -X POST https://api.agentphone.ai/v0/agent/sign-up \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'

# 2. Receive 6-digit code via email

# 3. Verify — provisions account, first number, and API key.
#    Response field is snake_case `api_key` (NOT `apiKey`), value starts with `sk_live_`.
curl -X POST https://api.agentphone.ai/v0/agent/verify \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "code": "123456"}'

# 4. From here, every request uses Bearer auth
#    Authorization: Bearer $AGENTPHONE_API_KEY
```

> **Security:** Never send your API key to any domain other than `api.agentphone.ai`.

### Voice modes

- **Webhook mode (default, $0.13/min):** AgentPhone transcribes the caller, POSTs an
  `agent.message` event to your server, you respond with text (or NDJSON stream) — that's what
  the caller hears.
- **Hosted mode ($0.22/min):** AgentPhone runs its own LLM with your `systemPrompt`. No
  server needed.

### Webhook security

Every webhook delivery includes four headers:

| Header | Meaning |
|---|---|
| `X-Webhook-Signature` | `sha256=<hex>` HMAC-SHA256 |
| `X-Webhook-Timestamp` | Unix timestamp — reject if > 5 min old |
| `X-Webhook-ID` | Unique delivery id (use for idempotency) |
| `X-Webhook-Event` | Event type (cheap dispatch key) |

Signing recipe: `signed_string = f"{timestamp}.".encode() + raw_body`, then
`HMAC-SHA256(secret, signed_string)`. Compare against the header with constant time.

```python
import hashlib, hmac

def verify_webhook_signature(*, secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    signed = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)
```

**Retry policy:** 6 attempts — immediate, +5 min, +30 min, +2 h, +6 h, +12 h.
**Idempotency:** dedupe on `X-Webhook-ID`.

### Event types

| Event | Channels | Purpose |
|---|---|---|
| `agent.message` | sms, mms, imessage, voice | Inbound message or transcribed voice turn |
| `agent.call_ended` | voice | Final transcript + call analysis |
| `agent.reaction` | imessage | Tapback reactions |

Each event payload contains a `data` field (channel-specific), an `event` discriminator,
a `channel` string, and `recentHistory` (recent turns of the same conversation — feed this
into your LLM context so the agent has memory within the call).

### Voice webhook response shape

The webhook response is what the caller hears. JSON object only — non-object responses are
ignored. Stream incremental chunks with NDJSON (`Content-Type: application/x-ndjson`).

| Field | Type | Meaning |
|---|---|---|
| `text` | string | What to speak |
| `hangup` | boolean | End the call after speaking |
| `action` | string | `"transfer"` or `"hangup"` |
| `digits` | string | DTMF input |
| `interim` | boolean | NDJSON only — mark this chunk as not-the-final-answer |

> **Critical best practice:** stream an interim chunk *immediately* before any slow work, so
> the caller doesn't hear silence while your LLM thinks:
> ```json
> {"text": "Let me check that for you.", "interim": true}
> ```

### Endpoints — Agents

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/agents/voices` | List 300+ voices across 8 providers. Each entry: `voice_id`, `voice_name`, `provider`, `gender`, `accent`, `preview_audio_url` (last three nullable). Pass `voice_id` as `voice` when creating an agent. |
| GET | `/v1/agents` | List agents |
| POST | `/v1/agents` | Create agent (name, voiceMode, systemPrompt, beginMessage, voice, modelTier) |
| GET | `/v1/agents/{agent_id}` | Get one |
| PATCH | `/v1/agents/{agent_id}` | Update fields (e.g. `beginMessage`) |
| DELETE | `/v1/agents/{agent_id}` | Delete |
| POST | `/v1/agents/{agent_id}/numbers` | Attach a number |
| DELETE | `/v1/agents/{agent_id}/numbers/{number_id}` | Detach a number |
| GET | `/v1/agents/{agent_id}/conversations` | List conversations |
| GET | `/v1/agents/{agent_id}/calls` | List calls |

**Agent configuration knobs:**
- `name` — display name
- `voiceMode` — `"webhook"` or `"hosted"`
- `systemPrompt` — used in hosted mode
- `beginMessage` — what the agent says first when picking up
- `voice` — voice id from `/v1/agents/voices`
- `modelTier` — `turbo` / `balanced` / `max`
- Optional: `transferNumber`, `voicemailMessage`

### Endpoints — Numbers / Calls / Messages / Webhooks

```text
POST   /v1/numbers                            # Provision a number
GET    /v1/numbers                            # List
DELETE /v1/numbers/{number_id}                # Release

POST   /v1/calls                              # Autonomous outbound call
POST   /v1/calls/web                          # Browser test call (no phone needed)
GET    /v1/calls                              # List with filters
GET    /v1/calls/{call_id}                    # Full call details + transcript
GET    /v1/calls/{call_id}/transcript/stream  # SSE live transcript

POST   /v1/messages                           # Send SMS / MMS / iMessage
GET    /v1/conversations                      # List threads

POST   /v1/webhooks                           # Register webhook
                                              # body: { url, contextLimit?, timeout? }
                                              # response: { secret }
                                              # contextLimit: 0–50 (default 10)
                                              # timeout:      5–120 s (default 30 voice)
```

### Minimal end-to-end setup

```bash
# 1. Register webhook → copy `secret` into AGENTPHONE_WEBHOOK_SECRET
curl -X POST https://api.agentphone.ai/v1/webhooks \
  -H "Authorization: Bearer $AGENTPHONE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-server.example.com/webhook"}'

# 2. Create an agent in webhook mode
curl -X POST https://api.agentphone.ai/v1/agents \
  -H "Authorization: Bearer $AGENTPHONE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Support Bot",
    "voiceMode": "webhook",
    "beginMessage": "Hi, thanks for calling. How can I help?"
  }'

# 3. Attach a number to the agent
curl -X POST https://api.agentphone.ai/v1/agents/<AGENT_ID>/numbers \
  -H "Authorization: Bearer $AGENTPHONE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"numberId": "<NUMBER_ID>"}'
```

### SDKs

```bash
pip install agentphone        # Python
npm install agentphone        # Node.js
```

MCP server also available — works with Claude Code, Cursor, Windsurf, Zed, OpenClaw.

### SMS / iMessage caveats

- Replies to text channels use `POST /v1/messages`, **not** the webhook response body.
- iMessage supports reactions and media carousels.
- US SMS requires 10DLC registration — voice path is the easier hackathon target.

---

## Moss

High-performance runtime for real-time semantic search. Sub-10 ms lookups, instant index
updates. Designed to fit inside a voice turn's latency budget.

- **Site:** https://moss.dev
- **Docs:** https://docs.moss.dev/docs
- **LLM-friendly index:** https://docs.moss.dev/llms.txt
- **Full reference:** https://docs.moss.dev/llms-full.txt
- **Control plane API:** `https://service.usemoss.dev/v1/manage`

### Concepts

- **Index** — a named, searchable collection of documents.
- **Document** — text + metadata + id.
- **Job** — async build/index work; poll `getJobStatus`.
- **Retrieval modes** — vector, keyword, hybrid (blend with `alpha`).
- **Loaded index** — once `loadIndex` is called, queries run locally for sub-10 ms latency.

### Install & auth

```bash
pip install moss              # Python
npm install @moss-dev/moss    # JS / TS
```

```bash
export MOSS_PROJECT_ID=...
export MOSS_PROJECT_KEY=...
```

### Python SDK essentials

```python
import asyncio, os
from moss import MossClient, DocumentInfo, QueryOptions

client = MossClient(os.getenv("MOSS_PROJECT_ID"), os.getenv("MOSS_PROJECT_KEY"))

docs = [
    DocumentInfo(id="refunds", text="Refunds are processed within 3-5 business days."),
    DocumentInfo(id="shipping", text="Free shipping on orders over $50 in the contiguous US."),
]

async def main():
    await client.create_index("faqs", docs, "moss-minilm")
    await client.load_index("faqs")
    result = await client.query(
        "faqs",
        "When will I get my money back?",
        QueryOptions(top_k=3, alpha=0.6),
    )
    for doc in result.docs:
        print(doc.text)
    print("took", result.time_taken_ms, "ms")

asyncio.run(main())
```

### JS/TS SDK essentials

```ts
import { MossClient, DocumentInfo } from '@moss-dev/moss'

const client = new MossClient(
  process.env.MOSS_PROJECT_ID!,
  process.env.MOSS_PROJECT_KEY!,
)

const docs: DocumentInfo[] = [
  { id: 'doc1', text: 'How do I track my order?…', metadata: { category: 'shipping' } },
  { id: 'doc2', text: 'What is your return policy?…', metadata: { category: 'returns' } },
]

await client.createIndex('faqs', docs, { modelId: 'moss-minilm' })
await client.loadIndex('faqs')
const results = await client.query('faqs', 'How do I return a damaged product?', { topK: 3 })
```

### `QueryOptions`

| Field | Meaning |
|---|---|
| `top_k` / `topK` | Number of results |
| `alpha` | 0.0 = pure keyword, 1.0 = pure semantic (hybrid in between) |
| filter / metadata | Restrict by metadata fields |

### Control-plane API (when not using the SDK)

`POST https://service.usemoss.dev/v1/manage` — single endpoint, action dispatched by JSON body.

Headers: `x-project-key: <key>`, `x-service-version: v1`. Body always includes `projectId` and
an `action`.

| Action | Purpose |
|---|---|
| `addDocs` | Append/upsert docs (`indexName`, `docs`, `upsert=true`) → returns `jobId` |
| `deleteDocs` | Remove by id (`indexName`, `docIds[]`) → returns `jobId` |
| `getDocs` | Retrieve stored docs (`docIds?`) — no embeddings |
| `initUpload` | Presigned URL for precomputed index data |
| `startBuild` | Trigger build using `jobId` from `initUpload` |
| `getJobStatus` | Poll: `building` / `completed` / `failed`, % progress, phase |
| `listIndexes` | Enumerate indexes in project |
| `getIndex` | Metadata for one index |
| `deleteIndex` | Drop it |
| `getIndexUrl` | Download link for built index |

### Integrations / framework adapters

LangChain, DSPy, Pipecat, LiveKit, ElevenLabs, Agora, VAPI, Vercel AI SDK, Next.js, VitePress.
MCP server exposes Moss as tools for Claude Code, Cursor, etc. CLI available for terminal use.

---

## Supermemory

Memory API for the AI era — long-term + short-term memory with a semantic graph that evolves
as new info arrives. Unlike stateless RAG, it tracks temporal context and current state.

- **Site:** https://supermemory.ai
- **Docs:** https://supermemory.ai/docs/intro
- **LLM-friendly index:** https://supermemory.ai/docs/llms.txt
- **Full reference:** https://supermemory.ai/docs/llms-full.txt

### Concepts

- **Document** — raw input you ingest (file, text, URL).
- **Memory** — extracted, normalized knowledge unit derived from documents.
- **Container Tag** — isolation key. One per user, project, or org — keeps memory spaces
  separated and scopes API keys.
- **Memory Graph relations:**
  - `Updates` — new info contradicts existing knowledge (state changes).
  - `Extends` — adds detail without replacing.
  - `Derives` — system infers new facts from patterns.
- **User Profile** — auto-built per container tag. Static facts (long-term) + dynamic facts
  (recent context). Read this for instant personalization without a search.

> **Why not just RAG?** RAG = stateless similarity search. Supermemory tracks state over time.
> If a user says "I love Adidas" → later "the shoes broke" → later "switched to Puma", RAG
> would still recommend Adidas (highest similarity). Supermemory tracks the progression.

### Install & auth

```bash
pip install supermemory          # Python
npm install supermemory          # TypeScript
```

```bash
export SUPERMEMORY_API_KEY="sm_..."
```

Bearer auth on every request: `Authorization: Bearer $SUPERMEMORY_API_KEY`. Scoped keys can be
restricted to specific container tags.

### Three context-delivery surfaces

1. **Memory API** — extracted, evolving facts learned from conversations.
2. **User Profiles** — curated static + dynamic per-entity info.
3. **RAG** — semantic document search with metadata filtering + contextual chunking.

### Canonical agent workflow

```python
from supermemory import Supermemory

client = Supermemory()

# 1. Pull context for this caller before responding
profile = client.profile(
    user_id="caller_+15551234567",
    query="what is this person asking about right now?",
)

# 2. Assemble messages
messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "system", "content": f"Caller facts: {profile.static}"},
    {"role": "system", "content": f"Recent context: {profile.dynamic}"},
    {"role": "system", "content": f"Relevant memories: {profile.memories}"},
    {"role": "user", "content": transcript},
]

# 3. Run your LLM, then store the interaction
client.add(
    content=f"User said: {transcript}\nAssistant said: {reply}",
    container_tag="caller_+15551234567",
)
```

### Endpoint groups

| Group | Examples |
|---|---|
| **Ingestion** | Add single/batch docs, file upload (PDF/audio/image), conversation ingest, update doc, delete doc |
| **Documents** | Get, list, chunk, presigned URL access, processing-status poll, search, bulk add/update/delete |
| **Memories** | Create, update (versioned), forget (soft delete), browse history |
| **Search** | Semantic memory search tuned for low-latency conversational use |
| **Container Tags** | Delete, configure, merge tags (consolidate memory spaces) |
| **Connections** | Manage external-provider syncs and resources |
| **User Profiles** | Retrieve profile, manage org settings, reset data |

### Supported content types

Text, conversations (chat logs), PDFs, Word, PowerPoint, images (OCR), audio (transcription),
video, code (AST-aware chunking — keeps functions/classes intact), CSV, JSON.

### Processing pipeline

1. Validation + storage
2. Content extraction (OCR / transcription as needed)
3. Chunking with content-type-specific strategies
4. Vector embedding generation
5. Relationship indexing into the memory graph

### Connectors (auto-sync)

Google Drive, Gmail, Notion, OneDrive, GitHub (Scale/Enterprise — supports webhooks), S3,
Web Crawler.

### Framework integrations

LangChain, CrewAI, Vercel AI SDK, OpenAI Agents, Microsoft Agent Framework, MCP. Plus
specialty tooling: SMFS (filesystem abstraction), MemoryBench (eval).

### Search options

- Hybrid (semantic + keyword) mode for technical queries.
- Optional `threshold` to filter by relevance score.
- Metadata filtering on top of vector search.

---

## Browser Use

Browser automation infrastructure for AI agents. Open-source self-healing harness, stealth
browsers with anti-detection + CAPTCHA solving, residential proxies in 195+ countries, plus
hosted "Browser Use Box" remote sessions. The "agent that uses the web" sponsor.

- **Site:** https://browser-use.com/
- **Docs:** https://docs.browser-use.com
- **Cloud platform:** https://cloud.browser-use.com
- **Open source:** https://github.com/browser-use/browser-use
- **API base:** `https://api.browser-use.com`
- **LLM-friendly docs index:** https://docs.browser-use.com/llms.txt

### Products

| Product | What it is |
|---|---|
| **Browser Harness** | Open-source self-healing automation framework |
| **Stealth Browsers** | Anti-detection + CAPTCHA solving |
| **Browser Use Box** | Remote box running Claude Code + Harness; control via Telegram, web UI, or SSH |
| **Web Agents** | Natural-language extraction, automation, testing, monitoring |
| **Custom Models** | LLMs fine-tuned for browser automation |
| **Proxies** | Residential IPs across 195+ countries |

### Install & auth

```bash
pip install browser-use-sdk         # Python
npm install browser-use-sdk         # TypeScript
```

Get an API key at <https://cloud.browser-use.com/settings>, then:

```bash
export BROWSER_USE_API_KEY=bu_...
```

Header form (raw HTTP): `X-Browser-Use-API-Key: bu_...`

### Quickstart — Python

```python
import asyncio
from browser_use_sdk.v3 import AsyncBrowserUse

async def main():
    client = AsyncBrowserUse()
    result = await client.run("List the top 20 posts on Hacker News today with their points")
    print(result.output)

asyncio.run(main())
```

### Quickstart — TypeScript

```typescript
import { BrowserUse } from "browser-use-sdk/v3";

const client = new BrowserUse();
const result = await client.run("List the top 20 posts on Hacker News today with their points");
console.log(result.output);
```

### Agent vs Browser modes

- **Agent** — `client.run(task)` or `client.sessions.create(...)`. AI-driven task completion.
  Accepts `task` and `model` params. Use this for the negotiator / web-wrangler tracks.
- **Browser** — `client.browsers.create(...)`. Raw browser access via Chrome DevTools
  Protocol. Customize screen size, timeout. Use this when you want fine control or to drive
  with your own LLM loop.

### Good fit for tracks

- **The negotiator** — Browser Use signs into a site, fills a form, completes the booking.
- **Web wranglers** — anything that needs to get past logins or click through flows.
- **The fixer** (multi-channel) — Browser Use as one tool in a Claude tool-call loop alongside
  AgentPhone or AgentMail.

---

## AgentMail

Email inbox API designed for AI agents. Create programmatic inboxes, send/receive email,
manage threads, and receive real-time events via webhooks or WebSockets.

- **Site:** https://agentmail.to/
- **Docs:** https://docs.agentmail.to
- **Console:** https://console.agentmail.to
- **API base:** `https://api.agentmail.to`
- **Pricing:** https://agentmail.to/pricing

> **Markdown trick:** append `.md` to any docs page URL to get the clean Markdown version.

### Capabilities

- Programmatic inbox creation with verifiable addresses
- Send + receive (REST + IMAP/SMTP)
- Full threading
- Attachments, multi-party conversations
- Webhooks **and** WebSockets for real-time events
- Custom domains with DKIM/SPF/DMARC

### Install & auth

```bash
# Python
pip install agentmail python-dotenv

# TypeScript
npm install agentmail dotenv

# CLI
npm install -g agentmail-cli
```

Auth header: `Authorization: Bearer $AGENTMAIL_API_KEY`

### Quickstart — Python

```python
from agentmail import AgentMail
import os
from dotenv import load_dotenv

load_dotenv()
client = AgentMail(api_key=os.getenv("AGENTMAIL_API_KEY"))

inbox = client.inboxes.create(client_id="my-agent-inbox-v1")

client.inboxes.messages.send(
    inbox.inbox_id,
    to="recipient@example.com",
    subject="Hello from AgentMail",
    text="Plain text body",
)

# Receive
messages = client.inboxes.messages.list(inbox.inbox_id)
for m in messages:
    print(m.extracted_text)
```

### Quickstart — TypeScript

```typescript
import { AgentMailClient } from "agentmail";
import "dotenv/config";

const client = new AgentMailClient({
  apiKey: process.env.AGENTMAIL_API_KEY!,
});

const inbox = await client.inboxes.create({ clientId: "my-agent-inbox-v1" });

await client.inboxes.messages.send(inbox.inboxId, {
  to: "recipient@example.com",
  subject: "Hello from AgentMail",
  text: "Plain text body",
});
```

### Endpoint groups (REST)

- **Inboxes** — create, list, get, update, delete
- **Messages** — send, reply, forward, list, get
- **Threads** — list, get, update, delete
- **Drafts** — create, manage, send
- **Webhooks** — create, list, update, delete (event types per inbox)
- **Domains** — create, verify, manage

MCP server is also available for direct tool integration with Claude Code / Cursor.

### Good fit for tracks

- **Inbox warriors** — triage + reply + follow-up loop on a per-user inbox.
- **The fixer** — email as one channel in a multi-channel agent.
- **Cold callers that close** — book meeting confirmations via email after a successful call.

---

## Stripe Agent Toolkit

Lets your agent earn and spend money. Wraps Stripe APIs as LLM tools so the model can create
Payment Links, accept funds, and otherwise touch financial primitives. Use restricted keys
(`rk_*`) to limit blast radius.

- **Docs:** https://docs.stripe.com/agents
- **Restricted-key reference:** https://docs.stripe.com/keys#limit-access

### Supported frameworks

- OpenAI Agents SDK (Python)
- Vercel AI SDK (Node.js)
- LangChain (Python)
- CrewAI (Python)
- Anything with function-calling (use the toolkit directly)

### Capabilities (out of the box)

- Create Payment Links dynamically (the easiest "accept funds" path)
- Plug into support workflows (refunds, customer lookups)
- Build test-data scaffolding

### Install (LangChain example)

```bash
pip install stripe-agent-toolkit langchain
```

```python
import os
from stripe_agent_toolkit.langchain.toolkit import StripeAgentToolkit

toolkit = StripeAgentToolkit(
    secret_key=os.getenv("STRIPE_SECRET_KEY"),  # use rk_* in production
    configuration={
        "actions": {
            "payment_links": {"create": True},
            "products": {"create": True},
            "prices": {"create": True},
        }
    },
)

tools = toolkit.get_tools()
# pass `tools` into your agent
```

### Install (Vercel AI SDK / Node)

```bash
npm install @stripe/agent-toolkit ai
```

```typescript
import { StripeAgentToolkit } from "@stripe/agent-toolkit/ai-sdk";

const toolkit = new StripeAgentToolkit({
  secretKey: process.env.STRIPE_SECRET_KEY!,
  configuration: {
    actions: {
      paymentLinks: { create: true },
      products: { create: true },
      prices: { create: true },
    },
  },
});

// toolkit.getTools() → pass into your model call
```

### Security

> "Use restricted API keys (`rk_*`) to limit your agent's access to only the functionality it
> requires."

Generate from Stripe Dashboard → Developers → API keys → Restricted keys. Whitelist only the
actions in your toolkit `configuration`.

### Good fit for tracks

- **The doer** — agent books a service, generates a Payment Link, sends it via SMS/email.
- **Cold callers that close** — qualify lead, collect payment intent during the call.
- **The negotiator** — agent agrees a price with a counterparty, materializes a Payment Link.

---

## Sponge

Financial infrastructure for the agent economy. Two products: **Sponge Wallet** lets an agent
hold funds and transact autonomously (bank, card, crypto) with other agents and businesses
without human authorization per transaction. **Sponge Gateway** lets businesses accept
payments from agents — onboard and charge agents through an API integration.

- **Marketing:** https://paysponge.com/
- **YC profile:** https://www.ycombinator.com/companies/sponge
- **Note:** docs URL not publicly resolvable via static fetch as of 2026-05-17. Ask the
  Sponge booth on-site for API access details and a sandbox key.

### Products

| Product | For | What it does |
|---|---|---|
| **Sponge Wallet** | Agent-side | Hold funds, transact autonomously over bank / card / crypto rails |
| **Sponge Gateway** | Business-side | Onboard + accept payments from agents via API, no human in the loop |

### Pairing with the rest of the stack

- **Wallet + Browser Use** — the negotiator agent goes to a site, agrees a price, and the
  wallet pays directly.
- **Wallet + Stripe** — agent earns via Stripe Payment Links, holds the funds in Sponge,
  spends them through Sponge on other agents' services.
- **Gateway + your hackathon product** — if you're building a service for other agents to
  consume, expose Sponge Gateway as the payment surface.

### Good fit for tracks

- **The negotiator** — autonomous purchases.
- **The doer** — real-world transactions (ordering, deliveries, services).
- **Wildcard** — agent-to-agent commerce demos.

---

## Cookbook: Moss × AgentPhone

End-to-end working example combining everything except Supermemory. Use it as the skeleton.

- **Source:** https://github.com/usemoss/moss/tree/main/examples/cookbook/agentphone

### Architecture

```
    +----------+        1. speech         +------------+
    |  caller  |  <-------------------->  | AgentPhone |
    +----------+        8. reply          +------------+
                                                |
                                         2. POST /webhook
                                         (signed, transcript)
                                                v
                                         +-----------------+
                                         |    server.py    |
                                         |  FastAPI route  |
                                         +-----------------+
                                           |             ^
                                 3. messages.create      |
                                 + tools=[moss_search]   |
                                           v             |
                                         +--------+      |
                                         | Claude |      | 7. NDJSON
                                         +--------+      |    {text}
                                           |    ^        |
                                 4. tool_use    |        |
                                 moss_search    | 6. final text
                                           v    |        |
                                         +-----------------+
                                         |   moss_search   |
                                         |    handler      |
                                         +-----------------+
                                                |
                                         5. semantic search
                                                v
                                         +-----------------+
                                         |   Moss index    |
                                         +-----------------+
```

Small talk skips steps 4–5 (Claude returns text on first call).

### Files

| File | Role |
|---|---|
| `moss_agentphone.py` | Tool schema, tool-call loop, signature verify, history mapping, NDJSON + log helpers |
| `server.py` | FastAPI shell — env, clients, `_moss_search`, `/webhook` route |
| `create_index.py` | One-time Moss index seeding |
| `test_integration.py` | Mocked unit tests |
| `.env.example` | Required env vars |
| `railway.json` | Railway deploy config |
| `pyproject.toml` | Deps |

### Dependencies (Python ≥ 3.11)

```toml
moss>=1.0.0
fastapi>=0.115.0
uvicorn>=0.30.0
anthropic>=0.39.0
python-dotenv>=1.0.0
```

### `.env.example`

```
MOSS_PROJECT_ID=your-moss-project-id
MOSS_PROJECT_KEY=your-moss-project-key
MOSS_INDEX_NAME=agentphone-demo-index
ANTHROPIC_API_KEY=your-anthropic-api-key
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
AGENTPHONE_WEBHOOK_SECRET=whsec_from_post_v1_webhooks
PORT=8000
```

### Tool schema

```python
TOOLS = [
    {
        "name": "moss_search",
        "description": (
            "Search the Moss knowledge base for documents that could "
            "answer the caller's question. Pass a focused natural "
            "language query."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]
```

### Tool-call loop (the core pattern)

Bounded at 5 iterations, exits when `stop_reason != "tool_use"`:

```python
async def run_tool_call(
    *,
    user_message: str,
    history: list[dict],
    anthropic_client,
    tool_handlers: dict[str, Callable],
    model: str,
    system_prompt: str,
    max_tokens: int = 256,
    max_iterations: int = 5,
) -> str:
    messages = [*history, {"role": "user", "content": user_message}]

    for _ in range(max_iterations):
        response = await anthropic_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            return _extract_text(response).strip()

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            handler = tool_handlers.get(block.name)
            output = await handler(block.input) if handler else f"Unknown tool: {block.name}"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        messages.append({"role": "user", "content": tool_results})

    return "Sorry, I am having trouble looking that up. Please try again."
```

### Mapping `recentHistory` → Anthropic messages

AgentPhone's webhook payload includes a `recentHistory` array of prior turns. Without
threading it back into Claude's `messages`, the agent has no memory within the call.

```python
def to_anthropic_history(recent: list[dict] | None) -> list[dict]:
    if not recent:
        return []
    out = []
    for entry in recent:
        text = (entry.get("content") or "").strip()
        if not text:
            continue
        role = "assistant" if entry.get("direction") == "outbound" else "user"
        out.append({"role": role, "content": text})
    return out
```

### Webhook route (FastAPI)

```python
@app.post("/webhook")
async def webhook(
    request: Request,
    x_webhook_signature: str = Header(...),
    x_webhook_timestamp: str = Header(...),
    x_webhook_id: str = Header(...),
    x_webhook_event: str = Header(...),
):
    body = await request.body()
    if not verify_webhook_signature(
        secret=WEBHOOK_SECRET,
        timestamp=x_webhook_timestamp,
        body=body,
        signature=x_webhook_signature,
    ):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    event = await request.json()
    if event.get("event") != "agent.message" or event.get("channel") != "voice":
        return {"ok": True}

    data = event.get("data") or {}
    transcript = data.get("transcript", "")
    history = to_anthropic_history(event.get("recentHistory"))

    async def generate() -> AsyncIterator[bytes]:
        yield ndjson({"text": "Let me check that for you.", "interim": True})
        try:
            answer = await run_tool_call(
                user_message=transcript,
                history=history,
                anthropic_client=anthropic_client,
                tool_handlers=TOOL_HANDLERS,
                model=MODEL,
                system_prompt=SYSTEM_PROMPT,
            )
        except Exception:
            answer = "Sorry, I ran into a problem. Could you try again?"
        yield ndjson({"text": answer})

    return StreamingResponse(generate(), media_type="application/x-ndjson")
```

### Moss tool handler

```python
async def _moss_search(args: dict) -> str:
    query = (args or {}).get("query", "").strip()
    if not query:
        return "moss_search requires a non-empty query string."

    result = await moss_client.query(
        INDEX_NAME, query, QueryOptions(top_k=5, alpha=0.8),
    )
    if not result.docs:
        return "No relevant excerpts found."
    return "\n".join(
        f"{i}. {getattr(d, 'text', '')}"
        for i, d in enumerate(result.docs, start=1)
    )
```

### System prompt that actually works for voice

```python
SYSTEM_PROMPT = (
    "You are a friendly customer-support agent on a phone call. "
    "When the caller asks about company policies, orders, refunds, "
    "shipping, returns, account help, or product facts, call the "
    "moss_search tool first and answer using ONLY what it returns. "
    "If the search returns nothing relevant, say so honestly. Keep "
    "replies short and conversational, two to three sentences."
)
```

### Index pre-load on app startup

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await moss_client.load_index(INDEX_NAME)
    yield
```

This is what gets you sub-10 ms queries instead of round-tripping to the control plane each
turn.

### Local dev with ngrok

```bash
uv sync
uv run python create_index.py
uv run python server.py        # terminal 1
ngrok http 8000                # terminal 2 — copy https URL
# Register the ngrok URL as your AgentPhone webhook
```

Free-tier ngrok URLs change on each restart — re-register the webhook each session.
Alternative tunnel: `cloudflared tunnel --url http://localhost:8000`.

### Deploy to Railway

`railway.json` is included. Set Root Directory to `examples/cookbook/agentphone`, paste env
vars in Variables tab (skip `PORT` — Railway injects it), generate a domain, register the
domain as the AgentPhone webhook, paste the returned `secret` into `AGENTPHONE_WEBHOOK_SECRET`,
let it redeploy.

### Healthy log lines

```
delivery=voice_... event=agent.message channel=voice
POST https://api.anthropic.com/v1/messages "HTTP/1.1 200 OK"
POST https://api.anthropic.com/v1/messages "HTTP/1.1 200 OK"
"POST /webhook HTTP/1.1" 200 OK
```

Two Anthropic calls = one tool round-trip (tool_use → result → final text). Small talk shows
one Anthropic call.

### Common pitfalls

| Symptom | Fix |
|---|---|
| `401 invalid webhook signature` | `AGENTPHONE_WEBHOOK_SECRET` is wrong. Use the `whsec_...` from `POST /v1/webhooks` response, **not** your `sk_live_...` API key |
| ngrok URL changes every session | Re-register webhook, or deploy to Railway |
| Agent says "What are you building?" | Default `beginMessage` — `PATCH /v1/agents/{id}` with a new one |
| Caller hears silence while LLM thinks | You're not streaming interim NDJSON — yield `{"text": "One moment.", "interim": true}` first |
| Agent forgets previous turn in same call | You're not threading `recentHistory` into Claude `messages` |

---

## Combination patterns

Each pattern below maps to one of the hackathon build tracks.

### 1. Voice receptionist with KB → Cold callers / The doer

AgentPhone (ingress) + Moss (KB lookup mid-turn). Already implemented in the cookbook above.

### 2. Persistent caller memory → upgrade for any voice agent

```
inbound call → webhook → supermemory.profile(caller_id) → seed system prompt with profile
                                                       → run LLM
                                                       → speak reply
on call_ended → supermemory.add(transcript, container_tag=caller_id)
```

Container tag = caller's E.164 number. Static facts (name, company) + dynamic facts (last
order, last complaint) get returned by `profile()` on the next call.

### 3. Cold caller that closes → Cold callers track

```
outbound: POST /v1/calls (AgentPhone, webhook mode)
  └─ each turn → Claude tool-call loop
       ├─ tool: moss_search        ──► product / pricing / objection-handling KB
       ├─ tool: book_meeting       ──► your calendar API
       └─ tool: create_payment_link ──► Stripe Agent Toolkit (upfront deposit)
  └─ end of call → AgentMail sends a confirmation email with the Payment Link
```

### 4. Inbox warrior → Inbox warriors track

```
AgentMail webhook on incoming email
  └─ supermemory.profile(sender)        ──► who is this person, what's the history
  └─ Claude tool-call loop
       ├─ tool: supermemory_recall      ──► past threads with this sender
       ├─ tool: moss_search             ──► company KB / templates
       └─ tool: schedule_meeting        ──► your calendar API
  └─ AgentMail reply or draft
  └─ supermemory.add(thread_summary, container_tag=sender)
```

### 5. The negotiator → The negotiator / Web wranglers tracks

```
Claude tool-call loop
  ├─ tool: browser_use_run("find best price for X on site Y, get to checkout")
  ├─ tool: sponge_wallet_pay(amount, recipient)   # or Stripe if you're charging the user
  └─ tool: agentmail_confirm(receipt_to_user)
```

Browser Use does the web work; Sponge/Stripe closes the loop financially.

### 6. The fixer → multi-channel showpiece

The "all three plus" version that nails the multi-channel judging criterion:

```
Inbound trigger from any channel
  ├─ voice  → AgentPhone webhook
  ├─ SMS    → AgentPhone webhook (channel=sms)
  ├─ email  → AgentMail webhook
  └─ web    → your dashboard

Unified handler:
  └─ supermemory.profile(user_id)                 ──► who they are
  └─ Claude tool-call loop with all of:
       ├─ moss_search          ──► your KB
       ├─ browser_use_run      ──► web tasks
       ├─ stripe_payment_link  ──► take money
       ├─ sponge_pay           ──► spend money
       ├─ agentmail_send       ──► follow-up email
       └─ agentphone_sms       ──► follow-up text
  └─ supermemory.add(interaction, container_tag=user_id)
```

### 7. Outbound campaign agent

`POST /v1/calls` (AgentPhone) with a list of targets and a `systemPrompt`. Use hosted mode for
simplicity, or webhook mode if you want Moss lookups during each call. Pipe results into
Supermemory so a follow-up call days later picks up where this one left off.

---

## Anthropic model selection

| Use | Model | Why |
|---|---|---|
| Voice webhook loop (default) | `claude-haiku-4-5-20251001` | Lowest latency — what the cookbook ships |
| Multi-tool reasoning over the call | `claude-sonnet-4-6` | Balanced quality/latency |
| Complex agent planning | `claude-opus-4-7` | Highest quality |

Voice budgets: aim for total turn latency < 1.5 s. Haiku + a preloaded Moss index hits this
easily. Sonnet usually does too with one tool call; Opus is risky for a single-turn voice loop.

---

## Env-var cheat sheet

```bash
# AgentPhone
AGENTPHONE_API_KEY=sk_live_...
AGENTPHONE_WEBHOOK_SECRET=whsec_...

# Moss
MOSS_PROJECT_ID=...
MOSS_PROJECT_KEY=...
MOSS_INDEX_NAME=agentphone-demo-index

# Supermemory
SUPERMEMORY_API_KEY=sm_...

# Browser Use
BROWSER_USE_API_KEY=bu_...

# AgentMail
AGENTMAIL_API_KEY=...

# Stripe (use restricted key in production)
STRIPE_SECRET_KEY=rk_...

# Sponge (sandbox key from on-site booth)
SPONGE_API_KEY=...

# Anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-4-5-20251001

# App
PORT=8000
```

---

## Useful links (one-stop)

### Hackathon

| Resource | URL |
|---|---|
| YC hackathon page | https://events.ycombinator.com/CallMyAgentHackathon |

### AgentPhone

| Resource | URL |
|---|---|
| Marketing | https://agentphone.ai/ |
| API docs | https://docs.agentphone.ai |
| Skills (LLM-friendly) | https://agentphone.ai/skills.md |
| Calls guide | https://docs.agentphone.ai/documentation/guides/calls |
| LLM index | https://docs.agentphone.ai/llms.txt |

### Moss

| Resource | URL |
|---|---|
| Site | https://moss.dev |
| Docs | https://docs.moss.dev/docs |
| LLM index | https://docs.moss.dev/llms.txt |
| Full LLM reference | https://docs.moss.dev/llms-full.txt |
| Cookbook (AgentPhone) | https://github.com/usemoss/moss/tree/main/examples/cookbook/agentphone |

### Supermemory

| Resource | URL |
|---|---|
| Site | https://supermemory.ai |
| Docs | https://supermemory.ai/docs/intro |
| LLM index | https://supermemory.ai/docs/llms.txt |

### Browser Use

| Resource | URL |
|---|---|
| Site | https://browser-use.com/ |
| Docs | https://docs.browser-use.com |
| Cloud quickstart | https://docs.browser-use.com/cloud/quickstart |
| LLM index | https://docs.browser-use.com/llms.txt |
| Cloud console | https://cloud.browser-use.com |
| GitHub | https://github.com/browser-use/browser-use |

### AgentMail

| Resource | URL |
|---|---|
| Site | https://agentmail.to/ |
| Docs | https://docs.agentmail.to |
| Console | https://console.agentmail.to |
| Quickstart | https://docs.agentmail.to/quickstart |
| Pricing | https://agentmail.to/pricing |

### Stripe (Agent Toolkit)

| Resource | URL |
|---|---|
| Agents overview | https://docs.stripe.com/agents |
| Restricted keys | https://docs.stripe.com/keys#limit-access |
| Payment Links | https://docs.stripe.com/payment-links |

### Sponge

| Resource | URL |
|---|---|
| Site | https://paysponge.com/ |
| YC profile | https://www.ycombinator.com/companies/sponge |

### Anthropic

| Resource | URL |
|---|---|
| API docs | https://docs.anthropic.com |
| Tool use guide | https://docs.anthropic.com/en/docs/build-with-claude/tool-use |
