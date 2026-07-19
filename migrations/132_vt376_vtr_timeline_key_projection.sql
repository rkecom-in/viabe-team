-- 132_vt376_vtr_timeline_key_projection.sql — VT-376 C2 view hardening (Cowork plan
-- ruling 20260612T015000Z, arm b).
--
-- CREATE OR REPLACE of vtr_step_timeline ONLY. Zero table / RLS / grant changes: the
-- mig-131 GRANT SELECT to app_vtr_role survives the replace (same owner-rights view
-- construction, identical column list — a REPLACE requirement).
--
-- WHAT CHANGES: mig-131 passed envelope VALUES through WHOLE for the 3 fully-name-free
-- audited kinds (agent_invocation / aborted_hard_limit / tenant_isolation_breach),
-- trusting that their writers emit constants/lengths/UUIDs only. The writers DO — but
-- that was trust, not structure. This migration replaces each whole-envelope passthrough
-- with an EXPLICIT jsonb_build_object key projection (the mig-131 webhook_received
-- treatment, extended), so a future writer-side key addition can NEVER leak through this
-- surface without a deliberate view change. webhook_received's 4-key allowlist stays
-- exactly as mig-131 built it.
--
-- KEY LISTS — pinned from the ACTUAL writer envelopes (read at build time, VT-376 B1):
--   agent_invocation         writer: agent/dispatch.py _write_dispatch_entry (~405-419)
--     input : inbound_body_len, trigger, dispatched_at        (length / constant / timestamp)
--     output: reason                                          (constant string)
--   aborted_hard_limit       writer: agent/dispatch.py _write_aborted_hard_limit (~491-506)
--     input : reason, inbound_body_len                        ('hard_limit_exceeded:<axis>' / length)
--     output: axis, observed, limit                           (enum / numbers)
--   tenant_isolation_breach  writer: context_validator.py _record_breach (output_envelope ONLY)
--     output: layer, offending_ids, counts                    (pre-flight shape, :103-107)
--           + expected_tenant, stray_tenants                  (POST-flight shape, :132-136 —
--             the plan-ack pin missed these two; verified against the writer per the build
--             contract. All five are enums / entity-UUID maps / counts — name-free, CL-390.)
--     input : the writer never sets input_envelope (NULL). The same output allowlist is
--             applied to input anyway — the mig-131 webhook_received precedent: narrowed
--             so a future writer can never leak unsafe keys through the unused direction.
--
-- FROZEN LIST DISCIPLINE: the 4-kind value-bearing list (webhook_received +
-- the 3 kinds above) is a CEILING fixed by the STEP-0 §3.2 PII audit. It NARROWS here
-- (whole-envelope → explicit keys) and may narrow again; it NEVER widens — neither new
-- kinds nor new projected keys — without a FRESH PII audit of the writer envelopes.
-- Every other step_kind stays key-arrays-only (the mig-130 pattern: keys show WHAT a
-- step carried, never to-what).
--
-- EXCLUDED BY CONSTRUCTION (unchanged from mig-131, CL-390): error, decision_rationale,
-- tool_calls never reach this surface; body_token / sender_phone_token /
-- twilio_message_sid / media_url_0 never escape webhook_received.
--
-- MULTI-VTR PRECONDITION (same as mig 115/118/130/131): NOT assignment-scoped. Before a
-- 2nd (non-Fazal) VTR: add WHERE tenant_id IN (SELECT ... FROM vtr_assignments).

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
                -- mig-131 4-key allowlist, unchanged: body_token / sender_phone_token /
                -- twilio_message_sid / media_url_0 are NEVER exposed.
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'message_type',          s.input_envelope -> 'message_type',
                         'num_media',             s.input_envelope -> 'num_media',
                         'dupe_status',           s.input_envelope -> 'dupe_status',
                         'status_callback_state', s.input_envelope -> 'status_callback_state'))
            WHEN s.step_kind = 'agent_invocation'
                     AND jsonb_typeof(s.input_envelope) = 'object'
                -- VT-376 C2: explicit writer-key projection (was whole-envelope).
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'inbound_body_len', s.input_envelope -> 'inbound_body_len',
                         'trigger',          s.input_envelope -> 'trigger',
                         'dispatched_at',    s.input_envelope -> 'dispatched_at'))
            WHEN s.step_kind = 'aborted_hard_limit'
                     AND jsonb_typeof(s.input_envelope) = 'object'
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'reason',           s.input_envelope -> 'reason',
                         'inbound_body_len', s.input_envelope -> 'inbound_body_len'))
            WHEN s.step_kind = 'tenant_isolation_breach'
                     AND jsonb_typeof(s.input_envelope) = 'object'
                -- The writer never sets input for this kind; allowlisted anyway (see header).
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'layer',           s.input_envelope -> 'layer',
                         'offending_ids',   s.input_envelope -> 'offending_ids',
                         'counts',          s.input_envelope -> 'counts',
                         'expected_tenant', s.input_envelope -> 'expected_tenant',
                         'stray_tenants',   s.input_envelope -> 'stray_tenants'))
            WHEN s.input_envelope IS NULL THEN NULL
            WHEN jsonb_typeof(s.input_envelope) = 'object'
                THEN COALESCE((SELECT jsonb_agg(k)
                               FROM jsonb_object_keys(s.input_envelope) AS k), '[]'::jsonb)
            ELSE '[]'::jsonb
       END AS input_envelope,
       CASE WHEN s.step_kind = 'webhook_received'
                     AND jsonb_typeof(s.output_envelope) = 'object'
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'message_type',          s.output_envelope -> 'message_type',
                         'num_media',             s.output_envelope -> 'num_media',
                         'dupe_status',           s.output_envelope -> 'dupe_status',
                         'status_callback_state', s.output_envelope -> 'status_callback_state'))
            WHEN s.step_kind = 'agent_invocation'
                     AND jsonb_typeof(s.output_envelope) = 'object'
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'reason', s.output_envelope -> 'reason'))
            WHEN s.step_kind = 'aborted_hard_limit'
                     AND jsonb_typeof(s.output_envelope) = 'object'
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'axis',     s.output_envelope -> 'axis',
                         'observed', s.output_envelope -> 'observed',
                         'limit',    s.output_envelope -> 'limit'))
            WHEN s.step_kind = 'tenant_isolation_breach'
                     AND jsonb_typeof(s.output_envelope) = 'object'
                -- BOTH _record_breach shapes project: pre-flight (layer/offending_ids/
                -- counts) and post-flight (layer/expected_tenant/stray_tenants).
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'layer',           s.output_envelope -> 'layer',
                         'offending_ids',   s.output_envelope -> 'offending_ids',
                         'counts',          s.output_envelope -> 'counts',
                         'expected_tenant', s.output_envelope -> 'expected_tenant',
                         'stray_tenants',   s.output_envelope -> 'stray_tenants'))
            WHEN s.output_envelope IS NULL THEN NULL
            WHEN jsonb_typeof(s.output_envelope) = 'object'
                THEN COALESCE((SELECT jsonb_agg(k)
                               FROM jsonb_object_keys(s.output_envelope) AS k), '[]'::jsonb)
            ELSE '[]'::jsonb
       END AS output_envelope
FROM pipeline_runs r
LEFT JOIN pipeline_steps s ON s.run_id = r.id;
