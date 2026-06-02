# Legal Risk Assessment — Viabe Team policy set

**Date:** 2026-06-02 · **Assessor:** Cowork · **Matter:** robustness of the policy set (Privacy Policy, Terms of Use, DPA + sub-processor schedule, AUP, Cookie Policy) for RKeCom Services OPC Pvt Ltd / Viabe Team. **Privileged:** No.

> **Not legal advice / not "foolproof."** This applies the severity×likelihood framework to harden the drafts and prioritise what counsel must close. Counsel sign-off (Fazal's plan) is the actual validation. The framework reduces and surfaces risk; it does not eliminate it.

## Posture summary
As **unpublished drafts**, the set is **low risk** (GREEN) — they aren't live, and Reports-Jun15 is owner-facing. Risk attaches at **publication / customer-messaging go-live** (post-launch). The items below are gates for *that*, not for Jun15.

## Risk register (per the framework)

| # | Risk (doc) | Sev | Lik | Score | Level | Hardened in draft? |
|---|---|---|---|---|---|---|
| 1 | **Liability cap / indemnity unset** (ToU §10–11, DPA §6) — uncapped exposure if published as-is | 4 | 3 | 12 | ORANGE | No — counsel must set |
| 2 | Sub-processor DPAs not confirmed + locations unverified (DPA Annex B) | 3 | 3 | 9 | YELLOW | Partial — listed + flow-down clause; execution pending |
| 3 | Placeholders published live (`[…]`, draft versions) | 4 | 2 | 8 | YELLOW | Gate: do not publish with placeholders |
| 4 | Security representations exceed actual implementation (PP §7, DPA Annex C) | 3 | 2 | 6 | YELLOW | Partial — flagged "confirm to implementation" |
| 5 | DPDP §6 consent sufficiency (PP §4 + consent copy) | 3 | 2 | 6 | YELLOW | Structure in place; counsel confirms adequacy |
| 6 | Controller/processor classification holds for Viabe | 3 | 2 | 6 | YELLOW | **Hardened** — DPA establishes it contractually |
| 7 | Grievance Officer not yet appointed (DPDP) | 2 | 3 | 6 | YELLOW | Placeholder; business action to appoint |
| 8 | Children's data (DPDP strict regime) if owners upload it | 3 | 2 | 6 | YELLOW | Partial — PP "not for children" + AUP prohibit |
| 9 | Cross-border transfer (sub-processors abroad) | 2 | 2 | 4 | GREEN | Disclosed; DPDP currently permissive |
| 10 | Governing law / arbitration unset (ToU §13) | 2 | 2 | 4 | GREEN | Placeholder; counsel sets |
| 11 | Cookie/analytics inaccuracy vs real implementation | 2 | 2 | 4 | GREEN | Flagged "confirm to implementation" |

## Hardening already applied (framework-driven)
- **DPA** turns "Viabe = processor" from assertion into contract: instructions-only, confidentiality, security (Annex C), sub-processor flow-down (Annex B), **breach notification**, deletion/return on termination, audit. (Closes risk #6.)
- **ToU** allocates customer-data **lawful basis + opt-in/opt-out responsibility to the Owner** (the fiduciary) + indemnity — materially reduces Viabe's exposure.
- **AUP** binds owners to WhatsApp/Meta + anti-spam + opt-in discipline — protects Viabe's WABA standing from a careless tenant (a real preemptive-enforcement risk).
- **Privacy Policy** enumerates DPDP Data-Principal rights, grievance route, retention (CL-416), sub-processors + cross-border, children clause.

## Recommended approach
1. **Nothing here blocks Reports-Jun15** — keep the set as reviewed drafts.
2. **Before publishing / customer-messaging go-live (post-launch), close the gates in priority order:** (1) liability caps + indemnity [counsel] → (2) execute sub-processor DPAs + verify locations [business/counsel] → (3) fill all placeholders + appoint Grievance Officer → (4) align security representations to real implementation [eng/security] → (5) host the Privacy Policy URL (Meta requirement).
3. **Counsel-judgment items (cannot resolve in-house):** liability/indemnity (#1), DPDP §6 consent adequacy (#5), processor-classification confirmation (#6), children-data handling (#8), arbitration/jurisdiction (#10).

## Residual risk
After the gates close + counsel sign-off: **GREEN**. Today, as unpublished drafts: **GREEN/low-YELLOW**, with the single ORANGE (#1 liability) being a *publish-time* gate, not a draft-stage problem.

## Escalation
Liability framework, DPDP §6 adequacy, and processor classification are "strongly recommended: counsel" under the framework. Sub-processor DPA execution is a business/legal action item. None require *outside* counsel solely on these facts, but DPDP is still settling — a privacy-specialist review is well worth it before customer-messaging scale.
