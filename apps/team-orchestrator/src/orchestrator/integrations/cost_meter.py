"""VT-733 slice B — the metering seam for everything that is NOT an LLM call.

Fazal 2026-08-05: "we can know how much is being consumed by the Manager, the specialists and any
other integrations we have." Mig 173 has metered LLM calls per-call since VT-619; Twilio messages,
Voyage embeddings, Sarvam ASR, Apify runs and ScrapingBee requests have never been metered at all —
so every per-tenant cost number to date has been an undercount of unknown size.

Deliberately mirrors ``orchestrator.llm.ledger.record_llm_call``:
  * one row per billable event, cost computed at write time,
  * FAIL-SOFT — metering never breaks a turn (CL-122). A vendor call that succeeded must not be
    rolled back because we could not write its cost row,
  * the applied rate is persisted ON the row, so a later rate edit never rewrites history.

ESTIMATED is first-class. Some vendors bill per-unit-opaque (an Apify actor run's compute units are
not knowable at call time), so those rows record the units we DO know at our best-known rate and
carry ``is_estimated=True``. Slice C's repricing brief must report estimated and measured spend
SEPARATELY — a pricing decision built on estimates that look like measurements is precisely the
failure this row exists to prevent.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RATES_PATH = Path(__file__).resolve().parents[3] / "config" / "integration_rates.yaml"


@lru_cache(maxsize=1)
def _rates() -> dict[str, dict[str, dict[str, Any]]]:
    """The vendor rate table, read once. A missing/unreadable file yields an EMPTY table rather than
    a raise: metering is best-effort, and an unpriced event still records its QUANTITY, which is the
    part we cannot reconstruct later."""
    try:
        import yaml

        with open(_RATES_PATH, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001 — an unreadable rate table must not break a live turn
        logger.warning("VT-733B: integration rate table unreadable; recording quantities only")
        return {}


def lookup_rate(vendor: str, unit: str) -> tuple[Decimal, bool]:
    """``(usd_per_unit, is_estimated)`` for a vendor+unit.

    An UNKNOWN pair returns ``(0, True)`` — zero cost, flagged estimated. Zero is the honest value
    (we do not know the price) and the estimated flag is what stops that zero from being read as
    "this was free" in the rollup. The alternative — inventing a rate — would put a fabricated number
    into the repricing input.
    """
    entry = (_rates().get(vendor) or {}).get(unit)
    if not isinstance(entry, dict):
        logger.info("VT-733B: no rate for vendor=%s unit=%s — recording quantity at 0", vendor, unit)
        return Decimal("0"), True
    return Decimal(str(entry.get("usd", 0) or 0)), bool(entry.get("estimated", False))


def record_integration_cost(
    *,
    tenant_id: Any,
    vendor: str,
    unit: str,
    quantity: float | int | Decimal = 1,
    agent: str | None = None,
    call_site: str | None = None,
    external_ref: str | None = None,
    is_estimated: bool | None = None,
) -> None:
    """Record ONE non-LLM billable event. Never raises.

    ``quantity`` is in the unit's own terms (messages, 1k-token blocks, audio seconds, actor runs,
    requests). ``is_estimated`` defaults to the rate table's own flag; a caller may force it True
    when the QUANTITY itself is an estimate even though the rate is contracted.

    ``external_ref`` is the vendor-side id (a Twilio SID, an Apify run id) so a month of rows can be
    reconciled against the actual invoice — the check that turns these estimates into measurements.
    """
    try:
        rate, rate_estimated = lookup_rate(vendor, unit)
        qty = Decimal(str(quantity or 0))
        cost = rate * qty
        estimated = rate_estimated if is_estimated is None else bool(is_estimated)
        _insert(
            tenant_id=tenant_id,
            vendor=vendor,
            unit=unit,
            quantity=qty,
            unit_rate_usd=rate,
            cost_usd=cost,
            is_estimated=estimated,
            agent=agent,
            call_site=call_site,
            external_ref=external_ref,
        )
    except Exception:  # noqa: BLE001 — CL-122: metering never breaks a turn
        logger.warning("VT-733B record_integration_cost swallowed (best-effort)", exc_info=True)


def _insert(**params: Any) -> None:
    """Write the row on the privileged pool (the seam owns the write; there is no write policy —
    same posture as llm_call_events). Lazy import so this module stays import-light."""
    from orchestrator.graph import get_pool

    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO integration_cost_events
                (tenant_id, vendor, unit, quantity, unit_rate_usd, cost_usd,
                 is_estimated, agent, call_site, external_ref)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(params["tenant_id"]) if params.get("tenant_id") is not None else None,
                params["vendor"],
                params["unit"],
                params["quantity"],
                params["unit_rate_usd"],
                params["cost_usd"],
                params["is_estimated"],
                params.get("agent"),
                params.get("call_site"),
                params.get("external_ref"),
            ),
        )


__all__ = ["lookup_rate", "record_integration_cost"]
