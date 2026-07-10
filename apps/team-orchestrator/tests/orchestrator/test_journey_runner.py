"""Phase-1.1 journey runner (canaries/journey_runner.py) — .viabe/journey-sim-spec.md.

Pure-logic tests (journey/phase parsing + sequencing, persona system-prompt/message building,
persona-fallback resolution, phase-level DB-assert dispatch — no DB/network) and mocked
orchestration tests for run_journey/main (ch.cmd_setup/ch.run_scenario_steps/ch.cmd_teardown/
ch._connect stubbed, ``anthropic`` never imported) — mirrors test_run_full_pack.py's structure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import convo_harness as ch  # noqa: E402
import journey_runner as jr  # noqa: E402


def _sr(label: str, run_id: str | None = "run-1", transcript: list | None = None) -> ch.StepResult:
    return ch.StepResult(
        ok=(label in ("PASS", "XFAIL")), xfail=(label == "XFAIL"), label=label, reasons=[],
        transcript=transcript if transcript is not None else [
            ch.Turn(role="owner", text="hi"), ch.Turn(role="assistant", text="hello"),
        ],
        run_status="completed", ingress_reason="started", run_id=run_id,
    )


# --- validate_journey / _validate_phase_shape --------------------------------------------------


def test_validate_journey_raises_on_no_phases():
    with pytest.raises(ValueError, match="no 'phases'"):
        jr.validate_journey({"name": "x"})


def test_validate_journey_raises_on_empty_phases():
    with pytest.raises(ValueError, match="no 'phases'"):
        jr.validate_journey({"name": "x", "phases": []})


def test_validate_journey_passes_on_well_formed_phases():
    jr.validate_journey({"phases": [{"steps": [{"message": "hi"}]}]})  # must not raise


def test_validate_journey_raises_when_a_phase_has_both_steps_and_scenario_ref():
    journey = {"phases": [{"steps": [{"message": "hi"}], "scenario_ref": "x.json"}]}
    with pytest.raises(ValueError, match="EXACTLY ONE"):
        jr.validate_journey(journey)


def test_validate_journey_raises_when_a_phase_has_neither():
    journey = {"phases": [{"name": "empty phase"}]}
    with pytest.raises(ValueError, match="EXACTLY ONE"):
        jr.validate_journey(journey)


# --- resolve_phase_source --------------------------------------------------------------------


def test_resolve_phase_source_inline_steps(tmp_path):
    phase = {"name": "p1", "steps": [{"message": "hi"}, {"message": "bye"}]}
    label, steps, default_xfail = jr.resolve_phase_source(phase, tmp_path)
    assert label == "p1"
    assert steps == [{"message": "hi"}, {"message": "bye"}]
    assert default_xfail is False


def test_resolve_phase_source_inline_expected_fail_default():
    phase = {"steps": [{"message": "hi"}], "expected_fail": True}
    _label, _steps, default_xfail = jr.resolve_phase_source(phase, Path("."))
    assert default_xfail is True


def test_resolve_phase_source_scenario_ref_loads_referenced_steps(tmp_path):
    (tmp_path / "ref.json").write_text(json.dumps({
        "name": "ref", "expected_fail": True, "steps": [{"message": "from ref"}],
        "setup_args": ["--onboarded", "--seed-lapsed-customers", "99"],
    }))
    label, steps, default_xfail = jr.resolve_phase_source({"scenario_ref": "ref.json"}, tmp_path)
    assert label == "scenario_ref:ref.json"
    assert steps == [{"message": "from ref"}]
    assert default_xfail is True  # inherited from the REFERENCED file's own expected_fail


def test_resolve_phase_source_scenario_ref_never_reads_setup_args(tmp_path):
    """The referenced scenario's setup_args must never surface anywhere in the resolved output —
    the journey's tenant is already provisioned; a phase must not imply re-seeding."""
    (tmp_path / "ref.json").write_text(json.dumps({
        "name": "ref", "steps": [{"message": "hi"}], "setup_args": ["--onboarded"],
    }))
    label, steps, _xfail = jr.resolve_phase_source({"scenario_ref": "ref.json"}, tmp_path)
    assert "setup_args" not in (label, steps)
    assert all("setup_args" not in s for s in steps)


