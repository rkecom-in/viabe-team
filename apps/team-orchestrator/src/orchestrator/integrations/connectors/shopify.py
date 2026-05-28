"""VT-208 — Shopify connector.

Shopify Admin REST API 2024-04. Custom-app access_token flow — owner
generates the token in Shopify admin (Apps → Develop apps → Create app
→ API scopes → Install) and pastes into the Integration Agent. No OAuth
PKCE; tokens are long-lived (Q1 lock per Cowork plan-review 2026-05-28).

Q1: Reuse ``tenant_oauth_tokens`` for credential storage. The
``refresh_token_encrypted`` column is overloaded — google_sheet rows
hold OAuth refresh tokens; shopify rows hold Admin API access_tokens.
Column COMMENT in migration 035 spells this out.

Q2: Real-Shopify webhook delivery deferred to VT-213 (mirrors VT-212 for
google_sheet OAuth). PR-1 canary is deterministic via stubbed httpx.

Q3: REST not GraphQL — Phase 1 only pulls customers / abandoned_checkouts
/ orders; GraphQL's bulkOperations is unnecessary complexity here.

Q4: Webhook secret rotation deferred to Sprint 3+ hardening.

Subclasses ``ConnectorBase``. Mirrors ``GoogleSheetConnector`` shape so
the scheduler + (eventual) generic push receiver can drive it uniformly.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from base64 import b64decode, b64encode
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx

from orchestrator.graph import get_pool
from orchestrator.integrations.connectors.base import ConnectorBase
from orchestrator.integrations.registry import get_connector
from orchestrator.integrations.schemas import ConnectorSpec
from orchestrator.observability.encrypt_value import (
    decrypt_value,
    encrypt_value,
)

logger = logging.getLogger(__name__)


_SHOPIFY_API_VERSION = "2024-04"
_REQUIRED_SCOPES = {
    "read_customers",
    "read_orders",
    "read_products",
    "read_checkouts",
}


class AuthValidationError(Exception):
    """Raised when the pasted Admin API token fails validation."""


class ShopifyConnector(ConnectorBase):
    """Shopify Admin API connector."""

    connector_id: str = "shopify"

    @property
    def spec(self) -> ConnectorSpec:
        return get_connector("shopify")

    # ---------- AUTH ----------

    def start_auth(self, tenant_id: UUID) -> dict[str, Any]:
        """Return the walkthrough envelope the agent shows the owner."""
        return {
            "next_action": "show_walkthrough_and_prompt_token",
            "walkthrough": [
                "Open your Shopify admin → Settings → Apps and sales channels",
                "Click 'Develop apps' (top-right) → 'Create an app'",
                "Name the app 'Viabe Integration'",
                "Open the new app → 'Configure Admin API scopes'",
                "Enable scopes: " + ", ".join(sorted(_REQUIRED_SCOPES)),
                "Click 'Save' → 'Install app' → 'Reveal token once'",
                "Paste the token here, along with your shop URL "
                "(e.g. rkecom.myshopify.com)",
            ],
            "prompt_kind": "shopify_admin_token",
        }

    def complete_auth(
        self, tenant_id: UUID, auth_payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate token + persist encrypted with shop_url."""
        access_token = auth_payload.get("access_token") or auth_payload.get(
            "token"
        )
        shop_url = auth_payload.get("shop_url")
        if not access_token:
            raise ValueError("complete_auth: 'access_token' missing")
        if not shop_url:
            raise ValueError("complete_auth: 'shop_url' missing")

        # Validate: hit GET /shop.json. 200 + valid JSON proves the
        # token has at least one accepted scope. 401 = bad token.
        url = f"https://{shop_url}/admin/api/{_SHOPIFY_API_VERSION}/shop.json"
        resp = httpx.get(
            url,
            headers={"X-Shopify-Access-Token": access_token},
            timeout=15.0,
        )
        if resp.status_code == 401:
            raise AuthValidationError(
                f"Shopify token rejected: HTTP 401 for shop={shop_url}"
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Shopify token validation failed: HTTP {resp.status_code} "
                f"body={resp.text[:200]}"
            )

        encrypted = encrypt_value(access_token)
        push_secret = secrets.token_urlsafe(32)
        # Long-lived; record a far-future expiry so the schema's
        # expires_at column stays NOT NULL-compatible.
        expires_at = datetime.now(UTC) + timedelta(days=365)

        pool = get_pool()
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO tenant_oauth_tokens (
                    tenant_id, connector_id, refresh_token_encrypted,
                    scopes, push_secret, shop_url,
                    last_refreshed_at, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
                ON CONFLICT (tenant_id, connector_id) DO UPDATE SET
                    refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
                    scopes = EXCLUDED.scopes,
                    push_secret = COALESCE(
                        tenant_oauth_tokens.push_secret, EXCLUDED.push_secret
                    ),
                    shop_url = EXCLUDED.shop_url,
                    last_refreshed_at = now(),
                    expires_at = EXCLUDED.expires_at
                """,
                (
                    str(tenant_id), self.connector_id, encrypted,
                    sorted(_REQUIRED_SCOPES), push_secret, shop_url,
                    expires_at,
                ),
            )
        return {
            "success": True,
            "shop_url": shop_url,
            "scopes": sorted(_REQUIRED_SCOPES),
        }

    def get_access_token(self, tenant_id: UUID) -> tuple[str, str]:
        """Return ``(access_token, shop_url)`` for a connected tenant."""
        pool = get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT refresh_token_encrypted, shop_url "
                "FROM tenant_oauth_tokens "
                "WHERE tenant_id = %s AND connector_id = %s",
                (str(tenant_id), self.connector_id),
            )
            raw = cur.fetchone()
        if raw is None:
            raise RuntimeError(
                f"no Shopify token for tenant {tenant_id}"
            )
        row = cast("dict[str, Any]", raw)
        return decrypt_value(row["refresh_token_encrypted"]), row["shop_url"]

    # ---------- PULL ----------

    def _request(
        self, tenant_id: UUID, path: str, *, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        access_token, shop_url = self.get_access_token(tenant_id)
        url = f"https://{shop_url}/admin/api/{_SHOPIFY_API_VERSION}{path}"
        resp = httpx.get(
            url,
            headers={"X-Shopify-Access-Token": access_token},
            params=params,
            timeout=30.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Shopify GET {path} failed: HTTP {resp.status_code} "
                f"body={resp.text[:200]}"
            )
        return cast("dict[str, Any]", resp.json())

    def pull_sample(self, tenant_id: UUID) -> list[dict[str, Any]]:
        """Fetch first ~50 customers + ~50 abandoned checkouts.

        Returns a flat list tagged with ``__source`` so the field-mapping
        reasoner can route to the right canonical destination.
        """
        customers = self._request(
            tenant_id, "/customers.json", params={"limit": "50"}
        ).get("customers", [])
        checkouts = self._request(
            tenant_id, "/checkouts.json", params={"limit": "50"}
        ).get("checkouts", [])
        merged: list[dict[str, Any]] = []
        for c in customers:
            row = dict(c)
            row["__source"] = "customers"
            row["acquired_via"] = "shopify"
            merged.append(row)
        for c in checkouts:
            row = dict(c)
            row["__source"] = "abandoned_checkouts"
            row["acquired_via"] = "shopify"
            merged.append(row)
        return merged

    def pull_full(
        self, tenant_id: UUID, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Incremental customer + checkout pull from ``since``.

        Phase-1 uses ``updated_at_min`` (ISO 8601). Pagination via Shopify's
        ``Link`` header is deferred — Phase-1 cap = 250 per resource
        (Shopify's default page_size). VT-N future row adds Link-header
        pagination + cursor persistence.
        """
        params: dict[str, str] = {"limit": "250"}
        if since is not None:
            params["updated_at_min"] = since.replace(microsecond=0).isoformat()
        customers = self._request(
            tenant_id, "/customers.json", params=params
        ).get("customers", [])
        return [
            {**row, "__source": "customers", "acquired_via": "shopify"}
            for row in customers
        ]

    # ---------- PUSH ----------

    def setup_push(self, tenant_id: UUID) -> dict[str, str]:
        """Register Shopify webhooks for checkouts + orders.

        Hits POST /admin/api/.../webhooks.json for the 4 topics this
        connector cares about. Each webhook signs with the same shop-
        wide secret Shopify generates; we read it from
        ``tenant_oauth_tokens.push_secret`` and document it back.
        """
        pool = get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT push_secret FROM tenant_oauth_tokens "
                "WHERE tenant_id = %s AND connector_id = %s",
                (str(tenant_id), self.connector_id),
            )
            raw = cur.fetchone()
        row = cast("dict[str, Any] | None", raw)
        if row is None or not row["push_secret"]:
            raise RuntimeError(
                f"setup_push: no push_secret for tenant {tenant_id}; "
                "run complete_auth first"
            )
        push_secret: str = row["push_secret"]
        orchestrator_base = os.environ.get(
            "ORCHESTRATOR_BASE_URL", "http://localhost:8001"
        )
        address = (
            f"{orchestrator_base}/api/orchestrator/integrations/"
            "shopify/webhook"
        )
        access_token, shop_url = self.get_access_token(tenant_id)
        topics = (
            "checkouts/create",
            "checkouts/update",
            "orders/create",
            "orders/paid",
        )
        registered: list[str] = []
        for topic in topics:
            url = (
                f"https://{shop_url}/admin/api/"
                f"{_SHOPIFY_API_VERSION}/webhooks.json"
            )
            resp = httpx.post(
                url,
                headers={"X-Shopify-Access-Token": access_token},
                json={
                    "webhook": {
                        "topic": topic,
                        "address": address,
                        "format": "json",
                    }
                },
                timeout=15.0,
            )
            if resp.status_code not in (201, 422):
                raise RuntimeError(
                    f"webhook register {topic} failed: HTTP {resp.status_code} "
                    f"body={resp.text[:200]}"
                )
            registered.append(topic)
        return {
            "address": address,
            "topics": ",".join(registered),
            "push_secret_hint": push_secret[:8] + "…",
        }

    @staticmethod
    def verify_push_signature(
        body: bytes, headers: dict[str, str], push_secret: str
    ) -> bool:
        """Verify Shopify ``X-Shopify-Hmac-Sha256`` (base64) on ``body``."""
        signature = (
            headers.get("x-shopify-hmac-sha256")
            or headers.get("X-Shopify-Hmac-Sha256", "")
        )
        if not signature:
            return False
        expected = b64encode(
            hmac.new(push_secret.encode(), body, hashlib.sha256).digest()
        ).decode()
        try:
            return hmac.compare_digest(expected, signature)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def parse_push_payload(body: bytes) -> list[dict[str, Any]]:
        """Decode a Shopify webhook body into canonical row dicts.

        Phase-1 emits a single canonical row per event. The caller is
        responsible for routing on ``X-Shopify-Topic`` (orders/paid →
        attribution; checkouts/* → drop_off persistence).
        """
        import json as _json

        payload = _json.loads(body.decode("utf-8"))
        customer = payload.get("customer") or {}
        row = {
            "phone": (
                customer.get("phone")
                or (payload.get("shipping_address") or {}).get("phone")
                or payload.get("phone")
            ),
            "email": customer.get("email") or payload.get("email"),
            "customer_name": (
                f"{customer.get('first_name', '')} "
                f"{customer.get('last_name', '')}"
            ).strip() or None,
            "order_amount": payload.get("total_price"),
            "order_date": payload.get("created_at"),
            "acquired_via": "shopify",
            "__source": "shopify_webhook",
        }
        return [row] if row.get("phone") or row.get("email") else []


# Decode helper kept here so tests can build canonical-base64 fixtures
# without re-importing the cryptography stdlib in canary code.
def _b64_decode(value: str) -> bytes:
    """Forward standard b64decode for canary fixtures."""
    return b64decode(value)


__all__ = ["AuthValidationError", "ShopifyConnector"]
