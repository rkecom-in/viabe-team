# Latest State Snapshot

**As of:** 2026-06-07 (overnight Cowork↔CC delivery). **Main HEAD:** `4ce9013` (VT-282, #411). **BINDING Team go-live: 2026-07-15** (Fazal 2026-06-06; Reports-Jun15 is the SEPARATE Reports milestone — do not conflate). Earlier As-of block (#393) retained below; **Cowork's morning brief does the full Rule-14 reconcile + dashboard regen.**

> **OVERNIGHT DELTA (#394–#411, CC-reconciled).** **Backlog-close queue CLOSED:** 3 stale closures (VT-14/114/308) + VT-358 (customer opt-out i18n) + VT-357 p2 (SLA-breach sweep) + VT-352 (Razorpay dead-letter + replay + vendor-orphan idempotency, money, adversarially bounce-amended) + VT-353 (legal-page DRAFT shells) + VT-322 (reaper) + VT-356 (runner mypy) + VT-107 (edge-case coverage manifest + gate-guard). **PM dashboard retired** (CL-430). **In-flight close:** VT-208→Done (Shopify drift), VT-161→Blocked (no cloud conductor), VT-162→Done, VT-318 reconciled (stays Blocked on live-WABA). **AOL wave:** VT-282 (escalation-rate+decay) Done #411; **VT-281 (#408 VTR de-identified views, pii) + VT-279 (#409 VTR/OWNER classifier) OPEN in Cowork's review**; VT-280 (VTR digest) HELD until #408+#409 land. VT-357 closed (p1→brain-send-wiring acceptance, p3→VT-108 template gate). **Vendor:** VT-109 KYC done (Live-plans+cutover-canaries residual), VT-111 DLT submitted. Launch prereqs unchanged: counsel package (CL-430/VT-156) + VT-231 Mumbai, pre-2026-07-15.

---
**[stale — superseded by the delta above; Cowork reconciles]** As of 2026-06-06 (#366–#393), HEAD `3e689d1` (VT-329 i18n, #393). Reports-Jun15 block carried-forward.

> Treat as suspect until reconciled (Rule #14). The **Sprint-8 close block is CC-reconciled** (every merged PR cross-checked to its row's `status: Done`). The Reports-Jun15 / VT-267 onboarding block is **carried-forward — Cowork reconciles it** (CC does not hold current PR-B/C/D + VT-283 state).

---

## CRITICAL PATH

**SPRINT-8 (Owner Surface & Billing + Launch Surface) — BUILDABLE CLOSE QUEUE COMPLETE (2026-06-06).** The Jun-6 autonomous Cowork↔CC batch merged **27 rows**: self-merge tier (VT-98/100/119/346 + reconciles), Cowork-review tier (VT-149/336/333/347/343-2a/354/345), and all **3 plan-first rows** (VT-328 rls dispatch-block, VT-329 Critical i18n; live-cutover/pii deferred). Earlier in the batch: VT-349/89-drop/326/327/330/95/96/97/332/334/355.

- **Owner surface:** free-form bilingual acks (VT-349), VT-84 hardening (VT-336: exclusion confidence floor + phone-strict + 5 keystone tests), approval defer + per-week budget (VT-334), SupportBot Phase-2a (VT-343: dup-Fazal-alert-on-replay gate + fatigue flag), refunded/cancelled **dispatch-block** at the single execution chokepoint (VT-328), **Hindi/Hinglish DSR/opt-out/negation** fix (VT-329 — Devanagari lookaround boundary + romanized negation; adversarially re-verified: 41 adversarial + 49 tests).
- **Billing:** Razorpay webhook hardening (VT-330), trial-end single-use token (VT-332), founding-slot release on cancel (VT-333, audit-only), owner_inputs (tenant_id, message_sid) UNIQUE **replay-idempotency** (VT-149).
- **Launch surface:** bilingual landing (VT-95) + signup OTP step (VT-96) + waitlist mode (VT-97) + honest social-proof (VT-98) + cookie-free A/B framework (VT-100); waitlist 6-month retention **scheduler** — DPDP bound now ENFORCED (VT-354).
- **Infra/docs:** pre-push rls_ckpt_tester drop (VT-346), get_business_profile graceful-degrade + app_role grants (VT-347), source-of-truth doc banners (VT-119), Lighthouse scaffold (VT-345), VT-355 dashboard-generator freeze.
- **Process:** CL-429 (merge-on-green self-merge for [BUILD] rows) established + exercised; the VT-329 BLOCK→fix→conditional-merge loop ran clean.

**Reports-Jun15 gate (carried-forward — Cowork reconcile).** Launch-blocker VT-231 (prod Supabase Mumbai; CL-422 — no real customer data on dev until it closes; Fazal-side, parked).

## IN FLIGHT (CC)

- **This Sprint-8 close-out reconcile PR** (VT-11→Done, VT-10 annotated, VT-9 held, sprint-brief + this snapshot) → Cowork's gate. Nothing else open.
- **Cowork reconcile:** VT-267 PR-B/C/D, VT-283 — CC does not hold these.

## BLOCKED ON

- **VT-9 close** — held on child **VT-157** (Critical, LAUNCH-GATING consent-capture, Queued). Hypothesis: superseded by VT-303 (ACTIVATE-TEAM enable + fail-closed brain consent gate). **Cowork's call:** close VT-157 superseded → VT-9 Done, or keep open.
- **VT-10 (live-cutover umbrella)** — all buildable billing children Done; closes at live-Razorpay go-live (gated on VT-231 + Fazal KYC/keys).
- **VT-357** (SupportBot Phase-2b, Sprint-9) — #1 completed-no-send [PLAN-FIRST design fork], #2 SLA sweep [marker-migration pre-approved], #3 /resolve [Fazal-gated template].
- **Customer-data-GO-LIVE prereqs (Fazal-gated):** VT-78 (prod residency / VT-231 Mumbai), VT-156 (privacy-notice publish), VT-353 (public legal pages), VT-318 (WABA STOP), VT-312 (detector thresholds). Full list: `.viabe/customer-data-go-live-prereqs.md`.
- **Sibling (flagged):** `integrations/customer_inbound.py` opt-out is whole-body-exact — the CUSTOMER opt-out surface has the same "please band karo" miss VT-329 fixed for the owner gate. Recommend a follow-up row.

## NEXT ACTION

- **CC (on signal):** merge this reconcile PR (Cowork-authorized); then next Cowork-dispatched row. VT-357 (Sprint-9) is the next substantive build (plan-first parts await Fazal/Cowork).
- **Cowork:** rule on VT-9/VT-157 (supersede?); regen PM + sprint dashboards off the closed board; brief Fazal Sprint-8-complete + the launch-prereq checklist; roster the customer_inbound opt-out sibling.

## DO NOT

- **Read "VT-329 Done" as "signup go-live ready."** VT-329 is the i18n GATE; the go-live flip also needs live Razorpay + VT-231 + the legal pages.
- Let **real customer data** touch dev pre-VT-231/Mumbai (CL-422). Dev = synthetic only.
- Re-flag **Seoul dev** as a DPDP issue (CL-422 — accepted with launch-gate sunset).
- Flip **VT-9 → Done** until VT-157 is closed/superseded (Cowork's ruling pending).
- Add DSR/opt-out **keywords** treating it as trivial — it's Type-2 governance (yaml header / VT-8 gate); VT-329 added the code-switched set under Cowork's explicit override.
- Add a new **trigger kind** without extending BOTH the Python `TriggerKind` Literal AND the `tenant_alerts.trigger_kind` CHECK in the same migration (CL-428).
- Build the privacy/consent **legal copy** in CC — Cowork drafts, Fazal/counsel legal-validates.