def test_resolve_journeys_dir_files_parse_and_resolve():
    """Both shipped journey files under canaries/journeys/ must validate + every phase resolve
    against the real canaries/scenarios/ dir — a live regression guard, not just a schema test."""
    journeys_dir = _CANARIES / "journeys"
    scenarios_dir = _CANARIES / "scenarios"
    paths = list(journeys_dir.glob("*.json"))
    assert len(paths) >= 2
    for path in paths:
        journey = jr.load_journey(str(path))
        jr.validate_journey(journey)
        for phase in journey["phases"]:
            _label, steps, _xfail = jr.resolve_phase_source(phase, scenarios_dir)
            assert steps  # every phase resolves to at least one step


# --- style_instruction / build_persona_system_prompt / render_conversation_for_persona --------


def test_style_instruction_known_styles_differ():
    en = jr.style_instruction("en")
    hinglish = jr.style_instruction("hinglish")
    devanagari = jr.style_instruction("devanagari")
    assert len({en, hinglish, devanagari}) == 3


def test_style_instruction_unknown_style_falls_back_to_default():
    assert jr.style_instruction("klingon") == jr.style_instruction("en")


def test_build_persona_system_prompt_includes_business_temperament_backstory():
    persona = {"business": "a bakery", "temperament": "anxious", "backstory": "Sales just dropped."}
    prompt = jr.build_persona_system_prompt(persona, "en")
    assert "a bakery" in prompt
    assert "anxious" in prompt
    assert "Sales just dropped." in prompt


def test_build_persona_system_prompt_tolerates_missing_optional_fields():
    prompt = jr.build_persona_system_prompt({}, "en")
    assert "small Indian business" in prompt  # default business phrase, no crash


def test_render_conversation_for_persona_empty_list():
    assert "just starting" in jr.render_conversation_for_persona([])


def test_render_conversation_for_persona_skips_system_role_and_labels_speakers():
    turns = [
        ch.Turn(role="owner", text="how many customers?"),
        ch.Turn(role="assistant", text="let me check"),
        ch.Turn(role="system", text="[internal route: none]"),
    ]
    rendered = jr.render_conversation_for_persona(turns)
    assert "You (owner): how many customers?" in rendered
    assert "Assistant: let me check" in rendered
    assert "internal route" not in rendered


def test_render_conversation_for_persona_accepts_plain_dicts():
    turns = [{"role": "owner", "text": "hi"}, {"role": "assistant", "text": "hello"}]
    rendered = jr.render_conversation_for_persona(turns)
    assert "You (owner): hi" in rendered
    assert "Assistant: hello" in rendered


def test_build_persona_user_content_includes_goal_and_conversation():
    content = jr.build_persona_user_content("ask for a plan", "Assistant: hi")
    assert "ask for a plan" in content
    assert "Assistant: hi" in content


# --- resolve_step_message ----------------------------------------------------------------------


def test_resolve_step_message_literal():
    msg = jr.resolve_step_message({"message": "hello"}, {}, "", api_key_present=False)
    assert msg == "hello"


def test_resolve_step_message_literal_missing_message_raises():
    with pytest.raises(ValueError, match="neither 'message' nor 'persona_turn'"):
        jr.resolve_step_message({}, {}, "", api_key_present=False)


def test_resolve_step_message_persona_turn_missing_fallback_raises():
    step = {"persona_turn": {"goal": "ask something"}}
    with pytest.raises(ValueError, match="fallback_message"):
        jr.resolve_step_message(step, {}, "", api_key_present=False)


