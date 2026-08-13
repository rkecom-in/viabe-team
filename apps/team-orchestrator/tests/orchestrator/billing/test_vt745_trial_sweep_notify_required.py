"""VT-745 — the trial sweep must never run without a live owner-notify seam.

Background (the blocker text for this was WRONG and this file records why). The claim
was "the owner notify is a logging stub, so trials expire silently." It is not:
``scheduled_triggers.trial_evaluation_scheduled`` wires the REAL ``_owner_notify``
(scheduled_triggers.py:276), a caller-side test guards that wiring
(test_scheduled_triggers.py::test_trial_evaluation_scheduled_wires_real_owner_notify),
and BOTH registry templates (``trial_ending``, ``trial_subscribe_link``) are
audience=owner / approved_for_live=true with real en/hi SIDs. The production path is fine.

What WAS real is the footgun behind it: ``run_trial_evaluation_body`` used to fall back to
a ``_default_notify`` logging no-op whenever ``notify_fn`` was omitted. That put "expire
the trial and tell the owner nothing" one missing kwarg away from the money path, defended
only by a test on the CALLER — a second caller, or a refactor that dropped the kwarg,
would have silently expired trials in production and no test in this file's neighbourhood
would have failed.

These tests pin the fix at the CALLEE, where it cannot be routed around:
  1. omitting ``notify_fn`` raises, and the sweep does not scan a single tenant;
  2. an explicit ``notify_fn=None`` raises BEFORE any tenant is transitioned (a mid-loop
     failure would strand the first tenant already moved to `lapsed`, un-notified);
  3. no silent-default notify may be reintroduced into the module.

No DB and no LLM: every case here raises before the sweep reaches its first query.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from orchestrator.billing import trial_sweep as ts  # noqa: E402


def test_missing_notify_fn_raises_and_scans_nothing(monkeypatch):
    """Omitting notify_fn is a call-site error, not a silent no-op sweep.

    Also asserts the sweep never reached ``_scan_active_trials`` — a raise that happened
    only once the loop hit its first notify would still have expired tenants first.
    """
    scanned: list[object] = []
    monkeypatch.setattr(
        ts, "_scan_active_trials", lambda now: scanned.append(now) or []
    )

    with pytest.raises(TypeError):
        ts.run_trial_evaluation_body()  # type: ignore[call-arg]

    assert scanned == [], "sweep must not scan tenants when the notify seam is missing"


def test_explicit_none_notify_fn_raises_before_any_transition(monkeypatch):
    """notify_fn=None must fail closed up-front, not TypeError mid-loop.

    The pre-fix hazard this pins: a None notify would have surfaced only at the first
    ``notify(...)`` call — i.e. AFTER ``_apply_trial_transition`` had already moved that
    tenant to `lapsed` — leaving a trial expired, its owner never told, and the remaining
    tenants unswept.
    """
    transitions: list[tuple] = []
    monkeypatch.setattr(
        ts, "_apply_trial_transition", lambda tid, ev: transitions.append((tid, ev))
    )
    monkeypatch.setattr(
        ts,
        "_scan_active_trials",
        lambda now: pytest.fail("sweep scanned tenants despite a None notify seam"),
    )

    with pytest.raises(TypeError) as exc:
        ts.run_trial_evaluation_body(notify_fn=None)  # type: ignore[arg-type]

    assert "notify_fn" in str(exc.value)
    assert transitions == [], "no tenant may be transitioned before the seam is validated"


def test_no_silent_default_notify_is_reintroduced():
    """Regression guard: the module must expose no zero-arg-callable notify default.

    ``_default_notify`` is deleted on purpose. If a future change re-adds any such default
    (under any name) and re-wires ``notify_fn`` to fall back to it, the two tests above
    would keep passing while production silently regressed — this one fails instead.
    """
    import inspect

    assert not hasattr(ts, "_default_notify"), (
        "_default_notify was deliberately deleted (VT-745) — a logging no-op default on "
        "the trial sweep's notify seam is the silent-expiry footgun itself"
    )

    sig = inspect.signature(ts.run_trial_evaluation_body)
    notify_param = sig.parameters["notify_fn"]
    assert notify_param.default is inspect.Parameter.empty, (
        "notify_fn must stay REQUIRED — any default value re-opens the silent-expiry path"
    )
    assert notify_param.kind is inspect.Parameter.KEYWORD_ONLY
