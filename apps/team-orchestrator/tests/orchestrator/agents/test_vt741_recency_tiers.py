"""VT-741 — the recency tiers, their ORDER, and the click substrate they read.

Fazal re-specified the rule on 2026-08-13 (the earlier positional windows were a drafting error):

    Tier A: replied or clicked within 30 days        -> 24h
    Tier B: read / clicked / replied within 90 days  -> 3 days
    Tier C: everyone else                            -> 7 days

The load-bearing property is the ORDER. Tier A is a strict subset of Tier B — anyone who replied
or clicked inside 30 days also did so inside 90 — so evaluating B first returns a wrong answer for
the whole of A, with no error and no log line. Half of this file exists to make that impossible to
regress silently, which is why the order is asserted behaviourally (a customer who satisfies BOTH)
and not merely by reading the tuple.

The other invariant: this is a SUPPRESSION layer. No tier may make the veto weaker than "some
positive interval", and every unanswerable case must land on the LONGEST interval, not the
shortest.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from orchestrator.agents.send_frequency import (
    FAIL_CLOSED_INTERVAL_HOURS,
    TIER_A,
    TIER_B,
    TIER_C,
    EngagementSignals,
    is_suppressed,
    read_engagement_signals,
    resolve_interval_hours,
    resolve_tier,
)

_TENANT = "11111111-1111-1111-1111-111111111111"
_CUSTOMER = "22222222-2222-2222-2222-222222222222"

_DAY = 86400.0

# repo root: tests/orchestrator/agents/<file> -> agents -> orchestrator -> tests ->
# team-orchestrator -> apps -> repo
ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "migrations" / "201_vt741_customer_hook_link_clicks.sql"
DSR_PURGE = ROOT / "apps" / "team-orchestrator" / "src" / "orchestrator" / "dsr_purge.py"
HOOK_LINKS = (
    ROOT / "apps" / "team-orchestrator" / "src" / "orchestrator" / "integrations" / "hook_links.py"
)
SEND_FREQUENCY = (
    ROOT / "apps" / "team-orchestrator" / "src" / "orchestrator" / "agents" / "send_frequency.py"
)
CLICK_TABLE = "customer_hook_links"


class _SignalConn:
    """Conn double for the tier reads.

    ONE statement is issued per resolve: the three-signal engagement query. The customer's phone
    is supplied BY THE CALLER (send_whatsapp_template already resolved it), not looked up here —
    reading `customers` from this module was a VT-72 wrapper-layer violation and an avoidable round
    trip per recipient. `raise_on` makes a statement fail so the fail-closed paths can be exercised
    separately rather than as one undifferentiated "error".
    """

    def __init__(
        self,
        *,
        replied_days: float | None = None,
        clicked_days: float | None = None,
        read_days: float | None = None,
        phone: str | None = "+919000000001",
        raise_on: str | None = None,
        engagement_row: object = "default",
    ) -> None:
        self._replied, self._clicked, self._read = replied_days, clicked_days, read_days
        self._phone = phone
        self._raise_on = raise_on
        self._engagement_row = engagement_row
        self._pending: object = None
        self.statements: list[str] = []
        self.params: list[tuple] = []

    @staticmethod
    def _secs(days: float | None) -> float | None:
        return None if days is None else days * _DAY

    def execute(self, sql, params=None):  # noqa: ANN001, ANN201
        self.statements.append(sql)
        self.params.append(params or ())
        if "FROM customers" in sql:
            if self._raise_on == "phone":
                raise OSError("customers unreadable")
            self._pending = None if self._phone is None else {"phone_e164": self._phone}
        elif "replied_age_s" in sql:
            if self._raise_on == "engagement":
                raise OSError("engagement unreadable")
            self._pending = (
                {
                    "replied_age_s": self._secs(self._replied),
                    "clicked_age_s": self._secs(self._clicked),
                    "read_age_s": self._secs(self._read),
                }
                if self._engagement_row == "default"
                else self._engagement_row
            )
        else:  # the recent_delivery_within probe — "no recent delivery" unless overridden
            self._pending = None
        return self

    def fetchone(self):  # noqa: ANN201
        return self._pending


class TestOrderIsTheRule:
    """A must be evaluated BEFORE B. Asserted by behaviour, not by reading the tuple."""

    @pytest.mark.parametrize("signal", ["replied", "clicked"])
    @pytest.mark.parametrize("age_days", [0.0, 1.0, 10.0, 29.9])
    def test_a_customer_who_satisfies_both_a_and_b_gets_a(
        self, signal: str, age_days: float
    ) -> None:
        """THE regression this file exists for.

        A reply 10 days ago satisfies Tier A (<=30d) AND Tier B (<=90d). Evaluate B first and this
        returns 72 — no error, no log, just the most engaged cohort quietly capped at the middle
        interval. 24 is the only correct answer.
        """
        conn = _SignalConn(**{f"{signal}_days": age_days})
        assert resolve_interval_hours(_TENANT, _CUSTOMER, conn=conn) == 24
        assert resolve_tier(_TENANT, _CUSTOMER, conn=_SignalConn(**{f"{signal}_days": age_days})) \
            is TIER_A

    def test_the_subset_relation_that_makes_order_matter_is_real(self) -> None:
        """Pinned explicitly: every Tier-A signal is also a Tier-B signal, and A's window is
        inside B's. If a future edit breaks this, order stops mattering and the test above stops
        meaning what it says — so state it directly rather than leaving it implicit."""
        assert set(TIER_A.signals) <= set(TIER_B.signals)
        assert TIER_A.window_days is not None
        assert TIER_B.window_days is not None
        assert TIER_A.window_days < TIER_B.window_days

    def test_a_read_alone_never_reaches_tier_a(self) -> None:
        """Tier A is 'replied or clicked' — a READ is deliberately excluded. A read is passive and
        Twilio reports it for a message the customer may have dismissed; it earns the middle tier
        only. This is the asymmetry that makes A and B two tiers rather than two windows on one
        predicate."""
        assert "read" not in TIER_A.signals
        conn = _SignalConn(read_days=1.0)
        assert resolve_interval_hours(_TENANT, _CUSTOMER, conn=conn) == 3 * 24


class TestTheTiersThemselves:
    @pytest.mark.parametrize(
        ("kwargs", "expected_hours", "why"),
        [
            ({"replied_days": 5.0}, 24, "replied inside 30d -> A"),
            ({"clicked_days": 29.0}, 24, "clicked inside 30d -> A"),
            ({"replied_days": 31.0}, 3 * 24, "replied outside 30d but inside 90d -> B"),
            ({"clicked_days": 45.0}, 3 * 24, "clicked outside 30d but inside 90d -> B"),
            ({"read_days": 89.0}, 3 * 24, "read inside 90d -> B"),
            ({"replied_days": 91.0}, 7 * 24, "everything stale -> C"),
            ({"read_days": 400.0}, 7 * 24, "a year-old read is not engagement -> C"),
            ({}, 7 * 24, "zero history -> C"),
        ],
    )
    def test_the_ratified_mapping(self, kwargs: dict, expected_hours: int, why: str) -> None:
        conn = _SignalConn(**kwargs)
        assert resolve_interval_hours(_TENANT, _CUSTOMER, conn=conn) == expected_hours, why

    @pytest.mark.parametrize("boundary", [30, 90])
    def test_the_window_boundary_is_inclusive_on_both_tiers(self, boundary: int) -> None:
        """Exactly-at-the-boundary counts as inside. Stated so the choice is a decision on record
        rather than whichever way a `<` happened to be typed."""
        inside = _SignalConn(replied_days=float(boundary))
        outside = _SignalConn(replied_days=float(boundary) + 0.01)
        assert resolve_interval_hours(_TENANT, _CUSTOMER, conn=inside) != \
            resolve_interval_hours(_TENANT, _CUSTOMER, conn=outside)

    def test_the_strongest_signal_wins_when_several_are_present(self) -> None:
        """A customer who replied 3 days ago and was read 80 days ago is Tier A. First match wins
        means the FIRST TIER that matches, not the first signal that exists."""
        conn = _SignalConn(replied_days=3.0, read_days=80.0)
        assert resolve_tier(_TENANT, _CUSTOMER, conn=conn) is TIER_A


class TestFailClosedIsTierC:
    """Every unanswerable case must land on the LONGEST interval. Not the shortest, and not a
    plausible-looking middle."""

    def test_no_connection_is_tier_c(self) -> None:
        assert resolve_interval_hours(_TENANT, _CUSTOMER) == FAIL_CLOSED_INTERVAL_HOURS
        assert resolve_tier(_TENANT, _CUSTOMER) is TIER_C

    def test_an_engagement_read_error_is_tier_c(self) -> None:
        conn = _SignalConn(replied_days=1.0, raise_on="engagement")
        assert resolve_interval_hours(_TENANT, _CUSTOMER, conn=conn) == FAIL_CLOSED_INTERVAL_HOURS

    def test_a_missing_row_is_tier_c(self) -> None:
        conn = _SignalConn(engagement_row=None)
        assert resolve_interval_hours(_TENANT, _CUSTOMER, conn=conn) == FAIL_CLOSED_INTERVAL_HOURS

    def test_a_malformed_row_is_tier_c_not_an_empty_history(self) -> None:
        """A one-column row where three were expected is a BROKEN READ. Returning "no signals"
        would hide it behind an answer that looks exactly like a genuinely unengaged customer."""
        conn = _SignalConn(engagement_row=(1,))
        assert read_engagement_signals(_TENANT, _CUSTOMER, conn=conn, phone_e164=conn._phone) is None
        assert resolve_interval_hours(_TENANT, _CUSTOMER, conn=conn) == FAIL_CLOSED_INTERVAL_HOURS

    def test_a_customer_with_no_phone_loses_only_the_reply_signal(self) -> None:
        """No phone -> no wa_conversations token -> no reply signal. It must NOT poison the click
        and read signals, which are keyed on customer_id and are still perfectly readable."""
        conn = _SignalConn(phone=None, clicked_days=2.0)
        assert resolve_interval_hours(_TENANT, _CUSTOMER, conn=conn) == 24
        assert conn.params[0][1] is None, "the phone_token bind must be NULL, not a bogus token"

    def test_a_phone_lookup_error_loses_only_the_reply_signal(self) -> None:
        conn = _SignalConn(raise_on="phone", read_days=10.0)
        assert resolve_interval_hours(_TENANT, _CUSTOMER, conn=conn) == 3 * 24

    def test_the_catch_all_interval_IS_the_fail_closed_interval(self) -> None:
        """"everyone else" and "we could not tell" must be the same number. Two constants would
        drift, and the drift would only ever show up as a live over-send."""
        assert TIER_C.interval_hours == FAIL_CLOSED_INTERVAL_HOURS


class TestStillOnlyASuppressionLayer:
    def test_no_tier_can_disable_the_veto(self) -> None:
        """An interval of 0 would query an empty window, never match, and silently turn this whole
        module into a no-op — the one edit that could genuinely raise send rate."""
        for tier in (TIER_A, TIER_B, TIER_C):
            assert tier.interval_hours > 0

    def test_the_import_time_guard_rejects_a_zero_interval(self) -> None:
        """Proves the guard actually fires rather than merely being present."""
        import orchestrator.agents.send_frequency as sf

        original = sf._TIER_ORDER
        try:
            sf._TIER_ORDER = (
                sf.Tier(name="X", interval_hours=0, window_days=1, signals=("replied",)),
                sf.TIER_C,
            )
            with pytest.raises(RuntimeError, match="non-positive interval"):
                sf._assert_tier_order_invariants()
        finally:
            sf._TIER_ORDER = original
        sf._assert_tier_order_invariants()  # the real table still passes

    def test_the_import_time_guard_rejects_a_swapped_tier_order(self) -> None:
        import orchestrator.agents.send_frequency as sf

        original = sf._TIER_ORDER
        try:
            sf._TIER_ORDER = (sf.TIER_B, sf.TIER_A, sf.TIER_C)
            with pytest.raises(RuntimeError, match="must ascend"):
                sf._assert_tier_order_invariants()
        finally:
            sf._TIER_ORDER = original

    def test_the_permissive_branch_is_still_a_plain_false(self) -> None:
        """VT-740's contract, re-checked against the tiered body: the only outputs are "suppress"
        and "no opinion". A shorter interval narrows this layer's veto; it never grants a send that
        opt-out, complaint-freeze or the opt-in gate refused."""
        suppressed, reason = is_suppressed(_TENANT, _CUSTOMER, conn=_SignalConn(replied_days=1.0))
        assert suppressed is False
        assert reason == ""

    def test_the_tier_reaches_the_enforcement_window(self) -> None:
        """The socket is only worth having if the number it returns is the number enforced. A
        Tier-A customer must be probed over 24h, not the fail-closed 168h."""
        conn = _SignalConn(replied_days=1.0)
        is_suppressed(_TENANT, _CUSTOMER, conn=conn)
        probe = [p for s, p in zip(conn.statements, conn.params, strict=True)
                 if "send_idempotency_keys" in s]
        assert probe, "the enforcement probe never ran"
        assert probe[0][3] == 24, f"Tier A must enforce a 24h window, got {probe[0][3]}h"


class TestQueryShape:
    def test_every_signal_read_is_scoped_to_one_tenant(self) -> None:
        conn = _SignalConn(replied_days=1.0)
        read_engagement_signals(_TENANT, _CUSTOMER, conn=conn, phone_e164=conn._phone)
        engagement = next(s for s in conn.statements if "replied_age_s" in s)
        assert engagement.count("tenant_id = %s") == 3, "all three sub-selects must be tenant-scoped"
        assert conn.params[0][0] == _TENANT
        assert conn.params[0][2] == _TENANT
        assert conn.params[0][4] == _TENANT

    def test_the_reply_lookup_binds_the_SAME_token_the_inbound_path_wrote(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole reply signal hangs on this one equality.

        `integrations/customer_inbound.py::handle_customer_inbound` writes
        `wa_conversations.phone_token = hash_phone(customer_phone)` — a SALTED SHA-256. Any other
        derivation here (an unsalted hash, the raw number, a customer id) binds a token that
        matches no row, and the read returns NULL forever: Tier A quietly becomes unreachable via
        replies and nothing errors.

        The salt is monkeypatched rather than assumed, because an ABSENT salt makes `hash_phone`
        raise, the token fall back to None, and this assertion pass vacuously — which is exactly
        how a test like this rots into decoration.
        """
        monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt741_test_salt")
        from orchestrator.utils.phone_token import hash_phone

        phone = "+919000000001"
        conn = _SignalConn(phone=phone, replied_days=1.0)
        read_engagement_signals(_TENANT, _CUSTOMER, conn=conn, phone_e164=conn._phone)
        expected = hash_phone(phone)
        assert expected.startswith("phone_tok_")
        assert conn.params[0][1] == expected
        assert phone not in str(conn.params[0]), "the raw number must never reach the tier query"

    def test_the_reply_signal_is_keyed_on_phone_token_not_customer_id(self) -> None:
        """wa_conversations has no customer_id — it is keyed on hash_phone(phone_e164). This is
        precisely why the rule had to be re-specified as recency: there is no per-message inbound
        history to count positions in. Building one is VT-744, not this row."""
        conn = _SignalConn(replied_days=1.0)
        read_engagement_signals(_TENANT, _CUSTOMER, conn=conn, phone_e164=conn._phone)
        engagement = next(s for s in conn.statements if "replied_age_s" in s)
        assert "wa_conversations" in engagement
        assert "w.phone_token = %s" in engagement
        assert "wa_conversations w\n         WHERE w.tenant_id = %s AND w.customer_id" not in \
            engagement

    def test_only_a_read_delivery_status_counts_as_the_read_signal(self) -> None:
        """migration 200 made 'read' its own state. 'delivered' is not a read — counting it would
        promote every successfully-transported message to the middle tier."""
        conn = _SignalConn()
        read_engagement_signals(_TENANT, _CUSTOMER, conn=conn, phone_e164=conn._phone)
        engagement = next(s for s in conn.statements if "replied_age_s" in s)
        assert "a.delivery_status = 'read'" in engagement
        assert "'delivered'" not in engagement

    def test_ages_come_from_postgres_now_not_the_local_clock(self) -> None:
        """Same reasoning as VT-740's window: the send path and the ledger must not disagree about
        "30 days ago" because a container's clock drifted."""
        conn = _SignalConn()
        read_engagement_signals(_TENANT, _CUSTOMER, conn=conn, phone_e164=conn._phone)
        engagement = next(s for s in conn.statements if "replied_age_s" in s)
        assert engagement.count("now()") == 3
        assert "datetime" not in engagement

    def test_a_future_dated_signal_is_ignored_rather_than_trusted(self) -> None:
        """A negative age means a clock skew or a bad backfill, not engagement. Treating it as
        "0 days ago" would hand Tier A to a corrupt row."""
        signals = EngagementSignals(replied_age_days=-5.0)
        assert TIER_A.matches(signals) is False
        assert TIER_B.matches(signals) is False
        assert TIER_C.matches(signals) is True


