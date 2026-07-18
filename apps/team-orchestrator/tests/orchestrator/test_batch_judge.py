"""#84 — canaries/batch_judge.py pure-function + mocked-client tests (no real Anthropic API call,
no real network, no real sleeping).

batch_judge.py never imports `anthropic` at module scope (every function takes an
already-constructed, duck-typed `client`) — these tests build lightweight `SimpleNamespace` doubles
shaped exactly like the installed SDK's `MessageBatch` / `MessageBatchIndividualResponse` /
`MessageBatchSucceededResult` / `MessageBatchErroredResult` objects (verified against
`anthropic==0.103.1` in this worktree) rather than importing the real SDK types.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import batch_judge as bj  # noqa: E402 — after the sys.path insert

# --- test doubles --------------------------------------------------------------------------------


def _succeeded(custom_id: str, text: str):
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(content=[SimpleNamespace(text=text)]),
        ),
    )


def _errored(custom_id: str, *, message: str = "bad request", err_type: str = "invalid_request_error"):
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(
            type="errored",
            error=SimpleNamespace(error=SimpleNamespace(message=message, type=err_type)),
        ),
    )


def _expired(custom_id: str):
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="expired"))


def _canceled(custom_id: str):
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="canceled"))


class _FakeBatchesResource:
    """Duck-typed stand-in for `client.messages.batches` — records every `create()` payload and
    lets tests script a sequence of `retrieve()` responses (to simulate polling) plus a fixed
    `results()` list."""

    def __init__(self, *, batch_id="msgbatch_1", retrieve_sequence=None, results=None):
        self.batch_id = batch_id
        self.create_calls: list[list[dict]] = []
        self.retrieve_calls = 0
        self._retrieve_sequence = list(retrieve_sequence) if retrieve_sequence is not None else None
        self._results = list(results) if results is not None else []

    def create(self, *, requests):
        self.create_calls.append(list(requests))
        return SimpleNamespace(id=self.batch_id)

    def retrieve(self, batch_id):
        self.retrieve_calls += 1
        if self._retrieve_sequence is not None:
            idx = min(self.retrieve_calls - 1, len(self._retrieve_sequence) - 1)
            return SimpleNamespace(processing_status=self._retrieve_sequence[idx])
        return SimpleNamespace(processing_status="ended")

    def results(self, batch_id):
        return iter(self._results)


class _FakeClient:
    def __init__(self, batches: _FakeBatchesResource):
        self.messages = SimpleNamespace(batches=batches)


def _fake_sleep_tracker():
    calls: list[float] = []

    def sleep_fn(seconds: float) -> None:
        calls.append(seconds)

    return calls, sleep_fn


def _fake_clock(start: float = 0.0):
    """A deterministic clock: each call to `.tick(n)` advances time; `clock_fn()` reads current."""

    state = {"t": start}

    def clock_fn() -> float:
        return state["t"]

    def advance(seconds: float) -> None:
        state["t"] += seconds

    return clock_fn, advance


# --- _build_request -------------------------------------------------------------------------------


def test_build_request_shape():
    item = bj.BatchJudgeItem(
        custom_id="scenario-0", system="SYS PROMPT", user_content="USER TEXT",
        model="claude-opus-4-8", max_tokens=4096,
    )
    req = bj._build_request(item)
    assert req == {
        "custom_id": "scenario-0",
        "params": {
            "model": "claude-opus-4-8",
            "max_tokens": 4096,
            "system": "SYS PROMPT",
            "messages": [{"role": "user", "content": "USER TEXT"}],
        },
    }


# --- _extract_text ---------------------------------------------------------------------------------


def test_extract_text_concatenates_text_blocks():
    message = SimpleNamespace(content=[SimpleNamespace(text="hello "), SimpleNamespace(text="world")])
    assert bj._extract_text(message) == "hello world"


def test_extract_text_ignores_non_text_blocks():
    message = SimpleNamespace(content=[SimpleNamespace(text="a"), SimpleNamespace(other="b")])
    assert bj._extract_text(message) == "a"


def test_extract_text_empty_when_no_content():
    assert bj._extract_text(SimpleNamespace(content=[])) == ""
    assert bj._extract_text(SimpleNamespace()) == ""


# --- _describe_error --------------------------------------------------------------------------------


def test_describe_error_with_message_and_type():
    error_response = SimpleNamespace(error=SimpleNamespace(message="bad model", type="invalid_request_error"))
    assert bj._describe_error(error_response) == "invalid_request_error: bad model"


def test_describe_error_falls_back_to_str_when_shape_unrecognized():
    assert bj._describe_error("some raw error string") == "some raw error string"


# --- _result_for_response ----------------------------------------------------------------------------


def test_result_for_response_succeeded():
    result = bj._result_for_response(_succeeded("scenario-0", "hello"))
    assert result.custom_id == "scenario-0"
    assert result.text == "hello"
    assert result.error is None
    assert result.ok is True


def test_result_for_response_errored_surfaces_explicit_error():
    result = bj._result_for_response(_errored("scenario-1", message="oops"))
    assert result.custom_id == "scenario-1"
    assert result.text is None
    assert result.ok is False
    assert "oops" in result.error


def test_result_for_response_expired_surfaces_explicit_error():
    result = bj._result_for_response(_expired("scenario-2"))
    assert result.ok is False
    assert "expired" in result.error


def test_result_for_response_canceled_surfaces_explicit_error():
    result = bj._result_for_response(_canceled("scenario-3"))
    assert result.ok is False
    assert "canceled" in result.error


def test_result_for_response_unknown_type_is_a_fail_not_skip_error():
    resp = SimpleNamespace(custom_id="scenario-4", result=SimpleNamespace(type="something_new"))
    result = bj._result_for_response(resp)
    assert result.ok is False
    assert "something_new" in result.error


# --- submit_batch ------------------------------------------------------------------------------------


def test_submit_batch_sends_one_request_per_item_and_returns_batch_id():
    batches = _FakeBatchesResource(batch_id="msgbatch_abc")
    client = _FakeClient(batches)
    items = [
        bj.BatchJudgeItem(custom_id="a", system="SYS", user_content="U1", model="m", max_tokens=10),
        bj.BatchJudgeItem(custom_id="b", system="SYS", user_content="U2", model="m", max_tokens=10),
    ]
    batch_id = bj.submit_batch(items, client=client)
    assert batch_id == "msgbatch_abc"
    assert len(batches.create_calls) == 1
    payload = batches.create_calls[0]
    assert [r["custom_id"] for r in payload] == ["a", "b"]
    assert payload[0]["params"]["messages"][0]["content"] == "U1"


def test_submit_batch_rejects_empty_items():
    client = _FakeClient(_FakeBatchesResource())
    with pytest.raises(ValueError, match="no items given"):
        bj.submit_batch([], client=client)


# --- wait_for_batch ----------------------------------------------------------------------------------


def test_wait_for_batch_returns_immediately_when_already_ended():
    batches = _FakeBatchesResource(retrieve_sequence=["ended"])
    client = _FakeClient(batches)
    clock_fn, _advance = _fake_clock()
    sleep_calls, sleep_fn = _fake_sleep_tracker()
    batch = bj.wait_for_batch(
        "msgbatch_1", client=client, poll_interval_s=10.0, timeout_s=1200.0,
        sleep_fn=sleep_fn, clock_fn=clock_fn,
    )
    assert batch.processing_status == "ended"
    assert sleep_calls == []  # never slept — no polling needed


def test_wait_for_batch_polls_until_ended():
    batches = _FakeBatchesResource(retrieve_sequence=["in_progress", "in_progress", "ended"])
    client = _FakeClient(batches)
    clock_fn, _advance = _fake_clock()
    sleep_calls, sleep_fn = _fake_sleep_tracker()
    batch = bj.wait_for_batch(
        "msgbatch_1", client=client, poll_interval_s=10.0, timeout_s=1200.0,
        sleep_fn=sleep_fn, clock_fn=clock_fn,
    )
    assert batch.processing_status == "ended"
    assert sleep_calls == [10.0, 10.0]
    assert batches.retrieve_calls == 3


def test_wait_for_batch_raises_batch_timeout_error_when_never_ends():
    batches = _FakeBatchesResource(retrieve_sequence=["in_progress"] * 10)
    client = _FakeClient(batches)
    clock_fn, advance = _fake_clock()

    def sleep_fn(seconds: float) -> None:
        advance(seconds)  # simulate time passing without a real sleep

    with pytest.raises(bj.BatchTimeoutError, match="msgbatch_1"):
        bj.wait_for_batch(
            "msgbatch_1", client=client, poll_interval_s=10.0, timeout_s=25.0,
            sleep_fn=sleep_fn, clock_fn=clock_fn,
        )


# --- collect_results ---------------------------------------------------------------------------------


def test_collect_results_maps_by_custom_id():
    batches = _FakeBatchesResource(results=[_succeeded("a", "text-a"), _succeeded("b", "text-b")])
    client = _FakeClient(batches)
    results = bj.collect_results("msgbatch_1", ["a", "b"], client=client)
    assert results["a"].text == "text-a"
    assert results["b"].text == "text-b"


def test_collect_results_fails_loud_on_missing_custom_id():
    """Fail-not-skip: a custom_id never returned by results() must not silently vanish from the
    output dict — it gets an explicit error entry instead."""
    batches = _FakeBatchesResource(results=[_succeeded("a", "text-a")])
    client = _FakeClient(batches)
    results = bj.collect_results("msgbatch_1", ["a", "b"], client=client)
    assert results["a"].ok is True
    assert results["b"].ok is False
    assert "missing from batch" in results["b"].error


def test_collect_results_surfaces_errored_expired_canceled_items():
    batches = _FakeBatchesResource(
        results=[_succeeded("a", "ok"), _errored("b"), _expired("c"), _canceled("d")],
    )
    client = _FakeClient(batches)
    results = bj.collect_results("msgbatch_1", ["a", "b", "c", "d"], client=client)
    assert results["a"].ok is True
    assert results["b"].ok is False
    assert results["c"].ok is False
    assert results["d"].ok is False


# --- run_batch_judge (end to end through the fake client) --------------------------------------------


def test_run_batch_judge_end_to_end_success():
    batches = _FakeBatchesResource(
        batch_id="msgbatch_e2e",
        retrieve_sequence=["in_progress", "ended"],
        results=[_succeeded("x", "verdict-x"), _succeeded("y", "verdict-y")],
    )
    client = _FakeClient(batches)
    items = [
        bj.BatchJudgeItem(custom_id="x", system="SYS", user_content="U", model="m", max_tokens=10),
        bj.BatchJudgeItem(custom_id="y", system="SYS", user_content="U2", model="m", max_tokens=10),
    ]
    _, sleep_fn = _fake_sleep_tracker()
    clock_fn, _advance = _fake_clock()
    results = bj.run_batch_judge(
        items, client=client, poll_interval_s=1.0, timeout_s=100.0, sleep_fn=sleep_fn, clock_fn=clock_fn,
    )
    assert results["x"].text == "verdict-x"
    assert results["y"].text == "verdict-y"
    assert len(batches.create_calls) == 1  # ONE batch request for both items


def test_run_batch_judge_propagates_timeout():
    batches = _FakeBatchesResource(retrieve_sequence=["in_progress"] * 5)
    client = _FakeClient(batches)
    clock_fn, advance = _fake_clock()

    def sleep_fn(seconds: float) -> None:
        advance(seconds)

    items = [bj.BatchJudgeItem(custom_id="x", system="SYS", user_content="U", model="m", max_tokens=10)]
    with pytest.raises(bj.BatchTimeoutError):
        bj.run_batch_judge(
            items, client=client, poll_interval_s=10.0, timeout_s=15.0, sleep_fn=sleep_fn, clock_fn=clock_fn,
        )
