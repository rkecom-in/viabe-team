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

from orchestrator.db import tenant_connection
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
# Scopes per CL-390 + CL-421:
#   spreadsheets.readonly   — pull rows (VT-207 substrate)
#   drive.metadata.readonly — register Drive Push channels (VT-222)
# Tenants onboarded pre-VT-222 may have only the spreadsheets scope;
# they fall back to polling (no Drive Push registration). See VT-222
# sheet-integration-runbook.md for the scope migration story.
_SCOPE_SHEETS = "https://www.googleapis.com/auth/spreadsheets.readonly"
_SCOPE_DRIVE_METADATA = "https://www.googleapis.com/auth/drive.metadata.readonly"
_SCOPE = f"{_SCOPE_SHEETS} {_SCOPE_DRIVE_METADATA}"

_DRIVE_WATCH_URL = "https://www.googleapis.com/drive/v3/files/{file_id}/watch"
_DRIVE_STOP_URL = "https://www.googleapis.com/drive/v3/channels/stop"
_DRIVE_FILES_LIST_URL = "https://www.googleapis.com/drive/v3/files"  # VT-608 picker discovery
_DRIVE_CHANNEL_TTL = timedelta(days=7)  # Drive max
_DRIVE_RENEW_WINDOW = timedelta(hours=48)


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

    def build_auth_url(self, tenant_id: UUID, *, state: str) -> str:
        """Step 1 of OAuth — returns the URL the owner clicks.

        VT-289: ``state`` is the single-use nonce minted by
        ``oauth_state.mint_install_state`` (from the authenticated ``/setup`` path) —
        NOT the raw tenant_id. The callback claims it and derives the tenant from the
        stored record, so a forged ``state`` cannot bind a token to another tenant.
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
            "state": state,
        }
        encoded = "&".join(f"{k}={httpx.QueryParams({k: v})[k]}" for k, v in params.items())
        return f"{_OAUTH_AUTH_URL}?{encoded}"

    def start_auth(self, tenant_id: UUID) -> dict[str, Any]:
        """ConnectorBase entry. Mints a VT-289 nonce (server-side) and returns the
        authorize URL. The owner-facing HTTP path goes through the secured
        ``/google_sheet/setup`` endpoint (INTERNAL_API_SECRET) — this programmatic
        entry is for the registry/scheduler."""
        from orchestrator.integrations.oauth_state import mint_install_state

        state = mint_install_state(tenant_id, self.connector_id)
        return {
            "auth_url": self.build_auth_url(tenant_id, state=state),
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

        # VT-608 raw-pool sweep (mirrors VT-603's own swap): RLS-scoped write keyed on the tenant
        # this method was CALLED with — tenant_oauth_tokens has RLS enabled+forced (mig 033).
        with tenant_connection(tenant_id) as conn:
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
        with tenant_connection(tenant_id) as conn:
            raw = conn.execute(
                "SELECT refresh_token_encrypted FROM tenant_oauth_tokens "
                "WHERE tenant_id = %s AND connector_id = %s",
                (str(tenant_id), self.connector_id),
            ).fetchone()
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

    # ---------- DISCOVERY (VT-608, the Sheets picker's own backend) ----------

    def list_spreadsheets(self, tenant_id: UUID, *, limit: int = 25) -> list[dict[str, str]]:
        """List the owner's Google Sheets spreadsheets (Drive ``files.list``, scoped to
        ``mimeType='application/vnd.google-apps.spreadsheet'`` — the ``drive.metadata.readonly``
        scope already granted covers this). Returns ``[{"id": ..., "name": ...}, ...]`` — the
        team-web picker page's own data source; no sheet CONTENT is read here."""
        access_token = self.get_access_token(tenant_id)
        resp = httpx.get(
            _DRIVE_FILES_LIST_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "q": "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
                "fields": "files(id,name)",
                "pageSize": limit,
                "orderBy": "modifiedTime desc",
            },
            timeout=15.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Drive files.list failed: HTTP {resp.status_code} body={resp.text[:200]}"
            )
        files = resp.json().get("files", [])
        return [{"id": str(f["id"]), "name": str(f.get("name", ""))} for f in files]

    def list_tabs(self, tenant_id: UUID, spreadsheet_id: str) -> list[str]:
        """List a spreadsheet's tab (sheet) names via Sheets ``spreadsheets.get`` — metadata only
        (``fields=sheets.properties.title``), never cell content."""
        access_token = self.get_access_token(tenant_id)
        resp = httpx.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "sheets.properties.title"},
            timeout=15.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Sheets spreadsheets.get failed: HTTP {resp.status_code} body={resp.text[:200]}"
            )
        sheets = resp.json().get("sheets", [])
        return [str(s["properties"]["title"]) for s in sheets if s.get("properties", {}).get("title")]

    # ---------- PULL ----------

    def pull_sample(
        self,
        tenant_id: UUID,
        spreadsheet_id: str = "",
        range_a1: str = "A1:Z50",
        *,
        tab_name: str = "",
    ) -> list[dict[str, Any]]:
        """Fetch first ~50 rows for field-mapping.

        VT-608 — ``tab_name`` (the owner's picker selection, RULING 2) scopes the range to a
        SPECIFIC sheet tab (``'{tab_name}'!A1:Z50``); omitted (the pre-VT-608 default) reads
        whichever tab Sheets' bare-range notation resolves to (its first/active sheet) —
        every existing caller that never passed a tab keeps that exact behavior.
        """
        if not spreadsheet_id:
            raise ValueError("pull_sample: spreadsheet_id required")
        access_token = self.get_access_token(tenant_id)
        effective_range = f"'{tab_name}'!{range_a1}" if tab_name else range_a1
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/"
            f"{spreadsheet_id}/values/{effective_range}"
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
        *,
        since: datetime | None = None,  # base-contract alias; ignored (row-index cursor)
        tab_name: str = "",
    ) -> list[dict[str, Any]]:
        """Incremental pull from ``since_row_index`` to end.

        Row-index cursor strategy per Q5 Option A. Assumes append-only;
        mid-sheet deletes shift indices and may re-ingest rows
        (dedupe via phone_hash handles duplicate inserts).

        VT-417 PR-2: returns the actual ``{column -> cell}`` row dicts (header row
        zipped with each data row) so the Drive-pull / scheduler paths can map them
        to ``CanonicalRow`` via ``ingest.sheet_row_to_canonical``. Previously this
        returned data-LESS ``CanonicalRow(source_row_index=...)`` envelopes — the
        cell values were discarded, so the pull lineage could never ingest a
        customer. ``since`` (the ``ConnectorBase`` datetime cursor) is accepted for
        signature uniformity but ignored — this connector cursors by ROW INDEX, not
        time (the scheduler's google_sheet path is a known gap: it has no
        spreadsheet_id / row-index cursor — the Drive poll path carries the
        resource_id and is the real sheet-pull driver).

        VT-608 — ``tab_name`` (the owner's picker selection, RULING 2) scopes both the header
        fetch and the data range to that SPECIFIC tab; omitted (every pre-VT-608 caller) keeps
        the exact prior bare-range behavior.
        """
        if not spreadsheet_id:
            raise ValueError("pull_full: spreadsheet_id required")
        access_token = self.get_access_token(tenant_id)
        tab_prefix = f"'{tab_name}'!" if tab_name else ""
        # Header (row 1) is needed to label cells — pull_full's old range
        # (A{start}:Z) skipped it, leaving rows un-labellable.
        header_resp = httpx.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{tab_prefix}A1:Z1",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        if header_resp.status_code != 200:
            raise RuntimeError(
                f"Sheets pull_full header fetch failed: HTTP {header_resp.status_code}"
            )
        header_values = header_resp.json().get("values", [])
        headers = [str(h) for h in header_values[0]] if header_values else []

        start_row = max(2, since_row_index + 1)
        range_a1 = f"{tab_prefix}A{start_row}:Z"
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
        rows: list[dict[str, Any]] = []
        for row_data in values:
            row: dict[str, Any] = {}
            for i, h in enumerate(headers):
                row[h] = row_data[i] if i < len(row_data) else None
            rows.append(row)
        return rows

    def verify_push_signature(
        self, body: bytes, headers: dict[str, str], push_secret: str
    ) -> bool:
        """Verify Apps Script HMAC-SHA256 signature.

        Header name: ``X-Viabe-Signature`` (lowercase hex digest).
        VT-210 Q4 lock: previously a standalone helper in
        ``apps_script_template``; promoted into the connector class so
        the generic VT-210 push receiver invokes one interface.
        """
        from orchestrator.integrations.connectors.apps_script_template import (
            verify_push_signature as _legacy_verify,
        )

        signature = headers.get("x-viabe-signature") or headers.get(
            "X-Viabe-Signature", ""
        )
        return _legacy_verify(
            body=body, signature=signature, push_secret=push_secret
        )

    def parse_push_payload(self, body: bytes) -> list[dict[str, Any]]:
        """Apps Script POST body shape: ``{"row_data": {...}, ...}``.

        Returns a one-element list (single-row append events).
        """
        import json as _json

        payload = _json.loads(body.decode("utf-8"))
        row_data = payload.get("row_data") or {}
        return [row_data] if row_data else []

    def setup_push(self, tenant_id: UUID, spreadsheet_id: str) -> dict[str, str]:
        """DEPRECATED per CL-421 (2026-05-29). Apps Script paste flow is
        customer-hostile. VT-222 replaces this with Drive Push
        Notifications (``register_drive_push_channel``). Kept for
        backward compatibility while existing Apps-Script-onboarded
        tenants migrate. New onboarding flows must use
        ``register_drive_push_channel`` exclusively.

        Generate Apps Script template + return push_secret.

        Owner pastes the script into the Sheet's Extensions → Apps
        Script panel. Script's ``onEdit`` POSTs changed rows to
        ``/api/orchestrator/integrations/sheet/push`` with the HMAC
        secret.
        """
        from orchestrator.integrations.connectors.apps_script_template import (
            render_apps_script,
        )

        with tenant_connection(tenant_id) as conn:
            raw = conn.execute(
                "SELECT push_secret FROM tenant_oauth_tokens "
                "WHERE tenant_id = %s AND connector_id = %s",
                (str(tenant_id), self.connector_id),
            ).fetchone()
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

    # ---------- DRIVE PUSH NOTIFICATIONS (VT-222 / CL-421) ----------

    def register_drive_push_channel(
        self, tenant_id: UUID, spreadsheet_id: str
    ) -> dict[str, Any]:
        """Register a Drive Push channel for the given Sheet.

        Calls Drive API ``files.watch`` with our webhook URL as the
        target. Persists channel into ``tenant_drive_channels``. Returns
        the channel descriptor (channel_id, expires_at, resource_id).

        Requires the tenant's OAuth token to carry the
        ``drive.metadata.readonly`` scope. Tenants onboarded pre-VT-222
        may lack it; this method raises ``RuntimeError`` in that case
        and the caller falls back to polling.
        """
        from uuid import uuid4

        access_token = self.get_access_token(tenant_id)
        channel_id = str(uuid4())
        channel_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + _DRIVE_CHANNEL_TTL
        orchestrator_base = os.environ.get(
            "ORCHESTRATOR_BASE_URL", "http://localhost:8001"
        )
        webhook_url = f"{orchestrator_base}/api/orchestrator/integrations/sheet/drive_push"

        resp = httpx.post(
            _DRIVE_WATCH_URL.format(file_id=spreadsheet_id),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "id": channel_id,
                "type": "web_hook",
                "address": webhook_url,
                "token": channel_token,
                "expiration": int(expires_at.timestamp() * 1000),
            },
            timeout=15.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Drive files.watch failed: HTTP {resp.status_code} "
                f"body={resp.text[:200]}"
            )

        resp_body = resp.json()
        # Drive returns its own resourceId we persist as the canonical
        # handle alongside our channel_id; both are needed for stop()
        resource_id = str(resp_body.get("resourceId", spreadsheet_id))
        # Drive may return a different expiration than we requested
        # (their max could be shorter for some accounts); honour theirs
        if resp_body.get("expiration"):
            expires_at = datetime.fromtimestamp(
                int(resp_body["expiration"]) / 1000, tz=UTC
            )

        # tenant_drive_channels has RLS enabled (mig 040).
        with tenant_connection(tenant_id) as conn:
            conn.execute(
                """
                INSERT INTO tenant_drive_channels
                    (tenant_id, connector_id, resource_id, channel_id,
                     channel_token, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    str(tenant_id),
                    self.connector_id,
                    resource_id,
                    channel_id,
                    channel_token,
                    expires_at,
                ),
            )
        return {
            "channel_id": channel_id,
            "resource_id": resource_id,
            "expires_at": expires_at.isoformat(),
        }

    def unregister_drive_push_channel(
        self, tenant_id: UUID, channel_id: str, resource_id: str
    ) -> None:
        """Stop a Drive Push channel via Drive ``channels.stop``.

        Removes the matching row from ``tenant_drive_channels``.
        Idempotent: missing channel raises silently (already stopped).

        VT-608 raw-pool sweep: ``tenant_id`` is now a REQUIRED param (its only caller,
        ``renew_drive_push_channel``, already has it from the channel row it read) rather than
        looking it up via a privileged pool read keyed on the opaque ``channel_id`` — every DB
        touch here is now RLS-scoped to a tenant the caller already resolved.
        """
        access_token = self.get_access_token(tenant_id)

        resp = httpx.post(
            _DRIVE_STOP_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"id": channel_id, "resourceId": resource_id},
            timeout=10.0,
        )
        # 204 = stopped; 404 = already stopped (acceptable)
        if resp.status_code not in (204, 404):
            logger.warning(
                "Drive channels.stop returned HTTP %d for channel=%s: %s",
                resp.status_code,
                channel_id,
                resp.text[:200],
            )

        with tenant_connection(tenant_id) as conn:
            conn.execute(
                "DELETE FROM tenant_drive_channels WHERE tenant_id = %s AND channel_id = %s",
                (str(tenant_id), channel_id),
            )

    def renew_drive_push_channel(
        self, channel_row: dict[str, Any]
    ) -> dict[str, Any]:
        """Renew an expiring channel.

        Atomic-ish: register the new channel FIRST so there's no gap
        in notification coverage, then stop the old one. If register
        fails, the old channel stays active and the caller can retry.
        """
        tenant_id = UUID(str(channel_row["tenant_id"]))
        spreadsheet_id = str(channel_row["resource_id"])
        old_channel_id = str(channel_row["channel_id"])
        old_resource_id = str(channel_row["resource_id"])

        new = self.register_drive_push_channel(tenant_id, spreadsheet_id)
        try:
            self.unregister_drive_push_channel(tenant_id, old_channel_id, old_resource_id)
        except Exception:  # noqa: BLE001 — never block renewal on stop failure
            logger.exception(
                "Drive channel renewal: stopping old channel %s failed; "
                "new channel %s already active",
                old_channel_id,
                new["channel_id"],
            )
        return new


__all__ = [
    "CanonicalRow",
    "GoogleSheetConnector",
    "SamplePayload",
]
