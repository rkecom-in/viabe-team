# PROMOTION PACKAGE — dev → prod (M3)

**Status:** ASSEMBLED, one input outstanding (gate (f), pack in flight). **Not a request to promote
yet** — it becomes one when the gate (f) line below carries numbers instead of a placeholder.
**Authorization:** `main` is Fazal-only. Fazal's word IS the authorization (Pillar 7); CC executes.

---

## 1. What is being promoted

`origin/dev` at **`b16be8b7`**. The substance is VT-725 shadow card-serving actually working, VT-749's
scoped corpus (migration 208), VT-742 §1 sender resolution, VT-764's guard correction, plus the
instrument and migration fixes made while proving all of it.

**Nothing in this promotion injects anything into a prompt.** `CardServingResult.INJECTS_INTO_PROMPT`
is a `ClassVar[bool] = False` and the serving seam asserts it before returning. Shadow retrieves,
scores and records attribution; the flip to injection is a separate code change gated on D3 and on
Fazal. Promoting this does not promote that.

## 2. Gate (a) — CLOSED on deployed evidence

Proven twice, the second time with controls designed to make a null result interpretable:

| control | value |
|---|---|
| deployed image digest | `sha256:0e9a1eaf…` (≠ pre-fix `725ebd46…`) |
| baseline `manager_turn:` rows | 400, 2 decision_ids |
| after | 800, 4 decision_ids |
| new decision_ids | `SMharnessbe276e84…`, `SMharness45e47795…` |
| sids in that run's own transcript | the same two — **1:1 match** |
| tenant alive at query time | yes, both — an absence would have been visible |

Retrieval outcome per deployed turn — cards were **selected**, not merely retrieved:
`SMharnessbe276e84…` 100 retrieved / 92 rejected / **8 selected** · `SMharness45e47795…`
100 / 99 / **1 selected**.

## 3. Gate (f) — **CLOSED. 128/130 clean, 0 new failure classes.**

Ran to completion on the fixed build: `=== summary: 130 scenario(s), 2 finding(s), 0 domain-floor
gap(s) ===`. **128/130 clean — exactly the 128/130 baseline.** The prediction was recorded in the row
BEFORE the run ("no change against baseline"), so it could not be fitted afterwards, and it held.

**Gate (f)'s actual test is "no new failure class," and it passes on evidence, not on the rate:**

| finding | non-clean in M2? | verdict |
|---|---|---|
| `m_conversation_multi_request_mixed_ask` | **yes — all 3 passes** | pre-existing class |
| `routing_dual_intent_connect_and_winback` | **yes — all 3 passes** | pre-existing class |

**New failure classes vs the M2 baseline: NONE.** Both survivors are the same class — a message
carrying two or three intents where the win-back half is dropped (`route='none'`, no campaign row).
`routing_dual_intent_connect_and_winback` is the **R1** already documented in §4's blast-radius table;
it is unchanged, not introduced.

**The result that outruns the gate:** M2 had **20** non-clean scenarios; this build has **2**. The
mechanism that dominated M2 — 16 of those 20 answering *"I can't build this yet — something I need
from your data is missing"* and never delegating — **is gone**. The pack's live output shows real
Sales-Recovery delegation instead: `route: sales_recovery`, a grounded draft naming a cohort, a date
window and a ₹ range, then an approval ask. §4's composition analysis was measured on the pre-fix
build and is now conservative rather than current.

**Nothing is injected**, which is what makes a clean result here meaningful: a regression would have
meant something WAS reaching the prompt. Nothing did.

## 4. Blast radius of the M2 pack's 9.5% — **R0 = 0**

Full per-run table: `.viabe/calibration/M2-BLAST-RADIUS.md`. Recomputed from the pass1/2/3 artifacts:
390 runs, 353 clean, 37 non-clean = 90.513%.

> **0 customer-visible harms in 390 runs. No customer was messaged at all in any failing run.**

Three independent checks, not an absence of complaint: every one of the seven distinct failure-
assertion kinds has the shape *expected X, found none*; the only send-count assertion present is an
**under**-send; and no reply claims a send occurred, under a detector validated on six positive
phrasings first. **R1 = 5** (a false "still working on that" when nothing was in progress; a
dual-intent message whose win-back half is silently dropped, stable 3/3). **R2 = 32.**

**Honest scope limit:** that pack ran on the pre-fix build. It is a measured statement about those
390 runs, not a claim that the system cannot cause customer-visible harm.

## 5. Migrations for prod

Repo holds through **208**; counter at 209. `apply_migrations.py` is idempotent, applies in order,
and **refuses to run unless the connected DB's `app_environment` sentinel matches `--expected-env`**
(VT-362 guard). The prod-applied set is confirmed BY THE APPLIER at promotion time, not asserted here
— asserting it from dev would be exactly the kind of claim this package exists to avoid.

Run shape (value flows OS-env → process, never into CC's context, per CL-431):

```
railway run --environment production --service vt-orchestrator-service -- \
  uv run --no-project python apps/team-orchestrator/scripts/apply_migrations.py --expected-env prod
```

**This run is itself Fazal-authorized** (prod-impacting). **Migration 208 note:** it now skips cleanly
with a NOTICE when the v3 corpus is absent, and still fails closed when a corpus IS present and does
not match. On prod, whichever branch it takes is the correct one and the applier reports which.

## 6. Prod `VOYAGE_API_KEY` — verify BY BEHAVIOUR, never by presence

Fazal has set it. **Do not verify by reading presence** — sealed vars read `unset` from the CLI
regardless, and a presence read is void under Rule 18 anyway. The dev incident is the precedent: the
variable's existence on the console told us nothing; the running process lacked it, and only a log
line from inside the deployment revealed that.

**Acceptance:** after promotion, one real prod turn writes `decision_evidence_links` rows with a
`manager_turn:` decision id. Until that row exists, prod serving is **unproven**, exactly as dev's was
while the row said LIVE.

## 7. Rollback

`TEAM_KNOWLEDGE_SERVING` unset on prod → `knowledge_serving_mode()` returns `off` → the seam costs one
env read per turn and touches no database. **That is a variable change, not a deploy**, and it is the
whole rollback for the serving half. Migrations are additive (a new corpus version and new card
versions; `knowledge_cards` rows are immutable by trigger), so the v3 corpus remains intact and
reconstructible — rollback does not require reverting 208.

## 8. What this package deliberately does NOT claim
- That prod serving works — §6 is an acceptance step, not a result.
- That the 9.5% composition holds on the fixed build — §4 is scoped to the pre-fix pack.
- That injection is safe — nothing here injects; that is D3 and Fazal's, separately.
