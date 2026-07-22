-- 181_vt683_last_wakeup_at.sql — VT-683 P3: the daily wake-up ≤1/day bookkeeping column.
--
-- WHY: the daily wake-up loop (team_wakeup2) sends AT MOST one wake-up per tenant per day. That
-- per-day bound needs DURABLE state — an in-memory guard would re-send after every process restart
-- (the cron fires 10:30 IST daily; a restart between two ticks must not double-wake). This column
-- records the last wake-up send; the wake-up sweep (scheduled_triggers.run_wakeup_sweep_body) skips
-- a tenant whose last_wakeup_at is within the min-interval window (owner_surface.wakeup.wakeup_due).
--
-- RLS on tenants is already enabled (mig 001); a new column needs no new policy. A tenant updates
-- its own row through tenant_connection (same path record_observed_language uses). NULL = never
-- woken (eligible on the first qualifying slot).
ALTER TABLE tenants ADD COLUMN last_wakeup_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN tenants.last_wakeup_at IS
    'VT-683 P3 — timestamp of the last daily wake-up (team_wakeup2) send. NULL = never woken. The '
    'wake-up sweep enforces <=1/day by skipping a tenant woken within WAKEUP_MIN_INTERVAL.';
