"""VT-72 — typed tenant-scoped table wrappers (Phase-1 hot tables).

One thin wrapper per LIVE tenant-scoped hot table. Each inherits the tenant
predicate + result validation from ``TenantScopedTable``. NEW code should read/
write these tables through these wrappers (the `no-direct-tenant-db-access` lint
gates regressions); existing sites are allowlisted pending the VT-306 migration.

Unbuilt-table wrappers (EpisodicEvents / KGEventsProcessed / CompositionAudits /
l3_patterns / l4_documents) are DEFERRED to VT-306 — wrapping unbuilt tables is
exactly the stale thing the no-stale bar forbids.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from orchestrator.db.base import TenantScopedTable

# VT-306: each wrapper inherits the generic tenant-predicated CRUD
# (find_by_id / list_for_tenant / insert / delete, all conn-optional) from
# TenantScopedTable and adds the table-specific typed queries the migrated
# call sites need. Every method is tenant-predicated + result-validated; writes
# take an optional ``conn`` so they can be ATOMIC with a sibling write (e.g. the
# VT-65 PR-2 customers-write + kg_emit in one txn).

# tenant_id is SELECTed so _validate (assert_tenant_scoped) can confirm scope —
# every wrapper read that validates MUST return tenant_id.
_CUSTOMER_DEDUP_COLS = "id, tenant_id, display_name, phone_e164, email, acquired_via"


class CustomersWrapper(TenantScopedTable):
    _table = "customers"

    def find_by_phone(
        self, tenant_id: UUID | str, phone_e164: str, *, conn: Any = None
    ) -> list[dict[str, Any]]:
        """Dedup lookup by phone (may return multiple candidates)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                f"SELECT {_CUSTOMER_DEDUP_COLS} FROM customers "  # noqa: S608 — static cols
                "WHERE tenant_id = %s AND phone_e164 = %s",
                (str(tid), phone_e164),
            ).fetchall()
        out = [dict(r) for r in rows]
        self._validate(out, tid)
        return out

    def find_by_email(
        self, tenant_id: UUID | str, email: str, *, conn: Any = None
    ) -> list[dict[str, Any]]:
        """Dedup lookup by email (may return multiple candidates)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                f"SELECT {_CUSTOMER_DEDUP_COLS} FROM customers "  # noqa: S608 — static cols
                "WHERE tenant_id = %s AND email = %s",
                (str(tid), email),
            ).fetchall()
        out = [dict(r) for r in rows]
        self._validate(out, tid)
        return out

    def update_on_merge(
        self,
        tenant_id: UUID | str,
        customer_id: UUID | str,
        *,
        display_name: Any,
        email: Any,
        phone_e164: Any,
        acquired_via: Any,
        updated_at: datetime,
        conn: Any = None,
    ) -> None:
        """Dedup merge: overwrite the canonical fields on an existing customer.
        Tenant-predicated (never a bare ``WHERE id``) so a stray id can't cross
        tenants even under a caller-owned conn."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            c.execute(
                "UPDATE customers SET display_name = %s, email = %s, "
                "phone_e164 = %s, acquired_via = %s, updated_at = %s "
                "WHERE tenant_id = %s AND id = %s",
                (display_name, email, phone_e164, acquired_via, updated_at,
                 str(tid), str(customer_id)),
            )

    def list_recipients_for_campaign(
        self, tenant_id: UUID | str, campaign_id: str, *, conn: Any = None
    ) -> list[dict[str, Any]]:
        """Campaign recipients joined to their customer status flags (customer_id,
        opt_out_status, complaint_status), oldest first. CL-390: status flags only,
        never phone/name. The join is tenant-matched on both sides."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                """
                SELECT cr.customer_id::text AS customer_id,
                       c.opt_out_status,
                       c.complaint_status
                FROM campaign_recipients cr
                JOIN customers c
                  ON c.id = cr.customer_id AND c.tenant_id = cr.tenant_id
                WHERE cr.campaign_id = %s AND cr.tenant_id = %s
                ORDER BY cr.added_at
                """,
                (campaign_id, str(tid)),
            ).fetchall()
        return [dict(r) for r in rows]

    def filter_existing_ids(
        self, tenant_id: UUID | str, ids: list[str], *, conn: Any = None
    ) -> set[str]:
        """Return the subset of ``ids`` that exist for this tenant (cohort
        validate). Pass the caller's conn to stay atomic with a sibling write
        (e.g. the campaign_recipients INSERT)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                "SELECT id::text AS id FROM customers "
                "WHERE tenant_id = %s AND id = ANY(%s::uuid[])",
                (str(tid), list(ids)),
            ).fetchall()
        return {dict(r)["id"] for r in rows}

    def count_created_in_range(
        self,
        tenant_id: UUID | str,
        start: datetime,
        end: datetime,
        *,
        conn: Any = None,
    ) -> int:
        """COUNT customers created in [start, end) — the monthly-report metric."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT count(*) AS n FROM customers "
                "WHERE tenant_id = %s AND created_at >= %s AND created_at < %s",
                (str(tid), start, end),
            ).fetchone()
        return int(dict(row)["n"]) if row else 0

    def list_display_names(
        self, tenant_id: UUID | str, *, conn: Any = None
    ) -> set[str]:
        """Case-folded set of non-null display_names (PII redactor name cache)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                "SELECT display_name FROM customers "
                "WHERE tenant_id = %s AND display_name IS NOT NULL",
                (str(tid),),
            ).fetchall()
        return {
            str(dict(r)["display_name"]).casefold()
            for r in rows
            if dict(r)["display_name"]
        }


