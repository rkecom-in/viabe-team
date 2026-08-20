"""VT-734 — a message that predates the ask can never be its approval, and a repeated request is
never consent.

The breach these pin (deployed dev, 2026-08-06): an owner sent the same campaign request twice while
the turn was slow. The SECOND REQUEST — sent 72 seconds BEFORE the campaign_send approval was even
created — resolved it 'approved', and the manager then told the owner it had messaged 19 customers.
Two independent holes let that happen, so there are two independent guards, and each is tested
against the exact shape of the breach.

Fazal's ruling (CL-2026-08-06-repeated-request-is-never-approval): resolution requires BOTH the
ordering (the inbound is strictly newer than the ask) AND the content (the message AFFIRMS the
presented plan). "An impatient owner paying one extra confirmation tap is the accepted cost."

No DB, no network: the wrapper read is stubbed, so the invariant itself is what is tested.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

# The module under test imports the typed DB wrappers, which import psycopg at module level — skip
# cleanly in the dep-less smoke env (house pattern) rather than failing collection there.
pytest.importorskip("psycopg")

from orchestrator.agent.approval_resume import (  # noqa: E402
    StaleApprovalDecisionError,
    _assert_decision_is_newer_than_the_ask,
    is_repeat_of_request,
)

_TENANT = uuid4()
_APPROVAL = uuid4()

# The real breach's timeline, to the second.
_ASKED_AT = datetime(2026, 8, 6, 23, 31, 14, tzinfo=UTC)
_REQUEST_SENT_AT = datetime(2026, 8, 6, 23, 30, 2, tzinfo=UTC)  # 72s BEFORE the ask
_REPLY_SENT_AT = datetime(2026, 8, 6, 23, 31, 40, tzinfo=UTC)  # after the ask

_THE_REQUEST = "jo bhi customers lapsed hain unn sabko ek saath Diwali offer bhej do abhi"


@pytest.fixture(autouse=True)
def _stub_boundary(monkeypatch: pytest.MonkeyPatch):
    """Stub the wrapper read so the invariant is tested, not psycopg."""
    boundary: dict[str, Any] = {"value": _ASKED_AT}

    class _Stub:
        def presented_or_armed_at(self, tenant_id, approval_id, *, conn=None):  # noqa: ANN001
            return boundary["value"]

    monkeypatch.setattr(
        "orchestrator.agent.approval_resume.PendingApprovalsWrapper", lambda: _Stub()
    )
    return boundary


# --------------------------------------------------------------------- ordering invariant
def test_the_actual_breach_is_refused() -> None:
    """The exact case: an inbound 72 seconds OLDER than the approval it would resolve."""
    with pytest.raises(StaleApprovalDecisionError, match="PREDATES"):
        _assert_decision_is_newer_than_the_ask(None, _TENANT, _APPROVAL, _REQUEST_SENT_AT)


def test_a_reply_sent_after_the_ask_is_allowed() -> None:
    _assert_decision_is_newer_than_the_ask(None, _TENANT, _APPROVAL, _REPLY_SENT_AT)


def test_a_reply_at_exactly_the_ask_instant_is_refused() -> None:
    """Strictly newer. A message stamped at the same instant cannot have been a response to it."""
    with pytest.raises(StaleApprovalDecisionError):
        _assert_decision_is_newer_than_the_ask(None, _TENANT, _APPROVAL, _ASKED_AT)


def test_one_second_after_is_allowed() -> None:
    _assert_decision_is_newer_than_the_ask(
        None, _TENANT, _APPROVAL, _ASKED_AT + timedelta(seconds=1)
    )


def test_system_resolution_skips_the_check() -> None:
    """The timeout sweep and supersede paths pass no message time — they are not owner decisions,
    and the invariant must not block the reaper from closing an abandoned approval."""
    _assert_decision_is_newer_than_the_ask(None, _TENANT, _APPROVAL, None)


def test_unknown_boundary_fails_closed(_stub_boundary) -> None:
    """A row whose arm/presentation instant cannot be read refuses the resolution. Fail-closed: the
    cost is one re-ask, versus authorising a send we cannot prove the owner saw."""
    _stub_boundary["value"] = None
    with pytest.raises(StaleApprovalDecisionError, match="cannot verify"):
        _assert_decision_is_newer_than_the_ask(None, _TENANT, _APPROVAL, _REPLY_SENT_AT)


# --------------------------------------------------------------------- content rule
def test_verbatim_resend_of_the_request_is_not_approval() -> None:
    assert is_repeat_of_request(_THE_REQUEST, _THE_REQUEST) is True


def test_a_shortened_re_ask_is_not_approval() -> None:
    """"sabko bhej do abhi" adds nothing the request did not already say — still a re-ask, even
    though every token of it lives in _APPROVE_VERB."""
    assert is_repeat_of_request("sabko bhej do abhi", _THE_REQUEST) is True


def test_haan_bhej_do_is_still_an_approval() -> None:
    """The affirmation the ruling protects: "haan" is decision content the request never contained,
    so VT-615's bare-Hinglish approval keeps working."""
    assert is_repeat_of_request("haan bhej do", _THE_REQUEST) is False


def test_a_rejection_is_never_swallowed_as_a_repeat() -> None:
    """A refusal must reach the classifier even if it reuses the request's words — the money-safe
    direction depends on rejections being heard."""
    assert is_repeat_of_request("nahi, mat bhejo", _THE_REQUEST) is False
    assert is_repeat_of_request("no", _THE_REQUEST) is False


def test_no_request_text_means_the_rule_does_not_fire() -> None:
    """Advisory input: an unreadable objective must never block a legitimate decision — the ordering
    invariant is the load-bearing half."""
    assert is_repeat_of_request("bhej do", None) is False
    assert is_repeat_of_request("", _THE_REQUEST) is False


def test_repeat_check_is_case_and_punctuation_insensitive() -> None:
    assert is_repeat_of_request("BHEJ DO, ABHI!", "bhej do abhi") is True