def test_resolve_step_message_persona_turn_no_api_key_uses_fallback_without_calling_persona():
    step = {"persona_turn": {"goal": "ask something"}, "fallback_message": "kitne hain?"}

    def _boom(_system, _user):
        raise AssertionError("call_persona must NOT be invoked when no API key is present")

    msg = jr.resolve_step_message(step, {}, "", api_key_present=False, call_persona=_boom)
    assert msg == "kitne hain?"


def test_resolve_step_message_persona_turn_with_api_key_calls_persona():
    step = {"persona_turn": {"goal": "ask something"}, "fallback_message": "fallback"}
    captured = {}

    def _fake_call(system, user_content):
        captured["system"] = system
        captured["user_content"] = user_content
        return "  live persona reply  "

    msg = jr.resolve_step_message(
        step, {"business": "a shop"}, "Assistant: hi", api_key_present=True, call_persona=_fake_call,
    )
    assert msg == "live persona reply"  # stripped
    assert "a shop" in captured["system"]
    assert "ask something" in captured["user_content"]
    assert "Assistant: hi" in captured["user_content"]


def test_resolve_step_message_persona_turn_step_style_overrides_persona_default():
    step = {"persona_turn": {"goal": "ask", "style": "hinglish"}, "fallback_message": "fallback"}
    captured = {}

    def _fake_call(system, _user):
        captured["system"] = system
        return "reply"

    jr.resolve_step_message(
        step, {"style": "en"}, "", api_key_present=True, call_persona=_fake_call,
    )
    assert jr.style_instruction("hinglish") in captured["system"]


def test_resolve_step_message_persona_turn_missing_goal_raises():
    step = {"persona_turn": {}, "fallback_message": "fallback"}
    with pytest.raises(ValueError, match="requires a 'goal'"):
        jr.resolve_step_message(step, {}, "", api_key_present=True, call_persona=lambda s, u: "x")


def test_resolve_step_message_persona_turn_no_injected_call_persona_raises():
    step = {"persona_turn": {"goal": "ask"}, "fallback_message": "fallback"}
    with pytest.raises(ValueError, match="no call_persona was injected"):
        jr.resolve_step_message(step, {}, "", api_key_present=True, call_persona=None)


def test_resolve_step_message_persona_turn_blind_to_asserts():
    """The persona is NEVER shown any assert_* key — it must not appear anywhere in what's passed
    to call_persona, so it can't be steered toward (or away from) a scenario's checks."""
    step = {
        "persona_turn": {"goal": "ask about lapsed customers"}, "fallback_message": "fallback",
        "assert_contains": ["SECRET_NEEDLE_12345"], "assert_not_contains": ["forbidden phrase"],
    }
    captured = {}

    def _fake_call(system, user_content):
        captured["blob"] = system + user_content
        return "reply"

    jr.resolve_step_message(step, {}, "", api_key_present=True, call_persona=_fake_call)
    assert "SECRET_NEEDLE_12345" not in captured["blob"]
    assert "forbidden phrase" not in captured["blob"]


# --- generate_persona_message (Anthropic call shape, client mocked) ----------------------------


def _fake_anthropic_response(text: str):
    block = MagicMock()
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


def test_generate_persona_message_extracts_and_strips_text():
    client = MagicMock()
    client.messages.create.return_value = _fake_anthropic_response("  haan bhej do  ")
    text = jr.generate_persona_message("system prompt", "user content", client=client)
    assert text == "haan bhej do"
    _args, kwargs = client.messages.create.call_args
    assert kwargs["model"] == jr.PERSONA_MODEL
    assert kwargs["system"] == "system prompt"


def test_generate_persona_message_empty_response_raises():
    client = MagicMock()
    client.messages.create.return_value = _fake_anthropic_response("")
    with pytest.raises(ValueError, match="empty text"):
        jr.generate_persona_message("sys", "user", client=client)


# --- evaluate_phase_db_asserts / _fold_failures_into_last --------------------------------------


