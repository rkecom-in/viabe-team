# FAZAL — WHAT NEEDS YOU

> **The ONE list of things waiting on Fazal.** If it is not here, nothing is waiting on him.
> Maintained by **CC** (adds/removes at every status change, same moment as its signal) and
> **Clau** (audits, adds decisions it surfaces). Fazal reads only this file to know his queue.
> Last updated: **2026-07-30 15:00 IST** · dev `f33aa950` · main `62a8b595`

---

## 🔴 BLOCKING — work stops until you act

**B0 — Hand Codex its brief.** `docs/agent-framework/CODEX-BRIEF-2026-08-03-seed-corpus-then-full-ingestion.md`
(pushed with CC's next push). Codex has been idle and now has real work: migration 189 + the
seed ingestion, then the full 118.

**B1 — Place the sealed eval dataset on the Mac, outside the repo.** Clau has authored it
(12 cases, digest `96304705…`, harness-validated PASS, family-isolated). The VT-705 contract
forbids it entering the repo, a branch, or any builder-visible artifact — so it cannot be
handed over through git. Drop the folder somewhere outside `~/development/viabe-team` and tell
CC the path. **This is the single item gating D3 (O8 activation), D7 (promotion bars) and
Codex's VT-723** — and it was Clau's overdue item, now ready.

---

## 🟡 DECISIONS — nothing is blocked, but these shape what happens next

| # | Decision | Context | Who's waiting |
|---|---|---|---|
| D1 | **Meta console: welcome-template UTILITY resubmission** | Package prepared by CC; declined as MARKETING (Twilio 63049). Blocks real onboarding delivery at scale. | Track-A launch item |
| D2 | **Meta console: hi-Latn (Hinglish) template variants** | Approved posture is Latin-script for Hinglish tenants; EN fallback live until you register. | O5 last bar item |
| D3 | **O8 activation flip** — when the Manager starts *using* the knowledge | Engine built + inert. Cards eligible. Needs: my sealed set + baseline first (in progress, mine). | O8 → serving |
| D4 | **VT-231 prod cutover call** | Framework flag promotion rides it. | O7 |
| D5 | **O9 dynamic sensing release** / **O8 learning-loop un-park depth** | Both held by your sequencing. | O0 north star |
| D7 | **Knowledge-card promotion bars** | Separate from D3's flip: how much measured evidence PROMOTES a card to advising tenants, and what demotes it. Clau brings the baseline numbers; the bars are your call. | O8 graduation |

**DECIDED 2026-07-30 — off the list:**

- **D6 owner email → ONBOARDING, both surfaces.** Fazal: "lets get email address from the tenant
  as part of the onboarding form and also in the WA onboarding journey." Overrides CC's
  post-activation recommendation. Rostered **VT-724** (High, CC). Skippable — never gates
  onboarding or activation.
- **VT-721 → ACTIVE on dev.** Fazal: "Go on with VT-721." Flip dispatched to CC.

---

## 🟢 FYI — happening, no action needed

- **VT-718 single emission choke: PROD-LIVE** (enforce, #547 → main). Watch: any false
  suppression on a real tenant → CC flags back to shadow immediately.
- **VT-719 asserted-facts ledger:** stages 1–3 dev-proven (mig 187, canary 9/9).
- **VT-722 enforce-parity: DEV-PROVEN** — canary 6/6 on live enforce + full-pack ×3 clean
  (no new failure classes). The Manager's commitment ledger now fills on every mode.
- **O8 engine:** BUILT + MERGED, inert (default off, 0 retrieval-eligible). #542/#543/#545/#548.
- **Rights gate:** reset per your ruling — 118 cards eligible, originality check replaces it.
- **VT-721 rolling 7-day plan: ACTIVE on dev + PROVEN** — 8/8 chain, owner ask returns a real
  enumerated plan. Two live defects caught and fixed en route (billing-template hijack of the
  bare "plan" token; brain compressing the plan away). Each revision now records a `week_plan`
  assertion, so a changed plan is an owned change.
- **VT-724 owner email: DEV-PROVEN** — both surfaces, consent-record email sending end-to-end,
  DSR scrub canaried. Five defects caught by live canary incl. URL-sniffer email theft.
- **Sealed eval set: AUTHORED + VALIDATED** (Clau, 2026-08-03). Waiting on B1 above.
- **VT-725 O8 SERVING: NEW, Critical.** The knowledge engine was built and **nothing called it** —
  zero references in the agent graph. The consumer row was never written; that is Clau's miss.
  Now rostered, shadow-first (retrieved + logged, never injected).
- **Docs:** the card-assignment model (`manager_global` / `manager_tenant` / `specialist:<agent>`
  / `disabled` + runtime flipping) is now in the CANONICAL tier — ARCHITECTURE §0.1.3,
  manager-objective §0, CLAUDE.md bootstrap. Your "update it everywhere" instruction is closed.
- **CC's queue:** VT-720 → VT-725 → full-pack ×3 → then your promotion.
- **Codex queue:** idle until the baseline lands, then VT-723.

---

## How this file is kept honest

1. **CC updates it at every status change** — same moment as its to-cowork signal. An item that
   needs Fazal and is not here is a process failure, not a memory lapse.
2. **Clau audits it** on every reconciliation against git + rows + signals (Rule #14), and adds
   any decision it surfaces.
3. **Removal requires the action to have happened** — not "probably fine now".
4. Detail lives in the VT rows and the objectives scoreboard; this file is only *what needs
   Fazal*, in his language, with why-it-matters.
