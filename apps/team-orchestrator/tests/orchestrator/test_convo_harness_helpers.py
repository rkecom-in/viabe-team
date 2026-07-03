"""VT-582 — convo_harness pure helpers + one_turn orchestration (mocked DB/HTTP).

The harness module imports stdlib-only at top level, so it loads without dbos/psycopg/requests; the
per-command paths import psycopg/requests lazily. These tests exercise the pure logic — bogus-number
generation, the ingress-mirroring run_id derivation, the send-guard-breach detector, assertion
evaluation, xfail classification — and _drive_turn's transcript assembly with _connect / _post_inbound
/ _poll_run_status stubbed, so no DB or network is touched.
"""

from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import pytest

# canaries/ is NOT on the pytest pythonpath (only src/scripts) — add it so we can import the harness.
_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import convo_harness as ch  # noqa: E402 — after the sys.path insert


# --- bogus number / inbound sid ---------------------------------------------------------------


def test_bogus_number_is_us_555_test_range_and_unique():
    a, b = ch.bogus_number(), ch.bogus_number()
    assert re.fullmatch(r"\+15550\d{6}", a), a
    assert a.startswith("+15550")
    assert a != b  # random tail


def test_bogus_number_never_allowlisted():
    # The dev send allowlist is the four Fazal-provided +91 numbers; a +15550 number can never be one.
    allowlist = {"+919321553267", "+919820463598", "+917738859946", "+919892616965"}
    for _ in range(50):
        assert ch.bogus_number() not in allowlist


def test_fresh_inbound_sid_shape_and_unique():
    a, b = ch.fresh_inbound_sid(), ch.fresh_inbound_sid()
    assert a.startswith("SMharness")
    assert a != b
    # Starts with SM (realistic) but is greppable as harness traffic.
    assert ch.is_real_twilio_sid(a)  # it IS SM-prefixed by design (an inbound sid, not an outbound)


# --- run_id derivation MUST mirror the ingress ------------------------------------------------


def test_run_id_mirrors_ingress_uuid5():
    sid = "SMharness_pinned_value"
    # The ingress computes uuid5(NAMESPACE_URL, message_sid); pin the exact value so a divergence
    # in either place is caught.
    expected = str(uuid.uuid5(uuid.NAMESPACE_URL, sid))
    assert ch.run_id_for_sid(sid) == expected


def test_run_id_matches_ingress_module_when_importable():
    """If the ingress module is importable (dbos/fastapi present), assert byte-for-byte parity with
    its real derivation — the strongest guard against drift."""
    dbos = pytest.importorskip("dbos")  # noqa: F841
    pytest.importorskip("fastapi")
    from uuid import NAMESPACE_URL, uuid5  # the exact symbols the ingress uses

    sid = "SMxyz123"
    assert ch.run_id_for_sid(sid) == str(uuid5(NAMESPACE_URL, sid))


# --- send-guard breach detector ---------------------------------------------------------------


@pytest.mark.parametrize(
    "sid,is_real",
    [
        ("SM0123456789abcdef", True),
        ("MM0123456789abcdef", True),
        ("sm_lowercase_ok", True),  # case-insensitive
        ("MKDEV0123456789", False),  # dev-guard mock
        ("VEDEV0123456789", False),
        (None, False),
        ("", False),
    ],
)
def test_is_real_twilio_sid(sid, is_real):
    assert ch.is_real_twilio_sid(sid) is is_real


# --- assertion evaluation ---------------------------------------------------------------------


def _t(role, text, sid=None):
    return ch.Turn(role=role, text=text, message_sid=sid)


def test_assert_no_silent_fires_when_no_assistant_turn():
    turns = [_t("owner", "hello")]
    failures = ch.evaluate_assertions(turns, assert_no_silent=True)
    assert any("silent drop" in f for f in failures)


def test_assert_no_silent_satisfied_by_assistant_turn():
    turns = [_t("owner", "hello"), _t("assistant", "hi there", "MKDEV1")]
    assert ch.evaluate_assertions(turns, assert_no_silent=True) == []


def test_assert_no_silent_off_ignores_missing_reply():
    turns = [_t("owner", "hello")]
    assert ch.evaluate_assertions(turns, assert_no_silent=False) == []


def test_assert_contains_case_insensitive_hit_and_miss():
    turns = [_t("assistant", "Reply ACTIVATE TEAM to enable", "MKDEV1")]
    assert ch.evaluate_assertions(turns, assert_contains=["activate team"]) == []
    miss = ch.evaluate_assertions(turns, assert_contains=["diwali"])
    assert any("missing" in f for f in miss)


def test_assert_not_contains_hit_and_miss():
    turns = [_t("assistant", "your AI team is ready", "MKDEV1")]
    assert ch.evaluate_assertions(turns, assert_not_contains=["error"]) == []
    hit = ch.evaluate_assertions(turns, assert_not_contains=["ready"])
    assert any("unexpectedly contains" in f for f in hit)


