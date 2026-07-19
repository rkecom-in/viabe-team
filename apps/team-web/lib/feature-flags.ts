/**
 * VT-448 — signup-flow feature flags. Two identify affordances are PARKED behind flags (default OFF,
 * Fazal 2026-06-26): the Sandbox MCA/PAN providers are gov-side unreliable (504s), so the PAN→GSTIN
 * identify and the DIN-KYC ownership affordance are gated OFF rather than deleted. MANUAL GSTIN entry
 * is the primary identify; public-number OTP (Twilio, reliable) is the only ownership proof.
 *
 * Both flags are read ONCE from build-time `NEXT_PUBLIC_*` env. A single source so the entity-match
 * step and the ownership step agree (no per-component drift). Flip a flag back to 'true' when a
 * reliable provider lands — the gated code stays intact behind these booleans, nothing was removed.
 *
 * Default-OFF semantics: only the literal string 'true' enables a flag. Absent / '' / 'false' / any
 * other value → false. NEXT_PUBLIC_* is inlined at build time, so this is fixed per build (no runtime
 * hydration mismatch — same value server + client).
 */

/** PAN→GSTIN identify + the registry-CIN "is this your company?" confirm. OFF → manual GSTIN is the
 *  primary identify and no CIN-confirm affordance is shown (the orchestrator's MCA enrich is parked). */
export const PAN_IDENTIFY_ENABLED = process.env.NEXT_PUBLIC_ENABLE_PAN_IDENTIFY === 'true'

/** The "verify with your DIN instead" ownership affordance. OFF → ownership is public-number OTP ONLY. */
export const DIN_KYC_ENABLED = process.env.NEXT_PUBLIC_ENABLE_DIN_KYC === 'true'

/**
 * The entity-match step's PRIMARY identify path when the owner taps the bottom CTA / has no listed
 * candidate to one-tap-confirm. With PAN identify OFF (default) the primary path is MANUAL GSTIN
 * entry; with it ON, the PAN-entry screen. Pure so the component + the test agree on the gated path.
 */
export function primaryIdentifyStep(
  panEnabled: boolean = PAN_IDENTIFY_ENABLED,
): 'pan_entry' | 'manual_gstin' {
  return panEnabled ? 'pan_entry' : 'manual_gstin'
}
