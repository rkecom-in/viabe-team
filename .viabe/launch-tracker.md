# Viabe Launch Tracker — Cowork-managed

**Source of truth** for the launch milestones across the three gates (Reports-Jun15 / Team-Jul15-Soft / Team-Aug15-Full).

> **🎯 BINDING LAUNCH TARGET (Fazal, 2026-06-06): Team go-live = 2026-07-15** (the Team-Jul15-Soft
> gate). Committed PRE-launch: the counsel package (privacy notice + owner_inputs framing + public
> legal copy — CL-430 / VT-156) + VT-231 (prod Supabase Mumbai). Vendor cutover legs in flight:
> VT-109 (Razorpay KYC DONE; Live plans + cutover canaries residual), VT-111 (DLT submitted).
> **Reports-Jun15 is a SEPARATE Reports-project milestone — do NOT conflate with the Team launch.**

**Owner:** Cowork. Migrated from Notion data source `413be4ab-870d-4895-bf35-dfd579142001` on 2026-05-26 by Cowork (Notion-MCP fetch).

**Coverage:** 35 / 48 milestones extracted in initial pass. Remaining 13 (MS-5/6/7/8/10/12/14/16/17/18/24/46/47) deferred — Cowork backfills opportunistically. The 35 captured include all 3 Launch Gates + the highest-visibility Sprint 1 / Reports-Jun15 milestones.

**Reads against this file:** Cowork surfaces overdue + due-this-week at session start. Fazal can ask "what's overdue?" / "show launch gate status" / "mark MS-N done" — I edit the file.

**Status note (2026-05-26):** every milestone below carries its **Notion-side status as of 2026-05-12** (when the Launch Tracker was last broadly updated). Many statuses are now STALE vs. actual repo + vendor reality. Cowork will reconcile at next Fazal-led pass; in the meantime, status fields below are an audit-trail snapshot, NOT current reality. See `Status reality reconciliation` section at bottom for known drift.

---

## 🚨 OVERDUE (as of 2026-05-26 IST)

**Reconciled 2026-05-26 ~16:55 IST with Fazal — 5 of 6 prior-overdue now flipped to Done; 1 explicitly Cancelled. No remaining overdue items.**

| MS | Title | Reconciled Status |
|---|---|---|
| ✅ MS-3 | Razorpay Live KYC submitted | **Done** (Fazal confirmed 2026-05-26) |
| ✅ MS-4 | Razorpay Live activated (Reports + Team plan IDs) | **Done** (Fazal confirmed — Razorpay Live account active) |
| ✅ MS-9 | viabe-team Supabase dev + prod (ap-south-1) | **Done** (Fazal confirmed both available; canary runs hit dev daily) |
| ✅ MS-1 | Meta WhatsApp Tier-A templates authored (5 EN+HI) | **Done** (Fazal confirmed — templates created) |
| ✅ MS-2 | Meta Tier-A templates submitted to Meta via Twilio | **Done** (Fazal confirmed — added to Twilio) |
| ❌ MS-45 | Daily alert mechanism live (Telegram) | **Cancelled 2026-05-26** — superseded by Cowork PM-role |

## ⏳ DUE THIS WEEK (next 7 days, through 2026-06-02)

| MS | Due | Title | Owner | Reconciled Status |
|---|---|---|---|---|
| MS-22 | 2026-05-28 (+2d) | Razorpay webhook URLs configured (Reports + Team) | Shared | **Partly Done** — Reports webhooks configured (Fazal 2026-05-26); Team webhooks deferred until Team is ready for test. **Reset target date to "when Team is ready for test."** |
| MS-13 | 2026-05-29 (+3d) | LangGraph orchestrator complete (8 subtasks VT-24-31) | Claude Code | ✅ **Done in repo** — VT-28/31/32/33-37/39 all shipped; VT-3.x DBOS substrate Done. Notion status stale; flipping. |

## ⏳ DUE THIS WEEK (next 7 days, through 2026-06-02)

| MS | Due | Title | Owner | Gate |
|---|---|---|---|---|
| MS-22 | 2026-05-28 (+2d) | Razorpay webhook URLs configured (Reports + Team) | Shared | Reports-Jun15 |
| MS-13 | 2026-05-29 (+3d) | LangGraph orchestrator complete (8 subtasks VT-24-31) | Claude Code | Team-Jul15-Soft |

