-- 079_privacy_audit_hashchain.sql — VT-80: real tamper-evident hash-chain +
-- hard append-only on privacy_audit_log (mig 008 created the table; VT-8 owns
-- the enforcement — this is it).
--
-- Decisions (Cowork 20260603T150000Z, Fazal privacy-first):
--   1. GLOBAL service-role chain — one chain across all tenant + NULL-tenant
--      rows, written/verified on the BYPASSRLS pool connection. RLS still
--      scopes per-tenant SELECTs; verification is an ops/service function.
--   2. HARD append-only — drop UPDATE/DELETE RLS policies + a trigger that
--      blocks UPDATE/DELETE/TRUNCATE even for the service role. DSR purge leaves
--      the audit untouched (DPDP 7-yr retention) so no legitimate mutator exists.
--   3. event_type CHECK seeded with the ACTUALLY-emitted types only; grow per
--      row as new events land (no speculative enumeration).
--
-- Pre-VT-80 rows carried a PLACEHOLDER this_hash (never a valid chain). Dev is
-- synthetic-only (CL-422, no real customer audit data) and prod (VT-231) starts
-- empty, so we TRUNCATE the stub rows here and begin the chain clean at genesis
-- (the first real event writes prev_hash = NULL). This runs BEFORE the
-- append-only trigger is installed.

-- 1. Clear pre-chain stub rows (synthetic; invalid chain). Must precede the
--    TRUNCATE-blocking trigger below.
TRUNCATE TABLE privacy_audit_log;

-- 2. Total-order column for the chain (unambiguous walk order; event_at can tie).
ALTER TABLE privacy_audit_log ADD COLUMN seq BIGSERIAL;
CREATE UNIQUE INDEX privacy_audit_log_seq_idx ON privacy_audit_log (seq);

-- 3. event_type CHECK — seed the emitted types (phone_tokens + dsr_purge x2).
--    Extend in a follow-up migration when a new event_type is actually written.
ALTER TABLE privacy_audit_log
    ADD CONSTRAINT privacy_audit_log_event_type_chk
    CHECK (event_type IN (
        'phone_token_resolved',
        'subject_data_purged',
        'subject_data_purged_table'
    ));

-- 4. Hard append-only. Drop the mutating RLS policies (immutability replaces
--    them) and install a trigger that blocks UPDATE/DELETE/TRUNCATE for EVERY
--    role incl. the BYPASSRLS service role. SELECT + INSERT policies remain.
DROP POLICY IF EXISTS privacy_audit_log_update ON privacy_audit_log;
DROP POLICY IF EXISTS privacy_audit_log_delete ON privacy_audit_log;

CREATE OR REPLACE FUNCTION privacy_audit_log_immutable()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'privacy_audit_log is append-only (VT-80 tamper-evident chain); % blocked',
        TG_OP;
END;
$$;

CREATE TRIGGER privacy_audit_log_no_row_mutate
    BEFORE UPDATE OR DELETE ON privacy_audit_log
    FOR EACH ROW EXECUTE FUNCTION privacy_audit_log_immutable();

CREATE TRIGGER privacy_audit_log_no_truncate
    BEFORE TRUNCATE ON privacy_audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION privacy_audit_log_immutable();
