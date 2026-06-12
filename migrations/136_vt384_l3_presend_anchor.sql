-- 136_vt384_l3_presend_anchor.sql — VT-384 Gap-5 PR-3: the L3 delivery-anchored hold columns.
--
-- The L3 auto-send wire (customer_send.py L3 arm) moves a batch to 'auto_send_pending', sends the
-- owner the `team_l3_presend_notice`, and records its Twilio SID here. The notice's `delivered`
-- status callback (runner.py status path) stamps `presend_notice_delivered_at` and derives
-- `send_not_before = delivered_at + hold_hours` (config/l3_autonomy.yaml). F6's anchor is DELIVERY,
-- not send: an undelivered notice = no informed silence, so the hold workflow demotes when no
-- delivery callback lands within `no_delivery_demote_minutes`.
--
-- `send_not_before` already exists on agent_draft_batches (mig-126:20, reserved). This migration
-- adds ONLY the three anchor columns. No new table; no RLS change (agent_draft_batches already
-- RLS+FORCE per mig-126). Additive NULLable columns — every existing row parses unchanged.
--
-- `auto_send_pending_at` is the HOLD-ENTRY anchor: it is stamped when enter_l3_hold flips the batch
-- to 'auto_send_pending', NOT at batch creation. The no-delivery demote window
-- (`no_delivery_demote_minutes`, config) is measured from THIS column, not `created_at` — a batch
-- drafted long before it was armed must still get its full grace from the moment the hold actually
-- began (else a stale-`created_at` batch would insta-demote on its first poll).

ALTER TABLE agent_draft_batches
    ADD COLUMN presend_notice_sid          TEXT NULL,        -- Twilio SID of team_l3_presend_notice
    ADD COLUMN presend_notice_delivered_at TIMESTAMPTZ NULL, -- F6 delivery anchor (callback-stamped)
    ADD COLUMN auto_send_pending_at        TIMESTAMPTZ NULL; -- hold-entry anchor (enter_l3_hold flip)

-- The SID-match lookup runs under the SERVICE pool (the status-callback path resolves the tenant
-- from the SID before any tenant GUC is set) — index it for that anchor write. Partial: only
-- batches that actually carry a notice SID participate.
CREATE INDEX agent_draft_batches_presend_notice_sid
    ON agent_draft_batches (presend_notice_sid)
    WHERE presend_notice_sid IS NOT NULL;
