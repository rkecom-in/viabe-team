# Testimonial + logo consent process (VT-98)

Pillar 7: the landing's social-proof section ships with **zero** testimonials/logos/metrics
(`apps/team-web/data/social-proof.json` empty). Real content is added ONLY through this process —
fabricated content is forbidden (PR review enforces).

## Adding a testimonial (post-launch)
1. **Signed owner release** — the owner gives explicit, written consent to publish their quote +
   name + business type + locality (+ optional photo). Store the signed release out-of-band;
   reference it in the PR.
2. **PR** updates `data/social-proof.json` `testimonials[]` (+ uploads the photo asset if any).
   The quote must be the owner's real words; no editing that changes meaning.
3. **Review + Fazal sign-off** (Type-1 governance). Reviewer confirms the release exists and the
   content is real (Pillar 7).

## Adding a customer/press logo
- Brand permission / logo licensing required before adding to `logos[]` / `press[]`. Track the
  permission with the PR. `alt` text = the brand name (a11y).

## Aggregate metrics
- Computed by `apps/team-web/scripts/compute_landing_metrics.py` (post-launch), k-anonymity
  gated at ≥10 tenants — never published below that cohort, never hand-entered.

## Phase 1
No real testimonials/logos/metrics exist yet. This process is scaffolded for Phase 1.5+; the
section shows honest "coming soon / we don't fabricate" placeholders until real content lands.
