"""VT-267 PR-B — first-data-step onboarding machinery.

Invokable given an existing tenant_id (live entry-wiring via create_tenant is the
separate D1 entry PR). Three pieces:
- ``method_selector`` — Haiku ranker: which record-keeping method to suggest first.
- ``floor`` — the floor state machine (infer → propose/confirm → confirmed; ghost →
  HOLD-safe-minimal) persisted in tenant_integration_state.pending_owner_input.
"""
