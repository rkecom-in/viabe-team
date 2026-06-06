-- 113_vt357_escalation_sla_marker.sql — VT-357 part 2: SLA-breach-alerted marker.
--
-- A SECOND Fazal alert fires when an OPEN escalation breaches its SLA (4h if opened during
-- business hours 10am–7pm IST, else 24h). `sla_alerted_at` makes the hourly sweep alert ONCE per
-- breach (idempotent — it won't re-ping the same unresolved escalation every hour). Pure-additive
-- (nullable column); no data altered. Cowork pre-approved the marker-migration approach (VT-357).
ALTER TABLE escalations ADD COLUMN IF NOT EXISTS sla_alerted_at TIMESTAMPTZ NULL;
