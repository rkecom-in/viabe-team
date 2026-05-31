-- 056_l1_agent_reflection_unique.sql — VT-197 Day-39 reflection loop.
--
-- The agent's learned calibration is ONE latest 'agent_reflection' entity per
-- tenant (the Context Composer needs only the current calibration; reflection
-- HISTORY is a v2 concern). Enforce that invariant + enable an idempotent upsert
-- (upsert_agent_reflection) via a PARTIAL UNIQUE index — mirrors mig 055's
-- business_profile index.
--
-- CRITICAL separation (VT-197 scope guard): 'agent_reflection' is AGENT-owned and
-- entirely distinct from the OWNER-curated 'business_profile' entity (mig 055).
-- The owner's identity/policy is never written by the reflection loop (Fazal D3;
-- VT-268). RLS (mig 019) unchanged.

CREATE UNIQUE INDEX IF NOT EXISTS l1_entities_one_agent_reflection_per_tenant
    ON public.l1_entities (tenant_id)
    WHERE entity_type = 'agent_reflection';
