# CODEX BRIEF — S2: Abandoned-Checkout Recovery specialist (paste to Codex)

**From Clau, 2026-08-13, under Fazal's grant:** *"assign tasks to Codex… we can get Codex to work on
the next specialist agent as per our list."* Next per the wishlist (Fazal ruling 2026-07-22, sales
first): **S2 — abandoned-checkout recovery. Value 10, complexity 4, the highest-ROI agent for the
launch persona.**

## What you are building

A specialist agent that recovers abandoned Shopify checkouts over WhatsApp for Indian D2C merchants
(₹2–20L/month). 60–80% of checkouts abandon; a consent-clean WhatsApp nudge recovers a meaningful
fraction. **This is a near-clone of Sales Recovery with a different trigger and cohort** — study
`agents/sales_recovery_executor.py` and clone its shape; do not invent a new pattern.

## Scope

1. **Trigger/cohort:** Shopify abandoned-checkout read (API + webhook) → cohort of abandoners with
   phone + consent basis. Read-only integration; follow `integrations/connectors/` patterns.
2. **The consent-surface expansion (S2–S4 shared prerequisite — half the value of this build):**
   a checkout-moment consent capture is a NEW consent basis, distinct from the lapsed-customer
   ledger. Per-purpose consent classes, DPDP-clean, Meta commerce-messaging compliant. Design this
   once so S3/S4 reuse it.
3. **Draft generation** through the existing agent framework (`activation_registry.py` entry,
   prereq registry, thin specialist memory per ARCHITECTURE §0.1.3).
4. **Templates:** draft copy for the out-of-window nudge (en + hi), registered via the template
   registry flow — Meta submission is Fazal's, never yours.
5. **Tests + a canary plan** (Rule 15: the Shopify read needs a real-API canary step, fail-not-skip
   — CC will run it; you write it).

## HARD BOUNDARIES — a PR that crosses any of these gets bounced whole

- **Every send rides the EXISTING rails:** the VT-45 send choke, the frequency gate (VT-741),
  Gate 0 in `agent_send_draft`, `ownership_verified`, Pillar-7 owner approval. **Your agent composes
  drafts; it never sends.** If your design needs a new send path, stop and say so instead.
- **No migrations run, no allocator touched, no merge.** Migration numbers and VT-ids come from CC.
  Ship SQL as a proposal file; CC allocates and applies.
- **No consent inference.** An abandoned checkout is NOT consent to market; the consent capture in
  §2 is what creates the basis. Fail-closed: no basis → no cohort membership.
- **No real sends, no real customer data.** Bogus fixtures only.
- Separate clone; your deliverable is a PR CC reviews at full thorough-review depth.

## Deliverables
PR with: cohort/trigger module · consent-surface design doc + implementation · registry entry ·
draft-generation path · template copy · tests · canary script. Design doc FIRST (1 page, the S2–S4
consent surface especially) — CC reviews the design before you build on it.
