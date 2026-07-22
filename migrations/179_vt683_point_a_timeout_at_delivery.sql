-- 179_vt683_point_a_timeout_at_delivery.sql — VT-683 P2c: POINT A, the decision clock
-- starts at DELIVERY, never at arm.
--
-- WHY (Fazal ruling 2026-07-21 — "proceed with point A"): a queued approval's decision
-- timeout must not tick while the ask sits undelivered in the owner-comms queue — the owner
-- cannot time out on an ask he never saw (VT-668 honest-expiry spirit). Migration 052 made
-- ``pending_approvals.timeout_at`` NOT NULL because the clock started at arm; P2c moves the
-- clock to delivery (``arm_pause_request`` inserts NULL, then sets it the moment the ask is
-- actually SENT to the owner — in-session interactive or the out-of-window template belt).
--
-- Sweep safety: the approval-timeout sweep scans ``resolved_at IS NULL AND timeout_at <= now()``
-- (scheduled_triggers._scan_timed_out_approvals) — a NULL timeout_at simply never matches, so an
-- undelivered ask can never be reaped as 'timed_out'. The owner-comms queue's own max-age drop
-- (P2c sweep) starts the clock (timeout_at = now()) when it drops a never-delivered approval, so
-- the underlying row still expires through the ONE existing choke point instead of a second
-- resolution path.
--
-- No data change: every existing row already carries a non-NULL timeout_at and keeps it.

ALTER TABLE pending_approvals ALTER COLUMN timeout_at DROP NOT NULL;

COMMENT ON COLUMN pending_approvals.timeout_at IS
    'VT-683 POINT A: the owner-decision deadline. NULL until the ask is DELIVERED to the owner '
    '(arm inserts NULL; the delivering code sets delivered-time + TTL). The timeout sweep only '
    'reaps non-NULL rows, so an undelivered ask never times out.';
