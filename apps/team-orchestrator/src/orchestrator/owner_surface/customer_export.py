"""VT-676 (CD2) — the customer-list CSV export, delivered to the VERIFIED OWNER on WhatsApp.

Owner asks "send me my customer list" → build a CSV of THEIR OWN customers (name / phone / status /
total spend), store it in a private tenant-scoped bucket, mint a SHORT-TTL signed URL, and send it
as a WhatsApp media attachment to the OTP-verified owner number. This is the FIRST path where RAW
customer name+phone deliberately leaves the orchestrator boundary — every rail below is binding:

  - RECIPIENT is SERVER-DERIVED ONLY: ``tenants.owner_phone`` (OTP-verified at onboarding) falling
    back to ``whatsapp_number`` — the same ``_resolve_owner_phone`` contract task_outcome /
    campaign_outcome / request_owner_approval use. NEVER a number from the message body.
  - The data is the owner's OWN tenant's customers (RLS-scoped ``CustomersWrapper`` read).
  - PRIVATE bucket + SHORT-TTL (300s) signed URL (the ``report_storage`` VT-86/341 pattern — a
    leaked signed URL is a PII document; keep the window tiny). The signed URL is NEVER logged.
  - tm_audit ``customer_list_exported`` records the EGRESS — row count + object path + message sid
    only, never content, never the URL (the ``dsr_export`` discipline: audit shape, not payload).
  - FAIL-SOFT: any failure returns False and the caller falls back to the honest
    ``LIST_SEND_ACK_PREAMBLE`` — the owner never gets silence, and a storage/transport blip never
    breaks the campaign dispatch this ride-along shares a turn with (VT-642).
  - Dev safety: the media send rides ``send_freeform_message`` → ``TEAM_TWILIO_MOCK_MODE`` /
    dev_send_guard exactly like every other owner send (mocked on dev unless allowlisted).
"""

from __future__ import annotations

import csv
import io
import logging
import os
from datetime import UTC, datetime
from uuid import UUID

from orchestrator.owner_surface.report_storage import _StorageClient, _supabase_storage

logger = logging.getLogger("orchestrator.owner_surface.customer_export")

# TEAM_-namespaced (cross-product env lint). Private bucket — access is mediated server-side only.
EXPORT_BUCKET = os.environ.get("TEAM_CUSTOMER_EXPORT_BUCKET", "customer-exports")

# A leaked signed URL replays for this window — keep it SHORT (a PII document). Same 300s as the
# monthly-report URL (VT-341, Cowork req).
_SIGNED_URL_TTL_SECONDS = 300

# Paged read size — the wrapper read is paginated; exports iterate to exhaustion.
_PAGE_SIZE = 500

#: The media message body (the caption WhatsApp shows with the file). Number-free; the file itself
#: carries the data. Fix-4c: no "download link expires" talk — the 300s signed-URL TTL only gates
#: TWILIO's fetch; once the document is attached, WhatsApp serves its own copy and expiry never
#: touches the owner. The old copy confused the live canary ("link inside" with no visible link).
CUSTOMER_LIST_CAPTION = (
    "Here's your customer list — names, numbers, status and total purchases, "
    "with lapsed customers flagged. The file is yours to keep."
)


def export_storage_path(tenant_id: str) -> str:
    """Tenant-scoped object path. Pure + deterministic per (tenant, day) — a same-day re-ask
    overwrites (upsert) instead of piling PII copies in the bucket.

    Fix-4g (canary-1 r1–r3, 2026-07-18): the file is a PDF. Three live attempts proved the
    Twilio WhatsApp channel drops non-PDF documents at Meta AFTER a successful create (MM sid
    minted, nothing delivered): text/csv failed r1, text/plain failed r2/r3. Twilio's supported
    WhatsApp media list is images/audio/video/vcard/**PDF** — PDF is the only document type it
    reliably delivers. Rendered via the SAME weasyprint path the monthly report uses."""
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"{tenant_id}/customers-{day}.pdf"


