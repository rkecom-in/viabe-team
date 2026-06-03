-- 085_l3_patterns.sql — VT-68 L3 cross-tenant pattern store.
--
-- L3 = anonymized cross-tenant priors the agent uses as background ("cafes in
-- tier-2 cities: 60-90d dormants who get a discount approve at ~40-50%").
-- Constructed nightly from campaigns + attributions ONLY when the contributing-
-- tenant set meets k-anonymity (n_tenants >= 10, CL-28). Aggregates ONLY — no
-- individual events, no per-tenant rows.
--
-- Pillar 7: these are GLOBAL priors, NOT per-tenant data. So:
--   * NO tenant_id column (by design — a pattern is never one tenant's data).
--   * NO RLS (nothing here is tenant-scoped; it is workspace-global + readable
--     by any tenant past the 180-day quarantine, enforced in l3_query, not RLS).
-- The n_tenants >= 10 CHECK is the structural k-anonymity floor at rest.
-- Claimed via scripts/migration_id_allocate.py (CL-424).

CREATE TABLE l3_patterns (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pattern_type    TEXT NOT NULL CHECK (pattern_type IN (
        'cohort_response_rate',
        'attribution_rate_by_recency',
        'template_effectiveness',
        'time_of_send_effectiveness'
    )),
    -- canonical cohort identifier, e.g. 'cafe|tier_2|60_90d' — coarse only
    -- (business_type | city_tier | recency_band); NEVER city/locality/per-tenant.
    cohort_key      TEXT NOT NULL,
    n_tenants       INTEGER NOT NULL CHECK (n_tenants >= 10),  -- k-anon floor at rest
    n_campaigns     INTEGER NOT NULL CHECK (n_campaigns >= 0),
    metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence_band TEXT CHECK (confidence_band IN ('low', 'medium', 'high')),
    constructed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 90-day safety floor; nightly reconstruction keeps rows fresh.
    expires_at      TIMESTAMPTZ NOT NULL
);

-- One row per (pattern_type, cohort_key): the retrieval key + the idempotent
-- nightly-rebuild upsert target.
CREATE UNIQUE INDEX l3_patterns_type_cohort_uniq
    ON l3_patterns (pattern_type, cohort_key);

-- Intentionally NO RLS: L3 is cross-tenant global priors (Pillar 7). Quarantine
-- + retrieval gating live in l3_query.py, not at the row-security layer.
