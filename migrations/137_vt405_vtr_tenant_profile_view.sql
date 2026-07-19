-- 137_vt405_vtr_tenant_profile_view.sql — VT-405 Part A: the VTR tenant-profile read view.
--
-- The Ops Console tenant page must show, the moment a founder signs up: signup fields + the
-- auto-discovered `business_profile_draft` + per-field confirmation status — VTR-role-scoped,
-- assignment-scoped, and non-PII (CL-390/CL-425/CL-426). `app_vtr_role` has ZERO grant on the raw
-- tables, so the ONLY legal door is a `vtr_*` view read through `privacy/vtr.vtr_connection()`.
--
-- This view JOINs the de-identified signup surface (tenants) + the discovered draft
-- (business_profile_draft, mig-122) + the onboarding journey (for the stage strip) + the canonical
-- confirmed profile (l1_entities business_profile) projected KEYS-ONLY.
--
-- PII posture:
--   * Signup/header value fields are non-PII: business_name (already on the VTR surface as
--     tenant_name, mig-134), phase, plan_tier, business_type, locality, city_tier, language, and the
--     signup/trial timestamps.
--   * Founder section: owner_name (owner_contact — the FOUNDER the VTR assists, not a third-party
--     customer) + the WhatsApp number MASKED to last-4 ONLY (`right(...,4)`); the raw number never
--     leaves the DB under app_vtr_role.
--   * Discovered DRAFT attributes/provenance are shown WITH VALUES — they are public business facts
--     sourced from GBP/website (auto_discovery), not customer PII.
--   * CONFIRMED canonical profile is exposed KEYS-ONLY (`confirmed_fields`) — owner free-text values
--     can be PII, so confirmation status is key-presence, never values (the mig-130 diff-keys rule).
--     `_field_provenance` (the VT-405 Part-B status sibling key) is excluded from confirmed_fields.
--
-- Assignment-scope predicate (mig-134 pattern, the CL-426 multi-VTR floor): admin role sees all;
-- a VTR sees ONLY tenants with an ACTIVE operator_assignments row. Unset GUC ⇒ app_vtr_operator()
-- NULL ⇒ matches nothing (fail-closed). Defense-in-depth UNDER require_vtr_action's per-tenant gate.

CREATE OR REPLACE VIEW vtr_tenant_profile AS
    SELECT
        t.id                                              AS tenant_id,
        t.business_name,
        t.phase,
        t.plan_tier,
        t.business_type,
        t.locality,
        t.city_tier,
        t.language_preference,
        t.preferred_language,
        t.signed_up_at,
        t.trial_started_at,
        t.phase_entered_at,
        t.owner_contact                                   AS owner_name,
        right(t.whatsapp_number, 4)                       AS whatsapp_last4,
        d.attributes                                      AS draft_attributes,
        d.provenance                                      AS draft_provenance,
        d.created_at                                      AS draft_created_at,
        d.updated_at                                      AS draft_updated_at,
        j.status                                          AS onboarding_status,
        coalesce(jsonb_array_length(j.question_queue), 0) AS onboarding_queue_len,
        cp.confirmed_fields
    FROM tenants t
    LEFT JOIN business_profile_draft d ON d.tenant_id = t.id
    LEFT JOIN onboarding_journey j ON j.tenant_id = t.id
    LEFT JOIN LATERAL (
        SELECT array_agg(k.key ORDER BY k.key) AS confirmed_fields
        FROM l1_entities e, jsonb_object_keys(e.attributes) AS k(key)
        WHERE e.tenant_id = t.id
          AND e.entity_type = 'business_profile'
          AND e.valid_to IS NULL
          AND k.key <> '_field_provenance'
    ) cp ON true
    WHERE current_user = 'app_vtr_admin_role'
       OR t.id IN (SELECT tenant_id FROM operator_assignments
                   WHERE operator_id = app_vtr_operator() AND unassigned_at IS NULL);

GRANT SELECT ON vtr_tenant_profile TO app_vtr_role, app_vtr_admin_role;
