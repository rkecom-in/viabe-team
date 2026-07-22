# Viabe Team — WhatsApp Template Registry

**Source of truth** for Meta-approved WhatsApp templates with their Twilio Content SIDs. **Cowork-managed.** This is the authoritative map from `template_name` → `twilio_content_sid` + body text that the runtime will reference when sending WhatsApp messages.

**Owner:** Cowork. Established 2026-05-26 IST as the HUMAN-readable companion to `apps/team-orchestrator/config/twilio_templates.yaml` (which already existed since VT-3.3c shipped). The yaml is the runtime source of truth (name → SID for code); this markdown file adds the bodies, variable signatures, tier categorization, and approval-status notes that the yaml deliberately doesn't carry. When templates are added or rotated, BOTH files update in lockstep.

**VT-163 update (2026-05-31):** Variable signatures are now ALSO machine-readable in `twilio_templates.yaml` under the `variables:` key (ordered list of snake_case param names; index i = positional `{{i+1}}`). This file remains the human-readable companion with message body copy. The yaml `variables:` lists are sourced from the signatures documented here. They update in lockstep — when a template is added or its signature changes, update both.

**Correction to my earlier framing 2026-05-26 17:05 IST:** I initially wrote this file claiming the 8 SIDs had "no repo presence" — that was a grep-mistake on my side; the yaml has had them since VT-3.3c. The actual gap is that the COPY (message text) + variable signatures + tier categorization aren't in either the yaml OR the Meta/Twilio consoles in a way that Cowork or Claude Code can reference at brief-time. THIS file closes THAT gap.

**Source authority:** Meta-approval status owned by Fazal (vendor relationship). Twilio Content SIDs are the canonical runtime identifier — assigned when the template is uploaded to Twilio Content API after Meta approval. The `template_name` is the human-readable handle used in code.

## TEMPLATE WHITELIST — the source of truth for what may send as a template (Fazal ruling 2026-07-18)

Owner-facing template surface is MINIMAL. Everything not on the whitelist delivers ONLY inside
the 24h conversation session (queued, idle-paced — VT-683). CC maintains this list.

### ACTIVE (whitelisted, in use)

| Template | Languages / SIDs | Purpose |
|---|---|---|
| **auth OTP** | Twilio Verify (no content SID) | login/verification |
| **`team_welcome4`** | en `HXc8188616…` · hi `HXd8a8d5…` · hing `HX7097590ccf0e901d893f78d9a9224e92` (Meta approval pending) | account-created + Complete Setup button |
| **`team_wakeup2`** | en `HXaedb9a8bff0163bd4c162c90cd05bc45` · hi `HXb2dd5579ea46c2715397f2e274ec533c` · hing `HXd2bfed18f25eb8c2319ccca9b22f5d35` — **Meta APPROVED 2026-07-18** (Fazal-confirmed; category verify pending CC ask) | daily wake-up v2 — "{{2}} item(s) waiting for your review" + Review button; opens the session + drains the queue (VT-683 P3). Vars: owner_name, pending_count |

### UNDER RETIREMENT REVIEW (registered, still have live callers — migrate into the session per VT-683)

| Template | Today's caller | Migration path |
|---|---|---|
| `team_weekly_approval` | weekly cadence arm (request_owner_approval) | → owner-comms queue (VT-683 P2) |
| `team_agent_draft_approval` | L2 agent-send arm | → queue (P2) |
| `team_l3_presend_notice` | L3 arm 2h hold | → queue (P2) |
| `team_autonomy_offer` | coordinator streak sweep | → queue (P2) |
| `team_agent_stuck_escalation` | extreme-scenario escalation | → queue (P2; consider keeping template for URGENT class — Fazal call at P2 review) |
| `team_opt_out_confirmation` | reactive to owner STOP (window open) | → freeform NOW (P1 — no template needed) |
| `team_status_ping` | reactive to owner ping | → freeform NOW (P1) |
| `team_dsr_acknowledgment` | reactive to owner DSR keyword | → freeform NOW (P1) |
| `team_error_handler` | async Twilio failure callback | keep as system fallback until P4 review |
| `team_reengage` | ~~manager stale-task nudge~~ | **DONE — MERGED into `team_wakeup2`** (VT-683 point B, Fazal 2026-07-22): `stale_resume.reengage_stale_task` now sends `team_wakeup2`. Deprecated; retained for back-compat. |
| `team_monthly_report` | **ORPHANED — no code sends it** (report ships via email/PDF) | **DONE — deprecated (VT-683 P4)**; retained for back-compat |

### DEPRECATED (never send)
`team_welcome` · `team_welcome2` · `team_welcome3` · **`team_wakeup` v1** (en `HXd6c8cb13…` hi `HX26d778c5…` hing `HX08b86198…` — Meta force-converted UTILITY→MARKETING 2026-07-18, the welcome2/3 class; superseded by `team_wakeup2`) · **`team_reengage`** (VT-683 point B — merged into `team_wakeup2`) · **`team_monthly_report`** (VT-683 P4 — orphaned; report ships via email/PDF).

---

## WhatsApp Senders (the sending numbers)

The number the runtime sends FROM (`TEAM_TWILIO_FROM_NUMBER`) — a Twilio WhatsApp Sender resource, per env. Recorded here so the sender identity isn't lost in the consoles.

| Env | Display name | Number | Twilio sender SID | Meta phone_number_id | Status |
|---|---|---|---|---|---|
| **prod** | **Viabe** | `+918108084223` | `XE5dca19b08f04ba5e11d69735c6969a9d` | `1166430683220266` | PROMOTED to prod 2026-07-20 (Fazal) — was the dev sender; templates already approved under this WABA |
| **dev** | **Viabe** | `+18704122234` | `XE47d50f0ba019ad3ad3cc252e511a2e9f` | _(verify WABA in Twilio console)_ | RE-ENABLED for dev 2026-07-20 (Fazal) — dev/prod now split senders |

