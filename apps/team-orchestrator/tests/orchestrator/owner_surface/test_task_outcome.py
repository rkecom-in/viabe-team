"""VT-611 pre-work #1 — the Team-Manager loop's owner-notification composer.

Mirrors ``test_campaign_outcome.py``'s own shape: pure composer-honesty tests (no DB), then
mocked-seam tests for the wiring function (``task_store``/``twilio_send``/``owner_notification``/
``freeform_acks`` all monkeypatched at their defining module — no live network, no live DB; the
loop's OWN DB-backed settle-path tests in ``test_workflow.py`` already prove the CALL into this
module happens after a real settle, on real durable state).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.owner_surface import task_outcome as to  # noqa: E402


# ----------------------------- pure: composer honesty (EN) -------------------------------
def test_compose_completed_with_effect_states_done_en() -> None:
    body = to.compose_task_outcome_message("completed_with_effect", "send the winback message", locale="en")
    assert "Done" in body
    assert "send the winback message" in body
    assert "declined" not in body.lower()
    assert "no action" not in body.lower()


def test_compose_completed_with_effect_no_objective_degrades_gracefully_en() -> None:
    body = to.compose_task_outcome_message("completed_with_effect", "", locale="en")
    assert "Done" in body
    assert "None" not in body  # empty objective never renders as the string "None"


# ----------------------------- T15: quoted objective + stale-turn reconcile ---------------
def test_compose_quotes_the_objective_not_bare_echo() -> None:
    """T15 — the objective is a QUOTED reference, never a bare verbatim echo of the owner's
    imperative ("I looked into run a Facebook ad campaign for me, but…" read as a broken echo)."""
    body = to.compose_task_outcome_message("escalated", "run a Facebook ad campaign for me", locale="en")
    assert '"run a Facebook ad campaign for me"' in body


def test_owner_sent_newer_message_no_anchor_is_not_stale() -> None:
    assert to._owner_sent_newer_message("t", {"source_message_ref": None}) is False
    assert to._owner_sent_newer_message("t", {}) is False


def test_owner_sent_newer_message_read_error_fails_soft(monkeypatch) -> None:
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("orchestrator.db.tenant_connection", _boom)
    assert to._owner_sent_newer_message("t", {"source_message_ref": "SMx"}) is False


def test_notify_prefixes_reconcile_framing_when_stale(monkeypatch) -> None:
    """T15 — a closure landing after the owner moved on carries the 'earlier request' framing."""
    from uuid import uuid4

    sent: list[str] = []
    task = {
        "owner_notification_status": "pending",
        "terminal_outcome": "escalated",
        "objective": {"objective": "run a Facebook ad campaign for me"},
        "source_message_ref": "SM" + "1" * 32,
    }
    monkeypatch.setattr("orchestrator.manager.task_store.get_task", lambda t, k: task)
    monkeypatch.setattr(
        "orchestrator.manager.task_store.set_owner_notification_status",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(to, "_check_send_idempotency_hit", lambda t, k: False)
    monkeypatch.setattr(to, "_record_send_idempotency", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(to, "_spawning_turn_already_answered", lambda t, task: False)
    monkeypatch.setattr(to, "_owner_sent_newer_message", lambda t, task: True)
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale", lambda t: "en"
    )
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_freeform_message",
        lambda body, recipient, **k: sent.append(body) or "SM_OK",
    )

    dispatched = to.maybe_notify_owner_of_task_outcome(
        str(uuid4()), str(uuid4()), recipient_phone="+10000000001"
    )

    assert dispatched is True
    assert sent and sent[0].startswith("About your earlier request — ")
    assert '"run a Facebook ad campaign for me"' in sent[0]


def test_compose_completed_no_action_never_claims_an_effect_en() -> None:
    body = to.compose_task_outcome_message("completed_no_action", "check the refund status", locale="en")
    assert "no action was needed" in body
    assert "Done" not in body
    assert "declined" not in body.lower()


def test_compose_cancelled_must_read_as_a_decline_en() -> None:
    body = to.compose_task_outcome_message("cancelled", "send a 20% discount campaign", locale="en")
    assert "declined" in body.lower()
    assert "Done" not in body
    assert "no action was needed" not in body


def test_compose_cancelled_no_objective_still_declines_en() -> None:
    body = to.compose_task_outcome_message("cancelled", "", locale="en")
    assert "declined" in body.lower()


def test_compose_escalated_is_honest_never_a_false_success_en() -> None:
    """A blocked/escalated terminal MUST read as an honest "couldn't complete it, I've stopped":
    never "Done"/success, never "declined" (the owner did NOT decline it), never "no action was
    needed" (action WAS needed and attempted). Impossible_promise honesty fix (official §2,
    2026-07-10): NO phantom "my team", NO unbacked "I'll follow up" promise — nothing auto-retries
    a blocked task, so promising follow-up was a Tier-1 trust-breaker."""
    body = to.compose_task_outcome_message("escalated", "re-engage the lapsed customers", locale="en")
    assert "re-engage the lapsed customers" in body
    assert "couldn't complete" in body.lower()
    assert "stopped" in body.lower()            # states the honest stop
    # impossible_promise regression guard — the phantom-team / follow-up promise MUST be gone.
    assert "flagged" not in body.lower()
    assert "follow up" not in body.lower()
    assert "team" not in body.lower()
    assert "done" not in body.lower()          # never a false success
    assert "declined" not in body.lower()       # the owner did not decline
    assert "no action was needed" not in body   # action WAS needed


def test_extract_objective_omits_pii_heavy_objective_never_leaks_to_owner() -> None:
    """DF6 — the stored objective is REDACTED at write, so a PII value the owner typed lives here as
    a token. Quoting it back must never surface the raw token (cross_tenant_phone_reassign_probe
    fabrication, official §2 2026-07-10) AND must not surface the neutral placeholder MID-SENTENCE
    either (a garbled "…his number is a phone number, use that one…"): the whole objective quote is
    OMITTED, and the composed closure degrades to its generic, objective-free phrasing."""
    task = {"objective": {"objective": "connect this to his shop, his number is "
                          "phone_tok_dffe2cc3a97476cf, use that one"}}
    obj = to._extract_objective_text(task)
    assert obj == ""                                 # PII-heavy objective omitted, not placeholder-quoted
    # and the composed closure that would have quoted it is clean AND ungarbled end-to-end
    body = to.compose_task_outcome_message("escalated", obj, locale="en")
    assert "phone_tok_" not in body and "_tok_" not in body
    assert "a phone number" not in body              # no garbled placeholder surfaced mid-sentence
    assert "couldn't complete" in body.lower()       # still the honest generic escalated closure


def test_compose_escalated_no_objective_degrades_gracefully_en() -> None:
    body = to.compose_task_outcome_message("escalated", "", locale="en")
    assert "couldn't complete" in body.lower()
    assert "None" not in body
    assert "done" not in body.lower()
    assert "team" not in body.lower()           # impossible_promise guard, empty-objective path too


def test_compose_escalated_hi_is_honest_no_false_success() -> None:
    body = to.compose_task_outcome_message("escalated", "ग्राहकों से दोबारा जुड़ें", locale="hi")
    assert "ग्राहकों से दोबारा जुड़ें" in body
    assert "रोक दिया" in body                     # states the honest stop
    assert "team" not in body                    # impossible_promise fix: no phantom team
    assert "update दूँगा" not in body            # no unbacked follow-up promise
    assert "हो गया" not in body                  # never the completed_with_effect "done"
    assert "अस्वीकृत" not in body                 # never the cancelled "declined"


# ----------------------------- pure: composer (HI) --------------------------------------
def test_compose_cancelled_hi_says_declined() -> None:
    body = to.compose_task_outcome_message("cancelled", "बिक्री अभियान", locale="hi")
    assert "अस्वीकृत" in body  # "declined" — the bilingual honesty pin
    assert "बिक्री अभियान" in body


def test_compose_completed_with_effect_hi() -> None:
    body = to.compose_task_outcome_message("completed_with_effect", "रिफंड भेजें", locale="hi")
    assert "हो गया" in body
    assert "रिफंड भेजें" in body


def test_compose_completed_no_action_hi() -> None:
    body = to.compose_task_outcome_message("completed_no_action", "", locale="hi")
    assert "कोई कार्रवाई की ज़रूरत नहीं" in body


def test_compose_unknown_locale_falls_back_to_en() -> None:
    assert to.compose_task_outcome_message(
        "completed_with_effect", "x", locale="xx"
    ) == to.compose_task_outcome_message("completed_with_effect", "x", locale="en")


# ----------------------------- VT-680 (§7C online impact judge) — the quality note --------
def test_compose_met_verdict_is_byte_identical_to_no_verdict() -> None:
    """'met' must never change the message — the honest note is scoped to partial/unmet only."""
    for outcome in ("completed_with_effect", "completed_no_action"):
        base = to.compose_task_outcome_message(outcome, "handle it", locale="en")
        met = to.compose_task_outcome_message(outcome, "handle it", locale="en", impact_verdict="met")
        assert base == met


def test_compose_unjudged_verdict_is_byte_identical_to_no_verdict() -> None:
    """'unjudged' (flag off, or the judge failed) must ALSO leave the message unchanged — the
    flag-off / judge-failure no-drift requirement (VT-680 D6)."""
    for outcome in ("completed_with_effect", "completed_no_action"):
        base = to.compose_task_outcome_message(outcome, "handle it", locale="en")
        unjudged = to.compose_task_outcome_message(
            outcome, "handle it", locale="en", impact_verdict="unjudged"
        )
        assert base == unjudged


def test_compose_partial_verdict_appends_honest_quality_note_en() -> None:
    body = to.compose_task_outcome_message(
        "completed_with_effect", "send the winback campaign", locale="en", impact_verdict="partial"
    )
    assert "Done" in body and "send the winback campaign" in body
    assert "take another pass" in body.lower()


def test_compose_unmet_verdict_appends_honest_quality_note_completed_no_action_en() -> None:
    body = to.compose_task_outcome_message(
        "completed_no_action", "check refund status", locale="en", impact_verdict="unmet"
    )
    assert "no action was needed" in body
    assert "take another pass" in body.lower()


def test_compose_partial_verdict_hi() -> None:
    body = to.compose_task_outcome_message(
        "completed_with_effect", "रिफंड भेजें", locale="hi", impact_verdict="partial"
    )
    assert "हो गया" in body
    assert "दोबारा कोशिश" in body


def test_compose_cancelled_ignores_impact_verdict() -> None:
    """'cancelled' is a decline — "want me to take another pass?" would be a non-sequitur, so the
    note is withheld even if a caller erroneously passed one (defense in depth: the real caller,
    ``_settle_declined_approval``'s notify, never passes one — see workflow.py)."""
    body = to.compose_task_outcome_message(
        "cancelled", "20% discount campaign", locale="en", impact_verdict="partial"
    )
    assert "declined" in body.lower()
    assert "take another pass" not in body.lower()


def test_compose_escalated_ignores_impact_verdict() -> None:
    body = to.compose_task_outcome_message(
        "escalated", "re-engage the lapsed customers", locale="en", impact_verdict="unmet"
    )
    assert "couldn't complete" in body.lower()
    assert "take another pass" not in body.lower()


def test_compose_unrecognized_verdict_value_appends_nothing() -> None:
    """A stray/unexpected verdict string (schema drift) degrades to no note, never a crash."""
    body = to.compose_task_outcome_message(
        "completed_with_effect", "handle it", locale="en", impact_verdict="something_else"
    )
    assert "take another pass" not in body.lower()


# ----------------------------- pure: _extract_objective_text -----------------------------
def test_extract_objective_text_reads_the_redacted_text() -> None:
    task = {"objective": {"objective": "handle the refund request", "schema_version": 1}}
    assert to._extract_objective_text(task) == "handle the refund request"


@pytest.mark.parametrize("objective", [None, {}, {"objective": None}, "not-a-dict"])
def test_extract_objective_text_defensive_on_unexpected_shapes(objective) -> None:
    assert to._extract_objective_text({"objective": objective}) == ""


# ----------------------------- wiring: maybe_notify_owner_of_task_outcome ----------------
def _patch(monkeypatch, *, task: dict, send_result="SM_OUT", send_raises: Exception | None = None):
    """Patch every seam maybe_notify_owner_of_task_outcome touches; return a captures dict."""
    seen: dict = {"ledger": [], "flips": [], "alerts": [], "idempotency_writes": []}

    monkeypatch.setattr("orchestrator.manager.task_store.get_task", lambda tid, taskid: dict(task))
    # VT-611 fix round: the crash/replay idempotency check + write are DB-touching seams (own
    # tenant_connection) — no live DB in this file (mocked-seam tests only, per the module
    # docstring). Default: no prior hit (every test here is a "first attempt" unless it overrides
    # this itself), and the write is captured, not persisted.
    monkeypatch.setattr(to, "_check_send_idempotency_hit", lambda tid, key: False)
    monkeypatch.setattr(
        to, "_write_send_idempotency_record",
        lambda tid, key, sid: seen["idempotency_writes"].append((key, sid)),
    )
    # DF6: the suppress-decision read (_spawning_turn_already_answered) is a DB-touching seam (its
    # own tenant_connection) — default it to "not answered" so every wiring test here FIRES the
    # closure; the suppress path is exercised explicitly in the DF6 tests below.
    monkeypatch.setattr(to, "_spawning_turn_already_answered", lambda tid, task: False)

    def _flip(tid, taskid, status, *, expected_from=None):
        seen["flips"].append((status, expected_from))
        task["owner_notification_status"] = status  # mutate the same dict the mock get_task closed over
        return True

    monkeypatch.setattr("orchestrator.manager.task_store.set_owner_notification_status", _flip)
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale", lambda t: "en"
    )

    def _send(body, phone, **kw):
        if send_raises is not None:
            raise send_raises
        seen["body"] = body
        seen["phone"] = phone
        seen["send_kwargs"] = kw
        return send_result

    monkeypatch.setattr("orchestrator.utils.twilio_send.send_freeform_message", _send)
    monkeypatch.setattr(
        "orchestrator.owner_surface.owner_notification.record_owner_notification",
        lambda tid, label, sid, **kw: seen["ledger"].append((label, sid, kw)),
    )
    monkeypatch.setattr(
        "orchestrator.alerts.dispatch.dispatch_alert", lambda trig: seen["alerts"].append(trig)
    )
    return seen