class CampaignsWrapper(TenantScopedTable):
    _table = "campaigns"

    def count_by_status_in_range(
        self, tenant_id: UUID | str, start: Any, end: Any, *, conn: Any = None
    ) -> dict[str, int]:
        """{status: count} of campaigns generated in [start, end) — monthly report."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                "SELECT status, count(*) AS n FROM campaigns "
                "WHERE tenant_id = %s AND generated_at >= %s AND generated_at < %s "
                "GROUP BY status",
                (str(tid), start, end),
            ).fetchall()
        return {dict(r)["status"]: int(dict(r)["n"]) for r in rows}

    def sum_arrr_closed_in_range(
        self, tenant_id: UUID | str, start: Any, end: Any, *, conn: Any = None
    ) -> int:
        """Attributed paise for campaigns CLOSING in [start, end) (attributions ⋈
        campaigns, tenant-scoped on attributions)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT COALESCE(SUM(a.attributed_paise), 0) AS arrr "
                "FROM attributions a JOIN campaigns c ON c.id = a.campaign_id "
                "WHERE a.tenant_id = %s "
                "AND c.attribution_closed_at >= %s AND c.attribution_closed_at < %s",
                (str(tid), start, end),
            ).fetchone()
        return int(dict(row)["arrr"]) if row else 0

    def top_campaigns_by_arrr_in_range(
        self, tenant_id: UUID | str, start: Any, end: Any, *, limit: int = 5, conn: Any = None
    ) -> list[dict[str, Any]]:
        """Top campaigns by attributed paise, closing in [start, end). Returns
        rows of {cid, arrr}, descending."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                "SELECT c.id::text AS cid, COALESCE(SUM(a.attributed_paise), 0) AS arrr "
                "FROM campaigns c JOIN attributions a ON a.campaign_id = c.id "
                "WHERE c.tenant_id = %s "
                "AND c.attribution_closed_at >= %s AND c.attribution_closed_at < %s "
                "GROUP BY c.id ORDER BY arrr DESC, c.id LIMIT %s",
                (str(tid), start, end, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def attribution_window_summary(
        self,
        tenant_id: UUID | str,
        window_start: Any,
        window_end: Any,
        *,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        """Per-campaign attribution rollup over a close-at window (campaigns
        LEFT JOIN attributions, tenant-matched on both sides), ordered by id."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                """
                SELECT
                    c.id::text AS campaign_id,
                    c.attribution_closed_at,
                    COUNT(DISTINCT COALESCE(a.customer_id::text, a.razorpay_payment_id,
                                            a.id::text)) AS transacting_count,
                    COALESCE(SUM(a.attributed_paise), 0) AS arrr_paise
                FROM campaigns c
                LEFT JOIN attributions a
                  ON a.campaign_id = c.id AND a.tenant_id = c.tenant_id
                WHERE c.tenant_id = %s
                  AND c.attribution_close_at >= %s
                  AND c.attribution_close_at <= %s
                GROUP BY c.id, c.attribution_closed_at
                ORDER BY c.id ASC
                """,
                (str(tid), window_start, window_end),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_recent_with_responses(
        self,
        tenant_id: UUID | str,
        *,
        days_back: int,
        limit: int,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        """Recent campaigns (newest first) with a LEFT-JOIN response count from
        attributions. The attributions join is tenant-matched on BOTH sides so it
        can't pull another tenant's rows."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                """
                SELECT
                    c.id::text AS campaign_id,
                    c.generated_at AS sent_at,
                    COALESCE(c.plan_json -> 'message_plan' ->> 'template_id', '') AS template_id,
                    c.status,
                    COUNT(a.id) AS response_count
                FROM campaigns c
                LEFT JOIN attributions a
                  ON a.campaign_id = c.id AND a.tenant_id = c.tenant_id
                WHERE c.tenant_id = %s
                  AND c.generated_at >= now() - make_interval(days => %s)
                GROUP BY c.id
                ORDER BY c.generated_at DESC
                LIMIT %s
                """,
                (str(tid), days_back, limit),
            ).fetchall()
        return [dict(r) for r in rows]


class PendingApprovalsWrapper(TenantScopedTable):
    _table = "pending_approvals"

    def find_open_for_tenant(
        self, tenant_id: UUID | str, *, conn: Any = None
    ) -> dict[str, Any] | None:
        """Most-recent UNRESOLVED approval for the tenant (any run), else None —
        the runner's 'is this inbound an approval reply?' check."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT id::text AS id, run_id::text AS run_id, approval_type, "
                "campaign_id::text AS campaign_id FROM pending_approvals "
                "WHERE tenant_id = %s AND resolved_at IS NULL "
                "ORDER BY requested_at DESC LIMIT 1",
                (str(tid),),
            ).fetchone()
        return dict(row) if row is not None else None

    def find_open_for_run(
        self, tenant_id: UUID | str, run_id: UUID | str, *, conn: Any = None
    ) -> dict[str, Any] | None:
        """The open (unresolved) approval for a run — idempotency guard +
        resume lookup."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT id::text AS id, decision, status, resolved_at "
                "FROM pending_approvals "
                "WHERE tenant_id = %s AND run_id = %s AND resolved_at IS NULL",
                (str(tid), str(run_id)),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_resolved(
        self,
        tenant_id: UUID | str,
        approval_id: UUID | str,
        *,
        decision: str,
        status: str,
        owner_message_sid: Any = None,
        conn: Any = None,
    ) -> int:
        """Resolve an open approval. Tenant-predicated (the pre-migration UPDATE
        was ``WHERE id`` only — VT-306 adds ``AND tenant_id`` so a stray id can't
        resolve another tenant's approval). Returns rows updated."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            cur = c.execute(
                "UPDATE pending_approvals "
                "SET decision = %s, status = %s, resolved_at = now(), "
                # COALESCE preserves an existing sid when the caller passes None
                # (matches the pre-migration behaviour; a redelivery keeps the sid).
                "owner_message_sid = COALESCE(%s, owner_message_sid) "
                "WHERE tenant_id = %s AND id = %s AND resolved_at IS NULL",
                (decision, status, owner_message_sid, str(tid), str(approval_id)),
            )
            return cur.rowcount if cur.rowcount is not None else 0


class OwnerInputsWrapper(TenantScopedTable):
    _table = "owner_inputs"


class PhoneTokenResolutionsWrapper(TenantScopedTable):
    _table = "phone_token_resolutions"
    _id_col = "phone_token"  # PK is the token, not `id`


__all__ = [
    "CampaignsWrapper",
    "CustomersWrapper",
    "OwnerInputsWrapper",
    "PendingApprovalsWrapper",
    "PhoneTokenResolutionsWrapper",
]
