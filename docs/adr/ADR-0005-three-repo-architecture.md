# ADR-0005: Three sibling repos — Team / Reports / Marketing

**Status:** Accepted

## Context

Viabe ships three distinct product surfaces:

- **Team** — multi-agent system for SMB owners (this repo; orchestrator + team-web + ingestion-worker)
- **Reports** — analytic reports product (separate marketing motion + customer base)
- **Marketing** — marketing site (`viabe.ai/` root; product positioning + signup flows)

Each has its own pace, deploy cadence, secrets, and reviewer set.

## Considered Options

- **A.** Monorepo — single CI; shared tooling; single PR can span products. Cross-product blast radius is large.
- **B.** Three sibling repos (chosen) — independent deploy cadence; smaller blast radius per PR; separate Vercel/Railway projects
- **C.** Monorepo with codeowners for hard isolation — still shares one CI fail-state across all three

## Decision

**B.** Three GitHub repos: `rkecom-in/viabe-team`, `rkecom-in/viabe-reports`, `rkecom-in/viabe-marketing`. Each gets its own Vercel project + Railway project (where applicable). Shared types extracted to npm packages if cross-repo coupling becomes load-bearing (none yet).

Path-based routing under a single domain (`viabe.ai/team`, `viabe.ai/report`, marketing at root) is a deployment layer concern handled by Vercel rewrites — see ADR-0006.

## Consequences

- (+) PRs in viabe-team don't touch Reports' CI; faster feedback loop per product
- (+) Each repo can pick the right reviewer set + the right pace (Reports cadence ≠ Team cadence)
- (+) Secrets stay per-repo; no accidental cross-product exposure
- (+) Mental model is clear: one product, one repo, one deploy
- (−) Three CI pipelines to maintain (each with its own gates / linters)
- (−) Shared utilities require an npm-package round-trip if extracted (none yet — accept duplication where cheap)
- (−) Three GitHub Actions billing surfaces — coordinate the runner mins

## References

- CL-41 (three-repo architecture decision)
- CL-132 (path-based routing under viabe.ai)
- ADR-0006 (path-based routing)
- VT-120 (deployment-shape architecture-of-record)
