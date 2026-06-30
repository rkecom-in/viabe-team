-- 148_vt517_ownership_gate.sql — VT-517: ownership verification = VTR-human-only.
--
-- SUPERSEDES VT-411 (mig 142 owner_channel_verified, the OTP/DIN owner-channel
-- signal). VT-411 never gated execution — it was informational. VT-517 makes
-- ownership a REAL, non-bypassable EXECUTION prerequisite (wired into the agent
-- send/execute Gate-0 via activation_registry) and moves verification to a VTR
-- human review. Fold/replace, no dual ownership path.
--
-- Renames (carry the DEFAULT false + any data, zero-cost) the VT-411 columns to
-- the VT-517 canonical names, then adds the VTR review state machine. Existing
-- tenants land ownership_verified=false / ownership_status='pending' → blocked
-- from EXECUTION (send/act) until a VTR marks them verified. Setup (onboarding/
-- connect/configure) is unaffected (it never reads the activation gate).
--
-- PROD NOTE (dev→main promotion, Fazal-authorized): existing real verified
-- tenants must be backfilled (ownership_verified=true, ownership_status=
-- 'verified') BEFORE this lands on prod, or they stop sending. On dev all
-- tenants are test/bogus so pending-by-default is the intended state.

-- 1) Rename the VT-411 columns to the VT-517 canonical names (idempotent-safe:
--    only runs if the old name still exists).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema='public' AND table_name='tenants'
                 AND column_name='owner_channel_verified') THEN
        ALTER TABLE public.tenants RENAME COLUMN owner_channel_verified TO ownership_verified;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema='public' AND table_name='tenants'
                 AND column_name='owner_channel_verified_at') THEN
        ALTER TABLE public.tenants RENAME COLUMN owner_channel_verified_at TO ownership_verified_at;
    END IF;
END $$;

-- 2) VTR review state machine + reviewer audit fields (net-new).
ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS ownership_status        TEXT        NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS ownership_reviewer_note TEXT        NULL,
    ADD COLUMN IF NOT EXISTS ownership_reviewer_evidence TEXT    NULL,
    ADD COLUMN IF NOT EXISTS ownership_reviewed_at   TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS ownership_reviewed_by   TEXT        NULL;

-- CHECK constraint for the status enum (added separately so ADD COLUMN IF NOT
-- EXISTS stays clean; guard against re-run).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='tenants_ownership_status_chk') THEN
        ALTER TABLE public.tenants
            ADD CONSTRAINT tenants_ownership_status_chk
            CHECK (ownership_status IN ('pending','verified','rejected'));
    END IF;
END $$;

-- 3) Reconcile status with any carried-over verified flag (dev: no-op; prod
--    backfill is separate + Fazal-authorized).
UPDATE public.tenants SET ownership_status='verified'
    WHERE ownership_verified = true AND ownership_status <> 'verified';

-- 4) Surface ownership on the VTR profile view so the Ops Console detail page +
--    pending queue can read it. CREATE OR REPLACE with the mig-138 body verbatim
--    + two trailing columns (REPLACE-safe: same leading columns, appended only).
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
            AND e.valid_to IS NULL)                       AS field_provenance,
        t.ownership_verified                              AS ownership_verified,
        t.ownership_status                                AS ownership_status
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

CREATE INDEX IF NOT EXISTS tenants_ownership_pending_idx
    ON public.tenants (ownership_status)
    WHERE ownership_status = 'pending';