def _pending_task(outcome: str, objective: str = "handle it") -> dict:
    return {
        "id": str(uuid4()),
        "objective": {"objective": objective},
        "terminal_outcome": outcome,
        "owner_notification_status": "pending",
    }


def test_notify_completed_with_effect_sends_flips_delivered_and_records_ledger(monkeypatch) -> None:
    task = _pending_task("completed_with_effect", "send the winback campaign")
    seen = _patch(monkeypatch, task=task)
    tenant_id, task_id = uuid4(), uuid4()

    sent = to.maybe_notify_owner_of_task_outcome(tenant_id, task_id, recipient_phone="+919811111111")

    assert sent is True
    assert "Done" in seen["body"] and "send the winback campaign" in seen["body"]
    assert seen["phone"] == "+919811111111"
    assert seen["flips"] == [("delivered", ("pending",))]
    assert seen["ledger"] == [("task_outcome_report", "SM_OUT", {"run_id": task_id})]
    assert seen["alerts"] == []


def test_notify_threads_impact_verdict_into_composed_body(monkeypatch) -> None:
    """VT-680 — ``impact_verdict`` passed into the wiring function reaches the composed message."""
    task = _pending_task("completed_with_effect", "send the winback campaign")
    seen = _patch(monkeypatch, task=task)

    sent = to.maybe_notify_owner_of_task_outcome(
        uuid4(), uuid4(), recipient_phone="+919811111111", impact_verdict="partial"
    )

    assert sent is True
    assert "take another pass" in seen["body"].lower()


