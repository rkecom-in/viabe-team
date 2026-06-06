/** VT-100 — anonymous experiment counters. NO PII: only counts of (experimentId, variant,
 * exposure|conversion) — never an IP, never a visitor/tracking ID (Pillar 7).
 *
 * Phase 1 is a NO-OP sink: the analytics backend (Plausible-or-similar, server-side counts) is
 * wired post-launch. The call sites + the typed surface exist now so experiments can be measured
 * the moment the backend lands — without scattering tracking through components later (Pillar 8).
 */

export type ConversionType = 'cta_click' | 'signup_started' | 'signup_completed'

/** Fires on render (server-side) — counts a variant exposure. Phase 1: no-op. */
export function trackExperimentExposure(experimentId: string, variant: string): void {
  // Phase 1: intentionally no backend. Only (experimentId, variant) would ever be sent — no PII.
  void experimentId
  void variant
}

/** Fires on a conversion (CTA click / signup). Phase 1: no-op. */
export function trackExperimentConversion(
  experimentId: string,
  variant: string,
  conversionType: ConversionType,
): void {
  void experimentId
  void variant
  void conversionType
}
