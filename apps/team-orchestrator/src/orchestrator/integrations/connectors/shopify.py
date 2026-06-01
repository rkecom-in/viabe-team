"""VT-208 — Shopify connector.

Shopify Admin REST API 2024-04. CLIENT-CREDENTIALS grant (VT-208 rework
2026-06-01): Shopify removed in-UI Admin API access-token paste, so auth is now
the OAuth2 client_credentials grant — app Client ID + Client Secret → POST the
store token endpoint → short-lived access_token. ZERO manual paste (CL-421).
Works because the app + dev store are in the SAME ORG (dev: the eComVibe Dev
Dashboard app + the kk4xva-di dev store; CL-422 synthetic only).

Grant (confirmed vs shopify.dev get-api-access-tokens, Cowork 2026-06-01):
    POST https://{SHOPIFY_STORE_DOMAIN}/admin/oauth/access_token
    Content-Type: application/x-www-form-urlencoded
    body: grant_type=client_credentials, client_id=SHOPIFY_API_KEY,
          client_secret=SHOPIFY_API_SECRET
    → { access_token, scope, expires_in }   (expires_in is 86399 = ~24h)
The access_token is X-Shopify-Access-Token for the Admin API. SHOPIFY_API_KEY /
SHOPIFY_API_SECRET / SHOPIFY_STORE_DOMAIN come from .viabe/secrets/shopify-dev.env.

Q1: Reuse ``tenant_oauth_tokens`` for credential storage. The
``refresh_token_encrypted`` column holds the Admin API access_token; expires_at /
last_refreshed_at track the 24h TTL → proactive re-grant within a 5-min skew.

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
from collections.abc import Callable
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
# Scopes the app is granted in the Dev Dashboard (Cowork VT-208). read_orders
# covers abandoned checkouts; if the live walk 403s the /checkouts.json pull,
# read_checkouts must be added in the Dashboard (flag for the live canary).
_REQUIRED_SCOPES = {"read_customers", "read_orders", "read_products"}
_TOKEN_PATH = "/admin/oauth/access_token"
_EXPIRY_SKEW = timedelta(minutes=5)  # proactive re-grant before the 24h TTL lapses

# (store_domain, client_id, client_secret) -> Shopify grant JSON
# ({access_token, scope, expires_in}). Injectable so tests run without the network.
GrantFn = Callable[[str, str, str], dict[str, Any]]


class AuthValidationError(Exception):
    """Raised when the client_credentials grant is rejected by Shopify."""


class ShopifyConfigError(Exception):
    """Raised when the SHOPIFY_API_KEY / _SECRET / _STORE_DOMAIN env is absent."""


def _shopify_env() -> tuple[str, str, str]:
    """(client_id, client_secret, store_domain) from .viabe/secrets/shopify-dev.env."""
    cid = os.environ.get("SHOPIFY_API_KEY")
    secret = os.environ.get("SHOPIFY_API_SECRET")
    domain = os.environ.get("SHOPIFY_STORE_DOMAIN")
    if not (cid and secret and domain):
        raise ShopifyConfigError(
            "SHOPIFY_API_KEY / SHOPIFY_API_SECRET / SHOPIFY_STORE_DOMAIN must be set "
            "(.viabe/secrets/shopify-dev.env)"
        )
    return cid, secret, domain


def _default_grant(store_domain: str, client_id: str, client_secret: str) -> dict[str, Any]:
    """Real Shopify client_credentials grant (form-encoded POST)."""
    resp = httpx.post(
        f"https://{store_domain}{_TOKEN_PATH}",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15.0,
    )
    if resp.status_code != 200:
        # shop_not_permitted (app+store not same org) is Shopify's #1 failure —
        # surface it verbatim, never mask (Cowork live-walk flag).
        raise AuthValidationError(
            f"Shopify client_credentials grant failed: HTTP {resp.status_code} "
            f"body={resp.text[:300]}"
        )
    return cast("dict[str, Any]", resp.json())


class ShopifyConnector(ConnectorBase):
    """Shopify Admin API connector."""

    connector_id: str = "shopify"

    def __init__(self, *, grant_fn: GrantFn | None = None) -> None:
        # grant_fn injectable for tests (default = the real client_credentials POST).
        self._grant_fn: GrantFn = grant_fn or _default_grant

    @property
    def spec(self) -> ConnectorSpec:
        return get_connector("shopify")

    # ---------- AUTH (client_credentials grant — zero paste, CL-421) ----------

    def start_auth(self, tenant_id: UUID) -> dict[str, Any]:
        """Zero-paste: the grant is server-side (app creds in env, app+store same
        org). Nothing for the owner to copy — just a confirm."""
        return {
            "next_action": "client_credentials_connect",
            "prompt_kind": "none",
            "message": (
                "Connecting your Shopify store automatically — no token to copy."
            ),
            "scopes": sorted(_REQUIRED_SCOPES),
        }

    def _grant_and_store(self, tenant_id: UUID) -> dict[str, Any]:
        """Run the client_credentials grant + persist the token (encrypted, 24h TTL)."""
        client_id, client_secret, store_domain = _shopify_env()
        grant = self._grant_fn(store_domain, client_id, client_secret)
        access_token = grant.get("access_token")
        if not access_token:
            raise AuthValidationError(
                f"grant returned no access_token: {str(grant)[:200]!r}"
            )
        expires_in = int(grant.get("expires_in") or 86399)
        scope_str = grant.get("scope") or ""
        scopes = (
            [s.strip() for s in scope_str.split(",") if s.strip()]
            if scope_str else sorted(_REQUIRED_SCOPES)
        )
        encrypted = encrypt_value(str(access_token))
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        push_secret = secrets.token_urlsafe(32)
        pool = get_pool()
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO tenant_oauth_tokens (
                    tenant_id, connector_id, refresh_token_encrypted,
                    scopes, push_secret, shop_url, last_refreshed_at, expires_at
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
                    scopes, push_secret, store_domain, expires_at,
                ),
            )
        return {"success": True, "shop_url": store_domain, "scopes": scopes}

    def complete_auth(
        self, tenant_id: UUID, auth_payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Perform the client_credentials grant + persist. ``auth_payload`` is
        unused (zero-paste) — kept for the ConnectorBase contract."""
        return self._grant_and_store(tenant_id)

    def _read_token_row(self, tenant_id: UUID) -> dict[str, Any] | None:
        pool = get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT refresh_token_encrypted, shop_url, expires_at "
                "FROM tenant_oauth_tokens WHERE tenant_id = %s AND connector_id = %s",
                (str(tenant_id), self.connector_id),
            )
            raw = cur.fetchone()
        return cast("dict[str, Any] | None", raw)

    def get_access_token(self, tenant_id: UUID) -> tuple[str, str]:
        """Return ``(access_token, shop_url)``; grant on first use, proactively
        re-grant within a 5-min skew of the 24h expiry."""
        row = self._read_token_row(tenant_id)
        expires_at = row["expires_at"] if row else None
        if (
            row is None
            or expires_at is None
            or expires_at <= datetime.now(UTC) + _EXPIRY_SKEW
        ):
            self._grant_and_store(tenant_id)  # first connect OR proactive refresh
            row = self._read_token_row(tenant_id)
            if row is None:
                raise RuntimeError(
                    f"Shopify grant did not persist a token for {tenant_id}"
                )
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