def test_notify_no_impact_verdict_is_byte_identical_to_pre_vt680(monkeypatch) -> None:
    """The default (no ``impact_verdict`` passed) must compose exactly the pre-VT-680 message."""
    task = _pending_task("completed_with_effect", "send the winback campaign")
    seen = _patch(monkeypatch, task=task)

    to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert "take another pass" not in seen["body"].lower()
    assert seen["body"] == to.compose_task_outcome_message(
        "completed_with_effect", "send the winback campaign", locale="en"
    )


def test_notify_cancelled_message_class_reads_as_declined(monkeypatch) -> None:
    task = _pending_task("cancelled", "20% discount campaign")
    seen = _patch(monkeypatch, task=task)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is True
    assert "declined" in seen["body"].lower()
    assert seen["flips"] == [("delivered", ("pending",))]


def test_notify_completed_no_action_message_class_never_claims_effect(monkeypatch) -> None:
    task = _pending_task("completed_no_action", "check refund status")
    seen = _patch(monkeypatch, task=task)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is True
    assert "no action was needed" in seen["body"]
    assert "Done" not in seen["body"]


def test_notify_escalated_sends_honest_stopped_message_flips_delivered(monkeypatch) -> None:
    """'escalated' is a HANDLED outcome (a blocked _block_* / review-escalate path writes it
    'pending'): it SENDS the honest "couldn't complete it, I've stopped" closure and flips
    delivered — no longer the pre-Step-5 silent skip. Impossible_promise fix (official §2,
    2026-07-10): the closure carries NO phantom "flagged for my team" / follow-up promise."""
    task = _pending_task("escalated", "re-engage the lapsed customers")
    seen = _patch(monkeypatch, task=task)
    tenant_id, task_id = uuid4(), uuid4()

    sent = to.maybe_notify_owner_of_task_outcome(tenant_id, task_id, recipient_phone="+919811111111")

    assert sent is True
    assert "couldn't complete" in seen["body"].lower()
    assert "flagged" not in seen["body"].lower()          # impossible_promise regression guard
    assert "follow up" not in seen["body"].lower()
    assert "Done" not in seen["body"] and "declined" not in seen["body"].lower()
    assert seen["flips"] == [("delivered", ("pending",))]
    assert seen["ledger"] == [("task_outcome_report", "SM_OUT", {"run_id": task_id})]


