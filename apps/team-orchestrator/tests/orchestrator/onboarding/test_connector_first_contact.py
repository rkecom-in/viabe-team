"""VT-626 — deterministic FIRST-CONTACT connect route. Unit-level: the DB/mint deps are
monkeypatched (no Postgres); the connect-intent regex, provider extractor, and gate logic run
for real. Verifies the routing DECISIONS — mint sheets, kick off shopify discovery, skip a live
flow (no double-mint), fall through on ambiguous/non-connect/opt-out, and fail-open."""

from __future__ import annotations

from uuid import uuid4

import pytest

# The fixture monkeypatches orchestrator.integrations.sheets_oauth, which imports
# orchestrator.integrations (-> pydantic). The dep-less CI 'test' / pre-push smoke
# lacks pydantic, so guard the whole module (it runs in full-dep DB-coverage/CI).
pytest.importorskip("pydantic")

from orchestrator.onboarding import connector_first_contact as fc

_TID = str(uuid4())
_RECIP = "+919811112222"


@pytest.fixture
def spies(monkeypatch):
    """Patch the lazily-imported mint/DB deps; record calls. read_integration_state defaults to
    None (first contact). Tests override it for the live-pending case."""
    calls = {
        "sheets": 0,
        "shopify": 0,
        "sends": [],
        "state": None,
        "connected": False,
        "last_sync": None,  # DF1(c): _last_sync_at DB truth (datetime or None)
        "window": [],  # DF1(d): conversation_log.active_window() turns for context-resolve
    }

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
    # STATUS-QUESTION branch + mint check-lead read DB-truth via _connected_or_healthy
    # (tenant_oauth_tokens OR healthy tenant_connector_status) — default not connected.
    monkeypatch.setattr(
        fc, "_connected_or_healthy", lambda tenant_id, provider: calls["connected"]
    )
    # DF1(c): the freshness gate reads tenant_connector_status.last_sync_at via _last_sync_at.
    monkeypatch.setattr(fc, "_last_sync_at", lambda tenant_id, provider: calls["last_sync"])
    # DF1(d): _resolve_provider_from_context scans the conversation window via active_window.
    monkeypatch.setattr(
        "orchestrator.conversation_log.active_window", lambda tenant_id, **k: calls["window"]
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
        "Bhaiya mera customer data Google Sheet mein hai. Kaise jodu isse Viabe se?",
    ],
)
def test_sheets_first_contact_mints(spies, body):
    res = _run(body)
    assert res is not None and res["routed"] == "sheets_first_contact_minted"
    assert res["phase"] == "phase_2_auth"
    assert spies["sheets"] == 1
    assert any("accounts.google.com" in s for s in spies["sends"]), "the OAuth link must be sent"


def test_mint_on_connected_provider_leads_with_checked_status(spies):
    """T11 residual (§2 judge x3) — 'sync seems stuck, check and reconnect it' on an ALREADY-
    CONNECTED provider must LEAD with the checked status, then the fresh re-auth link — never a
    bare link that evades the check the owner asked for."""
    spies["connected"] = True
    res = _run("My Google Sheet sync seems stuck, nothing's come in for 3 days — can you check and reconnect it?")
    assert res is not None and res["routed"] == "sheets_first_contact_minted"
    body_sent = spies["sends"][0]
    assert "I checked" in body_sent
    assert "shows connected" in body_sent
    assert "accounts.google.com" in body_sent, "the re-auth link still goes out (same message)"


def test_mint_on_unconnected_provider_has_no_check_lead(spies):
    spies["connected"] = False
    res = _run("connect my google sheet please")
    assert res is not None and res["routed"] == "sheets_first_contact_minted"
    assert "I checked" not in spies["sends"][0]


def test_mint_check_failure_fails_soft_to_plain_mint(spies, monkeypatch):
    def _boom(tenant_id, provider):
        raise RuntimeError("db down")

    monkeypatch.setattr(fc, "_connected_or_healthy", _boom)
    res = _run("connect my google sheet please")
    assert res is not None and res["routed"] == "sheets_first_contact_minted"
    body_sent = spies["sends"][0]
    assert "I checked" not in body_sent
    assert "accounts.google.com" in body_sent


def test_bare_state_request_offers_setup_no_url(spies):
    # "get this connected?" is a past-participle STATE phrasing with no imperative verb -> after the
    # 2026-07-10 split it routes to the status/OFFER branch (no URL dump), NOT an immediate mint.
    # The owner's next "yes, connect it" reply carries the imperative verb and mints. This is the
    # deliberate trade to kill the status-question URL-dump loop.
    res = _run("I use a Google Sheet to track my shop. Can we get this connected?")
    assert res is not None and res["routed"] == "connector_status_answered"
    assert spies["sheets"] == 0
    body_sent = spies["sends"][0]
    assert "https://" not in body_sent
    assert "set it up" in body_sent.lower()


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


# --- STATUS-QUESTION branch (2026-07-10): a connection-status QUESTION must be ANSWERED, never
#     answered-with-an-OAuth-URL-dump (the dominant Tier-1 loop_stall / ignored_speech_act bug). ---


@pytest.mark.parametrize(
    "body",
    [
        "I haven't seen my Google Sheet numbers update, is that thing even still connected?",
        "Can you at least tell me if my Google Sheet is connected?",
        "Wait, so my Google Sheet was never actually connected in the first place?",
    ],
)
def test_status_question_answers_without_url(spies, body):
    res = _run(body)
    assert res is not None and res["routed"] == "connector_status_answered"
    assert res["phase"] == "status_answer"
    assert spies["sheets"] == 0, "a status question must NOT mint an OAuth link"
    assert spies["shopify"] == 0
    assert len(spies["sends"]) == 1, "exactly one honest answer sent"
    body_sent = spies["sends"][0]
    assert "https://" not in body_sent, "must NOT dump/repeat the OAuth URL on a status question"
    # answers the status: not-connected default -> an OFFER to set it up, still no URL
    assert "connected" in body_sent.lower()


def test_status_question_connected_true_affirms_no_url(spies):
    spies["connected"] = True
    res = _run("can you at least tell me if my google sheet is connected?")
    assert res is not None and res["routed"] == "connector_status_answered"
    assert len(spies["sends"]) == 1
    body_sent = spies["sends"][0]
    assert "https://" not in body_sent
    assert body_sent.lower().startswith("yes"), "connected -> affirmative answer"
    assert "connected" in body_sent.lower()
    assert spies["sheets"] == 0


def test_status_question_live_flow_says_not_finished_no_url(spies):
    # not connected + a LIVE flow in progress -> honest 'not finished, reply done' — still NO URL.
    from datetime import UTC, datetime, timedelta

    future = (datetime.now(UTC) + timedelta(minutes=9)).isoformat()
    spies["state"] = {
        "phase": "phase_2_auth",
        "current_connector_id": "google_sheet",
        "pending_owner_input": {"awaiting": "oauth_completion", "expires_at": future},
    }
    res = _run("is my google sheet connected yet?")
    assert res is not None and res["routed"] == "connector_status_answered"
    body_sent = spies["sends"][0]
    assert "https://" not in body_sent
    assert "done" in body_sent.lower()
    assert spies["sheets"] == 0, "never re-dump the link mid-flow"


@pytest.mark.parametrize(
    "body",
    [
        "connect my google sheet",
        "please set up my google sheet",
    ],
)
def test_imperative_still_mints(spies, body):
    # the imperative path is UNCHANGED — a request-to-connect still mints the OAuth link.
    res = _run(body)
    assert res is not None and res["routed"] == "sheets_first_contact_minted"
    assert spies["sheets"] == 1
    assert any("accounts.google.com" in s for s in spies["sends"]), "the OAuth link must be sent"


# ----------------------------- DF1: cross-tenant / third-party guard ----------------------
@pytest.mark.parametrize(
    "body",
    [
        "my friend Rakesh runs a shop, check if his Shopify is connected",
        "uski shop ka account connect karo",
        "is their google sheet connected",
    ],
)
def test_third_party_ask_declined_never_emits_owner_status(spies, body):
    """A connect/status ask about SOMEONE ELSE'S business is declined honestly — never answered with
    the OWNER's own connection status (cross-tenant leak + verbatim-loop breaker)."""
    spies["connected"] = True  # even if the OWNER is connected, must NOT leak "yes, connected"
    res = _run(body)
    assert res is not None and res["routed"] == "connector_third_party_declined"
    assert spies["sends"], "must send an honest decline"
    reply = spies["sends"][-1].lower()
    assert "your own" in reply and "someone else" in reply
    assert "connected" not in reply.split("someone else")[0]  # no owner-status affirmation


def test_own_shopify_status_still_self_answers(spies):
    """Regression pin: 'is MY shopify connected?' carries no third-person possessive -> self-answers."""
    spies["connected"] = True
    res = _run("is my shopify connected?")
    assert res is not None and res["routed"] == "connector_status_answered"
    assert "connected" in spies["sends"][-1].lower()


# ----------------------------- DF1(a): owner-data-pull honesty (unconnected) --------------
def test_owner_data_pull_unconnected_answers_honestly_no_fabrication(spies):
    """A request to PULL the owner's own data when the Sheet isn't connected is answered HONESTLY
    (never fabricates having the data) + points at the one connect step (i_sheets_partial)."""
    spies["connected"] = False
    res = _run("Can you also pull in my order amounts and order dates from that same sheet?")
    assert res is not None and res["routed"] == "connector_owner_data_not_connected"
    reply = spies["sends"][-1].lower()
    assert "isn't connected" in reply or "connection isn't finished" in reply
    assert "google sheet" in reply


def test_owner_data_pull_when_connected_falls_through_to_brain(spies):
    """If the Sheet IS connected, a data-pull ask falls through to the brain (which can actually
    pull) — the honesty branch only fires on an UNCONNECTED provider."""
    spies["connected"] = True
    res = _run("pull in my order amounts from that same sheet")
    assert res is None  # fell through, not the not-connected honesty branch


def test_bare_capability_question_falls_through(spies):
    """A bare capability question ('can you map fields?') has no possessive data object -> not a
    data-pull -> falls through (never the not-connected honesty branch)."""
    res = _run("can you map fields?")
    assert res is None


# ----------------------------- DF1(c): sync-freshness pushback (DB-truth, no invented date) ------
def test_sync_pushback_connected_with_timestamp_states_db_truth(spies):
    """A freshness pushback ('are you sure? I haven't seen new numbers') on a CONNECTED sheet states
    the REAL tenant_connector_status.last_sync_at — never an invented date (reconnect_broken_sync)."""
    from datetime import datetime

    spies["connected"] = True
    spies["last_sync"] = datetime(2026, 7, 10, 14, 30)
    res = _run("are you sure bhaiya? I haven't seen new numbers on my google sheet in days")
    assert res is not None and res["routed"] == "connector_sync_freshness_answered"
    assert res["phase"] == "sync_freshness_answer"
    assert spies["sheets"] == 0, "a freshness answer must NOT mint an OAuth link"
    reply = spies["sends"][-1]
    assert "10 Jul 2026, 14:30" in reply, "must state the REAL DB last_sync, not an invented date"
    assert "https://" not in reply
    assert "re-authorize" in reply.lower(), "offers a re-check/re-auth"


def test_sync_pushback_connected_no_timestamp_honest_admit_no_date(spies):
    """Connected but the DB holds no last_sync_at -> HONESTLY admit there's no reliable sync time +
    offer re-check/re-auth. Must NOT compose any date literal."""
    spies["connected"] = True
    spies["last_sync"] = None
    res = _run("are you sure? kuch nahi aaya from my google sheet")
    assert res is not None and res["routed"] == "connector_sync_freshness_answered"
    reply = spies["sends"][-1].lower()
    assert "reliable last-sync time" in reply
    assert "2026" not in reply and "jul" not in reply, "no fabricated date when the DB has none"
    assert "re-check" in reply or "re-authorize" in reply


