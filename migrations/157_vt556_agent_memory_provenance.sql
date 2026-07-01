-- 157_vt556_agent_memory_provenance.sql — VT-556: provenance + authority for the VTR teach-loop.
--
-- VT-550 shipped the agent_memory MECHANISM (seed/learn + default-closed retrieval). VT-556 closes
-- the human-as-teacher loop: a VTR ingests a strategy/behavioural directive on a tenant page and the
-- Team Manager PICKS IT UP on its next run. A VTR-authored directive is NOT anonymous — it carries
-- WHO wrote it (provenance) and WHICH authority class it holds (contamination control, per the C3
-- learning-loop safety posture). The two columns below default to the pre-VT-556 posture so every
-- existing seed/learned row is unaffected.
--
--   authored_by_operator_id — the VERIFIED operator id (require_vtr_action's claim id) that authored
--     the directive. Soft pointer (no FK to an operator table; the operators substrate is JWT-issued,
--     not a local table). NULL = a non-operator write (seed / owner / system / the VT-550 learned path).
--   authority — the provenance CLASS. 'seed' (archetype/tenant seed, the pre-556 default), 'vtr'
--     (a VTR human-as-teacher directive — the only class a VTR endpoint may write), 'owner' (owner-
--     supplied), 'system' (agent self-learned). Retrieval/contradiction resolution can weight by
--     authority later; today it is provenance-of-record + the audit key.

ALTER TABLE agent_memory
    ADD COLUMN authored_by_operator_id UUID NULL,
    ADD COLUMN authority TEXT NOT NULL DEFAULT 'seed'
        CHECK (authority IN ('seed', 'vtr', 'owner', 'system'));

COMMENT ON COLUMN agent_memory.authored_by_operator_id IS
    'VT-556: verified operator id that authored a VTR directive (soft, no FK); NULL for non-operator writes.';
COMMENT ON COLUMN agent_memory.authority IS
    'VT-556: provenance class — seed|vtr|owner|system; a VTR directive is authority=vtr.';
