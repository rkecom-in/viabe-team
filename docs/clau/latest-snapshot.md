# Latest State Snapshot

**As of:** 2026-07-17. **dev HEAD:** `47cffa0` (VT-101 Stage-3 HOLD record — Stage 3 complete on dev; `origin/dev` matches). **main HEAD:** UNCHANGED (Fazal-only promotion per CL-432). **BINDING Team go-live: 2026-07-15 (passed — launch discipline continues).**

> Reconciled against `git log --oneline -8`, the sprint rows, and the latest Cowork/CC signals (Rule #14). The prior 2026-07-01 snapshot (VT-514/515/516 observability + Fazal's signup re-run) is superseded — that chain landed. The critical path is now the **VT-101 agent_framework migration**, which is COMPLETE on dev (both feature flags ON at `47cffa0`) with the §7.3 DB-inversion deliberately deferred (Fazal-explicit). Architecture is ratified: `docs/agent-framework/ARCHITECTURE.md` (CANONICAL, Fazal 2026-07-16, CL-2026-07-16-arch-ratified-migration).

---

## CRITICAL PATH

**VT-101 agent_framework migration — COMPLETE on dev.** Both framework feature flags are ON at `47cffa0`; Integration + Sales Recovery route through the ratified Manager/SubAgent/Tool contract (`apps/team-orchestrator/src/orchestrator/agent_framework/`). Stages landed: VT-664 Stage 1 (connector-Tools surface, inert) → Stage 2 (GateFacade owns the whole business-action round-trip) → Stage 3(a)(b) (SR through the framework) → Stage 3(c) (Integration sub-graph dissolved into Manager-held connector Tools). The **§7.3 DB-inversion is DEFERRED** (Fazal-explicit — not a rush item). Evidence + staged plan: `.viabe/vt101-migration-morning-report.md`.

## IN FLIGHT (CC)

- **VT-667** — campaign send ignores the owner's creative brief; **needs a plan** (not yet grant-authorized to build).
- **VT-663 (P2)** — per-tenant owner-language preference (Fazal item #2; design-first, NEXT BUILD).
- **Docs consolidation batch (CL-2026-07-17)** — this batch: 2 deletes + 18 archive moves into `docs/archive/` + stale-claim surgery + `docs/README.md` as THE index. Committed locally; awaiting main-session review + push.

## BLOCKED ON

- **Fazal — prod promotion decision.** `dev → main` (VT-231 Mumbai prod cutover + explicit Fazal promotion authorization, CL-432). No prod flag flips without Fazal.
- **Fazal — VT-667 plan grant.** Build does not start until Fazal authorizes the plan (agreed-fix vs new-scope: VT-667 is new build scope → needs the grant).
- **Fazal — Track B approval.** `861a56a8` armed `campaign_send` (TTL **2026-07-18**) awaiting sign-off before any real send.

## NEXT ACTION

- **Fazal:** morning review of `.viabe/vt101-migration-morning-report.md` (the VT-101 Stage-3 completion evidence + the §7.3-deferred decision + the prod-promotion question).
- **CC:** hold on VT-667 build pending the plan grant; VT-663 language is design-first. No new in-scope build until Fazal's review lands.
- **Cowork:** audit-after on the landed VT-101 stages + this docs batch; do not block CC (2026-06-28 full-autonomy ruling).

## DO NOT

- **No prod flag flips — Fazal-only.** The framework feature flags are ON on dev; flipping anything on prod (or a `main` merge) requires Fazal's explicit promotion instruction (CL-432, CL-431 prod-authority gate).
- **Do not rush the §7.3 DB-inversion.** It is deliberately deferred (Fazal-explicit); do not pull it forward.
- **Do not build VT-667 without the plan grant.** New build scope waits for Fazal's authorization (only agreed-fixes proceed immediately).
- Send a real WhatsApp before Fazal's sign-off. Dev = mock-off + `DEV_SEND_ALLOWLIST` (Fazal's numbers real, rest mocked); NEVER drive the live-number tenant (`63211ce5` = Fazal). Never fabricate a phone number.
- Build on a git worktree / fan out parallel writers on the shared tree — serial on the one tree (read-only design/audit can parallel).
- Trust this snapshot's HEAD or in-flight claims without reconciling `origin/dev`, sprint rows, and current signals (Rule #14).
