-- VT-734 — record WHEN an approval was actually presented to the owner.
--
-- Why this column exists (deployed-dev breach, 2026-08-06): a campaign_send approval was resolved
-- 'approved' by an owner message sent 72 SECONDS BEFORE the approval was created. The resolution
-- path never compared the two timestamps, so a message that could not possibly have been a response
-- to a plan the owner had not yet seen was consumed as consent to send to 19 customers.
--
-- Fazal's ruling (CL-2026-08-06-repeated-request-is-never-approval) requires the resolving inbound to
-- land strictly after the approval was ARMED **AND PRESENTED**. ``requested_at`` is the arm; nothing
-- recorded the presentation. ``timeout_at`` cannot substitute — it is set at delivery but stores a
-- FUTURE deadline (now() + N hours), so the delivery instant is not recoverable from it. Hence a
-- dedicated, explicit column rather than arithmetic on a deadline.
--
-- Nullable by design: rows armed before this migration have no presentation instant, and the
-- resolution invariant falls back to ``requested_at`` for them (strictly safer than no check, and it
-- would already have blocked the observed breach). Set once, at delivery, by
-- ``PendingApprovalsWrapper.start_decision_clock`` — the same idempotent write that starts the
-- owner-decision clock (VT-683 POINT A), so presentation time and clock start can never disagree.

ALTER TABLE pending_approvals
    ADD COLUMN IF NOT EXISTS presented_at timestamptz;

COMMENT ON COLUMN pending_approvals.presented_at IS
    'VT-734: when the approval ask was actually DELIVERED to the owner. The resolution invariant '
    'requires the resolving inbound to be strictly newer than this (falling back to requested_at '
    'when NULL) — a message that predates the ask can never be its decision.';
