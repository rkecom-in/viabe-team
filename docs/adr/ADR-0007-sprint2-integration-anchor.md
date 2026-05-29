# ADR-0007: Sprint 2 re-anchored to Integration Agent (ingestion-first)

**Status:** Accepted

## Context

Initial Sprint 2 plan: build SR-Agent (VT-4) + 11 MCP tools first; integrations later. Sprint 1 close revealed the dependency inverted: agent has nothing to reason over without per-tenant ingested data. The moat is "agent calibrated to THIS tenant's history + product catalogue + customer base," not "agent that can call X tool against generic data." So tools-first builds a capability the agent can't usefully deploy until data lands.

## Considered Options

- **A.** Tools-first (original Sprint 2 plan) — ship SR-Agent + MCP tools; integrations as Sprint 3
- **B.** Integration Agent first (chosen) — VT-204..VT-211 + VT-212/213 manual walks; SR-Agent deferred
- **C.** Parallel — both tracks in Sprint 2; rejected because review/test bandwidth is shared and integrations are the bottleneck

## Decision

**B.** Sprint 2 epic VT-204..VT-211 (Integration Agent + Shopify + Sheet substrate + onboarding wizard) + VT-212 (Sheet OAuth walk) + VT-213 (Shopify webhook walk). SR-Agent skeleton (VT-4) + 11 MCP tools (VT-40..49) explicitly deferred to Sprint 2.5 or Sprint 3.

Reports-Jun15 launch (2026-06-15) ships without SR-Agent specialist agents; concierge-phase (Fazal-driven manual outreach to early tenants) bridges until the agent skeleton lands.

## Consequences

- (+) Agent ships against real tenant data on day one — not generic synthetic scenarios
- (+) Integration substrate doubles as ingestion-source for L0/L1 KG (data lands in customers/contacts/orders tables; KG fragments derive from these)
- (+) Reports-Jun15 binding launch still happens because concierge covers the agent gap
- (+) When SR-Agent does land, it has rich per-tenant context (cohort history, order patterns) instead of cold-start
- (−) SR-Agent skeleton (VT-4) + 11 tool rows (VT-40..49) accumulate as backlog substrate-debt
- (−) Concierge phase requires Fazal manual time (acceptable; finite tenant count in Phase 1)
- (−) Sprint 2 scope is larger than originally planned (Integration epic + onboarding + connector substrate)

## References

- VT-204 (Integration Agent epic parent)
- VT-205, VT-206, VT-207, VT-208, VT-209, VT-210, VT-211 (Sprint 2 Integration substrate)
- VT-212, VT-213 (manual OAuth walks)
- VT-4 (deferred SR-Agent skeleton)
- VT-40..49 (deferred MCP tools)
- Reports-Jun15 launch milestone (2026-06-15)
