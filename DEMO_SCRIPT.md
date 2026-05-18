# JailCall — Demo Video Script

**Target length:** ~95–100s
**Format:** 16:9, no narrator (VO only in the framing beat)
**Tagline:** *You only get one call. We answer it.*

Audio in `> quote` blocks is real recorded audio from the actual JailCall demo call. Everything in **bold** is on-screen direction.

---

## [0:00–0:38] Cold open — 38s

Six Veo-generated clips, cut together as previously assembled. Diegetic audio only (footsteps, dial tone, fluorescent buzz, receiver clatter). Sparse piano enters at clip 2, thins to silence by end of clip 6.

Cold open ends on the cell breakdown. Hold one beat of black.

---

## [0:38–0:41] Transition card — 3s

**White text on black, sans-serif, centered. Dead silent.**

> *What if the number always answered?*

Hold 2s, dissolve.

---

## [0:41–0:51] Framing VO — 10s

**Visual:** Bay Area map appears, JailCall logo materializes over San Francisco County. Soft warm glow.

**VO (calm, plain, no pitch energy):**

> "Every year, ten million people get booked in the U.S. Most don't know a criminal lawyer's number. JailCall is the number you call when you don't know who to call."

---

## [0:51–1:11] Call 1 — 20s

**Split screen.** Left: phone UI with call-in-progress waveform. Right: live tool-call log streaming from `jailcall/static/index.html` dashboard.

**Audio (the actual call):**

> **JailCall** *(AgentPhone-played `beginMessage`)*: "Thanks for calling JailCall. I can help you find a criminal defense attorney in the Bay Area. What's your name?"
>
> **Caller:** "John Doe."
>
> **JailCall:** "Thanks John. What are you charged with? Just the general category — DUI, drug, assault, anything like that."
>
> **Caller:** "DUI."
>
> **JailCall:** "Got it. Reaching out now."
>
> *(brief pause while tools fire — Moss + 3 parallel AgentMail sends — ~3-4s)*
>
> **JailCall:** "I've reached out to three attorneys for you: Law Office of Robert Tayac, Gasner Criminal Law, and Lipton Law Office. They know you're at San Francisco County Jail and can reach you through the facility — either by phone or an in-person attorney visit. Hang in there."

**Right panel — cascading as the audio plays:**

```
→ moss_find_lawyers(charge_category="dui")
  ✓ 3 firms · 8ms
→ email_attorneys × 3 (parallel)
  ✓ Law Office of Robert Tayac    · sent
  ✓ Gasner Criminal Law           · sent
  ✓ Lipton Law Office             · sent
```

**Bottom third — three AgentMail email cards animate in with real `To:` addresses, `Subject:`, and a one-line preview. Each flips to green `SENT` stamp.**

---

## [1:11–1:16] Reply lands — 5s

**Cut to a clean AgentMail inbox UI.** A new email slides in at the top with notification chime:

```
From: Alanna D. Coopersmith <alanna@eastbaydefense.com>
Subject: Re: New client inquiry — John Doe, DUI booking
"Available to take this case. Can be at San Francisco
County Jail for an attorney visit this afternoon."
```

Hold on the email for 2s. No VO.

---

## [1:16–1:19] Time cut card — 3s

**Black, white text:**

> *A few hours later. He gets another call.*

---

## [1:19–1:37] Call 2 — the Supermemory payoff — 18s

**On-screen during this:** a Supermemory recall card slides in from the right, visibly populating the agent's context. Fields show:

```
John Doe
DUI · San Francisco County Jail
Last call: 3h ago · 3 firms contacted
Replies (1):
  Alanna D. Coopersmith — "Available, ETA this afternoon"
```

**Audio:**

> **JailCall:** "Welcome back John. Last we spoke you were booked on a DUI at San Francisco County Jail. How can I help?"
>
> **Caller:** "Did anyone get back to me?"
>
> **JailCall:** "Yes — Alanna Coopersmith wrote back. She can take your case and is available to be at the jail this afternoon for an attorney visit."
>
> **Caller:** "What happens at arraignment?"
>
> **JailCall:** "It's your first court appearance. The judge reads the DUI charge, asks how you plead, and sets bail. For a first-offense DUI in San Francisco, it usually happens within 48 hours of booking. Coopersmith should be there with you — want me to confirm she'll attend?"

Hold the Supermemory card through "Coopersmith should be there with you."

---

## [1:37–1:40] Outro — 3s

**Black. JailCall logo center. Tagline below in light gray. Sponsor strip at bottom.**

> **JailCall** — *You only get one call. We answer it.*
>
> *Built at Call My Agent · AgentPhone · Gemini · Moss · AgentMail · Supermemory · Browser Use*

End.

---

## Production notes

### Audio
- **Caller voice:** stressed-but-controlled, not theatrical. The contrast with the agent's calm sells the relief.
- **Agent voice:** whatever AgentPhone's actual voice config produces — record from a real call, don't re-synth.
- **No music after the cold open.** The product audio should breathe. Ambient room tone only.
- **Pause length matters.** The 3-4s gap between "Reaching out now" and the confirmation line is where the dispatch happens. Don't cut it short — that pause IS the agent working. Stretch it slightly if Moss is too fast in your dry run.

### Visual
- **Use the real dashboard.** `jailcall/static/index.html` shows the live tool-call stream during a call. Screen-capture from it for the right panel — don't fake a terminal UI.
- **AgentMail email cards** can be screenshotted from the real outbox. Use the actual sent emails from your dry run.
- **Supermemory recall card** is the most important visual. Design it intentionally — clean font, generous spacing, screenshotworthy. This is the image judges will remember.

### Timing flex
- If you run long: trim the framing VO from 10s to 7s by cutting "Every year, ten million people get booked in the U.S." → just open with "Most people don't know a criminal lawyer's number."
- If you run short: extend Call 2 with a beat where the agent confirms the meeting — adds 5s, reinforces the "lives in time" feeling.

### Pre-record vs live
The plan in `SPEC.md` → "Demo script (for judges)" assumes live dialing. For the video, **pre-record both calls** so audio is clean and timing is predictable. The dashboard can still be a real screen capture from a real test call.

### Things to swap before publishing
- Caller voice — replace placeholder name "John Doe" if you have a more natural-sounding alternative.
- Confirm the three firms returned by Moss in your dry run match the audio — Moss is deterministic per query but small index changes can reshuffle. Re-record the confirmation line if needed.
- Confirm Coopersmith's actual reply content (it's whatever you send to the AgentMail inbox during the dry run).

### Spec alignment
This script matches `SPEC.md` as of 2026-05-17:
- 2-field intake (name + charge, no callback)
- 2-tool architecture (Moss + AgentMail; memory is auto-injected, not a tool)
- San Francisco County Jail hardcoded
- 3 firms per dispatch, parallel sends in one iteration
- Final confirmation line is the spec-locked version verbatim
- Returning-caller greeting follows the recall-augmented pattern
