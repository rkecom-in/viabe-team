"""VT-86 — monthly report PDF storage (Supabase Storage, D2).

Stores rendered report PDFs in a tenant-scoped path with 12-month retention,
so the owner portal (VT-9.7) can re-download. Supabase Storage is the chosen
backend (D2 — supabase is already a dep; no new vendor). CL-422: dev holds
SYNTHETIC tenants only until prod-in-Mumbai (VT-231).

The path builder is pure + tested. The upload is a thin wrapper over an
injectable storage client (tests pass a mock; the canary does a real upload),
so dev machines without Supabase creds still exercise everything but the live
PUT.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

REPORTS_BUCKET = os.environ.get("MONTHLY_REPORTS_BUCKET", "monthly-reports")


class _StorageClient(Protocol):
    """Minimal shape we need from a Supabase Storage client (or a test mock)."""

    def upload(self, path: str, file: bytes, file_options: dict[str, Any]) -> Any: ...


def report_storage_path(tenant_id: str, year_month: str) -> str:
    """Tenant-scoped object path for a month's report. Pure + deterministic.

    `monthly-reports/{tenant_id}/{year_month}.pdf` — tenant_id as the leading
    path segment keeps one tenant's reports grouped + namespaced (the bucket is
    private; access is mediated server-side, never a public URL)."""
    return f"{tenant_id}/{year_month}.pdf"


def _supabase_storage(bucket: str) -> Any:
    """Build a Supabase Storage bucket client from env. Lazy — only the real
    upload path imports/needs it, so dev without creds still loads the module.

    Requires SUPABASE_URL + SUPABASE_SERVICE_KEY (service role — server-side
    only; never the anon key)."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise RuntimeError(
            "store_report_pdf: SUPABASE_URL / SUPABASE_SERVICE_KEY not set "
            "(service-role key required for server-side report storage)"
        )
    from supabase import create_client  # lazy

    return create_client(url, key).storage.from_(bucket)


def store_report_pdf(
    tenant_id: str,
    year_month: str,
    pdf_bytes: bytes,
    *,
    client: _StorageClient | None = None,
    bucket: str = REPORTS_BUCKET,
) -> str:
    """Upload the report PDF to tenant-scoped storage; return the object path.

    `client` is injectable for tests/canary; production resolves the real
    Supabase bucket from env. Upsert semantics (a re-run for the same month
    overwrites) keep the (tenant, year_month) report idempotent — matching the
    monthly_reports UNIQUE constraint.
    """
    path = report_storage_path(tenant_id, year_month)
    storage = client if client is not None else _supabase_storage(bucket)
    storage.upload(
        path,
        pdf_bytes,
        {"content-type": "application/pdf", "upsert": "true"},
    )
    return path


__all__ = ["REPORTS_BUCKET", "report_storage_path", "store_report_pdf"]
