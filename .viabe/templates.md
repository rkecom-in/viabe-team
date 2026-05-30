# Viabe Team — WhatsApp Template Registry

**Source of truth** for Meta-approved WhatsApp templates with their Twilio Content SIDs. **Cowork-managed.** This is the authoritative map from `template_name` → `twilio_content_sid` + body text that the runtime will reference when sending WhatsApp messages.

**Owner:** Cowork. Established 2026-05-26 IST as the HUMAN-readable companion to `apps/team-orchestrator/config/twilio_templates.yaml` (which already existed since VT-3.3c shipped). The yaml is the runtime source of truth (name → SID for code); this markdown file adds the bodies, variable signatures, tier categorization, and approval-status notes that the yaml deliberately doesn't carry. When templates are added or rotated, BOTH files update in lockstep.

**VT-163 update (2026-05-31):** Variable signatures are now ALSO machine-readable in `twilio_templates.yaml` under the `variables:` key (ordered list of snake_case param names; index i = positional `{{i+1}}`). This file remains the human-readable companion with message body copy. The yaml `variables:` lists are sourced from the signatures documented here. They update in lockstep — when a template is added or its signature changes, update both.

**Correction to my earlier framing 2026-05-26 17:05 IST:** I initially wrote this file claiming the 8 SIDs had "no repo presence" — that was a grep-mistake on my side; the yaml has had them since VT-3.3c. The actual gap is that the COPY (message text) + variable signatures + tier categorization aren't in either the yaml OR the Meta/Twilio consoles in a way that Cowork or Claude Code can reference at brief-time. THIS file closes THAT gap.

**Source authority:** Meta-approval status owned by Fazal (vendor relationship). Twilio Content SIDs are the canonical runtime identifier — assigned when the template is uploaded to Twilio Content API after Meta approval. The `template_name` is the human-readable handle used in code.

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

### `team_welcome`

- **Twilio Content SID:** `HX1b66c0daaa52dc0b8575e50eebadfdd1`
- **Tier:** Tier-A (launch-blocking, onboarding flow)
- **Variables:** `{{1}}` = owner name, `{{2}}` = trial end date

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
