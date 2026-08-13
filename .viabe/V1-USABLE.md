# V1-USABLE — the definition CC and Codex work toward, and how we know we got there

**Fazal 2026-08-11:** *"put in place tasks for Codex and CC, so that they are continuously working
towards creating our first actually usable version."*

Continuous work without a definition of done produces motion. This file is the definition. Both
lanes below map to it, and anything that maps to nothing is not V1 work.

---

## THE DEFINITION

> **V1-USABLE = one real Indian SMB owner, NOT on the allowlist, onboards on their own, gets the
> Manager to do real work for their real customers, and nothing unsafe happens — on prod.**

Not a demo. Not Fazal's number. Not a drill with a seeded tenant. One stranger, their real business,
their real customers, their real money.

## THE SIX EXIT CRITERIA — all measurable, none subjective

| # | Criterion | Proven by | Status 2026-08-11 |
|---|---|---|---|
| **E1** | **They can get in.** Owner completes signup → GST verify → onboarding → activation with no human unblocking them. | A non-Fazal number completing it unaided on dev | Signup gate built (VT-326). Journey proven on drills only. |
| **E2** | **The Manager does the work.** Asked for something in its lane, it delegates and completes — it does not say "I couldn't do it." | **<5% miss, delegations and honest failures counted SEPARATELY, ×3 on the SR pack** | Measured 6.1%. Mechanism named (M1–M4). Gates (c)+(d) open. **VT-738** |
| **E3** | **Sends are safe.** Consent-gated, frequency-governed, no double-send after a crash, no wedge, blocked customers permanently suppressed. | Each forced, not read from the SQL | Choke built. **VT-740** effect-aware wake + approval_resume gate open. **VT-741** tiers + click/reply signals open. |
| **E4** | **Failures are contained and visible.** A failed workflow does not silently re-run, and a VTR can see it and act. | Forced partial-send → withheld, alerted, diagnosable | Diagnosis built but **called by nothing**. Containment open. **VT-634 / VT-740** |
| **E5** | **The owner is never left uninformed.** No state change — trial expiry, campaign outcome, failure — happens silently. | Forced trial expiry produces a delivered message | **BROKEN.** `trial_sweep.py:36` notify is a logging stub while `:367` applies the transition. Silent-terminal detector inert. **B9** |
| **E6** | **It is on prod.** | Fazal's promotion word + a green promotion package | Prod is ~10 commits stale and carries the wedge, the stale-approval hole and the effect-blind re-run. **B3** |

**E7 — ADDED 2026-08-11 (Fazal, following `docs/clau/MOAT.md`). What happened is RECORDED.**
Every action, its authority, its context, its outcome, the owner's reaction, and any VTR override
with its reason — captured from the first real tenant. **VT-744.**
*Why it is an exit criterion and not a feature:* judgment is commodity and a corpus adds nothing, so
the only knowledge that is ever ours is what actually happened to real tenants. **Capture is cheap;
not capturing is permanent.** A year we did not record cannot be re-observed. Nothing consumes it in
V1 — that is what makes it feel optional, and why it is listed here instead.

**Not V1:** O8 `active` flip · the §12 **learning** half (capture only — VT-744) · O9 sensing ·
per-specialist billing · owner-owned WABA (VT-286, re-filed as a post-value upgrade) · corpus
authoring of any kind. All valuable. None of them stand between us and one real owner.

---

## CC LANE — the build. Pull order, never idle.

Strict order on 1–3; they share the brain and the send path. 4–7 may ride in gaps.

1. **VT-740 remainder** → E3 + E4. Effect-aware wake (fail-closed: unreadable ⇒ do not wake) ·
   `approval_resume.redrive_task` gated · withheld tasks raise `tenant_alerts` **and** are bounded ·
   diagnosis wired to a VTR surface.
2. **VT-741 remainder** → E3. Verify REPLY attribution against the schema · click tracking
   (extend `hook_links`, token-as-capability, own RLS + FORCE, `_PURGE_ORDER` in the same
   migration) · tiers into `resolve_interval_hours`, **A before B, first match wins**.
3. **VT-738 close** → E2. Gate (c) <5% on a stated denominator ×3, separate counts · gate (d)
   full-pack ×3 with no correctness gate weakened.
4. **B9** → E5. Trial-expiry notify, and the silent-terminal detector. **Coupled to the billing
   state machine** — fix the notify, do not build transition logic on a state machine that the
   pricing supersession is about to change.
5. **VT-739** → E4. The `ctx.run_id` identity fix CC correctly reverted; migration-shaped.
6. **VT-725** → not an exit criterion, but gate **(e) forced-failure degrade** is: an unproven
   degrade path can take down a live turn. Build the call site (shadow), then (e).
7. **B3 package current** → E6, so Fazal's word costs an hour of nothing.

**Then, if all closed:** VT-742 sender resolution + containment · VT-743 prevention at source ·
VT-730 · VT-634's console half.

---

## CODEX LANE — parallel, no dev contention, no infra

**Constraints, unchanged:** separate clone · never merges · never runs either allocator · never runs
migrations anywhere · never touches consoles, secrets or deploys · never sees the sealed evaluation
set. Trust follows the MODEL, not the name — on a lower-category model, briefs stay surgical and
every deliverable gets CC's thorough-review depth.

1. **NOW — the 25 blind answers.** `.viabe/calibration/CODEX-BRIEF-paste-this.md`. **Paste it; do
   not point Codex at the repo** — its clone contains the answers file and blind must mean blind.
2. **Primary-source verification of the 4 India playbooks.** Every rate, fee and legal rule currently
   cites *secondary* sources (BSP blogs, aggregator rate cards). Re-verify each against the primary
   — CBIC for GST, Meta's own pricing documentation, courier published rate cards — and record
   source + date + what it supersedes. **Facts are moat regardless of how the agreement test lands**,
   which is exactly why this lane is safe to run before the result.
   **Priority inside it:** the 1 Oct 2026 change to free in-window messaging. The pricing LEVEL will
   be set against it and my finding came from BSP blogs, not Meta.
3. **Eval-set expansion.** More India-SMB scenarios in the same shape as the 25. The held-out eval
   has been open on Clau's side for weeks and is needed whatever the agreement result says.
4. **Adversarial review of what CC lands** — against the no-drift contract, second pair of eyes,
   report to Clau. Never a gate on CC.

**Explicitly NOT for Codex, and this is a discipline point not a capacity one:** authoring decision
cards or any judgment corpus. That is blocked pending the agreement result — building a corpus before
the measurement is the exact "seed first, measure after" inversion that produced the O11 null. If the
answer comes back ≥20, that work never happens at all.

---

## CLAU LANE — mine, stated so it can be held against me

Score the 25 against the pre-registered prediction and **report where the prediction was wrong as
prominently as where it held.** Audit what CC lands. Keep `GO-LIVE-BLOCKERS.md` provenance-tagged —
four of the original eight were wrong because I wrote status from documents instead of code. Keep
`ROLLING-QUEUE.md` current; it went four days stale once and that is the failure that idled CC.

## The standing rule that governs all three lanes

**Every item must name the exit criterion it serves.** An item that serves none is not V1 work,
however good it is. That rule is the whole reason this file exists — it is what stops a corpus, a
console or an integration from quietly becoming the priority again.
