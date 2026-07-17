# TARGET CHECKLIST — achieved vs pending (single page, CC-maintained)

> **Maintenance rule (CC, standing):** update at every phase transition (land/push/gate-result),
> same moment as the Cowork signal. This is the OBJECTIVE-level view; per-row detail lives in
> `.viabe/sprint/VT-*.md`. Reconcile against `git log` before trusting (Rule #14).
> Last updated: **2026-07-18 ~04:15 IST** · dev HEAD at update: 2656d0e (+2 commits pushing).

## ✅ ACHIEVED (proven, on origin/dev)

### Trust objective (.viabe/manager-objective.md)
- [x] **Tier-1 trust-breakers = 0** — HEAD-authoritative (ack-fix 555c128 confirmed no-regression).
- [x] Money authority: DB = sole Tier-1 source + deterministic claim-binding (CL-2026-07-16).

### Agent Capability Framework (ARCHITECTURE.md, ratified)
- [x] **VT-101 migration complete on dev** — SR + Integration live through agent_framework behind
      TEAM_SR_VIA_FRAMEWORK + TEAM_INTEGRATION_VIA_FRAMEWORK (both ON dev; prod unset/Fazal-only,
      rides VT-231 Step 4.2). Flag-off = byte-identical rollback. §7.3 DB-inversion = the only
      deferred remainder (LAST, by design).
- [x] **VT-669** tool catalog (74 surfaces, drift-guarded) + required_tools manifests + 9th
      conformance sufficiency check + generated TOOLS.md + fail-loud gap gate.
- [x] **All 4 capability gaps CLOSED same-day** — VT-672 escalate · VT-673 read_active_plan ·
      VT-674 read_agent_memory · VT-675 richer-reads promoted (resolve-first IDOR wrappers).
      `check_capability_gaps.py` GREEN; registry auto-re-arms on the next named hole.
- [x] ARCHITECTURE.md fact-checked vs tree (5 claims verified; 2 stale sections corrected).

### Owner-journey defects (this week's batch)
- [x] **VT-666** send-token misroute · **VT-667 core** creative brief → draft (fix-4: correction =
      REVISION) · **VT-668** orphaned-approval silent no-op (unit/DB proven) · **VT-670**
      already-SENT duplicate re-arm guard (warn-once) · **VT-676 build+mock-e2e** customer-list CSV
      attachment (R7 front door + D3 ride-along; PII rails; mock e2e PASSED on deployed dev).

### LANGUAGE (VT-677, design RATIFIED, phases 1-3b BUILT)
- [x] Canonical owner_locale module (en|hinglish|hi) + D1 template-register mapping.
- [x] 2 live D1 violations fixed (_floor_language Devanagari-to-hinglish; monthly_report raw flow).
- [x] Triage language enum + Devanagari-override observed persist (D2-clean).
- [x] Signup seeds OBSERVED column; set_language_preference verbal-override tool (VT-268 reviewed).
- [x] Brain context line + ONE-sentence ambiguous-turn fallback + campaign-conflation guard.

## 🔄 IN FLIGHT (CC)
- [ ] **VT-677 ×3 full-journey re-drive** on deployed dev — the prompt-diff gate; row stays open
      until clean vs baseline. (Kick after the current push deploys.)
- [ ] Final VT-677/phase-3 push landing (pre-push suite running).

## ⏳ PENDING — CC queue (in order)
- [ ] **VT-671** — SR turn latency + j02 persona-variance tail (measurement-heavy).
- [ ] **#84** — Batch-API adoption (~50% off the measurement pipeline; cost-opt).
- [ ] VT-668 follow-up cosmetic (redrive-actor churn one-liner, next manager-loop touch).

## 🧍 PENDING — FAZAL (blocking their rows)
- [ ] **VT-676 live canary** — WhatsApp "Send me my customer list" from your phone → file opens +
      URL dies ≤300s. (Sealed Twilio token = the deployed service must originate; your message is
      the trigger.) Closes VT-676.
- [ ] **VT-668 live re-arm** — "Run my win-back campaign again" → approve → fixed consumer executes
      → true-count confirm. Closes VT-668's live proof.
- [ ] **Meta hi-Latn template variants** — register with Meta (D1); EN fallback live until approved.
- [ ] **Track B (#117)** — live hands-on Manager drive (needs you at the wheel).
- [ ] **VT-678 decision** — co-present ask one-turn routing (rostered, held on your call).
- [ ] **VT-231 prod cutover** (when you call it) — carries the framework-flag promotion (Step 4.2).

## 🅿 PARKED (deliberate, don't re-litigate)
- §7.3 DB-inversion (LAST, ratified) · founder-journey sim (post-gate, 3 Fazal decisions on reopen)
- RAG wire-up (post-trust-floor) · general numeric emission gate (shadow-first, VT-2 risk)
- pre-push flaky pair (test_sr_loop_e2e owner-notify race + test_transitions kill-window — roster
  a hardening row if they trip again)