Notes (dev wiring, 2026-06-16, CL-431-autonomous; **prefix question RESOLVED VT-488 2026-06-29**):
- Dev `TEAM_TWILIO_FROM_NUMBER` = `+918108084223` — stored as **plain E.164, NO `whatsapp:` prefix in env**. The `whatsapp:` channel scheme IS REQUIRED on the wire and the send path (`utils/twilio_send._wa()`) applies it **idempotently to BOTH `from_` AND `to=` at the call site** (never double-prefixed). **The Twilio log proved the prefix is mandatory:** the one send that went out with a RAW E.164 `from_=+918108084223` (no `whatsapp:`) hit Twilio error **21659**; the 21 sends that carried `whatsapp:` on both ends delivered. This supersedes the earlier "NO prefix in code / flip only if the canary proves it" framing — the prefix is ON, on both ends, decided. (VT-399 first added `_wa`; VT-488 confirms + makes it the standing contract.)
- VT-487 backstop: `_wa()` now also FAIL-CLOSES on a non-E.164 target (`^\+[1-9]\d{7,14}$`) — a malformed/corrupted number (e.g. a scientific-notation float artifact like `+91998886e+11`, the six 21211 "invalid To" failures) raises `BlockedRecipientError` and is never dispatched.
- **2026-07-20 (Fazal) — dev/prod senders SPLIT.** Previously ONE number (`+918108084223`) served both envs (`TEAM_TWILIO_FROM_NUMBER` matched dev↔prod). Now: **prod keeps `+918108084223`** (India — real customers; its WABA `1166430683220266` already carries the approved template set, so prod template sends work day-one), **dev returns to `+18704122234`** (US — the VT-488-dropped number, re-enabled; SID `XE47d50f0ba019ad3ad3cc252e511a2e9f`). ONLY env change made: Railway **dev** `TEAM_TWILIO_FROM_NUMBER` → `+18704122234` (CL-431-autonomous, `--skip-deploys`; effective on next dev restart). Prod was ALREADY `+918108084223` = correct, no prod change. Both numbers share the SAME `TEAM_TWILIO_ACCOUNT_SID` (verified) → one Twilio account, shared auth token, no new credential. Split senders RESOLVE the earlier "can't test dev inbound on the same number" problem — each env now has its own inbound webhook. **Fazal-console TODO:** (1) verify the US dev number's WABA has the templates approved (else dev template sends fail — tolerable, dev is mostly free-form session sends per the whitelist ruling); (2) point the dev number's Twilio inbound webhook → `viabe-team-web-dev` `/api/team/twilio/webhook`, prod number's → prod team-web webhook.
- Dev **inbound** callback (Twilio sender webhook) → team-web `…/api/team/twilio/webhook` on `viabe-team-web-dev`, behind a Vercel **Protection-Bypass-for-Automation** token. The bypass secret is kept OUT of the repo — it lives only in the Twilio sender callback URL and in `TEAM_TWILIO_WEBHOOK_URL` (Vercel `viabe-team-web-dev`, Production). Twilio signs the full callback URL incl. the bypass query param, so those two must stay byte-identical.

## Why this file exists in this place

WhatsApp templates have a registration lifecycle outside the codebase: author → Meta review → approve → upload to Twilio → get a SID. The SID is the only thing the runtime cares about. Without a durable repo-side record of the `template_name` → `SID` mapping:
- Code that wants to send a template can only hard-code SIDs (fragile)
- Status drift between Meta + Twilio + repo is invisible
- New templates get added without process

This file becomes the contract. Code in `apps/team-orchestrator/` (when wired) references this file (or a YAML mirror generated from it) to resolve `template_name → SID` at call time.

## Status (2026-05-26 IST)

**8 templates Meta-approved + Twilio-registered.** Per CL-5 / CL-11: target counts are 5 launch-blocking Tier-A + 17 Tier-B concierge-until-approved. The 8 below cover the 5 Tier-A (best estimate; Fazal-confirm categorization) plus 3 operational fallbacks.

---

## Approved templates

### `team_welcome4`  *(live welcome — Tier-A launch-blocking)*

- **Twilio Content SID (en):** `HXc8188616b2e97b557f4c7330157c4f8f`
- **Twilio Content SID (hi):** `HXd8a8d5945c79c75d373d9c24edd4b183`
- **Meta category:** **UTILITY** (created + submitted 2026-07-02 via Content API — `canaries/vt555_welcome4_create.py`; approval ASYNC — `status=received` at submit)
- **Content type:** `twilio/quick-reply` — a "Complete Setup" button (id/payload **`COMPLETE_SETUP`**, same both langs)
- **Variables:** `{{1}}` = owner name ONLY (the `{{2}}` trial-date var was dropped — no trial/free wording)
- **Copy (en):** `Hi {{1}}, your Viabe account has been created. To complete your setup, tap the button below.`
- **Copy (hi):** `नमस्ते {{1}}, आपका Viabe अकाउंट बन गया है। सेटअप पूरा करने के लिए नीचे दिए गए बटन पर टैप करें।` *(HI copy = Fazal-approved)*
- **Button (en/hi):** `Complete Setup` / `सेटअप पूरा करें`
- **Twilio Content SID (hing / hi-Latn):** `HX7097590ccf0e901d893f78d9a9224e92` — Roman-script
  Hindi registered under Meta language=en (Meta has no hi-Latn code), Fazal-created 2026-07-18
  (O5). Meta approval ASYNC; hinglish welcome routing flips to it on Fazal's approved-confirm.
- **TEMPLATE-WHITELIST RULING (Fazal 2026-07-18):** owner-facing template surface is MINIMAL —
  auth OTP (Twilio-handled) + welcome + wake-up/re-engage ONLY. All other owner comms ride the
  24h conversation session (owner replies daily → session opens → queued comms deliver at idle
  pace). NO hing variants for weekly_approval / monthly_report / draft_approval, by design.
- **VT-555:** replaces `team_welcome3` (Meta force-converted that UTILITY→MARKETING). Leads with the account-created FACT + one transactional next-step (a button), no offer/promotion/brand-greeting → Meta classifies UTILITY. `_default_welcome` + the `welcome` routing both point here. Continue-trigger: the onboarding journey lazy-starts on the button tap OR any typed reply.

---

