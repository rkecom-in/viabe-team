-- VT-741 — 'read' becomes its own delivery state instead of being collapsed into 'delivered'.
--
-- WHY
-- ---
-- Fazal ratified the per-customer frequency rule on 2026-08-10. Its middle tier — Tier B, a 3-day
-- interval for a customer who "read or clicked or replied in the last 10 messages within 90 days" —
-- depends on a READ signal.
--
-- Twilio already sends it. We already receive it. And we throw it away one line before it would
-- persist: `agents/customer_send.py::_DELIVERY_STATE_MAP` maps `"read" -> "delivered"`, because
-- mig 161's CHECK admits only ('delivered', 'failed', 'undelivered'). The entire middle tier rests
-- on a signal that arrives and is discarded.
--
-- WHAT THIS CHANGES
-- -----------------
-- Adds 'read' to the CHECK. Nothing else. The column stays NULL-able ("delivery unknown" remains
-- the honest default for pre-VT-564 sends), and no existing row is rewritten — a historical
-- 'delivered' row genuinely may or may not have been read, and back-filling a guess would poison
-- the exact signal this migration exists to make trustworthy.
--
-- ORDERING (the part that is easy to get wrong)
-- --------------------------------------------
-- A message goes delivered -> read, so these are TWO callbacks for one sid. The reconcile UPDATE
-- is first-write-wins (`AND delivery_status IS NULL`), which means the 'delivered' callback claims
-- the row and the later 'read' callback finds it non-NULL and silently no-ops. Adding the CHECK
-- value alone would therefore change nothing observable.
--
-- The application side (same VT-741 change) permits exactly ONE upgrade — 'delivered' -> 'read' —
-- and nothing else. 'read' is strictly more information than 'delivered'; every other transition
-- stays forbidden, so a delivery FAILURE can never be overwritten by a late positive callback.
-- That asymmetry is deliberate: losing a read costs a customer one tier of politeness, while
-- overwriting a failure would tell us a message landed when it did not.

ALTER TABLE public.agent_customer_contacts
    DROP CONSTRAINT IF EXISTS agent_customer_contacts_delivery_status_check;

ALTER TABLE public.agent_customer_contacts
    ADD CONSTRAINT agent_customer_contacts_delivery_status_check
    CHECK (delivery_status IN ('delivered', 'read', 'failed', 'undelivered'));

COMMENT ON COLUMN public.agent_customer_contacts.delivery_status IS
    'Terminal delivery outcome from the Twilio status callback (VT-564). NULL = unknown (the '
    'honest default for pre-VT-564 sends). VT-741: ''read'' is its own state, not folded into '
    '''delivered'' — the per-customer frequency tiers need it. Only one upgrade is permitted, '
    '''delivered'' -> ''read''; a failure state is never overwritten.';
