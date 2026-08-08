-- VT-735 — the per-tenant daily Fast-call budget the ratified tier policy requires.
--
-- `.viabe/model-tier-policy.md`: "Fast is allow-listed by call site (never a default anything can
-- inherit), and carries an internal per-tenant daily Fast-budget; exceeding it degrades to Standard
-- (never to Flex — a decisive moment never gets the slow tier) and flags the tenant on the VTR
-- console, because a tenant burning Fast budget is a tenant with a runaway loop, not a billing
-- event."
--
-- `resolve_service_tier` already accepts an injected `fast_budget_check` and already degrades to
-- STANDARD on a real 'no'. Nothing supplied it, so Fast has been unbounded (the hook fails OPEN by
-- design — a budget lookup failing must never make a safety-path call slower). This migration adds
-- the only piece of state the check needs.
--
-- NO new counter table. The count is DERIVED from `llm_call_events`, which already records
-- (tenant_id, service_tier, occurred_at) for every call. A separate counter would be a second
-- source of truth that could disagree with the console reading the same events — and the VT-733
-- console is precisely where the operator will look after this flags a tenant.
--
-- NULL = fall back to the application default. 0 = Fast disabled for this tenant (an explicit,
-- meaningful setting: everything degrades to Standard), which is why the CHECK allows 0.

BEGIN;

ALTER TABLE tenant_llm_limits
    ADD COLUMN max_fast_calls_day INTEGER
        CHECK (max_fast_calls_day IS NULL OR max_fast_calls_day >= 0);

COMMENT ON COLUMN tenant_llm_limits.max_fast_calls_day IS
    'VT-735: per-tenant daily cap on service_tier=fast calls. NULL = application default; '
    '0 = Fast disabled for this tenant (degrade everything to Standard). Exceeding it degrades '
    'to Standard and raises a fast_budget_exhausted trigger — a runaway-loop signal, not billing.';

-- The budget check runs on the Fast path, which exists for decisive moments (approval resolution,
-- opt-out/STOP). A sequential scan there would spend the latency the tier was chosen to save.
-- `llm_call_events_tenant_time (tenant_id, occurred_at DESC)` does not carry service_tier, so the
-- count would still have to re-check every row of a busy tenant's day. This partial index covers
-- exactly the counted set and nothing else.
CREATE INDEX llm_call_events_fast_tenant_day
    ON llm_call_events (tenant_id, occurred_at DESC)
    WHERE service_tier = 'fast';

COMMIT;
