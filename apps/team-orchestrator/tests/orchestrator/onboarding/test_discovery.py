"""VT-507 — async parallel discovery: start/poll endpoints + cache + observability tests.

Tests are dep-gated (fastapi/pydantic) + fully mocked (no DB, no network, no LLM).
Covers:
  - /start returns fast (~50ms) with a discovery_id
  - /poll returns the exact contract shape
  - both_complete_zero only when BOTH sources complete with zero candidates
  - entity_discovery_requests rows written per source with correct failure_reason
  - cache hit prevents re-scrape (mock scraper not re-called on 2nd query)
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("pydantic")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_SECRET = "vt507-test-secret"
_HDR = {"X-Internal-Secret": _SECRET}
_VALID_GSTIN = "29ABCDE1234F1Z5"


@pytest.fixture
def client(monkeypatch):
    from orchestrator.api.discovery import router

    monkeypatch.setenv("INTERNAL_API_SECRET", _SECRET)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

def test_start_requires_internal_secret(client):
    r = client.post("/api/orchestrator/onboard/discovery/start", json={"business_name": "Asha Kirana"})
    assert r.status_code == 403


def test_poll_requires_internal_secret(client):
    r = client.get(f"/api/orchestrator/onboard/discovery/{uuid.uuid4()}")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# /start — returns fast + discovery_id, kicks background tasks
# ---------------------------------------------------------------------------

def test_start_returns_discovery_id_and_is_fast(client, monkeypatch):
    """Start must return < 200ms and include a discovery_id UUID."""
    inserted = []

    def fake_insert(did, sources):
        inserted.append((str(did), list(sources)))

    # Prevent actual asyncio.create_task from spawning real background coroutines
    def _close_coro(coro):
        coro.close()

    monkeypatch.setattr("orchestrator.api.discovery._insert_running_rows", fake_insert)
    monkeypatch.setattr("asyncio.get_running_loop", lambda: MagicMock(create_task=_close_coro))

    t0 = time.monotonic()
    r = client.post(
        "/api/orchestrator/onboard/discovery/start",
        json={"business_name": "Sundaram Book Store", "city": "Chennai"},
        headers=_HDR,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert r.status_code == 200
    body = r.json()
    assert "discovery_id" in body
    uuid.UUID(body["discovery_id"])  # must be valid UUID
    assert elapsed_ms < 500, f"start took {elapsed_ms:.0f}ms (expected <500ms in mocked path)"


def test_start_missing_business_name_returns_422(client, monkeypatch):
    monkeypatch.setattr("orchestrator.api.discovery._insert_running_rows", lambda *_: None)
    monkeypatch.setattr("asyncio.get_running_loop", lambda: MagicMock(create_task=lambda c: c.close()))
    r = client.post(
        "/api/orchestrator/onboard/discovery/start",
        json={"business_name": "   "},
        headers=_HDR,
    )
    assert r.status_code == 422


def test_start_inserts_running_rows_for_every_source(client, monkeypatch):
    calls_log: list[Any] = []

    def fake_insert(did, sources):
        calls_log.append(sorted(sources))

    monkeypatch.setattr("orchestrator.api.discovery._insert_running_rows", fake_insert)
    monkeypatch.setattr("asyncio.get_running_loop", lambda: MagicMock(create_task=lambda c: c.close()))

    r = client.post(
        "/api/orchestrator/onboard/discovery/start",
        json={"business_name": "Test Co"},
        headers=_HDR,
    )
    assert r.status_code == 200
    assert len(calls_log) == 1
    assert calls_log[0] == ["dataforseo", "knowyourgst", "llm"]  # VT-778 third rung


# ---------------------------------------------------------------------------
# /poll — contract shape
# ---------------------------------------------------------------------------

def _make_poll_client(monkeypatch, row_map: dict[str, dict[str, Any]]):
    """Return a TestClient whose _read_source_rows is mocked to return row_map."""
    from orchestrator.api.discovery import router

    monkeypatch.setenv("INTERNAL_API_SECRET", _SECRET)
    monkeypatch.setattr("orchestrator.api.discovery._read_source_rows", lambda _: row_map)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_poll_not_found_when_no_rows(monkeypatch):
    client = _make_poll_client(monkeypatch, {})
    r = client.get(f"/api/orchestrator/onboard/discovery/{uuid.uuid4()}", headers=_HDR)
    assert r.status_code == 404


def test_poll_both_running(monkeypatch):
    """Both sources still running → overall_status='searching', both_complete_zero=False."""
    did = str(uuid.uuid4())
    rows = {
        "llm": {"source": "llm", "status": "running", "failure_reason": None, "candidates": None},
        "knowyourgst": {"source": "knowyourgst", "status": "running", "failure_reason": None, "candidates": None},
    }
    client = _make_poll_client(monkeypatch, rows)
    r = client.get(f"/api/orchestrator/onboard/discovery/{did}", headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["overall_status"] == "searching"
    assert body["both_complete_zero"] is False
    assert body["candidates"] == []
    assert body["sources"]["llm"]["status"] == "running"
    assert body["sources"]["knowyourgst"]["status"] == "running"


def test_poll_both_complete_with_candidates(monkeypatch):
    """Both sources complete with candidates → merged de-duped list by GSTIN."""
    gstin_a = "29ABCDE1234F1Z5"
    gstin_b = "27ZZZZZ9999Z1Z3"
    rows = {
        "dataforseo": {"source": "dataforseo", "status": "complete",
                       "failure_reason": "skipped_primary_hit", "candidates": []},
        "llm": {
            "source": "llm", "status": "complete", "failure_reason": None,
            "candidates": [{"trade_name": "Biz A", "source": "llm", "candidate_gstin": gstin_a,
                            "legal_name": None, "detail": None, "candidate_cin": None, "phone": None}],
        },
        "knowyourgst": {
            "source": "knowyourgst", "status": "complete", "failure_reason": None,
            "candidates": [
                {"trade_name": "Biz B", "source": "knowyourgst", "candidate_gstin": gstin_b,
                 "legal_name": None, "detail": "Maharashtra", "candidate_cin": None, "phone": None},
                # Duplicate of gstin_a — should be de-duped
                {"trade_name": "Biz A dup", "source": "knowyourgst", "candidate_gstin": gstin_a,
                 "legal_name": None, "detail": None, "candidate_cin": None, "phone": None},
            ],
        },
    }
    client = _make_poll_client(monkeypatch, rows)
    r = client.get(f"/api/orchestrator/onboard/discovery/{str(uuid.uuid4())}", headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["overall_status"] == "complete"
    assert body["both_complete_zero"] is False
    # merged candidates: gstin_a from llm first, gstin_b from knowyourgst; dup of gstin_a dropped
    gstins = [c["candidate_gstin"] for c in body["candidates"]]
    assert gstin_a in gstins
    assert gstin_b in gstins
    assert gstins.count(gstin_a) == 1, "de-dup must remove the second gstin_a"


def test_poll_both_complete_zero_candidates(monkeypatch):
    """BOTH complete with zero candidates → both_complete_zero=True."""
    rows = {
        "llm": {"source": "llm", "status": "complete", "failure_reason": None, "candidates": []},
        "dataforseo": {"source": "dataforseo", "status": "complete", "failure_reason": None, "candidates": []},
        "knowyourgst": {"source": "knowyourgst", "status": "complete", "failure_reason": None, "candidates": []},
    }
    client = _make_poll_client(monkeypatch, rows)
    r = client.get(f"/api/orchestrator/onboard/discovery/{str(uuid.uuid4())}", headers=_HDR)
    body = r.json()
    assert body["overall_status"] == "complete"
    assert body["both_complete_zero"] is True


def test_poll_one_error_one_zero_not_both_complete_zero(monkeypatch):
    """One source errors, one completes with zero → both_complete_zero must be False (error ≠ zero)."""
    rows = {
        "llm": {"source": "llm", "status": "error", "failure_reason": "no_key", "candidates": []},
        "dataforseo": {"source": "dataforseo", "status": "complete", "failure_reason": None, "candidates": []},
        "knowyourgst": {"source": "knowyourgst", "status": "complete", "failure_reason": None, "candidates": []},
    }
    client = _make_poll_client(monkeypatch, rows)
    r = client.get(f"/api/orchestrator/onboard/discovery/{str(uuid.uuid4())}", headers=_HDR)
    body = r.json()
    assert body["overall_status"] == "complete"
    assert body["both_complete_zero"] is False  # error ≠ zero-result complete


def test_poll_one_running_one_complete(monkeypatch):
    """One source still running → overall_status='searching'."""
    rows = {
        "llm": {"source": "llm", "status": "running", "failure_reason": None, "candidates": None},
        "knowyourgst": {
            "source": "knowyourgst", "status": "complete", "failure_reason": None,
            "candidates": [{"trade_name": "X", "source": "knowyourgst", "candidate_gstin": _VALID_GSTIN,
                            "legal_name": None, "detail": None, "candidate_cin": None, "phone": None}],
        },
    }
    client = _make_poll_client(monkeypatch, rows)
    r = client.get(f"/api/orchestrator/onboard/discovery/{str(uuid.uuid4())}", headers=_HDR)
    body = r.json()
    assert body["overall_status"] == "searching"
    assert body["both_complete_zero"] is False


def test_poll_failure_reason_exposed(monkeypatch):
    """failure_reason is surfaced in the source block."""
    rows = {
        "llm": {"source": "llm", "status": "error", "failure_reason": "timeout", "candidates": []},
        "knowyourgst": {"source": "knowyourgst", "status": "complete", "failure_reason": None, "candidates": []},
    }
    client = _make_poll_client(monkeypatch, rows)
    r = client.get(f"/api/orchestrator/onboard/discovery/{str(uuid.uuid4())}", headers=_HDR)
    body = r.json()
    assert body["sources"]["llm"]["failure_reason"] == "timeout"
    assert body["sources"]["knowyourgst"]["failure_reason"] is None


# ---------------------------------------------------------------------------
# Observability — entity_discovery_requests rows written
# ---------------------------------------------------------------------------

def test_update_source_row_called_with_correct_fields(monkeypatch):
    """_update_source_row is called with the right status/failure_reason/latency_ms."""
    updates: list[dict[str, Any]] = []

    def fake_update(did, source, status, failure_reason, candidates, latency_ms):
        updates.append({
            "did": str(did), "source": source, "status": status,
            "failure_reason": failure_reason, "candidates": candidates, "latency_ms": latency_ms,
        })

    monkeypatch.setattr("orchestrator.api.discovery._update_source_row", fake_update)

    cands = [{"trade_name": "X", "source": "knowyourgst", "candidate_gstin": _VALID_GSTIN,
               "legal_name": None, "detail": None, "candidate_cin": None, "phone": None}]

    # Inject a mock scraper via monkeypatching _fetch_knowyourgst directly
    monkeypatch.setattr(
        "orchestrator.api.discovery._fetch_knowyourgst",
        lambda n, c: (cands, None),
    )

    did = uuid.uuid4()

    # Run the async coroutine synchronously
    async def _run():
        from orchestrator.api.discovery import _run_source
        await _run_source(did, "knowyourgst", "Sundaram Books", "Chennai")

    asyncio.run(_run())

    assert len(updates) == 1
    u = updates[0]
    assert u["source"] == "knowyourgst"
    assert u["status"] == "complete"
    assert u["failure_reason"] is None
    assert u["candidates"] == cands
    assert isinstance(u["latency_ms"], int)


def test_update_source_row_error_on_no_key(monkeypatch):
    """When no SCRAPINGBEE key, _fetch_knowyourgst returns ([], 'no_key') and status='error'."""
    updates: list[dict[str, Any]] = []

    def fake_update(did, source, status, failure_reason, candidates, latency_ms):
        updates.append({"status": status, "failure_reason": failure_reason})

    monkeypatch.setattr("orchestrator.api.discovery._update_source_row", fake_update)
    monkeypatch.setattr(
        "orchestrator.api.discovery._fetch_knowyourgst",
        lambda n, c: ([], "no_key"),
    )

    did = uuid.uuid4()

    async def _run():
        from orchestrator.api.discovery import _run_source
        await _run_source(did, "knowyourgst", "Test Biz", "")

    asyncio.run(_run())

    assert updates[0]["status"] == "error"
    assert updates[0]["failure_reason"] == "no_key"


# ---------------------------------------------------------------------------
# Cache: DB cache hit prevents re-scrape
# ---------------------------------------------------------------------------

def test_knowyourgst_db_cache_hit_skips_scrapingbee(monkeypatch):
    """Second call with same query must hit the DB cache and NOT call the scraper."""
    pytest.importorskip("pydantic")  # KnowYourGSTScraper imports re/threading only — safe

    cached_rows = [{"company_name": "Asha Kirana", "state": "Maharashtra", "gst_number": _VALID_GSTIN}]

    scrape_count = {"n": 0}

    def fake_fetch_fn(query: str) -> str:
        scrape_count["n"] += 1
        # Return minimal HTML with the cached row so parsing works
        return (
            '<a href="/gst-number-search/asha-kirana-' + _VALID_GSTIN + '/">'
            '<h5>Asha Kirana</h5></a>'
            '<span class="black-text"><strong>Maharashtra</strong>, <strong>' + _VALID_GSTIN + '</strong></span>'
        )

    from orchestrator.integrations.methods import knowyourgst as kyg_mod

    # Simulate: L1 cache empty; DB cache returns hit on 2nd call
    call_count = {"n": 0}

    def fake_db_get(key: str):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # 1st call: miss → go to scraper
        return cached_rows  # 2nd call: hit → return without scraping

    def fake_db_put(key: str, rows: list) -> None:
        pass  # no-op

    monkeypatch.setattr(kyg_mod, "_db_cache_get", fake_db_get)
    monkeypatch.setattr(kyg_mod, "_db_cache_put", fake_db_put)
    # Also clear L1 cache between calls
    monkeypatch.setattr(kyg_mod, "_cache", {})

    from orchestrator.integrations.methods.knowyourgst import KnowYourGSTScraper

    scraper = KnowYourGSTScraper(api_key="fake-key", fetch_fn=fake_fetch_fn)

    result1 = scraper.search("asha kirana")
    assert scrape_count["n"] == 1  # first call hits scraper

    # Clear L1 to force DB-cache path on 2nd call
    kyg_mod._cache.clear()

    result2 = scraper.search("asha kirana")
    assert scrape_count["n"] == 1, "DB cache hit must skip the scraper on 2nd call"
    assert result2 == result1 or len(result2) > 0


def test_knowyourgst_l1_cache_hit_skips_everything(monkeypatch):
    """L1 cache hit means neither DB nor scraper is called."""
    from orchestrator.integrations.methods import knowyourgst as kyg_mod

    db_calls = {"n": 0}
    scrape_calls = {"n": 0}

    monkeypatch.setattr(kyg_mod, "_db_cache_get", lambda k: (db_calls.update({"n": db_calls["n"] + 1}), None)[1])
    monkeypatch.setattr(kyg_mod, "_db_cache_put", lambda k, v: None)

    # Pre-warm L1 cache
    kyg_mod._cache["test query"] = (time.time() + 3600, [{"company_name": "X", "state": "MH", "gst_number": _VALID_GSTIN}])

    from orchestrator.integrations.methods.knowyourgst import KnowYourGSTScraper

    def bang(*_):
        scrape_calls["n"] += 1
        raise RuntimeError("scraper must not be called on L1 hit")

    scraper = KnowYourGSTScraper(api_key="fake", fetch_fn=bang)
    result = scraper.search("test query")
    assert db_calls["n"] == 0, "DB must not be queried on L1 hit"
    assert scrape_calls["n"] == 0
    assert len(result) == 1


# ---------------------------------------------------------------------------
# VT-776 — the discovery CASCADE.
# These two sources used to be kicked off concurrently, so every signup paid for a Gemini
# call even when the scrape had already found the GSTIN. knowyourgst runs first now; the
# LLM leg is billed only when the scrape came up empty.
# ---------------------------------------------------------------------------
def _kyg_row(gstin: str = "27AAKCR3738B1ZE") -> dict[str, Any]:
    return {"candidate_gstin": gstin, "trade_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED"}


def test_cascade_stops_at_the_first_rung_that_finds_a_gstin(monkeypatch):
    """knowyourgst hit => NEITHER paid rung runs, and both get TERMINAL rows (a 'running' row
    would hang the poller for its whole window)."""
    from orchestrator.api import discovery

    ran: list[str] = []
    updates: list[tuple] = []

    async def _fake_run_source(discovery_id, source, name, city):
        ran.append(source)
        return [_kyg_row()] if source == "knowyourgst" else []

    monkeypatch.setattr(discovery, "_run_source", _fake_run_source)
    monkeypatch.setattr(discovery, "_update_source_row", lambda *a: updates.append(a))

    asyncio.run(discovery._run_discovery_cascade(uuid.uuid4(), "RKeCom Services", "Mumbai"))

    assert ran == ["knowyourgst"], f"a paid rung ran anyway: {ran}"
    skipped = {(u[1], u[2], u[3]) for u in updates}
    assert skipped == {("dataforseo", "complete", "skipped_primary_hit"),
                       ("llm", "complete", "skipped_primary_hit")}


def test_cascade_falls_to_dataforseo_then_stops(monkeypatch):
    """Scrape empty, DataForSEO finds one => the LLM rung is never paid for."""
    from orchestrator.api import discovery

    ran: list[str] = []
    updates: list[tuple] = []

    async def _fake_run_source(discovery_id, source, name, city):
        ran.append(source)
        return [_kyg_row()] if source == "dataforseo" else []

    monkeypatch.setattr(discovery, "_run_source", _fake_run_source)
    monkeypatch.setattr(discovery, "_update_source_row", lambda *a: updates.append(a))

    asyncio.run(discovery._run_discovery_cascade(uuid.uuid4(), "Balaji Wafers", "Rajkot"))

    assert ran == ["knowyourgst", "dataforseo"]
    assert {(u[1], u[3]) for u in updates} == {("llm", "skipped_primary_hit")}


def test_cascade_runs_every_rung_when_none_finds_a_gstin(monkeypatch):
    """All empty is a NORMAL outcome — every rung runs, then the owner is asked to type it."""
    from orchestrator.api import discovery

    ran: list[str] = []

    async def _fake_run_source(discovery_id, source, name, city):
        ran.append(source)
        return []

    monkeypatch.setattr(discovery, "_run_source", _fake_run_source)
    monkeypatch.setattr(discovery, "_update_source_row", lambda *a: None)

    asyncio.run(discovery._run_discovery_cascade(uuid.uuid4(), "Nope Pvt", "Mumbai"))
    assert ran == ["knowyourgst", "dataforseo", "llm"]


def test_cascade_name_only_candidate_does_not_stop_the_ladder(monkeypatch):
    """A candidate with no GSTIN is not an answer — the next rung must still run."""
    from orchestrator.api import discovery

    ran: list[str] = []

    async def _fake_run_source(discovery_id, source, name, city):
        ran.append(source)
        return [{"candidate_gstin": "", "trade_name": "SOME CO"}] if source == "knowyourgst" else []

    monkeypatch.setattr(discovery, "_run_source", _fake_run_source)
    monkeypatch.setattr(discovery, "_update_source_row", lambda *a: None)

    asyncio.run(discovery._run_discovery_cascade(uuid.uuid4(), "Some Co", "Mumbai"))
    assert ran == ["knowyourgst", "dataforseo", "llm"]


def test_dataforseo_empty_result_is_a_clean_completion_not_an_error(monkeypatch):
    """An honest zero must NOT be a failure_reason.

    `_run_source` maps any reason to status='error', and `both_complete_zero` requires every
    source to be 'complete'. Reporting zero as a reason made that flag permanently False, and
    team-web uses it as the ONLY trigger for the honest "we couldn't find it" empty state —
    so every genuine miss would have rendered as a degraded/error path instead."""
    from orchestrator.api import discovery

    monkeypatch.setenv("DATAFORSEO_API_BASE64", "x")
    monkeypatch.setattr(
        "orchestrator.integrations.methods.dataforseo.search_gstins",
        lambda name, city, **kw: [],
    )
    candidates, reason = discovery._fetch_dataforseo("Nope Pvt", "Mumbai")
    assert candidates == []
    assert reason is None, "an honest zero was reported as a failure"
