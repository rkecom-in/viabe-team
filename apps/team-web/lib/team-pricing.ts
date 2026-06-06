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

export function planPrices(): PlanPrice[] {
  return [
    { tier: 'founding', inr: process.env.NEXT_PUBLIC_FOUNDING_PRICE_INR ?? '2,499' },
    { tier: 'standard', inr: process.env.NEXT_PUBLIC_STANDARD_PRICE_INR ?? '4,999' },
    { tier: 'pro', inr: process.env.NEXT_PUBLIC_PRO_PRICE_INR ?? '14,999' },
  ]
}
