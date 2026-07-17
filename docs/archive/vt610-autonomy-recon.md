> **ARCHIVED 2026-07-17 — zero live authority; see docs/README.md.**

# VT-610 recon (2026-07-05, haiku scan — verified file:line map)
Key build facts:
- force_l3 seam: extend ops_vtr_console.py:571-643 action Literal + new autonomy.py force_l3()
  primitive (level='L3' IN-TXN like grant_l3; NO batch cancel; tm_audit autonomy_change with
  fail-closed emit-or-rollback like siblings).
- NEW columns (allocator migration): tenant_agent_autonomy.l3_force_granted_at TIMESTAMPTZ NULL +
  l3_force_granted_by_vtr TEXT NULL. Update vtr_agent_autonomy view (mig 130) to expose them
  (CL-390: still no revoke_reason).
- Gate: require_vtr_action() (ops_common.py) — X-Internal-Secret + X-Operator-Jwt + operator↔tenant
  assignment; reason scrub_pii + 500 clamp; ops_audit metadata-only same-txn.
- Takeover already atomic (takeover.py:37-82: workflow_controls pause + per-agent freeze incl.
  batch cancellation via cancel_open_batches autonomy.py:148-171). Release never promotes. Package 7
  needs NO takeover changes — verify + test only.
- MUST-NOT-BYPASS rail list (tests enumerate each): Gate-0 activation (onboarding_gate), per-recipient
  consent/opt-out/complaint/caps (customer_send gate stack), policy boundary
  (business_policy.assert_within_policy), always-confirm floor (autonomy.py:553-582 —
  first-contact/bulk>20/money/novel-template), business-impact gates (business_impact_choke +
  tenant_business_autonomy — force_l3 grants NOTHING here), RLS scoping, regression freeze
  (a regression freezes forced-L3 identically; force grants level, never immunity).
- Provenance display: earned = l3_granted_at set (approval FK); forced = l3_force_granted_at set;
  UI reads /api/orchestrator/ops/vtr-agent-state (ops_vtr_console.py:321-350).
Full detail: the recon message in session history 2026-07-05; primitives at autonomy.py:34-582.
