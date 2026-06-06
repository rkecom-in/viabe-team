---
vt_id: VT-9
title: VT-OwnerSurface — Twilio, sign-up, weekly approval, refund convo, portal
status: Done
# VT-9 DONE (Sprint-8 close-out, Cowork ruling 20260606T141500Z): all OwnerSurface children
# shipped — VT-81..88 Done; VT-157 (consent-capture) CLOSED superseded-by VT-303+VT-270 (the
# capture/durable/refusal/revocation mechanism is live; final consent COPY tracked as VT-272,
# counsel-gated). Flagged for Fazal's ratification in Cowork's close report (DPDP adjacency).
priority: High
sprint: Sprint 8 - Owner Surface & Billing
type: Feature
area: [Owner Surface, Frontend]
assignee: Clau
parent:
sub_items: [VT-81, VT-82, VT-83, VT-84, VT-85, VT-86, VT-87, VT-88, VT-157]
exec_order: 1
branch: "feat/vt-owner-surface"
version: "v1.0"
notion_legacy_id: 356387c2-cc5a-81a2-86f9-ffb0b60dffda
created: 2026-05-04
last_updated: 2026-05-25T03:45:00+05:30
---

## Why this parent exists
The owner is the only human in the loop. WhatsApp is the primary surface; the read-only portal at [viabe.ai/team](http://viabe.ai/team) is secondary. Every UX decision here either earns the owner's trust to delegate work to the agent, or fails to. Reports product had a much simpler customer surface (one PDF delivered, done). Team is a recurring relationship: weekly approval prompts, ad-hoc owner messages, day-39 refund conversations, monthly impact reports, support escalations. The surface is where the product feels reliable or feels broken.
This parent owns every owner-facing touchpoint. The orchestrator (VT-3) routes the work; this parent owns the message templates, the conversation flows, the portal screens, and the escalation paths. The refund-conversation engine (VT-9.5) is particularly delicate — it's where day-39 evaluations are communicated honestly to a churning subscriber.
## What this parent owns
1. Twilio inbound webhook hardening (signature verification, replay protection, rate limiting). The webhook itself exists from VT-3.3; this is hardening on top.
2. Sign-up flow: WhatsApp + landing-page entry points. Owner identity capture, KYC handoff (vendor TBD in VT-13.7), tenant provisioning.
3. Weekly approval UX: agent proposes a campaign, owner approves/edits/rejects via WhatsApp message templates. Approval is structural — agent does not send unapproved campaigns.
4. Edge-case interaction handlers: exclusion requests ("don't message customer X"), ad-hoc requests ("send a Diwali blast"), status checks ("how's last week's campaign doing?"), template-error fallbacks (Meta template rejected mid-campaign).
5. Refund-conversation engine: triggered by day-39 evaluation (VT-10.4) when ARRR < 2x cumulative fees. Proactive outreach with honest framing, no upsell pressure.
6. Monthly impact report: PDF generated and emailed via Resend. Shows ARRR, campaigns, attribution, what changed month-over-month. Re-uses Reports' PDF generator infrastructure where applicable.
7. Read-only portal at [viabe.ai/team](http://viabe.ai/team): dashboard view of campaigns, customers, attribution, billing. No writes from the portal — all writes are owner-WhatsApp-mediated.
8. SupportBot escalation fallback: if the agent gets confused or hits a hard limit, owner gets a clear message + escalation path to Fazal (Phase 1 has no human support team).
## Architectural rules binding every subtask
- Pillar 7 (owner is source of truth): every conversational flow respects this. Reconstitution after opt-out goes through owner verification (VT-8.5). Customer corrections go through owner-mediated DSR (VT-8.6).
- Pillar 8 (no patchwork): message templates are versioned and testable. Edge cases are handled by explicit handlers, not by string-matching at the dispatch layer.
- Pillar 3 (tenant isolation): the portal cannot show one owner another owner's data. Authentication and tenant scoping are mandatory on every screen.
- All Meta WhatsApp messages outside the 24-hour window MUST use approved templates (VT-13.3). Free-form messages outside that window are blocked at the tool level (VT-5.6).
- Refund conversations are honest. The engine does not attempt retention through pressure or guilt. Day-39 evaluation results are presented as data; the customer chooses.
- Monthly impact reports never overstate ARRR. If attribution is uncertain, report the uncertainty.
- The portal is read-only in Phase 1. Adding write capability requires Type 2 governance.
## Subtasks under this parent
1. **VT-9.1** — Twilio inbound webhook hardening.
2. **VT-9.2** — Sign-up flow (WhatsApp + landing).
3. **VT-9.3** — Weekly approval UX.
4. **VT-9.4** — Edge-case interactions (exclusion, ad-hoc, status, template-error).
5. **VT-9.5** — Refund-conversation engine (day-39 proactive).
6. **VT-9.6** — Monthly impact report PDF + Resend.
7. **VT-9.7** — Read-only portal at [viabe.ai/team](http://viabe.ai/team).
8. **VT-9.8** — SupportBot escalation fallback.
## Definition of done
- All 8 subtasks Done.
- Synthetic owner journey: sign up → onboard one ingestion method → first campaign approved via weekly UX → attribution closed → monthly report received → support question handled.
- Edge-case suite: each of the four edge-case categories has an integration test that drives the flow end-to-end.
- Refund-conversation engine: synthetic day-39 < 2x case triggers honest outreach with refund offer. Owner choice (refund vs continue) recorded structurally.
- Monthly impact report renders for a synthetic subscriber via Resend.
- Portal: tenant A cannot see tenant B's data (cross-tenant attack test on every page).
- All Meta templates used outside 24-hour window are pre-approved (no free-form in send_whatsapp_message outside window).
## Out of scope
- Twilio inbound webhook itself (VT-3.3) — hardening here, not creation.
- Day-39 evaluation logic (VT-10.4) — this parent owns the conversation engine that follows the evaluation, not the evaluation itself.
- Razorpay billing (VT-10).
- Landing page design (VT-11).
- KYC vendor integration (VT-13.7).
- Meta template approvals (VT-13.3).
## Branch convention
- Parent branch: `feat/vt-owner-surface`.
- Subtask branches: `feat/vt-owner-<short>` (e.g. `feat/vt-owner-weekly-approval`, `feat/vt-owner-refund-engine`).
- PR title format: `<type>(owner-surface): <description> (VT-9.N)`.
- Reviewers: CoderC + Frontend Engineer for portal screens; CoderX must review the refund-conversation engine; Fazal personally approves refund-conversation copy and weekly-approval template wording.
- Merge target: `dev`.

## Status history
- 2026-05-25 03:45 IST: migrated from Notion (notion_legacy_id: 356387c2-cc5a-81a2-86f9-ffb0b60dffda)