## 📅 Sprint-1 horizon (through Reports-Jun15)

| MS | Target | Title | Status |
|---|---|---|---|
| MS-15 | 2026-06-07 | 13 MCP tools complete (VT-39-51) | Not Started |
| MS-11 | 2026-06-08 | Descriptor homepage copy authored + reviewed | Not Started |
| MS-21 | 2026-06-10 | Reports bug burndown to launch-ready | Not Started |
| MS-19 | 2026-06-12 | L1 + L2 Knowledge Architecture (5 subtasks) | Not Started |
| MS-32 | 2026-06-15 | Meta Tier-B templates authored + submitted (17) | Not Started |
| **MS-35** | **2026-06-15** | **REPORTS LAUNCH GATE: Reports live + descriptor homepage** | **Not Started** |
| MS-29 | 2026-06-16 | Observability + Cost (VT-101-105) | ✅ **Done in repo** — VT-101/102/103/104 + VT-171 Logfire migration + VT-175 attributions/day-39 + VT-176 real bodies all merged. |
| MS-30 | 2026-06-15 | Twilio Team sender ID approved | ✅ **Done** (Fazal 2026-05-26 — sender approved on Twilio; Reports sends via it; Team uses same sender) |
| MS-20 | 2026-06-19 | L3 + L4 Knowledge Architecture (3 subtasks) | Not Started |
| MS-23 | 2026-06-20 | Privacy Architecture core (5 subtasks) | Not Started |
| MS-25 | 2026-06-26 | Owner Surface core (5 subtasks) | Not Started |
| MS-26 | 2026-06-29 | Owner Surface portal + monthly report + SupportBot | Not Started |
| MS-31 | 2026-06-30 | Meta Tier-A templates approved (5) | ✅ **Done** — Fazal 2026-05-26: 8 templates approved + Twilio SIDs captured at `.viabe/templates.md`. 5 launch-blocking Tier-A subset within. |
| MS-27 | 2026-07-02 | Billing + Trial + Refund + Founding Counter (6 subtasks) | Not Started |
| MS-33 | 2026-07-04 | Soak run harness ready | Not Started |
| MS-28 | 2026-07-05 | Landing Site complete (6 subtasks) | Not Started |
| MS-34 | 2026-07-08 | 3-day soak run completed (PASS) | Not Started |
| MS-48 | 2026-07-08 | Ops Console MVP (3 views) | Not Started |
| MS-36 | 2026-07-11 | Final security/privacy/legal review (DPDPA pass) | Not Started |
| MS-38 | 2026-07-13 | Concierge-mode operations procedures | Not Started |
| **MS-37** | **2026-07-15** | **TEAM SOFT LAUNCH GATE: 10 design partners onboarded** | **Not Started** |

## 📅 Post-soft-launch (Team-Aug15-Full)

| MS | Target | Title | Status |
|---|---|---|---|
| MS-39 | 2026-07-22 | Soft launch Week 1: first weekly cycles observed | Not Started |
| MS-43 | 2026-06-15 | Founder-led outreach: 30 candidates/week (starts at Reports gate) | Not Started |
| MS-44 | 2026-08-10 | 2-3 design partner case studies published | Not Started |
| MS-40 | 2026-08-12 | Soft launch Week 4: first day-39 evals + refund conversations | Not Started |
| MS-41 | 2026-08-12 | Meta Tier-B templates: 18+/22 approved | Not Started (vendor-side) |
| **MS-42** | **2026-08-15** | **TEAM FULL LAUNCH GATE: Public sign-up at viabe.ai/team** | **Not Started** |

---

## Status reality reconciliation (known drift from Notion as of 2026-05-26)

Notion statuses are mostly "Not Started" because the Launch Tracker hasn't been actively maintained since the 2026-05-25 cutover. Repo state shows substantial progress that Notion doesn't reflect. Known drift:

