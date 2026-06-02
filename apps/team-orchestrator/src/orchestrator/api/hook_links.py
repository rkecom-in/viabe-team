"""VT-288 — hook-link router.

  GET /r/{token}
      PUBLIC redirect (no auth — the token IS the capability). Resolves token→tenant
      server-side, records the click, and 302s to the tenant's live WABA wa.me. 404 on an
      unknown token or a tenant without a live WABA. This is the durable-attribution entry:
      the click is recorded server-side, independent of the editable wa.me text.

  POST /api/orchestrator/hooks/mint   (body: tenant_id, source?)
      INTERNAL_API_SECRET-guarded (team-web / campaign layer, owner-authenticated). Mints a
      hook-link token + returns the public `/r/<token>` URL to embed in an email/SMS hook.
"""

from __future__ import annotations

import hmac
import logging
import os
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Path
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from orchestrator.integrations.hook_channels import build_hook_url
from orchestrator.integrations.hook_links import (
    mint_hook_link,
    resolve_and_record_click,
    wa_me_url,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


class HookMintBody(BaseModel):
    tenant_id: str
    source: str | None = None


class HookMintResponse(BaseModel):
    token: str
    url: str


@router.post("/api/orchestrator/hooks/mint")
def hooks_mint(
    body: HookMintBody,
    x_internal_secret: str | None = Header(default=None),
) -> HookMintResponse:
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        tenant_uuid = UUID(body.tenant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid tenant_id") from None
    token = mint_hook_link(tenant_uuid, source=body.source)
    return HookMintResponse(token=token, url=build_hook_url(token))


@router.get("/r/{token}")
def hook_redirect(token: str = Path(...)) -> RedirectResponse:
    """Public hook redirect → the tenant's live WABA wa.me. Click recorded server-side."""
    resolved = resolve_and_record_click(token)
    if resolved is None:
        raise HTTPException(status_code=404, detail="link not found")
    return RedirectResponse(url=wa_me_url(resolved.wa_number), status_code=302)
