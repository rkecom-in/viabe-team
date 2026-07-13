# Phase 1.2 — Dynamic Sensing Layer + Manager Control Plane

**STATUS: HELD — NOT for CC yet. Do NOT build / do NOT action until Fazal explicitly releases it.**
CC's active scope is **Phase 1.1** (see `manager-objective.md` + the current CC directive). This document captures
the Phase-1.2 design NOW so it's not lost, but sharing it with CC prematurely would create scope confusion —
Fazal's explicit instruction 2026-07-10 ("share with CC at the right time, not now, to prevent misunderstanding").
`manager-objective.md` remains the single north-star; this is the phase-scoped execution plan for its autonomous layer.

Author: Cowork, 2026-07-10.

## The phase split (Fazal 2026-07-10)
- **Phase 1.1 — ACTIVE (CC building now):** the REACTIVE Team-Manager. Trust floor (trust-breakers = 0). Supports
  the 10 journeys (`.viabe/journey-sim-spec.md`). Onboarding, Integration, Sales Recovery + the other specialist
  lanes run via **owner triggers or timed/scheduled invokes**. Reactive task decompose+delegate (§7B), reactive
  outcome validation on a triggered task (§7C), always-on audit logging (§7D). = `manager-objective.md` MINUS the
  proactive / self-initiating layer.
- **Phase 1.2 — THIS doc (DEFERRED):** the DYNAMIC sensing layer + the Manager as its CONTROL PLANE, plus the
  proactive self-initiated standing plan. Entry-gated on Phase 1.1 being done.

## Capability → phase map (removes ambiguity)
| Capability | Phase | Why |
|---|---|---|
| Trust floor (§1–§6, trust-breakers=0) | 1.1 | The foundation everything stands on |
| Run the 10 journeys (onboarding/integration/specialist) | 1.1 | Reactive + owner/scheduled trigger |
| §7B LEAD — decompose + delegate WITHIN a triggered journey | 1.1 | Reactive |
| §7C VALIDATE — judge a triggered task's outcome | 1.1 | Reactive on the outcome |
| §7D AUDIT — decision/reason/thought/action logging | 1.1 | Always-on, foundational |
| §7.0 brain-central principle | 1.1 | Governs all Manager work now |
| **§7F dynamic watchers / pollers / listeners / schedulers + control plane** | **1.2** | The autonomous sensing layer |
| **§7A proactive STANDING monthly/daily plan (self-adapting, self-initiating)** | **1.2** | Proactive; needs the sensing substrate |
| **Self-INITIATED validation / re-check loops** | **1.2** | Self-triggered, not reactive |

Distinction that matters: within-journey tactical planning (decompose a triggered task) is §7B → 1.1. The STANDING
proactive monthly/daily plan that self-adapts and self-initiates is §7A → 1.2.

## Phase 1.2 scope (what gets built when released)
The modular model from `manager-objective.md` §7F:
- Reactive Manager + INDEPENDENT sensing services (pollers / listeners / watchers / schedulers) that TRIGGER the
  Manager with data when they detect an event / ingestion / schedule-fire / signal.
- **Control plane:** the Manager can SET / UNSET / DEFINE watchers at runtime; it selects the modality
  (schedule / event-trigger / webhook / callback / poll) from the runtime's BOUNDED menu — never invents scheduling.
- **Scope reasoning:** general-vs-specific (one order vs all pending-payment orders), consolidates duplicates into a
  general watcher; **tenant-scoped ONLY** (RLS / data isolation).
- **Lifecycle (first-class):** layered teardown — self-terminate on condition + TTL backstop + reaper sweep; Manager
  can also unset. Zero leaked or duplicated watchers.
- **Brain on every trigger:** deterministic sensing controls WHEN the brain is woken (no-change poll ≠ wake); the
  brain decides WHAT to do. No hardcoded action logic (§7.0).
- Plus proactive **§7A** standing plan: monthly plan grounded in tenant data, revisable daily on conditions,
  decomposed into daily actions — this is what tells the sensing layer what to watch for.

## Entry criteria (Phase 1.1 → 1.2 gate)
Do NOT start 1.2 until: **trust-breakers = 0 on the full pack; the 10 journeys pass (Tier-1 clean, Tier-2 ≥ 90%);
the reactive manager + specialist execution is proven live.** The reactive operator must be TRUSTWORTHY before it's
made AUTONOMOUS — self-initiation on a manager that still fabricates is the worst-case failure.

## Boundaries (carried from the objective — non-negotiable in 1.2)
- Brain-central (§7.0): sensing detects + triggers; the brain decides; no hardcoded action logic.
- Effect-boundary / Pillar-7: a self-initiated EFFECTFUL action (self-triggered send/spend) STILL gates to owner/VTR
  approval. Self-initiation is NOT a back door around approval.
- Tenant-scoped watchers only. Layered lifecycle (no leaks). Cost governed by sensing-controlled trigger volume.

## Measurement (when armed)
Continuous-operation scenarios (BUILD NEEDED): inject an event / schedule fire (NOT an owner message); assert the
Manager self-initiates the RIGHT task via the RIGHT modality at the RIGHT scope, and the watcher tears down. Plus
multi-day planning scenarios (a monthly plan adapts to an injected condition change → sound daily actions).
Trust-breakers = 0 (§2) applies to every self-initiated action.

## Why deferred (so we don't second-guess it)
Phase 1.1 delivers a shippable, measurable reactive operator (the 10-journey gate). Phase 1.2's autonomous layer is
net-new, higher-risk, and only safe ON a trustworthy reactive core. Splitting keeps CC focused, gives a clean gate,
and stops the sensing-layer complexity from destabilizing the trust work. Released to CC only when 1.1's gate is
met — Fazal's call on timing.
