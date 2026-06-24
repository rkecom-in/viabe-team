# Latest State Snapshot

**As of:** 2026-06-24. **dev HEAD:** `5e82d37` (VT-387 idempotency fix; #488). **main HEAD:** `2de4b36` (#436 — UNCHANGED; Fazal-only promotion per CL-432). **BINDING Team go-live: 2026-07-15.** Branch governance per CL-432 (`dev` staging / `main` Fazal-authorized promotion only).

> Reconciled against `git log origin/dev` (Rule #14). The Run-Control batch (CL-435/436) shipped long ago — the prior 2026-06-12 snapshot was ~12 days stale and is fully superseded by this one.

---

## CRITICAL PATH

**The verify-then-create owner-signup e2e is LIVE on dev — Fazal to run the Sundaram e2e.**

The "get everything in place before we restart the e2e" foundation is COMPLETE. The whole signup spine now enforces the CL-442 hard GST gate (no `gstin_verified` ⇒ no account, no trial) with verify-then-create: GSTIN is verified server-side BEFORE the `tenants` row is persisted, so nothing is held for a rejected business. Live URL: `https://viabe-team-dev.vercel.app/team/signup`.

**Shipped to dev this cycle (all merged, verified against `git log origin/dev`):**
- **VT-404** (#478) — welcome message invites the owner's reply (`team_welcome2`).
- **VT-405 A+B** (#476 `8ec6a64`, #477 `d7955ec`) — Ops VTR tenant discovery panel: signup + auto-discovered profile, scoped; per-field confirm + owner_name fix + provenance badges.
- **VT-406 A+B+reconciliation** (#479 `7c39051`, #482 `69831e8`, #484 `7a939ca`) — entity-match verify-gated signup spine + candidate-pick wizard + verified/found provenance; `run_signup` anchors the verified entity post-create.
- **VT-407 + minors** (#480 `a6d727a`, #487 `1d55bd4`) — widen Sandbox GSTIN parse + `discover_gst` source; strip empty/comma-only address subfields; geo-empty confirmed on RKECOM.
- **VT-408** (#481 `f5712f6`) — hard GST gate at signup, verify-then-create (CL-442).
- **VT-409** (#483 `da736bf`) — Sandbox auth-token fix: the GSTIN-search 500 was OUR top-level-vs-nested token bug (+ search api-version 1.0.0). Real `search_gstin` now returns `ok=True, status=Active`.
- **VT-387** (#488 `5e82d37`) — idempotency retry-window fix: transiently-failed agent draft is now retryable (drop `error` from the idempotent-hit set).
- **VT-388 addendum** (#485 `68bf801`) — `book_stationery` business type EN/HI (Sundaram e2e taxonomy).
- **CI hygiene** (#486 `6c345dd`) — quarantine DBOS-sigkill-resume + VT-384 isolation flakes (overnight).

**Externals unchanged (the launch long poles):** Meta F1 templates Meta-APPROVED (CL-438); counsel C1–C3 still the only gate to real customer sends (CL-439); VT-231 Mumbai cutover required before any `dev→main` promotion. Customer messaging remains FAIL-CLOSED (CL-434 three stops).

## IN FLIGHT (CC)

- None actively building. The verify-then-create signup batch closed; awaiting the Fazal e2e run.

## BLOCKED ON

- **Fazal:** run the Sundaram signup e2e on dev (`/team/signup`); Meta-side / counsel C1–C3; VT-231 Mumbai cutover.
- **VT-386** (PII redaction registry) — plan-first, parked at the Cowork gate.
- **Taxonomy 4 extra types** (tailor / florist / meat_fish / optician) — Fazal k-anonymity ruling pending before they're added.
- **VT-410** (`send_whatsapp_message` sibling idempotency bug) — Queued.

## NEXT ACTION

- **Fazal:** run the Sundaram verify-then-create e2e on dev and report results.
- **Cowork:** on the e2e result, sequence any follow-up fixes; gate VT-386 if/when dispatched.
- **CC:** stand by for the next dispatch; VT-410 when rostered.

## DO NOT

- Touch `main` (CL-432 — `dev→main` promotion is Fazal's word only; main is still `2de4b36`).
- Carry VT-376 / Run-Control-Panel forward as "in flight" — that batch (CL-435/436, VT-374/375/376/377/380/381) MERGED on 2026-06-12; it is DONE, not pending.
- Let the VT-406 "use my typed name / no-match" path proceed UNVERIFIED — CL-442 closed that backdoor; `invalid_gstin` rejects, `vendor_down` HOLDS with retry (never rejects a legit GST business).
- Wipe uncommitted shared-tree files (CL-418 extension): no `git reset --hard` / `checkout -f` over files you didn't author. Obstacle → signal + wait.
- Re-litigate CL-442 (hard GST gate at signup, verify-then-create) or CL-433 (no card in trial, no refund ever).
- Trust this snapshot's HEAD claims without `git log origin/dev` (Rule #14).
