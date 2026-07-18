"""canaries/batch_judge.py — #84 shared Anthropic Message Batches API helper for the OFFLINE
measurement judges (``transcript_judge.py`` / ``tier_rescore.py``). 50% cost off the x3 measurement
pipeline's judge calls versus the serial ``client.messages.create`` loop those two judges otherwise
run. PRODUCT / SERVING paths never import or call this module — it exists ONLY for offline judge
cost control (Fazal-granted #84).

No dependency on ``anthropic`` at import time (mirrors ``transcript_judge.py`` / ``tier_rescore.py``'s
own dep-less-at-import-time posture): every function here takes an already-constructed ``client`` and
treats it duck-typed (``client.messages.batches.create/.retrieve/.results``) — it never imports or
constructs the SDK client itself. Verified against the installed SDK in this worktree
(``anthropic==0.103.1``): ``client.messages.batches.create(requests=[...])`` (plain dicts — the SDK's
``Request``/``params`` shapes are ``TypedDict``s, not classes to instantiate) returns a
``MessageBatch`` with ``.id`` + ``.processing_status`` (``"in_progress" | "canceling" | "ended"``);
``client.messages.batches.retrieve(id)`` re-fetches the same shape for polling;
``client.messages.batches.results(id)`` returns an iterator of ``MessageBatchIndividualResponse``
(``.custom_id`` + ``.result``, where ``.result.type`` is one of ``succeeded`` / ``errored`` /
``canceled`` / ``expired`` — results arrive in ANY order, never keyed by position).

Fail-not-skip (Rule #15 posture, same as both judges): an errored/expired/canceled item, or a
custom_id the API never returns in ``results()`` at all, surfaces as an explicit
``BatchItemResult.error`` string — never a silently-dropped key. A batch that doesn't reach
``processing_status == "ended"`` within the timeout raises ``BatchTimeoutError`` rather than
returning a partial/guessed result.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

# Poll interval and overall timeout per the #84 brief: ~10s poll, ~20 min overall ceiling. Message
# Batches typically complete within an hour and the Anthropic API allows up to 24h — 20 minutes is a
# measurement-pipeline-appropriate ceiling, not an API limit; callers needing longer can override.
DEFAULT_POLL_INTERVAL_S = 10.0
DEFAULT_TIMEOUT_S = 20 * 60.0


class BatchTimeoutError(RuntimeError):
    """The batch did not reach ``processing_status == "ended"`` within the given timeout. Raised
    rather than returning a partial/in-progress batch's results — fail-not-skip: the caller must
    decide how to handle an incomplete run (retry, extend the timeout, or surface the failure),
    never silently treated as "no results"."""


@dataclass(frozen=True)
class BatchJudgeItem:
    """One judge call to submit as part of a Message Batch. Mirrors the positional arguments a
    serial ``client.messages.create(model=..., max_tokens=..., system=..., messages=[...])`` call
    would take — ``custom_id`` is the caller's own correlation key (results arrive in any order)."""

    custom_id: str
    system: str
    user_content: str
    model: str
    max_tokens: int


@dataclass(frozen=True)
class BatchItemResult:
    """The outcome of one batch item. Exactly one of ``text``/``error`` is set — ``ok`` is the
    discriminator callers should check before touching ``text``."""

    custom_id: str
    text: str | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


def _build_request(item: BatchJudgeItem) -> dict[str, Any]:
    """Build one Message Batches request entry as a plain dict shaped exactly like the SDK's
    ``batch_create_params.Request`` TypedDict (``custom_id`` + ``params``). Plain dicts are the
    correct, idiomatic construction here — ``Request`` and ``MessageCreateParamsNonStreaming`` are
    ``TypedDict``s in the installed SDK (``anthropic==0.103.1``), not classes requiring
    instantiation; a dict of this exact shape is what the SDK itself expects and validates.

    Temperature is deliberately never set here (mirrors both judges' own callers) — it 400s on every
    capable model this project uses (sonnet-5, opus-4-7/4-8); only haiku accepts it, and this helper
    has no opinion on which model a caller passes.
    """
    return {
        "custom_id": item.custom_id,
        "params": {
            "model": item.model,
            "max_tokens": item.max_tokens,
            "system": item.system,
            "messages": [{"role": "user", "content": item.user_content}],
        },
    }


def _extract_text(message: Any) -> str:
    """Pull every text block's ``.text`` off a ``succeeded`` result's ``.message`` — the exact same
    block-concatenation ``transcript_judge.judge_batch`` / ``tier_rescore._call_judge_once`` already
    do for a plain (non-batch) ``messages.create`` response, since a batch item's ``succeeded``
    result wraps an ordinary ``Message`` object with the same ``.content`` shape."""
    text = ""
    for block in getattr(message, "content", []) or []:
        block_text = getattr(block, "text", None)
        if isinstance(block_text, str):
            text += block_text
    return text


