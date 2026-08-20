"""VT-761 — "Can we get this connected?" is a request, and answering it as a question cost two turns.

THE MEASURED EXCHANGE (i_sheets_mapping_confirm_happy_path, byte-identical in all THREE gate (d)
passes — deterministic, not sampling):

    owner:  I use a Google Sheet to track my shop — columns are Customer Name, Phone Number,
            Order Amount, Order Date, and Address. Can we get this connected?
    reply:  No — your Google Sheet isn't connected yet. Want me to set it up?

    owner:  Done, I've connected it
    reply:  No — your Google Sheet isn't connected yet. Want me to set it up?

THE MECHANISM. `connector_first_contact` splits the connect signal into IMPERATIVE ("connect my
sheet" → mint a link) and STATE ("is it connected?" → answer from the DB, never dump a URL). The
split keys on VERB FORM, so a request written with a past participle — *can we get this connected* —
matched only the STATE regex and was routed to the branch that cannot mint. The module's own
docstring lists that exact phrasing as one it exists to catch.

WHY THE SECOND TURN IS THE SERIOUS ONE, AND WHY ONE FIX CLOSES BOTH. The row filed two defects (no
link, then no re-check). They are one: with no mint, no `phase_2_auth` was armed, so the owner's
"Done, I've connected it" never reached `connector_resume` — which runs BEFORE first-contact in the
runner and whose `_auth_wait_reply` already says exactly what the scenario asks for ("I don't see the
connection yet…", and per VT-712 never the same line twice). The second reply was not a second bug;
it was the first bug's shadow. Fixing the classification puts the turn back on the path that was
already built.

NOT A PHRASING LIST (standing no-lists rule). The detector is the ENGLISH CAUSATIVE construction —
request frame + get/make/have + object + participle. The request frame is load-bearing: it is what
keeps "did you get it connected?" and "have you connected it?" on the status branch, where they
belong.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from orchestrator.onboarding import connector_first_contact as cfc  # noqa: E402

_MEASURED_STEP_0 = (
    "I use a Google Sheet to track my shop — columns are Customer Name, Phone Number, "
    "Order Amount, Order Date, and Address. Can we get this connected?"
)


def _classify(text: str) -> tuple[bool, bool]:
    """(is_imperative, is_state) exactly as maybe_start_connector_onboarding computes them."""
    imperative = bool(
        cfc._CONNECT_IMPERATIVE_RE.search(text) or cfc._CONNECT_REQUEST_PARTICIPLE_RE.search(text)
    )
    return imperative, bool(cfc._CONNECT_STATE_RE.search(text))


def test_THE_MEASURED_TURN_now_classifies_as_a_request():
    """The whole row. Before: imperative=False, state=True → the status branch → no link, no armed
    flow, and the next turn stranded."""
    imperative, state = _classify(_MEASURED_STEP_0)
    assert imperative, "the measured step-0 message is still read as a status question"
    assert cfc._detect_provider(_MEASURED_STEP_0) == "google_sheet"
    # `state` stays True — the message DOES reference connection state. What changed is that the
    # branch order now lets the imperative win, which is the point: `if is_state and not is_imperative`.
    assert state


@pytest.mark.parametrize(
    "text",
    [
        "Let's get this connected",
        "please get it connected",
        "can you get my sheet connected",
        "could we get the spreadsheet linked up",
        "I want to get this set up",
        "shall we get that synced",
    ],
)
def test_request_frames_around_a_participle_are_requests(text):
    assert _classify(text)[0], f"a request was read as a status question: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "did you get it connected?",
        "have you connected it?",
        "is it connected?",
        "was it never connected?",
        "Done, I've connected it",
        "when did we get the shop connected last time?",
        "is my sheet still synced?",
    ],
)
def test_genuine_status_questions_are_NOT_hijacked_into_a_mint(text):
    """The status branch exists because dumping (and re-dumping) a URL at someone asking a question
    was the dominant Tier-1 loop_stall trust-breaker. This fix must not widen the mint by one inch
    past the causative-with-a-request-frame."""
    imperative, state = _classify(text)
    assert not imperative, f"a status question would now mint a link: {text!r}"
    assert state, f"precondition: {text!r} is a state reference at all"


def test_the_participle_detector_needs_BOTH_halves():
    """Neither half alone may fire it — a request frame with no causative ("can you connect it" is
    already the imperative regex's job) and a causative with no request frame ("he got it connected")
    both stay out, so the new regex adds exactly one construction."""
    assert not cfc._CONNECT_REQUEST_PARTICIPLE_RE.search("he got it connected last week")
    assert not cfc._CONNECT_REQUEST_PARTICIPLE_RE.search("can you tell me more about pricing")


def test_the_resume_path_the_mint_unlocks_still_says_what_it_checked():
    """The second turn's fix is not new code — it is `connector_resume` finally being reachable. Pin
    the copy the scenario asserts, so a rename there surfaces here rather than in a 5-hour pack run.
    """
    from orchestrator.onboarding.shopify_onboarding import _auth_wait_reply

    first = _auth_wait_reply(1, None, "Google Sheet")
    assert "don't see the connection" in first, (
        "the re-check reply no longer states what was checked — i_sheets step 1 asserts this phrase"
    )
    # VT-712's never-twice guard is what keeps the byte-identical repeat from coming back by another
    # route; assert it holds rather than assuming it.
    assert _auth_wait_reply(2, None, "Google Sheet") != first
    assert _auth_wait_reply(3, None, "Google Sheet") not in (first, _auth_wait_reply(2, None, "Google Sheet"))
