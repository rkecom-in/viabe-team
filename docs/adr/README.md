# Architectural Decision Records (ADRs)

MADR-lite. Each ADR captures a load-bearing architectural decision: context, considered options, decision, consequences. References cite the originating CL number from `docs/clau/decisions-ledger.md` and any related VT sprint rows.

## Numbering

- `0000-template.md` — the template; copy + rename for new ADRs
- `ADR-NNNN-kebab-title.md` — sequential, never renumber
- Once Accepted, content is immutable; supersession via a new ADR that flips Status of the prior

## Status values

- `Proposed` — under discussion; not yet locked
- `Accepted` — load-bearing today
- `Superseded by ADR-NNNN` — replaced; kept for history
- `Deprecated` — no longer relevant; kept for grep

## Initial set (VT-117)

| ADR | Title | Status | CL anchor |
|---|---|---|---|
| 0001 | DBOS substrate (vs Temporal) | Accepted | CL-36 |
| 0002 | LangGraph orchestrator + Agent SDK split | Accepted | CL-29 |
| 0003 | Supabase Postgres single-substrate | Accepted | CL-79 |
| 0004 | Zero-manual-paste connectors (Apps Script abandoned) | Accepted | CL-421 |
| 0005 | Three sibling repos (Team / Reports / Marketing) | Accepted | CL-41 |
| 0006 | Path-based routing under viabe.ai | Accepted | CL-132 |
| 0007 | Sprint 2 re-anchored to Integration Agent | Accepted | — |
| 0008 | Operator-JWT vs admin-token split | Accepted | CL-220 |
| 0009 | Memory tiering: L0 in-house, L1 Mem0-deferred | Accepted | CL-324 |
