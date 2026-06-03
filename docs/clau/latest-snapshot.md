# Latest State Snapshot

**As of:** 2026-06-04 (regenerated at Phase-1 close; reconciled against `git log --oneline` on main + the `.viabe/sprint/` board per Rule #14). **Main HEAD:** `bb88cb7` (VT-153 backfill, #302). **Reports-Jun15 in 11 days.**

> Treat as suspect until reconciled (Rule #14). The **Sprint-7 (VT-7/VT-8) section is fully reconciled by CC** (every child's `status:` checked + each gate's PR verified). The **Reports-Jun15 / VT-267 onboarding block is carried-forward** — CC does not hold current PR-B/C/D + VT-283 + VT-85 state; **Cowork reconciles that block**.

---

## CRITICAL PATH

**PHASE 1 (Sprint-7 Knowledge + Privacy Architecture) — BUILD COMPLETE + CLOSED (2026-06-04). VT-7 + VT-8 → Done.**

- **Knowledge moat (VT-7):** L1 entities/relationships (voyage vector(1024)+HNSW, hash-phone) · L2 episodic + kg_events dual-projection (VT-66/67/309) · L3 cross-tenant priors (n_tenants≥10 CHECK, VT-68/69) · L4 skills corpus (voyage-4-lite, VT-70) · single composition (Pillar-8 build_sales_recovery_context, VT-71). Each canaried on the live composition path; adversarially verified sound.
- **Privacy architecture (VT-8):** typed wrappers + no-direct-access lint (VT-72) · context isolation audit (VT-73) · k-anon k≥10 (VT-74) · coarsening (VT-75) · opt-out 7-day reconstitution (VT-76) · DSR export/delete + purge-completable + unscoped-DELETE guard + tenant-anonymize completeness (VT-77/145/154/160) · breach detection (VT-79) · audit hash-chain (VT-80) · body redaction forward+backfill (VT-144/153) · owner_inputs consent gate (VT-303). **This session's DSR-hardening gates: VT-154 (#300) → VT-160 (#301) → VT-153 (#302).**
- **CL-428** locked: tenant_alerts.trigger_kind CHECK must stay synced to the TriggerKind Literal (repaired the VT-79 drift in mig 089).

**Reports-Jun15 gate (carried-forward — Cowork reconcile).** Sprint-3 ingestion COMPLETE on main; active non-moat stream = VT-267 onboarding (+ VT-268). Launch-blocker VT-231 (prod Supabase Mumbai; CL-422 — no real customer data on dev until it closes; Fazal-side, parked).

## IN FLIGHT (CC)

- **None open.** All 3 DSR-hardening gates + the 10 COVERED flips + the VT-7/VT-8 close merged. This Phase-1-close docs PR is the only thing CC has open.
- **Cowork reconcile:** VT-267 PR-B/C/D, VT-283, VT-85 — CC does not hold these.

## BLOCKED ON

**Customer-data-GO-LIVE prereqs (Fazal-gated — distinct from Reports-Jun15):**
- **VT-78** (Critical) — prod **data residency** config (ap-south-1/ap-south-2 = Mumbai), the VT-231 cluster. The privacy code is region-agnostic + Done; choosing the prod region is a Fazal/infra deploy action. **Real customer-data blocker.**
- **VT-156** — privacy-notice publish (draft exists `docs/policy/`) → Fazal/counsel.
- **VT-312** — detector thresholds (unblocks reconstitution + L2 customer-referencing events on real data).
- **VT-313** L4 corpus authoring · **VT-314** voyage paid key · **VT-318** inbound STOP handler (WABA) · tier_2 city list sign-off (VT-75) · **VT-231** Mumbai prod (parked).

**Phase-2 (deferred):** VT-149 (idempotency UNIQUE), VT-161 (cloud-conductor), VT-311 (L2 18-mo retention).

## NEXT ACTION

- **CC (on signal):** next Cowork-dispatched row. Rostered fast-follows (non-launch-gated, CC-buildable): VT-316 (pre-push hook fail-loud), VT-317 (city-capture wire), VT-319 (ci.yml lint false-red comment-aware), VT-304/305/306/307/308 (privacy fast-follows).
- **Cowork:** regen PM + sprint dashboards off the closed board; brief Fazal Phase-1-complete + the launch-prereq checklist; reconcile the VT-267/283/85 block.

## DO NOT

- **Do NOT read "VT-8 Done" as "ready for real customer data."** VT-8 Done = the privacy-architecture BUILD scope. **VT-78 (prod residency) + VT-156 (privacy notice) gate customer-data-go-live and are still OPEN (Fazal/infra/legal).** Reports-Jun15 is NOT gated on them; customer-messaging-go-live IS.
- Let **real customer data** touch dev pre-VT-231/Mumbai (CL-422). Dev = synthetic only.
- Re-flag **Seoul dev** as a DPDP issue (CL-422 — accepted with launch-gate sunset).
- Treat the **reconstitution sweep as live coverage** — correct + canaried but a no-op on real data until VT-312 emits customer-referencing episodic events.
- Add a new **trigger kind** without extending BOTH the Python `TriggerKind` Literal AND the `tenant_alerts.trigger_kind` CHECK in the same migration (CL-428).
- Merge an **owner-facing** Shopify connector on client_credentials — owners need the OAuth managed-install (VT-283; CL-421/CL-427).
- Build the privacy/consent **legal copy** in CC — Cowork drafts, Fazal/counsel legal-validates.
