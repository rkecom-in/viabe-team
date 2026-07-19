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

# VT-632 (CL-2026-07-10, Fazal): the ONE lapsed/dormant definition. A customer is LAPSED iff they
# have >=1 'sale' and NO 'sale' in the last ``LAPSED_WINDOW_DAYS`` days. This constant is the SINGLE
# SOURCE OF TRUTH shared by BOTH the owner-facing count metric (``count_lapsed``) AND the Sales-
# Recovery SEND cohort (``lapsed_candidates`` below) — so "the number the owner hears == the set a
# campaign targets" (option 2). It SUPERSEDES the VT-312 tenant-relative percentile targeting: the
# cohort no longer uses a recency/spend percentile, it uses this fixed window. status_query.py +
# sales_recovery_executor.py both import it from here; never re-literal 45.
LAPSED_WINDOW_DAYS = 45

# VT-369 Sales-Recovery detection (CustomersWrapper.lapsed_candidates). phone_token derivation MUST
# stay byte-identical to privacy.hash_phone ('phone_tok_' + sha256(salt:phone) hex) — if the VT-122
# tokenisation ever changes this drifts FAIL-CLOSED (no token match → no candidates) and the
# executor's pin test fails loudly.
# VT-632 option 2: the dormancy predicate below (``days_since_last_sale >= %(lapsed_days)s``) is
# BYTE-EQUIVALENT to count_lapsed's "no sale in the last N days" — so the sendable cohort is exactly
# the sendability-filtered subset of the owner-facing lapsed set (NOT a percentile-gated superset).
_LAPSED_CANDIDATES_SQL = """
WITH sales AS (
    SELECT customer_id,
           MAX(entry_date)                  AS last_sale_date,
           (CURRENT_DATE - MAX(entry_date)) AS days_since_last_sale,
           SUM(amount_paise)                AS lifetime_spend_paise
    FROM customer_ledger_entries
    WHERE tenant_id = %(tenant_id)s AND entry_type = 'sale'
    GROUP BY customer_id
)
SELECT c.id AS customer_id,
       s.last_sale_date,
       s.days_since_last_sale,
       s.lifetime_spend_paise
FROM customers c
JOIN sales s ON s.customer_id = c.id
WHERE c.tenant_id = %(tenant_id)s
  AND c.opt_out_status = 'subscribed'
  AND c.complaint_status != 'open'
  AND c.phone_e164 IS NOT NULL
  AND s.days_since_last_sale >= %(lapsed_days)s
  AND EXISTS (
      SELECT 1
      FROM record_of_consent roc
      WHERE roc.tenant_id = c.tenant_id
        AND roc.phone_token = 'phone_tok_' || encode(
                sha256(convert_to(%(salt)s || ':' || c.phone_e164, 'UTF8')), 'hex')
        AND roc.opted_out_at IS NULL
        AND roc.consent_text_version = ANY(%(versions)s)
  )
  AND NOT EXISTS (
      SELECT 1
      FROM agent_customer_contacts acc
      WHERE acc.tenant_id = c.tenant_id
        AND acc.customer_id = c.id
        AND acc.sent_at >= now() - make_interval(days => %(suppression_days)s)
  )
ORDER BY s.lifetime_spend_paise DESC, c.id
LIMIT %(limit)s
"""


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

    def count_existing(
        self, tenant_id: UUID | str, customer_ids: list[str], *, conn: Any = None
    ) -> int:
        """How many of ``customer_ids`` are REAL, tenant-scoped customer rows (VT-607
        manager-review cohort grounding — a hallucinated/foreign id simply doesn't count).
        Read-only; the same existence test collapse's recipient resolution applies later."""
        tid = self._uuid(tenant_id)
        if not customer_ids:
            return 0
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT count(*) AS n FROM customers WHERE tenant_id = %s AND id = ANY(%s)",
                (str(tid), customer_ids),
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

    def count_lapsed(
        self, tenant_id: UUID | str, *, days: int, conn: Any = None
    ) -> int:
        """VT-632 — COUNT lapsed customers for the owner's "how many lapsed/dormant" status query.
        Fazal's canonical definition (2026-07-09): a LAPSED customer is one who USED to buy but has
        had NO sale in the last ``days`` (45 = ``LAPSED_WINDOW_DAYS``). So: has >=1 'sale' ledger
        entry (was active) AND no 'sale' within ``days`` (went quiet). Purchase-behaviour fact —
        NOT filtered by opt_out (that is a sendability filter, not the lapsed definition). Since
        CL-2026-07-10 (option 2) ``lapsed_candidates`` uses this SAME window, so the SR send cohort
        is exactly this set intersected with the sendability gates (subscribed / consent /
        suppression) — the number the owner hears IS the set a campaign targets. Tenant-predicated
        (RLS + explicit WHERE)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT count(*) AS n FROM customers c "
                "WHERE c.tenant_id = %(tid)s "
                "  AND EXISTS (SELECT 1 FROM customer_ledger_entries e "
                "              WHERE e.tenant_id = c.tenant_id AND e.customer_id = c.id "
                "                AND e.entry_type = 'sale') "
                "  AND NOT EXISTS (SELECT 1 FROM customer_ledger_entries e "
                "                  WHERE e.tenant_id = c.tenant_id AND e.customer_id = c.id "
                "                    AND e.entry_type = 'sale' "
                "                    AND e.entry_date > CURRENT_DATE - make_interval(days => %(days)s))",
                {"tid": str(tid), "days": days},
            ).fetchone()
        return int(dict(row)["n"]) if row else 0

    def list_customers_for_export(
        self, tenant_id: UUID | str, *, lapsed_days: int, limit: int, offset: int, conn: Any = None
    ) -> list[dict[str, Any]]:
        """VT-676 — the customer-list EXPORT read (owner-facing CSV): id, tenant_id, display_name,
        phone_e164 (RAW — leaves only via the verified-owner attachment path), opt_out_status,
        spend_paise, last_sale_date, and ``lapsed`` computed with the SAME canonical definition as
        ``count_lapsed`` (had a sale, none within ``lapsed_days``) — so the flag the owner sees in
        the file NEVER diverges from the count they hear in chat. Newest first, tenant-predicated."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                "SELECT c.id, c.tenant_id, c.display_name, c.phone_e164, c.opt_out_status, "
                "       COALESCE(SUM(l.amount_paise), 0) AS spend_paise, "
                "       MAX(l.entry_date) FILTER (WHERE l.entry_type = 'sale') AS last_sale_date, "
                "       (MAX(l.entry_date) FILTER (WHERE l.entry_type = 'sale') IS NOT NULL "
                "        AND MAX(l.entry_date) FILTER (WHERE l.entry_type = 'sale') "
                "            <= CURRENT_DATE - make_interval(days => %(days)s)) AS lapsed "
                "FROM customers c "
                "LEFT JOIN customer_ledger_entries l "
                "  ON l.tenant_id = c.tenant_id AND l.customer_id = c.id "
                "WHERE c.tenant_id = %(tid)s "
                "GROUP BY c.id, c.tenant_id, c.display_name, c.phone_e164, c.opt_out_status "
                "ORDER BY c.created_at DESC, c.id "
                "LIMIT %(limit)s OFFSET %(offset)s",
                {"tid": str(tid), "days": lapsed_days, "limit": limit, "offset": offset},
            ).fetchall()
        out = [dict(r) for r in rows]
        self._validate(out, tid)  # layer-2 tenant-isolation backstop
        return out

    def count_with_sales(self, tenant_id: UUID | str, *, conn: Any = None) -> int:
        """VT-632 — how many customers have ANY 'sale' ledger entry (the active base). Distinguishes
        an EMPTY ledger (no sales data at all -> a lapsed_count of 0 must NOT claim "everyone bought
        recently", which fabricates against a tenant with no data) from a real "0 lapsed of N".
        Tenant-predicated."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT count(DISTINCT c.id) AS n FROM customers c "
                "WHERE c.tenant_id = %(tid)s "
                "  AND EXISTS (SELECT 1 FROM customer_ledger_entries e "
                "              WHERE e.tenant_id = c.tenant_id AND e.customer_id = c.id "
                "                AND e.entry_type = 'sale')",
                {"tid": str(tid)},
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
        """percentile_cont of days-since-LAST-ACTIVITY per customer (VT-485).

        Recency is the days since the customer's most-recent ACTIVITY, where
        activity = the LATER of ``customers.last_inbound_at`` and the customer's
        latest purchase-ledger ``entry_date`` (``entry_type='sale'``). A customer
        is included iff at least ONE of those two signals exists.

        VT-485 fix: this previously read ``last_inbound_at`` ALONE, filtered
        ``IS NOT NULL`` — which EXCLUDED every Shopify-sourced customer who bought
        but never messaged (``last_inbound_at`` NULL), so a customer lapsed BY
        PURCHASE (bought 90+ days ago, never inbound) surfaced NO dormant cohort
        and the agent fell through to ``insufficient_data``. The purchase-ledger
        ``entry_date`` is the actual last-purchase recency; combining it with
        ``last_inbound_at`` (GREATEST = the freshest signal wins) makes a
        purchase-lapsed customer a valid dormant-cohort member without losing the
        inbound signal for chat-active customers. Returns ``{"p": [...]}`` or None.
        """
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "WITH last_sale AS ("
                "  SELECT customer_id, MAX(entry_date) AS last_sale_date "
                "  FROM customer_ledger_entries "
                "  WHERE tenant_id = %(tid)s AND entry_type = 'sale' "
                "  GROUP BY customer_id"
                "), activity AS ("
                "  SELECT GREATEST(c.last_inbound_at::date, ls.last_sale_date) "
                "         AS last_activity_date "
                "  FROM customers c "
                "  LEFT JOIN last_sale ls ON ls.customer_id = c.id "
                "  WHERE c.tenant_id = %(tid)s "
                "    AND (c.last_inbound_at IS NOT NULL OR ls.last_sale_date IS NOT NULL)"
                ") "
                "SELECT percentile_cont(%(pctls)s) WITHIN GROUP "
                "(ORDER BY (now()::date - last_activity_date)) AS p "
                "FROM activity WHERE last_activity_date IS NOT NULL",
                {"tid": str(tid), "pctls": list(pctls)},
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

    # --- VT-369 agent surface ------------------------------------------------

    def send_eligibility(
        self, tenant_id: UUID | str, customer_id: UUID | str, *, conn: Any = None
    ) -> dict[str, Any] | None:
        """The SEND-TIME re-read for the agent send choke point (VT-369 gate 3):
        opt_out_status + complaint_status + phone, fetched at the moment of send so
        draft-time state is never trusted. None = customer gone (never sendable)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT id, tenant_id, opt_out_status, complaint_status, phone_e164, display_name "
                "FROM customers WHERE tenant_id = %s AND id = %s",
                (str(tid), str(customer_id)),
            ).fetchone()
        if row is None:
            return None
        out = dict(row)
        self._validate([out], tid)
        return out

    def display_name(
        self, tenant_id: UUID | str, customer_id: UUID | str, *, conn: Any = None
    ) -> str | None:
        """Single customer's display_name (the VT-369 fact-bundle read)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT display_name FROM customers WHERE tenant_id = %s AND id = %s",
                (str(tid), str(customer_id)),
            ).fetchone()
        if row is None:
            return None
        return dict(row).get("display_name")

    def agent_optout_attribution(
        self,
        tenant_id: UUID | str,
        phone_token: str,
        *,
        salt: str,
        attribution_days: int = 30,
        spike_window_days: int = 7,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        """VT-369 PR-2 — opt-out attribution (plan §3b/§5.4): which agents contacted THIS
        opting-out customer within ``attribution_days``, and how many DISTINCT customers who
        opted out in the last ``spike_window_days`` had a ≤attribution_days contact from that
        same agent (the spike counter — ≥3 trips the optout_spike regression). Joins customers
        on the recomputed phone token (same expression as lapsed_candidates — drift fails
        CLOSED). Returns [] when this opt-out is not attributable to any agent."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                """
                WITH tok AS (
                    SELECT c.id AS customer_id
                    FROM customers c
                    WHERE c.tenant_id = %(tenant_id)s
                      AND c.phone_e164 IS NOT NULL
                      AND 'phone_tok_' || encode(
                            sha256(convert_to(%(salt)s || ':' || c.phone_e164, 'UTF8')), 'hex'
                          ) = %(phone_token)s
                ),
                hit AS (
                    SELECT DISTINCT acc.agent
                    FROM agent_customer_contacts acc
                    JOIN tok ON tok.customer_id = acc.customer_id
                    WHERE acc.tenant_id = %(tenant_id)s
                      AND acc.sent_at >= now() - make_interval(days => %(attribution_days)s)
                )
                SELECT h.agent,
                       (SELECT count(DISTINCT roc.phone_token)
                          FROM record_of_consent roc
                          JOIN customers c2
                            ON c2.tenant_id = roc.tenant_id
                           AND c2.phone_e164 IS NOT NULL
                           AND 'phone_tok_' || encode(
                                 sha256(convert_to(%(salt)s || ':' || c2.phone_e164, 'UTF8')), 'hex'
                               ) = roc.phone_token
                          JOIN agent_customer_contacts acc2
                            ON acc2.tenant_id = roc.tenant_id
                           AND acc2.customer_id = c2.id
                           AND acc2.agent = h.agent
                           AND acc2.sent_at >= now() - make_interval(days => %(attribution_days)s)
                         WHERE roc.tenant_id = %(tenant_id)s
                           AND roc.opted_out_at >= now() - make_interval(days => %(spike_window_days)s)
                       ) AS spike_count
                FROM hit h
                """,
                {
                    "tenant_id": str(tid),
                    "phone_token": phone_token,
                    "salt": salt,
                    "attribution_days": attribution_days,
                    "spike_window_days": spike_window_days,
                },
            ).fetchall()
        return [dict(r) if isinstance(r, dict) else {"agent": r[0], "spike_count": r[1]} for r in rows]

    def lapsed_candidates(
        self,
        tenant_id: UUID | str,
        *,
        lapsed_days: int,
        salt: str,
        versions: list[str],
        suppression_days: int,
        limit: int,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        """VT-369 Sales-Recovery detection — the ONE analytic read over customers:
        subscribed + complaint-clear + an ACTIVE marketing-cleared consent row
        (consent_text_version = ANY(versions) — the C2 allowlist, list-param, never
        literal IN ()) + LAPSED (no 'sale' in the last ``lapsed_days`` days — the SAME
        window as the owner-facing ``count_lapsed`` metric, CL-2026-07-10 option 2) + no
        agent contact within suppression_days; richest-first, capped. The ``lapsed_days``
        recency predicate is byte-equivalent to count_lapsed, so this cohort is exactly
        count_lapsed's sendability-filtered subset (NOT the old VT-312 percentile
        superset). Per-sweep cap: the result is ``ORDER BY spend DESC LIMIT limit``
        (``DEFAULT_DETECTION_LIMIT`` = 200, aligned to the daily send cap — CL-2026-07-10),
        so "owner count == cohort size" holds for the full realistic SMB range (≤200
        sendable-lapsed). For a rare >200-lapsed tenant one sweep targets the richest 200;
        the count stays the TRUE total and the tail batches across sweeps as the daily-send
        + 30d recontact-suppression caps clear (the VT-619 budget gate bounds cost). Empty
        ``versions`` matches nothing
        (structurally fail-closed). Lives HERE because per-tenant customers SQL belongs to
        the wrapper layer (the no-direct-tenant-db-access lint)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                _LAPSED_CANDIDATES_SQL,
                {
                    "tenant_id": str(tid),
                    "lapsed_days": lapsed_days,
                    "salt": salt,
                    "versions": versions,
                    "suppression_days": suppression_days,
                    "limit": limit,
                },
            ).fetchall()
        return [dict(r) for r in rows]


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

    def has_any_since(
        self, tenant_id: UUID | str, *, within_minutes: int, conn: Any = None
    ) -> bool:
        """True iff ANY campaigns row exists for the tenant created within ``within_minutes`` —
        the VT-655 emission-honesty draft-EXISTS fact-check (a claimed "your plan is ready" must
        be backed by a real ``campaigns`` row; the caller fail-closes on error)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT 1 FROM campaigns WHERE tenant_id = %s "
                "AND created_at >= now() - make_interval(mins => %s) LIMIT 1",
                (str(tid), int(within_minutes)),
            ).fetchone()
        return row is not None

    def recovered_paise_for_campaigns(
        self, tenant_id: UUID | str, campaign_ids: list[str], *, conn: Any = None
    ) -> dict[str, int]:
        """{campaign_id: recovered_paise} — per-campaign attributed ARRR
        (SUM ``attributions.attributed_paise``) for the given campaigns; the
        VT-563 context-builder recent-campaigns read. Tenant-predicated on
        ``attributions`` (RLS + explicit WHERE); ``campaigns`` is not touched.
        A campaign with no attribution rows is ABSENT from the map (the caller
        defaults it to 0)."""
        tid = self._uuid(tenant_id)
        ids = [str(c) for c in campaign_ids]
        if not ids:
            return {}
        with self._conn(tid, conn) as c:
            rows = c.execute(
                "SELECT campaign_id::text AS cid, "
                "       COALESCE(SUM(attributed_paise), 0) AS arrr "
                "FROM attributions "
                "WHERE tenant_id = %s AND campaign_id = ANY(%s) "
                "GROUP BY campaign_id",
                (str(tid), ids),
            ).fetchall()
        return {dict(r)["cid"]: int(dict(r)["arrr"]) for r in rows}

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

    def get_status(
        self, tenant_id: UUID | str, campaign_id: str, *, conn: Any = None
    ) -> str | None:
        """VT-558 — the campaign's current status (tenant-predicated). None = missing/cross-tenant
        or a non-str value; the caller treats None as 'not cancelled' (fail-open kill check)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT status FROM campaigns WHERE tenant_id = %s AND id = %s",
                (str(tid), str(campaign_id)),
            ).fetchone()
        if row is None:
            return None
        val = row.get("status") if isinstance(row, dict) else (row[0] if row else None)
        return val if isinstance(val, str) else None

    def cancel(self, tenant_id: UUID | str, campaign_id: str, *, conn: Any = None) -> bool:
        """VT-558 campaign true-kill — CAS a non-terminal campaign → 'cancelled'. Only 'proposed' /
        'approved' can be killed; a sent/rejected/failed/already-cancelled campaign is a no-op →
        False. The execute loop observes 'cancelled' at entry + each recipient boundary and stops the
        fan-out (the remaining recipients are counted ``killed`` and never sent)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            cur = c.execute(
                "UPDATE campaigns SET status = 'cancelled' "
                "WHERE tenant_id = %s AND id = %s AND status IN ('proposed', 'approved')",
                (str(tid), str(campaign_id)),
            )
            return (cur.rowcount or 0) > 0

    def unexecuted_campaign_exists_for_runs(
        self, tenant_id: UUID | str, run_ids: list[str], *, conn: Any = None
    ) -> bool:
        """VT-633 F-3 — true iff a campaign minted by one of ``run_ids`` (the manager loop's own
        dispatch run identities — see ``manager.verification.resolve_terminal_outcome``) is still
        stuck 'proposed'/'approved' (never reached ``execute_approved_campaign``'s own 'sent'
        advance, nor an ops-driven 'cancelled'/'rejected' terminal) AND has zero real
        ``campaign_messages`` rows recorded against it. This is the deterministic signature of the
        VT-633 defect: an approved-but-never-executed campaign whose PROPOSAL step alone (evidence_
        kind='campaign_plan', written at proposal time — before any send) satisfied the evidence-
        presence proxy, letting the task settle 'completed_with_effect' with zero customers ever
        contacted.

        The ``campaign_messages`` join goes via ``idempotency_key``'s documented
        ``{campaign_id}:{customer_id}`` prefix (``campaign/execute.py``'s D1 design), NOT the
        ``campaign_messages.campaign_id`` column — ``send_whatsapp_template._write_campaign_message``
        never populates that column (a pre-existing gap outside this row's scope); joining on it
        would find zero rows for EVERY campaign, real sends included, and downgrade rows that
        actually executed."""
        tid = self._uuid(tenant_id)
        ids = [str(r) for r in run_ids]
        if not ids:
            return False
        with self._conn(tid, conn) as c:
            row = c.execute(
                """
                SELECT 1 FROM campaigns c
                WHERE c.tenant_id = %s
                  AND c.run_id = ANY(%s)
                  AND c.status IN ('proposed', 'approved')
                  AND NOT EXISTS (
                      SELECT 1 FROM campaign_messages m
                      WHERE m.tenant_id = c.tenant_id
                        AND m.idempotency_key LIKE c.id::text || %s
                  )
                LIMIT 1
                """,
                (str(tid), ids, ":%"),
            ).fetchone()
        return row is not None

    def executed_campaign_exists_for_runs(
        self, tenant_id: UUID | str, run_ids: list[str], *, conn: Any = None
    ) -> bool:
        """VT-633 #52 — the INVERSE of ``unexecuted_campaign_exists_for_runs``: true iff a campaign
        minted by one of ``run_ids`` reached 'sent' AND has real ``campaign_messages`` rows (same
        ``{campaign_id}:{customer_id}`` idempotency-key prefix join — the campaign_id column is
        never written by the send path). The deterministic UPWARD verification floor's read: a
        task whose own dispatch EXECUTED its approved campaign has a DB-proven effect — the LLM
        verifier must never second-guess it (live: a successful sent:3 execution was verify-
        blocked into an escalate, telling the owner "sent to 3" then "couldn't finish")."""
        tid = self._uuid(tenant_id)
        ids = [str(r) for r in run_ids]
        if not ids:
            return False
        with self._conn(tid, conn) as c:
            row = c.execute(
                """
                SELECT 1 FROM campaigns c
                WHERE c.tenant_id = %s
                  AND c.run_id = ANY(%s)
                  AND c.status = 'sent'
                  AND EXISTS (
                      SELECT 1 FROM campaign_messages m
                      WHERE m.tenant_id = c.tenant_id
                        AND m.idempotency_key LIKE c.id::text || %s
                  )
                LIMIT 1
                """,
                (str(tid), ids, ":%"),
            ).fetchone()
        return row is not None

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

    def find_bound_task_for_approval(
        self, tenant_id: UUID | str, approval_id: UUID | str, *, conn: Any = None
    ) -> dict[str, Any] | None:
        """VT-668 — the resolution-seam approval→manager_task reverse join (composite read lives
        HERE because the pending_approvals fragment is wrapper-scoped). Returns
        ``{id, status, approval_type}`` for the task bound to ``approval_id``, else None. The link
        is one of two (both producers): the loop's ``stall_metadata->>'awaiting_approval_run_id'``
        stamp OR the legacy ``source_message_ref = run_id`` (a loop task's source_message_ref is a
        Twilio SID, never a run_id UUID — no collision). ``ORDER BY t.updated_at DESC`` is a
        defensive tie-break."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT t.id, t.status, p.approval_type, t.stall_metadata "
                "FROM pending_approvals p "
                "JOIN manager_tasks t ON t.tenant_id = p.tenant_id "
                "  AND (t.stall_metadata->>'awaiting_approval_run_id' = p.run_id::text "
                "       OR t.source_message_ref = p.run_id::text) "
                "WHERE p.tenant_id = %s AND p.id = %s "
                "ORDER BY t.updated_at DESC LIMIT 1",
                (str(tid), str(approval_id)),
            ).fetchone()
        return dict(row) if row is not None else None

    def open_run_for_task(
        self, tenant_id: UUID | str, task_id: UUID | str, *, conn: Any = None
    ) -> str | None:
        """VT-668 — the task→OPEN-approval forward join (reaper dead-letter surfacing, fix 3).
        Returns the ``run_id`` (text) of an unresolved approval bound to ``task_id`` (same two
        linkages as ``find_bound_task_for_approval``), else None."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT p.run_id::text AS run_id FROM pending_approvals p "
                "JOIN manager_tasks t ON t.tenant_id = p.tenant_id "
                "WHERE t.tenant_id = %s AND t.id = %s AND p.resolved_at IS NULL "
                "  AND (t.stall_metadata->>'awaiting_approval_run_id' = p.run_id::text "
                "       OR t.source_message_ref = p.run_id::text) "
                "ORDER BY p.requested_at DESC LIMIT 1",
                (str(tid), str(task_id)),
            ).fetchone()
        if row is None:
            return None
        return str(row["run_id"] if isinstance(row, dict) else row[0])

    def status_for_run(
        self, tenant_id: UUID | str, run_id: UUID | str, *, conn: Any = None
    ) -> str | None:
        """The approval row's status for ``run_id`` (VT-607 paused_approval wait poll), else
        None when no row exists. Read-only; run_id uniquely identifies the row (mig 052)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT status FROM pending_approvals WHERE tenant_id = %s AND run_id = %s",
                (str(tid), str(run_id)),
            ).fetchone()
        if row is None:
            return None
        return str(dict(row)["status"]) if isinstance(row, dict) else str(row[0])

    def decision_for_run(
        self, tenant_id: UUID | str, run_id: UUID | str, *, conn: Any = None
    ) -> str | None:
        """The approval row's ``decision`` for ``run_id`` (VT-607 fix round — the paused_approval
        resolution MUST route on the owner's actual decision, not just "no longer pending";
        ``status`` alone collapses ``needs_changes`` into ``rejected`` (mig 052), so reading
        ``status`` here would silently discard the needs_changes/rejected distinction the loop
        needs). None when no row exists, OR when the row is still unresolved (``decision`` is
        NULL while pending — mig 052). Read-only; run_id uniquely identifies the row."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT decision FROM pending_approvals WHERE tenant_id = %s AND run_id = %s",
                (str(tid), str(run_id)),
            ).fetchone()
        if row is None:
            return None
        decision = dict(row)["decision"] if isinstance(row, dict) else row[0]
        return str(decision) if decision is not None else None

    def approval_for_run(
        self, tenant_id: UUID | str, run_id: UUID | str, *, conn: Any = None
    ) -> dict[str, Any] | None:
        """VT-633 F-2 — the FULL approval row (``decision``/``approval_type``/``campaign_id``) for
        ``run_id``, resolved OR not (unlike ``find_open_for_run``, which only returns unresolved
        rows). The loop's approved-branch execution owner (``workflow._execute_approved_campaign``)
        needs this AFTER ``_approval_decision_for_run`` already read ``decision == 'approved'`` off
        the same run_id, to learn WHAT was approved (a ``campaign_send`` vs some other
        ``approval_type`` — a ``sensitive_data_access`` approval has nothing to execute here) and
        which campaign to fan out. None when no row exists for this run_id."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT decision, approval_type, campaign_id::text AS campaign_id "
                "FROM pending_approvals WHERE tenant_id = %s AND run_id = %s",
                (str(tid), str(run_id)),
            ).fetchone()
        return dict(row) if row is not None else None

    def has_open_for_tenant(self, tenant_id: UUID | str, *, conn: Any = None) -> bool:
        """True iff ANY unresolved approval exists for the tenant — the one-open-per-tenant
        collision probe (VT-384 demote C-c; mig-128 is the structural backstop)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT 1 FROM pending_approvals "
                "WHERE tenant_id = %s AND resolved_at IS NULL LIMIT 1",
                (str(tid),),
            ).fetchone()
        return row is not None

    def has_recent_of_type(
        self,
        tenant_id: UUID | str,
        approval_type: str,
        *,
        within_days: int,
        conn: Any = None,
    ) -> bool:
        """True iff an approval of ``approval_type`` was requested within ``within_days``
        — the VT-384 autonomy-offer cooldown probe."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT 1 FROM pending_approvals "
                "WHERE tenant_id = %s AND approval_type = %s "
                "AND requested_at >= now() - make_interval(days => %s) LIMIT 1",
                (str(tid), approval_type, int(within_days)),
            ).fetchone()
        return row is not None

    def find_unarmed_awaiting_batch(
        self, tenant_id: UUID | str, agent: str, *, conn: Any = None
    ) -> str | None:
        """Oldest ``awaiting_approval`` agent_draft_batches row for (tenant, agent) with NO
        unresolved approval referencing it — the VT-384 stranded/queued-demote re-arm probe
        (composite read lives HERE because the pending_approvals fragment is wrapper-scoped)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT b.id::text AS bid FROM agent_draft_batches b "
                "WHERE b.tenant_id = %s AND b.agent = %s AND b.status = 'awaiting_approval' "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM pending_approvals p "
                "    WHERE p.tenant_id = b.tenant_id AND p.draft_batch_id = b.id "
                "      AND p.resolved_at IS NULL) "
                "ORDER BY b.updated_at ASC, b.id ASC LIMIT 1",
                (str(tid), agent),
            ).fetchone()
        if row is None:
            return None
        return str(row["bid"] if isinstance(row, dict) else row[0])

    def approved_batch_for_send_approval(
        self, tenant_id: UUID | str, approval_id: UUID | str, *, conn: Any = None
    ) -> str | None:
        """The agent_draft_batches id linked to an ``agent_customer_send`` approval that has
        reached ``'approved'`` — the VT-418 start-after-commit lookup (the
        pending_approvals×agent_draft_batches join lives HERE, wrapper-scoped). None when the
        batch did not reach approved / it is not an agent_customer_send approval (a safe no-op)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT b.id::text AS batch_id "
                "FROM pending_approvals a "
                "JOIN agent_draft_batches b "
                "  ON b.tenant_id = a.tenant_id AND b.id = a.draft_batch_id "
                "WHERE a.tenant_id = %s AND a.id = %s "
                "  AND a.approval_type = 'agent_customer_send' "
                "  AND b.status = 'approved'",
                (str(tid), str(self._uuid(approval_id))),
            ).fetchone()
        if row is None:
            return None
        return str(row["batch_id"] if isinstance(row, dict) else row[0])

    def get_open_by_id(
        self, tenant_id: UUID | str, approval_id: UUID | str, *, conn: Any = None
    ) -> dict[str, Any] | None:
        """The unresolved approval by id (type + details) — the VT-384 ENABLE-grant
        resolution lookup."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT id::text AS id, approval_type, details FROM pending_approvals "
                "WHERE tenant_id = %s AND id = %s AND resolved_at IS NULL",
                (str(tid), str(approval_id)),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_open_of_type(
        self, tenant_id: UUID | str, approval_type: str, *, conn: Any = None
    ) -> dict[str, Any] | None:
        """Most-recent unresolved approval of ``approval_type`` — the VT-384 ENABLE
        reply's autonomy_upgrade lookup."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT id::text AS id, details FROM pending_approvals "
                "WHERE tenant_id = %s AND approval_type = %s AND resolved_at IS NULL "
                "ORDER BY requested_at DESC LIMIT 1",
                (str(tid), approval_type),
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

    def extend_on_defer(
        self,
        tenant_id: UUID | str,
        approval_id: UUID | str,
        *,
        timeout_hours: int = 48,
        conn: Any = None,
    ) -> int:
        """VT-334: extend an OPEN approval on a defer — bump defer_count, push timeout_at out
        ``timeout_hours``, keep it pending (resolved_at stays NULL). Tenant-predicated. Returns
        the NEW defer_count (0 if the row was not open / not found)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "UPDATE pending_approvals "
                "SET defer_count = defer_count + 1, "
                "    timeout_at = now() + make_interval(hours => %s) "
                "WHERE tenant_id = %s AND id = %s AND resolved_at IS NULL "
                "RETURNING defer_count",
                (timeout_hours, str(tid), str(approval_id)),
            ).fetchone()
        if not row:
            return 0
        return int(row["defer_count"] if isinstance(row, dict) else row[0])

    def delete_by_id(
        self, tenant_id: UUID | str, approval_id: UUID | str, *, conn: Any = None
    ) -> int:
        """VT-615 arm-then-send compensation: remove a just-armed (committed) pending row
        when the subsequent template send fails, so the orphan doesn't block the tenant's
        one-open queue until the timeout sweep reaps it. Tenant-predicated by-PK (never
        cross-tenant). Returns rows deleted."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            cur = c.execute(
                "DELETE FROM pending_approvals WHERE tenant_id = %s AND id = %s",
                (str(tid), str(approval_id)),
            )
            return cur.rowcount if cur.rowcount is not None else 0

    def set_owner_message_sid(
        self,
        tenant_id: UUID | str,
        approval_id: UUID | str,
        owner_message_sid: str,
        *,
        conn: Any = None,
    ) -> int:
        """VT-615 step 2c: record which owner message carried the approval template
        (metadata only; ``mark_resolved`` COALESCEs it on resolve). Tenant-predicated
        by-PK. Returns rows updated."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            cur = c.execute(
                "UPDATE pending_approvals SET owner_message_sid = %s "
                "WHERE tenant_id = %s AND id = %s",
                (owner_message_sid, str(tid), str(approval_id)),
            )
            return cur.rowcount if cur.rowcount is not None else 0

    def count_recent_campaign_requests(
        self, tenant_id: UUID | str, *, days: int = 7, conn: Any = None
    ) -> int:
        """VT-334 per-week messaging budget: how many owner-interrupt approval requests this
        tenant has had in the last ``days`` (the owner-fatigue guard skips a new one at >= 2).
        VT-369: the budget is SHARED across the campaign and agent surfaces — one 2/week
        owner-interrupt budget, so ``agent_customer_send`` rows count alongside
        ``campaign_send`` (plan §4.3; F3 confirms share-vs-raise)."""
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
                "SELECT count(*) AS n FROM pending_approvals "
                "WHERE tenant_id = %s "
                "AND approval_type IN ('campaign_send', 'agent_customer_send') "
                "AND created_at >= now() - make_interval(days => %s)",
                (str(tid), days),
            ).fetchone()
        if not row:
            return 0
        return int(row["n"] if isinstance(row, dict) else row[0])


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

    def insert_idempotent(
        self, tenant_id: UUID | str, payload: dict[str, Any], *, conn: Any = None
    ) -> dict[str, Any]:
        """VT-149: insert one owner_inputs row, IDEMPOTENT on (tenant_id, message_sid). On a
        DBOS webhook_pipeline_run REPLAY the second write CONFLICTs on the UNIQUE partial index
        (mig 111) → DO NOTHING; we then return the EXISTING row's id, so the replay does not
        double-write. A NULL message_sid has no dedup key → a plain insert. tenant_id is forced
        (the RLS WITH CHECK + the literal below); returns ``{"id": ...}``."""
        tid = self._uuid(tenant_id)
        msid = payload.get("message_sid")
        with self._conn(tid, conn) as c:
            row = c.execute(
                "INSERT INTO owner_inputs "
                "(id, tenant_id, run_id, message_sid, intent, segment, occasion) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, message_sid) WHERE message_sid IS NOT NULL "
                "DO NOTHING RETURNING id",
                (
                    payload["id"], str(tid), payload.get("run_id"), msid,
                    payload["intent"], payload.get("segment"), payload.get("occasion"),
                ),
            ).fetchone()
            if row is not None:
                return dict(row)
            # Conflict → a prior write for this message_sid exists; return its id (replay no-op).
            existing = c.execute(
                "SELECT id FROM owner_inputs WHERE tenant_id = %s AND message_sid = %s LIMIT 1",
                (str(tid), msid),
            ).fetchone()
        return dict(existing) if existing else {"id": payload["id"]}


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
