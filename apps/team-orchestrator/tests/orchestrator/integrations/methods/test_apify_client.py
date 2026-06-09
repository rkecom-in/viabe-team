"""VT-364 — the shared async Apify client (start → poll → fetch). Behavioral, no network:
an injected transport + sleep drive the run/poll/fetch sequence deterministically."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.methods.apify_client import run_actor  # noqa: E402

_NOSLEEP = lambda _s: None  # noqa: E731


def test_run_actor_start_poll_fetch_returns_items():
    calls = []

    def req(method, url, params, json):
        calls.append((method, url.rsplit("/v2/", 1)[-1]))
        if url.endswith("/runs"):
            return {"data": {"id": "RUN1", "defaultDatasetId": "DS1", "status": "RUNNING"}}
        if "/actor-runs/" in url:
            return {"data": {"id": "RUN1", "status": "SUCCEEDED", "defaultDatasetId": "DS1"}}
        if "/datasets/" in url:
            return [{"rating": 4.2}, {"rating": 5.0}]  # dataset-items is a JSON ARRAY
        raise AssertionError(url)

    items = run_actor("acme~scraper", {"maxItems": 1}, "tok", poll_s=0, sleep_fn=_NOSLEEP, request_fn=req)
    assert items == [{"rating": 4.2}, {"rating": 5.0}]
    assert calls[0] == ("POST", "acts/acme~scraper/runs")  # started by run, not run-sync
    assert any(m == "GET" and "actor-runs/RUN1" in u for m, u in calls)  # polled
    assert any(m == "GET" and "datasets/DS1/items" in u for m, u in calls)  # fetched


def test_run_actor_failed_status_is_fail_soft():
    def req(method, url, params, json):
        if url.endswith("/runs"):
            return {"data": {"id": "R", "defaultDatasetId": "D", "status": "RUNNING"}}
        return {"data": {"status": "FAILED"}}

    assert run_actor("a~b", {}, "t", poll_s=0, sleep_fn=_NOSLEEP, request_fn=req) == []


def test_run_actor_budget_exceeded_is_fail_soft():
    clock = {"t": 0.0}

    def mono():
        clock["t"] += 100.0  # every check jumps 100s → blows a 10s budget immediately
        return clock["t"]

    def req(method, url, params, json):
        if url.endswith("/runs"):
            return {"data": {"id": "R", "defaultDatasetId": "D", "status": "RUNNING"}}
        return {"data": {"status": "RUNNING"}}  # never terminal

    out = run_actor("a~b", {}, "t", budget_s=10, poll_s=0, sleep_fn=_NOSLEEP, request_fn=req, monotonic_fn=mono)
    assert out == []  # budget cap → fail-soft empty, never hangs


def test_run_actor_no_run_id_is_fail_soft():
    assert run_actor("a~b", {}, "t", request_fn=lambda *a: {"data": {}}, sleep_fn=_NOSLEEP) == []
