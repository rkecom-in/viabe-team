-- 138_vt405_vtr_tenant_profile_ownername_provenance.sql — VT-405 Part B (+ Part A owner_name fix).
--
-- Two changes to vtr_tenant_profile (mig 137), CREATE OR REPLACE (owner_name source swapped in place;
-- field_provenance appended — REPLACE-safe):
--
-- 1) owner_name FIX (Fazal: "owner name is missing"). Signup writes owner_name into the canonical
--    `business_profile` L1 entity (l1_entities.attributes->>'owner_name'), NOT tenants.owner_contact
--    (which is NULL post-signup). Read it from the entity.
--
-- 2) field_provenance (Part B badges): expose the entity's `_field_provenance` map
--    ({field: {source, status, confirmed_by, at}}) so the UI can badge each confirmed field
--    VTR-asserted (source='vtr') vs owner-confirmed (absent/owner). METADATA ONLY — source/status/
--    operator/ts, never a field VALUE (the keys-only/PII rule holds: confirmed VALUES still never surface).
--
-- Both via correlated scalar subqueries on the single (valid_to IS NULL) business_profile entity.

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
        (SELECT e.attributes ->> 'owner_name'
           FROM l1_entities e
          WHERE e.tenant_id = t.id AND e.entity_type = 'business_profile'
            AND e.valid_to IS NULL)                       AS owner_name,
        right(t.whatsapp_number, 4)                       AS whatsapp_last4,
        d.attributes                                      AS draft_attributes,
        d.provenance                                      AS draft_provenance,
        d.created_at                                      AS draft_created_at,
        d.updated_at                                      AS draft_updated_at,
        j.status                                          AS onboarding_status,
        coalesce(jsonb_array_length(j.question_queue), 0) AS onboarding_queue_len,
        cp.confirmed_fields,
        (SELECT e.attributes -> '_field_provenance'
           FROM l1_entities e
          WHERE e.tenant_id = t.id AND e.entity_type = 'business_profile'
            AND e.valid_to IS NULL)                       AS field_provenance
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

-- GRANTs preserved by CREATE OR REPLACE; re-stated for clarity (idempotent).
GRANT SELECT ON vtr_tenant_profile TO app_vtr_role, app_vtr_admin_role;
