# SPECIALIST AGENT WISHLIST — value × complexity (standing roster)

> Owner: Clau (Architect). Purpose: pick the RIGHT next agent, not the loudest.
> **Fazal ruling 2026-07-22 (Standing): SALES AGENTS FIRST — revenue impact positions us in
> the market — then Marketing/Branding/Reputation. Compliance: build-track continues (Codex),
> tenant-facing activation deferred (gates on O11 judgment-eval + stable launch cohort).**
> Value (1-10): owner revenue/pain impact × willingness-to-pay × frequency, FOR THE LAUNCH
> PERSONA (online-presence D2C/Shopify). Complexity (1-10, lower = easier): catalog/tool reuse,
> new connectors, new trust surface, measurement. Last scored: 2026-07-22.

## THE SALES CLUSTER (build order per the ruling)

| # | Agent | Value | Cmplx | Notes |
|---|-------|-------|-------|-------|
| S1 | Sales Recovery (win-back) | 9 | — | **LIVE** — the proven pattern the rest clone |
| S2 | **Abandoned-checkout recovery** | 10 | 4 | **NEXT.** 60–80% of D2C checkouts abandon; WhatsApp recovery is a proven category. Shopify abandoned-checkout data + existing send rails + approval gates = near-clone of SR with a different trigger/cohort. The single highest-ROI agent for the persona. |
| S3 | Repeat-purchase / replenishment | 8 | 4 | Purchase-cycle detection ("30-day customers due to reorder") → nudge campaigns. Same rails; new cohort logic. Consumables/beauty/food persona fit is excellent. |
| S4 | COD confirmation / RTO reduction | 9 | 5 | RTO (20–30% on COD) is Indian D2C's #1 money leak; WhatsApp order-confirmation flows measurably cut it. Customer-facing but TEMPLATED/deterministic (not conversational) — much lower trust risk than CS-inbound. Needs order webhooks + templates. |
| S5 | Upsell / cross-sell | 7 | 5 | Post-purchase recommendations; rides S3's triggers. Content quality is the bar (O11 helps). |
| S6 | Lead capture & follow-up | 8* | 7 | Inquiry → qualify → follow-up-to-close. Huge for services/B2B (*persona-dependent, like Collections); touches the customer-CONVERSATION surface — sequence behind the CS-inbound trust bar, not before. |

**Shared prerequisite for S2–S4 (tools-layer, build once):** consent-surface expansion —
per-purpose consent classes + checkout/order-moment opt-in capture (a customer who abandoned a
cart or placed a COD order is a NEW consent basis, distinct from the lapsed-customer ledger).
DPDP + Meta commerce-messaging rules apply. This is ONE tools/rails build that unlocks three
agents — roster it with S2.

## MARKETING / BRAND CLUSTER (second wave, per ruling)

| # | Agent | Value | Cmplx | Notes |
|---|-------|-------|-------|-------|
| M1 | Marketing / Campaigns | 9 | 4 | Promote from advisory shelf; reuses SR's entire surface + VT-667 brief pipeline; j02 already tests it. Festival/launch/restock/segment offers. |
| M2 | Reputation / Reviews | 7 | 4 | Post-purchase review asks (social proof → sales) + GBP review responses (apify_gbp read path exists; posting needs OAuth). |
| M3 | Branding / Content | 6 | 5 | Product descriptions, social drafts, catalog copy. Drafts-only (no send risk); quality bar needs O11. |

## EVERYTHING ELSE (hold their positions)

| Agent | Value | Cmplx | Status |
|-------|-------|-------|--------|
| Compliance — GSTR readiness | 7 | 3 | Codex builds (platform proof); tenant activation DEFERRED per ruling |
| Accounting prep (CA packs) | 6 | 3 | Cheap + sticky; bundle-mate when convenient |
| Collections / payment reminders | 7* | 4 | Persona-dependent; re-score post-launch |
| Inventory / reorder signals | 7 | 6 | Shopify data exists; medium-term |
| Customer-Service Inbound | 9 | 8 | HELD — biggest prize, new trust surface; after graduation |
| Online-presence / catalog quality | 6 | 6 | Wishlist |
| Cost-Opt / Tech | 4 | 5 | Stay advisory tools (VT-604) |

## Recommended build order (Clau, under the Sales-first ruling)
1. **S2 Abandoned-checkout recovery** + the consent-surface tools build → 2. **M1 Marketing**
   (near-free promote; can overlap S2 since surfaces are shared) → 3. **S3 Replenishment** →
   4. **S4 COD/RTO** → 5. **M2 Reputation** → then re-score with real cohort data.
   Every agent enters via the ACF promotion bar (activation + durable tasks + gated effect
   tools + required-tools manifest + brief). The Sales cluster narrative for market/investors:
   "every agent in the Sales suite provably adds revenue — win-back, cart recovery,
   replenishment, RTO reduction — each ₹5,000/month, each attributable in rupees."
