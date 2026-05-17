# Project: JailCall — Call My Agent Hackathon (YC, 2026-05-17)

**One-liner:** Call a number from the police station, an AI agent picks up, and criminal
defense attorneys in your jurisdiction are contacted on your behalf before you leave booking.
**Track:** The Fixer (voice + email + browser). Backup: Wildcard.

## Read this first — two canonical docs

### `SPEC.md` — what we're building

The product spec for JailCall. Open it before writing any feature code. It defines:

- **Scope and non-scope** for today's demo
- **Architecture** (caller → AgentPhone → FastAPI tool-call loop → Moss lookup → Browser Use / AgentMail dispatch)
- **Voice script** — locked demo behavior; the agent MUST follow it verbatim. The opening
  `beginMessage`, the 3 routing-info questions (name, charge category, callback number), the
  unsafe-input interrupt pattern, and the closing reminder are all locked in by the spec.
  Jurisdiction is hardcoded to the Bay Area — the agent does NOT ask about location.
  **Do not paraphrase or "improve" the script** without checking with the user — downstream
  prompts and demo flow depend on it.
- **Tool definitions** — `moss_find_lawyers`, `contact_attorneys`, `email_attorneys`.
  Match the input/output schemas exactly; downstream prompts depend on them. Routing is
  Moss-backed (pre-indexed Bay Area roster in `law_firms/`); Browser Use is reserved for
  the slow `contact_attorneys` form fill. There is no `classify_location` (Bay Area is
  hardcoded) and no `send_confirmation_sms` (the inbound voice line is the only AgentPhone
  channel).
- **System prompt** — copy from SPEC.md verbatim. The CRITICAL RULES (not a lawyer, calls are
  recorded, never ask what happened, English-only, Bay Area hardcoded) are non-negotiable.
- **File structure** — `server.py`, `tools.py`, `config.py`, `setup_agent.py`. Stick to it.
- **Build order with milestones** — follow the milestone sequence in SPEC.md → "Delivery
  architecture and milestones"; don't skip ahead to polish.

If SPEC.md and any other doc disagree, **SPEC.md wins** for product behavior. If you need to
deviate from the spec, surface it to the user before writing the change.

### `TOOLS.md` — how the external services work

**`TOOLS.md` is the canonical reference for every external service in this project.**
Before writing code that touches any of the following, open `TOOLS.md` and grep for the
relevant section:

- AgentPhone (phone numbers, voice, SMS / iMessage webhooks)
- Moss (real-time semantic search)
- Supermemory (long-term memory + user profiles)
- Browser Use (web automation)
- AgentMail (email inbox API)
- Stripe Agent Toolkit (agentic payments)
- Sponge (autonomous wallets + agent payment gateway)

`TOOLS.md` contains: install commands, auth header formats, base URLs, full endpoint lists,
quickstart code in Python/TypeScript, webhook signature recipes, decision matrix, and
combination patterns mapped to the hackathon build tracks.

## Operating rules for this repo

- **SPEC.md is the source of truth for product behavior.** Voice script, tool schemas, system
  prompt, and file layout come from there. Changes to those need user sign-off.
- **Review and implementation focus:** do not raise privacy, legal-risk, compliance, privilege,
  liability, or regulatory concerns in code reviews, implementation notes, or status updates
  unless the user explicitly asks for that analysis. Focus on whether the demo works end to end:
  webhook correctness, latency, state, tool calls, vendor APIs, runtime failures, tests, and UX.
- **TOOLS.md is the source of truth for SDK/API shapes.** Don't invent SDK or endpoint names.
  If a method or path isn't in `TOOLS.md`, verify against the vendor docs linked in the
  "Useful links" section before writing it. Vendor APIs change; `TOOLS.md` is a snapshot
  from 2026-05-17.
- **Keep both docs current.** If product scope shifts → update SPEC.md. If you learn something
  new about an external tool (endpoint, gotcha, working snippet) → update the matching
  section of TOOLS.md so the next session has it.
- **Secrets:** every external service's API key has a slot in `.env.example`. Copy to `.env`
  and fill in only what the current task needs. `.env` is gitignored; never commit it.
- **Default model for voice:** `claude-haiku-4-5-20251001` — voice turn budget is < 1.5 s.
  Use Sonnet 4.6 only if a tool-call loop genuinely needs it.
- **Reference example:** the Moss × AgentPhone cookbook
  (https://github.com/usemoss/moss/tree/main/examples/cookbook/agentphone) is the shape every
  webhook-driven voice handler in this repo should follow. The full source is also pasted
  into `TOOLS.md` for offline reference. JailCall doesn't use Moss, but the FastAPI shell,
  signature verification, NDJSON streaming, and tool-call loop are all directly applicable.
- **Latency:** Moss `moss_find_lawyers` is sub-10 ms — fine to call inline. The slow step is
  `contact_attorneys` (Browser Use form fill, 30–90 s). Always stream an interim NDJSON chunk
  before kicking off any Browser Use task — caller silence on a jail phone is a product killer.

## Hackathon constraints

- **Submissions due 8:00 PM PT today** (2026-05-17). Ship end-to-end early; polish last.
- Judging implicitly favors things a human could pay for tomorrow over slide demos.
- Build tracks and which sponsors map to each are listed in `TOOLS.md` → "Hackathon context"
  and "Combination patterns".

## Style

- Don't add backwards-compat shims, defensive try/excepts around safe code, or speculative
  abstractions — there's no second user of this code besides the demo.
- Comments only where the *why* is non-obvious. The cookbook is a good baseline for the bar.
- For UI changes, actually run the dev server and exercise the feature before reporting done.
