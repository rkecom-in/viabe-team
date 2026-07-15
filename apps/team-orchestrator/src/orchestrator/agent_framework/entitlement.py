"""Entitlement — the SOFT, COMPUTED enablement seam for a billable module (D-ENT).

CL-2026-07-15-entitlement-computed. A module's ``AgentManifest.entitlement_key`` is a SELF-DESCRIBING
SKU declaration ("this agent is billable; SKU = X") — NOT a price and NOT a hard gate. Entitlement is
COMPUTED from the EXISTING billing/metering substrate (VT-619 per-tenant×agent metering, migration
171), and it is SOFT: it NEVER hard-blocks and NEVER hardcodes a price (no ₹5000 in code).

Policy this seam implements:
  - A manifest with NO ``entitlement_key`` is a FREE capability → always entitled.
  - Otherwise the predicate is IN-TRIAL **OR** ACTIVE-PAID, read from billing at activation time.
  - Pre-launch, billing is not live, so this DEFAULTS TO SOFT-OPEN (returns ``True``) — a not-yet-wired
    billing backend must never block E2E. The ``TODO`` in ``check_entitlement`` marks exactly where the
    metering read wires in.

Any billing import is LAZY (keeps the framework package dep-less-smoke safe).
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from orchestrator.agent_framework.manifest import AgentManifest

logger = logging.getLogger("orchestrator.agent_framework.entitlement")


def check_entitlement(
    manifest: AgentManifest,
    tenant_id: UUID | str,
    *,
    now: datetime | None = None,
) -> bool:
    """Is ``tenant_id`` entitled to run the module ``manifest`` describes? SOFT + COMPUTED.

    Returns ``True`` for a free capability (no ``entitlement_key``). For a billable module the
    entitlement is COMPUTED (IN-TRIAL OR ACTIVE-PAID) from the billing/metering substrate — but until
    billing is live this is SOFT-OPEN (``True``) by design; it NEVER hard-blocks and NEVER encodes a
    price. ``now`` is injectable for deterministic trial-window computation once the metering read is
    wired (unused while soft-open).
    """
    if manifest.entitlement_key is None:
        return True  # free capability — no SKU declared, always entitled.

    # TODO(VT-619 wiring — do this AT ACTIVATION, when billing is live): replace the soft-open below
    # with the real computation from the per-tenant×agent metering substrate, e.g.
    #     from orchestrator.billing.usage_meter import entitlement_status
    #     status = entitlement_status(tenant_id, sku=manifest.entitlement_key, now=now)
    #     return status.in_trial or status.active_paid
    # It stays SOFT (compute-and-admit): NEVER hard-block, NEVER hardcode a price. Pre-launch billing
    # is not live, so admit the billable module rather than block E2E on a not-yet-wired backend.
    logger.debug(
        "entitlement: SOFT-OPEN sku=%s tenant=%s (billing not yet wired — returning True)",
        manifest.entitlement_key,
        tenant_id,
    )
    return True


__all__ = ["check_entitlement"]