def _describe_error(error_response: Any) -> str:
    """Render an errored result's nested error object into a one-line string. The SDK shape is
    ``MessageBatchErroredResult.error`` -> ``ErrorResponse.error`` -> a typed error object with
    ``.type``/``.message`` (e.g. ``InvalidRequestError``) — duck-typed here (``getattr`` chain) so a
    plain test double with the same shape works without importing any SDK type."""
    inner = getattr(error_response, "error", None)
    message = getattr(inner, "message", None)
    err_type = getattr(inner, "type", None)
    if message is not None:
        return f"{err_type or 'error'}: {message}"
    return str(error_response)


def _result_for_response(resp: Any) -> BatchItemResult:
    """Map one ``MessageBatchIndividualResponse`` (any of the 4 ``result.type`` variants) into a
    ``BatchItemResult``. Fail-not-skip: ``errored``/``expired``/``canceled`` all surface as an
    explicit ``.error`` string, never as a silently-omitted custom_id."""
    custom_id = str(resp.custom_id)
    result = resp.result
    result_type = getattr(result, "type", None)
    if result_type == "succeeded":
        return BatchItemResult(custom_id=custom_id, text=_extract_text(result.message), error=None)
    if result_type == "errored":
        return BatchItemResult(
            custom_id=custom_id, text=None, error=_describe_error(getattr(result, "error", None)),
        )
    if result_type in ("expired", "canceled"):
        return BatchItemResult(custom_id=custom_id, text=None, error=f"batch item {result_type}")
    return BatchItemResult(
        custom_id=custom_id, text=None, error=f"unrecognized batch result type: {result_type!r}",
    )


def submit_batch(items: Iterable[BatchJudgeItem], *, client: Any) -> str:
    """Submit ONE Message Batches request for every item in ``items`` and return the batch id.
    Raises ``ValueError`` if given no items — an empty batch is a caller bug, never silently a no-op.
    """
    request_items = list(items)
    if not request_items:
        raise ValueError("submit_batch: no items given")
    requests = [_build_request(item) for item in request_items]
    batch = client.messages.batches.create(requests=requests)
    return batch.id


def wait_for_batch(
    batch_id: str,
    *,
    client: Any,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> Any:
    """Poll ``client.messages.batches.retrieve(batch_id)`` until ``processing_status == "ended"``.
    Raises ``BatchTimeoutError`` (fail-not-skip) if ``timeout_s`` elapses first — never returns a
    partial/in-progress batch. ``sleep_fn``/``clock_fn`` are injectable so tests never actually
    sleep or depend on wall-clock time.
    """
    deadline = clock_fn() + timeout_s
    batch = client.messages.batches.retrieve(batch_id)
    while batch.processing_status != "ended":
        if clock_fn() >= deadline:
            raise BatchTimeoutError(
                f"batch {batch_id!r} did not reach processing_status='ended' within {timeout_s}s "
                f"(last status: {batch.processing_status!r})"
            )
        sleep_fn(poll_interval_s)
        batch = client.messages.batches.retrieve(batch_id)
    return batch


def collect_results(
    batch_id: str, expected_custom_ids: Iterable[str], *, client: Any,
) -> dict[str, BatchItemResult]:
    """Fetch results for an ENDED batch and map every custom_id to a ``BatchItemResult``.
    Fail-not-skip: any ``expected_custom_ids`` entry missing from the results stream gets an
    explicit "missing from batch results" error entry rather than being silently absent from the
    returned dict — a caller that only iterates ``results()`` output directly could otherwise lose a
    scenario without ever seeing an error for it.
    """
    expected = list(expected_custom_ids)
    results: dict[str, BatchItemResult] = {}
    for resp in client.messages.batches.results(batch_id):
        item_result = _result_for_response(resp)
        results[item_result.custom_id] = item_result
    for custom_id in expected:
        if custom_id not in results:
            results[custom_id] = BatchItemResult(
                custom_id=custom_id, text=None,
                error=f"missing from batch {batch_id!r} results (API never returned this custom_id)",
            )
    return results


def run_batch_judge(
    items: list[BatchJudgeItem],
    *,
    client: Any,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> dict[str, BatchItemResult]:
    """The end-to-end helper: submit -> poll -> collect, as ONE Anthropic Messages Batches request
    for the whole ``items`` list — the entire point of this module (50% cost vs. the serial
    per-item ``messages.create`` loop the offline judges otherwise run). Returns a dict keyed by
    every item's ``custom_id``; fail-not-skip end to end (see ``wait_for_batch`` / ``collect_results``
    docstrings) — never silently drops or guesses a result.
    """
    batch_id = submit_batch(items, client=client)
    wait_for_batch(
        batch_id, client=client, poll_interval_s=poll_interval_s, timeout_s=timeout_s,
        sleep_fn=sleep_fn, clock_fn=clock_fn,
    )
    return collect_results(batch_id, [item.custom_id for item in items], client=client)
