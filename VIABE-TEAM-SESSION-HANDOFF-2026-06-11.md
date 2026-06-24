# Viabe Team — Full Session Handoff (2026-06-11)

**Purpose:** seed a fresh session with the complete state of the Viabe Team delivery effort as of 2026-06-11, end of the 6-gap autonomous product build. Read this top-to-bottom; it replaces the prior conversation's context. After reading, run the reconciliation commands in §15 before trusting any specific HEAD/PR claim (memory is never authoritative — the repo is).

---

## 0. TL;DR (read this first)

- **You are "Cowork" — the delivery captain** for Viabe Team. You orchestrate **CC (Claude Code, the implementer)** via file signals, gate risky work with adversarial subagents, and authorize dev merges. **Fazal is the CEO and makes every final call**; he authorizes all promotions to production.
- **The 6-gap product build is COMPLETE on the `dev` branch.** Six product gaps (the things the original concept promised but hadn't been built) are merged + adversarially gated. 20 PRs this session.
- **The only unbuilt remainder is Gap-5 PR-3 (L3 autonomous customer sends)** — deliberately deferred because its merge is gated on counsel + Meta sign-offs that are weeks out.
- **Customer messaging is FAIL-CLOSED by construction** — three independent structural stops mean no real customer message can send until the external gates open. Safe to keep building/merging.
- **The launch blockers are external, not code:** Meta WhatsApp template approval + counsel DPDP sign-off. Plus a short tracked "pre-promotion" fix list before `dev → main`.
- **Operational reality:** the interactive CC runner saturates on long multi-gap runs; each heavy build eventually needs a long wait or a fresh CC restart. The daemon (currently STOPped) is the structural fix, but Fazal chose to run the interactive loop + restarts.

---

## 1. The four-role model

| Role | Who / what | Authority |
|---|---|---|
| **Fazal (CEO)** | The human. Fazal = VTR#1 (see Gap 6). | All final calls: product, pricing, privacy/legal, scope, launch. **Authorizes every production (`main`) merge** (Pillar 7). Can override anything; when he does, align and move on — no re-litigating. |
| **Cowork (you)** | The delivery captain. | Sprint progress, status reconciliation, briefs, routing work to CC, the adversarial gate, authorizing **dev** merges (Fazal delegated dev). Runs the loop without Clau by default. |
| **CC (Claude Code)** | The implementer. | Code-level design, refactors, tests, bug fixes, builds. Self-merges `[BUILD]` rows to dev on green; risk rows wait for Cowork's gate. Must log decisions for Clau's audit. |
| **Clau (architect)** | Implementation strategy + cross-sprint sequencing. | Audit-AFTER, at sprint boundaries / on request. Was NOT in the loop for this session. |

**Tone Fazal expects:** push back first, agree second, no glazing, lead with what's wrong, be direct/concise, flag a concern once then defer. He makes the call; you align.

---

## 2. What Viabe Team is

A **WhatsApp-first, multi-agent system for small Indian SMBs.** Owner-facing portal at **viabe.ai/team**. The product researches a business, onboards the owner conversationally, builds a plan, and runs specialist agents that do work on the owner's behalf (with the owner approving outbound customer messages).

- **Repo:** `github.com/rkecom-in/viabe-team` (PRIVATE — auth required). Local clone at `/Users/fazalkhan/development/viabe-team`.
- **Monorepo apps:**
  - `apps/team-orchestrator` — Python 3.13, DBOS + LangGraph + Anthropic SDK. The brain/critical path.
  - `apps/team-web` — Next.js 16 / React 19. Webhooks + marketing + dashboard + Ops Console.
  - `apps/team-ingestion-worker` — Python, Phase-1 `SystemExit` scaffold, NOT deployed (platform ingestion actually runs IN the orchestrator).
  - `packages/team-shared` — cross-app types.
- **NOTE — "Viabe Reports" is a SEPARATE project** (its own DB/KG; the Jun-15 "Reports" milestone in the repo `CLAUDE.md` belongs to Reports, not to this 6-gap product build). Don't scope the viabe-team knowledge graph by what Reports needs. The viabe-team KG is the main moat.
- **6-gap product-build launch target:** ~2026-07-15 (the gaps are built on dev; promotion to prod is gated — see §8–10).

---

## 3. Deploy topology + branch governance (CL-432, 2026-06-09)

**TWO long-lived branches, two environments:**

- **`dev`** → Railway **Dev** env + Vercel `viabe-team-dev` + Supabase **Seoul** (`ap-northeast-2`). The deployed, E2E-tested env. **CC self-merges `[BUILD]` rows here on green; Cowork authorizes the dev merge for risk rows after the adversarial gate.**
- **`main`** → Railway **Prod** env + Vercel `viabe-team` (PROD Vercel project still to be created = VT-231 cutover scope) + Supabase **Mumbai** (`ap-south-1`). **`main` merges are Fazal-authorized ONLY** — a `dev→main` promotion PR opens only on his explicit word.

**Flow:** feature branch → PR into `dev` → CC self-merge on green (risk rows: Cowork gate first) → Dev deploy → E2E → **Fazal authorizes dev→main promotion** → Prod.

**CC infra authority (CL-431):** CC has direct console access to Supabase (both), Railway, Vercel. CC manages **DEV** env vars autonomously; **every PROD env-var/secret change needs explicit Fazal authorization first.** Secrets hygiene is binding: CC never writes a live secret VALUE into any repo signal/log/PR (the repo is git — an echoed secret is committed); CC sets in the console and reports only the variable NAME. Supabase PROD creds: Fazal sets the value; CC never reads plaintext but a launched process may consume it from an injected env (`railway run --environment prod … apply_migrations.py`).

**Hard data constraint (CL-422):** NO real customer data on the Seoul dev DB until the Mumbai prod cutover (VT-231) closes.

---

## 4. Source of truth (Notion is a frozen read-only archive — never write to it)

| What | Where |
|---|---|
| Task board / sprint rows | `.viabe/sprint/VT-<N>.md` |
| Session log entries | `docs/clau/entries/CL-<N>.md` |
| Standing decisions | `docs/clau/decisions-ledger.md` |
| Latest snapshot (drifts — treat as hypothesis) | `docs/clau/latest-snapshot.md` |
| Active-context digest | `docs/clau/active-context-summary.md` |
| Launch tracker | `.viabe/launch-tracker.md` |
| WhatsApp template registry | `.viabe/templates.md` (canonical `template_name → SID`) |
| Discipline rules | `docs/clau/discipline-rules.md` |
| Signal protocol | `.viabe/protocol.md` |
| Gap plans (this build) | `.viabe/queue/VT-<N>/plan.md` (e.g. VT-369 457-line plan, VT-370 238-line plan) |
| VT-ID allocator | `python scripts/vt_id_allocate.py` (`--peek` to look, drop it to consume) |
| Migration-number allocator | `python scripts/migration_id_allocate.py --peek` (MANDATORY for new migrations — never hand-pick; CL-424) |

---

## 5. The autonomous Cowork ↔ CC loop

**Signal pipeline (file-based):**
- `.running/to-claudecode/` — Cowork → CC (brief-ready, task, task-merge, review, addendum, answer, question)
- `.running/to-cowork/` — CC → Cowork (task-result, merged, pr-open/pr-ready, question, status)
- `.running/processed/` — archive

**Signal frontmatter:** `from`, `to`, `type`, `ts`, `session_blanket_auth`, `authorized_by: fazal` (only when granted), `authorization_basis: "<quoted directive>"`. Briefs also need `cl_decisions_checked: [CL-N, ...]` (Rule #16).

**How it actually runs:** Fazal grants scope at batch level ("build the 6 gaps," "continue as Gap 1, close the rest"). Within that grant, Cowork dispatches briefs + runs the adversarial gate + authorizes dev merges; CC implements + opens PRs + merges per Pillar-7 task signals. **Self-triggered polling:** whenever CC has open work, Cowork polls `.running/to-cowork/` + `git` continuously — don't wait for Fazal to say "check CC."

**The Cowork delivery method that worked all session (replicate it):**
1. **Plan-first** on anything with real architecture/risk. For sensitive gaps, **run an adversarial SUBAGENT on the PLAN** (not just the code) — it caught real design flaws before a line was built.
2. CC builds (CC runs its own multi-builder + adversarial-verifier workflow; it catches bugs too).
3. **Cowork adversarial-subagent GATE on the built code, with EXECUTED evidence** (run greps/tests/rebuild logic — never trust code-reading or the author's claims; silent-shape bugs only surface on execution).
4. **Authorize the dev merge on the 3 CI gates green.**

**The 3 CI gates (critical nuance):** `ci-success` is an aggregator that **EXCLUDES the `migrations`, `orchestrator`, and `e2e` jobs** (they're flaky-quarantined). So `ci-success` green is necessary but NOT sufficient — for any money/PII/DB row you must confirm the **`migrations`** job (real-PG migration apply) AND the **`orchestrator`** job (the real-DB tests) are green directly. `e2e-playwright` "team-web failed to start" is a known non-gating flake.

---

## 6. Standing disciplines that bit us / matter most

- **Rule #14 — reconcile against ground truth.** Memory is never authoritative; the snapshot drifts. Always `git log` + `gh pr list --state merged` before trusting state.
- **Rule #15 — canary mandatory.** Every brief touching external API/SDK/persistence needs a real canary (real call, verify, fail-not-skip). **CC runs canaries** (its interactive loop has real network egress; Cowork's sandbox is proxy-blocked from vendor hosts and from `api.github.com`).
- **Rule #16 — pre-dispatch ledger scan** (`scripts/check_brief_against_ledger.py`) + `cl_decisions_checked` on briefs.
- **Rule #17 / CL-418 — shared git index.** Single FUSE-mounted working tree shared across Fazal + Cowork + CC. CC uses explicit `git add <files>`, never `-am`, never `git stash -u`. **Cowork must NEVER `git checkout` the shared tree** — it flips the Mac's working tree and strands CC; read via `git show <ref>:<path>` instead. If `git add` fails on `.git/index.lock`, only Fazal's Mac terminal can `rm` it (FUSE blocks the sandbox).
- **Pillar 7 — Fazal-authorized merges to main; owner-approval for customer sends.** Never auto-merge to main.
- **CL-424 — allocate every VT-ID + migration number ONCE up-front via the flock'd allocators before any parallel fan-out; one coherent PR per VT row.**
- **CL-431 secrets hygiene** (see §3). **CL-390 — strip PII at ingest; context-only.**
- **Repeated lessons that became gate probes:** mock cursors hide RLS bugs (require real-DB canaries); IDOR — derive owner/tenant from the target ROW server-side, never a client field; new tenant/PII tables must get RLS+FORCE RLS + go into `dsr_purge._PURGE_ORDER` from the start (DSR purge-order drifts); the authoritative opt-out/DSR matcher must run BEFORE any reply-consuming gate.

---

## 7. THE 6-GAP BUILD — full state (all MERGED to `dev`)

The six gaps are the original product concept's promises that hadn't been built. Each was plan-first → built by CC → Cowork adversarial-gated → merged to dev on the 3 green gates.

| Gap | VT | PR(s) | dev merge | What it is |
|---|---|---|---|---|
| **1** | VT-365 | #446 | `da12922` | **30-day free trial, no card, opt-in subscribe at day 30 (no auto-charge), refund subsystem fully removed.** GSTIN-gated at subscribe. CL-433. (Migration 121.) |
| **2a** | VT-366 | #447 | `93a2f10` | **Auto-Discovery Engine.** At signup, a DBOS bg workflow researches the business across **GBP + its website** (Serper deferred to a fast-follow), assembles an **owner-CONFIRMED draft** (never asserted to KG until confirmed). SSRF-guarded website fetch, cost-ceilinged, DSR-swept, FORCE RLS. (Migration 122, `business_profile_draft`.) |
| **2b** | VT-366 | #448 | `485d427` | **Onboarding question-brain.** `compose_onboarding_questions` — minimal ordered set, confirm-draft-first then true gaps, ≤6 CODE-capped, exclude-known CODE-filtered, fail-soft, bilingual EN/HI. |
| **3** | VT-367 | #449 + opt-out fix #450 | `6f4707b` → `e61148d` | **Guided paced WhatsApp onboarding journey.** One question per turn; a pre-brain intercept (fail-OPEN, journey-active-only, idempotent on redelivery); confirms promote only owner-confirmed fields. **Opt-out/DSR always wins** (the #450 fix — the journey gate was swallowing opt-out keywords). (Migration 123, `onboarding_journey`, FORCE RLS + DSR.) |
| **4** | VT-368 | #451 | `51a97b3` | **The spine: post-ingestion business summary + versioned 6-month roadmap.** Grounded by a cite-from-frozen-fact-bundle anti-hallucination validator (fabricated facts stripped → degrade to template; a hallucination never reaches the owner). Sizes to grounding (no padding). The roadmap is the contract Gap-5 agents execute against + Gap-6 VTR edits. (Migration 124, `business_plan` jsonb-versioned.) |
| **5 PR-1** | VT-369 | #452 | `ba54a00` | **Agent framework + Sales Recovery agent (L2-only).** One send choke point (`agent_send_draft`); the agent is tool-guardrailed away from any sender (structural, not convention). **Three independent structural send-stops** (see §9). + 2 adjacent VT-45 send-path fixes (complaint gate, freeform opt-out). (Migration 128.) |
| **5 PR-2** | VT-369 | #453 | `7fdc53b` | **Autonomy substrate (L2/L3), ZERO auto-send.** Per-(tenant,agent) state, ungameable clean-streak counter, regression table, **atomic kill switches** (freeze cancels open incl. awaiting_approval on the same txn), `is_frozen` fail-CLOSED. Opt-out attribution SAVEPOINT durability fix folded in. (Migration 129, `tenant_agent_autonomy`.) |
| **6** | VT-370 | #454 | `fb402fe` | **VTR Ops-Console surface.** VTR edits/enhances the plan (`edit_roadmap_item`, re-grounds + CAS-versioned) and corrects/freezes agents. **DB-enforced PII boundary** (a role with zero raw-table grants; 5 views are the only door; `diff_from_prev` VALUES stripped). **Net-new IDOR `require_vtr_action` gate** (no-JWT→403 even with valid internal secret; assignment-checked; 5-min TTL). Fazal=VTR#1 exception tier (audited reveal). (Migration 130 + `app_vtr_role`/`app_vtr_admin_role`.) |

**Migrations confirmed this build:** 121 (Gap1), 122 (Gap2a), 123 (Gap3), 124 (Gap4), 128 (Gap5 PR-1), 129 (Gap5 PR-2), 130 (Gap6). (Always confirm the next number via the allocator — never hand-pick.)

---

## 8. The remainder: Gap-5 PR-3 (L3 autonomous send)

**NOT BUILT — deliberately deferred to last.** Reasoning (CC's, accepted): its merge is gated on counsel + Meta sign-offs weeks out; building it now would rot a long-lived branch against dev and conflict with Gap-6's surfaces. Build it **fresh** when its preconditions are near, against the final surface.

**PR-3 scope:** the L3 auto-send wire, the L3-grant proposal/opt-in/notice flow, the F6 "silence-proceeds" pre-send notice window (any owner inbound demotes to L2 — needs a row-lock/CAS so a window-expiry send can't fire over an in-flight objection), the **owner kill-keyword detection** (the substrate freeze exists; the owner-inbound reply-parsing to trigger it is PR-3), and the **winback template-signature fix** (executor `WINBACK_TEMPLATE_PARAMS` must match the registry's `team_winback_simple` variables before the first real send, else it hard-refuses).

**PR-3 merge preconditions (all external):**
- **C1** (counsel): DPDP lawful basis for agent-initiated marketing under the processor model.
- **C2** (counsel): which consent_text_versions cover automated win-back → until set, `MARKETING_CONSENT_VERSIONS` stays an empty frozenset (structurally zero send candidates).
- **C3** (counsel): L3 opt-in sufficiency.
- **F1** (Fazal/Meta): Meta approval of the 5 customer-message templates (EN+HI, MARKETING category) — the longest pole.
- **F3** (Fazal): autonomy threshold sign-offs.
- **F6** (Fazal): the silence-equals-proceed inversion sign-off.
- Plus a fresh Cowork adversarial gate on the as-built PR-3 diff.

---

## 9. Why it's safe to keep building: customer messaging is FAIL-CLOSED

No real customer message can send until the external gates open, guaranteed by **three independent structural stops**, any one of which blocks alone:
1. The agent's send path (`agent_send_draft`) has **no production caller wired** to fire it autonomously (L3 is `NotImplementedError`).
2. `MARKETING_CONSENT_VERSIONS` is an **empty frozenset** → zero send candidates (`= ANY(empty array)` is always false).
3. The customer templates have **null Meta SIDs** → fail-closed `template_not_yet_approved` refuse (no free-form fallback).

Flipping any ONE without the others still can't send. Both F1 (SIDs) and C2 (consent versions) must flip together to arm customer messaging — and each flip must land with a CL ledger entry + a re-run of the gate canaries.

---

## 10. Tracked PRE-PROMOTION list (must close before `dev → main`)

These are NOT dev blockers (dev has no real owners/customers) but MUST close before production:
- **Gap-4 grounding-validator gaps:** the **sentence-leading proper-noun escape** (a fabricated platform/competitor name at sentence start passes) and **`text_hi` (Devanagari) proper-noun blindness**. Close both before any real owner sees a plan.
- **Gap-6 Devanagari validator blindness** — acceptable now (VTR#1 = Fazal, trusted exception tier); **reopen before onboarding a 2nd (non-Fazal) VTR**, same trigger as the multi-VTR view-scoping precondition.
- **F1/C2 arming discipline** — each flip = a CL ledger entry + a re-canary.
- **VT-231 prod cutover** — create the prod Vercel `viabe-team` project + Mumbai prod wiring (pre-existing launch blocker).
- Non-blocking fast-follows: Serper as an Auto-Discovery source + an intra-source cost cap; the orchestrator `require: ["exp"]` JWT hardening (already folded into Gap-6 pre-merge — verify).

---

## 11. Bugs the adversarial gate caught this session (the rigor record)

The plan-first + adversarial-subagent-gate method caught ~10 real bugs that would otherwise have shipped, several compliance-critical:
- **Two opt-out-swallow gaps** — the Gap-3 journey intercept AND the pre-existing live approval gate consumed opt-out keywords ("stop"/"cancel") as rejections instead of routing to the authoritative DSR/opt-out handler. Both fixed.
- **SSRF** on the Auto-Discovery website fetch (could hit cloud-metadata/RFC1918) — CC caught + guarded.
- **Missing FORCE ROW LEVEL SECURITY** on the new `business_profile_draft` PII table — Cowork's subagent caught.
- **Silently-broken KG emit** — `confirm_draft` called `drain_kg_events()` without `tenant_id` (promotion worked, emit silently failed).
- **Composite-FK bug** that would have **permanently rolled back DSR purges** for any tenant with an agent approval (`SET NULL` without a column list nulled `tenant_id`).
- **Dead arm path** (an approval-arming fn missing from the registry).
- **Opt-out durability** — attribution ran on the opt-out txn with no SAVEPOINT, so a server-side SQL error could roll back a real opt-out. Fixed with a SAVEPOINT + a canary that forces the failure.
- **Gap-4 "every clean generation silently degrades to template"** (inline citation markers) — CC's verifiers caught.

Lesson reinforced: **the adversarial gate must EXECUTE (greps/tests/isolated-logic), not read.** And on sensitive gaps, gate the PLAN too — that's where the opt-out-reintroduction and the unsafe-authz-precedent-inheritance were caught before any code.

---

## 12. Operational realities

- **CC saturation is the binding constraint.** The interactive `claude -c` runner accumulates context and saturates on long multi-gap runs — it goes quiet/stalls on heavy builds, sometimes for very long stretches (often it comes back; sometimes it needs a fresh restart). The **daemon** (`.viabe/daemon/`, spawns a fresh CC per signal = fresh context per task) is the structural fix but is **STOPped** (`.viabe/daemon/STOP`); enabling it is a Fazal action (must stop the interactive CC first — they race the inbox). Fazal chose to keep the interactive loop + restarts ("continue as Gap 1"). **Pattern that works:** CC honest-checkpoints when saturating (a clean WIP commit), then a focused continuation (often a fresh CC) finishes it; the work-in-progress lives in the shared FUSE tree, never lost.
- **Don't false-alarm on CC quiet.** This session I called "stall" several times and was wrong — CC was doing careful hand-builds or sitting in a foreground `gh pr checks --watch`. Distinguish: check whether the inbox drained, whether a branch/commit advanced, and whether it's mid-CI-watch before concluding it's wedged.
- **Branch protection:** main is an account-level ruleset; "require status checks" was turned OFF (VT-245) — the local **pre-push hook** (`scripts/install-hooks.sh`) is the real gate; CI is a non-blocking backstop. Trigger-diet: docs/sprint/session changes run 0 CI jobs.
- **gitleaks** runs in CI; even secret-shaped TEST literals trip it — rename + (if needed) rewrite the feature branch's history to a clean commit before merge.

---

## 13. Key infra / founding state

- **Founding tenant** (Fazal's own business entity) provisioned in **Mumbai prod** Supabase; owner UUID match-verified. Fazal = **VTR#1** (the exception tier in Gap 6).
- **Supabase connection gotcha:** the direct connection (`db.<ref>.supabase.co`) is IPv6 → unreachable from Railway (IPv4-only). Use the **session pooler** (`aws-0-ap-south-1.pooler.supabase.com:5432`).
- **Verified-live integrations:** Sandbox-by-Quicko GSTIN verification (key in `.viabe/secrets/sandbox.env`); DBOS Conductor (VT-161, `.viabe/secrets/dbos.env`); Apify ingestion (GBP/Swiggy/Zomato actors, async); Razorpay test plans; email deliverability (DNS verified).
- **Migration guard (VT-362):** `apply_migrations.py --expected-env <dev|prod>` + an `app_environment` sentinel; refuses on env mismatch; never prints the DSN.

---

## 14. The Sundaram Book Store journey (product narrative, for grounding)

The concrete end-to-end the six gaps now enable (Sundaram Book Store = the canonical Bangalore-bookstore persona):
1. **Signup (web):** name, "bookstore," Bangalore, WhatsApp number; OTP-gated; 30-day trial starts, no card (Gap 1).
2. **Auto-Discovery (Gap 2a, background):** pulls Sundaram's GBP (category/city/rating/website) + fetches the website → Haiku extract → an owner-confirmed *draft* (nothing asserted yet).
3. **Guided chat (Gaps 2b+3):** a welcome template opens WhatsApp; the question-brain composes a short ordered set — confirm-the-draft first, then real gaps — delivered one at a time, bilingual; confirmed facts promote to the KG; opt-out always wins.
4. **Summary + 6-month plan (Gap 4):** on journey completion, a grounded business summary + versioned roadmap are generated (no hallucination; sized to the facts) and delivered paced over WhatsApp. The owner asked for nothing — it just arrives.
5. **Agents act (Gap 5):** the coordinator dispatches roadmap items; the Sales Recovery agent drafts personalized win-back messages for lapsed customers and **queues them for owner approval (Pillar 7)** — owner approves/edits/rejects; only approved messages send; L3 auto-send is earned (20 clean approvals) + owner opt-in. **← currently fail-closed: drafts + approval flow run end-to-end, but the actual send is held until Meta+counsel gates open.**
6. **VTR oversight (Gap 6):** Fazal (VTR#1) can edit the roadmap or correct/freeze an agent via the Ops Console; customer PII is walled off at the DB layer.
7. **Day 30 (Gap 1):** trial-end nudge; no auto-charge (no card); owner opts in to subscribe (GSTIN-verified) or lapses to a re-engagement path. No refunds.

**Honest caveat:** plan + draft quality is only as good as the ingested data. For a thin-GBP bookstore with no transaction history yet, the first plan is modest and Sales Recovery has nothing to act on until real customer/transaction data is ingested. The scaffolding is all there; it sharpens as data accumulates.

---

## 15. Startup reconciliation (run before trusting any state claim)

```bash
cd /Users/fazalkhan/development/viabe-team
git fetch origin -q
git log --oneline origin/dev -12          # confirm the gap merge commits in §7
ls -lat .running/to-cowork/ .running/to-claudecode/   # any open signals?
gh pr list --state open --limit 10        # PR-3 not opened yet; nothing else expected
git branch -r | grep -iE 'vt369|vt370'    # gap branches
```
Read, in order: `CLAUDE.md`, `docs/clau/latest-snapshot.md` (treat as suspect — regenerate if it disagrees with `git log`), `docs/clau/decisions-ledger.md`, `docs/clau/active-context-summary.md`, `docs/clau/discipline-rules.md`. Then this file.

---

## 16. What to do next (the open decisions, all Fazal's)

The buildable set is closed; the loop is paused by design. It re-activates on any of:
- **Start the external gates in parallel** (they're the long poles): the **Meta WhatsApp template submission** (5 templates, EN+HI, MARKETING) and the **counsel DPDP conversation** (C1/C2/C3). When these near completion, dispatch **PR-3** to a fresh CC against the final surface.
- **Promotion prep:** close the §10 pre-promotion list so the `dev→main` path is clean, then Fazal authorizes the promotion PR.
- **VT-231 prod cutover** (Mumbai prod Vercel/Supabase wiring).
- Any new scope Fazal grants.

There is no in-flight build to monitor right now; CC is idle and holding cleanly. Don't continuously re-poll an idle CC — wait for a scope grant or a precondition to land, then re-engage the loop.

---

*End of handoff. Generated by Cowork, 2026-06-11, at the close of the 6-gap autonomous build. Verify specifics against the repo (§15) before acting — memory is a starting hypothesis, the repo is ground truth.*
