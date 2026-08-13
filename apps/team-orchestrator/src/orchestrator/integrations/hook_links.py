"""VT-288 — durable hook→WhatsApp attribution links.

Email/SMS hooks carry a short tokenised link we own (`/r/<token>`). The redirect resolves
token→tenant SERVER-SIDE, records the click, and 302s to the tenant's live WABA wa.me —
attribution is the server-side mapping + click record, NOT the user-editable `wa.me?text=`
payload (Cowork VT-288 gotcha).

Storage: `hook_links` (migration 071) is service-role-only (deny-all RLS) — the redirect
is public, has no tenant GUC, and resolves BY token. The bare service pool is the sole
access path; the token IS the capability. No PII (CL-390).

VT-741 — CUSTOMER-ATTRIBUTED CLICKS, ON THIS SAME SCHEME
---------------------------------------------------------
Fazal's recency tiers need a CLICK signal per CUSTOMER ("replied or clicked within 30
days" -> 24h; "read/clicked/replied within 90 days" -> 3 days). `hook_links` records a
click, but only against a TENANT — there is no customer on the row, so a click could never
be attributed to the recipient whose interval we are resolving.

This does NOT add a second link scheme. The public surface stays exactly `/r/<token>`,
served by the one redirect, and every token still lives in `hook_links`. A customer-bound
link is a `hook_links` row AND a `customer_hook_links` row (migration 201) sharing one
token; an unbound tenant-wide email/SMS hook stays a `hook_links` row alone, byte-identical
to before. `mint_customer_hook_link` is the only new mint; `resolve_and_record_click` now
stamps the binding too, on the same connection.

The token remains the sole capability. No customer id, tenant id, campaign id or phone
appears in the URL — the redirect resolves token -> tenant -> (optionally) customer entirely
server-side, exactly as VT-288 already did for the tenant.

`customer_hook_links` is a DIFFERENT PRIVACY CLASS from this table: it links a token to an
identified end customer and records their behaviour, i.e. subject data, where `hook_links`
is deny-all and PII-free by design. It therefore carries its OWN tenant-scoped RLS + FORCE
(mirroring agent_customer_contacts) rather than inheriting deny-all, and it is registered in
`dsr_purge._PURGE_ORDER` — DSR anonymizes the tenants row, so no FK cascade would ever clean
it (the episodic_events / L2 omission, not repeated a third time).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)

#: The customer-attributed binding table (VT-741, migration 201). Mirrored as a literal in
#: ``agents/send_frequency.py`` (which must stay import-light for the dep-less suite) and pinned
#: against both the migration and this module by a static test.
CUSTOMER_CLICK_TABLE = "customer_hook_links"


@dataclass(frozen=True, slots=True)
class HookResolution:
    tenant_id: UUID
    wa_number: str   # the tenant's live WABA number (E.164)
    source: str | None
    #: VT-741: set only when the token is customer-bound. ``None`` for an ordinary tenant-wide
    #: hook — resolved SERVER-SIDE from the token, never carried in the URL. Defaulted so every
    #: existing construction/consumer of this dataclass is unaffected.
    customer_id: UUID | None = None


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


def mint_customer_hook_link(
    tenant_id: UUID | str, customer_id: UUID | str, *, source: str | None = None
) -> str:
    """Mint a `/r/<token>` link BOUND to one customer. Returns the same opaque token shape.

    Two rows, one transaction: the `hook_links` row (the token, unchanged — this is not a second
    link scheme) and the `customer_hook_links` binding that lets a later click be attributed to
    this recipient. The transaction matters: a `hook_links` row without its binding would be an
    invisible attribution hole (the link works, the click records against the tenant, and the
    customer's frequency tier silently never sees it), which is exactly the failure mode that made
    click tracking absent for customer messages in the first place.

    Cross-tenant binding is not guarded here in Python — migration 201's composite FKs to
    `hook_links (tenant_id, token)` and `customers (tenant_id, id)` make it physically impossible,
    so a mismatched `customer_id` raises rather than writing a wrong row.
    """
    token = secrets.token_urlsafe(16)
    with get_pool().connection() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO hook_links (token, tenant_id, source) VALUES (%s, %s, %s)",
            (token, str(tenant_id), source),
        )
        conn.execute(
            f"INSERT INTO {CUSTOMER_CLICK_TABLE} (tenant_id, customer_id, token, source) "  # noqa: S608 — module constant
            "VALUES (%s, %s, %s, %s)",
            (str(tenant_id), str(customer_id), token, source),
        )
    logger.info(
        "customer hook_link minted tenant=%s customer=%s source=%s",
        tenant_id, customer_id, source,
    )
    return token


def _record_customer_click(cur: Any, token: str, tenant_id: Any) -> UUID | None:
    """Stamp the customer binding for this token, if there is one. Returns the customer id.

    FAIL-SOFT BY DESIGN, and the direction is deliberate. This runs inside the PUBLIC redirect: a
    customer tapped a link and is waiting for a WhatsApp handoff. Attribution is the secondary
    concern — a broken click table must never turn into a 500 on the customer's screen. Losing a
    click costs the customer's frequency tier one rung of engagement, which resolves toward Tier C,
    i.e. MORE suppression. The opposite trade (blocking the redirect to protect a metric) would be
    the wrong one.

    The `tenant_id` predicate is not decoration: it pins the binding to the tenant the token
    already resolved to, so a binding row can never be stamped across tenants even if the composite
    FK were ever dropped.
    """
    try:
        cur.execute(
            f"UPDATE {CUSTOMER_CLICK_TABLE} "  # noqa: S608 — module constant, never user input
            "   SET click_count = click_count + 1, "
            "       last_clicked_at = now(), "
            "       first_clicked_at = COALESCE(first_clicked_at, now()) "
            " WHERE token = %s AND tenant_id = %s "
            "RETURNING customer_id",
            (token, str(tenant_id)),
        )
        bound = cur.fetchone()
    except Exception:  # noqa: BLE001 — see the docstring; never break the customer's redirect
        logger.warning(
            "VT-741 customer click record FAILED (fail-soft, redirect continues) tenant=%s",
            tenant_id, exc_info=True,
        )
        return None
    if bound is None:
        return None  # an ordinary tenant-wide hook, not customer-bound
    raw = bound["customer_id"] if isinstance(bound, dict) else bound[0]
    return UUID(str(raw))


def resolve_and_record_click(token: str) -> HookResolution | None:
    """Resolve a hook token → (tenant, live WABA number, source, customer?), recording the click.

    Returns None for an unknown token OR a tenant without a `live` WABA (can't redirect
    to a number that can't receive). Atomic click increment in the same statement.

    VT-741: when the token is customer-bound, the per-customer click is stamped too. The customer
    is resolved from the TOKEN, server-side — the URL carries no customer, tenant, campaign or
    phone, so the attribution cannot be forged or enumerated by editing the link.
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
        # VT-741: stamped BEFORE the WABA lookup on purpose. A tenant whose WABA is not live
        # still had a real click from a real customer, and that engagement is true whether or not
        # we can complete the redirect — dropping it would understate the tier for a customer who
        # demonstrably engaged.
        customer_id = _record_customer_click(cur, token, tenant_id)
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
        customer_id=customer_id,
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
    """DSR helper (service pool): delete a tenant's hook links.

    NOTE (VT-741 finding, NOT fixed here): nothing calls this. `hook_links` is absent from
    `dsr_purge._PURGE_ORDER`, so this helper is dead code and a tenant's hook links survive a
    right-to-erasure purge. The customer-attributed table IS purged (it is registered in
    `_PURGE_ORDER`), so no customer linkage survives — but the tenant-level rows do. Raised
    separately; fixing it belongs with the `customers` gap in the same DSR sweep.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM hook_links WHERE tenant_id = %s", (str(tenant_id),))
        return cur.rowcount


__all__ = [
    "CUSTOMER_CLICK_TABLE",
    "HookResolution",
    "mint_customer_hook_link",
    "mint_hook_link",
    "resolve_and_record_click",
    "wa_me_url",
]
