# Latest State Snapshot

**As of:** 2026-07-21 (CC-refreshed at session close — Rule #14). **dev HEAD:** `eea4a17`+ (reconcile `git log origin/dev` first, always). **main HEAD:** `255344c` — **VT-231 PROMOTION EXECUTED; PROD IS LIVE.**

> Reconciled against `git log`, `.viabe/sprint/VT-683.md` + `VT-691.md` (the two open builds), and the `.running/to-cowork/` resume signal. The prior 2026-07-18 snapshot ("promote pending") is superseded — promotion landed, prod deployed on Mumbai, and prod launch-hardening is essentially done.

## CRITICAL PATH
**Prod is live + hardened; the inbound WhatsApp transport is PROVEN end-to-end** (Twilio prod number → Messaging Service → prod team-web `viabe-team-web.vercel.app` signature-validate → forward → orchestrator `/api/orchestrator/twilio-ingress` → 200). The last broken wire (Messaging Service inbound URL was pointing at DEV) is fixed. **Onboarding just needs a tenant** — a cold "Hi" from an un-onboarded number correctly hits `unknown_sender`. First-customer path: temp `ENABLE_PUBLIC_SIGNUP=true` (page: number→OTP→tenant→welcome→journey) OR ops-console create. Then the two open builds below.

## IN FLIGHT (CC) — both are FRESH-SESSION builds, fully designed on their rows
- **VT-683 P2c** (money-path, Pillar-7 surgery): reroute `arm_pause_request`'s load-bearing approval TEMPLATE send → INTERACTIVE in-session message when session_open (template = out-of-window belt until P3/P4). CRITICAL: a session-message button-tap must resolve the SAME `pending_approvals` row (else the VT-615 dropped-campaign class returns). POINT A (ruled): move `pending_approvals.timeout_at` off arm → onto delivery. P2a (mig 178 `owner_comms_queue` + CRUD) + P2b (`owner_comms_drainer.drain_one`) ALREADY LANDED on dev, tested. Adversarial-verify the resolution path + ×3 gate. Full seam design on the VT-683 row.
- **VT-691** WhatsApp-initiated signup: `unknown_sender` inbound → welcome + CONSENT request → on consent (LLM-primary intent) → `create_signup_tenant(consent=True, source='whatsapp')` NO OTP (WhatsApp = Meta phone-verified) → journey. NON-NEGOTIABLE: consent NOT skipped (DPDP); tenant only after consent; abuse flag + cooldown. Full design on the VT-691 row.

## BLOCKED ON / NEXT ACTION (Fazal)
- **Onboarding smoke front-door choice** (temp-enable public signup vs ops-console create) to run a real prod journey — the transport is proven; journey is dev-gate-proven (30/30).
- Kick the two builds in FRESH sessions ("continue VT-683 P2c" / "build VT-691") — money/consent-adjacent, deserve clean context + a gate.
- RZP plan IDs (at payment/subscription finalize) · P3 wake-up loop needs point B (merge `team_reengage`) · P4 template whitelist enforce (after P2c/P3).
- Optional hygiene: rotate the dev Honeycomb ingest key + dev Vercel protection-bypass token (both pasted into chat this session).

## DONE THIS SESSION (durable on dev/prod)
Promotion executed (#526, main=255344c) · prod boot clean on Mumbai · prod env hardened (Supabase Mumbai SECRET_KEY + 2 buckets, Sandbox/Resend/Shopify/Honeycomb keys, consent-versions blank, signup false, framework flags stay =1) · GEMINI rename · boot-conformance enforcement · api.viabe.ai OAuth (canary passed) · WhatsApp sender split (dev +18704122234 / prod +918108084223) · SUPABASE_SERVICE_KEY→SECRET_KEY (one name) · **VT-690 Honeycomb observability** (backend swap + tm_audit-reasoning-on-trace, both proven on dev) · VT-687 (j05 walker ack) · prod inbound transport wired + proven.

## DO NOT
Trust this snapshot's HEAD without `git log` · touch `main` without Fazal's word (Pillar 7) · rush the VT-683 P2c money-path surgery (dropped-campaign class — do it in a fresh session with adversarial-verify + gate) · skip DPDP consent on VT-691 · send synthetic inbounds to real tenants · bend correctness gates.
