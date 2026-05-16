/**
 * Shared types for Viabe Team apps.
 *
 * Phase 1 scaffold: placeholder types only — no business logic. The Python
 * counterparts live in `python/team_shared/` and are kept in sync via the
 * codegen workflow described in this package's README.
 */

/** Lifecycle state of a durable orchestrator workflow. */
export type WorkflowStatus = 'pending' | 'running' | 'succeeded' | 'failed'

/** Pricing tiers for Viabe Team. */
export type PricingTier = 'founding' | 'standard' | 'pro'
