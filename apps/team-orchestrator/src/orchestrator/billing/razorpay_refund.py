"""VT-93 — Razorpay refund / subscription-cancel seam. STUB until live.

# NEEDS-FAZAL: Razorpay LIVE keys + cutover (VT-89). This module is the single
seam ``refund_executor.execute_refund`` calls; the live implementation drops in
here behind the same :class:`RazorpayClient` protocol. NEVER live-provision keys
in this repo.

Fail-closed by construction: the default production client REFUSES to act unless
``TEAM_RAZORPAY_LIVE=1`` AND a live client is wired (it is not, until VT-89), so an
accidental deploy raises rather than moving real money. Tests/canaries inject a
fake client (success / failure / cancel-failure) — the real-PG state machine is
exercised without any vendor call.

Per-payment refunds (Razorpay refunds per payment_id) need the charge ledger that
VT-89's webhook builds; until then the executor refunds the running total
(``subscriptions.cumulative_fees_paid_paise``) as a single call. The protocol is
stable across that swap.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol


class RazorpayRefundError(RuntimeError):
    """Any failure of the Razorpay refund/cancel seam (transient or permanent)."""


class RazorpayNotConfigured(RazorpayRefundError):
    """Live Razorpay is not provisioned (NEEDS-FAZAL). Fail-closed default."""


@dataclass(frozen=True)
class RefundResult:
    ok: bool
    refund_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CancelResult:
    ok: bool
    raw: dict[str, Any] = field(default_factory=dict)


class RazorpayClient(Protocol):
    """The seam contract. ``idempotency_key`` is a deterministic per-(tenant,
    reason, step) key so a network retry cannot double-refund once live."""

    def refund(
        self, *, amount_paise: int, idempotency_key: str, subscription_id: str | None
    ) -> RefundResult: ...

    def cancel_subscription(
        self, subscription_id: str | None, *, idempotency_key: str
    ) -> CancelResult: ...


class _LiveRefusingClient:
    """Default production client — refuses every call. No money moves without
    Fazal's explicit cutover (Pillar 7 honest; never silently no-op-success)."""

    def refund(
        self, *, amount_paise: int, idempotency_key: str, subscription_id: str | None
    ) -> RefundResult:
        raise RazorpayNotConfigured(
            "Razorpay refund is not live-provisioned "
            "(NEEDS-FAZAL: TEAM_RAZORPAY_LIVE + keys; VT-89 cutover)"
        )

    def cancel_subscription(
        self, subscription_id: str | None, *, idempotency_key: str
    ) -> CancelResult:
        raise RazorpayNotConfigured(
            "Razorpay subscription-cancel is not live-provisioned (NEEDS-FAZAL; VT-89 cutover)"
        )


def default_razorpay_client() -> RazorpayClient:
    """Return the production client. Fail-closed: even with the live flag set,
    no client is wired yet (VT-89), so this raises rather than guess."""
    if os.environ.get("TEAM_RAZORPAY_LIVE") == "1":
        # NEEDS-FAZAL: construct + return the live razorpay client here at cutover.
        raise RazorpayNotConfigured(
            "TEAM_RAZORPAY_LIVE=1 but no live Razorpay client is wired (NEEDS-FAZAL; VT-89)"
        )
    return _LiveRefusingClient()
