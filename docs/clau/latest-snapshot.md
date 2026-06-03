# Latest State Snapshot

**As of:** 2026-06-04 (regenerated after the Sprint-7 moat closed; reconciled against `git log --oneline` on main + the `.viabe/sprint/` board per Rule #14). **Main HEAD:** `6fffd4d` (VT-76 reconstitution, #297). **Reports-Jun15 in 11 days.**

> Treat as suspect until reconciled (Rule #14). The **Sprint-7 moat section below is reconciled against the board by CC** (every row's `status:` checked). The **Reports-Jun15 / VT-267 onboarding section is partially carried-forward** — CC does not hold current PR-B/C/D + VT-283 + VT-85 state; **Cowork to reconcile that block** against the live board before trusting it.

---

## CRITICAL PATH

**Two tracks, both live.**

1. **Sprint-7 Knowledge Architecture moat — CLOSED (2026-06-04).** All moat layers shipped to main + each canaried on the live composition path:
   - **L1** entities/relationships (voyage vector(1024)+HNSW, hash-phone only) · **L2** episodic_events + kg_events dual-projection (VT-66/67/309) · **L3** cross-tenant priors (n_tenants≥10 CHECK, no RLS by design; VT-68/69) · **L4** skills corpus (voyage-4-lite; VT-70).
   - **Composition** (Pillar 8 single `build_sales_recovery_context`) stitches L1+L2+L3+L4+campaigns+owner_inputs under a global token budget with moat-protecting truncation + composition_audit (VT-71).
   - **Privacy layers:** k-anon admission k≥10 (VT-74) · context-isolation pre/post-flight audit (VT-73, #293) · city coarsening tier-only (VT-75, #295) · **opt-out 7-day reconstitution sweep + SLA (VT-76, #297) — the closing row** · PII-in-payload fix (VT-315) · test-DB reaper (VT-310).
   - **Adversarial verification** (ultracode workflow) confirmed the moat SOUND (k-anon gates, L3 CHECK, RLS isolation, L1 hash-only, downstream non-propagation) and caught one real CL-390 crack → fixed in VT-315.

2. **Reports-Jun15 gate (carried-forward — Cowork reconcile).** Per the prior snapshot the Sprint-3 ingestion engine is COMPLETE on main (primitives VT-52/53/54, two-surface ledger 273/276, 258 read-wire, 275 attribution bridge, methods 55-63, Apify 61/62; vision+voice live-verified; Shopify VT-208/#221 + VT-213 walk green). Active non-moat stream = **VT-267 owner onboarding** (+ VT-268 guardrails). Launch-blocker **VT-231** (prod Supabase Mumbai; CL-422 — no real customer data on dev until it closes; Fazal-side, parked).

## IN FLIGHT (CC)

- **None open.** VT-76 (#297) merged; the prior IN-FLIGHT trio **#222 (VT-213 Done), #223 (CL-427 connector-audit), #224 (VT-267 PR-A2 identity) are all MERGED.**
- This session's closure-docs PR (CL-428 + VT-79 note + snapshot/active-context regen) is the only thing CC is opening; VT-7/VT-8 parents are **held open** (see DO NOT).
- **Cowork reconcile:** current state of VT-267 **PR-B/C/D**, **VT-283** (Shopify owner OAuth managed-install), **VT-85/8.5** (consent-capture) — CC does not hold these; confirm against the board.

## BLOCKED ON

- **Fazal (launch-gated moat follow-ups):** **VT-312** detector thresholds (unblocks the reconstitution sweep + L2 customer-referencing events on real data) · **VT-313** L4 corpus authoring · **VT-314** voyage paid key (free-tier 3 RPM rate-limits) · **VT-318** inbound STOP handler (needs live customer-inbound / WABA) · tier_2 city list sign-off (VT-75) · **VT-231** Mumbai prod (parked).
- **Cowork:** regen the PM + sprint dashboards off the reconciled board; rule on the VT-7/VT-8 parent flip (long non-moat tail — see DO NOT); reconcile the VT-267/VT-283/VT-85 block above.

## NEXT ACTION

- **CC (on signal):** pick up the next Cowork-dispatched row. Moat-side rostered follow-ups (non-launch-gated): **VT-316** (pre-push hook fail-loud on empty ref-range) · **VT-317** (city-capture wire at onboarding → set_tenant_city_tier) · **VT-311** (L-tier follow-up) · the new **ci.yml `lint` false-positive** fix (Anthropic-in-comments since #260/VT-303 — needs a VT row).
- **Cowork:** dashboards regen; VT-7/VT-8 flip ruling; VT-267 stream reconcile.

## DO NOT

- **Do NOT flip VT-7 / VT-8 → Done.** Reconciled 2026-06-04: the **moat rows are all Done, but the parents have a long non-moat tail still open** — VT-7: VT-143/146 (To Do), VT-155/159 (Queued), VT-311/316 (Backlog), VT-312 (Blocked). VT-8: VT-78/147/149 (Backlog), VT-144/145/148 (To Do), VT-151 (Deferred), VT-153/154/156/158/160/161 (Queued), VT-303 (In Progress). The moat ≠ the parents. Cowork rules on whether the tail is Phase-2/deferrable before any flip.
- Let **real customer data** touch dev pre-VT-231/Mumbai (CL-422). Dev = synthetic only.
- Re-flag **Seoul dev** as a DPDP issue (CL-422 — accepted with launch-gate sunset).
- Treat the **reconstitution sweep as live coverage** — it is correct + canaried but a **no-op on real data until VT-312** emits customer-referencing episodic events. Mechanism live; coverage grows with VT-312.
- Merge an **owner-facing** Shopify connector on client_credentials — owners need the OAuth managed-install (VT-283); client_credentials is dev/own-store/same-org only (CL-421 / CL-427).
- Build the privacy/consent **legal copy** in CC — Cowork drafts, Fazal/counsel legal-validates.
