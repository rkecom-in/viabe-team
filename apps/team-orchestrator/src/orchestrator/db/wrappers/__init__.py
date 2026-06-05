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

import json
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

    def list_id_and_display_name(
        self, tenant_id: UUID | str, *, conn: Any = None
    ) -> list[dict[str, Any]]:
        """(id, display_name) for customers with a non-null name — VT-84 fuzzy
        owner-exclusion lookup. Tenant-predicated (RLS + explicit WHERE)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                "SELECT id, display_name FROM customers "
                "WHERE tenant_id = %s AND display_name IS NOT NULL",
                (str(tid),),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_owner_excluded(
        self, tenant_id: UUID | str, customer_id: UUID | str, *, conn: Any = None
    ) -> int:
        """VT-84: owner-side exclude. Sets opt_out_status='owner_excluded' ONLY from
        'subscribed' — a consumer 'opted_out'/'blocked' ALWAYS wins (precedence; never
        downgrade a legal opt-out to an owner preference). Returns rows updated
        (0 = already opted_out/blocked/excluded). Tenant-predicated."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            cur = c.execute(
                "UPDATE customers SET opt_out_status = 'owner_excluded', updated_at = now() "
                "WHERE tenant_id = %s AND id = %s AND opt_out_status = 'subscribed'",
                (str(tid), str(customer_id)),
            )
            return cur.rowcount if cur.rowcount is not None else 0

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
                "SELECT id::text AS id, tenant_id FROM customers "
                "WHERE tenant_id = %s AND id = ANY(%s::uuid[])",
                (str(tid), list(ids)),
            ).fetchall()
        out = [dict(r) for r in rows]
        self._validate(out, tid)  # layer-2 (VT-306 bounce: was skipped)
        return {r["id"] for r in out}

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

    def count_all(self, tenant_id: UUID | str, *, conn: Any = None) -> int:
        """Total customers for the tenant (VT-312 ledger summary)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT count(*) AS n FROM customers WHERE tenant_id = %s",
                (str(tid),),
            ).fetchone()
        return int(dict(row)["n"]) if row else 0

    def count_by_opt_out_status(
        self, tenant_id: UUID | str, statuses: tuple[str, ...], *, conn: Any = None
    ) -> int:
        """COUNT customers whose opt_out_status is in ``statuses`` — VT-84 status query
        (e.g. ('opted_out', 'owner_excluded') for the owner's 'how many opt-outs')."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT count(*) AS n FROM customers "
                "WHERE tenant_id = %s AND opt_out_status = ANY(%s)",
                (str(tid), list(statuses)),
            ).fetchone()
        return int(dict(row)["n"]) if row else 0

    def top_customers_by_spend(
        self, tenant_id: UUID | str, *, limit: int, conn: Any = None
    ) -> list[dict[str, Any]]:
        """Top customers by total ledger volume (SUM of amount_paise magnitudes) — VT-87
        owner-portal index. Returns id, display_name, phone_e164 (RAW — the API endpoint
        masks to last-4; raw never crosses the orchestrator boundary), spend_paise.
        Tenant-predicated (RLS + explicit WHERE); excludes opted-out/owner-excluded."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                "SELECT c.id, c.tenant_id, c.display_name, c.phone_e164, "
                "       COALESCE(SUM(l.amount_paise), 0) AS spend_paise "
                "FROM customers c "
                "LEFT JOIN customer_ledger_entries l "
                "  ON l.tenant_id = c.tenant_id AND l.customer_id = c.id "
                "WHERE c.tenant_id = %s AND c.opt_out_status = 'subscribed' "
                "GROUP BY c.id, c.tenant_id, c.display_name, c.phone_e164 "
                "ORDER BY spend_paise DESC, c.id "
                "LIMIT %s",
                (str(tid), limit),
            ).fetchall()
        out = [dict(r) for r in rows]
        self._validate(out, tid)  # VT-338 nit-1: layer-2 tenant-isolation backstop (was skipped)
        return out

    def list_customers_page(
        self,
        tenant_id: UUID | str,
        *,
        limit: int,
        offset: int,
        excluded_only: bool = False,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        """Paginated customer list for the owner portal (VT-338). Returns id, tenant_id
        (for _validate), display_name, phone_e164 (RAW — the API endpoint masks to last-4;
        raw never crosses the boundary), opt_out_status, spend_paise. Newest first,
        tenant-predicated. ``excluded_only`` filters to opted_out/owner_excluded."""
        tid = self._uuid(tenant_id)
        status_clause = (
            "AND c.opt_out_status = ANY(%s) " if excluded_only else ""
        )
        params: list[Any] = [str(tid)]
        if excluded_only:
            params.append(["opted_out", "owner_excluded"])
        params.extend([limit, offset])
        with self._conn(tid, conn) as c:
            rows = c.execute(
                "SELECT c.id, c.tenant_id, c.display_name, c.phone_e164, c.opt_out_status, "
                "       COALESCE(SUM(l.amount_paise), 0) AS spend_paise "
                "FROM customers c "
                "LEFT JOIN customer_ledger_entries l "
                "  ON l.tenant_id = c.tenant_id AND l.customer_id = c.id "
                f"WHERE c.tenant_id = %s {status_clause}"  # noqa: S608 — status_clause is a static literal
                "GROUP BY c.id, c.tenant_id, c.display_name, c.phone_e164, c.opt_out_status "
                "ORDER BY c.created_at DESC, c.id "
                "LIMIT %s OFFSET %s",
                tuple(params),
            ).fetchall()
        out = [dict(r) for r in rows]
        self._validate(out, tid)  # layer-2 tenant-isolation backstop
        return out

    def recency_days_percentiles(
        self, tenant_id: UUID | str, pctls: list[float], *, conn: Any = None
    ) -> dict[str, Any] | None:
        """percentile_cont of days-since-last-inbound over customers with a
        last_inbound_at (VT-312). Returns the row dict ({"p": [...]}) or None."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT percentile_cont(%s) WITHIN GROUP "
                "(ORDER BY (now()::date - last_inbound_at::date)) AS p "
                "FROM customers WHERE tenant_id = %s AND last_inbound_at IS NOT NULL",
                (list(pctls), str(tid)),
            ).fetchone()
        return dict(row) if row else None

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

    def list_recent_basic(
        self, tenant_id: UUID | str, *, limit: int = 5, conn: Any = None
    ) -> list[dict[str, Any]]:
        """Recent campaigns (id, status, generated_at), newest first — the
        context-builder snapshot. Adds the explicit tenant predicate the
        pre-migration RLS-only query relied on."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                "SELECT id, tenant_id, status, generated_at FROM campaigns "
                "WHERE tenant_id = %s ORDER BY generated_at DESC LIMIT %s",
                (str(tid), limit),
            ).fetchall()
        out = [dict(r) for r in rows]
        self._validate(out, tid)
        return out

    def set_status(
        self, tenant_id: UUID | str, campaign_id: str, status: str, *, conn: Any = None
    ) -> int:
        """Set a campaign's status (tenant-predicated). Returns rows updated."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            cur = c.execute(
                "UPDATE campaigns SET status = %s WHERE tenant_id = %s AND id = %s",
                (status, str(tid), str(campaign_id)),
            )
            return cur.rowcount if cur.rowcount is not None else 0

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
                    c.tenant_id,
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
                GROUP BY c.id, c.tenant_id, c.attribution_closed_at
                ORDER BY c.id ASC
                """,
                (str(tid), window_start, window_end),
            ).fetchall()
        out = [dict(r) for r in rows]
        self._validate(out, tid)  # layer-2 (VT-306 bounce: was skipped)
        return out

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
                    c.tenant_id,
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
        out = [dict(r) for r in rows]
        self._validate(out, tid)  # VT-338: layer-2 tenant-isolation backstop (nit-1 pattern)
        return out


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

    def list_pending(
        self, tenant_id: UUID | str, *, limit: int, conn: Any = None
    ) -> list[dict[str, Any]]:
        """Unconsumed owner inputs, newest first (context-builder). Adds the
        explicit tenant predicate the pre-migration RLS-only query relied on."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                "SELECT id, tenant_id, intent, segment, occasion, created_at "
                "FROM owner_inputs WHERE tenant_id = %s AND consumed_at IS NULL "
                "ORDER BY created_at DESC LIMIT %s",
                (str(tid), limit),
            ).fetchall()
        out = [dict(r) for r in rows]
        self._validate(out, tid)
        return out


