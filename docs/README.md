# Viabe Team — Documentation Map (THE index)

**This file is the single documentation index.** It supersedes `docs/documentation-hierarchy.md`
(2026-06-06, four-role era — scheduled for deletion). If a doc isn't listed here, it is either
structured history (session-log entries, sprint rows) or archived.
Maintainer: Cowork. Re-audit trigger: any architecture ratification or role-model change.

## Tier 1 — Canonical (authority; conflicts resolve IN THIS ORDER)

| Doc | Authority over |
|---|---|
| `docs/clau/decisions-ledger.md` | Standing decisions. Never re-litigate. |
| `.viabe/manager-objective.md` | WHAT the Team-Manager must achieve (behavioral north-star, two-tier bar). |
| `docs/agent-framework/ARCHITECTURE.md` | HOW the system is shaped (Manager/SubAgent/Tool; ratified 2026-07-16). |
| `docs/clau/phase1-plan.md` | Phase-1 product scope (LOCKED, Fazal 2026-07-01). |
| `docs/clau/discipline-rules.md` | Working discipline (Rules #1–#18). |
| `CLAUDE.md` (repo root) | Session bootstrap + role model (THREE roles: Fazal / Cowork / CC). |

## Tier 2 — Live operational substrate (kept current by the loop; never "read-only")

- `docs/clau/latest-snapshot.md` — 5-field state snapshot (reconcile before trusting, Rule #14)
- `docs/clau/active-context-summary.md` — active-CL digest (Rule #16 substrate)
- `.viabe/launch-tracker.md` · `.viabe/templates.md` (WhatsApp template→SID registry)
- `.viabe/protocol.md` (signal schema) · `.viabe/cc-startup-protocol.md` · `.viabe/BOOTSTRAP.md`
- `.viabe/consent-text.md` · `.viabe/customer-data-go-live-prereqs.md`
- `docs/viabe_team_supported_model.md` (LLM/model/env of record)
- Sprint rows `.viabe/sprint/VT-*.md` · session log `docs/clau/entries/CL-*.md`

## Tier 3 — Specs armed for future execution (not stale; deliberately parked)

- `.viabe/journey-sim-spec.md` (ARMED) · `.viabe/phase-1.2-dynamic-sensing-spec.md` (HELD)
- `.viabe/prod-failed-workflow-handling-spec.md` (rides VT-231)
- `docs/clau/vt231-prod-cutover-plan.md` (refresh counts before use)

## Tier 4 — Builder guides

- `docs/agent-framework/README.md` (contract reference) + `docs/agent-framework-build-sales-recovery.md` (tutorial)
- Runbooks: `docs/clau/{deployment-shape,dev-env-runbook,admin-endpoints-runbook,region-verify-runbook,sheet-integration-runbook}.md`
- `docs/clau/operating-brief.md` (role model narrative) · `docs/clau/operating-policies-and-conditions.md`

## Tier 5 — Policy drafts (`docs/policy/`)

All 8 are unpublished DRAFTs pending counsel review (Fazal). None are live.

## Archive (`docs/archive/` — historical record, ZERO live authority)

Completed-program and consumed-design docs live here after the 2026-07-17 consolidation:
the team-manager rebuild set (design/reuse-map/signoff-ledger/test-matrix), the capability/
manager-loop program trackers, Clau-era docs (resurrection, automation-plan v1), l0/l1 design
rationale, executed run reports (Sundaram e2e, live win-back), build recons, AUTONOMOUS-BUILD-6GAPS,
rail-harness findings. If you're reading an archived doc to decide anything current, stop —
use Tier 1.
