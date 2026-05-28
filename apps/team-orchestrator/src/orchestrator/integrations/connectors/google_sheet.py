"""VT-207 — Google Sheets connector.

OAuth 2.0 (PKCE-compatible web flow) + Sheets v4 sample/full pull.
Subclasses VT-205's ``ConnectorBase``. Refresh tokens encrypted at
rest via VT-191 Fernet substrate (shared ``encrypt_value`` helper).

Q1/Q2/Q3/Q5 Option A locked per Cowork plan-review 2026-05-28.

Per CL-390: scope minimised to ``spreadsheets.readonly``.
Per CL-71: every DB write is service-role via tenant_connection.
Per CL-19: typed return shapes via Pydantic (CanonicalRow shape
declared inline; future VT row can extract).
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
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


_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


@dataclass(frozen=True)
class SamplePayload:
    """Result of ``pull_sample``."""

    row_count: int
    column_names: list[str]
    rows: list[dict[str, Any]]


@dataclass(frozen=True)
class CanonicalRow:
    """One owner-supplied Sheet row mapped to canonical fields.

    Shape kept minimal; the field-mapping reasoner (VT-209) translates
    arbitrary Sheet columns into these slots. None values mean the
    column wasn't mapped or the source cell was empty.
    """

    source_row_index: int
    customer_name: str | None = None
    phone: str | None = None
    email: str | None = None
    order_amount: str | None = None
    order_date: str | None = None


def _env_required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} not set — required for Google OAuth (see "
            ".viabe/secrets/google-oauth.env)"
        )
    return value


class GoogleSheetConnector(ConnectorBase):
    """Google Sheets ConnectorBase implementation."""

    connector_id: str = "google_sheet"

    @property
    def spec(self) -> ConnectorSpec:
        return get_connector("google_sheet")

    # ---------- AUTH ----------

    def build_auth_url(self, tenant_id: UUID) -> str:
        """Step 1 of OAuth — returns the URL the owner clicks.

        ``state`` carries ``tenant_id`` so the callback can resolve the
        write target. Production deployments should sign + verify
        state to prevent CSRF; Phase-1 stores it raw.
        """
        client_id = _env_required("GOOGLE_OAUTH_CLIENT_ID")
        redirect_uri = _env_required("GOOGLE_OAUTH_REDIRECT_URI")
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": str(tenant_id),
        }
        encoded = "&".join(f"{k}={httpx.QueryParams({k: v})[k]}" for k, v in params.items())
        return f"{_OAUTH_AUTH_URL}?{encoded}"

    def start_auth(self, tenant_id: UUID) -> dict[str, Any]:
        return {
            "auth_url": self.build_auth_url(tenant_id),
            "next_action": "show_auth_url_to_owner",
        }

    def complete_auth(
        self, tenant_id: UUID, auth_payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Exchange auth_code → refresh_token; persist encrypted."""
        client_id = _env_required("GOOGLE_OAUTH_CLIENT_ID")
        client_secret = _env_required("GOOGLE_OAUTH_CLIENT_SECRET")
        redirect_uri = _env_required("GOOGLE_OAUTH_REDIRECT_URI")
        code = auth_payload.get("code")
        if not code:
            raise ValueError("complete_auth: 'code' missing from auth_payload")

        resp = httpx.post(
            _OAUTH_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Google OAuth token exchange failed: HTTP {resp.status_code} "
                f"body={resp.text[:200]}"
            )
        token_data = resp.json()
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            raise RuntimeError(
                "Google OAuth response missing refresh_token. Ensure "
                "access_type=offline + prompt=consent are requested."
            )
        expires_in = int(token_data.get("expires_in", 3600))
        scopes = (token_data.get("scope") or _SCOPE).split()

        encrypted = encrypt_value(refresh_token)
        push_secret = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

        pool = get_pool()
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO tenant_oauth_tokens (
                    tenant_id, connector_id, refresh_token_encrypted,
                    scopes, push_secret, last_refreshed_at, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, now(), %s)
                ON CONFLICT (tenant_id, connector_id) DO UPDATE SET
                    refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
                    scopes = EXCLUDED.scopes,
                    push_secret = COALESCE(tenant_oauth_tokens.push_secret, EXCLUDED.push_secret),
                    last_refreshed_at = now(),
                    expires_at = EXCLUDED.expires_at
                """,
                (
                    str(tenant_id), self.connector_id, encrypted,
                    scopes, push_secret, expires_at,
                ),
            )
        return {
            "success": True,
            "scopes": scopes,
            "expires_at": expires_at.isoformat(),
        }

    def _load_refresh_token(self, tenant_id: UUID) -> str:
        pool = get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT refresh_token_encrypted FROM tenant_oauth_tokens "
                "WHERE tenant_id = %s AND connector_id = %s",
                (str(tenant_id), self.connector_id),
            )
            raw = cur.fetchone()
        if raw is None:
            raise RuntimeError(
                f"no OAuth token for tenant {tenant_id} / {self.connector_id}"
            )
        row = cast("dict[str, Any]", raw)
        return decrypt_value(row["refresh_token_encrypted"])

    def get_access_token(self, tenant_id: UUID) -> str:
        """Exchange refresh_token → short-lived access_token via Google."""
        client_id = _env_required("GOOGLE_OAUTH_CLIENT_ID")
        client_secret = _env_required("GOOGLE_OAUTH_CLIENT_SECRET")
        refresh_token = self._load_refresh_token(tenant_id)
        resp = httpx.post(
            _OAUTH_TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            },
            timeout=15.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Google OAuth refresh failed: HTTP {resp.status_code} "
                f"body={resp.text[:200]}"
            )
        access_token = resp.json().get("access_token")
        if not access_token:
            raise RuntimeError("Google OAuth refresh response missing access_token")
        return str(access_token)

    # ---------- PULL ----------

    def pull_sample(
        self,
        tenant_id: UUID,
        spreadsheet_id: str = "",
        range_a1: str = "A1:Z50",
    ) -> list[dict[str, Any]]:
        """Fetch first ~50 rows for field-mapping."""
        if not spreadsheet_id:
            raise ValueError("pull_sample: spreadsheet_id required")
        access_token = self.get_access_token(tenant_id)
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/"
            f"{spreadsheet_id}/values/{range_a1}"
        )
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Sheets pull_sample failed: HTTP {resp.status_code} "
                f"body={resp.text[:200]}"
            )
        values = resp.json().get("values", [])
        if not values:
            return []
        headers = [str(h) for h in values[0]]
        rows: list[dict[str, Any]] = []
        for row_data in values[1:]:
            row: dict[str, Any] = {}
            for i, header in enumerate(headers):
                row[header] = row_data[i] if i < len(row_data) else None
            rows.append(row)
        return rows

    def pull_full(
        self,
        tenant_id: UUID,
        spreadsheet_id: str = "",
        since_row_index: int = 0,
    ) -> list[CanonicalRow]:
        """Incremental pull from ``since_row_index`` to end.

        Row-index cursor strategy per Q5 Option A. Assumes append-only;
        mid-sheet deletes shift indices and may re-ingest rows
        (dedupe via phone_hash handles duplicate inserts).
        """
        if not spreadsheet_id:
            raise ValueError("pull_full: spreadsheet_id required")
        access_token = self.get_access_token(tenant_id)
        start_row = max(2, since_row_index + 1)
        range_a1 = f"A{start_row}:Z"
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/"
            f"{spreadsheet_id}/values/{range_a1}"
        )
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Sheets pull_full failed: HTTP {resp.status_code}"
            )
        values = resp.json().get("values", [])
        return [
            CanonicalRow(source_row_index=since_row_index + i + 1)
            for i, _ in enumerate(values)
        ]

    def setup_push(self, tenant_id: UUID, spreadsheet_id: str) -> dict[str, str]:
        """Generate Apps Script template + return push_secret.

        Owner pastes the script into the Sheet's Extensions → Apps
        Script panel. Script's ``onEdit`` POSTs changed rows to
        ``/api/orchestrator/integrations/sheet/push`` with the HMAC
        secret.
        """
        from orchestrator.integrations.connectors.apps_script_template import (
            render_apps_script,
        )

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
        script = render_apps_script(
            tenant_id=str(tenant_id),
            spreadsheet_id=spreadsheet_id,
            orchestrator_base=orchestrator_base,
            push_secret=push_secret,
        )
        return {"apps_script": script}


__all__ = [
    "CanonicalRow",
    "GoogleSheetConnector",
    "SamplePayload",
]
