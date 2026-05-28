#!/usr/bin/env python3
"""VT-208 Shopify connector canary (Rule #15, DR-15).

Deterministic — no real Shopify store. httpx is monkeypatched to
return fixture responses; FastAPI TestClient drives the webhook
endpoint with synthetic HMAC-signed bodies.

Subshell-source supabase-dev.env:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      ./.venv/bin/python canaries/vt208_shopify_connector.py
    )

Wall-clock budget ≤ 20s.

5 assertions per brief:

- A1: token validation contract — 200 + valid payload → complete_auth
  succeeds + persists encrypted token + shop_url; 401 → AuthValidationError.
- A2: sample pull shape — stubbed customers + checkouts JSON →
  pull_sample returns merged list tagged ``acquired_via='shopify'``.
- A3: webhook HMAC verify — valid base64 signature accepted; tampered
  body rejected.
- A4: webhook routing — checkouts/create → drop_off path;
  orders/paid → attribution branch (logged).
- A5: dedupe vs Sheet customer — pre-existing Sheet phone_hash → same
  Shopify push lands as a merged dedupe decision (not a new row).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from base64 import b64encode
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANTS: list[str] = []


def assertion(
    num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None
) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _preflight() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    from cryptography.fernet import Fernet

    os.environ.setdefault(
        "TEAM_PHONE_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt208-canary-salt")
    print("PREFLIGHT OK")


def _insert_tenant(pool: Any) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"vt208-canary-{tid[:8]}"),
        )
    INSERTED_TENANTS.append(tid)
    return tid


def _cleanup(pool: Any) -> None:
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM phone_token_resolutions "
            "WHERE tenant_id IN (SELECT id FROM tenants WHERE business_name LIKE 'vt208-canary-%')"
        )
        conn.execute(
            "DELETE FROM tenant_oauth_tokens "
            "WHERE tenant_id IN (SELECT id FROM tenants WHERE business_name LIKE 'vt208-canary-%')"
        )
        conn.execute(
            "DELETE FROM tenants WHERE business_name LIKE 'vt208-canary-%'"
        )


class _StubResponse:
    def __init__(self, status_code: int, json_body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._json = json_body
        self.text = json.dumps(json_body)[:500]

    def json(self) -> dict[str, Any]:
        return self._json


def run_canary() -> int:
    _preflight()

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    pool = get_pool()
    _cleanup(pool)  # purge any prior canary leftovers

    import orchestrator.integrations.connectors.shopify as shopify_mod
    from orchestrator.integrations.connectors.shopify import (
        AuthValidationError,
        ShopifyConnector,
    )

    # Patch httpx.get / httpx.post on the shopify module
    original_get = shopify_mod.httpx.get
    original_post = shopify_mod.httpx.post

    stub_state: dict[str, Any] = {"shop_status": 200}

    def fake_get(url: str, headers: dict[str, str] | None = None,
                 params: dict[str, str] | None = None, timeout: float = 30.0) -> _StubResponse:
        if "/shop.json" in url:
            if stub_state["shop_status"] != 200:
                return _StubResponse(stub_state["shop_status"], {"errors": "x"})
            return _StubResponse(200, {"shop": {"id": 1, "name": "fixture-shop"}})
        if "/customers.json" in url:
            return _StubResponse(200, {"customers": [
                {"id": 1, "phone": "+919876543210", "first_name": "Alice"},
                {"id": 2, "phone": "+918888888888", "first_name": "Bob"},
            ]})
        if "/checkouts.json" in url:
            return _StubResponse(200, {"checkouts": [
                {"id": 11, "phone": "+917777777777", "total_price": "500"},
            ]})
        return _StubResponse(404, {})

    def fake_post(url: str, headers: dict[str, str] | None = None,
                  json: dict[str, Any] | None = None, timeout: float = 15.0) -> _StubResponse:
        if "/webhooks.json" in url:
            return _StubResponse(201, {"webhook": {"id": 99}})
        return _StubResponse(404, {})

    shopify_mod.httpx.get = fake_get  # type: ignore[assignment]
    shopify_mod.httpx.post = fake_post  # type: ignore[assignment]

    try:
        connector = ShopifyConnector()

        # ---------------- A1 — token validation ----------------
        t1 = _insert_tenant(pool)
        result_ok = connector.complete_auth(
            UUID(t1),
            {"access_token": "shpat_test_token", "shop_url": "test.myshopify.com"},
        )
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT shop_url, scopes FROM tenant_oauth_tokens "
                "WHERE tenant_id = %s AND connector_id = 'shopify'",
                (t1,),
            )
            stored = cur.fetchone()
        # Now test 401
        stub_state["shop_status"] = 401
        raised_401 = False
        try:
            connector.complete_auth(
                UUID(t1),
                {"access_token": "bad", "shop_url": "test.myshopify.com"},
            )
        except AuthValidationError:
            raised_401 = True
        stub_state["shop_status"] = 200

        pass_1 = (
            result_ok.get("success") is True
            and stored is not None
            and stored["shop_url"] == "test.myshopify.com"
            and "read_customers" in stored["scopes"]
            and raised_401
        )
        assertion(
            1, "token validation: 200 → success + persisted shop_url; 401 → AuthValidationError",
            pass_1, observed={"ok_result": result_ok, "stored": dict(stored) if stored else None,
                              "raised_401": raised_401},
            expected={"success": True, "raised_401": True},
        )

        # ---------------- A2 — sample pull shape ----------------
        sample = connector.pull_sample(UUID(t1))
        customer_count = sum(1 for r in sample if r["__source"] == "customers")
        checkout_count = sum(1 for r in sample if r["__source"] == "abandoned_checkouts")
        all_tagged = all(r["acquired_via"] == "shopify" for r in sample)
        pass_2 = customer_count == 2 and checkout_count == 1 and all_tagged
        assertion(
            2, "pull_sample: merged customers+checkouts tagged acquired_via='shopify'",
            pass_2, observed={"customer_count": customer_count, "checkout_count": checkout_count,
                              "all_tagged": all_tagged},
            expected={"customer_count": 2, "checkout_count": 1, "all_tagged": True},
        )

        # ---------------- A3 — webhook HMAC verify ----------------
        body = json.dumps({"phone": "+919876543210", "total_price": "100"}).encode("utf-8")
        secret = "shopify-canary-secret"
        valid_sig = b64encode(
            hmac.new(secret.encode(), body, hashlib.sha256).digest()
        ).decode()
        accept = ShopifyConnector.verify_push_signature(
            body, {"X-Shopify-Hmac-Sha256": valid_sig}, secret
        )
        # tamper body
        tampered = body + b"X"
        reject = ShopifyConnector.verify_push_signature(
            tampered, {"X-Shopify-Hmac-Sha256": valid_sig}, secret
        )
        pass_3 = accept is True and reject is False
        assertion(
            3, "webhook HMAC: valid signature accepted; tampered body rejected",
            pass_3, observed={"accept": accept, "reject": reject},
            expected={"accept": True, "reject": False},
        )

        # ---------------- A4 — webhook routing ----------------
        # Seed t1's push_secret via direct UPDATE (complete_auth set it but we
        # need a known value for HMAC fixture).
        with pool.connection() as conn:
            conn.execute(
                "UPDATE tenant_oauth_tokens SET push_secret = %s "
                "WHERE tenant_id = %s AND connector_id = 'shopify'",
                (secret, t1),
            )

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from orchestrator.api import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        # checkout body
        checkout_body = json.dumps({
            "id": 200, "phone": "+919000000001", "customer": {"first_name": "C"},
        }).encode("utf-8")
        checkout_sig = b64encode(
            hmac.new(secret.encode(), checkout_body, hashlib.sha256).digest()
        ).decode()
        resp_checkout = client.post(
            "/api/orchestrator/integrations/shopify/webhook",
            content=checkout_body,
            headers={
                "X-Viabe-Tenant": t1,
                "X-Shopify-Topic": "checkouts/create",
                "X-Shopify-Hmac-Sha256": checkout_sig,
                "Content-Type": "application/json",
            },
        )

        # orders/paid body
        order_body = json.dumps({
            "id": 300, "phone": "+919000000002", "total_price": "1500",
            "created_at": datetime.now(UTC).isoformat(),
        }).encode("utf-8")
        order_sig = b64encode(
            hmac.new(secret.encode(), order_body, hashlib.sha256).digest()
        ).decode()
        resp_order = client.post(
            "/api/orchestrator/integrations/shopify/webhook",
            content=order_body,
            headers={
                "X-Viabe-Tenant": t1,
                "X-Shopify-Topic": "orders/paid",
                "X-Shopify-Hmac-Sha256": order_sig,
                "Content-Type": "application/json",
            },
        )
        pass_4 = (
            resp_checkout.status_code == 200
            and resp_checkout.json().get("rows_persisted") == 1
            and resp_order.status_code == 200
            and resp_order.json().get("attribution_hits") == 1
        )
        assertion(
            4, "webhook routing: checkouts → drop_off; orders/paid → attribution",
            pass_4, observed={
                "checkout": {"http": resp_checkout.status_code, "body": resp_checkout.json()},
                "order": {"http": resp_order.status_code, "body": resp_order.json()},
            },
            expected={"checkout_persisted": 1, "order_attribution": 1},
        )

        # ---------------- A5 — dedupe vs Sheet ----------------
        # Land a Sheet-tagged row for the same phone the next Shopify
        # push will deliver, then deliver the Shopify push and confirm
        # only ONE phone_token_resolutions row exists for that tenant+phone.
        from orchestrator.integrations.dedupe import dedupe_customer_row

        same_phone = "+919555555555"
        dedupe_customer_row(
            tenant_id=UUID(t1),
            phone_e164=same_phone,
            connector_id="google_sheet",
            canonical_row={"phone": same_phone, "name": "Pre-existing"},
        )
        shopify_same_body = json.dumps({
            "id": 400, "phone": same_phone, "customer": {"first_name": "Same"},
        }).encode("utf-8")
        shopify_same_sig = b64encode(
            hmac.new(secret.encode(), shopify_same_body, hashlib.sha256).digest()
        ).decode()
        client.post(
            "/api/orchestrator/integrations/shopify/webhook",
            content=shopify_same_body,
            headers={
                "X-Viabe-Tenant": t1,
                "X-Shopify-Topic": "checkouts/create",
                "X-Shopify-Hmac-Sha256": shopify_same_sig,
                "Content-Type": "application/json",
            },
        )
        from orchestrator.observability.phone_tokens import _hash_phone

        same_token = _hash_phone(same_phone)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM phone_token_resolutions "
                "WHERE tenant_id = %s AND phone_token = %s",
                (t1, same_token),
            )
            count_raw = cur.fetchone()
        count = count_raw["n"] if count_raw else 0
        pass_5 = count == 1
        assertion(
            5, "dedupe: same phone via Sheet then Shopify → 1 row (cross-connector merge)",
            pass_5, observed={"count_for_same_phone_token": count, "phone_token": same_token},
            expected={"count": 1},
        )

    finally:
        shopify_mod.httpx.get = original_get  # type: ignore[assignment]
        shopify_mod.httpx.post = original_post  # type: ignore[assignment]
        _cleanup(pool)

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)} assertion(s) failed", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
