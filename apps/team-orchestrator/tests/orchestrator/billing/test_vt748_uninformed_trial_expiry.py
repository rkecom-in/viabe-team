"""VT-748 — a trial may not expire with the owner uninformed AND nobody paged.

THE FAIL-OPEN, and why the B9 lane missed it. That lane was briefed on "the owner notify is a logging
stub", which was fiction — the real seam was already wired. It fixed the footgun it was pointed at
(VT-745: `notify_fn` with no silent default) and stopped. About sixty lines below that fix sat the
live instance of the lane's actual theme:

    if v.decision == "expire":
        _apply_trial_transition(tid, "trial_expired")
        link_params = _compose_trial_subscribe_link(tid)
        if link_params is not None:
            notify(tid, "trial_subscribe_link", language, link_params)
        # ...and if it was None: nothing. No message, no log at warning, no alert.

`_compose_trial_subscribe_link` returns None when `OWNER_JWT_SECRET` is unset or dormant **and on any
exception at all** — a DB hiccup, an import failure. So the tenant moves to 'lapsed', their trial is
over, and they are told nothing while nobody is paged.

The lesson the row asks to record is about briefs, not about trials: **a brief is a hypothesis.** The
named defect was fiction and the theme was real sixty lines away.

These tests are the callee-side pin, in the same spirit as the VT-745 file: no DB, no LLM, no network —
`_scan_active_trials`, `evaluate_trial`, the transition and the alert are all stubbed, so what is under
test is the BRANCHING, which is where the hole was.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.billing import trial_sweep as ts  # noqa: E402

_NOW = datetime(2026, 8, 14, 7, 0, tzinfo=timezone.utc)


def _stub_sweep(monkeypatch, *, decision, transition="applied", link_params=None):
    """Wire one tenant through the sweep with everything external stubbed.

    Returns (tenant_id, notified, alerted) where the two lists record what the sweep did.
    """
    tid = uuid4()
    notified: list[tuple] = []
    alerted: list[dict] = []

    monkeypatch.setattr(ts, "_scan_active_trials", lambda now: [tid])
    monkeypatch.setattr(ts, "_paused", lambda _t: False)
    monkeypatch.setattr(ts, "_preferred_language", lambda _t: "en")
    monkeypatch.setattr(ts, "_resolve_owner_name", lambda _t: "Test Business")
    monkeypatch.setattr(ts, "_apply_trial_transition", lambda _t, _e: transition)
    monkeypatch.setattr(ts, "_compose_trial_subscribe_link", lambda _t: link_params)
    monkeypatch.setattr(
        ts, "_alert_uninformed_trial_expiry",
        lambda t, *, reason, transition: alerted.append(
            {"tenant_id": t, "reason": reason, "transition": transition}
        ),
    )

    verdict = SimpleNamespace(
        decision=decision, trial_end=_NOW + timedelta(days=1), tenant_id=tid
    )
    monkeypatch.setattr(
        "orchestrator.billing.trial_evaluator.evaluate_trial", lambda _t, _n: verdict
    )

    ts.run_trial_evaluation_body(
        now=_NOW, notify_fn=lambda t, name, lang, params: notified.append((t, name, lang, params))
    )
    return tid, notified, alerted


def test_an_expiry_whose_link_cannot_be_composed_pages_someone(monkeypatch):
    """VT-748 exit gate (a) — THE DEFECT. Pre-fix this produced zero notifies and zero alerts: the
    owner lapsed in silence and nothing anywhere recorded it."""
    tid, notified, alerted = _stub_sweep(monkeypatch, decision="expire", link_params=None)

    assert notified == [], "there is no link to send — that part is unchanged"
    assert len(alerted) == 1, (
        "THE FAIL-OPEN: a trial expired, the owner was told nothing, and nobody was paged"
    )
    assert alerted[0]["reason"] == "subscribe_link_compose_failed"
    assert alerted[0]["tenant_id"] == tid


def test_a_normal_expiry_still_just_notifies_and_does_not_page(monkeypatch):
    """The fix must not turn every expiry into an alert. A composed link means the owner WAS told, and
    an operator has nothing to do — paging here would train people to ignore the alert."""
    tid, notified, alerted = _stub_sweep(
        monkeypatch, decision="expire",
        link_params={"owner_name": "Test Business", "subscribe_link": "https://x/y"},
    )

    assert len(notified) == 1 and notified[0][1] == "trial_subscribe_link"
    assert alerted == [], "a successful notify must not also page an operator"


def test_a_failed_phase_transition_does_not_tell_the_owner_their_trial_ended(monkeypatch):
    """VT-748, the second hole in the same branch. `_apply_trial_transition` swallowed its exceptions
    and returned None, so the sweep could not tell "expired" from "tried to expire and it blew up" —
    and sent the subscribe link either way. That link is a statement of fact about the account state;
    sending it after a failed mutation tells the owner their trial ended when it did not."""
    tid, notified, alerted = _stub_sweep(
        monkeypatch, decision="expire", transition="failed",
        link_params={"owner_name": "Test Business", "subscribe_link": "https://x/y"},
    )

    assert notified == [], (
        "a link composed fine, but the phase never moved — sending it would be a false claim about "
        "the owner's account on the money path"
    )
    assert len(alerted) == 1 and alerted[0]["reason"] == "phase_transition_failed"


def test_the_warn_path_is_untouched(monkeypatch):
    """A 'warn' verdict is not an expiry: nothing has lapsed, so there is nothing to fail closed on.
    Pinned so the alerting added here cannot leak into the warn branch."""
    tid, notified, alerted = _stub_sweep(monkeypatch, decision="warn")

    assert len(notified) == 1 and notified[0][1] == "trial_ending"
    assert alerted == []


def test_the_transition_helper_reports_which_of_the_three_things_happened():
    """The branching above is only as good as the signal it reads. `_apply_trial_transition` must
    distinguish applied / noop / failed — a bare bool would collapse "already transitioned" (a normal
    idempotent re-run) into the same bucket as "the mutation raised"."""
    import inspect

    src = inspect.getsource(ts._apply_trial_transition)
    for outcome in ('return "applied"', 'return "noop"', 'return "failed"'):
        assert outcome in src, f"missing {outcome} — the caller cannot branch on what it cannot see"
    assert "-> str" in inspect.getsource(ts._apply_trial_transition).split("\n")[0]


def test_no_substitute_message_is_sent_in_place_of_the_missing_one(monkeypatch):
    """The tempting 'fix' is to fall back to the `trial_ending` template so the owner hears SOMETHING.
    That template says "your trial period ends on {{2}}" — future tense — so sending it to a lapsed
    owner trades silence for a false statement, which is worse. This pins that we did not do it. The
    real closure needs a new Meta-approved link-less template, which is Fazal's call (VT-748 note)."""
    tid, notified, alerted = _stub_sweep(monkeypatch, decision="expire", link_params=None)

    assert all(name != "trial_ending" for _t, name, _l, _p in notified), (
        "never send a WARN template to describe an expiry that already happened"
    )
    assert len(alerted) == 1