def build_customer_list_csv(tenant_id: UUID | str) -> tuple[bytes, int]:
    """The owner's full customer list as CSV bytes + the row count.

    Columns: name, phone, status, total_spend_inr, last_purchase, lapsed — the portal fields
    (display_name / phone_e164 RAW / opt_out_status / spend_paise) PLUS the recency the R7
    lapsed-list ask needs: ``lapsed`` is computed by the wrapper with the SAME canonical
    ``count_lapsed`` definition (had a sale, none within ``LAPSED_WINDOW_DAYS``), so the flag in
    the file never diverges from the count the owner hears in chat. Spend is paise→INR verbatim
    (no derived/estimated numbers). RLS-scoped read; pages to exhaustion.
    """
    from orchestrator.db.wrappers import LAPSED_WINDOW_DAYS, CustomersWrapper

    customers = CustomersWrapper()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "phone", "status", "total_spend_inr", "last_purchase", "lapsed"])
    count = 0
    offset = 0
    while True:
        page = customers.list_customers_for_export(
            tenant_id, lapsed_days=LAPSED_WINDOW_DAYS, limit=_PAGE_SIZE, offset=offset
        )
        if not page:
            break
        for row in page:
            spend_inr = int(row.get("spend_paise") or 0) / 100
            last_sale = row.get("last_sale_date")
            writer.writerow(
                [
                    row.get("display_name") or "",
                    row.get("phone_e164") or "",
                    row.get("opt_out_status") or "",
                    f"{spend_inr:.2f}",
                    str(last_sale) if last_sale else "",
                    "yes" if row.get("lapsed") else "no",
                ]
            )
            count += 1
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return buf.getvalue().encode("utf-8"), count


def render_customer_list_pdf(tenant_id: UUID | str) -> tuple[bytes, int]:
    """The owner's customer list as PDF bytes + row count (fix-4g — the ONLY document type the
    Twilio WhatsApp channel reliably delivers). Same paged RLS read + columns as the CSV builder;
    rendered as one compact table via the SAME weasyprint path the monthly report uses (lazy
    import — system cairo/pango libs live in the orchestrator Docker image)."""
    from html import escape

    from orchestrator.db.wrappers import LAPSED_WINDOW_DAYS, CustomersWrapper

    customers = CustomersWrapper()
    rows_html: list[str] = []
    count = 0
    offset = 0
    while True:
        page = customers.list_customers_for_export(
            tenant_id, lapsed_days=LAPSED_WINDOW_DAYS, limit=_PAGE_SIZE, offset=offset
        )
        if not page:
            break
        for row in page:
            spend_inr = int(row.get("spend_paise") or 0) / 100
            last_sale = row.get("last_sale_date")
            lapsed = bool(row.get("lapsed"))
            rows_html.append(
                "<tr{cls}><td>{name}</td><td>{phone}</td><td>{status}</td>"
                "<td class='num'>₹{spend:,.2f}</td><td>{last}</td><td>{flag}</td></tr>".format(
                    cls=" class='lapsed'" if lapsed else "",
                    name=escape(str(row.get("display_name") or "")),
                    phone=escape(str(row.get("phone_e164") or "")),
                    status=escape(str(row.get("opt_out_status") or "")),
                    spend=spend_inr,
                    last=escape(str(last_sale) if last_sale else "—"),
                    flag="lapsed" if lapsed else "",
                )
            )
            count += 1
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    if count == 0:
        return b"", 0

    day = datetime.now(UTC).strftime("%d %b %Y")
    html = (
        "<style>"
        "body{font-family:sans-serif;font-size:9pt;margin:24px}"
        "h1{font-size:13pt;margin-bottom:2px}"
        "p.meta{color:#555;margin-top:0}"
        "table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #ccc;padding:4px 6px;text-align:left}"
        "th{background:#f0f0f0}"
        "td.num{text-align:right}"
        "tr.lapsed td{background:#fff4e5}"
        "</style>"
        f"<h1>Customer list</h1><p class='meta'>{count} customers · {day} · "
        f"lapsed = no purchase in {int(LAPSED_WINDOW_DAYS)} days (highlighted)</p>"
        "<table><tr><th>Name</th><th>Phone</th><th>Status</th><th>Total purchases</th>"
        "<th>Last purchase</th><th></th></tr>"
        + "".join(rows_html)
        + "</table>"
    )
    from weasyprint import HTML  # lazy: system-dep, not importable everywhere

    return HTML(string=html).write_pdf(), count


def store_customer_export(
    tenant_id: str,
    pdf_bytes: bytes,
    *,
    client: _StorageClient | None = None,
    bucket: str = EXPORT_BUCKET,
) -> str:
    """Upload the export PDF to the private bucket; return the object path. Upsert (same-day
    re-ask overwrites). content-type application/pdf — the one document type the Twilio WhatsApp
    channel delivers (fix-4g; text/csv and text/plain both died async at Meta, r1–r3)."""
    path = export_storage_path(tenant_id)
    storage = client if client is not None else _supabase_storage(bucket)
    storage.upload(path, pdf_bytes, {"content-type": "application/pdf", "upsert": "true"})
    return path


# Back-compat alias (tests + any external caller); same behavior, PDF-era name preferred.
store_customer_csv = store_customer_export


