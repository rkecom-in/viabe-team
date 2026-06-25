"""VT-208 — Shopify connector.

Shopify Admin REST API 2026-04 (VT-422: bumped from 2024-04 — outside Shopify's ~1yr supported window by 2026-06).

CANONICAL AUTH = OAuth authorization-code install (VT-283 / VT-422)
------------------------------------------------------------------
VT-422 promoted Shopify to the PUBLIC OAuth app. The canonical, production auth
path is the standard Shopify OAuth authorization-code install (``build_oauth_install_url``
→ owner consent → callback → ``_oauth_exchange_and_store`` → OFFLINE token). This is
the only path that works for a REAL merchant on a DIFFERENT org, and it is what the
registry now declares (``auth_flow="oauth2"``). ZERO manual paste (CL-421).

client_credentials = DEV / own-store TEST FALLBACK ONLY
-------------------------------------------------------
The OAuth2 client_credentials grant (``_grant_and_store``, ``_shopify_env``,
``SHOPIFY_STORE_DOMAIN``, ``shopify-dev.env``) is RETAINED as the dev/own-store
test fallback — it is same-org-only (app + store in ONE org; dev: the eComVibe Dev
Dashboard app + the kk4xva-di dev store; CL-422 synthetic only) and is the only way
to exercise pulls against our own dev store without a different-org merchant. It is
NOT the canonical path and is never used for a real merchant install. Do NOT delete
it (VT-422 GAP-4).

client_credentials grant mechanics (test fallback):

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

VT-283 — OWNER-FACING OAuth managed-install (the production zero-paste path)
----------------------------------------------------------------------------
client_credentials (above) only works when the app + store share an org, so it
is the DEV/own-store path only (CL-427). A real merchant on a DIFFERENT org must
install via the standard Shopify OAuth authorization-code flow: the owner types
their ``*.myshopify.com`` domain once, approves on Shopify's consent screen, and
Shopify redirects to our callback with ``code`` — zero paste (CL-421).

The connector is DUAL-MODE — ``complete_auth`` branches on the payload:
  * ``{code, shop}`` present  -> OAuth authorization-code (merchant install).
  * empty / None              -> client_credentials (dev/own-store, existing).

OAuth-install yields an OFFLINE access token (no expiry), stored with
``expires_at = NULL`` — which is also the discriminator ``get_access_token``
uses to skip the client_credentials re-grant for OAuth tenants.

# live OAuth-install walk deferred to E2E (Fazal 2026-06-02): the install path
# cannot be live-walked on our own dev store (same-org = client_credentials); a
# real merchant store on a different org is needed. Fazal ruled the live walk
# happens during end-to-end testing, NOT as a VT-283 gate — VT-283 is Done on
# unit/DB coverage. See .viabe/launch-tracker.md (E2E real-merchant OAuth walk).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from base64 import b64decode, b64encode
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID

import httpx

from orchestrator.integrations.ingest import CanonicalRow, SaleLine

from orchestrator.graph import get_pool
from orchestrator.integrations.connectors.base import ConnectorBase
from orchestrator.integrations.registry import get_connector
from orchestrator.integrations.schemas import ConnectorSpec
from orchestrator.observability.encrypt_value import (
    decrypt_value,
    encrypt_value,
)

logger = logging.getLogger(__name__)


_SHOPIFY_API_VERSION = "2026-04"
# VT-422 GAP-0 — read + WRITE scopes for the PUBLIC OAuth app. This constant is the
# SINGLE SOURCE: the authorize URL, the offline token, and Shopify's consent screen
# all derive from it, so widening it here is all the install needs to request write.
# It is the PIN consumed at the canary (which needs Fazal's Partner app). read_orders
# covers abandoned checkouts; if the live walk 403s /checkouts.json, read_checkouts
# is added in the Dashboard (flag for the live canary).
#   read_orders    — sale-of-record substrate (backfill + orders/create)
#   read_customers — identity anchor (phone/email/name)
#   read_products  — product context for sales-recovery messaging
# Write block — Fazal decision pending (Cowork relaying): write_orders only vs
# write_orders + write_customers. Default to BOTH for the build; if an unused write
# scope spooks Shopify review, drop write_customers to a fast-follow and keep
# write_orders (the e2e backdated-seed unblock).
#   write_orders    — e2e backdated-seed via the app token + future order-action agent
#   write_customers — future write-agent (tag/note/update customer for recovery campaigns)
_REQUIRED_SCOPES = {
    "read_customers",
    "read_orders",
    "read_products",
    "write_orders",
    "write_customers",
}
_TOKEN_PATH = "/admin/oauth/access_token"
_AUTHORIZE_PATH = "/admin/oauth/authorize"
_EXPIRY_SKEW = timedelta(minutes=5)  # proactive re-grant before the 24h TTL lapses

# A merchant shop domain MUST match this before it is interpolated into a
# redirect URL (Cowork VT-283 #1: never trust raw owner input into a redirect).
_SHOP_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")

# (store_domain, client_id, client_secret) -> Shopify grant JSON
# ({access_token, scope, expires_in}). Injectable so tests run without the network.
GrantFn = Callable[[str, str, str], dict[str, Any]]

# (shop, client_id, client_secret, code) -> OAuth token JSON
# ({access_token, scope}). Injectable so tests run without the network.
ExchangeFn = Callable[[str, str, str, str], dict[str, Any]]


class ShopDomainError(Exception):
    """Raised when a merchant shop domain fails the ``*.myshopify.com`` check."""


def _validate_shop_domain(shop: str) -> str:
    """Return ``shop`` lower-cased if it is a well-formed ``*.myshopify.com``
    domain; raise ``ShopDomainError`` otherwise (no scheme, no path, no port)."""
    candidate = (shop or "").strip().lower()
    if not _SHOP_DOMAIN_RE.match(candidate):
        raise ShopDomainError(
            f"shop must be a bare <name>.myshopify.com domain; got {shop!r}"
        )
    return candidate


# Public alias — the OAuth-install router validates the owner-entered shop before
# minting a VT-289 nonce (so an invalid domain never reaches the state store).
validate_shop_domain = _validate_shop_domain


def _shopify_oauth_creds() -> tuple[str, str]:
    """(client_id, client_secret) for the OAuth-install flow — reuses the same
    app credentials as client_credentials. STORE_DOMAIN is NOT needed (the shop
    comes from the owner-entered domain), so this is a narrower check than
    ``_shopify_env``."""
    cid = os.environ.get("SHOPIFY_API_KEY")
    secret = os.environ.get("SHOPIFY_API_SECRET")
    if not (cid and secret):
        raise ShopifyConfigError(
            "SHOPIFY_API_KEY / SHOPIFY_API_SECRET must be set "
            "(.viabe/secrets/shopify-dev.env)"
        )
    return cid, secret


def _shopify_redirect_uri() -> str:
    """The public team-web callback URL Shopify redirects to (Cowork VT-283 #2:
    the Vercel deployment, registered in the Shopify app's allowed redirect
    URLs — dev = the preview URL, prod = the real viabe domain)."""
    uri = os.environ.get("SHOPIFY_OAUTH_REDIRECT_URI")
    if not uri:
        raise ShopifyConfigError(
            "SHOPIFY_OAUTH_REDIRECT_URI must be set for the OAuth-install flow "
            "(the public team-web callback URL; .viabe/secrets/shopify-dev.env)"
        )
    return uri


def _default_oauth_exchange(
    shop: str, client_id: str, client_secret: str, code: str
) -> dict[str, Any]:
    """Real Shopify OAuth authorization-code exchange (form-encoded POST).

    Unlike Google, Shopify's token exchange does NOT take ``redirect_uri``;
    the offline (no-expiry) access token is returned directly.
    """
    resp = httpx.post(
        f"https://{shop}{_TOKEN_PATH}",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        },
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise AuthValidationError(
            f"Shopify OAuth code-exchange failed: HTTP {resp.status_code} "
            f"body={resp.text[:300]}"
        )
    return cast("dict[str, Any]", resp.json())


def verify_oauth_hmac(
    query_params: dict[str, str], client_secret: str
) -> bool:
    """Verify the HMAC Shopify appends to the OAuth redirect query.

    Shopify signs the redirect query (sans ``hmac``/``signature``) with the app
    secret: sort the remaining params lexicographically, join as ``k=v`` pairs
    with ``&``, HMAC-SHA256 with the secret, HEX digest. (This is DISTINCT from
    the webhook HMAC, which is base64 over the raw body — see
    ``ShopifyConnector.verify_push_signature``.)

    Returns False on a missing/mismatched hmac — never raises.
    """
    provided = query_params.get("hmac", "")
    if not provided:
        return False
    message_pairs = sorted(
        f"{k}={v}"
        for k, v in query_params.items()
        if k not in ("hmac", "signature")
    )
    message = "&".join(message_pairs)
    expected = hmac.new(
        client_secret.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    try:
        return hmac.compare_digest(expected, provided)
    except (TypeError, ValueError):
        return False


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


# ---------- VT-417: Shopify order → CanonicalRow mapping ----------
# The single Shopify-specific mapper. PII boundary (§3): persists ONLY
# phone / email / name + the order TOTAL as ONE sale magnitude. Address and
# line-items are NEVER read into the CanonicalRow — they are dropped here.

_SHOPIFY_ACQUIRED_VIA = "shopify"
_INR = "INR"


def _normalize_e164(raw: str | None) -> str | None:
    """Best-effort E.164 for an Indian-first store; ``None`` if un-normalizable.

    Shopify usually stores E.164 already for IN. If we cannot confidently
    normalize, return ``None`` and let email / name anchor the customer (never
    invent a number). Mirrors the methods' ``contacts._normalize_phone`` shape but
    drops the confidence channel (connector data is structured, not OCR).
    """
    if not raw:
        return None
    s = raw.strip()
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if has_plus and digits.startswith("91") and len(digits) == 12:
        return "+" + digits
    if has_plus:
        return "+" + digits  # already international (non-IN) — trust it
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "+91" + digits[1:]
    return None  # ambiguous bare digits — don't guess a country code


def _total_price_to_paise(total_price: Any) -> int | None:
    """Shopify ``total_price`` (major-unit decimal STRING, e.g. "499.00") → paise.

    ``round(Decimal(total_price) * 100)``. Returns ``None`` on a missing /
    unparseable / negative value (the sale is then skipped, not written as 0).
    """
    if total_price is None:
        return None
    try:
        paise = int((Decimal(str(total_price)) * 100).to_integral_value())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return paise if paise >= 0 else None


def _order_date(created_at: Any) -> date | None:
    """Shopify ``created_at`` (ISO 8601) → date-only (the ledger stores DATE)."""
    if not created_at:
        return None
    try:
        return datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        # Fallback: a bare ISO date prefix.
        try:
            return date.fromisoformat(str(created_at).strip()[:10])
        except (ValueError, TypeError):
            return None


@dataclass(frozen=True)
class _OrderMapResult:
    """The mapper outcome — the row plus WHY a sale was/wasn't attached, so the
    caller can count ``skipped_non_inr`` without re-deriving currency."""

    row: CanonicalRow | None         # None when the order has no identity anchor
    skipped_non_inr: bool = False


def shopify_order_to_canonical(payload: dict[str, Any]) -> _OrderMapResult:
    """Map a Shopify ``orders/create`` (or backfill ``orders.json``) order into a
    ``CanonicalRow``. Identity = phone(E.164) / email / name. Sale = the order
    TOTAL → ONE ``SaleLine`` (confidence 1.0 — structured API data is certain).

    Currency guard (§2.3): ``amount_paise`` is INR-minor. A non-INR order keeps
    the customer (identity) but SKIPS the sale (no FX in scope) and is flagged
    ``skipped_non_inr`` so the caller can count it — NEVER silently converted.

    Address and order line-items are NOT read (PII boundary, §3).
    """
    customer = payload.get("customer") or {}
    phone_raw = (
        customer.get("phone")
        or (payload.get("shipping_address") or {}).get("phone")
        or payload.get("phone")
    )
    phone_e164 = _normalize_e164(phone_raw)
    email_raw = customer.get("email") or payload.get("email")
    email = email_raw.strip().lower() if isinstance(email_raw, str) and email_raw.strip() else None
    display_name = (
        f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        or None
    )

    if not (phone_e164 or email or display_name):
        return _OrderMapResult(row=None)

    sales: tuple[SaleLine, ...] = ()
    skipped_non_inr = False
    currency = (payload.get("currency") or "").upper()
    paise = _total_price_to_paise(payload.get("total_price"))
    entry_date = _order_date(payload.get("created_at"))

    if paise is not None and entry_date is not None:
        if currency and currency != _INR:
            # Non-INR: keep identity, skip the sale (no FX). Log the CURRENCY only,
            # never the amount-as-rupees (CL-390).
            skipped_non_inr = True
            logger.info("shopify_order_to_canonical: non-INR order skipped sale currency=%s", currency)
        else:
            sales = (SaleLine(amount_paise=paise, entry_date=entry_date, confidence=1.0),)

    return _OrderMapResult(
        row=CanonicalRow(
            phone_e164=phone_e164,
            email=email,
            display_name=display_name,
            sales=sales,
            consent=None,  # option A (§2.4): Shopify writes NO consent
        ),
        skipped_non_inr=skipped_non_inr,
    )


# ---------- VT-425 Phase A: Shopify SAMPLE row → CanonicalRow (fixed-schema auto-map) ----------
# The `pull_sample` shape is /customers.json + /checkouts.json objects (NOT orders) —
# a different schema than `shopify_order_to_canonical` (which maps /orders.json). Phase A
# onboarding auto-maps Shopify's KNOWN, FIXED customer/checkout schema → CanonicalRow with
# NO owner mapping form and NO field-mapping reasoner (CL-443 fixed-schema auto-map). Because
# the schema is fixed, NO column NAME or cell VALUE is ever sent to an LLM (CL-104 satisfied by
# construction — Phase A's map path has no LLM call at all).
#
# PII boundary (§3, identical to the order mapper): persist ONLY phone(E.164) / email / name
# + (for abandoned checkouts) the checkout TOTAL as ONE sale magnitude. Address / line-items
# are NEVER read into the CanonicalRow.


def shopify_sample_row_to_canonical(payload: dict[str, Any]) -> CanonicalRow | None:
    """Map ONE Shopify ``pull_sample`` row (a /customers.json customer OR a
    /checkouts.json abandoned checkout, tagged ``__source``) → ``CanonicalRow``.

    Fixed-schema (no owner mapping, no reasoner):
      * ``__source == 'customers'`` — identity only (phone / email / first+last name).
        A bare contact carries no sale (empty ``sales``).
      * ``__source == 'abandoned_checkouts'`` — identity + an INR ``total_price`` →
        ONE ``SaleLine`` (an abandoned-checkout value is a real demand signal; the
        Sales-Recovery substrate wants it). Non-INR keeps identity, skips the sale
        (no FX — mirrors the order mapper's currency guard).

    Returns ``None`` when no identity anchor (phone / email / name) is present.
    PII boundary: address / line-items are dropped here, never read into CanonicalRow.
    """
    source = payload.get("__source")
    # /checkouts.json nests the buyer under "customer"; /customers.json is the buyer itself.
    customer = payload.get("customer") if source == "abandoned_checkouts" else payload
    customer = customer or {}

    phone_raw = (
        customer.get("phone")
        or payload.get("phone")
        or (customer.get("default_address") or {}).get("phone")
        or (payload.get("shipping_address") or {}).get("phone")
    )
    phone_e164 = _normalize_e164(phone_raw)
    email_raw = customer.get("email") or payload.get("email")
    email = (
        email_raw.strip().lower()
        if isinstance(email_raw, str) and email_raw.strip()
        else None
    )
    display_name = (
        f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        or None
    )

    if not (phone_e164 or email or display_name):
        return None

    sales: tuple[SaleLine, ...] = ()
    if source == "abandoned_checkouts":
        currency = (payload.get("currency") or "").upper()
        paise = _total_price_to_paise(payload.get("total_price"))
        entry_date = _order_date(payload.get("created_at"))
        if paise is not None and entry_date is not None and (not currency or currency == _INR):
            sales = (SaleLine(amount_paise=paise, entry_date=entry_date, confidence=1.0),)

    return CanonicalRow(
        phone_e164=phone_e164,
        email=email,
        display_name=display_name,
        sales=sales,
        consent=None,  # option A — Shopify writes no consent (detector AND-gate stays closed)
    )


class ShopifyConnector(ConnectorBase):
    """Shopify Admin API connector."""

    connector_id: str = "shopify"

    def __init__(
        self,
        *,
        grant_fn: GrantFn | None = None,
        exchange_fn: ExchangeFn | None = None,
    ) -> None:
        # grant_fn / exchange_fn injectable for tests (defaults = the real POSTs).
        self._grant_fn: GrantFn = grant_fn or _default_grant
        self._exchange_fn: ExchangeFn = exchange_fn or _default_oauth_exchange

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

    # ---------- AUTH (OWNER-FACING OAuth managed-install — VT-283) ----------

    def build_oauth_install_url(
        self, tenant_id: UUID, shop: str, *, state: str
    ) -> str:
        """Step 1 of the merchant OAuth install — the URL the owner is sent to.

        ``shop`` is the owner-entered ``*.myshopify.com`` domain (validated before
        interpolation — Cowork #1). VT-289: ``state`` is the single-use nonce minted
        by ``oauth_state.mint_install_state`` (NOT the raw tenant_id) — the callback
        claims it and derives the tenant from the stored record, so a forged ``state``
        cannot bind a token to another tenant. Offline access is requested (Shopify's
        default — ``grant_options[]`` is omitted; an online token would pass
        ``grant_options[]=per-user``), so background pulls keep working after the
        merchant session ends (Cowork #3).
        """
        shop = _validate_shop_domain(shop)
        client_id, _ = _shopify_oauth_creds()
        redirect_uri = _shopify_redirect_uri()
        query = urlencode(
            {
                "client_id": client_id,
                "scope": ",".join(sorted(_REQUIRED_SCOPES)),
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return f"https://{shop}{_AUTHORIZE_PATH}?{query}"

    def _oauth_exchange_and_store(
        self, tenant_id: UUID, shop: str, code: str
    ) -> dict[str, Any]:
        """Exchange the merchant's OAuth ``code`` for an OFFLINE access token and
        persist it encrypted. Offline tokens do not expire → ``expires_at = NULL``
        (the discriminator ``get_access_token`` reads to skip the
        client_credentials re-grant for OAuth tenants)."""
        shop = _validate_shop_domain(shop)
        client_id, client_secret = _shopify_oauth_creds()
        token = self._exchange_fn(shop, client_id, client_secret, code)
        access_token = token.get("access_token")
        if not access_token:
            raise AuthValidationError(
                f"OAuth exchange returned no access_token: {str(token)[:200]!r}"
            )
        scope_str = token.get("scope") or ""
        scopes = (
            [s.strip() for s in scope_str.split(",") if s.strip()]
            if scope_str else sorted(_REQUIRED_SCOPES)
        )
        encrypted = encrypt_value(str(access_token))
        push_secret = secrets.token_urlsafe(32)
        pool = get_pool()
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO tenant_oauth_tokens (
                    tenant_id, connector_id, refresh_token_encrypted,
                    scopes, push_secret, shop_url, last_refreshed_at, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, now(), NULL)
                ON CONFLICT (tenant_id, connector_id) DO UPDATE SET
                    refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
                    scopes = EXCLUDED.scopes,
                    push_secret = COALESCE(
                        tenant_oauth_tokens.push_secret, EXCLUDED.push_secret
                    ),
                    shop_url = EXCLUDED.shop_url,
                    last_refreshed_at = now(),
                    expires_at = NULL
                """,
                (
                    str(tenant_id), self.connector_id, encrypted,
                    scopes, push_secret, shop,
                ),
            )
        return {
            "success": True,
            "mode": "oauth_install",
            "shop_url": shop,
            "scopes": scopes,
        }

    def _grant_and_store(self, tenant_id: UUID) -> dict[str, Any]:
        """TEST / OWN-STORE FALLBACK ONLY (VT-422 GAP-4) — run the client_credentials
        grant + persist the token (encrypted, 24h TTL).

        Same-org-only (app + store in ONE org), so it CANNOT serve a real merchant on
        a different org — that is the canonical OAuth-install path
        (``_oauth_exchange_and_store``). Retained because it is the only way to
        exercise pulls against our own dev store without a different-org merchant.
        Do NOT delete; do NOT treat as the canonical auth path.
        """
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
        """Dual-mode (VT-283):

        * ``auth_payload`` carries ``code`` + ``shop`` -> OAuth authorization-code
          (merchant install) -> offline token persisted.
        * empty / None -> client_credentials grant (dev/own-store, existing path).

        Mode = presence of ``code``; ``shop`` is required alongside it (a code
        without a shop is a malformed callback, not a client_credentials request).
        """
        if auth_payload and auth_payload.get("code"):
            shop = auth_payload.get("shop")
            if not shop:
                raise ValueError(
                    "complete_auth: OAuth 'code' present but 'shop' missing"
                )
            return self._oauth_exchange_and_store(
                tenant_id, str(shop), str(auth_payload["code"])
            )
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
        """Return ``(access_token, shop_url)``.

        OAuth-install tenants (offline token, ``expires_at IS NULL``) use the
        stored token as-is — NEVER re-granting via client_credentials, which
        would fail for a real merchant on a different org. client_credentials
        tenants (24h TTL) grant on first use and proactively re-grant within a
        5-min skew of expiry.
        """
        row = self._read_token_row(tenant_id)
        if row is not None:
            expires_at = row["expires_at"]
            if expires_at is None:
                # offline OAuth-install token — no expiry, use as-is (VT-283).
                return decrypt_value(row["refresh_token_encrypted"]), row["shop_url"]
            if expires_at > datetime.now(UTC) + _EXPIRY_SKEW:
                return decrypt_value(row["refresh_token_encrypted"]), row["shop_url"]
        # No row (first client_credentials connect) OR a near-expiry
        # client_credentials token → (re-)grant.
        self._grant_and_store(tenant_id)
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

    def pull_orders(
        self, tenant_id: UUID, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """VT-417 — initial-backfill ORDERS pull (the sale-of-record substrate).

        ``pull_full`` only pulls ``/customers.json`` (identity, no sales). For
        backfill of the Sales-Recovery substrate we need orders. Phase-1 cap = 250
        (Shopify's default page_size); Link-header pagination beyond one page is a
        VT-N follow-up (a known backfill ceiling for high-volume stores) — mirrors
        the documented ``pull_full`` cap.
        """
        params: dict[str, str] = {"status": "any", "limit": "250"}
        if since is not None:
            params["updated_at_min"] = since.replace(microsecond=0).isoformat()
        return self._request(
            tenant_id, "/orders.json", params=params
        ).get("orders", [])

    def backfill_orders(
        self, tenant_id: UUID, since: datetime | None = None
    ) -> dict[str, int]:
        """Pull orders (capped 250) → map each via ``shopify_order_to_canonical``
        → land via ``ingest_customer_rows(acquired_via='shopify')`` (the SAME seam
        the live webhook uses). Returns counts only (no PII).
        """
        from orchestrator.integrations.ingest import ingest_customer_rows

        orders = self.pull_orders(tenant_id, since)
        rows: list[CanonicalRow] = []
        skipped_non_inr = 0
        for order in orders:
            mapped = shopify_order_to_canonical(order)
            if mapped.skipped_non_inr:
                skipped_non_inr += 1
            if mapped.row is not None:
                rows.append(mapped.row)
        summary = ingest_customer_rows(
            tenant_id, rows, acquired_via=_SHOPIFY_ACQUIRED_VIA
        )
        return {
            "orders_pulled": len(orders),
            "committed": summary.committed,
            "sales_written": summary.sales_written,
            "sales_skipped_duplicate": summary.sales_skipped_duplicate,
            "ambiguous": summary.ambiguous,
            "dropped": summary.dropped,
            "skipped_non_inr": skipped_non_inr,
        }

    # ---------- PUSH ----------

    def setup_push(self, tenant_id: UUID) -> dict[str, str]:
        """Register Shopify webhooks for checkouts + orders.

        Hits POST /admin/api/.../webhooks.json for the 4 topics this connector cares
        about. VT-422: an APP-registered webhook is signed by Shopify with the APP's
        ``client_secret`` (``SHOPIFY_API_SECRET``) — NOT a per-tenant secret — so the
        webhook handler verifies against the app secret (see api/shopify_webhook.py).
        ``push_secret`` is vestigial for this OAuth path (retained for the sheet path);
        it is read here only as an install precondition + echoed as a hint.

        Wired to fire on OAuth-install success (api/shopify_oauth.py callback, VT-422)
        so the webhooks actually register on install.
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
            "currency": payload.get("currency"),  # VT-417 — money-row currency guard
            "acquired_via": "shopify",
            "__source": "shopify_webhook",
        }
        return [row] if row.get("phone") or row.get("email") else []


# Decode helper kept here so tests can build canonical-base64 fixtures
# without re-importing the cryptography stdlib in canary code.
def _b64_decode(value: str) -> bytes:
    """Forward standard b64decode for canary fixtures."""
    return b64decode(value)


__all__ = [
    "AuthValidationError",
    "ShopifyConfigError",
    "ShopDomainError",
    "ShopifyConnector",
    "shopify_order_to_canonical",
    "shopify_sample_row_to_canonical",
    "validate_shop_domain",
    "verify_oauth_hmac",
]