def test_evaluate_phase_db_asserts_empty_is_a_noop(monkeypatch):
    def _boom(_dsn):
        raise AssertionError("must not connect when db_asserts is absent")

    monkeypatch.setattr(ch, "_connect", _boom)
    assert jr.evaluate_phase_db_asserts("dsn", "t1", None) == []
    assert jr.evaluate_phase_db_asserts("dsn", "t1", {}) == []


def test_evaluate_phase_db_asserts_dispatches_each_key_tenant_wide(monkeypatch):
    monkeypatch.setattr(ch, "_connect", lambda dsn: MagicMock())
    calls = []

    def _fake_route(_conn, tenant_id, run_id, **kw):
        calls.append(("route", tenant_id, run_id, kw))
        return ["route failure"]

    def _fake_effects(_conn, tenant_id, run_id, **kw):
        calls.append(("effects", tenant_id, run_id, kw))
        return []

    def _fake_grounded(_conn, tenant_id, run_id, **kw):
        calls.append(("grounded", tenant_id, run_id, kw))
        return ["grounded failure"]

    monkeypatch.setattr(ch, "assert_route", _fake_route)
    monkeypatch.setattr(ch, "assert_side_effects", _fake_effects)
    monkeypatch.setattr(ch, "assert_grounded_count", _fake_grounded)

    db_asserts = {
        "assert_route": {"expect_sr_delegation": True},
        "assert_side_effects": {"expect_sent_count": 0},
        "assert_grounded_count": {"expected_count": 8},
    }
    failures = jr.evaluate_phase_db_asserts("dsn", "t1", db_asserts)
    assert failures == ["route failure", "grounded failure"]
    # every dispatched call is scoped tenant-wide (run_id=None), never a per-turn run_id
    assert all(c[2] is None for c in calls)
    assert {c[0] for c in calls} == {"route", "effects", "grounded"}


def test_fold_failures_into_last_noop_on_empty():
    results = [_sr("PASS")]
    jr._fold_failures_into_last(results, [])
    assert results[0].label == "PASS"


def test_fold_failures_into_last_noop_on_empty_results():
    jr._fold_failures_into_last([], ["some failure"])  # must not raise


def test_fold_failures_into_last_flips_last_result_to_fail():
    results = [_sr("PASS"), _sr("PASS")]
    jr._fold_failures_into_last(results, ["phase db assert failed"])
    assert results[0].label == "PASS"  # only the LAST one is touched
    assert results[1].label == "FAIL"
    assert "phase db assert failed" in results[1].reasons


# --- orchestration (mocked — no real turn/DB/network is ever driven) ---------------------------


def _stub_infra(monkeypatch):
    calls: dict[str, list] = {"setup": [], "teardown": []}

    monkeypatch.setattr(ch, "_dsn", lambda: "dsn")
    monkeypatch.setattr(ch, "_ingress_base", lambda url: "http://orch")
    monkeypatch.setattr(ch, "_dev_secret", lambda: "secret")
    monkeypatch.setattr(ch, "_connect", lambda dsn: MagicMock())

    def _fake_cmd_setup(ns):
        calls["setup"].append(ns)
        ns.tenant_id = f"tenant-{len(calls['setup'])}"
        return 0

    def _fake_cmd_teardown(ns):
        calls["teardown"].append(ns.tenant_id)
        return 0

    monkeypatch.setattr(ch, "cmd_setup", _fake_cmd_setup)
    monkeypatch.setattr(ch, "cmd_teardown", _fake_cmd_teardown)
    return calls


def test_run_journey_provisions_one_tenant_and_tears_down(monkeypatch):
    calls = _stub_infra(monkeypatch)
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    journey = {
        "name": "j-test", "setup_args": ["--onboarded"],
        "phases": [{"steps": [{"message": "hi"}]}, {"steps": [{"message": "bye"}]}],
    }
    result = jr.run_journey(
        journey, scenarios_dir=Path("."), ingress_url=None, timeout=5.0, keep_tenants=False,
        verbose=False,
    )
    assert len(calls["setup"]) == 1  # ONE tenant for the whole journey, not one per phase
    assert calls["teardown"] == ["tenant-1"]
    assert result.tenant_id == "tenant-1"
    assert len(result.results) == 2  # one StepResult per step, across both phases
    assert len(result.phases) == 2


