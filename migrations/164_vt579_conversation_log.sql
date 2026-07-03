-- 164_vt579_conversation_log.sql — VT-579: the LIFETIME owner↔system conversation log.
--
-- CL-2026-07-03-conversation-memory-architecture (Fazal, live drill, binding): "we must have the chat
-- conversation in context of the team-manager, the entire conversation in a permanent storage (lifetime
-- conversation in permanent storage and referred to whenever required) and an active memory (last 10 or
-- 20 conversation not more than 24 hour which will always be part of the team-manager's LLM context)."
--
-- Migration 162/163 gave the ONBOARDING turn brain its own rolling window + distilled summary ON the
-- journey row. This table is the TENANT-WIDE, PERMANENT log that outlives onboarding: every owner↔system
-- message (both directions) persists verbatim so (1) the Team-Manager's dispatch context ALWAYS carries
-- the last ≤20 turns within 24h (conversation_log.active_window) and (2) the manager's tool belt can
-- SEARCH the whole lifetime (conversation_log.search_history) whenever a turn warrants it.
--
-- PII / retention: this is the tenant's OWN conversation — the SAME data class as
-- onboarding_journey.recent_turns / .answers (the owner's own words). Retention = lifetime-of-relationship;
-- the SOLE deletion path is a data-subject request (DSR-purge). Hence tenant RLS + FORCE + the dsr_purge
-- registration in dsr_purge._PURGE_ORDER (this same PR). No raw phone column; message text is the owner's
-- own conversation (not app-logged). Mirrors the mig 155 (agent_memory) full-table RLS idiom.
--
-- Idempotent per (tenant_id, message_sid): a redelivered Twilio message / a DBOS step retry re-attempting
-- the same inbound/outbound record collapses to ONE row (partial unique index; record_turn does
-- ON CONFLICT DO NOTHING). message_sid is NULL for records with no transport id (nothing to dedup on).

CREATE TABLE conversation_log (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    -- direction of the turn: 'owner' = the business owner → us; 'assistant' = us → the owner.
    role         TEXT NOT NULL CHECK (role IN ('owner', 'assistant')),
    -- the message text, verbatim (capped in code ~1000 chars). The tenant's own conversation.
    text         TEXT NOT NULL,
    -- the Twilio MessageSid when the turn corresponds to a transport message; NULL otherwise (a
    -- system-composed record with no single sid). Drives the idempotency dedup below.
    message_sid  TEXT NULL,
    -- which surface produced the turn: 'journey' (onboarding turn brain), 'manager' (Team-Manager
    -- dispatch conversation), 'system' (system-composed templates/acks). NULL when unclassified.
    surface      TEXT NULL CHECK (surface IN ('journey', 'manager', 'system')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The window/search scan: newest-first within a tenant (active_window reads the head, search scans it).
CREATE INDEX conversation_log_tenant_created
    ON conversation_log (tenant_id, created_at DESC);

-- Idempotency: a (tenant, message_sid) is unique when a sid is present. record_turn does
-- ON CONFLICT DO NOTHING against this partial index so a redelivery / DBOS retry never double-logs.
CREATE UNIQUE INDEX conversation_log_tenant_sid
    ON conversation_log (tenant_id, message_sid) WHERE message_sid IS NOT NULL;

ALTER TABLE conversation_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_log FORCE ROW LEVEL SECURITY;

-- Tenant-scoped: a tenant reads + appends ONLY its own conversation (record_turn INSERTs, active_window /
-- search_history SELECT, under tenant_connection = app_role + tenant GUC). Append-only by design: no
-- tenant UPDATE/DELETE policy (nothing rewrites a logged turn; DSR-purge deletes via the BYPASSRLS
-- service path, like every other table in _PURGE_ORDER).
CREATE POLICY conversation_log_select ON conversation_log FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY conversation_log_insert ON conversation_log FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());

-- Operator (VTR / Ops Console) read — same de-identified operator surface every tenant table exposes
-- (mig 155 idiom): a JWT carrying operator_claim=true may SELECT (assignment-scoping is enforced by the
-- de-identified views layer, not here).
CREATE POLICY conversation_log_operator_select ON conversation_log
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );
