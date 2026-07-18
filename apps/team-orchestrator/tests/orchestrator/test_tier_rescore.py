"""#84 — tier_rescore.py batch-mode (`--batch`) tests, pure/mocked-client (no real Anthropic API
call). Covers `rescore_bundle_via_batches` (the Message Batches API path built on `batch_judge.py`)
and the `--batch` CLI flag's dispatch in `main()`. The pre-existing serial path
(`rescore_bundle` / `rescore_transcript` / `_call_judge_once`) is exercised by
`test_tier_rescore_ground_truth.py`'s pure-function tests and is UNCHANGED by this work — the tests
below additionally pin that `--batch` off (the default) still calls the serial function.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import tier_rescore as tr  # noqa: E402 — after the sys.path insert

# --- test doubles --------------------------------------------------------------------------------


def _valid_verdict_json(*, breakers=None, quality=True):
    return json.dumps({
        "trust_breakers": breakers or [],
        "quality_acceptable": quality,
        "quality_reason": "fine",
    })


def _ok(text):
    return lambda cid: SimpleNamespace(
        custom_id=cid,
        result=SimpleNamespace(type="succeeded", message=SimpleNamespace(content=[SimpleNamespace(text=text)])),
    )


def _bad_json():
    return lambda cid: SimpleNamespace(
        custom_id=cid,
        result=SimpleNamespace(type="succeeded", message=SimpleNamespace(content=[SimpleNamespace(text="not json")])),
    )


def _api_error():
    return lambda cid: SimpleNamespace(
        custom_id=cid,
        result=SimpleNamespace(
            type="errored",
            error=SimpleNamespace(error=SimpleNamespace(message="server exploded", type="api_error")),
        ),
    )


class _ScriptedBatches:
    """Fake `client.messages.batches` that scripts a PER-custom_id sequence of outcome-factories —
    the Nth time a custom_id appears across ANY `create()` call, its Nth scripted outcome-factory is
    invoked. Models the retry-round behavior of `rescore_bundle_via_batches` (round 1 = every
    scenario, round 2 = only the ones needing retry) without the fake needing to know which round
    it's in."""

    def __init__(self, outcomes_by_custom_id: dict[str, list]):
        self._outcomes = {k: list(v) for k, v in outcomes_by_custom_id.items()}
        self._appearance_count: dict[str, int] = {}
        self.create_calls: list[list[str]] = []
        self._pending_results: dict[str, list] = {}
        self._batch_counter = 0

    def create(self, *, requests):
        self._batch_counter += 1
        batch_id = f"batch-{self._batch_counter}"
        custom_ids = [r["custom_id"] for r in requests]
        self.create_calls.append(custom_ids)
        results = []
        for cid in custom_ids:
            count = self._appearance_count.get(cid, 0)
            factories = self._outcomes.get(cid, [_ok(_valid_verdict_json())])
            factory = factories[min(count, len(factories) - 1)]
            self._appearance_count[cid] = count + 1
            results.append(factory(cid))
        self._pending_results[batch_id] = results
        return SimpleNamespace(id=batch_id)

    def retrieve(self, batch_id):
        return SimpleNamespace(processing_status="ended")

    def results(self, batch_id):
        return iter(self._pending_results.get(batch_id, []))


class _FakeClient:
    def __init__(self, batches: _ScriptedBatches):
        self.messages = SimpleNamespace(batches=batches)


def _entries(names: list[str]) -> list[dict]:
    return [{"name": n, "steps": [{"transcript": [{"role": "owner", "text": "hi"}]}]} for n in names]


# --- rescore_bundle_via_batches — happy path -------------------------------------------------------


def test_rescore_bundle_via_batches_all_succeed_first_round():
    entries = _entries(["s0", "s1"])
    outcomes = {
        "scenario-0": [_ok(_valid_verdict_json())],
        "scenario-1": [_ok(_valid_verdict_json(quality=False))],
    }
    client = _FakeClient(_ScriptedBatches(outcomes))
    verdicts, unscored = tr.rescore_bundle_via_batches(entries, model="claude-sonnet-5", client=client)
    assert unscored == []
    assert [v.scenario for v in verdicts] == ["s0", "s1"]
    assert verdicts[0].quality_acceptable is True
    assert verdicts[1].quality_acceptable is False
    assert client.messages.batches.create_calls[0] == ["scenario-0", "scenario-1"]
    # no retry round needed -> exactly ONE Anthropic Batches API create() call
    assert len(client.messages.batches.create_calls) == 1


def test_rescore_bundle_via_batches_preserves_original_entry_order():
    """verdicts is a stable subsequence of the ORIGINAL entry order, matching rescore_bundle's own
    ordering guarantee, even though Message Batches results arrive keyed (not positionally)."""
    entries = _entries(["z", "a", "m"])
    outcomes = {
        "scenario-0": [_ok(_valid_verdict_json())],
        "scenario-1": [_ok(_valid_verdict_json())],
        "scenario-2": [_ok(_valid_verdict_json())],
    }
    client = _FakeClient(_ScriptedBatches(outcomes))
    verdicts, unscored = tr.rescore_bundle_via_batches(entries, model="claude-sonnet-5", client=client)
    assert [v.scenario for v in verdicts] == ["z", "a", "m"]


