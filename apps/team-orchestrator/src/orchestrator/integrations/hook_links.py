"""VT-288 — durable hook→WhatsApp attribution links.

Email/SMS hooks carry a short tokenised link we own (`/r/<token>`). The redirect resolves
token→tenant SERVER-SIDE, records the click, and 302s to the tenant's live WABA wa.me —
attribution is the server-side mapping + click record, NOT the user-editable `wa.me?text=`
payload (Cowork VT-288 gotcha).

Storage: `hook_links` (migration 071) is service-role-only (deny-all RLS) — the redirect
is public, has no tenant GUC, and resolves BY token. The bare service pool is the sole
access path; the token IS the capability. No PII (CL-390).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HookResolution:
    tenant_id: UUID
    wa_number: str   # the tenant's live WABA number (E.164)
    source: str | None


def mint_hook_link(tenant_id: UUID | str, *, source: str | None = None) -> str:
    """Mint + persist a hook link token for a tenant. Returns the opaque token to embed
    in the public `/r/<token>` URL."""
    token = secrets.token_urlsafe(16)
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO hook_links (token, tenant_id, source) VALUES (%s, %s, %s)",
            (token, str(tenant_id), source),
        )
    logger.info("hook_link minted tenant=%s source=%s", tenant_id, source)
    return token


def resolve_and_record_click(token: str) -> HookResolution | None:
    """Resolve a hook token → (tenant, live WABA number, source), recording the click.

    Returns None for an unknown token OR a tenant without a `live` WABA (can't redirect
    to a number that can't receive). Atomic click increment in the same statement.
    """
    if not token:
        return None
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE hook_links
               SET click_count = click_count + 1, last_clicked_at = now()
             WHERE token = %s
            RETURNING tenant_id, source
            """,
            (token,),
        )
        row = cur.fetchone()
        if row is None:
            logger.info("hook_link resolve: unknown token")
            return None
        tenant_id = row["tenant_id"] if isinstance(row, dict) else row[0]
        source = row["source"] if isinstance(row, dict) else row[1]
        # the tenant's live WABA number — only redirect to a number that can receive.
        cur.execute(
            "SELECT phone_number FROM tenant_whatsapp_accounts "
            "WHERE tenant_id = %s AND status = 'live' AND phone_number IS NOT NULL",
            (str(tenant_id),),
        )
        wa = cur.fetchone()
    if wa is None:
        logger.info("hook_link resolve: tenant %s has no live WABA", tenant_id)
        return None
    wa_number = wa["phone_number"] if isinstance(wa, dict) else wa[0]
    return HookResolution(
        tenant_id=UUID(str(tenant_id)),
        wa_number=str(wa_number),
        source=source if source is None else str(source),
    )


def wa_me_url(wa_number: str, *, prefill: str | None = None) -> str:
    """Build a wa.me deep link. The prefill text is a CONVENIENCE only — attribution does
    NOT depend on it (it's recorded server-side at click time). E.164 without the '+'."""
    digits = wa_number.lstrip("+")
    base = f"https://wa.me/{digits}"
    if prefill:
        from urllib.parse import quote

        return f"{base}?text={quote(prefill)}"
    return base


def _purge_tenant_hook_links(conn: Any, tenant_id: UUID) -> int:
    """DSR helper (service pool): delete a tenant's hook links."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM hook_links WHERE tenant_id = %s", (str(tenant_id),))
        return cur.rowcount


__all__ = ["HookResolution", "mint_hook_link", "resolve_and_record_click", "wa_me_url"]