def test_run_journey_drives_one_step_at_a_time(monkeypatch):
    """Every ch.run_scenario_steps call must carry EXACTLY ONE resolved step — never a batched
    phase list — so a later persona_turn can react to an earlier step's real reply."""
    _stub_infra(monkeypatch)
    seen_step_counts = []

    def _fake_run_scenario_steps(_dsn, _base, _secret, _tenant_id, steps, **_kw):
        seen_step_counts.append(len(steps))
        return [_sr("PASS", run_id=None)]

    monkeypatch.setattr(ch, "run_scenario_steps", _fake_run_scenario_steps)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    journey = {
        "phases": [{"steps": [{"message": "a"}, {"message": "b"}, {"message": "c"}]}],
    }
    jr.run_journey(
        journey, scenarios_dir=Path("."), ingress_url=None, timeout=5.0, keep_tenants=False,
        verbose=False,
    )
    assert seen_step_counts == [1, 1, 1]


def test_run_journey_keep_tenants_skips_teardown(monkeypatch):
    calls = _stub_infra(monkeypatch)
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    journey = {"phases": [{"steps": [{"message": "hi"}]}]}
    jr.run_journey(
        journey, scenarios_dir=Path("."), ingress_url=None, timeout=5.0, keep_tenants=True,
        verbose=False,
    )
    assert calls["teardown"] == []


