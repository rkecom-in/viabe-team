# Viabe Team — Documentation Map (THE index)

**This file is the single documentation index.** If a doc isn't listed here, it is structured
history (session-log entries, sprint rows), archived, or misplaced — flag misplaced ones.
Maintainer: Clau. Re-audit trigger: any ratification, role change, or when root/top-level
strays accumulate. Last reorganization: 2026-07-22 (target state; moves executed by CC).

## Where things live (the one rule)

| Kind of document | Home |
|---|---|
| Business / concept / investor | `docs/concept/` |
| Strategy (internal) | `docs/strategy/` |
| Architecture + agent framework | `docs/agent-framework/` · `docs/architecture/` · `docs/adr/` |
| Operations: runbooks | `docs/runbooks/` |
| Verification / test evidence | `docs/verification/` · `docs/audits/` |
| Delivery loop (Clau/CC substrate) | `docs/clau/` + `.viabe/` |
| Policy drafts | `docs/policy/` |
| Diagrams | `docs/diagrams/` |
| Dead / superseded / consumed | `docs/archive/` (zero live authority) |
| Repo root | ONLY: README, CLAUDE.md, AGENTS.md, configs. Nothing else. |

## Tier 1 — Canonical (conflicts resolve IN THIS ORDER)

1. `docs/clau/decisions-ledger.md` — Standing decisions. Never re-litigate.
2. `.viabe/manager-objective.md` — behavioral north-star (two-tier bar).
3. `docs/agent-framework/ARCHITECTURE.md` — the ACF (Manager/SubAgent/Tool; ratified 2026-07-16).
4. `docs/clau/phase1-plan.md` — Phase-1 product scope (LOCKED).
5. `docs/clau/discipline-rules.md` — working discipline.
6. `CLAUDE.md` (root) — session bootstrap + three-role model (Fazal / Clau / CC).

## Live scoreboards & rosters

- `.viabe/objectives.md` — objective status of record (CC-maintained; artifact renders it)
- `.viabe/agent-roster-wishlist.md` — specialist agents, value × complexity (Sales-first ruling)
- `.viabe/launch-tracker.md` · `.viabe/templates.md` (name→SID registry)
- `docs/clau/latest-snapshot.md` (reconcile before trusting) · `docs/clau/active-context-summary.md`

## Business & strategy

- `docs/concept/Viabe_Team_Concept_Investor_v2_0.docx` (+ .pdf) — CURRENT concept, investor edition
- `docs/strategy/how-viabe-wins.md` — INTERNAL (Fazal+Clau): landscape, moat, exit posture
- Superseded concept v1 docs (Pulse era) → `docs/archive/`

## Agent framework (builders start here)

- `docs/agent-framework/ARCHITECTURE.md` — canonical model
- `docs/agent-framework/TOOLS.md` — GENERATED tool catalog (never hand-edit)
- `docs/agent-framework/EXTERNAL-BUILDER-ONBOARDING.md` — third-party builder kit (Codex-proven)
- `docs/agent-framework/README.md` — contract reference · `docs/agent-framework/build-sales-recovery.md` — tutorial

## Operating loop (Clau ↔ CC)

- `.viabe/protocol.md` (signals) · `.viabe/cc-startup-protocol.md` · `.viabe/BOOTSTRAP.md`
- `docs/clau/operating-brief.md` · `docs/clau/COWORK-CC-OPERATING-STANDARD.md`
- `.viabe/consent-text.md` · `.viabe/customer-data-go-live-prereqs.md` · `.viabe/welcome-template-resubmission-package.md` (pending Fazal STEP 0)

## Armed / parked specs

- `.viabe/journey-sim-spec.md` (ARMED) · `.viabe/phase-1.2-dynamic-sensing-spec.md` (HELD)
- `.viabe/prod-failed-workflow-handling-spec.md` (rides prod hardening)

## Runbooks (`docs/runbooks/`)

- `VIABE-LAUNCH-RUNBOOK.md` (moved from docs/ top level) + breach-response + the VT-era
  runbooks migrated from docs/clau/ (deployment-shape, dev-env, admin-endpoints,
  region-verify, sheet-integration)

## Reference

- `docs/viabe_team_supported_model.md` — models/pricing/env of record
- `docs/verification/edge-case-coverage-manifest.md` (moved) · `docs/audits/` · `docs/diagrams/`
- `docs/team/meta-templates-batch2.md` (moved; canonical bilingual template BODIES — live-referenced, repointed)
- Technical Reference v1 → `docs/archive/` (HISTORICAL; v2 = VT-665)

## Tier 5 — Policy drafts (`docs/policy/`)

All unpublished DRAFTs pending counsel review (Fazal). None live.

## Archive (`docs/archive/` — zero live authority)

Everything superseded/consumed, each with a banner. 2026-07-22 additions: session handoff
(June), pm_dashboard.html + sprint_dashboard.html (retired — the Clau artifact is the
dashboard), Pulse-era concept docs, vt101 morning report, Technical Reference v1.
If you're reading an archived doc to decide anything current — stop; use Tier 1.
