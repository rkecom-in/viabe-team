# ADR-0006: Path-based routing under viabe.ai (`/team`, `/report`, root = marketing)

**Status:** Accepted

## Context

Three sibling repos (ADR-0005) need to render under one customer-facing domain. Options:

- Subdomains (`team.viabe.ai`, `report.viabe.ai`, `viabe.ai`) — DNS overhead; CORS edge cases; SEO scattered
- Subpaths (`viabe.ai/team`, `viabe.ai/report`, `viabe.ai/`) — single domain; SEO consolidates; routing handled at the edge

## Considered Options

- **A.** Subdomains — clean separation; cookie scoping needs care; multiple SSL certs
- **B.** Subpaths via Vercel rewrites (chosen) — one cert, one domain, edge-level path matching
- **C.** Single monolith Next.js app aggregating all surfaces — couples deploy cadence again (rejected by ADR-0005)

## Decision

**B.** `viabe.ai/team/*` rewrites to the team-web Vercel deployment; `viabe.ai/report/*` rewrites to the reports Vercel deployment; `viabe.ai/*` (everything else) routes to the marketing site. Cookies scoped per path (`/team/ops/*` for operator-JWT — see ADR-0008).

## Consequences

- (+) Single SSL cert, single CDN, one canonical domain
- (+) Cookie scoping by path (operator-JWT visible only to `/team/ops/*`)
- (+) SEO juice consolidates on viabe.ai
- (+) Owners type one domain name regardless of which product they hit
- (−) Vercel rewrite config is load-bearing — a misconfigured rewrite can route Team requests to Reports
- (−) Cross-product navigation (Team → Reports) requires explicit links; no shared layout
- (−) Edge rewrites add ~10-30ms latency vs direct subdomain hits

## References

- CL-132 (path-based routing decision)
- ADR-0005 (three sibling repos)
- ADR-0008 (operator-auth cookie scoping)
- VT-120 (deployment-shape AoR)
