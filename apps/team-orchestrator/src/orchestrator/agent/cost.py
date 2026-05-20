"""Token-to-paise cost attribution for the sales_recovery agent (VT-32).

Phase 1 method (deterministic table; CL-242):

  cost_paise = round(
      (input_tokens  * paise_per_M_in  +
       output_tokens * paise_per_M_out) / 1_000_000
  )

The per-million-token rates are Anthropic's published list prices,
converted at a fixed USD→INR rate (₹85 / USD). The conversion is a
*budget-attribution* number, not a billing number — Anthropic invoices
in USD on cache-aware totals. A later subtask wires live billing
reconciliation (out of scope here).

Rate refresh: when Anthropic changes a list price OR when the FX
assumption drifts by more than ~5% from spot, update ``RATES``. Each
entry stays an int (paise per million tokens) so cost math is integer
arithmetic end-to-end — no float drift, no rounding surprises.
"""

from __future__ import annotations

from dataclasses import dataclass

# Fixed USD→INR for Phase 1 conversion. SINGLE source of truth — the
# rate-table docs in docs/team/sr-agent-skeleton.md reference this
# constant by name. Update HERE first when refreshing; the doc carries
# the refresh policy + the as-of date.
#
# As-of: 2026-05-20 (FX assumption set when VT-32 landed).
_USD_TO_INR = 85
_PAISE_PER_INR = 100  # 100 paise = 1 INR


def _paise_per_million(usd_per_million: float) -> int:
    """USD/M → paise/M. Pure integer once we apply the FX + paise scaling."""
    return round(usd_per_million * _USD_TO_INR * _PAISE_PER_INR)


@dataclass(frozen=True)
class _Rate:
    """Per-model rate. Both fields are paise per *million* tokens (ints)."""

    input_paise_per_million: int
    output_paise_per_million: int


# Phase 1 rates. Source: Anthropic public pricing as of 2026-05-04.
RATES: dict[str, _Rate] = {
    # Opus 4.7 — $15 / M input, $75 / M output.
    "claude-opus-4-7": _Rate(
        input_paise_per_million=_paise_per_million(15.0),
        output_paise_per_million=_paise_per_million(75.0),
    ),
    # Haiku 4.5 — $1 / M input, $5 / M output.
    "claude-haiku-4-5": _Rate(
        input_paise_per_million=_paise_per_million(1.0),
        output_paise_per_million=_paise_per_million(5.0),
    ),
}


def compute_cost_paise(*, model: str, input_tokens: int, output_tokens: int) -> int:
    """Return cost in paise for the given token usage on ``model``.

    Raises ``KeyError`` if ``model`` is not in ``RATES`` — that surfaces an
    unconfigured model immediately rather than silently zeroing the cost
    (which would corrupt budget telemetry).
    """
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError(
            f"token counts must be non-negative; got input={input_tokens}, "
            f"output={output_tokens}"
        )
    rate = RATES[model]
    total = (
        input_tokens * rate.input_paise_per_million
        + output_tokens * rate.output_paise_per_million
    )
    return round(total / 1_000_000)


__all__ = ["RATES", "compute_cost_paise"]
