# Promotion package — evidence for Fazal's word (dev → main)

> Queue item 6. Assembled by CC 2026-08-06 while the gate runs. **Status: INCOMPLETE — one slot is
> open (the ×3 gate verdict). Nothing here is a recommendation to promote; it is the evidence, and
> the word is Fazal's.** Every claim below cites what was observed, and where I retracted something
> the retraction is kept rather than deleted.

## The open slot

**Critical ×3 gate — IN FLIGHT (26 of 79 scenarios at the time of writing).** Every scenario so far
has come back `clean` 3/3. This package cannot be read as complete until that number reaches 79 and
the summary line is recorded here verbatim, including any `BLOCK` or `CROSS-RUN DIVERGENCE`.

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
