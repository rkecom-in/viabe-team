"""VT-140 — shared seed / teardown helpers for the Sprint 1+2 E2E harness.

Synthetic-only (CL-422): every row written here is fabricated test data —
business profile, customers, campaign cohort, pending run, attribution. NO
real customer data ever touches dev (hard constraint until VT-231 prod-Mumbai).

CL-390 discipline: phones are synthetic ``+9199…`` numbers; the harness logs
ids / counts / SIDs only, never names or phone bodies.

Two tenants are always seeded:
  - T1: the tenant the loop drives (business profile + cohort + inbound).
  - T2: a DECOY tenant with its own customers / campaign / attribution. T2
        exists ONLY to prove ZERO cross-tenant leakage — every RLS-scoped read
        in the loop under T1's context must return T1 rows and never T2's.

Seeding uses a PRIVILEGED autocommit connection (RLS bypassed) so the harness
can stage rows for two tenants without juggling per-tenant contexts; the LOOP
under test reads through ``tenant_connection`` (RLS enforced), so the isolation
assertion is genuine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg

from orchestrator.utils.phone_token import hash_phone


@dataclass
class SeededTenant:
    """One synthetic tenant's seeded identity + cohort."""

    tenant_id: UUID
    run_id: UUID
    owner_phone: str
    whatsapp_number: str
    # customer_id -> opt_out_status ('subscribed' | 'opted_out' | 'blocked')
    customers: dict[UUID, str] = field(default_factory=dict)

    @property
    def subscribed_ids(self) -> list[UUID]:
        return [cid for cid, s in self.customers.items() if s == "subscribed"]

    @property
    def opted_out_ids(self) -> list[UUID]:
        return [cid for cid, s in self.customers.items() if s != "subscribed"]


@dataclass
class SeedResult:
    """The full seeded fixture: the driven tenant T1 + the decoy T2."""

    t1: SeededTenant
    t2: SeededTenant
    # campaign_id seeded for T2 (decoy) so the isolation assertion can probe
    # an attribution-bearing decoy campaign and prove it never surfaces.
    t2_campaign_id: UUID


def _synthetic_phone() -> str:
    """A synthetic +9199… number (CL-422 — fabricated, never a real customer)."""
    return f"+9199{uuid4().int % 10**8:08d}"


