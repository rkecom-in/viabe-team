-- 153_vt527_pending_questions.sql — VT-527 (B4): generic owner-clarification questions.
--
-- The manager's clarify/confirm loop for ARBITRARY tasks — NOT onboarding-specific. This is
-- deliberately separate from ``onboarding_journey`` (mig 123), which is singular-per-tenant and
-- reset-on-restart; a general mechanism needs MULTIPLE concurrent open questions across tasks.
-- It reuses two proven patterns instead of inventing a third:
--   * redelivery idempotency via ``last_message_sid`` (onboarding journey.handle_reply);
--   * an expiry TTL + a sweep index (pending_approvals timeout_at, mig 052).
--
-- B3's CLARIFY decision parks a task ``waiting_owner``; THIS is the mechanism that carries the
-- actual question to the owner and correlates the reply back. The VTR-in-the-loop LIVE resume
-- (a reviewer answering a paused task) inverts CL-426's async-VTR posture and is BLOCKED on Fazal
-- — this table serves the owner-ask + the keep-async default; a VTR answer path layers on later.
--
-- PII: ``question_text`` (manager-composed) + ``answer_text`` (the owner's raw reply — may carry a
-- name/number) are REDACTED at write (pii_redactor, CL-390). ``task_id``/``run_id`` are SOFT
-- pointers (no FK) — a question can precede/outlive a specific task/run (the VT-521 soft-ref
-- lesson). Tenant-scoped → RLS + FORCE + operator SELECT + dsr_purge in the SAME PR (VT-518).

CREATE TABLE pending_questions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    task_id          UUID NULL,                          -- soft ref → manager_tasks (no FK)
    run_id           UUID NULL,                          -- soft ref (no FK)
    question_kind    TEXT NOT NULL DEFAULT 'clarification' CHECK (question_kind IN
        ('clarification', 'confirmation', 'business_fact')),
    question_text    TEXT NOT NULL,                       -- REDACTED at write
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN
        ('open', 'answered', 'expired', 'cancelled')),
    answer_text      TEXT NULL,                           -- REDACTED at write (owner reply)
    last_message_sid TEXT NULL,                           -- redelivery idempotency (journey pattern)
    asked_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NULL,                    -- optional owner-response TTL
    answered_at      TIMESTAMPTZ NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pending_questions_tenant_id_uniq UNIQUE (tenant_id, id)
);
-- At most ONE open question per (tenant, task) — serialize the ask per task (a task doesn't
-- hold two open clarifications at once). Questions with no task_id are not constrained.
CREATE UNIQUE INDEX pending_questions_one_open_per_task
    ON pending_questions (tenant_id, task_id)
    WHERE status = 'open' AND task_id IS NOT NULL;
-- Expiry sweep (mirror pending_approvals): scan open, past-TTL rows.
CREATE INDEX pending_questions_expiry
    ON pending_questions (expires_at)
    WHERE status = 'open' AND expires_at IS NOT NULL;
CREATE INDEX pending_questions_tenant_status
    ON pending_questions (tenant_id, status);

ALTER TABLE pending_questions ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_questions FORCE ROW LEVEL SECURITY;
CREATE POLICY pending_questions_select ON pending_questions FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY pending_questions_insert ON pending_questions FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY pending_questions_update ON pending_questions FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY pending_questions_delete ON pending_questions FOR DELETE
    USING (tenant_id = app_current_tenant());

CREATE POLICY pending_questions_operator_select ON pending_questions
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );
