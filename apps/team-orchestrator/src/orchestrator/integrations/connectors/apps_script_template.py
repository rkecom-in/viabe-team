"""VT-207 Apps Script template generator.

DEPRECATED per CL-421 (2026-05-29). The Apps Script paste flow is
customer-hostile (target persona = SMB owner, not developer). VT-222
replaces this substrate with Drive Push Notifications
(``GoogleSheetConnector.register_drive_push_channel``) primary +
10-minute polling fallback. New onboarding flows must NOT generate or
relay Apps Script bodies.

Kept for backward compatibility — existing Apps-Script-onboarded
tenants continue to push via the legacy webhook while they migrate.
Do not extend. Do not reference from new prompts
(``integration_agent_system.md``) or onboarding flows
(``/team/onboard`` page).

Owner pastes the rendered script into their Sheet's Extensions →
Apps Script panel. The ``onEdit`` trigger POSTs changed rows to the
orchestrator's ``/api/orchestrator/integrations/sheet/push`` endpoint
with HMAC-signed headers.

Per Q4 Option A locked: per-(tenant, connector) push_secret stored on
``tenant_oauth_tokens.push_secret``; multi-Sheet-per-tenant requires
follow-up VT-N (Phase-1 single instance assumption documented in
migration 033 header).
"""

from __future__ import annotations


_APPS_SCRIPT_TEMPLATE = """\
// Viabe Team — auto-generated push script for tenant {tenant_id}
// Spreadsheet: {spreadsheet_id}
// DO NOT modify the orchestrator_url or push_secret below.

const ORCH_URL = "{orchestrator_base}/api/orchestrator/integrations/sheet/push";
const TENANT_ID = "{tenant_id}";
const SPREADSHEET_ID = "{spreadsheet_id}";
const PUSH_SECRET = "{push_secret}";

function onEdit(e) {{
  try {{
    const sheet = e.source.getActiveSheet();
    const row = e.range.getRow();
    if (row < 2) return;  // skip header row
    const lastCol = sheet.getLastColumn();
    const rowValues = sheet.getRange(row, 1, 1, lastCol).getValues()[0];
    const headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
    const payload = {{
      tenant_id: TENANT_ID,
      spreadsheet_id: SPREADSHEET_ID,
      row_index: row,
      row_data: Object.fromEntries(headers.map((h, i) => [h, rowValues[i]])),
    }};
    const body = JSON.stringify(payload);
    const signature = Utilities.computeHmacSha256Signature(body, PUSH_SECRET)
      .map((b) => ("0" + (b < 0 ? b + 256 : b).toString(16)).slice(-2))
      .join("");
    UrlFetchApp.fetch(ORCH_URL, {{
      method: "post",
      contentType: "application/json",
      headers: {{ "X-Viabe-Signature": signature, "X-Viabe-Tenant": TENANT_ID }},
      payload: body,
      muteHttpExceptions: true,
    }});
  }} catch (err) {{
    console.error("Viabe push failed:", err);
  }}
}}
"""


def render_apps_script(
    *,
    tenant_id: str,
    spreadsheet_id: str,
    orchestrator_base: str,
    push_secret: str,
) -> str:
    """Render the Apps Script for one (tenant, spreadsheet) pair."""
    return _APPS_SCRIPT_TEMPLATE.format(
        tenant_id=tenant_id,
        spreadsheet_id=spreadsheet_id,
        orchestrator_base=orchestrator_base,
        push_secret=push_secret,
    )


def verify_push_signature(*, body: bytes, signature: str, push_secret: str) -> bool:
    """Verify the HMAC-SHA256 signature on an incoming push POST.

    Body is the raw request bytes; signature is the lowercase hex
    digest. Constant-time compare via ``hmac.compare_digest``.
    """
    import hashlib
    import hmac

    expected = hmac.new(
        push_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


__all__ = ["render_apps_script", "verify_push_signature"]
