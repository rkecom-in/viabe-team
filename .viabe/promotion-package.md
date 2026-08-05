# Promotion package — evidence for Fazal's word (dev → main)

> Queue item 6. Assembled by CC 2026-08-06 while the gate runs. **Status: INCOMPLETE — one slot is
> open (the ×3 gate verdict). Nothing here is a recommendation to promote; it is the evidence, and
> the word is Fazal's.** Every claim below cites what was observed, and where I retracted something
> the retraction is kept rather than deleted.

## The gate result — COMPLETE, and read it with its caveats

**All 79 critical scenarios covered ×3.** Verbatim summary line:
`=== summary: 59 critical scenario(s), 16 block(s) ===`

Three things that line does NOT say, which belong next to it:
1. **It was a RESUMED run, not one continuous pack** — the runner says so itself: *"20 scenario(s)
   came from a PRIOR segment and were not re-driven here."* 59 driven in the final segment + 20
   reused = 79. Reported as resumed because it was.
2. **"16 blocks" = 10 step-level failures + 6 cross-run divergence entries**, spread over **6
   distinct scenarios** — not 16 broken scenarios.
3. **All 6 share ONE root cause:** the Sales-Recovery delegation miss (queue item 2, deferred by
   Fazal). `second_plan_queue_busy` 3/3 · `sr_l2_owner_approval_gates_send` 2/3 ·
   `sr_second_plan_queue_busy` 2/3 · `sr_owner_cannot_bypass_approval_by_asking` 1/3 ·
   `routing_db_proof_finance_vs_sr` 1/3 · `plan_queue_status_check_while_pending` 1/3.

**Measured delegation rate:** 34 turns delegated (`route=sales_recovery`) against 11 honest-failure
replies across the run — and `sr_second_plan_queue_busy` proves it is genuinely non-deterministic:
identical seed (8 customers), identical message, `route` diverged `['none','sales_recovery','none']`
with the successful run grounding 8 customers and correctly reaching `paused`.
I corrected my own estimate TWICE here: first as "~1-in-3" (understated), then as "~40% miss"
(sampled from inside the SR-heavy alphabetical cluster, overstated). The honest statement is the
divergence itself, not a single rate.

### What a clean gate would and would NOT have proven
**Every money-adjacent scenario that blocked did so UPSTREAM of its safety assertion.** In
`sr_owner_cannot_bypass_approval_by_asking` the manager never claimed an autonomy change; in
`sr_l2_owner_approval_gates_send` there was no campaign to send. So the delegation defect MASKS
safety assertions rather than violating them — meaning this gate measures reliability on those paths
more than it measures safety. That is a real limit on what the pass proves, and Fazal should have it.

**Across all 79 × 3: zero false claims, zero unapproved sends, zero new failure classes.**

Also unfinished, and deliberately NOT blocking the gate: the sealed no-O8 baseline (queue 4) and the
VT-725 flip canary (queue 5).

## What changed since the last promotion window

### VT-732 — model governance (DEV-PROVEN)
Every model choice now comes from the env. The finding that started it: all five `TEAM_MODEL_*` vars
read `gpt-5.6-luna` on dev while the bill said Sonnet.
- **~30 call sites** ported off direct Anthropic clients onto the tier seam.
- **`config/models.yaml` was a SECOND governance surface** neither audit had: 9 `VIABE_ENV`-slotted
  pins, `sales_recovery`'s dev slot being `claude-sonnet-5` — every SR draft. Retired onto tiers.
- **Boot proof on deployed dev:** `llm tier conformance: classifier=gpt-5.6-luna,
  complex=gpt-5.6-luna, review=gpt-5.6-luna, routine=gpt-5.6-luna, specialist=gpt-5.6-luna`.