def test_notify_is_idempotent_already_delivered_no_op(monkeypatch) -> None:
    """The dedup: owner_notification_status != 'pending' is a clean no-op — no send attempted,
    no second flip, no double-ledger row (a re-run of the same DBOS step on replay/retry)."""
    task = _pending_task("completed_with_effect")
    task["owner_notification_status"] = "delivered"  # already handled
    seen = _patch(monkeypatch, task=task)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is False
    assert "body" not in seen
    assert seen["flips"] == []
    assert seen["ledger"] == []


def test_notify_unhandled_terminal_outcome_is_out_of_scope_no_send(monkeypatch) -> None:
    """'failed' has no path that writes it 'pending' today (VT-632 Step 5 wired 'escalated', not
    'failed') — this is the defensive scope fence for the still-unhandled value: skip, no send, no
    flip. ('escalated' is now HANDLED — see test_notify_escalated_* below.)"""
    task = _pending_task("failed")
    seen = _patch(monkeypatch, task=task)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is False
    assert "body" not in seen
    assert seen["flips"] == []


def test_notify_no_phone_defers_leaves_pending(monkeypatch) -> None:
    task = _pending_task("completed_with_effect")
    seen = _patch(monkeypatch, task=task)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone=None)

    assert sent is False
    assert "body" not in seen
    assert seen["flips"] == []  # left 'pending' — deferred, not failed
    assert task["owner_notification_status"] == "pending"


def test_notify_window_closed_defers_leaves_pending_never_fabricates_a_send(monkeypatch) -> None:
    """The freeform-vs-template fork's window-closed branch: NEVER a fabricated content SID,
    NEVER a dishonest 'delivered' — deferred (left 'pending'), no alert (this is not a failure,
    it is an expected out-of-window defer)."""
    exc = RuntimeError("window closed")
    exc.code = 63016  # type: ignore[attr-defined]
    task = _pending_task("cancelled")
    seen = _patch(monkeypatch, task=task, send_raises=exc)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is False
    assert seen["flips"] == []
    assert seen["ledger"] == []
    assert seen["alerts"] == []
    assert task["owner_notification_status"] == "pending"


def test_notify_send_failure_flips_failed_and_alerts_fail_soft(monkeypatch) -> None:
    """A DEFINITIVE (non-window) send failure is NOT deferred — it flips 'failed' and fires the
    outbound_failure alert (an un-notified owner must be surfaced), but never raises (fail-soft:
    this runs right after the settle it reports on and must never unwind it)."""
    exc = RuntimeError("twilio 500")
    exc.code = 20500  # type: ignore[attr-defined]  # NOT the window-closed code
    task = _pending_task("completed_with_effect")
    seen = _patch(monkeypatch, task=task, send_raises=exc)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is False
    assert seen["flips"] == [("failed", ("pending",))]
    assert len(seen["alerts"]) == 1
    assert seen["alerts"][0].trigger_kind == "outbound_failure"


def test_notify_task_not_found_is_a_no_op(monkeypatch) -> None:
    monkeypatch.setattr("orchestrator.manager.task_store.get_task", lambda tid, taskid: None)
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_freeform_message",
        lambda *a, **kw: pytest.fail("must not send when the task cannot be loaded"),
    )
    assert to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+91981") is False


# ----------------------- fix round: crash/replay idempotency + fail-soft flips -----------------
def test_notify_idempotent_hit_skips_resend_completes_delivered_flip(monkeypatch) -> None:
    """A known ``send_idempotency_keys`` hit for this (task, outcome) means a PRIOR attempt's
    Twilio send already succeeded — only the flip (which never landed, per the crash window) never
    completed. This call must NOT re-send; it only completes the flip."""
    task = _pending_task("completed_with_effect")
    seen = _patch(monkeypatch, task=task)
    monkeypatch.setattr(to, "_check_send_idempotency_hit", lambda tid, key: True)
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_freeform_message",
        lambda *a, **kw: pytest.fail("must not re-send on an idempotency hit (crash/replay dedup)"),
    )

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is False  # no NEW dispatch this call — the send already happened pre-crash
    assert seen["flips"] == [("delivered", ("pending",))]
    assert seen["ledger"] == []  # no NEW ledger row either — this call made no send
    assert seen["idempotency_writes"] == []


def test_notify_replay_after_crash_before_flip_sends_once_then_skips(monkeypatch) -> None:
    """Literal crash/replay simulation: call the notify step TWICE against a task that STAYS
    'pending' both times (the delivered-flip is forced to no-op — simulating that it crashed
    before commit). Twilio must fire exactly ONCE; the second call finds the idempotency row the
    first call wrote and skips straight to (re-attempting) the flip."""
    task = _pending_task("completed_with_effect", "handle it")
    _patch(monkeypatch, task=task)  # default seams (locale/ledger/alerts); overridden below
    send_calls: list[str] = []

    def _send_and_record(body, phone, **kw):
        send_calls.append(body)
        return "SM_REPLAY"

    monkeypatch.setattr("orchestrator.utils.twilio_send.send_freeform_message", _send_and_record)

    # Stand in for the send_idempotency_keys ledger with a plain dict (keyed by idempotency_key).
    ledger: dict[str, str] = {}
    monkeypatch.setattr(
        to, "_write_send_idempotency_record",
        lambda tid, key, sid: ledger.__setitem__(key, sid),
    )
    monkeypatch.setattr(to, "_check_send_idempotency_hit", lambda tid, key: key in ledger)
    # The crash simulation: the flip NEVER actually commits (owner_notification_status stays
    # 'pending' for the replay to observe) — record the attempt, but don't mutate task state.
    flip_attempts: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        "orchestrator.manager.task_store.set_owner_notification_status",
        lambda tid, taskid, status, *, expected_from=None: flip_attempts.append(
            (status, expected_from)
        ),
    )

    tenant_id, task_id = uuid4(), uuid4()
    first = to.maybe_notify_owner_of_task_outcome(tenant_id, task_id, recipient_phone="+919811111111")
    second = to.maybe_notify_owner_of_task_outcome(tenant_id, task_id, recipient_phone="+919811111111")

    assert first is True
    assert second is False  # the idempotent-hit path on replay — no NEW dispatch
    assert len(send_calls) == 1  # exactly ONE Twilio call across both attempts
    assert flip_attempts == [
        ("delivered", ("pending",)), ("delivered", ("pending",)),
    ]  # both calls attempt (and both "crash before commit") the same flip


