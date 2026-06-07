-- 116_vt279_escalation_route.sql — VT-279: the VTR/OWNER route on each escalation.
--
-- The deterministic classifier (orchestrator/owner_surface/vtr_classifier) tags each escalation at
-- creation: a business-KNOWLEDGE gap → 'vtr'; an authority/preference/identity decision → 'owner'
-- (Pillar 7, CL-426). VT-280's digest reads this to fan the right items to the VTR vs the owner —
-- and a 'vtr' item is shown only through the VT-281 de-identified views (no raw PII to the VTR).
-- Additive, nullable (pre-VT-279 rows stay NULL = unclassified). No data altered.
ALTER TABLE escalations ADD COLUMN IF NOT EXISTS route TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'escalations_route_chk') THEN
        ALTER TABLE escalations
            ADD CONSTRAINT escalations_route_chk CHECK (route IS NULL OR route IN ('vtr', 'owner'));
    END IF;
END $$;