class PhoneTokenResolutionsWrapper(TenantScopedTable):
    _table = "phone_token_resolutions"
    _id_col = "phone_token"  # PK is the token, not `id`


class PlatformListingsWrapper(TenantScopedTable):
    _table = "platform_listings"

    def upsert(
        self,
        tenant_id: UUID | str,
        platform: str,
        external_listing_id: str,
        *,
        rating: float | None = None,
        attributes: dict[str, Any] | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        """Upsert one platform listing, keyed by (tenant, platform,
        external_listing_id). Returns the row. Composes atomically with the VT-65
        outbox emit when given ``conn``.

        CL-390: ``attributes`` MUST be structured non-PII facts only
        (name/category/cuisines/hours/items) — never raw review text. The caller
        owns that contract; this layer just persists what it's handed.
        """
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                """
                INSERT INTO platform_listings
                    (tenant_id, platform, external_listing_id, rating,
                     attributes, fetched_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, now())
                ON CONFLICT (tenant_id, platform, external_listing_id) DO UPDATE
                    SET rating = EXCLUDED.rating,
                        attributes = EXCLUDED.attributes,
                        fetched_at = now(),
                        updated_at = now()
                RETURNING *
                """,
                (str(tid), platform, external_listing_id, rating,
                 json.dumps(attributes or {})),
            ).fetchone()
        out = dict(row)
        self._validate([out], tid)
        return out


__all__ = [
    "CampaignsWrapper",
    "CustomersWrapper",
    "OwnerInputsWrapper",
    "PendingApprovalsWrapper",
    "PhoneTokenResolutionsWrapper",
    "PlatformListingsWrapper",
]