### `team_welcome3` — **DEPRECATED (VT-555)**: Meta force-converted this from **UTILITY → MARKETING** after approval → would be declined like team_welcome2 (63049). Superseded by `team_welcome4` (strictly-transactional UTILITY quick-reply, trial wording removed). SIDs en `HX3ec52f76cf477cebf80b3eff5835817e` / hi `HX1ee2cb5bb504137ff8be5071ee9b7799`. The appeal path was dropped for the fresh template (Fazal 2026-07-02). Not sent by any live path.

---

### `team_welcome2` — **DEPRECATED (VT-520)**: Meta-approved as **MARKETING** → delivery declined (63049 "Meta chose not to deliver this marketing message"). Superseded by `team_welcome3` (UTILITY). SIDs en `HX65602e94b48bb2d6e82c70630d01da20` / hi `HXa2e1bcb65189ed25ec1f6b92d9458108`. Not sent by any live path.

---

### `team_welcome` — **DEPRECATED (VT-404)**, superseded by `team_welcome2`. Not sent by any live path.

- **Twilio Content SID:** `HX1b66c0daaa52dc0b8575e50eebadfdd1` (en) · `HXf154fc0f582955f65c75b6306662388a` (hi)
- **Tier:** Tier-A (retained for history/back-compat resolution only)
- **Variables:** `{{1}}` = owner name, `{{2}}` = trial end date
- **Why retired:** the copy told a passive owner to WAIT for a message ("You'll receive a WhatsApp
  message here when the proposal is ready") — it never invited a reply, so the 24h window never
  opened and onboarding stalled (Sundaram e2e).

```
Hi {{1}}, your Viabe Team account is now active. Your trial period ends on {{2}}.
During this period, your agent will review your business data and prepare its first
campaign proposal for your review. You'll receive a WhatsApp message here when the
proposal is ready.
```

---

### `team_weekly_approval`

- **Twilio Content SID:** `HX44b053c946a230ea0d2d3d2dc6118964`
- **Tier:** Tier-A (launch-blocking, core proposal flow)
- **Variables:** `{{1}}` = customer segment, `{{2}}` = campaign mode, `{{3}}` = projected recovery ₹

```
This week I'd like to run a {{2}} campaign targeting {{1}} customers. Based on
similar campaigns, this could recover approximately ₹{{3}} in revenue. Reply YES
to approve, NO to skip this week, or EDIT to discuss changes. I'll wait for your
reply before sending anything.
```

---

### `team_opt_out_confirmation`

- **Twilio Content SID:** `HX6365c429e75c2e191bf396e1c6ba8708`
- **Tier:** Tier-A (compliance, customer-paused flow)
- **Variables:** `{{1}}` = owner name

```
Got it, {{1}}. I've paused all automated messages and campaigns immediately. Your
subscription remains active for billing purposes, but I won't initiate anything new
until you tell me to restart. To resume, reply START. To cancel your subscription
entirely, reply CANCEL and I'll process that for you. Thanks for letting me know.
```

---

### `team_dsr_acknowledgment`

- **Twilio Content SID:** `HXcda0b9bb6ea92c072fb8eb7d06163ef0`
- **Tier:** Tier-A (DPDP compliance, mandatory acknowledgment within 30 days)
- **Variables:** `{{1}}` = owner name, `{{2}}` = DSR type (e.g. "data access", "deletion"), `{{3}}` = completion deadline date

```
Hi {{1}}, I've received your {{2}} request. Per the Digital Personal Data Protection
Act, I have 30 days to respond fully. I'll complete your request by {{3}} and
confirm here on WhatsApp once done. If you have questions in the meantime, reply
to this message and I'll get back to you within one business day.
```

---

### `team_agent_stuck_escalation`

- **Twilio Content SID:** `HX6f15db7fee7037c570ba122387f39b10`
- **Tier:** Tier-A or Tier-B (operational fallback — agent surfaces uncertainty; Fazal-confirm)
- **Variables:** `{{1}}` = owner name, `{{2}}` = what the agent got stuck on, `{{3}}` = context

```
Hi {{1}}, I got stuck on {{2}}. {{3}}. Can you reply here with your guidance? Or
if you'd prefer to talk to a human directly, reply ESCALATE and someone from the
Viabe team will reach out to you. I'd rather pause and ask than guess and get it
wrong.
```

---

### `team_unable_to_complete_request`

- **Twilio Content SID:** `HXb545fe12033d79293f61bc614baa4caf`
- **Tier:** Tier-A or Tier-B (operational fallback; Fazal-confirm)
- **Variables:** `{{1}}` = owner name, `{{2}}` = request description, `{{3}}` = failure reason

```
Hi {{1}}, we tried to complete your request: {{2}}. It didn't go through because:
{{3}}. We'll keep working on it and update you. If you'd like to change the
approach, just reply here.
```

---

### `team_error_handler`

- **Twilio Content SID:** `HXe9212e16b8647a5d9ab6fcff647bf600`
- **Tier:** Tier-A or Tier-B (system-level fallback; Fazal-confirm)
- **Variables:** `{{1}}` = owner name, `{{2}}` = action we tried, `{{3}}` = possible reasons

```
Hi {{1}}, we've been trying to {{2}} but keep running into issues. Possible reasons:
{{3}}. We've paused this for now. Reply here with how you'd like us to proceed, or
we'll try a different approach in 24 hours.
```

---

### `team_status_ping`

- **Twilio Content SID:** `HX11199e6fc93eaa1f8b26071995614476`
- **Tier:** Tier-A (keep-alive ping during quiet weeks; Fazal-confirm)
- **Variables:** `{{1}}` = owner name, `{{2}}` = last activity description, `{{3}}` = next-up description

```
Hi {{1}}, things are running. Last activity on your account: {{2}}. {{3}} is up next.
```

---

### `team_reengage`  *(VT-486 — owner-facing OUT-OF-WINDOW (>24h) re-engagement; system-invoked, NOT agent-selectable)*

> **DEPRECATED (VT-683 point B, Fazal 2026-07-22):** merged into the daily wake-up — `manager/stale_resume.reengage_stale_task` now sends **`team_wakeup2`** (one re-engage surface). Retained for history/back-compat resolution only; no live path sends this.

- **Twilio Content SID (en):** `HXbdb250089fafc02a0d75ce6817e9ce11`
- **Twilio Content SID (hi):** `HX27a50d65fedbb7b6a3c2fb6a6a24f13c`
- **Category:** Utility · **Audience:** owner · **NO STOP line** (STOP is a customer-marketing opt-out, not an owner utility)
- **Variables:** `{{1}}` = owner_name
- **VT-486:** the window-aware owner send. When >24h have passed since the tenant's last inbound, the
  24h owner-care window is CLOSED, so a free-form re-engage send fails (Twilio 63016). This
  Meta-approved template is sent instead (out-of-window). The owner's reply RE-OPENS the 24h window
  and the conversation continues free-form (the VT-349 in/out-of-window split + the VT-479 in-window
  path). Routed via `template_routing.yaml` `reengage: any → team_reengage`; the composer selects it
  out-of-window. `agent_selectable: false`.

```
Hi {{1}}, this is your Viabe Team. I have updates ready on your business — are you available to continue? Reply to this message and I'll pick up where we left off.
```

---

### `team_campaign_not_sent`  *(VT-248 — SYSTEM-invoked on fail-closed campaign rejection, NOT agent-selectable)*

- **Twilio Content SID:** `HXcedcda2a0bc1e8f47b37950ef458feb4` (en) / `HXcd2688e6ea1862c063378b18e382e700` (hi)
- **Category:** Utility · **Content type:** Text
- **Variables:** `{{1}}` = owner name, `{{2}}` = count of targets that couldn't be verified
- **Privacy invariant (VT-241):** the owner sees the COUNT only — never ids, never a cross-tenant distinction. The full rejected-id list stays in the operator audit log.

```
Hi {{1}}, I couldn't send this week's campaign: {{2}} of the targeted customers couldn't be verified, so I held the entire campaign — nothing was sent. Reply here to retry or adjust the targeting.
```

---

### `onboarding_confirm_yesno`  *(VT-479 — INTERACTIVE in-session quick-reply buttons; NOT a Meta-approved 24h-window template)*

- **Twilio Content SID:** `HX60ace8008b02439ca0db444dee6327d2` (en)
- **Content type:** `twilio/quick-reply` (Yes / No / Skip buttons) · **Approval:** NONE NEEDED — in-session interactive content (≤3 buttons) does not require Meta template approval; this HX is a Twilio Content-API registration only (created by CC on the dev account).
- **Sent by:** `onboarding/journey._send` for a CONFIRM question, via `twilio_send.send_interactive_message` (NOT `send_template_message`). The journey only sends in response to an owner inbound → the 24h session window is open by construction.
- **Variables:** `{{1}}` = the confirm question text (the reconciled prompt, e.g. "We found you're a Local services business — is that right?").
- **Button `id` payloads:** `confirm_yes` / `confirm_no` / `confirm_skip`. The button TITLE ("Yes"/"No"/"Skip") flows back as the inbound `Body` and matches the existing `_YES`/`_NO`/`_SKIP` token sets in `handle_reply` — so no answer-parse change was needed; buttons just remove the brittle free-text "yes" reliance (the VT-477 root). Falls back to plain freeform text on any send failure.

```
{{1}}
[ Yes ]  [ No ]  [ Skip ]
```

---

### `team_signup_consent_buttons`  *(VT-691 — INTERACTIVE in-session signup CONSENT ask; NOT a Meta-approved 24h-window template)*

- **Twilio Content SID:** en `HXa81bc34018ba6e4349622962b6235f06`
- **Content type:** `twilio/quick-reply` (static bilingual body, no variables; two buttons) · **Approval:** NONE NEEDED — in-session interactive (created by CC 2026-07-22, canary `canaries/vt691_consent_buttons_create.py`).
- **Sent by:** `onboarding/whatsapp_signup._send_consent_prompt` to an UN-onboarded number whose own inbound just opened the 24h window (freeform text with a typed-exact instruction is the fallback).
- **Fazal ruling (2026-07-22):** the signup does NOT start unless the person explicitly presses **"I agree"**; **"I do not agree"** is the explicit refusal path (DPDP/EU). The button TITLE echoes back as the inbound Body; the consent GRANT set is the exact-normalized title — **fully deterministic, no LLM in the grant path**. Free-text "yes" re-prompts with the buttons (bounded, 3 prompts max → expired+silent).
- **Button `id` payloads:** `consent_agree` / `consent_disagree`. NEVER change a title without updating `_AGREE_TITLE`/`_DISAGREE_TITLE` in `whatsapp_signup.py` in the same commit.

```
Namaste! This is Viabe Team — … consent text with viabe.ai/team/dpdp + /privacy links …
[ I agree ]  [ I do not agree ]
```

---

### `journey_suggest_3`  *(VT-694 — INTERACTIVE suggested-answer buttons on journey questions; NOT a Meta-approved template)*

- **Twilio Content SID:** en `HX41f744c4e398e5d09fd69243a766871c`
- **Content type:** `twilio/quick-reply` with **VARIABLE BUTTON TITLES** (canary-proved: Twilio accepts `{{n}}` in `actions[].title`) — `{{1}}` = the question, `{{2}}..{{4}}` = up to three suggested answers (most-likely first; padded with "Skip"). Canary `canaries/vt694_suggest_buttons_create.py`.
- **Sent by:** `journey._send_suggestion_buttons` (deterministic walker gap questions with suggestions + turn-brain dynamic button sets). A tap echoes the suggestion text as the inbound Body = the recorded answer; typing still works. Freeform fallback on any failure.
- **Button `id` payloads:** `suggest_1` / `suggest_2` / `suggest_3` (titles are the data; ids unused by parsing).

```
{{1}}
[ {{2}} ]  [ {{3}} ]  [ {{4}} ]
```

---

### `team_approval_buttons`  *(VT-683 P2c — INTERACTIVE in-session approval ask; NOT a Meta-approved 24h-window template)*

- **Twilio Content SIDs:** en `HX6b8aa56b3497301f86152983686064d7` · hi `HX3b0f0c7926f557e4de1d007682cdaabe`
- **Content type:** `twilio/quick-reply` (two decision buttons) · **Approval:** NONE NEEDED — in-session interactive content (≤3 buttons) needs no Meta template approval; the HX pair is a Twilio Content-API registration only (created by CC 2026-07-22, canary `canaries/vt683_approval_buttons_create.py`).
- **Sent by:** `agent/tools/request_owner_approval.arm_pause_request` when the owner's 24h session is OPEN, via `twilio_send.send_interactive_message` — replacing the load-bearing approval TEMPLATE send in-window. The Meta template (`team_weekly_approval` / `team_agent_draft_approval`) stays as the OUT-of-window belt until P3 wake-up + P4 whitelist retire it.
- **Variables:** `{{1}}` = the PII-safe approval ask text (`payload.summary`, composed by the arming caller).
- **Button `id` payloads:** `approval_yes` / `approval_no` (same both langs). **LOAD-BEARING:** the button TITLE flows back as the inbound `Body`, which `try_resume_pending_approval` → `classify_approval_reply` resolves deterministically against the SAME open `pending_approvals` row: en "Yes, approve"→approved / "No, reject"→rejected · hi "हाँ, मंज़ूर है"→approved / "नहीं, रहने दो"→rejected. No title collides with the opt-out/DSR guard; none is weak-ack-only. Never change a title without re-verifying both classifiers.

```
{{1}}
[ Yes, approve ]  [ No, reject ]
```

---

## Implications for code

When code in `apps/team-orchestrator/` starts sending WhatsApp messages (currently only the orchestrator + supervisor + SR-Agent skeleton exist; output composer VT-30 is Backlog), it needs:

1. **A canonical Python mapping** `TEMPLATE_SIDS: dict[str, str]` (probably at `apps/team-orchestrator/src/orchestrator/templates.py`)
2. **Variable validation** — each template's positional variables documented + checked at send-time
3. **No hard-coded SIDs scattered through code** — single import surface

That's a future VT row (likely **VT-178** when filed) — *"WhatsApp template registry as Python module + Twilio send wrapper."* Wires this `.viabe/templates.md` registry into the runtime. Depends on VT-30 (Composer) probably; possibly earlier.

For now, this markdown file IS the registry. CC reads it when needed; Cowork edits it when new templates land.

## Tier-A vs Tier-B categorization (Fazal-confirm needed)

Per CL-5: target counts are 5 Tier-A launch-blocking + 17 Tier-B concierge-until-approved (per CL-11, count grew to 22 total — current 8 + future 14). My best-guess Tier-A categorization above; Fazal-confirm at next pass.

## Status history

- 2026-05-26 17:05 IST: file created by Cowork. 8 approved templates + Twilio Content SIDs recorded from Fazal-provided list. Substrate gap closed (CL-5 + CL-11 referenced template counts but never the SIDs).

## Hindi (hi) variants + team_monthly_report (VT-163-fix-1/2/3)

Twilio issues a SEPARATE SID per language; the registry key is `(template_name, language) -> content_sid` (config/twilio_templates.yaml). Keywords (YES/NO/EDIT/START/CANCEL/ESCALATE) stay English (literal handler triggers).

### `team_monthly_report`  *(VT-163-fix-2 — system-invoked by VT-86, not agent-selectable)*

> **DEPRECATED (VT-683 P4, 2026-07-22):** ORPHANED — zero call sites in `src/`; the monthly report ships via email/PDF, never a WhatsApp template. Retained for history/back-compat resolution only.

- **Twilio Content SID:** `HX7a247e236782425866a8e20fd78df275` (en) / `HX252be212f9372e187caa03df117adc02` (hi)
- **Type:** Media (document/PDF header) + body. Category: Utility.
- **Variables:** `{{1}}` = owner name, `{{2}}` = month, `{{3}}` = recovered ₹

```
Hi {{1}}, your Viabe Team report for {{2}} is ready — I've attached the full PDF. It covers the campaigns I ran with your approval, the customers reached, and the revenue attributed to them: ₹{{3}} this month. Tap the document above to view the details.
```

```
नमस्ते {{1}}, {{2}} के लिए आपकी Viabe Team रिपोर्ट तैयार है — मैंने पूरी PDF संलग्न कर दी है। इसमें वे कैंपेन शामिल हैं जो मैंने आपकी मंज़ूरी से चलाए, कितने ग्राहकों तक पहुँचा गया, और उनसे जुड़ा राजस्व: इस महीने ₹{{3}}। विवरण देखने के लिए ऊपर दिए दस्तावेज़ पर टैप करें।
```

---

### Hindi bodies for the 8 existing templates

**`team_welcome` [hi]** — `HXf154fc0f582955f65c75b6306662388a` — **DEPRECATED (VT-404)**, replaced by
`team_welcome2` [hi] `HXa2e1bcb65189ed25ec1f6b92d9458108` (reply-inviting copy).

```
नमस्ते {{1}}, आपका Viabe Team अकाउंट अब सक्रिय हो गया है। आपकी ट्रायल अवधि {{2}} को समाप्त होगी। इस दौरान, आपका एजेंट आपके व्यवसाय के डेटा की समीक्षा करेगा और आपकी समीक्षा के लिए अपना पहला कैंपेन प्रस्ताव तैयार करेगा। प्रस्ताव तैयार होने पर आपको यहीं WhatsApp पर संदेश मिलेगा।
```

**`team_weekly_approval` [hi]** — `HX4c63feb64d392ada48b0fe11cb1d067d`

```
इस हफ़्ते मैं {{1}} ग्राहकों को लक्षित करते हुए एक {{2}} कैंपेन चलाना चाहता हूँ। इसी तरह के कैंपेन के आधार पर, इससे लगभग ₹{{3}} का राजस्व वापस मिल सकता है। मंज़ूरी देने के लिए YES लिखें, इस हफ़्ते छोड़ने के लिए NO, या बदलाव पर चर्चा के लिए EDIT लिखें। कुछ भी भेजने से पहले मैं आपके जवाब का इंतज़ार करूँगा।
```

**`team_opt_out_confirmation` [hi]** — `HX960b6de9033e0a5954a38fc09b25da2b`

```
ठीक है, {{1}}। मैंने सभी स्वचालित संदेश और कैंपेन तुरंत रोक दिए हैं। बिलिंग के लिए आपकी सदस्यता सक्रिय रहेगी, लेकिन जब तक आप दोबारा शुरू करने के लिए नहीं कहते, मैं कुछ भी नया शुरू नहीं करूँगा। फिर से शुरू करने के लिए START लिखें। अपनी सदस्यता पूरी तरह रद्द करने के लिए CANCEL लिखें और मैं उसे प्रोसेस कर दूँगा। बताने के लिए धन्यवाद।
```

**`team_dsr_acknowledgment` [hi]** — `HXac6e8f1193d97252c1afeb3516d4c9b6`

```
नमस्ते {{1}}, मुझे आपका {{2}} अनुरोध मिल गया है। डिजिटल पर्सनल डेटा प्रोटेक्शन अधिनियम के अनुसार, मेरे पास पूरी तरह जवाब देने के लिए 30 दिन हैं। मैं आपका अनुरोध {{3}} तक पूरा करूँगा और पूरा होने पर यहीं WhatsApp पर पुष्टि करूँगा। इस बीच कोई सवाल हो, तो इस संदेश का जवाब दें और मैं एक कार्यदिवस के भीतर आपसे संपर्क करूँगा।
```

**`team_agent_stuck_escalation` [hi]** — `HX913b93eecd3bf9401116365f268a1008`

```
नमस्ते {{1}}, मैं {{2}} पर अटक गया हूँ। {{3}}। क्या आप यहाँ अपना मार्गदर्शन देकर जवाब दे सकते हैं? या अगर आप सीधे किसी व्यक्ति से बात करना चाहें, तो ESCALATE लिखें और Viabe टीम का कोई सदस्य आपसे संपर्क करेगा। ग़लत अनुमान लगाकर गलती करने से बेहतर है कि मैं रुककर पूछूँ।
```

**`team_unable_to_complete_request` [hi]** — `HXa232c5bc481f90bb5f8b32d05591859a`

```
नमस्ते {{1}}, हमने आपका अनुरोध पूरा करने की कोशिश की: {{2}}। यह इस वजह से पूरा नहीं हो सका: {{3}}। हम इस पर काम करते रहेंगे और आपको अपडेट देंगे। अगर आप तरीका बदलना चाहें, तो यहीं जवाब दें।
```

**`team_error_handler` [hi]** — `HXe02bb244729c5e829fcad2453e0262ec`

```
नमस्ते {{1}}, हम {{2}} करने की कोशिश कर रहे हैं लेकिन बार-बार दिक्कतों का सामना कर रहे हैं। संभावित कारण: {{3}}। हमने फ़िलहाल इसे रोक दिया है। आप कैसे आगे बढ़ना चाहते हैं यह यहाँ जवाब देकर बताएँ, वरना हम 24 घंटे में कोई दूसरा तरीका आज़माएँगे।
```

**`team_status_ping` [hi]** — `HXa386953554630e233f5875299f2d2c94`

```
नमस्ते {{1}}, सब कुछ ठीक चल रहा है। आपके अकाउंट पर आख़िरी गतिविधि: {{2}}। अगला कदम: {{3}}।
```

**`team_campaign_not_sent` [hi]** — `HXcd2688e6ea1862c063378b18e382e700`  *(VT-248)*

```
नमस्ते {{1}}, मैं इस हफ़्ते का कैंपेन नहीं भेज सका: लक्षित ग्राहकों में से {{2}} की पुष्टि नहीं हो सकी, इसलिए मैंने पूरा कैंपेन रोक दिया — कुछ भी नहीं भेजा गया। दोबारा कोशिश करने या लक्ष्यीकरण बदलने के लिए यहाँ उत्तर दें।
```

## Business-initiated owner templates (VT-45-wire, Fazal 2026-06-06)

The 5 owner-facing business-initiated templates (out-of-window) — SIDs provisioned by Fazal
2026-06-06, wired into `twilio_templates.yaml` (was fail-closed `null`). **Cowork: the
Meta-approved BODY COPY (EN + HI) for these 5 lives in the Twilio/Meta console; add the body
text + variable signatures here at the next pass — I have the SID map, not the approved copy.**
The 3 in-window acks (`refund_processing`, `support_handoff`, `team_edge_case_ack`) are NOT
templates — they become free-form sends in VT-349 and are removed from the registry there.

| template | tier | en SID | hi SID |
|---|---|---|---|
| `trial_ending` | VT-90 trial lifecycle | `HX7a7e4a40e500b632b65d4060d62da592` | `HX93ceca39d063ce4eaebefbc6751e01b3` |
<!-- VT-365 (Fazal 2026-06-09): removed `trial_extension_offered`, `trial_max_reached` (no extensions),
     `refund_offer` (VT-85 day-39), `refund_completed` (VT-93) — the refund subsystem + trial extensions
     are gone. 30-day flat trial → `trial_ending` warn → subscribe-or-lapse. SIDs retired in Twilio. -->

| `support_resolved` | VT-108 batch-2 · SupportBot resolve (owner) | `HX4a14a1dc0e84beeee383094c5d47942a` | `HXd3a19118d25953cc77ee8915b32099a6` |
| `trial_subscribe_link` | VT-108 batch-2 · trial-end pay link (owner; VT-332 send) | `HX3c61f10c65156d381438c265b09474a9` | `HX3d8bb10b75c83d0ebc9310d66504e729` |
| `dsr_deletion_completed` | VT-108 batch-2 · DSR purge confirmation (customer) | `HXa2aada217c00112c386966f8daa1984c` | `HX60e633af93225f5e46c78203e0b99c44` |
| `breach_notification_owner` | VT-108 batch-2 · breach notice (owner) — incident-use only, ops path, never agent_selectable | `HX269a7f69da791f24b4cee23bd820383e` | `HX1b4a5c64f7f4c3d07c0ba8798fa120bf` |
| `breach_notification_customer` | VT-108 batch-2 · breach notice (customer) — incident-use only, ops path, never agent_selectable | `HXdbf0129d38d60d57b11851d8acf581e6` | `HX48dcbb5f65877f8592296921b3bad100` |

## VT-369 agent surface (Gap-5) — 5 templates, F1 ARMED (VT-383 / CL-438, 2026-06-12)

The first agent-initiated customer-messaging surface (Sales Recovery win-backs + the
owner approval/autonomy loop). **F1 landed 2026-06-12 (CL-438):** Fazal delivered the 10
Meta-approved Twilio Content SIDs (5 templates × en/hi). The VT-383 Content-API canary
(`apps/team-orchestrator/canaries/vt383_f1_content_api.py`) fetched each SID — **every
`meta_status` is `approved`** — and the APPROVED body + per-language `body_sha256` were
recorded as canon in `.viabe/queue/VT-383-canary-results.json`. The bodies below are
**verbatim from that approved canary output** (Fazal edited copy at submission; the
approved body is canon, not earlier drafts). The two `team_winback_*` carry the customer
STOP opt-out line in the FIXED Meta body (canary asserted STOP present, both langs) and
are now `agent_selectable: true`; the three `owner_notification` surfaces are
system-invoked and stay `agent_selectable: false`.

| template | tier | en SID | hi SID |
|---|---|---|---|
| `team_winback_simple` | VT-369 agent surface · customer win-back (category `customer_marketing`, `optout_line: true`, `agent_selectable: true`) | `HX601925a292da89e9d00d3fdf8742f765` | `HX5da4406f8a6691f52555cd179f40be73` |
| `team_winback_offer` | VT-369 agent surface · customer win-back with offer (category `customer_marketing`, `optout_line: true`, `money_bearing: true` — always-confirm floor, never L3 auto-send; `agent_selectable: true`) | `HX637d3dc2969a722f627e0dfd2c166b1e` | `HX9370d1b1a1c917a88ef512b7d545ac46` |
| `team_agent_draft_approval` | VT-369 agent surface · owner L2 approval ask (category `owner_notification`, system-invoked, `agent_selectable: false`) | `HX1fa31e0339d5739d7936e6edf39e08a3` | `HX81929b92dd3a159e920b5eb338700cf8` |
| `team_l3_presend_notice` | VT-369 agent surface · owner L3 pre-send notice, delivery-anchored 2h hold (category `owner_notification`, system-invoked, `agent_selectable: false`) | `HXb114769da63f0c72d4a9f01c2fd0ed80` | `HX8184dfe127d1f5bc124384192a4793be` |
| `team_autonomy_offer` | VT-369 agent surface · owner L3 opt-in offer — C3 consent evidence; body promises the standing "stop" kill keyword (category `owner_notification`, system-invoked, `agent_selectable: false`) | `HX150525f3963603ad00d234bd01b37224` | `HXae12acceccc259235478a7a60c53d628` |

### Approved bodies (verbatim from the VT-383 canary — Meta status `approved`)

All `body_sha256` values below are `sha256(approved_body_utf8)` and are pinned per-language
in `apps/team-orchestrator/config/twilio_templates.yaml` (`body_sha256: {en, hi}`).

#### `team_winback_simple`  *(customer · `customer_marketing` · `optout_line: true` · `agent_selectable: true`)*

- **Variables:** `{{1}}` = customer_name, `{{2}}` = business_name
- **en SID:** `HX601925a292da89e9d00d3fdf8742f765` · **body_sha256:** `15edf62d44cfc28b478a9b589529a9fa6cf0b7367622fbfeb23ab1ad63a4d740`

```
Hi {{1}}, this is a message from {{2}}. We haven't seen you in a while and we'd love to welcome you back — your favourites are waiting for you. Visit us or reply here and we'll help you right away.
Reply STOP to stop receiving these messages.
```

- **hi SID:** `HX5da4406f8a6691f52555cd179f40be73` · **body_sha256:** `d32d3a974cb63b78cb505430cd465f62bed597f02056646c270ba92a47163dc4`

```
नमस्ते {{1}}, यह संदेश {{2}} की ओर से है। आपको काफ़ी समय से नहीं देखा — हमें आपकी कमी महसूस हो रही है। दोबारा पधारें या यहीं जवाब दें, हम तुरंत आपकी मदद करेंगे।
इन संदेशों को रोकने के लिए STOP लिखें।
```

#### `team_winback_offer`  *(customer · `customer_marketing` · `optout_line: true` · `money_bearing: true` · `agent_selectable: true`)*

- **Variables:** `{{1}}` = customer_name, `{{2}}` = business_name, `{{3}}` = offer_description (grounded against the customer fact bundle; validator-enforced)
- **en SID:** `HX637d3dc2969a722f627e0dfd2c166b1e` · **body_sha256:** `b8ecced1091c1ecfdb7ac302ce8ca16fc5ffdaae284841bd08913aab2ec4810e`

```
Hi {{1}}, a special offer from {{2}}: {{3}}. Show this message when you visit, or reply here to know more. We'd love to see you again!
Reply STOP to stop receiving these messages.
```

- **hi SID:** `HX9370d1b1a1c917a88ef512b7d545ac46` · **body_sha256:** `5a31e9acb6e57fd2652a9c94fed76864c00a3b3b9acdb891360b4390ae3c6db9`

```
नमस्ते {{1}}, {{2}} की ओर से आपके लिए एक खास पेशकश: {{3}}। आने पर यह संदेश दिखाएँ या अधिक जानकारी के लिए यहीं जवाब दें। आपके फिर से आने का इंतज़ार रहेगा!
इन संदेशों को रोकने के लिए STOP लिखें।
```

#### `team_agent_draft_approval`  *(owner · `owner_notification` · system-invoked · `agent_selectable: false`)*

- **Variables:** `{{1}}` = owner_name, `{{2}}` = draft_count, `{{3}}` = sample_message (rendered at arm-time from an RLS read of `agent_drafts`; goes into the WhatsApp send ONLY — never into the `pending_approvals` row, no-customer-PII rule)
- **en SID:** `HX1fa31e0339d5739d7936e6edf39e08a3` · **body_sha256:** `5fba29d4eec7937a8510fab43f3ac1a4a07fd3854efcac869d7f935a50c21501`

```
Hi {{1}}, your Viabe assistant has prepared {{2}} customer message(s) for your approval. Sample: "{{3}}"
Reply YES to approve and send, EDIT to change, or NO to reject. Nothing is sent without your approval.
```

- **hi SID:** `HX81929b92dd3a159e920b5eb338700cf8` · **body_sha256:** `94e40feada67567d350e0951901d470df6f272f4bfe3eace89660f9fb5547477`

```
नमस्ते {{1}}, आपके Viabe असिस्टेंट ने आपकी मंज़ूरी के लिए {{2}} ग्राहक संदेश तैयार किए हैं। नमूना: "{{3}}"
भेजने की मंज़ूरी के लिए YES, बदलाव के लिए EDIT, या अस्वीकार करने के लिए NO लिखें। आपकी मंज़ूरी के बिना कुछ भी नहीं भेजा जाएगा।
```

#### `team_l3_presend_notice`  *(owner · `owner_notification` · system-invoked · `agent_selectable: false`)*

- **Variables:** `{{1}}` = owner_name, `{{2}}` = send_count
- **en SID:** `HXb114769da63f0c72d4a9f01c2fd0ed80` · **body_sha256:** `fcafd820af259593814a79eb6f46d40ec09d59157889ede657c97a08abebaca1`

```
Hi {{1}}, under the autonomy you enabled, your Viabe assistant will automatically send {{2}} customer message(s) in 2 hours. No action is needed if you're okay with this. Reply with anything to pause and review them first — replying STOP turns off automatic sending.
```

- **hi SID:** `HX8184dfe127d1f5bc124384192a4793be` · **body_sha256:** `92759384a731b7b7d14d5fcf4943835327769c82bba99d61deddb4f6ee6cdbfc`

```
नमस्ते {{1}}, आपकी दी गई अनुमति के तहत आपका Viabe असिस्टेंट 2 घंटे में {{2}} ग्राहक संदेश अपने आप भेज देगा। सहमत हैं तो कुछ करने की ज़रूरत नहीं। पहले देखना चाहें तो कोई भी जवाब भेजें — भेजना रुक जाएगा। STOP लिखने से ऑटोमैटिक भेजना बंद हो जाएगा।
```

#### `team_autonomy_offer`  *(owner · `owner_notification` · system-invoked · `agent_selectable: false`)*

- **Variables:** `{{1}}` = owner_name, `{{2}}` = streak_count
- **en SID:** `HX150525f3963603ad00d234bd01b37224` · **body_sha256:** `c39414a0c6f0901a785cc7d6d3305076e861cd171005e6412d12e9a9c9491680`

```
Hi {{1}}, you've approved {{2}} messages from your Viabe assistant in a row without changes. Would you like it to send similar routine messages automatically from now on? You'll get a notice 2 hours before every automatic send, and you can always say STOP to turn this off instantly.
Reply ENABLE to turn on automatic sending, or ignore this message to keep approving each one.
```

- **hi SID:** `HXae12acceccc259235478a7a60c53d628` · **body_sha256:** `c036772fdf65196b978bdc6cc4a4bbe8eab907cc75363dcbdf8aa73f7688a66c`

```
नमस्ते {{1}}, आपने अपने Viabe असिस्टेंट के लगातार {{2}} संदेश बिना बदलाव के मंज़ूर किए हैं। क्या आप चाहेंगे कि ऐसे रोज़मर्रा के संदेश अब अपने आप भेजे जाएँ? हर ऑटोमैटिक भेजने से 2 घंटे पहले आपको सूचना मिलेगी, और आप कभी भी STOP कहकर इसे तुरंत बंद कर सकते हैं।
ऑटोमैटिक भेजना चालू करने के लिए ENABLE लिखें, या हर संदेश खुद मंज़ूर करते रहने के लिए इस संदेश को अनदेखा करें।
```

**Draft variable signatures** (yaml `variables:` is the machine mirror; both files update
in lockstep when F1 copy is finalized):

- `team_winback_simple` — `{{1}}` customer_name, `{{2}}` business_name. Fixed body MUST
  contain the customer STOP opt-out line.
- `team_winback_offer` — `{{1}}` customer_name, `{{2}}` business_name, `{{3}}`
  offer_description (grounded against the customer fact bundle; validator-enforced).
  Fixed body MUST contain the customer STOP opt-out line.
- `team_agent_draft_approval` — `{{1}}` owner_name, `{{2}}` draft_count, `{{3}}`
  sample_message (rendered at arm-time from an RLS read of `agent_drafts`; goes into the
  WhatsApp send ONLY — never into the `pending_approvals` row, no-customer-PII rule).
- `team_l3_presend_notice` — `{{1}}` owner_name, `{{2}}` send_count.
- `team_autonomy_offer` — `{{1}}` owner_name, `{{2}}` streak_count.

**Body-hash pinning (DONE at F1 — VT-383, plan §3c):** the Meta-APPROVED content was
fetched once via the Twilio Content API (canary), the opt-out line asserted against the
APPROVED body (not this doc), and a per-language `body_sha256` pinned next to each SID in
`twilio_templates.yaml`. CI now fails on any doc/yaml/Meta drift (the
`templates_registry.canary_load` body_sha256 check + `test_template_registry_gap5.py`
armed-shape pins). The approved-body hashes are recorded above and in
`.viabe/queue/VT-383-canary-results.json` (canon).

**`agent_selectable` note (resolved at F1):** the two `team_winback_*` are now
`agent_selectable: true` — Meta-approved with real SIDs wired, so they correctly enter
`approved_template_names()` / the live VT-45 selectable set. The Gap-5 drafting gate
requires `category: customer_marketing` + `agent_selectable: true` at send time, which
both winbacks now satisfy. The three `owner_notification` entries stay `false` forever
(system-invoked, never agent-chosen).
