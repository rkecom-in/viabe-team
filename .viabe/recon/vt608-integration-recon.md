# VT-608 recon + team-lead rulings (2026-07-05)

## Verified map (sonnet recon, file:line in session history)
- Tool surface: list/start/pull real for Shopify; mapping tools = stubs; recurring real (VT-603-scoped).
  NET-NEW: read_integration_state (fn exists shopify_onboarding.py:266), check_oauth_status,
  propose/confirm_mapping (MUST call integrations/field_mapping.py — the real unwired reasoner,
  CL-104 confidence routing), commit_ingestion, verify_connector, schedule_recurring_pull (reuse
  integrations/scheduler.py — real DBOS 5-min fan-out, daily-cadence parser).
- Raw get_pool() sites owed: shopify.py:592,649,699,866 + shopify_onboarding.py:707 + (NEW finding)
  google_sheet.py:180,208,403,488,521,553.
- Phase persistence: tenant_integration_state (mig 031) + the _write_state UPSERT-before-send pattern.
- OAuth callbacks stateless; resume is reactive-on-next-message today (no wake).
- Reusable: 5 canaries (vt206/207/208/210/222) + 59 existing tests across the surface.

## RULINGS (binding for the VT-608 build)
1. **Two control paths:** the deterministic runner gate (runner.py:908) STAYS — it is the
   legacy/shadow production behavior and the LLM-down floor. In ENFORCE mode the loop owns
   integration objectives: the runner gate DEFERS (deterministic check: an active loop task with a
   current integration_agent step exists for the tenant → gate returns None) and the loop's
   specialist uses the same tenant_integration_state truth. No dual-writer races: both paths write
   through the same phase-state functions; the defer check prevents concurrent ownership. Full
   gate retirement is NOT this row.
2. **Sheets picker:** WA-in-app-browser link-out per CL-443 — minimal team-web page (post-OAuth
   list spreadsheets/tabs → POST selection to an INTERNAL_API_SECRET-guarded endpoint → persists to
   pending_owner_input/phase state). Thin but in scope; no manual credential paste (CL-421).
3. **commit_ingestion:** VT-268's fail-closed guardrail STANDS. The tool returns a typed
   PROPOSAL (effect-intent style); the actual ingestion commit executes SERVER-SIDE in the
   workflow step after manager_review accepts — mirrors the campaign effect rail exactly.
4. **Callback resume:** NO new wake plumbing. Integration waits reuse the workflow's existing
   ask_owner-style poll loop; the callback persists token/state (already does), the poll's
   deterministic check (shopify_is_connected / sheets equivalent) picks it up next tick. Documented
   latency = poll interval. Event-driven wake = a future optimization row, not Phase 1.
5. **google_sheet.py raw pools:** IN SCOPE — same defect class, fix in the same sweep as the
   Shopify sites.
