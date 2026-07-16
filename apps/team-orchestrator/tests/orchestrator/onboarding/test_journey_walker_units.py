"""R9 — pure-unit coverage for the onboarding walker's deterministic helpers (no DB / no LLM).

These pin the string-shaping helpers added in the R9 walker batch so they are verifiable without
a live Postgres (the DB-backed integrated behaviours live in ``test_journey.py``):

  - ``_completion_recap`` / ``_completion_message`` — one-line recap of captured fields at
    completion (item 5), with a byte-identical fallback to today's copy on empty answers;
  - ``_prefix_defer_ack`` / ``_DEFER_ACK`` — the skip defer-ack (item 1);
  - ``_is_kickoff_token`` — a re-tapped "Complete Setup" mid-journey is a NON-answer (item 6).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# journey imports psycopg (tenant_connection / Jsonb) at module import; skip the dep-less smoke.
pytest.importorskip("psycopg")

from orchestrator.onboarding import journey as j  # noqa: E402

# The pre-R9 completion copy — the recap fallback must reproduce it BYTE-for-BYTE on empty answers.
_LEGACY_EN = "Thanks — that's everything we need to get started. We're setting up your assistant now."


# --- item 5: completion recap ---------------------------------------------------------------------


@pytest.mark.parametrize("answers", [None, {}, {"__flow__": "profile_previewed"}, {"operating_hours": "9-9"}])
def test_completion_recap_empty_or_no_recap_field_is_blank(answers):
    # No recap-worthy field (or empty) → ('', '') so the completion falls back to today's exact copy.
    assert j._completion_recap(answers) == ("", "")


def test_completion_message_empty_is_byte_identical_to_legacy():
    assert j._completion_message()["reply_en"] == _LEGACY_EN
    assert j._completion_message({})["reply_en"] == _LEGACY_EN
    assert j._completion_message({"operating_hours": "9-9"})["reply_en"] == _LEGACY_EN


def test_completion_recap_names_captured_fields():
    en, hi = j._completion_recap({"business_type": "leather bags", "city": "Pune"})
    assert "leather bags" in en and "Pune" in en
    assert "leather bags" in hi and "Pune" in hi
    assert en.startswith(" Here's what I've noted:")


def test_completion_message_carries_recap_and_keeps_closer():
    msg = j._completion_message({"business_type": "leather bags", "city": "Pune"})
    assert "leather bags" in msg["reply_en"] and "Pune" in msg["reply_en"]
    assert msg["reply_en"].startswith("Thanks — that's everything we need to get started.")
    assert msg["reply_en"].endswith("We're setting up your assistant now.")
    assert msg["done"] is True


def test_completion_recap_collapses_business_type_and_category_to_one():
    # Only ONE business line even if both business_type and category are present.
    en, _ = j._completion_recap({"business_type": "sweets", "category": "Sweet shop", "city": "Pune"})
    assert en.count("sweets") == 1
    assert "Sweet shop" not in en, "category is suppressed when business_type already recaps the business"
    assert "Pune" in en


def test_completion_recap_caps_at_three_fields():
    en, _ = j._completion_recap(
        {"business_type": "bags", "city": "Pune", "about": "we sell bags", "website": "x.in"}
    )
    # business_type + city + about = 3; website is dropped (recap stays one short line).
    assert en.count(",") == 2


def test_completion_recap_ignores_non_string_and_blank_values():
    en, _ = j._completion_recap({"business_type": "  ", "city": None, "about": "we sell bags"})
    assert "we sell bags" in en
    assert en.count(",") == 0, "blank/None fields contribute nothing to the recap"


def test_completion_recap_dedups_identical_values():
    # VT-639: the VT-601 cross-fill copies a descriptive business_type verbatim into 'about'; the
    # recap must NOT emit "noted: <desc>, <desc>" — identical values collapse to one.
    en, hi = j._completion_recap(
        {"business_type": "mithai aur namkeen", "about": "mithai aur namkeen", "city": "Pune"}
    )
    assert en.count("mithai aur namkeen") == 1
    assert hi.count("mithai aur namkeen") == 1
    assert "Pune" in en  # distinct values are still kept
    # case-insensitive dedup
    en2, _ = j._completion_recap({"business_type": "Leather Bags", "about": "leather bags"})
    assert en2.lower().count("leather bags") == 1


# --- VT-639: GST nature-of-business deflection detector -------------------------------------------


def test_gst_nature_deflection_detects_pure_tax_codes():
    # A pure GST 'nature of business' tax-activity code offered as the business type is a DEFLECTION,
    # not a description — deterministic-walker parity with the turn-brain's 'never present GST nature
    # values as business-type' rule. The scenario's own phrasing must trip it.
    assert j._is_gst_nature_deflection("Supplier of Services")
    assert j._is_gst_nature_deflection(
        "actually humara GST mein 'Supplier of Services' likha hai, wahi bata dete hain"
    )
    assert j._is_gst_nature_deflection("we're a supplier of goods")
    assert j._is_gst_nature_deflection("Works Contract")
    assert j._is_gst_nature_deflection("Input Service Distributor")


def test_gst_nature_deflection_ignores_real_descriptions():
    # A genuine description — even a rich NON-taxonomy one (VT-601 salvages these into 'about') — is
    # NOT a deflection. Narrow by design: only pure tax-activity phrases with no sector meaning.
    assert not j._is_gst_nature_deflection("hum toh bas mithai aur namkeen banate aur bechte hain")
    assert not j._is_gst_nature_deflection("Probe Traders, a hardware shop in Pune")
    assert not j._is_gst_nature_deflection("leather bags")
    # mentions GST but carries a real description → NOT a deflection (no pure tax-code phrase present)
    assert not j._is_gst_nature_deflection("we sell sweets, we're GST registered")
    # 'retail'/'wholesale' ARE usable business sectors, deliberately NOT deflection phrases
    assert not j._is_gst_nature_deflection("retail business")
    assert not j._is_gst_nature_deflection("")


def test_reprompt_gst_nature_asks_what_they_sell_both_locales():
    r = j._reprompt_gst_nature({"field": "business_type", "draft_value": "sweets"})
    assert r["done"] is False
    assert r["re_present"] is True
    assert r["reply_en"] and r["reply_hi"]
    # must NOT echo a rejection of the draft — it's a deflection, not a 'no'
    assert "sweets" not in r["reply_en"].lower()


# --- item 1: skip defer-ack -----------------------------------------------------------------------


def test_defer_ack_copy_present_both_locales():
    assert j._DEFER_ACK["en"] and j._DEFER_ACK["hi"]


def test_prefix_defer_ack_prepends_both_locales():
    out = j._prefix_defer_ack({"reply_en": "What are your hours?", "reply_hi": "समय?", "done": False})
    assert out["reply_en"].startswith(j._DEFER_ACK["en"])
    assert "What are your hours?" in out["reply_en"]
    assert out["reply_hi"].startswith(j._DEFER_ACK["hi"])
    assert out["done"] is False


def test_prefix_defer_ack_empty_hi_degrades_to_ack_only():
    out = j._prefix_defer_ack({"reply_en": "Q?", "reply_hi": "", "done": False})
    assert out["reply_hi"] == j._DEFER_ACK["hi"], "empty reply_hi → the ack alone, stripped"


# --- item 6: kickoff-token re-tap is a NON-answer --------------------------------------------------


@pytest.mark.parametrize("body", ["Complete Setup", "complete setup", "  COMPLETE SETUP  "])
def test_is_kickoff_token_matches_the_exact_button_body(body):
    assert j._is_kickoff_token(body) is True


@pytest.mark.parametrize(
    "body",
    ["complete setup please", "let's complete the setup", "setup", "9am to 9pm", "", "haan"],
)
def test_is_kickoff_token_rejects_non_exact_bodies(body):
    assert j._is_kickoff_token(body) is False


# --- VT-660: honest journey completion — gate on profile_collection_complete, not queue-exhaustion --
#
# The j05_b2b_onboarding_thin_discovery Tier-1 wrong_action: a THIN 2a draft composed a short/empty
# queue that exhausted after ONE answer, so handle_reply's queue-exhaustion path emitted the completion
# closer ("that's everything we need to get started … setting up your assistant now") while
# conductor.profile_collection_complete would still say more necessary fields remain. These pin the fix
# WITHOUT a DB: the walker's DB seams (get_journey / _advance / _complete / _install_recomposed_queue /
# _tenant_phase_and_type / _compose_queue) are monkeypatched to in-memory fakes, so the branching logic
# (complete-vs-hold-vs-recompose) is exercised for real while the leaf writes are stubbed.

_COMPLETION_MARK = "that's everything we need to get started"


class _FakeJourneyState:
    """A tiny in-memory stand-in for the onboarding_journey row so handle_reply's read/advance/install
    seams round-trip without Postgres. Records _complete / _install calls for assertions."""

    def __init__(self, *, queue, cursor=0, answers=None, skipped=None, status="active", last_sid=None):
        self.s = {
            "status": status,
            "question_queue": list(queue),
            "cursor": cursor,
            "answers": dict(answers or {}),
            "skipped": list(skipped or []),
            "last_message_sid": last_sid,
            "recent_turns": [],
            "conversation_summary": None,
        }
        self.complete_calls = 0
        self.installs: list[tuple[list, str | None]] = []

    def snapshot(self):
        s = self.s
        return {
            **s,
            "question_queue": list(s["question_queue"]),
            "answers": dict(s["answers"]),
            "skipped": list(s["skipped"]),
        }


def _wire_walker(monkeypatch, state: _FakeJourneyState, *, profile_complete: bool,
                 recompose_queue=None):
    """Monkeypatch the walker's DB/decision seams onto ``state``. ``profile_complete`` controls the
    authoritative signal; ``recompose_queue`` is what _compose_queue yields on the incomplete path."""
    monkeypatch.setattr(j, "get_journey", lambda tid: state.snapshot())

    def _advance(tid, cur, ans, skp, sid):
        state.s["cursor"] = cur
        state.s["answers"] = dict(ans)
        state.s["skipped"] = list(skp)
        state.s["last_message_sid"] = sid

    def _complete(tid):
        state.s["status"] = "complete"
        state.complete_calls += 1

    def _install(tid, queue, sid):
        state.s["question_queue"] = list(queue)
        state.s["cursor"] = 0
        state.s["last_message_sid"] = sid
        state.installs.append((list(queue), sid))

    monkeypatch.setattr(j, "_advance", _advance)
    monkeypatch.setattr(j, "_complete", _complete)
    monkeypatch.setattr(j, "_install_recomposed_queue", _install)
    monkeypatch.setattr(j, "_tenant_phase_and_type", lambda tid: (None, "restaurant"))
    monkeypatch.setattr(j, "_confirm", lambda *a, **k: None)
    monkeypatch.setattr(j, "_journey_profile_complete", lambda *a, **k: profile_complete)
    monkeypatch.setattr(j, "_compose_queue", lambda tid, bt: list(recompose_queue or []))


_GAP_Q = {"field": "city", "kind": "gap", "prompt_en": "Which city are you in?", "prompt_hi": "शहर?"}


# --- _journey_profile_complete: reuse of the authoritative deterministic signal -------------------


@pytest.mark.parametrize("signal", [True, False])
def test_journey_profile_complete_passes_through_conductor_signal(monkeypatch, signal):
    # Delegates verbatim to conductor.profile_collection_complete over the live draft.
    monkeypatch.setattr(
        "orchestrator.onboarding.conductor.profile_collection_complete", lambda **k: signal
    )
    monkeypatch.setattr("orchestrator.onboarding.draft_profile.get_draft", lambda tid: {"attributes": {}})
    assert j._journey_profile_complete("t", "restaurant", {"city": "Pune"}, []) is signal


def test_journey_profile_complete_fails_safe_to_complete_on_error(monkeypatch):
    # CRITICAL regression bar: a derivation error must NEVER strand a tenant behind the status='complete'
    # gate — it degrades to today's queue-exhaustion==complete behaviour (returns True).
    def _boom(tid):
        raise RuntimeError("draft read blew up")

    monkeypatch.setattr("orchestrator.onboarding.draft_profile.get_draft", _boom)
    assert j._journey_profile_complete("t", "restaurant", {}, []) is True


# --- handle_reply point-2 (after _advance exhausts the queue) -------------------------------------


def test_point2_profile_complete_still_fires_completion(monkeypatch):
    # REGRESSION BAR: when the profile IS complete at queue-exhaustion (the normal full-queue case,
    # where exhaustion and profile_collection_complete AGREE) the closer MUST still fire — else agent
    # activation (onboarding_gate hard-requires status='complete') never happens.
    state = _FakeJourneyState(queue=[_GAP_Q], cursor=0)
    _wire_walker(monkeypatch, state, profile_complete=True)
    r = j.handle_reply("t", "Pune", "sid-1")
    assert r["done"] is True
    assert _COMPLETION_MARK in r["reply_en"]
    assert state.complete_calls == 1
    assert state.s["status"] == "complete"


def test_point2_incomplete_thin_draft_holds_no_completion(monkeypatch):
    # THE j05 REPRODUCTION: queue exhausts after one answer but the profile is NOT complete and the thin
    # draft still yields nothing to ask → an honest HOLDING message, NEVER the premature closer.
    state = _FakeJourneyState(queue=[_GAP_Q], cursor=0)
    _wire_walker(monkeypatch, state, profile_complete=False, recompose_queue=[])
    r = j.handle_reply("t", "Pune", "sid-1")
    assert r["done"] is False
    assert _COMPLETION_MARK not in r["reply_en"]
    assert r["reply_en"] == j._opener()["prompt_en"]
    assert state.complete_calls == 0
    assert state.s["status"] == "active"


def test_point2_incomplete_recompose_presents_pending_question(monkeypatch):
    # Queue exhausts, profile incomplete, but the draft has since populated more necessities → recompose
    # + present the pending question (NOT the closer). The new queue is installed (cursor reset to head).
    pending = {"field": "about", "kind": "gap", "prompt_en": "Tell us about your business", "prompt_hi": "बताइए?"}
    state = _FakeJourneyState(queue=[_GAP_Q], cursor=0)
    _wire_walker(monkeypatch, state, profile_complete=False, recompose_queue=[pending])
    r = j.handle_reply("t", "Pune", "sid-1")
    assert r["done"] is False
    assert r["reply_en"] == "Tell us about your business"
    assert r.get("re_present") is True
    assert _COMPLETION_MARK not in r["reply_en"]
    assert state.complete_calls == 0
    assert state.installs and state.installs[0][0] == [pending]
    assert state.s["question_queue"] == [pending]


# --- handle_reply point-1 (queue already exhausted at entry — the follow-up-turn-after-hold case) --


def test_point1_incomplete_at_entry_holds_no_completion(monkeypatch):
    # A later inbound arrives while the queue is exhausted-but-active (a prior hold left cursor past end).
    # Entry-path exhaustion is gated the same way: incomplete + nothing to recompose → hold, not a closer.
    state = _FakeJourneyState(queue=[_GAP_Q], cursor=1)  # cursor past end → _current is None at entry
    _wire_walker(monkeypatch, state, profile_complete=False, recompose_queue=[])
    r = j.handle_reply("t", "any nudge", "sid-2")
    assert r["done"] is False
    assert _COMPLETION_MARK not in r["reply_en"]
    assert state.complete_calls == 0
    assert state.s["status"] == "active"


def test_point1_complete_at_entry_fires_completion(monkeypatch):
    # Entry-path exhaustion with a genuinely-complete profile → the closer fires (regression bar, point 1).
    state = _FakeJourneyState(queue=[_GAP_Q], cursor=1, answers={"city": "Pune"})
    _wire_walker(monkeypatch, state, profile_complete=True)
    r = j.handle_reply("t", "thanks", "sid-2")
    assert r["done"] is True
    assert _COMPLETION_MARK in r["reply_en"]
    assert state.complete_calls == 1
    assert state.s["status"] == "complete"


# --- VT-660 THIRD seam: the turn-brain exhaustion-completion (ONBOARDING_TURN_BRAIN path) ----------
#
# _handle_reply_with_turn_brain emits the SAME _completion_message template on queue-exhaustion when
# there is NO populate-first card. That is the identical premature-completion bug as handle_reply, but
# behind the ONBOARDING_TURN_BRAIN flag — so the fix must be FLAG-INDEPENDENT. These pin: the CARD
# branch is a legitimate close (untouched); the NO-CARD branch is gated on _complete_or_hold exactly
# like the walker. Same no-DB approach: the turn-brain's leaf seams are monkeypatched.


def _wire_turn_brain(monkeypatch, state, *, profile_complete, recompose_queue=None, populated=None,
                     answers=None, new_cursor=None, plan_reply="brain reply", plan_buttons=None):
    """Extend _wire_walker with the turn-brain path's own leaf seams. ``populated`` drives the
    populate-first card; ``answers``/``new_cursor`` stand in for _apply_turn_plan / cursor-advance."""
    _wire_walker(monkeypatch, state, profile_complete=profile_complete, recompose_queue=recompose_queue)
    monkeypatch.setattr(j, "_maybe_refresh_owner_website", lambda *a, **k: None)
    monkeypatch.setattr(j, "populate_profile_from_draft", lambda tid: dict(populated or {}))
    monkeypatch.setattr(j, "_apply_turn_plan", lambda tid, g, plan, da: (dict(answers or {}), []))
    monkeypatch.setattr(
        j, "_advance_cursor_past_answered",
        lambda g, a, s: (new_cursor if new_cursor is not None else len(g.get("question_queue") or [])),
    )
    monkeypatch.setattr(j, "_append_recent_turns", lambda *a, **k: None)
    monkeypatch.setattr("orchestrator.onboarding.draft_profile.get_draft", lambda tid: {"attributes": {}})
    plan = SimpleNamespace(reply_text=plan_reply, buttons=list(plan_buttons or []),
                           extracted_answers={}, mark_confirmed=())
    monkeypatch.setattr("orchestrator.onboarding.turn_brain.compose_turn", lambda *a, **k: plan)
    return plan


def test_turn_brain_no_card_complete_fires_completion(monkeypatch):
    # REGRESSION BAR (turn-brain): no card + profile COMPLETE → the closer fires (recap names the field).
    state = _FakeJourneyState(queue=[_GAP_Q], cursor=0)
    _wire_turn_brain(monkeypatch, state, profile_complete=True, answers={"city": "Pune"}, new_cursor=1)
    r = j._handle_reply_with_turn_brain("t", "Pune", "sid-tb-1")
    assert r["turn_brain"] is True
    assert r["done"] is True
    assert _COMPLETION_MARK in r["reply_text"]
    assert "Pune" in r["reply_text"]
    assert state.complete_calls == 1
    assert state.s["status"] == "complete"


def test_turn_brain_no_card_incomplete_thin_draft_holds(monkeypatch):
    # THE j05 REPRODUCTION on the turn-brain seam: no card + INCOMPLETE + thin draft → honest hold,
    # never the premature closer, journey stays active.
    state = _FakeJourneyState(queue=[_GAP_Q], cursor=0)
    _wire_turn_brain(monkeypatch, state, profile_complete=False, recompose_queue=[],
                     answers={"city": "Pune"}, new_cursor=1)
    r = j._handle_reply_with_turn_brain("t", "Pune", "sid-tb-1")
    assert r["done"] is False
    assert _COMPLETION_MARK not in r["reply_text"]
    assert r["reply_text"] == j._opener()["prompt_en"]
    assert state.complete_calls == 0
    assert state.s["status"] == "active"


def test_turn_brain_no_card_incomplete_recompose_presents_question(monkeypatch):
    # No card + INCOMPLETE + the draft has since populated more necessities → recompose + present the
    # pending question (NOT the closer); journey stays active; the fresh queue is installed.
    pending = {"field": "about", "kind": "gap", "prompt_en": "Tell us about your business", "prompt_hi": "बताइए?"}
    state = _FakeJourneyState(queue=[_GAP_Q], cursor=0)
    _wire_turn_brain(monkeypatch, state, profile_complete=False, recompose_queue=[pending],
                     answers={"city": "Pune"}, new_cursor=1)
    r = j._handle_reply_with_turn_brain("t", "Pune", "sid-tb-1")
    assert r["done"] is False
    assert r["reply_text"] == "Tell us about your business"
    assert _COMPLETION_MARK not in r["reply_text"]
    assert state.complete_calls == 0
    assert state.installs and state.installs[0][0] == [pending]


def test_turn_brain_card_branch_completes_unchanged(monkeypatch):
    # The CARD branch is a LEGITIMATE populate-first close — NOT gated. Even with profile_complete=False
    # the card presents + the journey completes (proves the gate leaves the card path untouched), and the
    # generic _completion_message closer is NOT stacked over the card.
    state = _FakeJourneyState(queue=[_GAP_Q], cursor=0)
    _wire_turn_brain(monkeypatch, state, profile_complete=False,
                     populated={"business_type": "restaurant", "city": "Pune"},
                     answers={"city": "Pune"}, new_cursor=1,
                     plan_reply="Here's your profile — tap to edit anything")
    r = j._handle_reply_with_turn_brain("t", "Pune", "sid-tb-1")
    assert r["done"] is True
    assert r["reply_text"] == "Here's your profile — tap to edit anything"
    assert _COMPLETION_MARK not in r["reply_text"]
    assert state.complete_calls == 1
    assert state.s["status"] == "complete"


# --- VT-662: deterministic about-gap capture floor (turn-brain ignored_speech_act re-ask) ----------
#
# The LLM turn-brain sometimes re-asks the open free-text ``about`` gap on the SAME turn the owner
# DESCRIBED their business (measured on j05, 2/2 byte-identical). ``_apply_turn_plan`` records only what
# the LLM extracted, so nothing captured the missed gap and it was re-asked next turn. The floor records
# a SUBSTANTIVE owner statement into an open ``about`` gap so the brain sees it ALREADY-COLLECTED and
# cannot re-ask. Only ``about`` (a finite schema field), only while open — never a keyword list.

_ABOUT_GAP = {"field": "about", "kind": "gap",
              "prompt_en": "Tell us about your business", "prompt_hi": "बताइए?"}
_J05_TURN = "We stock and distribute packaged goods in bulk to retail stores across the region."


@pytest.mark.parametrize(
    "body,expected",
    [
        (_J05_TURN, True),                                   # the j05 turn — a real description
        ("We supply goods wholesale to businesses", True),   # 5 tokens, substantive
        ("hi", False),                                       # bare greeting
        ("namaste", False),                                  # bare greeting (HI)
        ("haan theek hai", False),                           # bare gap-affirmation particles
        ("no", False),                                       # bare negation
        ("skip", False),                                     # skip token
        ("Mumbai", False),                                   # single token — a value, not a description
        ("what products do you mean?", False),               # a question (has '?')
        ("", False),                                         # empty
    ],
)
def test_is_substantive_statement(body, expected):
    assert j._is_substantive_statement(body) is expected


def test_capture_about_gap_records_when_open_and_substantive():
    g = {"question_queue": [_ABOUT_GAP], "cursor": 0}
    answers: dict = {}
    assert j._capture_missed_about_gap(g, answers, [], _J05_TURN) is True
    assert answers["about"] == _J05_TURN.strip()


def test_capture_about_gap_noop_when_already_answered():
    # The brain DID extract about → the floor must NOT clobber it.
    g = {"question_queue": [_ABOUT_GAP], "cursor": 0}
    answers = {"about": "brain-extracted value"}
    assert j._capture_missed_about_gap(g, answers, [], _J05_TURN) is False
    assert answers["about"] == "brain-extracted value"


def test_capture_about_gap_noop_when_skipped():
    g = {"question_queue": [_ABOUT_GAP], "cursor": 0}
    answers: dict = {}
    assert j._capture_missed_about_gap(g, answers, ["about"], _J05_TURN) is False
    assert "about" not in answers


def test_capture_about_gap_noop_when_no_about_gap_in_queue():
    g = {"question_queue": [_GAP_Q], "cursor": 0}  # a city gap, no about
    answers: dict = {}
    assert j._capture_missed_about_gap(g, answers, [], _J05_TURN) is False
    assert answers == {}


def test_capture_about_gap_noop_when_about_is_confirm_not_gap():
    # An about CONFIRM needs a real confirm/correct — not a free-text capture.
    g = {"question_queue": [{"field": "about", "kind": "confirm"}], "cursor": 0}
    answers: dict = {}
    assert j._capture_missed_about_gap(g, answers, [], _J05_TURN) is False
    assert answers == {}


def test_capture_about_gap_noop_when_body_is_greeting():
    g = {"question_queue": [_ABOUT_GAP], "cursor": 0}
    answers: dict = {}
    assert j._capture_missed_about_gap(g, answers, [], "hi there") is False
    assert "about" not in answers


def test_turn_brain_captures_missed_about_gap_end_to_end(monkeypatch):
    # THE j05 FIX on the turn-brain seam: the LLM extracted nothing (apply → {}), an about gap is open,
    # the owner gave a real description → the floor records `about` so the persisted answers carry it and
    # the brain cannot re-ask it next turn.
    state = _FakeJourneyState(queue=[_ABOUT_GAP], cursor=0)
    _wire_turn_brain(monkeypatch, state, profile_complete=False, answers={}, new_cursor=0,
                     plan_reply="What products or services does your business offer?")
    r = j._handle_reply_with_turn_brain("t", _J05_TURN, "sid-j05-1")
    assert r["done"] is False
    assert state.s["answers"]["about"] == _J05_TURN.strip()


def test_turn_brain_does_not_clobber_extracted_about(monkeypatch):
    # When the brain DID extract about, the floor is a no-op — the brain's value persists unchanged.
    state = _FakeJourneyState(queue=[_ABOUT_GAP], cursor=0)
    _wire_turn_brain(monkeypatch, state, profile_complete=False,
                     answers={"about": "packaged-goods wholesaler"}, new_cursor=0,
                     plan_reply="Got it — anything else?")
    r = j._handle_reply_with_turn_brain("t", _J05_TURN, "sid-j05-2")
    assert r["done"] is False
    assert state.s["answers"]["about"] == "packaged-goods wholesaler"
