-- 104_vt94_founding_tier_counter.sql — VT-94: atomic, race-safe founding-tier counter.
--
-- founding_tier_counter: a SENTINEL single row (id=1). The atomic claim
--   UPDATE ... SET claimed_count = claimed_count + 1 WHERE id = 1 AND claimed_count < cap
-- is race-safe by Postgres row-level locking — concurrent claims serialize on the row, so
-- exactly `cap` ever succeed (never cap+1). Workspace-wide (no tenant) -> deny-all RLS,
-- service-role only (mirrors razorpay_webhook_events, mig 004). cap=100 is a public Type-3
-- commitment; the literal 100 in the CHECK is the hard integrity bound.
CREATE TABLE founding_tier_counter (
    id               INT PRIMARY KEY CHECK (id = 1),
    claimed_count    INT NOT NULL DEFAULT 0 CHECK (claimed_count >= 0 AND claimed_count <= 100),
    cap              INT NOT NULL DEFAULT 100,
    last_claimed_at  TIMESTAMPTZ,
    last_released_at TIMESTAMPTZ
);
INSERT INTO founding_tier_counter (id, claimed_count, cap) VALUES (1, 0, 100);

ALTER TABLE founding_tier_counter ENABLE ROW LEVEL SECURITY;
ALTER TABLE founding_tier_counter FORCE ROW LEVEL SECURITY;
CREATE POLICY founding_tier_counter_no_tenant_access ON founding_tier_counter
    FOR ALL USING (false) WITH CHECK (false);

-- founding_tier_claims: per-tenant audit (one slot per tenant — tenant_id UNIQUE).
-- released_at is AUDIT-ONLY: the no-reopen-founding policy means the counter NEVER
-- decrements (docs/team/founding-tier-policy.md). Tenant-scoped -> RLS + DSR purge.
CREATE TABLE founding_tier_claims (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL UNIQUE REFERENCES tenants (id),
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at TIMESTAMPTZ
);
CREATE INDEX founding_tier_claims_tenant_idx ON founding_tier_claims (tenant_id);

ALTER TABLE founding_tier_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE founding_tier_claims FORCE ROW LEVEL SECURITY;
-- App-role reads are tenant-scoped; the signup-txn write + DSR purge use service-role
-- (BYPASSRLS), like the rest of the billing tables.
CREATE POLICY founding_tier_claims_select ON founding_tier_claims
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY founding_tier_claims_insert ON founding_tier_claims
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
