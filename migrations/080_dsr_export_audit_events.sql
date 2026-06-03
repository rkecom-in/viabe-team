-- 080_dsr_export_audit_events.sql — VT-77: extend the privacy_audit_log
-- event_type CHECK with the two self-serve DSR-export events.
--
-- This is the "grow the CHECK per row" path VT-80 (mig 079) set up: add an
-- event_type ONLY when its event is actually emitted. VT-77's dsr_export.py
-- emits dsr_export_requested + dsr_export_completed through log_privacy_event.
--
-- DDL on the constraint is not blocked by the append-only ROW trigger
-- (that trigger fires on UPDATE/DELETE of rows, not on ALTER TABLE).

ALTER TABLE privacy_audit_log
    DROP CONSTRAINT privacy_audit_log_event_type_chk;

ALTER TABLE privacy_audit_log
    ADD CONSTRAINT privacy_audit_log_event_type_chk
    CHECK (event_type IN (
        'phone_token_resolved',
        'subject_data_purged',
        'subject_data_purged_table',
        'dsr_export_requested',
        'dsr_export_completed'
    ));
