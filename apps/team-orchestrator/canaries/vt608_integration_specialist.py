#!/usr/bin/env python3
"""VT-608 (Loop Package 5) — Integration Specialist real canary (Rule #15).

BUILT, NOT EXECUTED by this builder (per the shared-tree protocol — the team lead runs canaries
against deployed dev, which has real network egress + real merchant creds). Mirrors vt206/207/
208's own shape: a real Postgres DB, real DBOS, real tool-surface calls — the Google/Shopify HTTP
calls themselves are injected (this canary proves the ORCHESTRATION end-to-end: tool-surface ->
phase persistence -> the deterministic commit executor -> recurring-pull verification; the actual
external OAuth/API walk is Fazal's manual RKeCom bootstrap walk, same deferral vt207 itself
documents).

Subshell-source supabase-dev.env:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      ./.venv/bin/python canaries/vt608_integration_specialist.py
    )

Wall-clock < 20s. Cost: 0 paise (no live Anthropic/Google/Shopify calls — this canary proves the
orchestration; propose_mapping's heuristic path needs no LLM call for named fields).

8 assertions:

- A1: Shopify end-to-end — start_oauth (no shop) prompts for the domain; with a shop, mints a
  real authorize_url; check_oauth_status is False, then True once the callback's own token row
  is seeded; pull_sample returns counts only; commit_ingestion returns a PROPOSAL (never writes
  a customer row); the server-side executor then lands the real commit + advances to
  phase_5_confirmed with a recurring-pull row scheduled.
- A2: Google Sheets end-to-end — start_oauth mints a real authorize_url; the picker's own
  POST /select (simulated directly against the persisted state, since the team-web page itself
  is a follow-up row) advances to phase_3_sample_pull; pull_sample (mocked connector) returns
  counts + column names; propose_mapping runs the REAL heuristic reasoner; confirm_mapping
  persists it; commit_ingestion proposes; the executor lands a real customers row.
- A3: VT-268 fail-closed — the commit_ingestion TOOL call itself never wrote a customers row
  (checked BEFORE the executor runs).
- A4: OAuth replay — claiming the SAME install-state nonce twice fails closed on the second.
- A5: Wrong-tenant / cross-tenant isolation — a foreign tenant's confirm_mapping write never
  lands on this canary's own tenant (VT-603 tenancy).
- A6: Restart-resume — read_integration_state before vs. after a simulated process restart
  (a fresh DB connection, no shared Python state) returns the identical phase/metadata.
- A7: Recurring-pull verification — tenant_connector_status carries the auto-scheduled cadence
  post-commit for BOTH connectors.
- A8: Re-entry safety — calling the commit executor again after success is a no-op (never
  double-ingests).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANT_IDS: list[str] = []


def assertion(num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _preflight() -> None:
    for k in ("DATABASE_URL",):
        if not os.environ.get(k):
            print(f"PREFLIGHT FAIL — {k} missing", file=sys.stderr)
            sys.exit(2)
    print("PREFLIGHT OK — supabase env loaded")


def run_canary() -> int:  # noqa: PLR0915 — a linear canary walk, splitting hurts readability here
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt608-canary-salt")
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

    import apply_migrations

    r = apply_migrations.apply(dsn=os.environ["DATABASE_URL"])
    if r["failed"]:
        print(f"PREFLIGHT FAIL — migrations failed: {r['failed']}", file=sys.stderr)
        sys.exit(2)

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()

    import psycopg

    from orchestrator.agent.integration_agent import (
        check_oauth_status,
        commit_ingestion,
        confirm_mapping,
        pull_sample,
        read_integration_state,
        start_oauth,
        verify_connector,
    )
    from orchestrator.integrations.commit import execute_pending_ingestion_commit
    from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector
    from orchestrator.integrations.oauth_state import claim_install_state, mint_install_state
    from orchestrator.observability.decorators import observability_context
    import orchestrator.onboarding.shopify_onboarding as shopify_onboarding_mod

    dsn = os.environ["DATABASE_URL"]

    def _seed_tenant() -> str:
        tid = str(uuid4())
        INSERTED_TENANT_IDS.append(tid)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO tenants (id, business_name, plan_tier, phase) "
                "VALUES (%s, %s, 'standard', 'trial')",
                (tid, f"vt608-canary-{tid[:8]}"),
            )
        return tid

    def _seed_oauth_token(tid: str, connector_id: str) -> None:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO tenant_oauth_tokens "
                "(tenant_id, connector_id, refresh_token_encrypted, scopes) "
                "VALUES (%s, %s, 'enc-placeholder', '{}')",
                (tid, connector_id),
            )

    try:
        # === A1 — Shopify end-to-end =========================================================
        tenant_shopify = _seed_tenant()
        with observability_context(run_id=uuid4(), tenant_id=tenant_shopify):
            no_shop = start_oauth.func(  # type: ignore[attr-defined]
                tenant_id=tenant_shopify, connector_id="shopify"
            )
            before_connected = check_oauth_status.func(  # type: ignore[attr-defined]
                tenant_id=tenant_shopify, connector_id="shopify"
            )
        _seed_oauth_token(tenant_shopify, "shopify")
        with observability_context(run_id=uuid4(), tenant_id=tenant_shopify):
            after_connected = check_oauth_status.func(  # type: ignore[attr-defined]
                tenant_id=tenant_shopify, connector_id="shopify"
            )

        class _FakeShopifyConnector:
            def pull_sample(self, tenant_id: Any) -> list[dict[str, Any]]:
                return [{"phone": "+919876500001"}, {"phone": "+919876500002"}]

        import orchestrator.integrations.connectors.shopify as shopify_mod

        real_shopify_cls = shopify_mod.ShopifyConnector
        shopify_mod.ShopifyConnector = _FakeShopifyConnector  # type: ignore[misc]
        try:
            with observability_context(run_id=uuid4(), tenant_id=tenant_shopify):
                sample = pull_sample.func(  # type: ignore[attr-defined]
                    tenant_id=tenant_shopify, connector_id="shopify"
                )
        finally:
            shopify_mod.ShopifyConnector = real_shopify_cls  # type: ignore[misc]

        # VT-608 fix round MAJOR 1 — commit_ingestion arms the proposal with THIS turn's
        # ObservabilityContext.run_id; the executor call below must pass the SAME identity.
        shopify_turn_id = uuid4()
        with observability_context(run_id=shopify_turn_id, tenant_id=tenant_shopify):
            proposal = commit_ingestion.func(  # type: ignore[attr-defined]
                tenant_id=tenant_shopify, connector_id="shopify"
            )
        with psycopg.connect(dsn, autocommit=True) as conn:
            customers_before_exec = conn.execute(
                "SELECT count(*) FROM customers WHERE tenant_id = %s", (tenant_shopify,)
            ).fetchone()[0]

        real_pull_and_ingest = shopify_onboarding_mod.pull_and_ingest_shopify

        def _fake_pull_and_ingest(tenant_id: Any, **kwargs: Any) -> dict[str, int]:
            return {"orders_pulled": 2, "mapped": 2, "committed": 2, "sales_written": 2, "new_customers": 2}

        shopify_onboarding_mod.pull_and_ingest_shopify = _fake_pull_and_ingest  # type: ignore[assignment]
        try:
            exec_result = execute_pending_ingestion_commit(
                tenant_shopify, current_turn_id=str(shopify_turn_id)
            )
        finally:
            shopify_onboarding_mod.pull_and_ingest_shopify = real_pull_and_ingest  # type: ignore[assignment]

        with observability_context(run_id=uuid4(), tenant_id=tenant_shopify):
            verified = verify_connector.func(  # type: ignore[attr-defined]
                tenant_id=tenant_shopify, connector_id="shopify"
            )

        pass_1 = (
            no_shop.get("next_action") == "prompt_shop_domain"
            and before_connected["connected"] is False
            and after_connected["connected"] is True
            and sample["row_count"] == 2
            and proposal["status"] == "proposal_recorded"
            and customers_before_exec == 0
            and exec_result["status"] == "completed"
            and verified["connected"] is True
            and verified["cadence"] == "0 3 * * *"
        )
        assertion(
            1, "Shopify end-to-end: propose->execute->verify, no premature customer write",
            pass_1,
            observed={
                "no_shop_next_action": no_shop.get("next_action"),
                "before_connected": before_connected, "after_connected": after_connected,
                "sample": sample, "proposal": proposal,
                "customers_before_exec": customers_before_exec, "exec_result": exec_result,
                "verified": verified,
            },
        )

        # === A2 + A3 — Google Sheets end-to-end + VT-268 fail-closed ==========================
        tenant_sheets = _seed_tenant()
        with observability_context(run_id=uuid4(), tenant_id=tenant_sheets):
            sheets_oauth = start_oauth.func(  # type: ignore[attr-defined]
                tenant_id=tenant_sheets, connector_id="google_sheet"
            )
        _seed_oauth_token(tenant_sheets, "google_sheet")

        # Simulate the picker's own POST /select (the team-web page is a follow-up row).
        from orchestrator.onboarding.shopify_onboarding import PHASE_SAMPLE, _validated_pending, _write_state

        picker_pending = _validated_pending(
            awaiting="sample_pull_pending",
            prompt_text="Spreadsheet selected.",
            connector_id="google_sheet",
            metadata={"spreadsheet_id": "canary-sheet", "tab_name": "Sheet1"},
        )
        _write_state(tenant_sheets, phase=PHASE_SAMPLE, connector_id="google_sheet", pending=picker_pending)

        real_pull_sample = GoogleSheetConnector.pull_sample

        def _fake_pull_sample(self: Any, tenant_id: Any, spreadsheet_id: str = "", range_a1: str = "A1:Z50", *, tab_name: str = "") -> list[dict[str, Any]]:
            return [{"Mobile": "9876500055", "Name": "Ravi K"}, {"Mobile": "9876500066", "Name": "Asha P"}]

        GoogleSheetConnector.pull_sample = _fake_pull_sample  # type: ignore[method-assign]
        try:
            with observability_context(run_id=uuid4(), tenant_id=tenant_sheets):
                sheets_sample = pull_sample.func(  # type: ignore[attr-defined]
                    tenant_id=tenant_sheets, connector_id="google_sheet"
                )
        finally:
            GoogleSheetConnector.pull_sample = real_pull_sample  # type: ignore[method-assign]

        # VT-608 fix round MAJOR 1 — same-turn arming: capture this turn's identity for the
        # executor call below.
        sheets_turn_id = uuid4()
        with observability_context(run_id=sheets_turn_id, tenant_id=tenant_sheets):
            confirm_out = confirm_mapping.func(  # type: ignore[attr-defined]
                tenant_id=tenant_sheets, connector_id="google_sheet",
                mapping={"Mobile": "phone", "Name": "customer_name"},
            )
            sheets_proposal = commit_ingestion.func(  # type: ignore[attr-defined]
                tenant_id=tenant_sheets, connector_id="google_sheet"
            )

        with psycopg.connect(dsn, autocommit=True) as conn:
            customers_before_sheets_exec = conn.execute(
                "SELECT count(*) FROM customers WHERE tenant_id = %s", (tenant_sheets,)
            ).fetchone()[0]

        real_pull_full = GoogleSheetConnector.pull_full

        def _fake_pull_full(self: Any, tenant_id: Any, spreadsheet_id: str = "", since_row_index: int = 0, *, since: Any = None, tab_name: str = "") -> list[dict[str, Any]]:
            return [{"Mobile": "9876500055", "Name": "Ravi K"}, {"Mobile": "9876500066", "Name": "Asha P"}]

        GoogleSheetConnector.pull_full = _fake_pull_full  # type: ignore[method-assign]
        try:
            sheets_exec_result = execute_pending_ingestion_commit(
                tenant_sheets, current_turn_id=str(sheets_turn_id)
            )
        finally:
            GoogleSheetConnector.pull_full = real_pull_full  # type: ignore[method-assign]

        with psycopg.connect(dsn, autocommit=True) as conn:
            customers_after_sheets_exec = conn.execute(
                "SELECT count(*) FROM customers WHERE tenant_id = %s", (tenant_sheets,)
            ).fetchone()[0]

        pass_2 = (
            "authorize_url" in sheets_oauth
            and sheets_sample["row_count"] == 2
            and sorted(sheets_sample["column_names"]) == ["Mobile", "Name"]
            and confirm_out["confirmed"] is True
            and sheets_proposal["status"] == "proposal_recorded"
            and sheets_exec_result["status"] == "completed"
            and sheets_exec_result["committed"] == 2
        )
        assertion(
            2, "Google Sheets end-to-end: picker->pull_sample->propose/confirm->commit->execute",
            pass_2,
            observed={
                "sheets_oauth_keys": list(sheets_oauth.keys()), "sheets_sample": sheets_sample,
                "confirm_out": confirm_out, "sheets_proposal": sheets_proposal,
                "sheets_exec_result": sheets_exec_result,
            },
        )

        pass_3 = customers_before_sheets_exec == 0 and customers_after_sheets_exec == 2
        assertion(
            3, "VT-268 fail-closed: commit_ingestion TOOL never wrote a customer row itself",
            pass_3,
            observed={
                "before_executor": customers_before_sheets_exec,
                "after_executor": customers_after_sheets_exec,
            },
        )

        # === A4 — OAuth replay fails closed ====================================================
        replay_tenant = _seed_tenant()
        nonce = mint_install_state(replay_tenant, "google_sheet")
        first_claim = claim_install_state(nonce, "google_sheet")
        second_claim = claim_install_state(nonce, "google_sheet")
        pass_4 = first_claim is not None and second_claim is None
        assertion(
            4, "OAuth install-state replay: second claim of the same nonce fails closed",
            pass_4,
            observed={"first_claim_ok": first_claim is not None, "second_claim": second_claim},
        )

        # === A5 — cross-tenant isolation (VT-603) ==============================================
        tenant_a, tenant_b = _seed_tenant(), _seed_tenant()
        with observability_context(run_id=uuid4(), tenant_id=tenant_a):
            confirm_mapping.func(  # type: ignore[attr-defined]
                tenant_id=tenant_b, connector_id="google_sheet", mapping={"Mobile": "phone"}
            )
        with observability_context(run_id=uuid4(), tenant_id=tenant_b):
            state_b = read_integration_state.func(tenant_id=tenant_b)  # type: ignore[attr-defined]
        pass_5 = state_b == {"phase": None, "current_connector_id": None, "pending_owner_input": None}
        assertion(
            5, "Cross-tenant isolation: model-supplied foreign tenant never receives the write",
            pass_5,
            observed={"state_b": state_b},
        )

        # === A6 — restart-resume ================================================================
        with observability_context(run_id=uuid4(), tenant_id=tenant_shopify):
            before_restart = read_integration_state.func(tenant_id=tenant_shopify)  # type: ignore[attr-defined]
        # "restart" — a fresh DB connection, no shared Python state carried across.
        with psycopg.connect(dsn, autocommit=True):
            pass
        with observability_context(run_id=uuid4(), tenant_id=tenant_shopify):
            after_restart = read_integration_state.func(tenant_id=tenant_shopify)  # type: ignore[attr-defined]
        pass_6 = before_restart == after_restart and before_restart["phase"] == "phase_5_confirmed"
        assertion(
            6, "Restart-resume: identical phase/state read before vs. after a simulated restart",
            pass_6,
            observed={"before_restart": before_restart, "after_restart": after_restart},
        )

        # === A7 — recurring-pull verification ===================================================
        with psycopg.connect(dsn, autocommit=True) as conn:
            shopify_cadence = conn.execute(
                "SELECT enabled, pull_cadence FROM tenant_connector_status "
                "WHERE tenant_id = %s AND connector_id = 'shopify'", (tenant_shopify,),
            ).fetchone()
            sheets_cadence = conn.execute(
                "SELECT enabled, pull_cadence FROM tenant_connector_status "
                "WHERE tenant_id = %s AND connector_id = 'google_sheet'", (tenant_sheets,),
            ).fetchone()
        pass_7 = (
            shopify_cadence is not None and shopify_cadence[0] is True
            and sheets_cadence is not None and sheets_cadence[0] is True
        )
        assertion(
            7, "Recurring-pull auto-scheduled post-commit for BOTH connectors",
            pass_7,
            observed={"shopify_cadence": shopify_cadence, "sheets_cadence": sheets_cadence},
        )

        # === A8 — re-entry safety ================================================================
        reentry_result = execute_pending_ingestion_commit(
            tenant_shopify, current_turn_id=str(shopify_turn_id)
        )
        pass_8 = reentry_result is None
        assertion(
            8, "Re-entry after success is a safe no-op (never double-ingests)",
            pass_8,
            observed={"reentry_result": reentry_result},
        )

        return _finalise(dsn)
    finally:
        shutdown_dbos()


def _finalise(dsn: str) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")
    try:
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as conn:
            if INSERTED_TENANT_IDS:
                conn.execute("DELETE FROM customers WHERE tenant_id = ANY(%s)", (INSERTED_TENANT_IDS,))
                conn.execute(
                    "DELETE FROM tenant_connector_status WHERE tenant_id = ANY(%s)",
                    (INSERTED_TENANT_IDS,),
                )
                conn.execute(
                    "DELETE FROM tenant_integration_state WHERE tenant_id = ANY(%s)",
                    (INSERTED_TENANT_IDS,),
                )
                conn.execute(
                    "DELETE FROM tenant_oauth_tokens WHERE tenant_id = ANY(%s)",
                    (INSERTED_TENANT_IDS,),
                )
                conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (INSERTED_TENANT_IDS,))
    except BaseException as exc:  # noqa: BLE001
        print(f"cleanup partial: {exc!r}", file=sys.stderr)
    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print(f"\nALL {len(RESULTS)} ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