def export_signed_url(
    path: str,
    *,
    ttl_seconds: int = _SIGNED_URL_TTL_SECONDS,
    client: _StorageClient | None = None,
    bucket: str = EXPORT_BUCKET,
) -> str | None:
    """SHORT-TTL signed URL for a stored export. None on any storage error (caller falls back).
    The returned URL is a PII document handle — callers MUST NOT log it."""
    storage = client if client is not None else _supabase_storage(bucket)
    try:
        result = storage.create_signed_url(path, ttl_seconds)
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    return result.get("signedURL") or result.get("signedUrl") or result.get("signed_url")


def _resolve_owner_phone(tenant_id: UUID | str) -> str | None:
    """Owner recipient: ``tenants.owner_phone`` (OTP-verified) falling back to ``whatsapp_number``.
    Mirrors task_outcome/campaign_outcome/request_owner_approval verbatim. Best-effort None."""
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT owner_phone, whatsapp_number FROM tenants WHERE id = %s",
                (str(tenant_id),),
            ).fetchone()
    except Exception:
        logger.exception("VT-676 customer-export: owner-phone resolve failed tenant=%s", tenant_id)
        return None
    if row is None:
        return None
    row = dict(row)
    phone = row.get("owner_phone") or row.get("whatsapp_number")
    return str(phone) if phone else None


def send_customer_list_to_owner(
    tenant_id: UUID | str,
    *,
    storage_client: _StorageClient | None = None,
) -> bool:
    """The whole VT-676 delivery: build → store → sign → media-send to the verified owner → audit.

    Returns True iff the media send succeeded (the caller suppresses its fallback ack). ANY failure
    (empty list, storage, URL, transport) returns False — the caller rides the honest
    ``LIST_SEND_ACK_PREAMBLE`` fallback instead. Never raises.
    """
    try:
        # Fix-4g: PDF, not CSV — the only Twilio-WhatsApp-deliverable document type (r1–r3).
        pdf_bytes, row_count = render_customer_list_pdf(tenant_id)
        if row_count == 0:
            # Nothing to export — not an error, but nothing honest to attach either.
            return False

        recipient = _resolve_owner_phone(tenant_id)
        if not recipient:
            logger.warning("VT-676 customer-export: no verified owner phone tenant=%s", tenant_id)
            return False

        path = store_customer_export(str(tenant_id), pdf_bytes, client=storage_client)
        url = export_signed_url(path, client=storage_client)
        if not url:
            logger.warning("VT-676 customer-export: signed-URL mint failed tenant=%s", tenant_id)
            return False

        # The media send rides the standard owner freeform funnel (mock-mode + dev_send_guard +
        # conversation-log recording all apply). The signed URL travels ONLY as media_url — never
        # in the body, never in a log line.
        from orchestrator.utils.twilio_send import send_freeform_message

        # Fix-4d (canary-1 r2): surface MUST be a conversation_log-legal value — the CHECK
        # constraint allows only journey|manager|system, so "customer_export" made the caption's
        # transcript insert fail SILENTLY (best-effort) and the follow-up ack's "file just above"
        # pointed at a message conversation_log never saw.
        sid = send_freeform_message(
            CUSTOMER_LIST_CAPTION,
            recipient,
            tenant_id=tenant_id,
            surface="manager",
            media_urls=[url],
        )

        # VT-676 fix-4b (live canary 2026-07-18): register this send with the owner-notification
        # ledger so the async Twilio status callback RECONCILES it — a failed/undelivered media
        # message flips the row + opens the internal incident (owner_notification.record_owner_
        # notification_delivery), instead of a success-claiming text with no file being invisible
        # forever. This was the ONE owner-send path not wired in (campaign/task/owner_send are).
        from orchestrator.owner_surface.owner_notification import record_owner_notification

        record_owner_notification(tenant_id, "customer_list_export", sid)

        from orchestrator.observability.tm_audit import emit_tm_audit

        emit_tm_audit(
            event_layer="does", event_kind="customer_list_exported",
            actor="team_manager", tenant_id=tenant_id,
            summary="customer list exported + sent to the verified owner as a WhatsApp CSV "
            "attachment (VT-676)",
            decision={"rows": row_count, "object_path": path, "message_sid": sid},
        )
        logger.info(
            "VT-676 customer-export: delivered tenant=%s rows=%d sid=%s", tenant_id, row_count, sid
        )
        return True
    except Exception:  # noqa: BLE001 — fail-soft: the caller falls back to the honest ack
        logger.exception("VT-676 customer-export: delivery failed (fallback ack) tenant=%s", tenant_id)
        return False


__all__ = [
    "CUSTOMER_LIST_CAPTION",
    "EXPORT_BUCKET",
    "build_customer_list_csv",
    "export_signed_url",
    "export_storage_path",
    "render_customer_list_pdf",
    "send_customer_list_to_owner",
    "store_customer_csv",
    "store_customer_export",
]
