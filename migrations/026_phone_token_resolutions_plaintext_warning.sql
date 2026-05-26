-- 026_phone_token_resolutions_plaintext_warning.sql — Cond 1 from VT-184
-- plan-review.
--
-- VT-184 Phase 1 stores phone numbers as PLAINTEXT in the
-- ``phone_number_encrypted`` column. The column name was forward-
-- looking when migration 007 + the VT-187 normalization defined the
-- schema; the actual encryption (Fernet/AES with
-- ``TEAM_PHONE_ENCRYPTION_KEY``) ships under VT-191 (filed by Cowork
-- post-merge per Cowork plan-review commitment, 2026-05-26).
--
-- This migration ATTACHES a runtime-visible COMMENT to the column so
-- anyone inspecting the schema via ``pg_description`` /
-- ``information_schema.columns`` sees the lie + the gate. Three
-- defense-in-depth layers (per Cond 1):
--   Layer 1 — this migration (pg_description; runtime-visible)
--   Layer 2 — module docstring (``phone_tokens.py``)
--   Layer 3 — function docstring (``register_phone_token``)
--
-- Pre-production gate: VT-191 (encryption rollover + back-fill) MUST
-- ship before first production tenant onboarding. Critical priority,
-- Sprint 1 per Cowork commitment.

COMMENT ON COLUMN phone_token_resolutions.phone_number_encrypted IS
    'PLAINTEXT until VT-191 encryption — DO NOT promote to prod without encryption per CL-390 privacy posture';
