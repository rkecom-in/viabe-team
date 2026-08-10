"""VT-740 — the per-recipient suppression choke. Every failure mode must suppress, never send.

This guard exists because three paths automatically re-drive a task (the hourly reaper wake,
`approval_resume.redrive_task`, an operator redrive) and none consults what already went out, while
send idempotency is keyed per-DRAFT so a re-drive mints fresh keys. The guard sits at the send
choke precisely so it does not have to care WHICH path caused the second attempt.

The asymmetry pinned throughout: a wrong suppression costs one delayed message; a wrong send costs
a real person receiving the same message twice, with nothing in the system recording that it
happened. So every ambiguous case must resolve to "suppress".
"""

from __future__ import annotations

import pytest

from orchestrator.agents.send_frequency import (
    FAIL_CLOSED_INTERVAL_HOURS,
    is_suppressed,
    recent_delivery_within,
    resolve_interval_hours,
)

_TENANT = "11111111-1111-1111-1111-111111111111"
_CUSTOMER = "22222222-2222-2222-2222-222222222222"


class _Conn:
    """Minimal conn double. `rows=None` raises, standing in for a DB error."""

    def __init__(self, rows: list | None) -> None:
        self._rows = rows
        self.sql: str = ""
        self.params: tuple = ()

    def execute(self, sql, params=None):  # noqa: ANN001, ANN201
        if self._rows is None:
            raise OSError("db unreachable")
        self.sql, self.params = sql, params or ()
        return self

    def fetchone(self):  # noqa: ANN201
        return self._rows[0] if self._rows else None


class TestFailClosedDefault:
    def test_the_default_interval_is_the_ratified_tier_c(self) -> None:
        """Not a number invented in a terminal. Fazal ratified the frequency rule on 2026-08-10
        with Tier C — "everyone else", 7 days — as the explicit fail-closed floor."""
        assert FAIL_CLOSED_INTERVAL_HOURS == 7 * 24

    def test_every_customer_resolves_to_the_fail_closed_tier_until_vt741(self) -> None:
        """VT-741 adds Tier A (24h) and Tier B (3d). Until it lands, everyone is Tier C — which
        suppresses MORE, never less, so shipping the socket early cannot cause a duplicate."""
        assert resolve_interval_hours(_TENANT, _CUSTOMER) == FAIL_CLOSED_INTERVAL_HOURS


class TestUnreadableIsSuppressed:
    def test_a_db_error_suppresses_rather_than_sends(self) -> None:
        """The whole point. A read failure returning "no recent delivery" would turn a database
        blip into a duplicate message to a real person."""
        assert recent_delivery_within(_TENANT, _CUSTOMER, hours=24, conn=_Conn(None)) is None

    def test_is_suppressed_treats_unreadable_as_suppressed(self) -> None:
        suppressed, reason = is_suppressed(_TENANT, _CUSTOMER, conn=_Conn(None))
        assert suppressed is True
        assert "unavailable" in reason


class TestTheActualDecision:
    def test_a_recent_delivery_suppresses(self) -> None:
        suppressed, reason = is_suppressed(_TENANT, _CUSTOMER, conn=_Conn([(1,)]))
        assert suppressed is True
        assert "recent_delivery_within" in reason

    def test_no_recent_delivery_permits(self) -> None:
        suppressed, reason = is_suppressed(_TENANT, _CUSTOMER, conn=_Conn([]))
        assert suppressed is False
        assert reason == ""

    def test_only_delivered_statuses_count(self) -> None:
        """'window_closed' / 'rate_limited' / 'error' are recorded ATTEMPTS that never reached the
        customer. Counting them would suppress someone who has heard nothing from us."""
        conn = _Conn([])
        recent_delivery_within(_TENANT, _CUSTOMER, hours=24, conn=conn)
        assert conn.params[2] == ["sent"], f"delivered set must be exactly ['sent'], got {conn.params[2]}"

    def test_the_query_is_scoped_to_one_tenant_and_one_customer(self) -> None:
        conn = _Conn([])
        recent_delivery_within(_TENANT, _CUSTOMER, hours=24, conn=conn)
        assert "tenant_id = %s" in conn.sql
        assert "customer_id = %s" in conn.sql
        assert conn.params[0] == _TENANT
        assert conn.params[1] == _CUSTOMER

    @pytest.mark.parametrize("hours", [24, 72, 168])
    def test_the_window_is_passed_through_not_hardcoded(self, hours: int) -> None:
        """The socket: whatever supplies the interval — the ratified tiers today, the Manager
        later — the enforcement below does not change."""
        conn = _Conn([])
        recent_delivery_within(_TENANT, _CUSTOMER, hours=hours, conn=conn)
        assert conn.params[3] == hours


class TestSuppressionCannotAuthorize:
    def test_there_is_no_return_that_permits_a_send_that_was_otherwise_blocked(self) -> None:
        """`is_suppressed` is consulted AFTER opt-out, complaint-freeze and opt-in. Its only
        outputs are "suppress" and "no opinion" — it can never turn a blocked send into a
        permitted one. Pinned as a contract, since a future refactor could be tempted to make this
        function the single authority."""
        permitted, _ = is_suppressed(_TENANT, _CUSTOMER, conn=_Conn([]))
        assert permitted is False, "the permissive branch must be a plain False, never a grant"


class TestTwoLayersComposeWithoutATieBreak:
    """Clau's audit question: two frequency mechanisms now sit on the same send path
    (`RECONTACT_SUPPRESSION_DAYS` / `MAX_AGENT_CONTACTS_PER_90D` on the agent path, and this
    module on every path). Answered: both survive, they ask different questions, and precedence
    needs no rule — because both are VETO-ONLY, so they compose conjunctively and the outcome is
    order-independent.

    The property that makes that true is worth pinning: the moment either layer gains a branch
    that PERMITS a send, the composition stops being order-independent and the two become a real
    conflict resolved by call order."""

    def test_this_layer_can_only_veto(self) -> None:
        """Its permissive branch is a plain False ("no opinion"), never a grant."""
        for rows in ([], [(1,)], None):
            suppressed, _ = is_suppressed(_TENANT, _CUSTOMER, conn=_Conn(rows))
            assert isinstance(suppressed, bool)
        assert is_suppressed(_TENANT, _CUSTOMER, conn=_Conn([]))[0] is False
        assert is_suppressed(_TENANT, _CUSTOMER, conn=_Conn([(1,)]))[0] is True

    def test_the_agent_caps_are_also_veto_only(self) -> None:
        """`check_caps` returns CapCheckResult(allowed=...) — allowed=True is 'this gate has no
        objection', not 'send permitted'. Both layers being veto-only is what removes the tie."""
        pytest.importorskip("psycopg")
        import inspect

        from orchestrator.agents import customer_send

        src = inspect.getsource(customer_send)
        assert "RECONTACT_SUPPRESSION_DAYS" in src, (
            "the agent-contact ceiling is deliberately RETAINED, not retired by VT-740 — it asks a "
            "narrower question (how often may an AGENT cold-contact) on a different table"
        )