class TestClickSubstrate:
    """Migration 201 — the customer-attributed click table Part A reads."""

    def test_the_click_table_name_matches_the_migration_and_hook_links(self) -> None:
        """send_frequency holds the table name as a LITERAL (it must stay import-light for the
        dep-less suite, and hook_links pulls in psycopg). This is the gate that keeps the literal
        from drifting away from the migration."""
        import orchestrator.agents.send_frequency as sf

        assert sf._CLICK_TABLE == CLICK_TABLE
        assert f"CREATE TABLE public.{CLICK_TABLE}" in MIGRATION.read_text(encoding="utf-8")
        assert f'CUSTOMER_CLICK_TABLE = "{CLICK_TABLE}"' in HOOK_LINKS.read_text(encoding="utf-8")
        assert CLICK_TABLE in sf._ENGAGEMENT_SQL

    def test_the_table_is_registered_in_the_dsr_purge_order(self) -> None:
        """DSR ANONYMIZES the tenants row, so no FK cascade ever cleans a tenant-scoped table.
        This omission already shipped on episodic_events and the L2 surfaces; it is not shipping a
        third time on a table whose entire content is customer behaviour."""
        tree = ast.parse(DSR_PURGE.read_text(encoding="utf-8"))
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_PURGE_ORDER"
        )
        assert assignment.value is not None
        assert CLICK_TABLE in ast.literal_eval(assignment.value)

    def test_it_carries_its_own_rls_plus_force_not_hook_links_deny_all(self) -> None:
        """A customer-attributed click table is a different privacy class from hook_links (which
        is deny-all and PII-free by design). Deny-all would be the wrong copy here: it would lock
        the tenant out of their own engagement data while doing nothing extra for the customer."""
        sql = MIGRATION.read_text(encoding="utf-8")
        assert f"ALTER TABLE public.{CLICK_TABLE} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE public.{CLICK_TABLE} FORCE ROW LEVEL SECURITY" in sql
        for operation in ("select", "insert", "update", "delete"):
            assert f"CREATE POLICY {CLICK_TABLE}_{operation}" in sql
        assert sql.count("app_current_tenant()") >= 4
        assert "USING (false)" not in sql, "deny-all is hook_links' posture, not this table's"

    def test_cross_tenant_binding_is_physically_impossible(self) -> None:
        """Same-tenant composite FKs on BOTH sides, so a wrong customer_id raises instead of
        writing a row that attributes one tenant's customer to another tenant's link."""
        sql = MIGRATION.read_text(encoding="utf-8")
        assert re.search(
            r"FOREIGN KEY \(tenant_id, token\)\s*\n\s*REFERENCES public\.hook_links "
            r"\(tenant_id, token\)", sql,
        )
        assert re.search(
            r"FOREIGN KEY \(tenant_id, customer_id\)\s*\n\s*REFERENCES public\.customers "
            r"\(tenant_id, id\)", sql,
        )
        assert "ADD CONSTRAINT hook_links_tenant_token_uniq UNIQUE (tenant_id, token)" in sql

    def test_the_token_stays_the_only_capability(self) -> None:
        """No customer, tenant, campaign or phone in the URL — the redirect resolves the binding
        server-side from the token, exactly as VT-288 already resolved the tenant."""
        source = HOOK_LINKS.read_text(encoding="utf-8")
        assert "WHERE token = %s AND tenant_id = %s" in source
        assert "secrets.token_urlsafe(16)" in source
        redirect = (
            ROOT / "apps" / "team-orchestrator" / "src" / "orchestrator" / "api" / "hook_links.py"
        ).read_text(encoding="utf-8")
        assert '@router.get("/r/{token}")' in redirect, "no second link scheme was introduced"
        assert "customer_id" not in redirect.split('@router.get("/r/{token}")')[1]

    def test_it_is_not_a_second_link_scheme(self) -> None:
        """One token space. A customer-bound link is a hook_links row AND a binding row sharing a
        token — the mint writes both in one transaction, so a link can never exist with its
        attribution silently missing."""
        sql = MIGRATION.read_text(encoding="utf-8")
        assert "token            TEXT NOT NULL UNIQUE" in sql
        source = HOOK_LINKS.read_text(encoding="utf-8")
        mint = source.split("def mint_customer_hook_link")[1].split("\ndef ")[0]
        assert "conn.transaction()" in mint
        assert "INSERT INTO hook_links" in mint
        assert "INSERT INTO {CUSTOMER_CLICK_TABLE}" in mint, (
            "the binding INSERT must go through the shared table constant, not a second literal"
        )

    def test_the_click_record_never_breaks_the_customer_facing_redirect(self) -> None:
        """The redirect is a customer waiting on a WhatsApp handoff. A broken click table must
        cost a metric, never a 500 — and losing a click pushes the tier DOWN (more suppression),
        which is the safe direction."""
        source = HOOK_LINKS.read_text(encoding="utf-8")
        record = source.split("def _record_customer_click")[1].split("\ndef ")[0]
        assert "except Exception" in record
        assert "return None" in record


class TestNoPerMessageInboundTableWasBuilt:
    def test_the_reply_signal_is_recency_only(self) -> None:
        """Clau ruled a per-message inbound capture is VT-744, not this row. The tell would be a
        new inbound table or a COUNT/LIMIT-shaped positional read; neither exists."""
        import orchestrator.agents.send_frequency as sf

        engagement = sf._ENGAGEMENT_SQL.upper()
        # A positional read ("the last N messages") needs a row ordering and a cut-off. Recency
        # needs neither — it is three max() timestamps. The absence is the evidence.
        assert "LIMIT" not in engagement
        assert "ORDER BY" not in engagement
        assert "COUNT(" not in engagement
        assert engagement.count("MAX(") == 3

        code = SEND_FREQUENCY.read_text(encoding="utf-8").split('"""', 2)[2]
        assert "CREATE TABLE" not in code
        migration = MIGRATION.read_text(encoding="utf-8")
        assert re.findall(r"CREATE TABLE public\.([a-z_]+)", migration) == [CLICK_TABLE], (
            "migration 201 must create the click binding and nothing else — an inbound-capture "
            "table here would be VT-744 scope leaking into this row"
        )
