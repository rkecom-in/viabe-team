"""VT-712 — the OAuth wait-loop fix (run-4 sim, Fazal: "the worst gap").

Three diseases, three pins:
1. The ``toks & _DONE`` particle false-floor ("ho"/"kar"/"ok" inside ANY Hinglish sentence
   read as completion) → phrase-based ``_is_done_reply``.
2. Verbatim verify-fail on every turn → the ``_auth_wait_reply`` ladder (link only on the
   first attempt; later attempts back off + advertise 'new link').
3. A fresh-link ask got the same canned line → the sheets gate's narrow two-signal floor
   ("link" + a renewal word) remints a REAL authorize URL.
"""
from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

from orchestrator.onboarding import sheets_resume  # noqa: E402
from orchestrator.onboarding.shopify_onboarding import (  # noqa: E402
    _auth_wait_reply,
    _is_done_reply,
)

_TID = "55555555-5555-5555-5555-555555555555"


# --- 1. the done floor (the exact run-4 messages) ---------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("done", True),
        ("ho gaya", True),
        ("Kar diya bhai", True),
        ("ok", True),  # bare affirmation as the WHOLE message still counts
        ("connected!", True),
        # run-4 loop victims — particles must never read as completion:
        ("Arre kyu nahi ho raha ye? Maine allow bhi kiya tha. Link phir se bhejo, fresh try karta hoon", False),
        ("Yaar same link toh bhej rahe ho fir se, koi naya link nahi mila.", False),
        ("Ok give me 10 min, trying again from scratch. Will message done once actually approved.", True),
        ("Wait sorry, doing it now, one sec", False),
    ],
)
def test_is_done_reply_truth_table(body: str, expected: bool) -> None:
    assert _is_done_reply(body) is expected


def test_bare_affirm_only_as_whole_message() -> None:
    assert _is_done_reply("ok") is True
    assert _is_done_reply("ok so what happens next with my data?") is False


# --- 2. the reply ladder ----------------------------------------------------------------------


def test_wait_reply_ladder_never_verbatim() -> None:
    r1 = _auth_wait_reply(1, "https://example.com/auth", "Google")
    r2 = _auth_wait_reply(2, "https://example.com/auth", "Google")
    r3 = _auth_wait_reply(3, "https://example.com/auth", "Google")
    assert len({r1, r2, r3}) == 3, "three attempts, three different messages"
    assert "https://example.com/auth" in r1, "first attempt carries the link"
    assert "https://example.com/auth" not in r2 and "https://example.com/auth" not in r3, (
        "backoff never re-pastes the link"
    )
    assert "new link" in r2 and "new link" in r3, "the fresh-link path is advertised"
    assert _auth_wait_reply(7, None, "Shopify") == r3, "ladder is stable past 3"


# --- 3. the sheets gate routing ---------------------------------------------------------------


def _wire_sheets(monkeypatch, *, connected: bool = False):
    import orchestrator.onboarding.shopify_onboarding as so

    calls: dict[str, Any] = {"sent": [], "state_writes": [], "minted": 0}
    state = {
        "phase": so.PHASE_AUTH,
        "current_connector_id": "google_sheets",
        "pending_owner_input": {
            "awaiting": "oauth_completion",
            "connector_id": "google_sheets",
            "walkthrough_url": "https://accounts.google.com/old",
        },
    }
    monkeypatch.setattr(so, "read_integration_state", lambda t: state)
    monkeypatch.setattr(
        so, "_send",
        lambda recipient, text, *, tenant_id=None: calls["sent"].append(text),
    )
    monkeypatch.setattr(
        so, "_write_state",
        lambda t, *, phase, connector_id, pending: calls["state_writes"].append(dict(pending or {})),
    )
    import orchestrator.integrations.commit as commit

    monkeypatch.setattr(commit, "is_connector_connected", lambda t, c: connected)
    import orchestrator.integrations.sheets_oauth as soauth

    def _mint(t, **k):
        calls["minted"] += 1
        return {"authorize_url": "https://accounts.google.com/FRESH"}

    monkeypatch.setattr(soauth, "start_sheets_oauth", _mint)
    return calls, state


def test_fresh_link_ask_remints(monkeypatch) -> None:
    calls, _ = _wire_sheets(monkeypatch)
    r = sheets_resume.maybe_resume_sheets_onboarding(
        _TID, "Link phir se bhejo, fresh try karta hoon", "SM1", "+919999006001"
    )
    assert r is not None and r["routed"] == "sheets_auth_link_reminted"
    assert calls["minted"] == 1
    assert "FRESH" in calls["sent"][0]


def test_patience_message_falls_to_brain(monkeypatch) -> None:
    calls, _ = _wire_sheets(monkeypatch)
    r = sheets_resume.maybe_resume_sheets_onboarding(
        _TID, "Wait sorry, doing it now, one sec", "SM2", "+919999006002"
    )
    assert r is None, "no canned line — the brain owns off-floor replies now"
    assert calls["sent"] == []


def test_done_verify_fail_ladders_and_persists_attempts(monkeypatch) -> None:
    calls, state = _wire_sheets(monkeypatch, connected=False)
    r1 = sheets_resume.maybe_resume_sheets_onboarding(_TID, "done", "SM3", "+919999006003")
    assert r1 is not None and r1["routed"] == "sheets_auth_not_connected"
    assert "accounts.google.com/old" in calls["sent"][0], "attempt 1 carries the link"
    assert calls["state_writes"][-1]["metadata"]["verify_attempts"] == 1
    # second failed verify — pending in state was mutated in place by the gate
    r2 = sheets_resume.maybe_resume_sheets_onboarding(_TID, "ho gaya", "SM4", "+919999006004")
    assert r2 is not None
    assert calls["sent"][1] != calls["sent"][0], "never verbatim twice"
    assert "accounts.google.com/old" not in calls["sent"][1], "backoff drops the link"
    assert calls["state_writes"][-1]["metadata"]["verify_attempts"] == 2