- **MS-9 (Supabase dev + prod, target 2026-05-16):** Repo + canary runs hit the Supabase dev DB daily. PROD project status unknown to Cowork; likely also provisioned. **Suggested status: ✅ Done.** Action: Fazal-verify + flip.
- **MS-29 (Observability + Cost, target 2026-06-16):** VT-101 LangSmith → superseded by VT-171 Logfire; VT-102 pipeline_log Done; VT-103 cost dashboard Done; VT-104 PII redactor Done; VT-171 Logfire migration Done; VT-175 attributions+day-39 Done; VT-176 real bodies Done. **5/5 observability subtasks shipped + 1 superseded. Suggested status: ✅ Done.**
- **MS-13 (LangGraph orchestrator, target 2026-05-29):** VT-24-31 — VT-31 (Pre-Filter Gate) Done, VT-32 (Agent SDK) Done, VT-33-37 Done, VT-39 Done, VT-28 (scheduled triggers) Done. VT-3.2 transitions Done. **Most subtasks shipped. Suggested status: ✅ Done (verify).**
- **MS-45 (Daily alert mechanism):** ❌ **CANCELLED 2026-05-26** — superseded by Cowork's PM role. VT-121 also cancelled. This milestone should be marked Cancelled / Deferred-by-design.
- **MS-3 (Razorpay KYC):** Still legitimately Not Started per Fazal-owned status; Fazal needs to submit. **Overdue 11d — surfaces in alerts.**
- **MS-1 / MS-2 (Meta Tier-A authored + submitted):** Templates authored may be in-progress per recent template work; submission to Meta likely not yet done. **Fazal-confirm.**

## Backfill TODO (missing from this file pending Cowork extraction)

13 milestones not yet extracted from Notion (MS-5/6/7/8/10/12/14/16/17/18/24/46/47). Cowork backfills opportunistically in subsequent sessions. None of the missing IDs are launch-gate headers (those are MS-35/37/42, all captured). Most missing items are likely vendor-approval intermediates or Sprint-2-3 sub-bullets that don't gate Reports-Jun15.

## Deferred-to-E2E walks (don't lose these)

Real-call walks deferred from a VT-row's Done gate to end-to-end testing (the
row ships on unit/DB coverage; the live walk happens at E2E).

- **VT-283 Shopify OAuth managed-install — real-merchant OAuth walk.** The
  owner-facing OAuth install path (`shopify/setup` → consent → `oauth/callback`
  → offline-token exchange) is built + unit/DB-tested, but CANNOT be live-walked
  on our own dev store (same-org = client_credentials). A real merchant store on
  a DIFFERENT org is required. Fazal ruled (2026-06-02) the live walk happens
  during E2E testing, not as a VT-283 gate → **VT-283 Done on unit tests**. Do
  NOT treat the OAuth path as production-proven until this E2E walk runs against
  a real merchant store. Not Reports-Jun15-blocking (owner reports are the gate).
- **VT-286 WABA Embedded Signup — real-merchant Meta Embedded-Signup walk.** The
  owner-owned WABA connect (`whatsapp/setup` → Meta ES popup → `embedded-callback`
  → token exchange + dedicated-number provision → status `verifying`) is built +
  unit/DB-tested with injected exchange/provision, but CANNOT be live-walked until
  the Meta Tech Provider track + `whatsapp.env` creds land (Fazal initiates
  separately). The live ES walk against a real merchant WABA is E2E-deferred. **Twilio
  flag:** if the live walk shows tech-provider ES insufficient for owner-owned WABAs,
  STOP + flag (do NOT switch to Meta Graph silently). Send is fail-CLOSED until WABA
  `live`, so nothing ships to customers pre-walk. Not Reports-Jun15-blocking.
- **TRADEMARK DEPENDENCY (Fazal, 2026-06-09):** The Viabe trademark filing now gates THREE things: (1) WABA display name "Viabe" (Meta), (2) the SMS DLT header (Fazal postponed the entity-name `RKECOM` interim header in favor of a Viabe-branded header post-TM), and (3) — consequently — the **owner-login OTP path**, since WhatsApp-OTP needs WABA and SMS-OTP needs the DLT header, and BOTH now wait on the TM. **No login channel is independent of the trademark.** Mitigation: both Meta and Airtel typically accept a PENDING trademark APPLICATION NUMBER (not a grant) — so the real gate is FILING the TM (days), not the grant (months). Action: file the Viabe TM ASAP + capture the application number; if Meta/Airtel reject the pending application, reconsider the `RKECOM` interim SMS header so login isn't stranded.
- **VT-267 PR-C owner onboarding wizard — real OAuth round-trip + WhatsApp-WebView E2E.** The
  wizard (Review-&-Confirm + system-browser OAuth handoff + re-check resume) is built +
  unit/DB-tested (20 vitest incl. a no-`window.open` source assertion + 5 real-PG canary). Two
  walks are E2E-deferred: (1) the **profile-draft enrichment is Apify-key-gated** — the wizard
  displays the existing L1 business_profile, EMPTY until Apify is provisioned (no live GBP/Swiggy/
  Zomato scrape pre-keys); (2) the **real OAuth round-trip + WhatsApp in-app-browser handoff** can
  only be walked on a real device against live Google/Meta OAuth (the no-popup anchor handoff is
  source-asserted, but the actual WebView→system-browser→return hop is device-gated). Not
  Reports-Jun15-blocking. (Cowork-flagged at PR-C merge, 2026-06-03.)

