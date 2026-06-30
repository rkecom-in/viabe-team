# Latest State Snapshot

**As of:** 2026-07-01. **dev HEAD:** `a41a904` (VT-515 debug/failure log + live VTR feed; `origin/dev` matches). **main HEAD:** UNCHANGED (Fazal-only promotion per CL-432). **BINDING Team go-live: 2026-07-15.**

> Reconciled against `git log origin/dev`, the sprint rows, and the latest Cowork/CC signals (Rule #14). The prior 2026-06-29 snapshot was materially stale: it described the VT-490/491/492 win-back chain, which is superseded. The critical path is now **Fazal's first real signup/onboarding journey on deployed dev** (the public signup form → discovery → GST verify → OTP → account create), plus the **VT-514/515/516 observability** Fazal asked for. Branch is `cc-winback-followups` tracking `origin/dev`.

---

## CRITICAL PATH

**Get Fazal's real signup for "RKeCom Services Pvt Ltd" to create an account end-to-end on deployed dev, with full observability of every failure.**

The deployed-dev signup journey was repaired across this session from Fazal's live runs (each fix proven on the deployed path before claiming, raw-evidence bar):
- **VT-507** — async parallel discovery (LLM + knowyourgst), never blocks the form; 24h **persistent** cache (replaced the in-process dict wiped on redeploy); `entity_discovery_requests` observability. Done.
- **VT-509** — LLM leg structured-only (no garbage "not found" cards); the scraper cache-poison fixed (my VT-507 cache was caching `[]` and serving it 24h — the scraper code always worked); source label genericized to "public records". Done.
- **VT-510** — confirm-seam name-match normalized (the OPC/parens/spacing diff no longer false-rejects an active GSTIN); verify-before-OTP confirmed; GSTIN shown on the card. Done, proven (`gstin_verified` on deployed).
- **VT-512** — the create payload field-mismatch (`verified_gstin` → `gstin`); create was 422-rejecting EVERY signup since `69831e8` (the verified GSTIN never reached the create gate). Done.
- **VT-511** — design pass across all signup screens + 10 screenshots at `apps/team-web/screenshots-vt511/` for eye-review. Done.
- **VT-513** — GSTIN-uniqueness build CANCELLED (a chain shares one GSTIN across stores). **Launch = single-owner-only** (multi-store parked). Cleared Fazal's prior tenant `63211ce5` (269 child rows) so his re-run creates fresh. Done.

Observability (Fazal's ask; **dev-full-parity** — build on dev now, not prod-later):
- **VT-515** — debug/failure log (`debug_events`, mig 146) + a live "Debug / Failures" feed on the Ops Console. `emit_debug_event` is fail-soft + PII-redacted; wired to every signup-path failure incl the silent-degrades. **Proven on deployed dev** (a garbage-name discovery emitted 3 correlated rows). In Review (Cowork audit-after).

## IN FLIGHT (CC)

- **VT-515** — pushed (`a41a904`), mig 146 applied to dev, emit proven on deployed dev. The browser Realtime feed is Fazal/Cowork eye-check on the Ops Console → "Debug / Failures".

## BLOCKED ON

- **Fazal's signup re-run** — the tenant is cleared + all path fixes deployed at `a41a904`; awaiting Fazal to re-run "RKeCom Services Pvt Ltd" (the bottom-left version stamp must read `a41a904`). His re-run is the acceptance for the whole signup chain.
- **`dev -> main`** — VT-231 Mumbai prod cutover + explicit Fazal promotion authorization (CL-432).

## NEXT ACTION

- **CC:** build **VT-514** (Team-Manager AUDIT/trace log — choke-point instrumented through the guarded-tool/rail + reasoning + dispatch layers; the no-orphan-action invariant proving completeness; point-in-time knowledge snapshots; PII by-reference via `pii_redactor`) + **VT-516** (the per-tenant grouped summary-first viewer over the audit+debug logs, reusing the Supabase Realtime stream). Build-now on dev, Cowork audit-after. Reuse the substrate: `pipeline_steps` + `agent_invocation`/`agent_reasoning_step` envelopes, `debug_events` (mig 146), `lib/ops/stream.ts subscribePipelineSteps`. Next migration = **147**.
- **Cowork:** audit-after; pre-check ONLY the audit-log PII boundary (CL-390, by-reference) + verify the no-orphan invariant. Do not block CC (2026-06-28 full-autonomy ruling).
- **Fazal:** re-run the signup; review the VT-511 screenshots + the live Debug feed by eye.

## DO NOT

- Touch `main` without Fazal's explicit promotion instruction (CL-432).
- Re-add a GSTIN/business-uniqueness block — single-owner-only launch, multi-store PARKED (CL-2026-07-01-single-owner-only). Dedup stays whatsapp_number-only.
- Send a real WhatsApp before Fazal's sign-off. Dev = mock-off + `DEV_SEND_ALLOWLIST` (Fazal's 3 numbers real, rest mocked); NEVER drive the live-number tenant. Never fabricate a phone number (only Fazal-provided enter a real path).
- Log raw PII in the audit log — by-reference + structured facts via `pii_redactor` (CL-390); the VT-514 PII boundary is the one thing Cowork pre-checks.
- Build parallel logging — extend the existing envelopes (`pipeline_steps`/`agent_invocation`/`agent_reasoning_step`/`debug_events`), don't duplicate.
- Build on a git worktree / fan out parallel writers on the shared tree — serial on the one tree (read-only design/audit can parallel).
- Trust this snapshot's HEAD or in-flight claims without reconciling `origin/dev`, sprint rows, and current signals (Rule #14).
