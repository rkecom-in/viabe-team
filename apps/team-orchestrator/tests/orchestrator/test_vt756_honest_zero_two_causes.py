"""VT-756 — an absence of data must not be reported as a measurement.

THE DEFECT, measured on deployed dev (m_conversation_hinglish_status_smalltalk 3/3, and again in
gate (d) on m_conversation_multi_request_mixed_ask):

    owner:     (code-mixed status ask, tenant --onboarded, NOTHING connected, no rows)
    assistant: You currently have 0 customers in your ledger.

`status_query.customer_count` counted rows and stated the number, with no connection check. "You
have 0 customers" and "you haven't connected your customer data yet" are different claims: the first
says we looked and found none. An owner reading it concludes their business has no customers on
record — or that Viabe lost them.

WHY TWO SENTENCES AND NOT ONE. `if n == 0: say not-connected` would replace one wrong answer with
another the moment a connected source genuinely returns zero rows. Zero has two causes and each
warrants a different next action from the owner: connect something, or check the sync.

THE SHAPE THIS TESTS FOR. The honest pattern already existed FOUR LINES BELOW in the same function —
`top_spend` degrades to "I don't have enough data yet", and `lapsed_count` guards on `has_base`. So
this was never a missing capability; it was one branch that never got the treatment its neighbours
got. These tests pin the whole class, not the one line: every branch that reports an aggregate now
states its precondition or is shown not to need one.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from orchestrator.owner_inputs import status_query as sq  # noqa: E402

_TENANT = "11111111-2222-3333-4444-555555555555"


class _Customers:
    def __init__(self, total: int = 0, excluded: int = 0):
        self._total, self._excluded = total, excluded

    def count_all(self, _tenant_id):
        return self._total

    def count_by_opt_out_status(self, _tenant_id, _statuses):
        return self._excluded


@pytest.fixture
def wire(monkeypatch):
    """Patch the two reads the branches make; return a setter for (row count, connected)."""
    def _set(*, total: int, connected: bool, excluded: int = 0):
        import orchestrator.db.wrappers as w

        monkeypatch.setattr(w, "CustomersWrapper", lambda: _Customers(total, excluded))
        import orchestrator.integrations.connection_truth as ct

        monkeypatch.setattr(ct, "customer_data_source_connected", lambda _t: connected)

    return _set


# --- customer_count: the branch the row is named for -------------------------------------------


def test_nothing_connected_and_zero_rows_NEVER_states_a_number(wire):
    """Exit gate (a). The failure mode is a numeral, so the assertion is about numerals — not about
    which sentence we happened to choose."""
    wire(total=0, connected=False)
    ans = sq.answer_status_query(_TENANT, "how many customers do I have?")
    assert ans is not None
    assert not any(ch.isdigit() for ch in ans), f"a numeral survived into an unconnected answer: {ans!r}"
    assert "0 customers" not in ans
    assert "connect" in ans.lower(), f"the owner is not told what to do next: {ans!r}"


def test_connected_and_genuinely_zero_says_so_WITHOUT_claiming_nothing_is_connected(wire):
    """Exit gate (b) — the fix must not swap one lie for another. A tenant whose source IS connected
    and really has no rows is owed the opposite sentence."""
    wire(total=0, connected=True)
    ans = sq.answer_status_query(_TENANT, "how many customers do I have?")
    assert ans is not None
    low = ans.lower()
    assert "connected" in low
    assert "don't have your customer data" not in low, (
        f"a connected tenant was told nothing is connected: {ans!r}"
    )


def test_a_real_count_is_unchanged(wire):
    """Exit gate (c). n > 0 needs no connection check at all — rows exist, so data exists, whatever
    the connector tables say. This is also what keeps a seeded ledger answering as it always did."""
    wire(total=8, connected=False)
    assert sq.answer_status_query(_TENANT, "how many customers do I have?") == (
        "You currently have 8 customers in your ledger."
    )


def test_the_status_branch_does_not_SWALLOW_a_connection_read_error(monkeypatch):
    """The fail-closed posture belongs in ONE place (customer_data_source_connected, tested below).
    This asserts the caller does not add a second, silent one — a swallowed error here would answer
    with whatever the fallback happened to be, which is how a fail-soft seam ends up fabricating."""
    import orchestrator.db.wrappers as w

    monkeypatch.setattr(w, "CustomersWrapper", lambda: _Customers(0, 0))
    from orchestrator.integrations import connection_truth as ct

    def _boom(_t):
        raise RuntimeError("connector tables unreachable")

    monkeypatch.setattr(ct, "customer_data_source_connected", _boom)
    with pytest.raises(RuntimeError):
        sq.answer_status_query(_TENANT, "how many customers do I have?")


def test_connection_truth_itself_fails_closed(monkeypatch):
    """The fail-closed posture lives INSIDE customer_data_source_connected — prove it there, since
    that is the contract the callers rely on."""
    from orchestrator.integrations import connection_truth as ct
    import orchestrator.db as db

    reached = []

    def _boom(_t):
        reached.append(1)
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "tenant_connection", _boom)
    assert ct.customer_data_source_connected(_TENANT) is False
    assert reached, (
        "the patched reader was never called — without this the test passes on ANY failure path "
        "(no DATABASE_URL, import error) and proves nothing about fail-closed"
    )


# --- scope 3: the same shape in the sibling branches -------------------------------------------


def test_opt_out_count_on_an_EMPTY_ledger_is_not_a_reassuring_statistic(wire):
    """"0 customers are excluded from your campaigns" against an empty ledger reads as a FINDING —
    nobody has opted out — when the truth is that nobody is loaded."""
    wire(total=0, connected=False, excluded=0)
    ans = sq.answer_status_query(_TENANT, "how many customers have opted out?")
    assert ans is not None
    assert "excluded from your campaigns" not in ans, f"population statistic on an empty ledger: {ans!r}"
    assert not any(ch.isdigit() for ch in ans)


def test_opt_out_count_still_answers_when_there_IS_a_population(wire):
    wire(total=40, connected=True, excluded=3)
    ans = sq.answer_status_query(_TENANT, "how many customers have opted out?")
    assert ans == "3 customers are excluded from your campaigns (opted out or owner-excluded)."


def test_customer_list_on_an_EMPTY_ledger_sends_no_file(wire, monkeypatch):
    """An empty CSV announced as "I've sent your customer list" is the zero fabrication with a file
    attached to it — the owner opens a document that says their business has no customers."""
    sent: list[str] = []
    import orchestrator.owner_surface.customer_export as ce

    monkeypatch.setattr(ce, "send_customer_list_to_owner", lambda t: sent.append(t) or True)
    wire(total=0, connected=False)
    ans = sq.answer_status_query(_TENANT, "send me the list of my customers")
    assert sent == [], "an empty ledger produced a customer-list file"
    assert ans is not None and "sent" not in ans.lower()


def test_every_aggregate_branch_states_its_precondition():
    """Exit gate (e) — the enumeration, kept in the test suite so it cannot silently rot.

    A branch is SAFE either because it guards on its data precondition, or because its claim is about
    OUR records rather than the owner's data source (a campaign we did or did not run is true whatever
    the owner has connected).
    """
    src = __import__("pathlib").Path(sq.__file__).read_text()
    guarded = {
        "customer_count": "_honest_empty_ledger",      # VT-756, this row
        "opt_out_count": "_honest_empty_ledger",       # VT-756 scope 3
        "customer_list": "_honest_empty_ledger",       # VT-756 scope 3
        "top_spend": "if not ranked",                  # already honest before this row
        "lapsed_count": "if not has_base",             # already honest
        "lapsed_list": "if not has_base",              # already honest
    }
    for qtype, marker in guarded.items():
        seg = src.split(f'if qtype == "{qtype}":', 1)
        assert len(seg) == 2, f"branch {qtype} is gone — this enumeration is stale"
        body = seg[1].split('if qtype == "', 1)[0]
        assert marker in body, f"{qtype} lost its data precondition ({marker!r} not found)"
    # last_campaign / billing claim nothing about the owner's connected data — no precondition needed.
    assert 'if qtype == "last_campaign":' in src and 'if qtype == "billing":' in src
