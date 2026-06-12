# Latest State Snapshot

**As of:** 2026-06-12 (mid Run-Control-Panel batch; restored after a working-tree wipe of uncommitted Cowork substrate — see DO NOT). **dev HEAD:** `96c5504` (VT-375 row close; merge `9213024` #459). **main HEAD:** `2de4b36` (#436). **BINDING Team go-live: 2026-07-15.** Branch governance per CL-432 (`dev` staging / `main` Fazal-authorized promotion only).

> Reconciled against `git log origin/dev` (Rule #14). The 6-gap state is CL-434; the Run-Control batch grant is CL-435.

---

## CRITICAL PATH

**VTR RUN-CONTROL PANEL A–D (CL-435, session-blanket autonomous), on top of the COMPLETE 6-gap build (CL-434).**

- **Phase A — VT-374 MERGED** (#458 `b8bd111`): run-control substrate. Controllable/observed step registry; consume-first overrides (recovery-idempotent); boundary-only checkpointed pause with **opt-out/DSR pause-EXEMPT (I6)**; app-level `rerun_from` (fresh uuid4 identity, lineage, 409-on-open-approval, side-effect policy); `vtr_step_timeline` keys-only view; gate manifest + import-raise + CI grep; step-harness CLI; **VT-300 `run_controls` RETIRED** (mig-131). Twice-gated (plan v1 FAILED → v2 redesign; build gate found B1 missing-workers → fixed 1b62c69).
- **Phase B — VT-375 MERGED** (#459 `9213024`): programs projection (past/running/upcoming-7d) + read-only canvas + binding honesty copy. **C1 race test exposed a REAL rerun/approval TOCTOU → ruling A: detect-and-escalate** (overlap → rerun closed `escalated` + alert, never silent; mig-128 still guarantees never-two-open). B-analysis (locks) recorded in the row.
- **Phase C — VT-376 DISPATCHED (risk row, in flight):** interactive controls wired to Phase-A POSTs; plan-ack RULED (narrow rerun-vs-rerun advisory lock; C2 = mig-132 explicit key projection for the 3 passthrough kinds; mutation map approved); VT-380 rider (ops 500 + hydration #418, allowlist removed). NOT self-merge → Cowork gate.
- **Phase D — VT-377 NEXT:** multi-VTR capability-complete (assignment-scoped views + Gap-6 Devanagari validator close + panel honors assignments; NO human onboarding — Fazal ruling).

**Customer messaging remains FAIL-CLOSED** (CL-434 three stops). Externals unchanged: Meta F1 templates + counsel C1–C3 (the launch long poles) + VT-231 Mumbai cutover before any `dev→main` promotion.

## IN FLIGHT (CC)

- **VT-376** (Phase C) building on branch `vt376-runcontrol-interactive`; ~2-min nudges.

## BLOCKED ON

- **Fazal:** VT-379 dispatch ruling (unredacted `pipeline_steps.error` + 3 direct-INSERT writers — privacy gap, rostered Queued); Meta F1 submission; counsel C1–C3; VT-231.
- Phase D dispatches on Phase C merge (Cowork-sequenced, not blocked).

## NEXT ACTION

- **Cowork:** gate VT-376 on pr-ready (executed evidence; rendered verification with hydration allowlist REMOVED) → authorize merge → dispatch VT-377 → close batch (snapshot + CL-436 batch-close entry + dashboard regen + docs PR for the substrate files).
- **CC:** finish VT-376 per ruled plan-ack.

## DO NOT

- **Wipe uncommitted shared-tree files (CL-418 EXTENSION, incident 2026-06-12):** a tree operation during the VT-375→376 branch churn discarded Cowork's uncommitted ledger/active-context/snapshot edits (restored from context). NO `git reset --hard` / `checkout --force` / `checkout -- <paths>` over files you didn't author. Obstacle → signal + wait. The substrate docs ride the NEXT PR via explicit `git add`.
- Merge VT-376 without the Cowork gate (risk row — mutations + mig-132 + gate-adjacent).
- Touch `main` (CL-432 — promotion is Fazal's word only).
- Re-litigate: C1-A detect-and-escalate (locks analysis recorded), keys-only timeline (I1), the frozen 4-kind passthrough list (narrowed by mig-132, never widened without a fresh PII audit).
- Trust this snapshot's HEAD claims without `git log` (Rule #14).
