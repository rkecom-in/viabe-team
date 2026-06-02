"""VT-286 — owner-owned WABA onboarding via Meta Embedded Signup (Twilio tech-provider).

Meta mandates client-owned WhatsApp Business Accounts (On-Behalf-Of is dead). The owner
runs a ~5-min Embedded Signup popup (+ business verification + display name = their shop
name + privacy URL); everything after is automated by a system user. We provision a
DEDICATED new number per tenant (Fazal's choice) and persist the WABA + token per tenant
in `tenant_whatsapp_accounts` (migration 069).

Security / privacy:
- The owner-facing entry goes through the secured `/whatsapp/setup` endpoint
  (INTERNAL_API_SECRET) which mints a VT-289 state nonce; the callback CLAIMS the nonce
  and derives the tenant from the stored record (never the URL).
- Access token encrypted at rest via the VT-191 Fernet substrate (CL-390). RLS-real:
  writes go through `tenant_connection` (SET ROLE app_role + tenant GUC; CL-82/88).

Send-gate (Jan-2026 Meta rule): a tenant CANNOT send until status == 'live' (business
verification + privacy URL approved). `wa_send_allowed` is fail-CLOSED.

Twilio tech-provider: the real exchange/provision target Twilio's WhatsApp Embedded
Signup / Senders API with a subaccount-per-tenant shape (Cowork VT-286 #1). Both are
INJECTABLE so the unit/DB canary runs without the network; the live real-merchant walk
is E2E-deferred (Fazal initiates the Tech Provider track separately — do NOT block).
# live Embedded-Signup walk deferred to E2E (Fazal 2026-06-02) — see launch-tracker.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx

from orchestrator.db import tenant_connection
from orchestrator.observability.encrypt_value import encrypt_value

logger = logging.getLogger(__name__)

_EMBEDDED_SIGNUP_DIALOG = "https://www.facebook.com/v19.0/dialog/oauth"
_VALID_STATUSES = ("pending", "verifying", "name_approved", "live")

# code -> {access_token, waba_id}. Injectable (default = real Twilio/Meta exchange).
ExchangeFn = Callable[[str], dict[str, Any]]
# waba_id -> {phone_number, phone_number_id}. Injectable (default = Twilio number purchase).
ProvisionFn = Callable[[str], dict[str, Any]]


class WhatsAppConfigError(Exception):
    """Raised when the WA_APP_ID / WA_CONFIG_ID / WA_REDIRECT_URI env is absent."""


@dataclass(frozen=True, slots=True)
class WabaAccount:
    tenant_id: UUID
    waba_id: str | None
    phone_number: str | None
    display_name: str | None
    status: str


def _wa_env() -> tuple[str, str, str]:
    """(app_id, config_id, redirect_uri) from .viabe/secrets/whatsapp.env."""
    app_id = os.environ.get("WA_APP_ID")
    config_id = os.environ.get("WA_CONFIG_ID")
    redirect_uri = os.environ.get("WA_REDIRECT_URI")
    if not (app_id and config_id and redirect_uri):
        raise WhatsAppConfigError(
            "WA_APP_ID / WA_CONFIG_ID / WA_REDIRECT_URI must be set "
            "(.viabe/secrets/whatsapp.env)"
        )
    return app_id, config_id, redirect_uri


def _default_exchange(code: str) -> dict[str, Any]:
    """Real Twilio/Meta Embedded-Signup token exchange (tech-provider). Unverified
    against live until the E2E walk; the unit/DB canary injects a fake."""
    app_id, _, redirect_uri = _wa_env()
    app_secret = os.environ.get("WA_APP_SECRET", "")
    resp = httpx.get(
        "https://graph.facebook.com/v19.0/oauth/access_token",
        params={
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"WABA Embedded-Signup exchange failed: HTTP {resp.status_code} "
            f"body={resp.text[:300]}"
        )
    data = resp.json()
    return {"access_token": data.get("access_token"), "waba_id": data.get("waba_id")}


def _default_provision(waba_id: str) -> dict[str, Any]:
    """Provision a dedicated number for the WABA (Twilio number purchase). Unverified
    against live until the E2E walk; the canary injects a fake."""
    raise NotImplementedError(
        "live Twilio number provisioning wired at E2E (Fazal Tech Provider track)"
    )


def build_embedded_signup_url(tenant_id: UUID, state: str) -> str:
    """Step 1 — the Meta Embedded Signup URL the owner is sent to. ``state`` is the
    single-use VT-289 nonce (not the raw tenant); the callback claims it."""
    app_id, config_id, redirect_uri = _wa_env()
    query = urlencode(
        {
            "client_id": app_id,
            "config_id": config_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "override_default_response_type": "true",
        }
    )
    return f"{_EMBEDDED_SIGNUP_DIALOG}?{query}"


def connect_waba(
    tenant_id: UUID | str,
    code: str,
    *,
    display_name: str | None = None,
    exchange_fn: ExchangeFn | None = None,
    provision_fn: ProvisionFn | None = None,
) -> WabaAccount:
    """Exchange the Embedded-Signup code → token + WABA id, provision a dedicated
    number, and persist (encrypted) at status='verifying'. The tenant_id MUST already
    be resolved from the VT-289 nonce (never the URL state)."""
    exchange = exchange_fn or _default_exchange
    provision = provision_fn or _default_provision

    token = exchange(code)
    access_token = token.get("access_token")
    waba_id = token.get("waba_id")
    if not access_token or not waba_id:
        raise RuntimeError(
            f"Embedded-Signup exchange missing access_token/waba_id: {str(token)[:200]!r}"
        )
    provisioned = provision(str(waba_id))
    phone_number = provisioned.get("phone_number")
    phone_number_id = provisioned.get("phone_number_id")

    encrypted = encrypt_value(str(access_token))
    tid = str(tenant_id)
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            """
            INSERT INTO tenant_whatsapp_accounts
                (tenant_id, waba_id, phone_number_id, phone_number, display_name,
                 status, access_token_encrypted)
            VALUES (%s, %s, %s, %s, %s, 'verifying', %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                waba_id = EXCLUDED.waba_id,
                phone_number_id = EXCLUDED.phone_number_id,
                phone_number = EXCLUDED.phone_number,
                display_name = COALESCE(EXCLUDED.display_name, tenant_whatsapp_accounts.display_name),
                status = 'verifying',
                access_token_encrypted = EXCLUDED.access_token_encrypted,
                last_updated = now()
            """,
            (tid, str(waba_id), phone_number_id, phone_number, display_name, encrypted),
        )
    logger.info("WABA connected tenant=%s waba=%s status=verifying", tid, waba_id)
    return WabaAccount(
        tenant_id=UUID(tid),
        waba_id=str(waba_id),
        phone_number=phone_number if phone_number is None else str(phone_number),
        display_name=display_name,
        status="verifying",
    )


def set_status(tenant_id: UUID | str, status: str) -> bool:
    """Advance the WABA status (pending→verifying→name_approved→live). Returns True if
    a row was updated."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"invalid WABA status {status!r}; valid: {_VALID_STATUSES}")
    with tenant_connection(tenant_id) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE tenant_whatsapp_accounts SET status = %s, last_updated = now() "
            "WHERE tenant_id = %s",
            (status, str(tenant_id)),
        )
        return cur.rowcount > 0


def wa_send_allowed(tenant_id: UUID | str) -> bool:
    """Fail-CLOSED send gate: True iff the tenant's WABA is `live` (business verified +
    display name approved). No row, or any non-live status, returns False. VT-287's send
    path MUST call this."""
    with tenant_connection(tenant_id) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM tenant_whatsapp_accounts WHERE tenant_id = %s",
            (str(tenant_id),),
        )
        row = cur.fetchone()
    if row is None:
        return False
    status = row["status"] if isinstance(row, dict) else row[0]
    return status == "live"


__all__ = [
    "WhatsAppConfigError",
    "WabaAccount",
    "build_embedded_signup_url",
    "connect_waba",
    "set_status",
    "wa_send_allowed",
]
