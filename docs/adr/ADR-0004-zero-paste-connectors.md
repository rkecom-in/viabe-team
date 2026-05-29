# ADR-0004: Zero-manual-paste connectors after OAuth (Apps Script abandoned)

**Status:** Accepted

## Context

VT-207 PR-1 shipped the Google Sheet connector with an Apps Script paste flow: owner OAuths, orchestrator generates an Apps Script body, owner pastes into Sheet → Extensions → Apps Script, saves, adds an `onEdit` trigger. The substrate works; the canary passes; HMAC verification is correct.

VT-212 manual walk (2026-05-29) surfaced that this flow is customer-hostile. The target persona is a Tier-2/3 Indian SMB owner — a salon owner, restaurant owner, retail-shop owner — not a developer. Asking them to paste code into Apps Script + add a trigger is structurally wrong, not just polish-needed.

Fazal raised this directly: "We do both Options. Option B becomes primary and Option A works as a backup plan" — meaning the Drive Push Notification path (Option B) becomes the primary, and the manual Apps Script approach (Option A) is dropped from new onboarding.

## Considered Options

- **A.** Keep Apps Script paste flow for cost/simplicity — rejected; customer-hostile
- **B.** Replace with Drive Push Notifications primary + 10-minute polling fallback (chosen)
- **C.** Both flows side-by-side with feature flag — rejected; doubles the substrate without resolving the customer issue

## Decision

**B.** All Integration Agent connectors MUST be zero-manual-paste after OAuth. This is codified as CL-421 (Standing, Fazal-locked, 2026-05-29):

> "All Integration Agent connectors MUST be zero-manual-paste after OAuth. No Apps Script paste, no copy-paste secrets, no developer-shaped setup steps. OAuth grant + auto-configuration via vendor API is the only acceptable customer-facing flow."

Sheet connector pivots to Drive Push Notifications (Files.watch) primary + 10-minute polling fallback (VT-222). Shopify connector (VT-208 / VT-213) already conforms — Custom Apps OAuth + auto webhook subscriptions. Apps Script substrate (`setup_push`, `apps_script_template.render_apps_script`) marked deprecated, kept for backward compatibility while existing Apps-Script-onboarded tenants migrate.

## Consequences

- (+) Onboarding shrinks from ~5 steps to ~2 (consent + done)
- (+) Operator burden reduced (no debugging owners' Apps Script paste errors)
- (+) Real-time push (Drive Files.watch) is strictly better than polling-only Apps Script triggers
- (+) Standing decision applies to future connectors (Stripe, Razorpay, etc.) — design constraint at brief time
- (−) Channel renewal scheduler (every 6h) adds operational complexity (worth it)
- (−) Tenants onboarded pre-VT-222 with only `spreadsheets.readonly` scope can't auto-register Drive Push — fall back to polling-only silently; opt-in re-OAuth grants the new scope
- (−) Polling-only fallback adds up to 10-min ingestion latency vs sub-second push

## References

- CL-421 (Standing, Fazal-locked, 2026-05-29 — zero-manual-paste connectors)
- VT-207 PR-1 (original Apps Script substrate)
- VT-212 manual walk (empirical trigger)
- VT-222 (Drive Push redesign)
- VT-208 / VT-213 (Shopify conforms by default)
- docs/clau/sheet-integration-runbook.md