def _seed_tenant(
    conn: Any,
    *,
    business_name: str,
    n_subscribed: int,
    n_opted_out: int,
    seed_inbound: bool,
    ownership_verified: bool = True,
) -> SeededTenant:
    """Insert one tenants row + a pipeline_runs row + a customer cohort.

    The cohort is ``n_subscribed`` subscribed customers + ``n_opted_out``
    opted-out customers (CL-421 consent surface). One subscribed customer gets
    a recent ``last_inbound_at`` (drives the loop's "recent inbound" trigger).
    """
    owner_phone = _synthetic_phone()
    whatsapp_number = _synthetic_phone()
    row = conn.execute(
        """
        INSERT INTO tenants (business_name, plan_tier, phase, whatsapp_number,
                             owner_phone, verification_status, ownership_verified)
        VALUES (%s, 'founding', 'paid_active', %s, %s, 'gstin_verified', %s)
        RETURNING id
        """,
        (business_name, whatsapp_number, owner_phone, ownership_verified),
    ).fetchone()
    tenant_id = UUID(str(row[0] if not isinstance(row, dict) else row["id"]))

    # VT-460: the agent + campaign customer-send paths now pass the shared onboarded (Gate-0) +
    # WABA-live pre-gate. A real tenant reaching a send has all four activation signals plus a live
    # WABA; seed them so the e2e loop reaches the campaign send (else the pre-gate blocks it and
    # campaign_messages records 0 rows). journey-complete + gstin_verified (above) + ≥1 enabled
    # connector + ≥1 customer (the cohort below) + a 'live' WABA.
    conn.execute(
        "INSERT INTO onboarding_journey (tenant_id, status, completed_at) "
        "VALUES (%s, 'complete', now())",
        (str(tenant_id),),
    )
    conn.execute(
        "INSERT INTO tenant_connector_status (tenant_id, connector_id, enabled, last_status, "
        "last_ingested_date) VALUES (%s, %s, TRUE, 'ok', CURRENT_DATE)",
        (str(tenant_id), f"conn-{tenant_id.hex[:8]}"),
    )
    conn.execute(
        "INSERT INTO tenant_whatsapp_accounts (tenant_id, status, phone_number) "
        "VALUES (%s, 'live', %s)",
        (str(tenant_id), _synthetic_phone()),
    )

    rrow = conn.execute(
        """
        INSERT INTO pipeline_runs (tenant_id, run_type, status)
        VALUES (%s, 'orchestrator', 'running')
        RETURNING id
        """,
        (str(tenant_id),),
    ).fetchone()
    run_id = UUID(str(rrow[0] if not isinstance(rrow, dict) else rrow["id"]))

    customers: dict[UUID, str] = {}
    recent = datetime.now(UTC) - timedelta(hours=2)
    for i in range(n_subscribed):
        phone = _synthetic_phone()
        crow = conn.execute(
            """
            INSERT INTO customers (tenant_id, display_name, phone_e164,
                                   last_inbound_at, opt_out_status, source)
            VALUES (%s, %s, %s, %s, 'subscribed', 'vt140-synthetic')
            RETURNING id
            """,
            (
                str(tenant_id),
                f"Synthetic Customer {i}",
                phone,
                recent if i == 0 and seed_inbound else None,
            ),
        ).fetchone()
        cid = UUID(str(crow[0] if not isinstance(crow, dict) else crow["id"]))
        customers[cid] = "subscribed"
        # VT-301 / CL-429: a business-initiated send now requires a recorded
        # WhatsApp opt-in. Subscribed customers in the real flow opted in via
        # the inbound/hook path (VT-287); seed the matching consent row so the
        # send-gate lets the loop reach them. CL-390: only the token is stored.
        conn.execute(
            """
            INSERT INTO record_of_consent
                (tenant_id, phone_token, consent_text_version, consent_method, source)
            VALUES (%s, %s, 'wa_inbound_optin_v0', 'wa_inbound_optin', 'vt140-synthetic')
            ON CONFLICT (tenant_id, phone_token) DO NOTHING
            """,
            (str(tenant_id), hash_phone(phone)),
        )

    for i in range(n_opted_out):
        crow = conn.execute(
            """
            INSERT INTO customers (tenant_id, display_name, phone_e164,
                                   opt_out_status, source)
            VALUES (%s, %s, %s, 'opted_out', 'vt140-synthetic')
            RETURNING id
            """,
            (
                str(tenant_id),
                f"Synthetic OptedOut {i}",
                _synthetic_phone(),
            ),
        ).fetchone()
        cid = UUID(str(crow[0] if not isinstance(crow, dict) else crow["id"]))
        customers[cid] = "opted_out"

    return SeededTenant(
        tenant_id=tenant_id,
        run_id=run_id,
        owner_phone=owner_phone,
        whatsapp_number=whatsapp_number,
        customers=customers,
    )


