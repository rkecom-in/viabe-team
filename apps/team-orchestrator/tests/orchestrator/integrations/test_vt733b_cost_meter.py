"""VT-733 slice B — the non-LLM cost meter.

The contract these pin is mostly about HONESTY, because slice C's repricing brief is built on this
table: an unknown rate must record as zero-flagged-estimated (never an invented number), a vendor's
own "estimated" flag must ride through to the row, and a metering failure must never break the turn
that already spent the money.

No DB: the insert is stubbed, so what is tested is the pricing + flagging decision.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from orchestrator.integrations import cost_meter


@pytest.fixture(autouse=True)
def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(cost_meter, "_insert", lambda **kw: rows.append(kw))
    return rows


def test_known_rate_prices_the_event(_capture: list[dict[str, Any]]) -> None:
    cost_meter.record_integration_cost(
        tenant_id="t-1", vendor="voyage", unit="embedding_1k_tokens", quantity=10
    )
    row = _capture[0]
    assert row["vendor"] == "voyage"
    assert row["quantity"] == Decimal("10")
    assert row["cost_usd"] == row["unit_rate_usd"] * Decimal("10")
    assert row["cost_usd"] > 0
    assert row["is_estimated"] is False  # a contracted rate


def test_vendor_estimated_flag_rides_through(_capture: list[dict[str, Any]]) -> None:
    """Twilio's WhatsApp pricing is country/category dependent — flagged estimated until an invoice
    is reconciled, and the ROW must carry that so the rollup can separate it."""
    cost_meter.record_integration_cost(
        tenant_id="t-1", vendor="twilio", unit="template_message", quantity=19
    )
    assert _capture[0]["is_estimated"] is True


def test_unknown_vendor_unit_records_quantity_at_zero_flagged_estimated(
    _capture: list[dict[str, Any]],
) -> None:
    """The alternative — inventing a rate — would put a fabricated number into the repricing input.
    Zero is the honest cost when we do not know the price; the flag is what stops that zero from
    reading as 'this was free'."""
    cost_meter.record_integration_cost(
        tenant_id="t-1", vendor="brand-new-vendor", unit="widgets", quantity=7
    )
    row = _capture[0]
    assert row["quantity"] == Decimal("7")  # the part we cannot reconstruct later is kept
    assert row["cost_usd"] == Decimal("0")
    assert row["is_estimated"] is True


def test_caller_can_force_estimated_when_the_QUANTITY_is_a_guess(
    _capture: list[dict[str, Any]],
) -> None:
    """A contracted rate applied to an estimated quantity is still an estimate."""
    cost_meter.record_integration_cost(
        tenant_id="t-1", vendor="voyage", unit="embedding_1k_tokens", quantity=3,
        is_estimated=True,
    )
    assert _capture[0]["is_estimated"] is True


def test_metering_failure_never_breaks_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """CL-122. The vendor call already happened and the money is already spent — a ledger write
    failure must not roll that back or raise into the caller."""
    def _boom(**_kw: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(cost_meter, "_insert", _boom)
    cost_meter.record_integration_cost(tenant_id="t-1", vendor="twilio", unit="template_message")


def test_platform_level_event_needs_no_tenant(_capture: list[dict[str, Any]]) -> None:
    cost_meter.record_integration_cost(tenant_id=None, vendor="apify", unit="actor_run")
    assert _capture[0]["tenant_id"] is None


def test_external_ref_is_kept_for_invoice_reconciliation(_capture: list[dict[str, Any]]) -> None:
    """Reconciling a month of rows against the real invoice is what turns these estimates into
    measurements — it needs the vendor-side id."""
    cost_meter.record_integration_cost(
        tenant_id="t-1", vendor="twilio", unit="template_message", external_ref="SM123",
    )
    assert _capture[0]["external_ref"] == "SM123"


def test_lookup_rate_is_readable_on_its_own() -> None:
    rate, estimated = cost_meter.lookup_rate("sarvam", "asr_seconds")
    assert rate > 0 and estimated is True
    assert cost_meter.lookup_rate("nope", "nope") == (Decimal("0"), True)