## Policy ↔ Implementation conformance (gate: customer-messaging go-live; NOT Reports-Jun15)

Tracks where published `docs/policy/` claims must match shipped implementation before any real
customer data / messaging goes live. Sourced from the 2026-06-03 conformance pass
(CL-390/416/422). Reports-Jun15 is NOT gated on these; customer-messaging go-live IS.

| Policy claim | Reality | Action / owner | Gate |
|---|---|---|---|
| Opt-in required before WhatsApp send | VT-301 enforces `has_consent_for_phone` fail-closed | merged #240 — CC | done (at merge) |
| Phone encrypted at rest | consent tokenised + resolution seam Fernet-encrypted; `customers.phone_e164` PLAINTEXT (RLS/access-controlled) | claim corrected in privacy-policy §7 + DPA Annex C; **optional hardening row: encrypt `customers.phone_e164`** — Fazal to decide | pre-publish |
| Hosted in India (Mumbai) | **prod-Mumbai DB IN PLACE (Fazal 2026-07-02)** — deploy when tested (prod deploy stays Fazal-authorized); dev=Seoul, CL-422 no real data on dev until cutover | deploy-when-tested — Fazal-authorized | before any real customer data |
| Shopify OAuth (managed install) | **HAVE (Fazal 2026-07-02)** — credentials in place | — | — |
| Razorpay Live | **IN PLACE (Fazal 2026-07-02)** — KYC + Live activated + plans | — | — |
| Meta WhatsApp `team_welcome3` | Meta APPROVED but **force-converted to MARKETING** → welcome real-delivery GATED until reclassified UTILITY; **Fazal filing a reclassification appeal (2026-07-02)** | Meta appeal — Fazal | before welcome real-delivery |
| Google OAuth (GBP) | status to confirm if a live path needs it (Fazal 2026-07-02) | confirm — Fazal | if live GBP path |
| Sub-processor: Voyage | not used in source | marked planned-not-active (done) | — |
| Cookies: analytics | essential-only, no analytics | corrected to essential-only (done) | — |
| Data-principal access/correction + Grievance Officer | erasure (`purge_consent`) ✓; **DPDP review DONE (Fazal 2026-07-02 — counsel/DPDP closed)**; access/correction UI still to build; confirm Grievance Officer appointment is inside the DPDP close or flag if separate | build access/correction; confirm officer — Fazal | pre-publish |
| VTR capacity + assignment-scoping | **3 VTRs × ≥100 tenants (~300), 1:100 = graduated target (Fazal 2026-07-02, CL-2026-07-02-vtr-capacity)**; per-tenant assignment-scoping ALREADY BUILT (VT-377/mig-134 + VT-295 UI, 18/18 green — CL-2026-07-02-vtr-scoping-already-built) — 2nd/3rd VTR NOT DB/UI-blocked | assign tenants to new VTRs — operational | before 2nd/3rd VTR |
| Children's data not collected | no age controls; AUP prohibition only | decide controls — build/policy | pre-publish |

**Hardening candidate (flag for Fazal, NOT rostered):** column-encrypt `customers.phone_e164`
vs relying on RLS + access controls. Defensible either way — many systems keep operational PII
access-controlled, not column-encrypted. Do not roster without Fazal's go.

## Maintenance protocol

- **Updates:** Cowork edits this file when status changes (Fazal says "mark MS-N done" → edit YAML).
- **Surfacing:** at session start, Cowork reads this file + surfaces overdue + due-this-week without prompting.
- **Notion side:** Notion Launch Tracker is now archival. Do not edit it. All future updates land here.
- **Path to full migration:** when Cowork next has bandwidth, backfill the 13 missing milestones via additional Notion-MCP fetches.
