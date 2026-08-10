"""VT-741 — 'read' must survive to the ledger, and must not be able to erase a failure.

Fazal's ratified frequency rule has a middle tier keyed on "read or clicked or replied". Twilio
already sends the read callback and we already receive it; `_DELIVERY_STATE_MAP` was mapping
`"read" -> "delivered"`, discarding it one line before it would persist.

Fixing the map alone would have changed nothing observable, which is the trap these tests exist to
pin: a message produces TWO callbacks (delivered, then read) and the reconcile UPDATE is
first-write-wins, so the 'delivered' callback claims the row and the 'read' callback matches
nothing. The upgrade predicate is the load-bearing half.

The asymmetry, deliberately: losing a read costs a customer one tier of politeness; letting a late
positive callback overwrite a recorded FAILURE would tell us a message landed when it did not.
"""

from __future__ import annotations

import pytest

# orchestrator.agents.customer_send imports psycopg at module load; the dep-less smoke suite
# (which mirrors CI 'test') does not install it.
pytest.importorskip("psycopg")

from orchestrator.agents.customer_send import (  # noqa: E402
    _DELIVERY_FAILURE_STATES,
    _DELIVERY_STATE_MAP,
    _DELIVERY_UPGRADE_FROM_TO,
)


class TestReadIsItsOwnState:
    def test_read_is_no_longer_folded_into_delivered(self) -> None:
        """The one-line defect. Tier B rests entirely on this mapping."""
        assert _DELIVERY_STATE_MAP["read"] == "read"

    def test_the_other_states_are_untouched(self) -> None:
        assert _DELIVERY_STATE_MAP["delivered"] == "delivered"
        assert _DELIVERY_STATE_MAP["failed"] == "failed"
        assert _DELIVERY_STATE_MAP["undelivered"] == "undelivered"

    def test_read_is_not_a_failure(self) -> None:
        """It must not start firing the reviewer outbound_failure alert."""
        assert "read" not in _DELIVERY_FAILURE_STATES


class TestOnlyOneUpgradeIsPermitted:
    def test_the_upgrade_is_exactly_delivered_to_read(self) -> None:
        assert _DELIVERY_UPGRADE_FROM_TO == ("delivered", "read")

    def test_no_failure_state_is_upgradable(self) -> None:
        """A recorded delivery failure must be unreachable by any later callback. If this pair ever
        widens to include a failure state, a message that never arrived starts reporting as read."""
        source, target = _DELIVERY_UPGRADE_FROM_TO
        assert source not in _DELIVERY_FAILURE_STATES
        assert target not in _DELIVERY_FAILURE_STATES

    def test_the_upgrade_target_is_a_real_mapped_state(self) -> None:
        """Guards the half-migration: widening the CHECK without fixing the map, or vice versa,
        leaves an upgrade that can never fire."""
        assert _DELIVERY_UPGRADE_FROM_TO[1] in set(_DELIVERY_STATE_MAP.values())


class TestTheUpdatePredicate:
    def test_the_reconcile_update_permits_the_upgrade(self) -> None:
        """Pinned as source text because the behavioural proof needs two sequential callbacks
        against a live row (realdb's job). What must never regress silently is that the statement
        stopped being purely `delivery_status IS NULL` — with only that predicate, every read
        callback in production is a no-op and the tier is fed by nothing."""
        import inspect

        from orchestrator.agents import customer_send

        src = inspect.getsource(customer_send.reconcile_customer_send_delivery)
        assert "delivery_status IS NULL" in src, "first-write-wins must still hold for new rows"
        assert "OR (%s = %s AND delivery_status = %s)" in src, (
            "the delivered->read upgrade predicate is what makes the read signal reachable at all"
        )
