"""VT-628 — shared LLM sampling config.

Pin ``temperature=0`` for deterministic manager behaviour: run-to-run gate
variance (the same scenario swinging ±1-2 judge points) was traced substantially
to sampling at the API default temperature of 1.0 on every LLM call. A
non-deterministic manager cannot hold a quality floor, and a floor cannot be
*measured* with a non-deterministic ruler.

FAMILY GATE — ``claude-opus-*`` models reject the ``temperature`` param with a
400 ("temperature ... not supported"), verified for BOTH opus-4-7 and opus-4-8.
So opus calls MUST omit it (they stay non-deterministic — an accepted, documented
limit; the opus specialist lanes' content variance is irreducible via temperature
and would need a model change to pin). Every non-opus call (haiku / sonnet) gets
``temperature=0``.
"""

from __future__ import annotations

from typing import Any


def sampling_kwargs(model_id: str) -> dict[str, Any]:
    """Return the sampling kwargs for an Anthropic / ChatAnthropic call.

    ``{"temperature": 0.0}`` for every non-opus model (deterministic); ``{}`` for
    opus (which 400s on the param). Spread into the call/ctor:
    ``client.messages.create(model=m, **sampling_kwargs(m), ...)`` or
    ``ChatAnthropic(model=m, **sampling_kwargs(m))``.
    """
    if "opus" in model_id.lower():
        return {}
    return {"temperature": 0.0}
