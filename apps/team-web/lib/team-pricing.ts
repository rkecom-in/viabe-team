/**
 * VT-95 — plan DISPLAY prices for the landing page, in ₹ (rupees), sourced from config/env
 * (Pillar 7: prices are NEVER hardcoded as a literal in the page). The authoritative source is
 * the orchestrator's config/plans.yaml (paise); these NEXT_PUBLIC_* envs mirror the rupee
 * display value. The `gate-no-price-literals` gate greps the paise literals (not these rupee
 * values) — and these live in one config helper, not scattered across the JSX.
 */
export type PlanTier = 'founding' | 'standard' | 'pro'

export interface PlanPrice {
  tier: PlanTier
  /** ₹ rupees, formatted (Indian grouping), e.g. "2,499". */
  inr: string
}

/**
 * VT-429 — the OWNER-FACING offered-tiers allowlist (mirror of the orchestrator's authoritative
 * server-side gate in config/plans.yaml `offered_tiers`). This drives which plan cards render on
 * the marketing/plan-selection surface; the LOAD-BEARING gate is still the orchestrator
 * `assert_tier_offered` server-side check (a hidden card is presentation; the server is the
 * money authority). DUPLICATION NOTE: team-web is a separate app/deploy and does NOT read the
 * orchestrator's plans.yaml, so the launch policy is expressed here independently via env
 * (`NEXT_PUBLIC_OFFERED_TIERS`, comma-separated), exactly as prices already mirror config via env.
 *
 * FAIL-CLOSED, matching the server: an ABSENT/blank/whitespace-only `NEXT_PUBLIC_OFFERED_TIERS`
 * means "offer NOTHING" → no cards render. It NEVER defaults to offer-all. At launch the env is
 * set to `standard` (Standard-only); widening = add a tier to the env list (and the server list).
 */
export function offeredTiers(): Set<PlanTier> {
  const raw = process.env.NEXT_PUBLIC_OFFERED_TIERS ?? ''
  const known: PlanTier[] = ['founding', 'standard', 'pro']
  const requested = new Set(
    raw
      .split(',')
      .map((t) => t.trim())
      .filter((t) => t.length > 0),
  )
  // Intersect with the KNOWN tiers (an unknown token in the env can't conjure a card).
  return new Set(known.filter((t) => requested.has(t)))
}

/** All DEFINED plans with their display price. Internal — the offered filter is applied by
 * {@link planPrices}; founding + pro stay defined here so re-offering is a one-env-edit. */
function allPlanPrices(): PlanPrice[] {
  return [
    { tier: 'founding', inr: process.env.NEXT_PUBLIC_FOUNDING_PRICE_INR ?? '2,499' },
    { tier: 'standard', inr: process.env.NEXT_PUBLIC_STANDARD_PRICE_INR ?? '4,999' },
    { tier: 'pro', inr: process.env.NEXT_PUBLIC_PRO_PRICE_INR ?? '14,999' },
  ]
}

/**
 * The plan cards to PRESENT to an owner — only the offered tiers (VT-429). Founding/Pro stay
 * defined (in {@link allPlanPrices}) but are filtered out unless listed in `NEXT_PUBLIC_OFFERED_TIERS`.
 * Fail-closed: an empty offered set → an empty list (no cards), never all plans.
 */
export function planPrices(): PlanPrice[] {
  const offered = offeredTiers()
  return allPlanPrices().filter((p) => offered.has(p.tier))
}
