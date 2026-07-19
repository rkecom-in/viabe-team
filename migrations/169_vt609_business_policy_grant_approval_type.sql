-- 169_vt609_business_policy_grant_approval_type.sql — VT-609 fix round (team-lead review CRITICAL):
-- the pending_approvals approval_type extension for the onboarding-conductor's business-policy
-- PROPOSAL.
--
-- Context: business_policy.grant_business_policy is a MONEY-BEARING, machine-enforceable-bounds
-- write (the deterministic guard EVERY autonomous customer_send/spend action reads). Its own
-- docstring always said "a specialist can PROPOSE a policy... but only the owner's resolution
-- calls this" — but it had ZERO callers until the onboarding_conductor specialist (VT-609), which
-- initially called it DIRECTLY from the specialist's own tool-call turn (a Pillar-7 violation: no
-- owner-approval provenance, no bounds validation, granted_by NULL). This migration is the DB half
-- of the fix: a new approval_type so the specialist's ``propose_business_policy`` tool can ARM a
-- durable, resolvable pending_approvals row carrying the PROPOSED bounds (mirrors
-- business_impact_choke's dispatch_autonomy_offer -> resolve_and_grant_l3 shape) instead of
-- granting directly. Only the owner's explicit resolution (a SEPARATE tool call, triggered by the
-- specialist recognizing the owner's own yes/no to the SPECIFIC bounds it just showed them) calls
-- grant_business_policy, tying the grant to this approval row's id (granted_by provenance).
--
-- The ApprovalType Literal in agent/tools/request_owner_approval.py is the source of truth
-- (CL-428); this keeps the DB CHECK in exact sync, same PR.

ALTER TABLE pending_approvals DROP CONSTRAINT pending_approvals_approval_type_check;
ALTER TABLE pending_approvals ADD CONSTRAINT pending_approvals_approval_type_check
    CHECK (approval_type IN (
        'campaign_send', 'cohort_size_exceeded', 'sensitive_data_access', 'other',
        'agent_customer_send', 'autonomy_upgrade', 'l3_presend_notice',
        'business_impact_action', 'business_policy_grant'));
