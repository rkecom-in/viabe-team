-- 131_vt374_run_control_substrate.sql — VT-374 Phase-A run-control substrate (CL-435).
--
-- n8n-style step-level control for the VTR console: pause workflows at controllable seams,
-- pre-register one-shot step overrides, stamp app-level re-run lineage, and expose a keys-only
-- step timeline to app_vtr_role. Four pieces:
--   1. workflow_controls — pause records per (tenant, workflow_kind). Active = released_at IS
--      NULL; the partial-unique index enforces ONE active pause per scope (release-then-re-pause
--      leaves history rows).
--   2. step_overrides — one-shot pre-registered step pins, consume-first (FOR UPDATE SKIP LOCKED
--      + consumed_at stamped in the SAME txn BEFORE execution — plan §4.2/F8). Next-run rows
--      (workflow_id IS NULL) REQUIRE expires_at (CHECK below; the sweep cancels expired rows).
--   3. pipeline_runs / pipeline_steps lineage + control columns (F3 — app-level lineage, no DBOS
--      workflow-id dependency; the timeline shows what was controlled).
--   4. vtr_step_timeline + vtr_workflow_controls — the VTR read surface (mig-130 construction).
--
-- N1 RETIRE arm: run_controls (mig 078, VT-300) is DROPPED. STEP-0 confirmed single-purpose —
-- exactly one production consumer (supervisor campaign-send hold) + one writer (the
-- ops_runcontrol endpoint); the supervisor seam migrates onto workflow_controls in this same
-- row. Dropping also closes the latent DSR gap (run_controls was never in
-- dsr_purge._PURGE_ORDER).
--
-- Both new tables: RLS + FORCE with ZERO policies (deny-all under FORCE — the mig-078
-- construction; only the RLS-bypassing service pool reaches them), ZERO app_vtr_role direct
-- grants, and both added to dsr_purge._PURGE_ORDER in this same change (I3). PII-at-rest note
-- (F7): pinned_input / pinned_output / reason are redacted at WRITE (pii_redactor WITH the
-- tenant's customer-name registry) before they land here; purge is keyed on tenant_id.

-- ─── 1. workflow_controls — pause records ───

CREATE TABLE workflow_controls (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants (id),
    workflow_kind TEXT NOT NULL CHECK (workflow_kind IN
        ('webhook_inbound', 'agent_dispatch', 'auto_discovery', 'plan_generate',
         'plan_deliver', 'trial_sweep', 'ingestion', 'campaign_send')),
    set_by        UUID NOT NULL,         -- the operator (VTR/VTAdmin); no FK — operators are not a DB table (mig-078 posture)
    reason        TEXT,                  -- redacted at WRITE (name registry); ≤500 API-enforced (F7)
    set_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at   TIMESTAMPTZ,
    released_by   UUID
);

-- ONE active pause per (tenant, kind).
CREATE UNIQUE INDEX workflow_controls_one_active
    ON workflow_controls (tenant_id, workflow_kind) WHERE released_at IS NULL;

ALTER TABLE workflow_controls ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow_controls FORCE ROW LEVEL SECURITY;

-- ─── 2. step_overrides — one-shot pre-registered step pins ───

CREATE TABLE step_overrides (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants (id),
    workflow_kind   TEXT NOT NULL CHECK (workflow_kind IN
        ('webhook_inbound', 'agent_dispatch', 'auto_discovery', 'plan_generate',
         'plan_deliver', 'trial_sweep', 'ingestion', 'campaign_send')),
    step_name       TEXT NOT NULL,
    workflow_id     UUID,                -- target pipeline_runs.id; NULL = next-run (expires_at then REQUIRED)
    pinned_input    JSONB,               -- allowed_keys-validated at the API (I7/F6); redacted at write
    pinned_output   JSONB,               -- pure_return steps only (v1 registry: none) — API-enforced 422
    reason          TEXT,                -- redacted at WRITE; ≤500 API-enforced (F7)
    created_by      UUID NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,         -- F8: bounds next-run pins; the sweep cancels expired rows
    consumed_at     TIMESTAMPTZ,         -- stamped in the consume txn BEFORE execution (F8)
    consumed_run_id UUID,                -- N2: DBOS recovery re-applies when consumed_run_id = current run
    cancelled_at    TIMESTAMPTZ,
    CONSTRAINT step_overrides_nextrun_expiry
        CHECK (workflow_id IS NOT NULL OR expires_at IS NOT NULL)
);

-- The consume hot read: unconsumed, uncancelled pins for a (tenant, kind, step).
CREATE INDEX step_overrides_pending ON step_overrides
    (tenant_id, workflow_kind, step_name)
    WHERE consumed_at IS NULL AND cancelled_at IS NULL;

ALTER TABLE step_overrides ENABLE ROW LEVEL SECURITY;
ALTER TABLE step_overrides FORCE ROW LEVEL SECURITY;

-- ─── 3. lineage + control columns on the pipeline tables ───

-- F3: app-level rerun lineage (fresh uuid4 run identity; never the source sid / work-item uuid5).
-- Plain UUID, no self-FK — the DSR purge deletes a tenant's runs in one statement; lineage is
-- display metadata, not referential.
ALTER TABLE pipeline_runs ADD COLUMN rerun_of_run_id UUID;
ALTER TABLE pipeline_runs ADD COLUMN rerun_from_step TEXT;

-- What was controlled, shown on the timeline (plan §4.3).
ALTER TABLE pipeline_steps ADD COLUMN override_id UUID;
ALTER TABLE pipeline_steps ADD COLUMN paused_ms INTEGER;

-- ─── 4. N1 RETIRE: run_controls (mig 078) ───

-- No FK dependents and no view reads it (verified: no other migration references run_controls).
-- Index idx_run_controls_pending drops with the table. The supervisor consumer +
-- run_control_handler + ops endpoint re-point at the new substrate in this same VT row — a
-- retired table keeps no live writers.
DROP TABLE public.run_controls;

-- ─── 5. vtr_step_timeline — the keys-only step timeline ───

-- mig-130 construction: owner-rights view (NOT security_invoker), the role grant is the sole
-- gate; app_vtr_role keeps ZERO grants on the raw pipeline tables.
--
-- Envelope posture (plan §6 = the Gap-6 precedent; STEP-0 §3.2 audit): envelope VALUES pass
-- through ONLY for the audited name-free step_kinds —
--   webhook_received        EXPLICIT 4-KEY ALLOWLIST projection (see below), NOT full passthrough
--   agent_invocation        (lengths/constants) — full value passthrough
--   aborted_hard_limit      (enums/numbers) — full value passthrough
--   tenant_isolation_breach (UUIDs/counts) — full value passthrough
-- Every other kind projects KEY ARRAYS only (the mig-130 diff_from_prev pattern — keys show
-- WHAT a step carried, never to-what; read-time redaction of free text was already rejected
-- in mig 130). Non-object envelopes (defensive — writers always pass dicts) project '[]'.
--
-- webhook_received NARROWING (frozen list = a CEILING, not a floor): the WebhookReceivedInput
-- schema (observability/envelopes/webhook_received.py) also carries body_token,
-- sender_phone_token, and twilio_message_sid (and a transport may pass media_url_0) — none of
-- which are safe to surface. So rather than pass the WHOLE name-free envelope through, this view
-- projects an EXPLICIT key allowlist via jsonb_strip_nulls(jsonb_build_object(...)): exactly
-- message_type / num_media / dupe_status / status_callback_state, present-if-present. body_token,
-- sender_phone_token, twilio_message_sid, media_url_0 are NEVER exposed by construction.
--
-- EXCLUDED BY CONSTRUCTION (CL-390): error, decision_rationale, tool_calls — all three are
-- unredacted at write (STEP-0 §3.2) and NEVER reach this surface. Raw envelopes stay
-- exception-tier via the EXISTING Gap-6 audited admin path, not via any grant here.
--
-- MULTI-VTR PRECONDITION (same as mig 115/118/130): NOT assignment-scoped. Before a 2nd
-- (non-Fazal) VTR: add WHERE tenant_id IN (SELECT ... FROM vtr_assignments).
CREATE OR REPLACE VIEW vtr_step_timeline AS
SELECT r.tenant_id,
       r.id         AS run_id,
       r.run_type,
       r.status     AS run_status,
       r.started_at AS run_started_at,
       r.ended_at   AS run_ended_at,
       r.rerun_of_run_id,
       r.rerun_from_step,
       s.id         AS step_id,
       s.step_seq,
       s.step_kind,
       s.step_name,
       s.status     AS step_status,
       s.started_at,
       s.ended_at,
       s.duration_ms,
       s.override_id,
       s.paused_ms,
       CASE WHEN s.step_kind = 'webhook_received'
                     AND jsonb_typeof(s.input_envelope) = 'object'
                -- EXPLICIT 4-key allowlist (NOT full passthrough): body_token /
                -- sender_phone_token / twilio_message_sid / media_url_0 are NEVER exposed.
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'message_type',          s.input_envelope -> 'message_type',
                         'num_media',             s.input_envelope -> 'num_media',
                         'dupe_status',           s.input_envelope -> 'dupe_status',
                         'status_callback_state', s.input_envelope -> 'status_callback_state'))
            WHEN s.step_kind IN ('agent_invocation', 'aborted_hard_limit',
                                 'tenant_isolation_breach')
                THEN s.input_envelope
            WHEN s.input_envelope IS NULL THEN NULL
            WHEN jsonb_typeof(s.input_envelope) = 'object'
                THEN COALESCE((SELECT jsonb_agg(k)
                               FROM jsonb_object_keys(s.input_envelope) AS k), '[]'::jsonb)
            ELSE '[]'::jsonb
       END AS input_envelope,
       CASE WHEN s.step_kind = 'webhook_received'
                     AND jsonb_typeof(s.output_envelope) = 'object'
                -- Same 4-key allowlist on output (output_envelope is None for this kind today;
                -- narrowed anyway so a future writer can never leak the unsafe keys here).
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'message_type',          s.output_envelope -> 'message_type',
                         'num_media',             s.output_envelope -> 'num_media',
                         'dupe_status',           s.output_envelope -> 'dupe_status',
                         'status_callback_state', s.output_envelope -> 'status_callback_state'))
            WHEN s.step_kind IN ('agent_invocation', 'aborted_hard_limit',
                                 'tenant_isolation_breach')
                THEN s.output_envelope
            WHEN s.output_envelope IS NULL THEN NULL
            WHEN jsonb_typeof(s.output_envelope) = 'object'
                THEN COALESCE((SELECT jsonb_agg(k)
                               FROM jsonb_object_keys(s.output_envelope) AS k), '[]'::jsonb)
            ELSE '[]'::jsonb
       END AS output_envelope
FROM pipeline_runs r
LEFT JOIN pipeline_steps s ON s.run_id = r.id;

-- ─── 6. vtr_workflow_controls — pause state for the panel ───

-- Companion view so the panel never shows "not paused" while a hold is active (active =
-- released_at IS NULL). EXCLUDED: reason (free text — redacted-at-write, but the VTR surface
-- gets structural fields only) and set_by/released_by (operator ids). Exactly these 4 columns.
CREATE OR REPLACE VIEW vtr_workflow_controls AS
    SELECT tenant_id, workflow_kind, set_at, released_at FROM workflow_controls;

GRANT SELECT ON vtr_step_timeline TO app_vtr_role;
GRANT SELECT ON vtr_workflow_controls TO app_vtr_role;