def _seed_decoy_campaign(conn: Any, t2: SeededTenant) -> UUID:
    """Seed a decoy campaign + recipients + an attribution row for T2.

    These rows MUST never surface under T1's RLS context. The decoy campaign
    is given an attribution row so the cross-tenant attribution probe is real.
    """
    plan_dict = {
        "version": "1.0",
        "status": "proposed",
        "tenant_id": str(t2.tenant_id),
        "run_id": str(t2.run_id),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    from psycopg.types.json import Jsonb

    crow = conn.execute(
        """
        INSERT INTO campaigns (tenant_id, run_id, plan_json, status, generated_at)
        VALUES (%s, %s, %s, 'sent', now())
        RETURNING id
        """,
        (str(t2.tenant_id), str(t2.run_id), Jsonb(plan_dict)),
    ).fetchone()
    campaign_id = UUID(str(crow[0] if not isinstance(crow, dict) else crow["id"]))

    for cid in t2.subscribed_ids:
        conn.execute(
            """
            INSERT INTO campaign_recipients (campaign_id, customer_id, tenant_id)
            VALUES (%s, %s, %s)
            """,
            (str(campaign_id), str(cid), str(t2.tenant_id)),
        )

    # A decoy attribution so the isolation probe sees a real (hidden) snapshot.
    a_customer = next(iter(t2.subscribed_ids), None)
    conn.execute(
        """
        INSERT INTO attributions (tenant_id, campaign_id, customer_id,
                                  attributed_paise, attribution_method,
                                  attribution_confidence)
        VALUES (%s, %s, %s, 999999, 'exact_match', 1.0)
        """,
        (str(t2.tenant_id), str(campaign_id), str(a_customer) if a_customer else None),
    )
    return campaign_id


def seed(dsn: str) -> SeedResult:
    """Seed T1 (driven) + T2 (decoy). Privileged autocommit connection.

    T1: 4 subscribed + 1 opted_out = 5 customers (matches the brief's "045"
    cohort intent: a mix of subscribed + 1 opted_out). One subscribed customer
    carries a recent inbound.
    T2: 2 subscribed + 1 opted_out, plus a sent campaign + attribution decoy.
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        t1 = _seed_tenant(
            conn,
            business_name="VT-140 E2E Synthetic T1",
            n_subscribed=4,
            n_opted_out=1,
            seed_inbound=True,
        )
        t2 = _seed_tenant(
            conn,
            business_name="VT-140 E2E Decoy T2",
            n_subscribed=2,
            n_opted_out=1,
            seed_inbound=False,
            ownership_verified=False,
        )
        t2_campaign_id = _seed_decoy_campaign(conn, t2)
    return SeedResult(t1=t1, t2=t2, t2_campaign_id=t2_campaign_id)


def teardown(dsn: str, result: SeedResult) -> None:
    """Delete every seeded row for both tenants.

    Order respects FK dependencies: attributions / campaign_messages /
    send_idempotency_keys / campaign_recipients / pending_approvals →
    campaigns → customers → checkpoints → pipeline_steps → pipeline_runs →
    subscriber_states → tenants. Best-effort: a partial teardown logs but does
    not raise (mirrors sprint1_e2e_smoke._finalise).
    """
    tenant_ids = [str(result.t1.tenant_id), str(result.t2.tenant_id)]
    run_ids = [str(result.t1.run_id), str(result.t2.run_id)]
    with psycopg.connect(dsn, autocommit=True) as conn:
        # Child rows keyed by tenant_id.
        for table in (
            "attributions",
            "campaign_messages",
            "send_idempotency_keys",
            "campaign_recipients",
            "pending_approvals",
            "campaigns",
            "customers",
            "subscriber_states",
        ):
            try:
                conn.execute(
                    f"DELETE FROM {table} WHERE tenant_id = ANY(%s)",
                    (tenant_ids,),
                )
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        # Checkpoint tables keyed by thread_id == run_id.
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            try:
                conn.execute(
                    f"DELETE FROM {table} WHERE thread_id = ANY(%s)",
                    (run_ids,),
                )
            except Exception:  # noqa: BLE001
                pass
        for table in ("pipeline_steps", "pipeline_runs"):
            try:
                conn.execute(
                    f"DELETE FROM {table} WHERE tenant_id = ANY(%s)",
                    (tenant_ids,),
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            conn.execute(
                "DELETE FROM tenants WHERE id = ANY(%s)", (tenant_ids,)
            )
        except Exception:  # noqa: BLE001
            pass