def test_notify_delivered_flip_raises_is_fail_soft_never_propagates(monkeypatch) -> None:
    """The delivered-flip write happens AFTER a successful, irreversible send. A DB error on THAT
    write must be caught + alerted, never propagate out of the notify step — the settle it reports
    on already committed; nothing here may unwind it."""
    task = _pending_task("completed_with_effect")
    seen = _patch(monkeypatch, task=task)

    def _flip_raises(tid, taskid, status, *, expected_from=None):
        raise RuntimeError("db connection reset")

    monkeypatch.setattr("orchestrator.manager.task_store.set_owner_notification_status", _flip_raises)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is True  # the message WAS dispatched — that's the truth this returns
    assert len(seen["alerts"]) == 1
    assert seen["alerts"][0].trigger_kind == "outbound_failure"


def test_notify_failed_flip_raises_is_fail_soft_never_propagates(monkeypatch) -> None:
    """Mirrors the above for the DEFINITIVE-send-failure branch's own flip (to 'failed', nested
    inside the send-failure except) — a DB error on that write must also never propagate; the
    outbound_failure alert still fires."""
    exc = RuntimeError("twilio 500")
    exc.code = 20500  # type: ignore[attr-defined]  # NOT the window-closed code
    task = _pending_task("completed_with_effect")
    seen = _patch(monkeypatch, task=task, send_raises=exc)

    def _flip_raises(tid, taskid, status, *, expected_from=None):
        raise RuntimeError("db connection reset")

    monkeypatch.setattr("orchestrator.manager.task_store.set_owner_notification_status", _flip_raises)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is False
    assert len(seen["alerts"]) == 1
    assert seen["alerts"][0].trigger_kind == "outbound_failure"


def test_notify_idempotency_ledger_insert_failure_does_not_block_delivered_flip(monkeypatch) -> None:
    """The idempotency-ledger INSERT is best-effort — a failure there (e.g. a DB hiccup right after
    the send) must not prevent the delivered-flip from being attempted; the notification still
    completes normally from the owner's perspective."""
    task = _pending_task("completed_with_effect")
    seen = _patch(monkeypatch, task=task)

    def _write_raises(tid, key, sid):
        raise RuntimeError("db connection reset")

    monkeypatch.setattr(to, "_write_send_idempotency_record", _write_raises)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is True
    assert seen["flips"] == [("delivered", ("pending",))]


# --------------------- DF6: stale-closure SUPPRESS (already-answered spawning turn) --------------
class _FakeConn:
    """Minimal ``tenant_connection`` stand-in: ``with conn as c: c.execute(...).fetchall()``."""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        return self

    def fetchall(self):
        return self._rows


_ACK_EN = "Got it — I'm on it and I'll update you shortly."  # runner._COMPLETED_NO_REPLY_FALLBACK
_ACK_HI = "समझ गया — मैं इस पर काम कर रहा हूँ और जल्द ही आपको अपडेट करूँगा।"


def test_is_substantive_owner_reply_excludes_ack_and_closures() -> None:
    # the interim "I'm on it" ack (both locales) is NOT a substantive answer
    assert to._is_substantive_owner_reply(_ACK_EN) is False
    assert to._is_substantive_owner_reply(_ACK_HI) is False
    # this module's own stale-closure (prefixed) + escalated closure body are NOT answers either
    assert to._is_substantive_owner_reply(
        to._STALE_CLOSURE_PREFIX["en"] + "anything"
    ) is False
    assert to._is_substantive_owner_reply(
        to.compose_task_outcome_message("escalated", "run an ad", locale="en")
    ) is False
    # a real honest connector answer IS substantive
    assert to._is_substantive_owner_reply(
        "WooCommerce isn't supported yet — I can connect Shopify or a Google Sheet."
    ) is True
    assert to._is_substantive_owner_reply("") is False


def test_spawning_turn_answered_true_when_substantive_reply_present(monkeypatch) -> None:
    """A substantive assistant turn recorded AFTER the spawning owner turn → answered (suppress)."""
    rows = [(_ACK_EN,), ("WooCommerce isn't supported yet — I can connect Shopify instead.",)]
    monkeypatch.setattr("orchestrator.db.tenant_connection", lambda tid: _FakeConn(rows))
    assert to._spawning_turn_already_answered(
        uuid4(), {"source_message_ref": "SM" + "1" * 32}
    ) is True


def test_spawning_turn_answered_false_on_ack_only_silent_stall(monkeypatch) -> None:
    """A genuine silent stall: the spawning turn got ONLY the "I'm on it" ack (no real reply) →
    NOT answered → the closure MUST still fire (VT-632 Step-5 no-silence guarantee)."""
    monkeypatch.setattr("orchestrator.db.tenant_connection", lambda tid: _FakeConn([(_ACK_EN,)]))
    assert to._spawning_turn_already_answered(
        uuid4(), {"source_message_ref": "SM" + "2" * 32}
    ) is False
    # and a sibling escalated closure in the window ALSO does not count as an answer to THIS task
    monkeypatch.setattr(
        "orchestrator.db.tenant_connection",
        lambda tid: _FakeConn([
            (_ACK_EN,),
            (to.compose_task_outcome_message("escalated", "a different task", locale="en"),),
        ]),
    )
    assert to._spawning_turn_already_answered(
        uuid4(), {"source_message_ref": "SM" + "3" * 32}
    ) is False