def test_run_journey_tears_down_even_when_a_step_raises(monkeypatch):
    calls = _stub_infra(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(ch, "run_scenario_steps", _boom)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    journey = {"phases": [{"steps": [{"message": "hi"}]}]}
    with pytest.raises(RuntimeError):
        jr.run_journey(
            journey, scenarios_dir=Path("."), ingress_url=None, timeout=5.0, keep_tenants=False,
            verbose=False,
        )
    assert calls["teardown"] == ["tenant-1"]


def test_run_journey_no_api_key_uses_fallback_message_for_persona_turn(monkeypatch):
    _stub_infra(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    seen_messages = []

    def _fake_run_scenario_steps(_dsn, _base, _secret, _tenant_id, steps, **_kw):
        seen_messages.append(steps[0]["message"])
        return [_sr("PASS", run_id=None)]

    monkeypatch.setattr(ch, "run_scenario_steps", _fake_run_scenario_steps)

    journey = {
        "phases": [{"steps": [{
            "persona_turn": {"goal": "ask about lapsed customers"},
            "fallback_message": "kitne lapsed customers hain?",
        }]}],
    }
    jr.run_journey(
        journey, scenarios_dir=Path("."), ingress_url=None, timeout=5.0, keep_tenants=False,
        verbose=False,
    )
    assert seen_messages == ["kitne lapsed customers hain?"]


def test_run_journey_persona_turn_reacts_to_prior_step_reply(monkeypatch):
    """With an API key present, a SECOND persona_turn (same or later phase) must see the FIRST
    step's real assistant reply in its conversation-so-far — proves live conversation chaining,
    not just message-list concatenation."""
    _stub_infra(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(jr, "_persona_client", lambda: "fake-client")

    call_log = []

    def _fake_generate(system, user_content, *, client, model=jr.PERSONA_MODEL):
        call_log.append(user_content)
        return f"persona reply #{len(call_log)}"

    monkeypatch.setattr(jr, "generate_persona_message", _fake_generate)

    step_replies = [
        [ch.Turn(role="owner", text="persona reply #1"),
         ch.Turn(role="assistant", text="UNIQUE_ASSISTANT_REPLY_MARKER")],
        [ch.Turn(role="owner", text="persona reply #2"),
         ch.Turn(role="assistant", text="second reply")],
    ]

    def _fake_run_scenario_steps(_dsn, _base, _secret, _tenant_id, _steps, **_kw):
        return [_sr("PASS", run_id=None, transcript=step_replies.pop(0))]

    monkeypatch.setattr(ch, "run_scenario_steps", _fake_run_scenario_steps)

    journey = {
        "phases": [{"steps": [
            {"persona_turn": {"goal": "ask first thing"}, "fallback_message": "fallback 1"},
            {"persona_turn": {"goal": "ask second thing"}, "fallback_message": "fallback 2"},
        ]}],
    }
    jr.run_journey(
        journey, scenarios_dir=Path("."), ingress_url=None, timeout=5.0, keep_tenants=False,
        verbose=False,
    )
    assert len(call_log) == 2
    assert "just starting" in call_log[0]  # no history yet for the first persona turn
    assert "UNIQUE_ASSISTANT_REPLY_MARKER" in call_log[1]  # second turn sees the real first reply


def test_run_journey_phase_db_asserts_fold_into_that_phases_last_step_only(monkeypatch):
    _stub_infra(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    monkeypatch.setattr(jr, "evaluate_phase_db_asserts", lambda dsn, tid, asserts: (
        ["phase 1 db fail"] if asserts == {"marker": "phase1"} else []
    ))

    journey = {
        "phases": [
            {"steps": [{"message": "a"}], "db_asserts": {"marker": "phase1"}},
            {"steps": [{"message": "b"}]},
        ],
    }
    result = jr.run_journey(
        journey, scenarios_dir=Path("."), ingress_url=None, timeout=5.0, keep_tenants=False,
        verbose=False,
    )
    assert result.results[0].label == "FAIL"  # phase 1's own last step carries the failure
    assert result.results[1].label == "PASS"  # phase 2 is untouched
    assert result.phases[0].db_assert_failures == ["phase 1 db fail"]
    assert result.phases[1].db_assert_failures == []


def test_run_journey_sleeps_wait_s_between_phases(monkeypatch):
    _stub_infra(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    sleeps = []
    monkeypatch.setattr(jr.time, "sleep", lambda s: sleeps.append(s))

    journey = {
        "phases": [
            {"steps": [{"message": "a"}], "wait_s": 5},
            {"steps": [{"message": "b"}]},  # last phase: no trailing sleep expected either way
        ],
    }
    jr.run_journey(
        journey, scenarios_dir=Path("."), ingress_url=None, timeout=5.0, keep_tenants=False,
        verbose=False,
    )
    assert sleeps == [5]


# --- main (CLI) ---------------------------------------------------------------------------------


def test_main_writes_json_report_in_run_full_pack_shape(monkeypatch, tmp_path):
    _stub_infra(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])

    journey_path = tmp_path / "j.json"
    journey_path.write_text(json.dumps({
        "name": "j-test", "notes": "a test journey", "setup_args": ["--onboarded"],
        "phases": [{"name": "p1", "steps": [{"message": "hi"}]}],
    }))
    report_path = tmp_path / "bundle.json"

    rc = jr.main([str(journey_path), "--json-report", str(report_path)])
    assert rc == 0

    bundle = json.loads(report_path.read_text())
    assert len(bundle) == 1
    entry = bundle[0]
    # the exact keys convo_harness.py's own _build_json_report produces — transcript_judge.py /
    # tier_rescore.py must consume this unchanged.
    for key in ("name", "setup_args", "notes", "steps", "summary"):
        assert key in entry
    assert entry["name"] == "j-test"
    assert entry["notes"] == "a test journey"
    assert len(entry["steps"]) == 1
    assert entry["steps"][0]["label"] == "PASS"
    assert entry["phases"][0]["name"] == "p1"


def test_main_exit_1_when_a_step_fails(monkeypatch, tmp_path):
    _stub_infra(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("FAIL", run_id=None)])

    journey_path = tmp_path / "j.json"
    journey_path.write_text(json.dumps({"phases": [{"steps": [{"message": "hi"}]}]}))

    rc = jr.main([str(journey_path)])
    assert rc == 1
