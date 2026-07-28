# Codex brief — 2026-07-28: history rewrite (act first) + PR #542 bounce fix

Issued by Clau. Two items, strictly in this order. **Do the re-root before touching any code —
your local branches are on dead history and any push will be rejected.**

---

## 1. GIT HISTORY WAS REWRITTEN (Fazal-authorized) — re-root your clone FIRST

`archives/` was purged from **every commit** on `dev` and `main` with git-filter-repo (193MB →
22MB pack); both branches were force-pushed. Tip trees are byte-identical outside `archives/`,
so no code content changed — but every SHA did. Branch protections were restored after the
rewrite, so a stale-history push fails loudly (non-fast-forward) rather than corrupting
anything.

**Do this in `viabe-team-codex` before anything else:**

```bash
cd /Users/fazalkhan/development/viabe-team-codex
git fetch origin --prune
git checkout -B dev origin/dev            # re-root the base branch
```

Then re-home each of your branches:

- **`codex/vt-709-o8-contracts-registry`** — CC already re-homed it on the remote. Your local
  copy is stale. Reset to origin's version, do NOT rebase your old local commits on top:
  ```bash
  git fetch origin
  git checkout codex/vt-709-o8-contracts-registry
  git reset --hard origin/codex/vt-709-o8-contracts-registry   # NEW head fc8c7ca0 (was e92dd813)
  ```
  This is what collapsed PR #542's diff from 3,460 phantom files to the real 42.
- **`codex/vt-710-o8-ingestion-retrieval`** — rebase onto the rewritten `dev` (or cherry-pick
  your commits onto a fresh branch from `origin/dev`). Re-open/refresh PR #543 after.
- **`codex/gstr1-readiness`** (fe5c01b8) — same treatment when you next touch it; it can wait.

If a rebase produces a diff that looks enormous, stop — that means you're still on old
history. Re-root and retry rather than resolving phantom conflicts.

**Corpus note:** `archives/business-knowledge/` files are SAFE locally but are now **untracked
and gitignored — never re-commit them**. Your conversion pipeline reads them as local input
only; the derived candidate/rights JSONL artifacts that live under version control are
unaffected.

---

## 2. PR #542 (VT-709 checkpoint A) — BOUNCED on one real defect

Everything else in the review passed (migrations verified written-not-run against the live dev
DB, DSR canary asserting physical zero rows with a co-resident tenant surviving, inert boundary
intact, static purity checks green, 283 focused + 212 realdb tests). **One blocker:**

Your own test `test_vt709_card_versions_immutable_lifecycle_append_only_and_rights_removal_preserved`
fails on a **fresh** Postgres:

```
psycopg.errors.RaiseException: knowledge_lifecycle_events is append-only (VT-709); UPDATE blocked
CONTEXT: SQL statement "UPDATE ONLY knowledge_lifecycle_events SET card_id = NULL WHERE …"
```

Two mechanisms you built in migration 183 fight each other: the append-only trigger blocks the
`UPDATE` that the `card_id` FK's `ON DELETE SET NULL` performs. **Consequence: rights-removal
(card hard-delete) is impossible** — which is precisely the path the 96 `unknown`-rights cards
may need.

**Fix — your choice of approach**, both acceptable to CC:
- permit the FK-driven update in the trigger (e.g. allow when `pg_trigger_depth() > 0`, or when
  the *only* changed column is `card_id → NULL`); or
- drop `ON DELETE SET NULL` in favour of an explicit tombstone step inside the purge path.

**Before resubmitting: run the migrations from zero on a clean local Postgres.** The "real-PG
canaries green" claim didn't hold on a fresh DB — that's the gap to close in your own gate, not
just this one trigger.

**Non-blocking follow-up (note it, don't fix now):** an end-to-end global-purity test — seed a
tenant identifier, run ingestion + prior promotion, assert it appears in no global text field.
`assert_global_payload_pure` exists and is exercised statically; the e2e version is missing.

---

## 3. Sequence from here

1. Re-root (§1). 2. Fix the trigger conflict, migrations-from-zero green, push to the re-homed
`vt-709` branch → #542 re-review. 3. Rebase the `vt-710` branch; #543 stays stacked and is
reviewed after #542 merges. 4. Then **checkpoint C (VT-711)** — learning loop, k-gated priors,
admission/ablation machinery, rollout modes default-off — per `CODEX-O8-PROGRAM-BRIEF.md`.

Unchanged: no migrations run anywhere, nothing activated, no allocator use (numbers come from
Clau; 184+ unallocated), egress canaries authored-not-executed (CC runs them).
