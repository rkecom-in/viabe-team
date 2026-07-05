-- 166_vt606_limit_exhausted_incident_kind.sql — VT-606 (durable manager loop) team-lead ruling:
-- add 'limit_exhausted' to incidents.incident_kind.
--
-- manager_task_workflow's revision/cycle-limit exhaustion needs its own self-describing incident
-- kind for ops queries — do not overload 'other' (no signal) or 'failed_run' (implies a run
-- failure, not a plan/step budget cap). Additive: widen the existing CHECK constraint, drop +
-- re-add (no data rewrite — every existing row holds a still-valid kind). Mirrors the
-- 159_vt558_campaign_cancel.sql precedent for widening a status/kind CHECK.

ALTER TABLE incidents DROP CONSTRAINT incidents_incident_kind_check;
ALTER TABLE incidents ADD CONSTRAINT incidents_incident_kind_check CHECK (incident_kind IN (
    'silent_terminal', 'failed_run', 'owner_unreachable', 'other', 'limit_exhausted'));
