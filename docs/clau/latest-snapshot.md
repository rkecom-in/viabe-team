# Latest State Snapshot

**As of:** 2026-07-26 (Codex reconciliation — Rule #14). **dev HEAD:** `8d0706f`. **main/prod:** `cb584d9` per the 2026-07-24 promotion signal (#539); the local `main` ref is stale and was not used.

> Reconciled against `git log --oneline -10`, the latest `.running/to-cowork/` signals, VT-683/691/698–704 rows, and `gh pr list --state open --limit 5`. The prior 2026-07-21 snapshot was materially stale: VT-683 P2c/P3/P4 and VT-691 subsequently shipped, the first onboarding arc reached prod, and dev advanced from `eea4a17` to `8d0706f`.

## CRITICAL PATH
Prod is live. WhatsApp-initiated signup is live with the consent gate intact, and the onboarding comprehension/simulator fixes through VT-703 were promoted to prod by #539. The product has moved from transport/onboarding construction to proving the real first-customer journey, measuring agent behavior, and giving operators a trustworthy per-tenant activity view.

## IN FLIGHT
- **VT-704** — per-tenant 30-day Activity Flow is built on dev at `8d0706f`; awaiting Fazal's visual check on the dev Ops Console and promotion direction.
- **VT-691** — WhatsApp signup is live; still awaiting a real cold-message first-customer smoke from a non-tenant number.
- **VT-683 tail** — P1–P4 are on dev; remaining operational proof is the live wake-up canary, a clean shadow period before template-whitelist enforcement, and notice-class template reroutes.
- **Open roster from the latest promotion signal** — OAuth verbatim-loop, enforce-mode beat parity, discovery latency, re-ask budget, and the VT-698 double-intro ruling.

## BLOCKED ON / NEXT ACTION
- Fazal visually checks VT-704 against a tenant with real 30-day data, then decides whether to promote.
- Run the first-customer cold-message smoke for VT-691 and the live wake-up canary for VT-683 without synthetic traffic to real tenants.
- Reconcile stale sprint-row status text for already-promoted VT-700–703 in the normal delivery loop; this snapshot follows git/signals rather than those stale labels.

## DO NOT
Trust local `main` (`2de4b36`) as current; the promotion signal establishes prod at `cb584d9` · treat stale VT row labels as stronger than git/signals · send synthetic inbound messages to real tenants · enable template-whitelist enforcement without its clean shadow period · touch `main` or prod configuration without Fazal's authorization.