- **Ledger proof:** `llm_call_events` for the drive tenants is 100% `gpt-5.6-luna`, zero Claude.
- **Regression tripwire:** `gate-no-model-literals` in `ci.yml` (inside `ci-success`'s needs).
- Two latent production defects surfaced by the port and fixed: `compute_cost_paise` raised
  `KeyError` on any non-Claude model (an SR run would have died at cost attribution, AFTER the
  spend), and the `ANTHROPIC_API_KEY?` guards answered "no key" on a gpt-tiered box, silently
  disabling working paths.
- One more found live: on a reasoning model a small `max_tokens` cap returns NO text (the cap covers
  reasoning). Our caps were Anthropic-sized — 60, 16, 10. Floored at 1024 for openai/xai.

### VT-734 — approval breach (DEV-PROVEN, ×3)
An owner's repeated request resolved a `campaign_send` approval created **72 seconds later**, and 19
customers were really messaged. Both halves of Fazal's ruling built: an ordering invariant at the
single resolution choke point (fails CLOSED) and a repeat-of-request content rule ahead of every
classifier. Re-proof ×3: approval `pending`, campaign `proposed`, **0 sent** — versus
`approved`/`sent`/**19**.
**Retraction kept:** I first reported a third defect ("claimed a send with zero campaign_messages").
Wrong — the rows exist; I joined a column the send path never writes. The manager told the truth,
which makes the incident worse, not better.

### Measurement integrity (this is why earlier numbers were wrong)
Three runners defaulted to a **90s** step deadline while the product's own in-turn wait is **~96s** —
so any turn whose async task did not answer fast was recorded TIMEOUT *by construction*. Fixed in
`run_critical_x3`, `convo_harness` and `run_full_pack`. The bulk-send "defect" that had blocked the
chain for a week was mostly this.

## PROMOTION-BLOCKING — found 2026-08-07, after the gate. Read this before deciding.

### 1. The concurrency wedge is REAL, deterministic, and live on dev. I retract "unknown".
I reported the wedge as UNKNOWN-for-the-pack because the harness reaps tenants before
`manager_tasks` can be queried. That was reporting the limit of ONE method as if it were the limit of
all of them — the code and the database both answer the question.

**The chain, every link verified in source:**
1. Any prereq/policy/limit failure → `_block_*` (`manager/workflow.py:446/473`) sets
   `status='blocked'`, `terminal_outcome='escalated'`, and arms **no `next_retry_at`**.
2. The owner gets the honest closure *"I couldn't complete it on my own — so I've stopped"*
   (`owner_surface/task_outcome.py:266`). Correct and honest — and the tell.
3. `blocked` ∈ `TASK_ACTIVE` (`task_store.py:44`) → the row **holds the tenant's one active slot**.
4. `workflow.py:1421` promotes the queue only `if final_status in TASK_TERMINAL`. **`blocked` is not
   terminal** — nothing behind it is ever promoted.
5. `orphan_reaper.py:189-199` wakes `blocked` rows **with** an elapsed `next_retry_at`; one
   **without** is, in the code's own words, *"left for a human."*

**Live on dev, real rows, not the harness:** 5 tenants hold a `blocked` + no-retry slot — 3 via the
`escalated` honest-closure path. **Newest 9 days old, oldest a month.** Nothing woke them.

**Effect:** the tenant can still chat — the turn-brain answers — but **no task of any kind will ever
execute for them again**, and the manager tells them work is "already in progress". Silent,
permanent, invisible to the owner, and it looks like the product working.

**Fazal's deferral rested on "not reproducible post-fix (0/2, was 1/2)" — a rare race. That premise
is falsified.** The entry is the ordinary honest-failure closure, our most common failure path.
Proven boundary: slot-holding is proven; queue-starvation behind it is mechanically implied by
`workflow.py:1421` but not directly observed (no dev tenant currently has both).

**Fix, not built — deferred by Fazal and his to un-defer:** `dead_letter` is documented at
`task_store.py:36-37` as *terminal but operator-redrivable; never auto-retried* — precisely what an
escalated block IS. Settling it there releases the slot, promotes the queue, keeps ops redrive.

### 2. The knowledge corpus was RLS-invisible to the application. Fixed (mig 196, `9a39b48b`).
Eight O8 tables had `ENABLE ROW LEVEL SECURITY` with **zero policies** = deny-all for any non-owner
role. `app_role` read **0** rows where `postgres` read **182**. The curated corpus had never been
readable by the application on ANY environment — so VT-725's "the engine is built and nothing calls
it" was only half the gap: even once called, it would have returned nothing.

Migration 196 adds SELECT-only policies `TO app_role` (writes stay closed — an agent must never
author its own knowledge; `TO public` refused because Supabase's default grants would have published
the corpus through PostgREST, so REVOKEs were added). **Prod carries the same defect** — all twelve
O8 tables exist there and are empty, so the policy gap ships with them unless 196 goes too.

**VT-725 exit gate (d) — specialist narrowing — now PASSES on dev, positively:** manager 64
candidates, `sales_recovery_agent` 25 (its lane only), with other-domain cards present so a leak had
somewhere to show. Not an empty-result pass.

### 3. D3 is NOT takeable on evidence: no card can clear the retrieval floor.
Manager `minimum_score = 0.62`. Hold every scoring component at its measured value and set semantic
similarity to a **perfect 1.0**: ceiling **0.5407**. Mark every card universal too: **0.6127**, still
short. Universal AND recency restored: 0.6427, clears. **Best actually observed: 0.270.**

Two causes, both metadata/design rather than code: every card has `applicability_universal = false`
with empty industries/size_bands/maturity_stages (maximum unknown-dimension penalty → applicability
**0.10**), and `_recency()` returns **0.0 by construction** for evergreen cards. Deciding which cards
are "universal" is a claim about the knowledge; moving the floor is a design call. **CC did not tune
the gate to make an answer appear.**

**Correction to an earlier claim in this document:** the VT-727 canary PASS is real but narrower than
it reads — its one retrieved card came from a **self-query** (it embeds a card's own text as the
query). It proves persistence and engine mechanics, not that retrieval answers a business question.

### 4. The O11 treatment arm did not exist.
`--knowledge-mode` was only RECORDED in the output bundle and never reached the prompt, so a
"treatment" run would have been the baseline wearing a different label and any lift computed from the
pair would have been sampling noise. Real retrieval built (`canaries/o11_knowledge.py`, `6f5b75e6`)
with instrumentation that states outright when an arm injected nothing. **Sealed baseline: DONE,
12/12, real.** Sealed treatment deliberately NOT run while the floor blocks injection — spending the
sealed set on a run that injects zero cards would manufacture a number, not measure one.

## Known-open, stated so promotion is a decision and not a surprise
1. **Latency residue.** On the first turn of a slow job the owner waits ≥96s before the honest ack.
   The candidate fix (make the D1 budget a deadline from turn start) is written up and deliberately
   NOT applied blind — T9 inc-3 set that constant for a measured reason.
2. **Concurrency wedge — deferred with a written case.** Not reproducible post-fix (0/2, was 1/2);
   a `blocked` task is non-terminal and holds a tenant's one active slot permanently, so if it
   recurs it wedges that tenant. The pack is the larger sample; it will be reported either way,
   including if it does not appear.
3. **Prod env decisions on Fazal:** `TEAM_ENABLE_WEB_SEARCH` (dev is set; identity adjudication runs
   search-less without it) · prod `TEAM_MODEL_*` values, since `models.yaml`'s prod slots no longer
   apply · prod cap values (there are NO ceilings configured anywhere today) · the VT-35 10k agent
   token budget, which a reasoning model exceeds immediately on the legacy path.
4. **Cost visibility ships behind the gate**, not with it: VT-733 A complete, B partial (Twilio +
   Sarvam wired; Voyage/Apify/ScrapingBee need `tenant_id` threaded to their seams).

## Promotion mechanics (unchanged, Pillar 7)
`main` is Fazal-authorised ONLY. A `dev`→`main` promotion PR opens on his word, relayed by Clau. CC
never merges to `main`.
