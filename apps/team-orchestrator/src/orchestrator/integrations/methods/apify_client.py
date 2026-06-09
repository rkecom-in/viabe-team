"""VT-364 — shared Apify run client: async start → poll → fetch (replaces the blocking run-sync).

The old path hit ``/run-sync-get-dataset-items`` with a single ``timeout=120.0`` httpx POST — a held
connection that ReadTimeouts on slow actors (Zomato >120s at 100 reviews) → fail-soft EMPTY context.
This client instead:
  1. START   POST /v2/acts/{actor}/runs        → run id + defaultDatasetId (returns immediately).
  2. POLL    GET  /v2/actor-runs/{runId}        → backoff until a terminal status, under a budget cap.
  3. FETCH   GET  /v2/datasets/{dsId}/items     → on SUCCEEDED.
Each HTTP call has a SHORT timeout; the WAIT is the poll loop, not a 120s held connection. Ingestion
is a background DBOS workflow (not a request path), so a multi-minute poll is fine.

Fail-soft (CL-390 unchanged): a non-SUCCEEDED terminal state / budget-exceeded → ``[]`` (the caller
degrades to empty context). Genuine HTTP/network errors raise (the ingest_* callers already catch +
degrade). ``time.sleep`` here is application runtime, not a Workflow-script (the sleep/time ban is a
Workflow-script restriction only).
"""

from __future__ import annotations

import time
from typing import Any, Callable

_BASE = "https://api.apify.com/v2"
_HTTP_TIMEOUT = 30.0  # per-call (start/poll/fetch are fast); NOT the overall wait
_TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMING-OUT"}

# Injectable transport for tests: (method, url, params, json) -> parsed JSON (dict for run/poll, a
# list for the dataset-items fetch).
RequestFn = Callable[[str, str, dict[str, Any], dict[str, Any] | None], Any]


def _default_request(
    method: str, url: str, params: dict[str, Any], json: dict[str, Any] | None
) -> Any:
    import httpx

    resp = httpx.request(method, url, params=params, json=json, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def run_actor(
    actor: str,
    run_input: dict[str, Any],
    token: str,
    *,
    budget_s: float = 480.0,
    poll_s: float = 4.0,
    request_fn: RequestFn | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> list[dict[str, Any]]:
    """Start the actor, poll until terminal (≤ budget_s), fetch its dataset items. Returns [] on any
    non-success terminal state or budget exhaustion. ``actor`` is the ``user~name`` Apify path id."""
    req = request_fn or _default_request
    params = {"token": token}

    start = req("POST", f"{_BASE}/acts/{actor}/runs", params, run_input)
    data = start.get("data", start) if isinstance(start, dict) else {}
    run_id = data.get("id")
    ds_id = data.get("defaultDatasetId")
    status = data.get("status")
    if not run_id:
        return []

    deadline = monotonic_fn() + budget_s
    while status not in _TERMINAL:
        if monotonic_fn() >= deadline:
            return []  # budget exceeded — fail-soft (the run keeps going Apify-side; we just degrade)
        sleep_fn(poll_s)
        poll = req("GET", f"{_BASE}/actor-runs/{run_id}", params, None)
        d = poll.get("data", poll) if isinstance(poll, dict) else {}
        status = d.get("status")
        ds_id = d.get("defaultDatasetId") or ds_id

    if status != "SUCCEEDED" or not ds_id:
        return []

    items = req("GET", f"{_BASE}/datasets/{ds_id}/items", {"token": token, "clean": "true"}, None)
    # the dataset-items endpoint returns a JSON ARRAY; _default_request wraps non-dict via dict() —
    # so call it raw here through the same transport but tolerate a list.
    if isinstance(items, list):
        return items
    inner = items.get("items") if isinstance(items, dict) else None
    return inner if isinstance(inner, list) else []