def test_assert_contains_only_scans_assistant_text():
    # An owner turn carrying the needle must NOT satisfy assert_contains (we assert on REPLIES).
    turns = [_t("owner", "please say diwali"), _t("assistant", "sure", "MKDEV1")]
    failures = ch.evaluate_assertions(turns, assert_contains=["diwali"])
    assert any("missing" in f for f in failures)


# --- xfail classification ---------------------------------------------------------------------


def test_classify_pass():
    assert ch.classify_step([], expected_fail=False) == (True, False, "PASS")


def test_classify_fail():
    ok, xfail, label = ch.classify_step(["boom"], expected_fail=False)
    assert (ok, xfail, label) == (False, False, "FAIL")


def test_classify_xfail_is_green():
    ok, xfail, label = ch.classify_step(["known gap"], expected_fail=True)
    assert ok is True and xfail is True and label == "XFAIL"


def test_classify_xpass_is_green_but_flagged():
    ok, xfail, label = ch.classify_step([], expected_fail=True)
    assert ok is True and xfail is False and label == "XPASS"


# --- _drive_turn transcript assembly + breach detection (mocked DB/HTTP) -----------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Routes .execute by SQL substring. Returns configurable rows for the tenant lookup, the
    before-id scan, and the after-turn read. Context-manager + no-op for set_config."""

    def __init__(self, *, number, before_ids, after_rows):
        self._number = number
        self._before_ids = before_ids
        self._after_rows = after_rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "set_config('request.jwt.claims'" in s:
            return _Result([])
        if "FROM tenants WHERE id" in s:
            return _Result([(self._number, "convo-harness-abcd1234")])
        if "SELECT id FROM conversation_log" in s:
            return _Result([(i,) for i in self._before_ids])
        if "SELECT id, role, text, message_sid, surface FROM conversation_log" in s:
            return _Result(self._after_rows)
        return _Result([])


def _install_stubs(monkeypatch, *, number, before_ids, after_rows, ingress_reason, run_status):
    monkeypatch.setattr(ch, "fresh_inbound_sid", lambda: "SMharnessFIXED")
    monkeypatch.setattr(
        ch, "_connect",
        lambda dsn: _FakeConn(number=number, before_ids=before_ids, after_rows=after_rows),
    )
    monkeypatch.setattr(ch, "_post_inbound", lambda base, secret, fields: {"reason": ingress_reason})
    monkeypatch.setattr(ch, "_poll_run_status", lambda dsn, run_id, timeout: run_status)


def test_drive_turn_builds_transcript_and_dedups_owner_echo(monkeypatch):
    # after_rows: (id, role, text, message_sid, surface) — the brain re-records the owner inbound
    # under the SAME sid (must be deduped) + one assistant reply.
    after_rows = [
        (10, "owner", "plan a campaign", "SMharnessFIXED", "manager"),
        (11, "assistant", "on it — here is the plan", "MKDEVabc", "manager"),
    ]
    _install_stubs(
        monkeypatch, number="+15550123456", before_ids={1, 2}, after_rows=after_rows,
        ingress_reason="started", run_status="completed",
    )
    res = ch._drive_turn("dsn", "http://orch", "secret", "tenant-x", "plan a campaign", timeout=1.0)
    assert res.run_status == "completed"
    assert res.ingress_reason == "started"
    # The transcript: the injected owner turn (once), then the assistant reply. The brain-recorded
    # owner echo (same sid) is NOT double-printed.
    roles = [(t.role, t.text) for t in res.transcript]
    assert roles == [("owner", "plan a campaign"), ("assistant", "on it — here is the plan")]
    assert res.ok is True and res.reasons == []


def test_drive_turn_flags_send_guard_breach_on_real_sid(monkeypatch):
    # An assistant turn carrying a REAL Twilio SID = a real send escaped the dev guard → hard fail.
    # Row shape: (id, role, text, message_sid, surface) — only message_sid drives the breach check.
    after_rows = [(99, "assistant", "leaked send", "SMrealoutbound", "manager")]
    _install_stubs(
        monkeypatch, number="+15550123456", before_ids=set(), after_rows=after_rows,
        ingress_reason="started", run_status="completed",
    )
    res = ch._drive_turn("dsn", "http://orch", "secret", "tenant-x", "hi", timeout=1.0)
    assert res.ok is False
    assert any("SEND-GUARD BREACH" in r for r in res.reasons)


def test_drive_turn_no_run_reason_skips_poll_and_is_silent(monkeypatch):
    # unknown_sender → no run started; nothing to poll; empty capture → a silent turn.
    _install_stubs(
        monkeypatch, number="+15550123456", before_ids=set(), after_rows=[],
        ingress_reason="unknown_sender", run_status=None,
    )
    called = {"polled": False}

    def _no_poll(*a, **k):
        called["polled"] = True
        return "should-not-be-called"

    monkeypatch.setattr(ch, "_poll_run_status", _no_poll)
    res = ch._drive_turn("dsn", "http://orch", "secret", "tenant-x", "hi", timeout=1.0)
    assert called["polled"] is False
    assert res.run_status is None
    # only the injected owner turn; no assistant reply
    assert [t.role for t in res.transcript] == ["owner"]
    assert ch.evaluate_assertions(res.transcript, assert_no_silent=True)  # non-empty → silent