# --- retry-once semantics (SAME as the serial rescore_transcript) ----------------------------------


def test_rescore_bundle_via_batches_retries_a_parse_failure_then_succeeds():
    entries = _entries(["s0", "s1"])
    outcomes = {
        "scenario-0": [_ok(_valid_verdict_json())],
        "scenario-1": [_bad_json(), _ok(_valid_verdict_json())],  # fails parse once, then succeeds
    }
    client = _FakeClient(_ScriptedBatches(outcomes))
    verdicts, unscored = tr.rescore_bundle_via_batches(entries, model="claude-sonnet-5", client=client)
    assert unscored == []
    assert {v.scenario for v in verdicts} == {"s0", "s1"}
    # TWO Anthropic Batches API create() calls: round 1 (both) + round 2 (retry, s1 only)
    assert len(client.messages.batches.create_calls) == 2
    assert client.messages.batches.create_calls[1] == ["scenario-1"]


def test_rescore_bundle_via_batches_retries_an_api_error_then_succeeds():
    entries = _entries(["s0"])
    outcomes = {"scenario-0": [_api_error(), _ok(_valid_verdict_json())]}
    client = _FakeClient(_ScriptedBatches(outcomes))
    verdicts, unscored = tr.rescore_bundle_via_batches(entries, model="claude-sonnet-5", client=client)
    assert unscored == []
    assert len(verdicts) == 1
    assert len(client.messages.batches.create_calls) == 2


def test_rescore_bundle_via_batches_still_failing_after_retry_is_unscored():
    """Two failures in a row -> UnscoredResult, never silently dropped (Rule #15 posture, same as
    rescore_transcript's own retry-once-then-unscored behavior)."""
    entries = _entries(["s0", "s1"])
    outcomes = {
        "scenario-0": [_ok(_valid_verdict_json())],
        "scenario-1": [_bad_json(), _bad_json()],
    }
    client = _FakeClient(_ScriptedBatches(outcomes))
    verdicts, unscored = tr.rescore_bundle_via_batches(entries, model="claude-sonnet-5", client=client)
    assert [v.scenario for v in verdicts] == ["s0"]
    assert [u.scenario for u in unscored] == ["s1"]
    assert unscored[0].error  # non-empty explicit error message


def test_rescore_bundle_via_batches_mixed_order_preserved_with_one_unscored():
    entries = _entries(["a", "b", "c"])
    outcomes = {
        "scenario-0": [_ok(_valid_verdict_json())],
        "scenario-1": [_bad_json(), _bad_json()],
        "scenario-2": [_ok(_valid_verdict_json())],
    }
    client = _FakeClient(_ScriptedBatches(outcomes))
    verdicts, unscored = tr.rescore_bundle_via_batches(entries, model="claude-sonnet-5", client=client)
    assert [v.scenario for v in verdicts] == ["a", "c"]
    assert [u.scenario for u in unscored] == ["b"]


# --- CLI wiring ------------------------------------------------------------------------------------


def test_build_parser_batch_flag_defaults_off():
    args = tr.build_parser().parse_args(["bundle.json"])
    assert args.batch is False


def test_build_parser_batch_flag_can_be_set():
    args = tr.build_parser().parse_args(["bundle.json", "--batch"])
    assert args.batch is True


def test_main_default_dispatches_to_serial_rescore_bundle(tmp_path, monkeypatch):
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps([{"name": "s0", "steps": []}]), encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(tr, "_client", lambda: object())
    calls = {"serial": 0, "batch": 0}

    def fake_serial(entries, *, model, client):
        calls["serial"] += 1
        return [], []

    def fake_batch(entries, *, model, client, **kw):
        calls["batch"] += 1
        return [], []

    monkeypatch.setattr(tr, "rescore_bundle", fake_serial)
    monkeypatch.setattr(tr, "rescore_bundle_via_batches", fake_batch)
    tr.main([str(bundle_path)])
    assert calls == {"serial": 1, "batch": 0}


def test_main_batch_flag_dispatches_to_rescore_bundle_via_batches(tmp_path, monkeypatch):
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps([{"name": "s0", "steps": []}]), encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(tr, "_client", lambda: object())
    calls = {"serial": 0, "batch": 0}

    def fake_serial(entries, *, model, client):
        calls["serial"] += 1
        return [], []

    def fake_batch(entries, *, model, client, **kw):
        calls["batch"] += 1
        return [], []

    monkeypatch.setattr(tr, "rescore_bundle", fake_serial)
    monkeypatch.setattr(tr, "rescore_bundle_via_batches", fake_batch)
    tr.main([str(bundle_path), "--batch"])
    assert calls == {"serial": 0, "batch": 1}
