# Customer-data go-live prerequisites — LOUD tracker

**Created 2026-06-04 at Phase-1 close (Fazal Option A).** Purpose: keep the Fazal-gated launch-prerequisites
**impossible to miss** now that VT-7 + VT-8 are flipped → Done. **VT-7/VT-8 "Done" = the privacy + knowledge
ARCHITECTURE BUILD scope is complete + verified — NOT "ready for real customer data."** The items below still
gate go-live and are tracked here so nothing Critical hides behind a Done parent.

> **None of these gate Reports-Jun15.** They gate **customer-data-go-live** / **customer-messaging-go-live**.
> The Reports-Jun15 milestone runs on business reporting, not the customer-data path.

Cross-linked from: `.viabe/sprint/VT-7.md`, `.viabe/sprint/VT-8.md`, `docs/clau/latest-snapshot.md` (DO-NOT).

---

## Launch-gating prereqs (Fazal / infra / legal — NOT CC code rows)

| VT | Priority | What | Owner | Gates |
|----|----------|------|-------|-------|
| **VT-78** | **CRITICAL** | Prod **data-residency config** — ap-south-1 (Mumbai) primary + ap-south-2 backup. Part of the **VT-231** prod-Supabase cluster. The privacy CODE is region-agnostic + Done; choosing the prod region is a deploy-time action. | **Fazal / infra** | **customer-data-go-live** (real customer data must not touch dev/Seoul — CL-422). NOT Jun15. |
| **VT-156** | Critical | **Privacy notice** — draft exists (`docs/policy/viabe_privacy_notice_draft.md`, 10 counsel items open); needs lawyer review + publish on the owner-facing surface (CL-389/CL-391). | **Fazal / counsel** | **customer-messaging-go-live** (DPDP truthful-disclosure-before-consent). NOT Jun15. |
| **VT-312** | — | **Customer-state detector thresholds** (customer_dormant / high_value_threshold_crossed). | **Fazal (product)** | L2 customer-referencing event coverage **+ the VT-76 reconstitution sweep's real-data effect** (the sweep is correct + canaried but a no-op on real data until VT-312 emits). |
| **VT-313** | — | L4 skills **corpus authoring** (the corpus L4 reads is empty until authored). | **Fazal / Clau** | L4 skill retrieval quality. |
| **VT-314** | — | **Voyage paid key** + CI secret (free-tier 3 RPM rate-limits embeddings). | **Fazal** | L1/L4 embedding throughput at scale. |
| **VT-318** | — | Inbound **WABA STOP-handler** (consumer_opt_out classifier + immediate effects + templates). | **WABA-gated** (live customer-inbound path). | the RECEIVE side of opt-out (VT-76 ships the sweep mechanism; this is the inbound trigger). |
| tier_2 list | — | Census-top-50 **tier_2 city list** sign-off (VT-75 coarsening data). | **Fazal** | tier_2 city coarsening correctness. |
| **VT-231** | Critical | Prod **Supabase Mumbai** project (the parent of VT-78). | **Fazal** | customer-data-go-live (no real data on dev until it closes — CL-422). |

## Engineering fast-follows (rostered, NOT launch-gating — CC-buildable on dispatch)

VT-304 / VT-305 / VT-306 / VT-307 / VT-308 (privacy fast-follows) · **VT-311** (L2 18-month soft-delete retention + 100K-event perf, Phase-2) · **VT-316** (pre-push hook fail-loud on empty ref-range) · **VT-317** (city-capture wire at onboarding → set_tenant_city_tier) · **VT-319** (ci.yml "no-LLM-in-deterministic-subtree" lint → comment-aware; falsely RED since #260/VT-303).

## Phase-2 (deferred, confirmed)

VT-149 (`(tenant_id, message_sid)` UNIQUE — operational idempotency, already defended) · VT-161 (DBOS cloud-conductor recovery re-verify — conditional, not wired).

---

## Provenance
Phase-1 (Sprint-7 Knowledge + Privacy Architecture) build scope CLOSED 2026-06-04 (VT-7 + VT-8 → Done, main
`82ad996`). DSR-hardening gates this session: VT-154 (#300) → VT-160 (#301) → VT-153 (#302). Tracker requested
by Fazal (Option A, Cowork 20260604T061500Z) so the residual go-live prereqs — especially **VT-78 (Critical
prod residency)** — never hide behind a Done parent.
