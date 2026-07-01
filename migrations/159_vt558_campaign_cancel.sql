-- 159_vt558_campaign_cancel.sql — VT-558 (B6): a 'cancelled' terminal for a campaign true-kill.
--
-- The per-agent autonomy freeze already atomically halts a LANE's in-flight batches. VT-558 adds the
-- finer, campaign-TARGETED kill: an operator cancels ONE campaign (ops/run-control/kill-campaign),
-- the execute loop observes it at entry AND at each recipient boundary and stops the fan-out — the
-- remaining recipients are never sent. 'cancelled' is the terminal that state lands in.
--
-- The status CHECK was auto-named ``campaigns_status_check`` by the inline column CHECK in mig-016;
-- drop + re-add it with the extra value. No data rewrite — every existing row holds a still-valid
-- status (proposed|approved|rejected|sent|failed).

ALTER TABLE campaigns DROP CONSTRAINT campaigns_status_check;
ALTER TABLE campaigns ADD CONSTRAINT campaigns_status_check CHECK (status IN (
    'proposed', 'approved', 'rejected', 'sent', 'failed', 'cancelled'));
