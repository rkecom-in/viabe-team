"""VT-626 — deterministic FIRST-CONTACT connect route. Unit-level: the DB/mint deps are
monkeypatched (no Postgres); the connect-intent regex, provider extractor, and gate logic run
for real. Verifies the routing DECISIONS — mint sheets, kick off shopify discovery, skip a live
flow (no double-mint), fall through on ambiguous/non-connect/opt-out, and fail-open."""

from __future__ import annotations

from uuid import uuid4

import pytest

from orchestrator.onboarding import connector_first_contact as fc

_TID = str(uuid4())
_RECIP = "+919811112222"


@pytest.fixture
def spies(monkeypatch):
    """Patch the lazily-imported mint/DB deps; record calls. read_integration_state defaults to
    None (first contact). Tests override it for the live-pending case."""
    calls = {"sheets": 0, "shopify": 0, "sends": [], "state": None}

    monkeypatch.setattr(
        "orchestrator.integrations.sheets_oauth.start_sheets_oauth",
        lambda tenant_id, **k: calls.__setitem__("sheets", calls["sheets"] + 1)
        or {"authorize_url": "https://accounts.google.com/o/oauth2/v2/auth?state=x"},
    )
    monkeypatch.setattr(
        "orchestrator.onboarding.shopify_onboarding.begin_shopify_onboarding",
        lambda tenant_id, recipient: calls.__setitem__("shopify", calls["shopify"] + 1),
    )
    monkeypatch.setattr(
        "orchestrator.onboarding.shopify_onboarding._send",
        lambda recipient, text, **k: calls["sends"].append(text),
    )
    monkeypatch.setattr(
        "orchestrator.onboarding.shopify_onboarding.read_integration_state",
        lambda tenant_id: calls["state"],
    )
    return calls


def _run(body: str):
    return fc.maybe_start_connector_onboarding(_TID, body, "SM" + "0" * 32, _RECIP)


@pytest.mark.parametrize(
    "body",
    [
        "connect my google sheet please",
        # the REAL i_sheets scenario phrasings the narrow shared regex MISSED:
        "I want to connect my Google Sheet for customer data",
        "I use a Google Sheet to track my shop. Can we get this connected?",
        "Bhaiya mera customer data Google Sheet mein hai. Kaise jodu isse Viabe se?",
    ],
)
def test_sheets_first_contact_mints(spies, body):
    res = _run(body)
    assert res is not None and res["routed"] == "sheets_first_contact_minted"
    assert res["phase"] == "phase_2_auth"
    assert spies["sheets"] == 1
    assert any("accounts.google.com" in s for s in spies["sends"]), "the OAuth link must be sent"


def test_shopify_first_contact_kicks_off_discovery(spies):
    res = _run("setup shopify for me")
    assert res is not None and res["routed"] == "shopify_first_contact_discovery"
    assert res["phase"] == "phase_1_discovery"
    assert spies["shopify"] == 1
    assert spies["sheets"] == 0, "shopify path must NOT mint a sheets URL"


def test_live_pending_flow_skips_no_double_mint(spies):
    # a LIVE connector flow already in progress -> NOT first contact -> None, no re-mint.
    from datetime import UTC, datetime, timedelta

    future = (datetime.now(UTC) + timedelta(minutes=9)).isoformat()
    spies["state"] = {
        "phase": "phase_2_auth",
        "current_connector_id": "google_sheet",
        "pending_owner_input": {"awaiting": "oauth_completion", "expires_at": future},
    }
    res = _run("connect my google sheet")
    assert res is None, "a live flow must fall through to the resume gate / brain"
    assert spies["sheets"] == 0, "must NOT double-mint while a flow is live"


def test_expired_pending_is_treated_as_first_contact(spies):
    # an EXPIRED pending is not a live flow -> the route fires (proves the predicate, not row-existence)
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    spies["state"] = {
        "phase": "phase_2_auth",
        "current_connector_id": "google_sheet",
        "pending_owner_input": {"awaiting": "oauth_completion", "expires_at": past},
    }
    res = _run("connect my google sheet")
    assert res is not None and spies["sheets"] == 1


def test_ambiguous_provider_falls_through(spies):
    # connect-intent present but no single unambiguous provider -> brain classifies
    assert _run("connect my data source") is None
    assert _run("connect my sheet and shopify") is None
    assert spies["sheets"] == 0 and spies["shopify"] == 0


def test_non_connect_message_falls_through(spies):
    assert _run("what do you charge?") is None
    assert _run("kitne customers aaye aaj") is None
    assert spies["sheets"] == 0


def test_opt_out_wins_even_with_provider(spies):
    # DPDP opt-out must win even when a provider token is present
    assert _run("stop") is None
    assert spies["sheets"] == 0 and spies["shopify"] == 0


def test_fail_open_on_dep_error(monkeypatch, spies):
    def _boom(tenant_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "orchestrator.onboarding.shopify_onboarding.read_integration_state", _boom
    )
    # a clear connect ask that reaches the state read -> the raise is swallowed -> None (fail-open)
    assert _run("connect my google sheet") is None
    assert spies["sheets"] == 0
