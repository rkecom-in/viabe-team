-- 011_tenant_opt_out.sql — opt-out flag on tenants (VT-3.8 opt_out_handler).
--
-- The VT-Foundation tenants table (001) had no opt-out column. The Pre-Filter
-- Gate's opt_out_handler sets this flag when an owner sends STOP / UNSUBSCRIBE.
-- RLS on tenants is already enabled (001); a new column needs no new policy.
ALTER TABLE tenants ADD COLUMN opt_out BOOLEAN NOT NULL DEFAULT false;
