-- 178_vt683_owner_comms_queue.sql — VT-683 P2: the session-first owner-comms queue.
--
-- WHY (Fazal ruling 2026-07-18): the owner-facing TEMPLATE surface shrinks to the whitelist
-- (OTP / welcome / wake-up). Everything else — approval asks, notices, ready reports — must ride
-- the 24h conversation session, delivered asynchronously at idle pace. Out-of-window sends can no
-- longer push a Meta template; they QUEUE here and drain when a session is open. This is the
-- durable substrate for that queue.
--
-- POINT A (Fazal 2026-07-21 — "proceed with point A"): a queued APPROVAL's decision timeout clock
-- starts at DELIVERY (``delivered_at``), never at ``queued_at`` — the owner cannot time out on an
-- ask he never saw (VT-668 honest-expiry spirit). ``decision_deadline_at`` is therefore NULL until
-- delivery, then set = ``delivered_at`` + a TTL by the delivering code. The ACTION's own business
-- freshness is re-checked SEPARATELY at resolution (a week-late festival campaign still honest-
-- declines) — the queue never makes a stale action fresh.
--
-- Lifecycle: queued -> delivered (owner saw it in an open session; decision clock starts)
--            queued -> dropped  (honest-expiry: never delivered within the max-age bound).
-- A delivered approval whose deadline passes is expired at the UNDERLYING approval object (the
-- pending-approvals row via ``decision_ref``), not by mutating this row — this row is the delivery
-- ledger, the approval object stays the money authority.

CREATE TABLE owner_comms_queue (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    -- 'approval' = owner decision needed (ranks highest, carries decision_ref + a delivery deadline);
    -- 'notice'   = informational, no decision; 'report' = a ready artifact/summary to hand over.
    kind                 TEXT NOT NULL CHECK (kind IN ('approval', 'notice', 'report')),
    -- what to render + deliver (prerendered text + a pointer). Redacted at write like every JSONB.
    payload              JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- higher drains first; approvals default above reports/notices (set by the writer).
    priority             INT NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'queued'
                             CHECK (status IN ('queued', 'delivered', 'dropped')),
    -- pointer to the underlying decision object (e.g. {"kind":"pending_approval","id":"…"}) so
    -- resolution + the action-freshness gate find the real ask. NULL for notice/report kinds.
    decision_ref         JSONB NULL,
    queued_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- when actually SHOWN to the owner in an open session. NULL until delivered. POINT A: the
    -- approval decision clock starts HERE.
    delivered_at         TIMESTAMPTZ NULL,
    -- POINT A: for kind='approval', = delivered_at + TTL, set by the delivering code at delivery.
    decision_deadline_at TIMESTAMPTZ NULL,
    -- transport sid of the freeform-in-session delivery send.
    message_sid          TEXT NULL,
    -- honest-expiry audit: 'max_age' | 'superseded' | 'resolved_elsewhere'.
    dropped_reason       TEXT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The drainer's "next deliverable for this tenant" scan: highest priority, oldest first, only queued.
CREATE INDEX owner_comms_queue_drain
    ON owner_comms_queue (tenant_id, priority DESC, queued_at)
    WHERE status = 'queued';

-- The delivered-approval deadline sweep (expire_undecided): find delivered approvals past deadline.
CREATE INDEX owner_comms_queue_deadline
    ON owner_comms_queue (decision_deadline_at)
    WHERE status = 'delivered' AND kind = 'approval';

ALTER TABLE owner_comms_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE owner_comms_queue FORCE ROW LEVEL SECURITY;

-- Tenant-scoped: a tenant's own session enqueues (writers under tenant_connection), reads (the
-- drainer picks the next item), and UPDATEs its own rows (queued -> delivered on drain). No tenant
-- DELETE (the service-role sweep drops via BYPASSRLS, like every other table's purge path).
CREATE POLICY owner_comms_queue_select ON owner_comms_queue FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY owner_comms_queue_insert ON owner_comms_queue FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY owner_comms_queue_update ON owner_comms_queue FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

-- Operator (VTR / Ops Console) de-identified read — the same operator_claim surface every tenant
-- table exposes (mig 155 idiom); assignment-scoping is enforced at the de-identified views layer.
CREATE POLICY owner_comms_queue_operator_select ON owner_comms_queue
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );

COMMENT ON TABLE owner_comms_queue IS
    'VT-683 P2 — session-first owner-comms queue. Out-of-window approvals/notices/reports queue '
    'here and drain at idle pace inside an open 24h session (no Meta template). POINT A: an '
    'approval''s decision deadline is set at delivered_at, never queued_at.';