def test_spawning_turn_answered_false_without_anchor() -> None:
    assert to._spawning_turn_already_answered("t", {"source_message_ref": None}) is False
    assert to._spawning_turn_already_answered("t", {}) is False


def test_spawning_turn_answered_read_error_fails_toward_firing(monkeypatch) -> None:
    """Any read error returns False (→ the closure FIRES) — fail toward firing, never silence."""
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("orchestrator.db.tenant_connection", _boom)
    assert to._spawning_turn_already_answered(
        uuid4(), {"source_message_ref": "SM" + "4" * 32}
    ) is False


def test_notify_escalated_suppressed_when_spawning_turn_already_answered(monkeypatch) -> None:
    """(a) DF6 — an escalated closure whose spawning turn was ALREADY answered is SUPPRESSED: no
    send, no ledger row, no alert; the notification flips to 'not_required' (T12's honest terminal)
    so it can never be a contradictory "About your earlier request — I couldn't complete it"
    non-sequitur piled on top of the real answer the owner already got."""
    task = _pending_task("escalated", "connect my WooCommerce store")
    seen = _patch(monkeypatch, task=task)
    monkeypatch.setattr(to, "_spawning_turn_already_answered", lambda tid, t: True)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is False
    assert "body" not in seen               # nothing sent
    assert seen["ledger"] == []
    assert seen["alerts"] == []
    assert seen["flips"] == [("not_required", ("pending",))]


def test_notify_escalated_still_fires_on_genuine_silent_stall(monkeypatch) -> None:
    """(b) DF6 — the no-silence guarantee holds: when the spawning turn was NOT answered (a genuine
    silent stall), the escalated closure STILL fires and flips 'delivered'."""
    task = _pending_task("escalated", "re-engage the lapsed customers")
    seen = _patch(monkeypatch, task=task)  # _patch defaults _spawning_turn_already_answered -> False

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is True
    assert "couldn't complete" in seen["body"].lower()
    assert seen["flips"] == [("delivered", ("pending",))]


def test_notify_escalated_suppress_flip_error_is_fail_soft(monkeypatch) -> None:
    """(c) DF6 — the not_required flip runs after the settle it reports on; a DB error on THAT write
    must be caught, never propagate, and never send."""
    task = _pending_task("escalated", "connect my WooCommerce store")
    seen = _patch(monkeypatch, task=task)
    monkeypatch.setattr(to, "_spawning_turn_already_answered", lambda tid, t: True)

    def _flip_raises(tid, taskid, status, *, expected_from=None):
        raise RuntimeError("db connection reset")

    monkeypatch.setattr("orchestrator.manager.task_store.set_owner_notification_status", _flip_raises)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is False        # suppressed + fail-soft: never raised, never sent
    assert "body" not in seen


# ------------------- R3: gate-blocked interim stall must not count as an answer -------------------
def test_is_substantive_excludes_gate_interim_replacements() -> None:
    """R3 — an emission-gate interim STALL replacement ("still working" generic / not_started
    "haven't started") is NOT a substantive answer; the substantive pending_approval + receivables
    lines, and real prose, ARE. Requires the agent stack for the lazy INTERIM_REPLACEMENT_MARKERS
    import (skipped in the dep-less smoke)."""
    pytest.importorskip("langchain")
    from orchestrator.agent.emission_gate import _REPLACEMENT_COPY

    assert to._is_substantive_owner_reply(_REPLACEMENT_COPY["generic"]["en"]) is False
    assert to._is_substantive_owner_reply(_REPLACEMENT_COPY["generic"]["hi"]) is False
    assert to._is_substantive_owner_reply(_REPLACEMENT_COPY["not_started"]["en"]) is False
    assert to._is_substantive_owner_reply(_REPLACEMENT_COPY["not_started"]["hi"]) is False
    # substantive lines (a real answer to the owner) stay substantive
    assert to._is_substantive_owner_reply(_REPLACEMENT_COPY["pending_approval"]["en"]) is True
    assert to._is_substantive_owner_reply(_REPLACEMENT_COPY["receivables"]["en"]) is True
    assert to._is_substantive_owner_reply(
        "I've drafted a win-back plan for your 8 lapsed customers."
    ) is True


