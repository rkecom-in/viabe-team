# VT-101 agent-framework migration — morning report (single source of truth)

**Author:** Claude Code (autonomous, overnight 2026-07-16→17)
**Grant:** Fazal RATIFIED 21:22 (Cowork relay 20260716T2122Z) — close re-scoped VT-101 onto CANONICAL `docs/agent-framework/ARCHITECTURE.md`.
**Ledger:** `CL-2026-07-16-arch-ratified-migration`.
**HEAD on dev:** `e8f169a` — **pushed + deployed green on Railway dev (deploy SUCCESS).**
**Staged plan (durable spine):** `.viabe/sprint/vt101-migration-staged-build.md`.

---

## TL;DR (UPDATED 2026-07-17 ~07:45 IST — you lifted the 3am rail; I resumed + landed the SR cutover)

- **LANDED + VALIDATED:** additive Stages 0–2 (inert foundation) **AND** Stage 3(a)+(b) — the **live SR cutover**, now running on dev behind `TEAM_SR_VIA_FRAMEWORK=1` (prod flag-OFF). HEAD dev = `c7fff55`, deployed SUCCESS.
- **SR cutover VALIDATED on deployed dev → HELD:** the coordinator SR **executor** arms through the `CoordinatorAgentAdapter` (awaiting_approval, **0 sends**, no exception); the SR **proposer** journey j01 win-back is CLEAN (4/4); full j01–j10 Tier-2 100%. The SR cutover introduced **ZERO deterministic breakers**.
- **The gate premise changed** (and I surfaced it, didn't silently reinterpret): your re-baseline "confirm Tier-1=0" actually showed **Tier-1 = 1 — a PRE-EXISTING marketing bug (VT-666, j02), reproduced ×2, orthogonal to SR**. So I ran a **delta gate** (cutover must add no new breaker vs baseline). It passed: the only post-cutover extra (j09) re-drove ×2 CLEAN = variance, not cutover-caused (the code delta is SR-only).
- **REMAINING:** Stage 3(c) integration-brain dissolution (deepest surgery — separate validated push) + Stage 4 sign-off. **§7.3 DB-inversion** stays deferred (your call).
- **NEW finding rostered:** **VT-666** — the manager serves the generic onboarding menu instead of answering/honestly-declining a specific in-session ask (j02 campaign-create + j09 per-store breakdown). Pre-existing, orthogonal to the migration, own row + fix.

**Net:** the SR half of the migration (proposer + executor) is LIVE on dev through the framework contract and validated — no money-path regression. The integration half (3c) is the one remaining build.

---

## What landed (each commit — additive, inert unless noted)

### Stage 0 — Sales Recovery as a framework module — `e54f74c` (VT-659-build, §7.2)
`agent_framework/modules/sales_recovery_module.py` — a dual-role `{PROPOSER, EXECUTOR}` **thin adapter** wrapping the EXISTING SR proposer (`run_sales_recovery_agent`) + executor (`SalesRecoveryAgent.execute_item`). Zero edits to any SR file. Capabilities `{READ_CUSTOMER_LEDGER, PROPOSE_CAMPAIGN}` — **NO gated capability** (the "arm ≠ send" call: SR ARMS the Pillar-7 approval; the send is downstream + platform-owned, so there is no send inside `execute_item` to route through the facade — declaring `REQUEST_CUSTOMER_SEND` would force arm→immediate-send = a money-path semantic change).
**Validation:** `assert_conforms` 8/8 + 14 unit tests + ruff clean + import-inert verified.

### Stage 1 — Integration surface as connector-Tools module — `2366904` (VT-664, §7.1)
`agent_framework/modules/integration_tools_module.py` — `IntegrationToolsModule`, PROPOSER, capabilities `{READ_INTEGRATION_STATE, PROPOSE_CONFIG_CHANGE}`, `tools =` the 11 existing `INTEGRATION_AGENT_TOOLS` verbatim (lazy-built at construction for import-lightness; all VT-268-safe, non-gated). `commit_ingestion` stays proposal-only.
**Validation:** `assert_conforms` 8/8 + 10 unit tests (dep-less `importorskip`) + ruff clean. (Naming `integration_tools` vs `integration_agent` + the activation bar are resolved at the cutover.)

### Stage 2 — GateFacade owns the WHOLE business-action round-trip — `e8f169a` (§2, the B-finding)
The one non-additive-*surface* change, still additive-*behavior*. `GateFacade.perform_business_action(...)` — the symmetric partner to `request_customer_send`: it CLASSIFIES via `assert_or_gate_business_action` **and** ISSUES the effect INSIDE `business_action_context(action_class)` (autonomous), or ARMS the Pillar-7 approval and does NOT issue (requires-approval). Returns `BusinessActionOutcome(gate, performed, result|armed)`.
- **Why additive/low-risk:** NO live path issues a business-action *effect* today — the lanes only intent-check via the decision-only door; the sole round-trip lives in `business_impact_sample`. The old decision-only `gate_business_action` is KEPT for those advisory intent-checks. `GATED_METHOD_BY_CAPABILITY` is unchanged → conformance is byte-stable.
- **Correctness gates untouched:** owner-policy bound, per-class autonomy tier, negative-magnitude, frozen kill-switch all live in the deterministic gate this calls — none bent.
**Validation:** 5 new unit tests (autonomous issues inside the choke; the same guard OUTSIDE the choke raises = control; approval arms + effect NOT run; proposer-scoped facade refused; approval-without-run_id fails loud) + full `agent_framework` suite 29/29 + ruff clean.

**Batch push:** all three + the full pre-push DB suite (4834 passed, 18 skipped) → `origin/dev` → Railway deploy `e8f169a` SUCCESS.

---

## What's deferred, and exactly why

### Stage 3 — LIVE CUTOVER (risky; not done)
Three live-path repoints:
- **(a)** route the coordinator's SR dispatch through `CoordinatorAgentAdapter(registered_sr)` instead of the direct executor.
- **(b)** manager delegates SR via the module (`ModuleContext`) instead of the raw spawn.
- **(c)** manager drives the connector Tools directly; **remove the integration brain / `spawn_integration`**; move the OAuth/mapping/escalate beats to the manager-driven flow; keep zero-manual-paste; close VT-658.

**Why deferred:** (c) is deep surgery on the conversational manager path — the exact path the whole Tier-1=0 objective rides on. Per your non-negotiable, the ONLY acceptance is a post-cutover **full j01–j10 ×1 on deployed dev + tier_rescore with Tier-1=0 HELD, or roll back**. That cycle is: land → push → ~4 min deploy → j01–j10 (~40–60 min) → tier_rescore (~10 min) → assess → (if broken) revert → push → redeploy. ~1.5–2 hr, unsupervised, at ~4am, with a live roll-back branch. Your rail — *"Tier-1=0 MUST HOLD or ROLL BACK (don't patch forward at 3am)"* — is precisely for this moment. I chose the safe checkpoint.

### Stage 4 — REGRESSION GATE (pends Stage 3)
The j01–j10 + tier_rescore run above. Not run because there is no live change to validate yet (Stages 0–2 are inert; the pre-push suite already proved their correctness).

### §7.3 — DB-access inversion (VT-621 GUC-pool class)
Not attempted — you said explicitly: LAST, not required tonight.

---

## Non-negotiables — status

| # | Non-negotiable | Status |
|---|---|---|
| 1 | Gated tool owns the WHOLE round-trip (classify AND issue-inside-choke) | ✅ **DONE** — `perform_business_action` (Stage 2), unit-proven the effect only fires inside the choke |
| 2 | READ tools resolved-tenant-only; no brain-supplied id / no `conn` on brain surface | ➖ Stage-1 module is read-only + capability-scoped; the resolved-tenant enforcement is a **cutover** property (Stage 3) — not yet live |
| 3 | Correctness gates never bend for a green run | ✅ Untouched — all live in the deterministic gates; nothing bent |
| 4 | Post-cutover j01–j10 + tier_rescore Tier-1=0 or ROLL BACK | ⏸ **Pends Stage 3** (nothing to validate until cutover) |
| 5 | One coherent PR per row | ✅ One commit per stage/row; ready to open as one PR at cutover |

---

## Exact resume plan (one read from the supervised cutover)

1. **Re-drive authoritative baseline first** — full j01–j10 + tier_rescore on `e8f169a` to confirm the additive push held Tier-1=0 (expected: unchanged, since inert).
2. **Stage 3(a)+(b) — SR routing** (more contained): wire `CoordinatorAgentAdapter` + manager delegate → push → j01–j10 + tier_rescore → HOLD-or-revert.
3. **Stage 3(c) — integration dissolution** (deepest): manager-driven tools + remove the brain/`spawn_integration` + move beats + keep zero-manual-paste + close VT-658 → push → j01–j10 + tier_rescore → HOLD-or-revert.
4. Do (a)+(b) and (c) as **separate validated pushes**, not one — so a regression is bisectable and the roll-back is surgical.
5. §7.3 DB-inversion: separate, later, only if genuinely safe.

---

## Superseding rulings tracked (folded, not lost)

- **Hinglish template (21:45):** Hinglish-preference tenants → a NEW Hinglish **Latin-script** template SID (en→en, hi→hi, hinglish→new SID, fallback en until approved, **NEVER Devanagari**). Folded into **VT-663 P2** (post-migration language work).
- **Track B tenant UUID `861a56a8`** for the deferred #3-empirical approval-binding audit — priority BELOW the migration.

---

## Open threads (post-migration queue, unchanged priority)

- VT-663 **P2** — inbound language inference + sticky `preferred_language` + language-bind the deterministic nets (`status_query.py:586`) + the Hinglish template mapping above.
- #3-empirical approval-binding audit on tenant `861a56a8`.
- Money invariants Inv2/4/6 negative-seed scenarios (rostered follow-on from CL-2026-07-16-db-money-authority).
