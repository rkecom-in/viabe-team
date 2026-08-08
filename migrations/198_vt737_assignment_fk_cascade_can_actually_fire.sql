-- VT-737 — a knowledge_card_assignments row could never be deleted once any event referenced it.
--
-- Found while running the VT-725 flip canary (gate (b)) on dev: the canary's own cleanup crashed
-- with
--
--     specialist_memory_events is append-only (VT-711); UPDATE blocked
--     CONTEXT: UPDATE ONLY specialist_memory_events SET tenant_id = NULL, assignment_id = NULL ...
--
-- Migration 186 declared the tombstone behaviour deliberately — the append-only trigger carries an
-- explicit exemption "Preserve event tombstones when a referenced assignment/card is removed" — but
-- that exemption could never fire, for TWO independent reasons:
--
--   1. The FK is COMPOSITE, `FOREIGN KEY (tenant_id, assignment_id) ... ON DELETE SET NULL`. A
--      bare SET NULL on a composite FK nulls EVERY column in it, so the cascade emits
--      `SET tenant_id = NULL, assignment_id = NULL`. The trigger's exemption requires every column
--      other than assignment_id/memory_card_id to be unchanged, and tenant_id changing to NULL
--      fails that test. The exemption was written for a single-column FK.
--   2. `tenant_id` is NOT NULL, so even with the trigger out of the way the cascade would violate
--      the column constraint.
--
-- Net effect: deleting an assignment (or a specialist memory card) raised, always. The flip
-- mechanism Fazal ratified is meant to be "changeable at runtime"; removing an override entirely
-- was a one-way door on any tenant that had ever emitted an event. Nothing in the test suite caught
-- it because nothing deleted an assignment that had a referencing event.
--
-- Fix: PostgreSQL 15+ (dev/prod are on 17.6) allows a COLUMN LIST on SET NULL, so the cascade nulls
-- only the FK's own nullable column and leaves tenant_id intact. That is exactly the shape the
-- trigger's exemption was written to allow, so the tombstone survives as designed.
--
-- The audit trail is NOT weakened. `assignment_ref` / `memory_card_ref` are separate, permanently
-- populated columns guarded by `specialist_memory_events_exactly_one_target`; they are the durable
-- record. Only the live FK pointer is cleared, which is what "tombstone" meant all along.

BEGIN;

ALTER TABLE public.specialist_memory_events
    DROP CONSTRAINT specialist_memory_events_assignment_tenant_fk;

ALTER TABLE public.specialist_memory_events
    ADD CONSTRAINT specialist_memory_events_assignment_tenant_fk
    FOREIGN KEY (tenant_id, assignment_id)
    REFERENCES public.knowledge_card_assignments (tenant_id, id)
    ON DELETE SET NULL (assignment_id);

ALTER TABLE public.specialist_memory_events
    DROP CONSTRAINT specialist_memory_events_card_tenant_fk;

ALTER TABLE public.specialist_memory_events
    ADD CONSTRAINT specialist_memory_events_card_tenant_fk
    FOREIGN KEY (tenant_id, memory_card_id)
    REFERENCES public.specialist_memory_cards (tenant_id, id)
    ON DELETE SET NULL (memory_card_id);

COMMIT;