def test_sync_pushback_resolves_provider_from_window(spies):
    """The pushback names no provider in-message -> the gate resolves it from an explicit prior
    conversation mention, then answers from DB truth."""
    from datetime import datetime

    spies["connected"] = True
    spies["last_sync"] = datetime(2026, 7, 9, 9, 0)
    spies["window"] = [{"role": "owner", "text": "I want to connect my Google Sheet for orders"}]
    res = _run("are you sure? I haven't seen new numbers come through")
    assert res is not None and res["routed"] == "connector_sync_freshness_answered"
    reply = spies["sends"][-1]
    assert "Google Sheet" in reply
    assert "09 Jul 2026, 09:00" in reply


def test_sync_pushback_not_connected_falls_through(spies):
    """Tight gate: a freshness pushback on a provider that ISN'T connected on our side falls through
    (no honest-status to give here) — proves the connected conjunct."""
    spies["connected"] = False
    res = _run("are you sure? I haven't seen new numbers on my google sheet")
    assert res is None
    assert spies["sheets"] == 0 and not spies["sends"]


def test_sync_pushback_unresolvable_provider_falls_through(spies):
    """No in-message provider and nothing to resolve from context -> the gate can't ground an answer
    -> falls through (proves the provider conjunct)."""
    spies["connected"] = True
    spies["window"] = []
    res = _run("are you sure? I haven't seen new numbers come through")
    assert res is None
    assert not spies["sends"]


def test_bare_are_you_sure_no_data_absence_does_not_fire(spies):
    """Precision: a bare doubt ('are you sure about that?') with NO data-absence phrasing must NOT
    fire the freshness gate even on a connected provider — it falls through to the brain."""
    spies["connected"] = True
    res = _run("are you sure about my google sheet?")
    assert res is None
    assert not spies["sends"]


# ----------------------------- DF1(d): provider-resolve-from-context ------------------------------
def test_provider_resolve_from_integration_state_mints(spies):
    """A connect imperative with NO in-message provider ('connect it again') resolves the provider
    from tenant_integration_state.current_connector_id, then mints."""
    spies["state"] = {
        "phase": "phase_2_auth",
        "current_connector_id": "google_sheet",
        "pending_owner_input": None,  # not a LIVE flow -> mints (no double-mint guard trip)
    }
    res = _run("connect it again please, use the same one you already have")
    assert res is not None and res["routed"] == "sheets_first_contact_minted"
    assert spies["sheets"] == 1
    assert any("accounts.google.com" in s for s in spies["sends"])


def test_provider_resolve_from_conversation_window_mints(spies):
    """No provider in-message and no integration-state -> resolves from an explicit prior
    conversation mention, then mints."""
    spies["state"] = None
    spies["window"] = [{"role": "owner", "text": "please connect my google sheet"}]
    res = _run("connect it again please")
    assert res is not None and res["routed"] == "sheets_first_contact_minted"
    assert spies["sheets"] == 1


def test_provider_resolve_shopify_from_state_kicks_discovery(spies):
    spies["state"] = {
        "phase": "phase_1_discovery",
        "current_connector_id": "shopify",
        "pending_owner_input": None,
    }
    res = _run("connect it again")
    assert res is not None and res["routed"] == "shopify_first_contact_discovery"
    assert spies["shopify"] == 1
    assert spies["sheets"] == 0


def test_provider_resolve_no_context_falls_through(spies):
    """A connect imperative with no in-message provider and nothing resolvable from context -> None
    (fail-open, unchanged from before the rail)."""
    spies["state"] = None
    spies["window"] = []
    res = _run("connect it again please")
    assert res is None
    assert spies["sheets"] == 0 and spies["shopify"] == 0


def test_bare_google_mention_does_not_resolve(spies):
    """A bare 'Google' in prior context must NOT broaden to google_sheet (no in-message provider
    either) -> falls through. Guards against over-eager provider inference."""
    spies["state"] = None
    spies["window"] = [{"role": "owner", "text": "had to log into Google again this morning"}]
    res = _run("connect it again")
    assert res is None
    assert spies["sheets"] == 0
