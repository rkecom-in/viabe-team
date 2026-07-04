"""VT-582 — convo_harness pure helpers + one_turn orchestration (mocked DB/HTTP).

The harness module imports stdlib-only at top level, so it loads without dbos/psycopg/requests; the
per-command paths import psycopg/requests lazily. These tests exercise the pure logic — bogus-number
generation, the ingress-mirroring run_id derivation, the send-guard-breach detector, the three-way
reply-verdict (ok/timeout/silent) + assertion evaluation, xfail classification — and _drive_turn's
transcript assembly with _connect / _post_inbound / _poll_run_status stubbed, so no DB or network is
touched.

VT-598 additions (below the VT-582 tests): assert_not_d1 (pass/fail/hi-variant), the json-report
bundle builder's shape + round-trip (append-safe accumulation), the assert_run_reason(_not)
not-supported wiring, a scenario-file validation sweep over every canaries/scenarios/*.json, and
(VT-598 addendum) the dev-test consent-seed preference wiring (mocked HTTP, no DB).
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

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


# --- three-way reply verdict (ok / timeout / silent) ------------------------------------------
#
# The run-23 calibration gap: a step was flagged "NO assistant reply (silent drop)" while
# run_status='running' — the poll returned before the deployed LLM turn finished (10-40s is normal).
# reply_verdict must distinguish TIMEOUT (still running) from a TRUE SILENT (terminal, zero replies).


def _t(role, text, sid=None):
    return ch.Turn(role=role, text=text, message_sid=sid)


def test_reply_verdict_ok_when_assistant_turn_present_regardless_of_status():
    turns = [_t("owner", "hi"), _t("assistant", "hello", "MKDEV1")]
    assert ch.reply_verdict(turns, "running") == "ok"
    assert ch.reply_verdict(turns, "completed") == "ok"
    assert ch.reply_verdict(turns, None) == "ok"


def test_reply_verdict_timeout_when_still_running_with_no_reply():
    turns = [_t("owner", "hi")]
    assert ch.reply_verdict(turns, "running") == "timeout"


def test_reply_verdict_silent_when_terminal_status_with_no_reply():
    turns = [_t("owner", "hi")]
    assert ch.reply_verdict(turns, "completed") == "silent"


def test_reply_verdict_silent_when_no_run_status_at_all():
    # _drive_turn's no-run-reason branch (ingress rejected — nothing to poll): run_status=None.
    turns = [_t("owner", "hi")]
    assert ch.reply_verdict(turns, None) == "silent"


# --- assertion evaluation ---------------------------------------------------------------------


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


def test_assert_no_silent_does_not_fire_on_timeout():
    # The calibration fix: run_status='running' + zero replies is a TIMEOUT, not a silent drop —
    # evaluate_assertions must NOT raise assert_no_silent for it (cmd_script buckets TIMEOUT
    # separately, checking reply_verdict itself BEFORE calling evaluate_assertions).
    turns = [_t("owner", "hello")]
    assert ch.evaluate_assertions(turns, run_status="running", assert_no_silent=True) == []


def test_assert_no_silent_fires_on_terminal_status_with_no_reply():
    turns = [_t("owner", "hello")]
    failures = ch.evaluate_assertions(turns, run_status="completed", assert_no_silent=True)
    assert any("silent drop" in f for f in failures)


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
        if "SELECT id, role, text, message_sid, surface, created_at FROM conversation_log" in s:
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
    assert ch.reply_verdict(res.transcript, res.run_status) == "silent"
    assert ch.evaluate_assertions(res.transcript, run_status=res.run_status, assert_no_silent=True)  # non-empty → silent


# --- VT-598: assert_not_d1 (pass / fail / hi-variant) ------------------------------------------


def test_is_d1_fallback_only_true_for_bare_en_line():
    assert ch.is_d1_fallback_only(ch._D1_FALLBACK_EN) is True


def test_is_d1_fallback_only_true_for_bare_hi_line():
    assert ch.is_d1_fallback_only(ch._D1_FALLBACK_HI) is True


def test_is_d1_fallback_only_true_with_trivial_padding():
    # A few stray characters around the D1 line still count as "just D1" (under the substantive floor).
    text = f"  {ch._D1_FALLBACK_EN}  ok"
    assert ch.is_d1_fallback_only(text) is True


def test_is_d1_fallback_only_false_with_real_substance():
    text = (
        f"{ch._D1_FALLBACK_EN} In the meantime here's what I found: your top 3 lapsed customers "
        "are Priya, Rahul and Anita — want me to draft a message to them?"
    )
    assert ch.is_d1_fallback_only(text) is False


def test_is_d1_fallback_only_false_when_d1_absent():
    assert ch.is_d1_fallback_only("Sure — connect your Shopify store at yourstore.myshopify.com") is False


def test_assert_not_d1_fires_when_reply_is_bare_d1():
    turns = [_t("owner", "make me a plan"), _t("assistant", ch._D1_FALLBACK_EN, "MKDEV1")]
    failures = ch.evaluate_assertions(turns, assert_not_d1=True)
    assert any("assert_not_d1" in f for f in failures)


def test_assert_not_d1_fires_on_hi_variant():
    turns = [_t("owner", "plan banao"), _t("assistant", ch._D1_FALLBACK_HI, "MKDEV1")]
    failures = ch.evaluate_assertions(turns, assert_not_d1=True)
    assert any("assert_not_d1" in f for f in failures)


def test_assert_not_d1_satisfied_by_a_real_answer():
    turns = [_t("owner", "how does this work"), _t("assistant", "Here's exactly how it works: ...", "MKDEV1")]
    assert ch.evaluate_assertions(turns, assert_not_d1=True) == []


def test_assert_not_d1_off_ignores_bare_d1():
    turns = [_t("owner", "make me a plan"), _t("assistant", ch._D1_FALLBACK_EN, "MKDEV1")]
    assert ch.evaluate_assertions(turns, assert_not_d1=False) == []


def test_assert_not_d1_does_not_fire_when_d1_mentioned_alongside_real_content():
    # The D1 phrase appearing IN PASSING (with plenty of other substance) is not the failure mode.
    turns = [_t("assistant", (
        f"{ch._D1_FALLBACK_EN} Also, your Shopify store probe-store-a.myshopify.com is now connected "
        "and syncing orders every 15 minutes."
    ), "MKDEV1")]
    assert ch.evaluate_assertions(turns, assert_not_d1=True) == []


# --- VT-598: assert_run_reason / assert_run_reason_not — NOT SUPPORTED, wired as an explicit FAIL


def test_assert_run_reason_is_not_supported_and_fails():
    turns = [_t("owner", "hi"), _t("assistant", "a real answer", "MKDEV1")]
    failures = ch.evaluate_assertions(turns, assert_run_reason="edge_case:status_query")
    assert any("NOT SUPPORTED" in f and "assert_run_reason:" in f for f in failures)


def test_assert_run_reason_not_is_not_supported_and_fails():
    turns = [_t("owner", "hi"), _t("assistant", "a real answer", "MKDEV1")]
    failures = ch.evaluate_assertions(turns, assert_run_reason_not="edge_case:status_query")
    assert any("NOT SUPPORTED" in f and "assert_run_reason_not:" in f for f in failures)


def test_assert_run_reason_unset_is_a_pure_no_op():
    turns = [_t("owner", "hi"), _t("assistant", "a real answer", "MKDEV1")]
    assert ch.evaluate_assertions(turns) == []


# --- VT-598: _new_conversation_turns carries created_at (json-report needs it) ------------------


def test_new_conversation_turns_reads_created_at_from_6_tuple(monkeypatch):
    # (id, role, text, message_sid, surface, created_at) — the post-VT-598 SELECT shape.
    rows = [(1, "assistant", "hi there", "MKDEV1", "manager", "2026-07-04T00:00:00+00:00")]
    conn = _FakeConn(number="+15550123456", before_ids=set(), after_rows=rows)
    turns = ch._new_conversation_turns(conn, "tenant-x", set())
    assert turns[0].created_at == "2026-07-04T00:00:00+00:00"


def test_new_conversation_turns_tolerates_missing_created_at_column():
    # A 5-tuple (pre-VT-598 shape) must not raise — created_at falls back to None.
    rows = [(1, "assistant", "hi there", "MKDEV1", "manager")]
    conn = _FakeConn(number="+15550123456", before_ids=set(), after_rows=rows)
    turns = ch._new_conversation_turns(conn, "tenant-x", set())
    assert turns[0].created_at is None


# --- VT-598: json-report bundle — shape + round-trip (append-safe) ------------------------------


def _step_result(label, transcript):
    return ch.StepResult(
        ok=(label == "PASS"), xfail=False, label=label, reasons=[],
        transcript=transcript, run_status="completed", ingress_reason="started",
    )


def test_turn_to_dict_shape():
    turn = ch.Turn(role="assistant", text="hello", message_sid="MKDEV1", surface="manager",
                    created_at="2026-07-04T00:00:00+00:00")
    d = ch._turn_to_dict(turn)
    assert d["role"] == "assistant"
    assert d["text"] == "hello"
    assert d["surface"] == "manager"
    assert d["created_at"] == "2026-07-04T00:00:00+00:00"


def test_build_json_report_shape():
    scenario = {"name": "probe_scenario", "steps": [{"message": "hi"}]}
    results = [_step_result("PASS", [ch.Turn(role="assistant", text="hello", message_sid="MKDEV1")])]
    summary = {"passed": 1, "xfailed": 0, "xpassed": 0, "failed": 0, "timed_out": 0}
    entry = ch._build_json_report(scenario, "canaries/scenarios/probe_scenario.json", "tenant-x",
                                   scenario["steps"], results, summary)
    assert entry["scenario"] == "canaries/scenarios/probe_scenario.json"
    assert entry["name"] == "probe_scenario"
    assert entry["tenant_id"] == "tenant-x"
    assert "harness_sha" in entry  # may be None outside a git checkout — key must be present
    assert entry["summary"] == summary
    assert len(entry["steps"]) == 1
    step = entry["steps"][0]
    assert step["message"] == "hi"
    assert step["label"] == "PASS"
    assert step["transcript"][0]["text"] == "hello"
    # round-trips through json.dumps/loads cleanly (no raw datetime/dataclass objects left)
    json.loads(json.dumps(entry))


def test_append_json_report_creates_and_accumulates(tmp_path):
    path = str(tmp_path / "bundle.json")
    scenario_a = {"name": "scenario_a", "steps": [{"message": "hi"}]}
    scenario_b = {"name": "scenario_b", "steps": [{"message": "yo"}]}
    results = [_step_result("PASS", [ch.Turn(role="assistant", text="ok", message_sid="MKDEV1")])]
    summary = {"passed": 1, "xfailed": 0, "xpassed": 0, "failed": 0, "timed_out": 0}

    entry_a = ch._build_json_report(scenario_a, "a.json", "tenant-a", scenario_a["steps"], results, summary)
    ch._append_json_report(path, entry_a)

    with open(path, encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert isinstance(loaded, list)
    assert len(loaded) == 1
    assert loaded[0]["name"] == "scenario_a"

    entry_b = ch._build_json_report(scenario_b, "b.json", "tenant-b", scenario_b["steps"], results, summary)
    ch._append_json_report(path, entry_b)

    with open(path, encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert len(loaded) == 2  # accumulated, not clobbered
    assert [e["name"] for e in loaded] == ["scenario_a", "scenario_b"]


def test_append_json_report_starts_fresh_on_corrupt_file(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text("not valid json {{{", encoding="utf-8")
    scenario = {"name": "scenario_a", "steps": [{"message": "hi"}]}
    results = [_step_result("PASS", [ch.Turn(role="assistant", text="ok", message_sid="MKDEV1")])]
    summary = {"passed": 1, "xfailed": 0, "xpassed": 0, "failed": 0, "timed_out": 0}
    entry = ch._build_json_report(scenario, "a.json", "tenant-a", scenario["steps"], results, summary)
    ch._append_json_report(str(path), entry)  # must not raise on the corrupt pre-existing file
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert len(loaded) == 1


# --- VT-598: every new scenario JSON parses + has the required schema keys ---------------------


_SCENARIOS_DIR = _CANARIES / "scenarios"


@pytest.mark.parametrize("scenario_path", sorted(_SCENARIOS_DIR.glob("*.json")), ids=lambda p: p.name)
def test_every_scenario_json_loads_and_has_required_keys(scenario_path):
    scenario = ch._load_scenario(str(scenario_path))
    assert isinstance(scenario, dict)
    assert "name" in scenario, scenario_path
    assert "steps" in scenario and isinstance(scenario["steps"], list) and scenario["steps"], scenario_path
    for step in scenario["steps"]:
        assert "message" in step and isinstance(step["message"], str) and step["message"], (
            scenario_path, step,
        )


# --- VT-598 addendum: dev-test consent-seed wiring (salt-mismatch fix) --------------------------
#
# LIVE FINDING: --seed-lapsed-customers previously called record_consent() directly in the
# harness's own process, tokenising with a throwaway salt that never matches the deployed
# service's (sealed) TEAM_PHONE_HASH_SALT — a seeded cohort always read as empty on deployed dev.
# The fix: prefer POSTing to the new dev-test consent-seed endpoint (server-side salt) whenever an
# ingress base + secret are BOTH available.


def test_optional_ingress_base_from_arg():
    assert ch._optional_ingress_base("https://example.test/") == "https://example.test"


def test_optional_ingress_base_from_env(monkeypatch):
    monkeypatch.setenv("TEAM_ORCHESTRATOR_URL", "https://env.example.test/")
    assert ch._optional_ingress_base(None) == "https://env.example.test"


def test_optional_ingress_base_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("TEAM_ORCHESTRATOR_URL", raising=False)
    assert ch._optional_ingress_base(None) is None


def test_consent_seed_uses_endpoint_requires_both_base_and_secret():
    assert ch._consent_seed_uses_endpoint("https://x", "secret") is True
    assert ch._consent_seed_uses_endpoint("https://x", None) is False
    assert ch._consent_seed_uses_endpoint(None, "secret") is False
    assert ch._consent_seed_uses_endpoint(None, None) is False


def test_record_seed_consent_prefers_endpoint_when_url_given(monkeypatch):
    """The core VT-598 addendum assertion: given an ingress base + secret, the seeding path calls
    the endpoint (mocked HTTP — no real network, no DB), NEVER the local record_consent."""
    post_stub = MagicMock()
    monkeypatch.setattr(ch, "_post_consent_seed", post_stub)
    record_consent_stub = MagicMock()
    monkeypatch.setattr("orchestrator.privacy.consent.record_consent", record_consent_stub)

    ch._record_seed_consent(
        "tenant-x", "+15550123456", "dev-test-v0",
        ingress_base="https://dev.example.test", ingress_secret="the-secret",
    )

    post_stub.assert_called_once_with(
        "https://dev.example.test", "the-secret", "tenant-x", "+15550123456", "dev-test-v0",
    )
    record_consent_stub.assert_not_called()


def test_record_seed_consent_falls_back_to_local_when_no_url(monkeypatch):
    post_stub = MagicMock()
    monkeypatch.setattr(ch, "_post_consent_seed", post_stub)
    record_consent_stub = MagicMock()
    monkeypatch.setattr("orchestrator.privacy.consent.record_consent", record_consent_stub)

    ch._record_seed_consent(
        "tenant-x", "+15550123456", "dev-test-v0", ingress_base=None, ingress_secret=None,
    )

    post_stub.assert_not_called()
    record_consent_stub.assert_called_once_with(
        "tenant-x", "+15550123456", consent_text_version="dev-test-v0",
    )


def test_record_seed_consent_falls_back_when_only_secret_given(monkeypatch):
    # Both must be present — a secret with no base URL is not enough to use the endpoint.
    post_stub = MagicMock()
    monkeypatch.setattr(ch, "_post_consent_seed", post_stub)
    record_consent_stub = MagicMock()
    monkeypatch.setattr("orchestrator.privacy.consent.record_consent", record_consent_stub)

    ch._record_seed_consent(
        "tenant-x", "+15550123456", "dev-test-v0", ingress_base=None, ingress_secret="the-secret",
    )

    post_stub.assert_not_called()
    record_consent_stub.assert_called_once()


# --- _post_consent_seed: the actual HTTP call (mocked requests.post) ----------------------------


class _FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.text = text

    def json(self):
        return self._json_body


def test_post_consent_seed_posts_expected_url_headers_and_body(monkeypatch):
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002 - mirrors requests' kwarg name
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse(200, {"recorded": True, "active": True, "phone_token_prefix": "abc123456789"})

    monkeypatch.setattr("requests.post", _fake_post)

    result = ch._post_consent_seed(
        "https://dev.example.test", "the-secret", "tenant-x", "+15550123456", "dev-test-v0",
    )

    assert captured["url"] == "https://dev.example.test/api/orchestrator/dev-test/consent-seed"
    assert captured["json"] == {
        "tenant_id": "tenant-x", "phone_e164": "+15550123456", "consent_text_version": "dev-test-v0",
    }
    assert captured["headers"]["X-Internal-Secret"] == "the-secret"
    assert result["recorded"] is True


def test_post_consent_seed_dies_on_non_200(monkeypatch):
    def _fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        return _FakeResponse(403, text="invalid internal secret")

    monkeypatch.setattr("requests.post", _fake_post)

    with pytest.raises(SystemExit) as exc_info:
        ch._post_consent_seed("https://dev.example.test", "wrong-secret", "tenant-x", "+15550123456", "v0")
    assert exc_info.value.code == 2
