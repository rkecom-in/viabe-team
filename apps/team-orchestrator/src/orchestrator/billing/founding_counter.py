"""VT-94 — founding-tier counter (atomic, race-safe).

The first ``cap`` (100) paid customers get locked founding pricing. The claim is a single
atomic UPDATE guarded by ``claimed_count < cap`` — Postgres row-level locking serializes
concurrent claims on the sentinel row, so EXACTLY ``cap`` ever succeed (never cap+1).

The claim runs INSIDE the signup transaction (atomic with the tenant INSERT): a
rolled-back signup never leaks a slot — which matters because slots are NEVER released
(the no-reopen-founding policy; docs/team/founding-tier-policy.md). ``release_founding_slot``
is AUDIT-ONLY — it stamps ``released_at`` but the counter never decrements.

Workspace-wide (no tenant scope) -> service-role only. All callers pass their own
connection (dict_row, the get_pool default) so the claim joins the caller's transaction.
"""

from __future__ import annotations

from typing import Any, NamedTuple
from uuid import UUID


class ClaimResult(NamedTuple):
    claimed: bool
    claimed_count: int  # the counter value after this call (observability)


class FoundingStatus(NamedTuple):
    claimed_count: int
    remaining: int
    cap: int
    all_claimed: bool
    public_count: int  # min(claimed_count, cap) — for the landing-site widget


def try_claim_founding_slot(conn: Any, tenant_id: UUID | str) -> ClaimResult:
    """Atomically claim a founding slot for ``tenant_id`` WITHIN the caller's transaction.

    The ``claimed_count < cap`` predicate + the row lock make this race-safe: exactly
    ``cap`` claims ever succeed. On success records the audit row; ``tenant_id`` is UNIQUE,
    so a re-claim for the same tenant does NOT double-count (the spurious increment is
    rolled back in-txn). Returns ClaimResult(claimed, claimed_count)."""
    row = conn.execute(
        "UPDATE founding_tier_counter SET claimed_count = claimed_count + 1, "
        "last_claimed_at = now() WHERE id = 1 AND claimed_count < cap "
        "RETURNING claimed_count"
    ).fetchone()
    if row is None:
        # Cap reached. Distinguish "never claimed" from "this tenant ALREADY holds a slot"
        # (a re-claim at cap) via the audit table — so a founding tenant isn't told False.
        existing = conn.execute(
            "SELECT id FROM founding_tier_claims WHERE tenant_id = %s", (str(tenant_id),)
        ).fetchone()
        return ClaimResult(claimed=existing is not None, claimed_count=_current_count(conn))

    count = int(row["claimed_count"])
    inserted = conn.execute(
        "INSERT INTO founding_tier_claims (tenant_id, claimed_at) VALUES (%s, now()) "
        "ON CONFLICT (tenant_id) DO NOTHING RETURNING id",
        (str(tenant_id),),
    ).fetchone()
    if inserted is None:
        # This tenant already holds a slot — undo the spurious increment (same txn) so a
        # re-claim never double-counts. The tenant still HAS a founding slot.
        conn.execute(
            "UPDATE founding_tier_counter SET claimed_count = claimed_count - 1 WHERE id = 1"
        )
        return ClaimResult(claimed=True, claimed_count=count - 1)
    return ClaimResult(claimed=True, claimed_count=count)


def release_founding_slot(conn: Any, tenant_id: UUID | str) -> None:
    """AUDIT-ONLY release: stamp ``released_at`` on the tenant's claim. The counter is
    NEVER decremented (no-reopen-founding policy) — a churned founding slot stays counted,
    so the tier can't be quietly reopened to the detriment of original founding tenants."""
    conn.execute(
        "UPDATE founding_tier_claims SET released_at = now() "
        "WHERE tenant_id = %s AND released_at IS NULL",
        (str(tenant_id),),
    )


def get_founding_status(conn: Any) -> FoundingStatus:
    """Read the counter. public_count = min(claimed_count, cap) for the widget."""
    row = conn.execute(
        "SELECT claimed_count, cap FROM founding_tier_counter WHERE id = 1"
    ).fetchone()
    if row is None:
        # The sentinel row is seeded by migration 104; its absence means an un-migrated DB.
        raise RuntimeError("founding_tier_counter sentinel row (id=1) is missing")
    count = int(row["claimed_count"])
    cap = int(row["cap"])
    return FoundingStatus(
        claimed_count=count,
        remaining=max(0, cap - count),
        cap=cap,
        all_claimed=count >= cap,
        public_count=min(count, cap),
    )


def _current_count(conn: Any) -> int:
    row = conn.execute("SELECT claimed_count FROM founding_tier_counter WHERE id = 1").fetchone()
    return int(row["claimed_count"]) if row else 0