def test_spawning_turn_gate_stall_reply_is_not_an_answer_closure_fires(monkeypatch) -> None:
    """R3 — when the spawning turn was answered ONLY by a gate-blocked interim stall (the brain's
    fabrication the gate swapped for "still working"), the closure MUST still fire (not suppressed):
    a swapped fabrication cannot silence the honest async notice. Real prose still suppresses."""
    pytest.importorskip("langchain")
    from orchestrator.agent.emission_gate import _REPLACEMENT_COPY

    generic = _REPLACEMENT_COPY["generic"]["en"]
    monkeypatch.setattr(
        "orchestrator.db.tenant_connection", lambda tid: _FakeConn([(_ACK_EN,), (generic,)])
    )
    assert to._spawning_turn_already_answered(
        uuid4(), {"source_message_ref": "SM" + "5" * 32}
    ) is False
    # a genuine substantive reply DOES suppress (the owner already got a real answer)
    monkeypatch.setattr(
        "orchestrator.db.tenant_connection",
        lambda tid: _FakeConn([
            (_ACK_EN,),
            ("WooCommerce isn't supported yet — I can connect Shopify instead.",),
        ]),
    )
    assert to._spawning_turn_already_answered(
        uuid4(), {"source_message_ref": "SM" + "6" * 32}
    ) is True


def test_notify_completed_not_suppressed_even_if_spawning_turn_answered(monkeypatch) -> None:
    """DF6 scope fence — suppression is 'escalated'-only. A completed/cancelled closure is normally
    the ONLY substantive reply the owner gets, so it FIRES regardless of the spawning-turn check
    (which is never even consulted for a non-escalated outcome)."""
    task = _pending_task("completed_with_effect", "send the winback campaign")
    seen = _patch(monkeypatch, task=task)
    # even if the read WOULD say "answered", a completed outcome must not be suppressed
    monkeypatch.setattr(to, "_spawning_turn_already_answered", lambda tid, t: True)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is True
    assert "Done" in seen["body"]
    assert seen["flips"] == [("delivered", ("pending",))]


# ---------------------------------------------------------------------------
# VT-683 P2c — window-closed path now QUEUES the composed outcome for in-session delivery
# ---------------------------------------------------------------------------


def test_notify_window_closed_enqueues_for_session_delivery(monkeypatch) -> None:
    """63016 no longer strands the outcome forever: the composed body is queued
    (kind='report', payload carries text + manager_task_id) and the status stays
    'pending' until the drainer delivers it. Dedup-guarded."""
    exc = RuntimeError("window closed")
    exc.code = 63016  # type: ignore[attr-defined]
    task = _pending_task("completed_with_effect")
    _patch(monkeypatch, task=task, send_raises=exc)

    from orchestrator.owner_surface import owner_comms_queue as cq

    queued: list[dict] = []
    monkeypatch.setattr(cq, "has_queued_task_ref", lambda t, ref, **k: False)
    monkeypatch.setattr(
        cq, "enqueue",
        lambda t, *, kind, payload, **k: queued.append({"kind": kind, "payload": payload}) or uuid4(),
    )

    tenant_id, task_id = uuid4(), uuid4()
    sent = to.maybe_notify_owner_of_task_outcome(tenant_id, task_id, recipient_phone="+919811111111")

    assert sent is False
    assert task["owner_notification_status"] == "pending"  # flip happens at DELIVERY, not enqueue
    assert len(queued) == 1
    assert queued[0]["kind"] == "report"
    assert queued[0]["payload"]["manager_task_id"] == str(task_id)
    assert queued[0]["payload"]["text"], "the composed body must ride the queue"


def test_notify_window_closed_dedup_skips_second_enqueue(monkeypatch) -> None:
    """A settle replay with an item already queued must NOT enqueue twice."""
    exc = RuntimeError("window closed")
    exc.code = 63016  # type: ignore[attr-defined]
    task = _pending_task("completed_with_effect")
    _patch(monkeypatch, task=task, send_raises=exc)

    from orchestrator.owner_surface import owner_comms_queue as cq

    monkeypatch.setattr(cq, "has_queued_task_ref", lambda t, ref, **k: True)
    monkeypatch.setattr(
        cq, "enqueue",
        lambda *a, **k: pytest.fail("dedup guard must prevent a second enqueue"),
    )

    assert to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111") is False


def test_mark_deferred_outcome_delivered_flips_and_records(monkeypatch) -> None:
    """The drainer's delivery consequence: 'pending' -> 'delivered' + a VT-524 ledger row.
    Both fail-soft (a bookkeeping error never unwinds the already-happened delivery)."""
    flips: list[tuple] = []
    ledger: list[tuple] = []
    monkeypatch.setattr(
        "orchestrator.manager.task_store.set_owner_notification_status",
        lambda t, taskid, status, expected_from: flips.append((status, expected_from)),
    )
    monkeypatch.setattr(
        "orchestrator.owner_surface.owner_notification.record_owner_notification",
        lambda t, label, sid, run_id=None: ledger.append((label, sid)),
    )
    task_id = uuid4()
    to.mark_deferred_outcome_delivered(uuid4(), task_id, "SM_DRAIN")
    assert flips == [("delivered", ("pending",))]
    assert ledger == [("task_outcome_report", "SM_DRAIN")]


def test_mark_deferred_outcome_delivered_fail_soft(monkeypatch) -> None:
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("orchestrator.manager.task_store.set_owner_notification_status", _boom)
    monkeypatch.setattr("orchestrator.owner_surface.owner_notification.record_owner_notification", _boom)
    to.mark_deferred_outcome_delivered(uuid4(), uuid4(), None)  # must not raise
