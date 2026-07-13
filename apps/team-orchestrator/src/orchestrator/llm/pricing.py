"""Migration-173 cost pricing: read ``model_pricing`` + compute per-call cost.

``model_pricing`` (migration 173) is the GLOBAL, VTR-writable price registry — USD
per MTok in/out per model, plus a per-model ``flex_multiplier`` for the batch/flex
service tier. This module is the READ + COMPUTE side of the cost ledger:

  * ``compute_cost_usd(model, service_tier, tokens_in, tokens_out) -> Decimal`` —
    the money math. NEVER crashes: an unknown model logs a WARNING naming it and
    costs 0 (a mispriced call must never break a turn; VTR seeds the row later).
  * a module-level TTL cache (~5 min) over the ``model_pricing`` table, fail-SOFT
    to a hard-coded seed mirror of migration 173's seed so costing survives a DB
    blip (the enforcement + audit paths keep working even if the registry read
    fails).

The seed mirror is kept in lock-step with migration 173's ``INSERT`` seed. VTR
tuning happens in the TABLE (the live cache picks it up within the TTL); the seed
is only the cold-start / DB-down fallback and the safety net for a model the live
registry is momentarily missing.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from decimal import Decimal

logger = logging.getLogger(__name__)

# Per-model price entry: (usd_per_mtok_in, usd_per_mtok_out, discount_multiplier,
# cached_in_multiplier).
_PriceEntry = tuple[Decimal, Decimal, Decimal, Decimal]

# Fail-soft seed mirror of the ``model_pricing`` seed across migrations 173 (Anthropic +
# OpenAI), 174 (Google Gemini), and 175 (Z.ai GLM) (READ THEM: the migrations are the
# contract). Note the per-model multipliers are NOT uniform — glm-5.2 (175) carries
# cached_in_multiplier 0.186 / discount_multiplier 1.0, unlike the 0.1 / 0.5 defaults. Seed
# prices VERIFIED 2026-07-13 from the official
# pages. Prices are VTR-tunable in the TABLE — the live cache picks changes up
# within the TTL; this mirror is only the DB-down fallback. Keep it in lock-step
# with the migration seed (the PG integration test asserts no drift). Elements:
# (in, out, discount_multiplier, cached_in_multiplier) — PER-MODEL: discount is 0.5 for
# flex/batch models but 1.0 for GLM (no batch tier); cached is 0.1 default but 0.186 for GLM.
# NOTE: sonnet-5 is INTRODUCTORY $2/$10 through 2026-08-31, then $3/$15 from
# 2026-09-01 (VTR bumps the row; mirror it here then).
_SEED_PRICING: dict[str, _PriceEntry] = {
    "claude-sonnet-5": (Decimal("2.0000"), Decimal("10.0000"), Decimal("0.5"), Decimal("0.1")),
    "claude-opus-4-8": (Decimal("5.0000"), Decimal("25.0000"), Decimal("0.5"), Decimal("0.1")),
    "claude-haiku-4-5-20251001": (Decimal("1.0000"), Decimal("5.0000"), Decimal("0.5"), Decimal("0.1")),
    "claude-haiku-4-5": (Decimal("1.0000"), Decimal("5.0000"), Decimal("0.5"), Decimal("0.1")),
    "gpt-5.6-sol": (Decimal("5.0000"), Decimal("30.0000"), Decimal("0.5"), Decimal("0.1")),
    "gpt-5.6-terra": (Decimal("2.5000"), Decimal("15.0000"), Decimal("0.5"), Decimal("0.1")),
    "gpt-5.6-luna": (Decimal("1.0000"), Decimal("6.0000"), Decimal("0.5"), Decimal("0.1")),
    # Migration 174 (Gemini 3.5 / 3.1). gemini-3.1-pro-preview is recorded at the <=200k-context
    # rate ($2/$12); >200k tiers ($4/$18) are out of scope (our contexts stay under 200k).
    "gemini-3.5-flash": (Decimal("1.5000"), Decimal("9.0000"), Decimal("0.5"), Decimal("0.1")),
    "gemini-3.1-flash-lite": (Decimal("0.2500"), Decimal("1.5000"), Decimal("0.5"), Decimal("0.1")),
    "gemini-3.1-pro-preview": (Decimal("2.0000"), Decimal("12.0000"), Decimal("0.5"), Decimal("0.1")),
    # Migration 175 (Z.ai GLM). NOTE: glm-5.2's multipliers DIFFER from the 173/174 defaults —
    # cached_in_multiplier 0.186 (cache-read $0.26/$1.40, NOT 0.1) and discount_multiplier 1.0
    # (GLM publishes no batch/flex tier, so a mistaken service_tier='batch' must not under-cost).
    "glm-5.2": (Decimal("1.4000"), Decimal("4.4000"), Decimal("1.0"), Decimal("0.186")),
    # Migration 176 (xAI Grok). grok-4.5 = flagship, no batch tier -> discount 1.0. grok-4.3 =
    # cheaper, 20% batch -> discount 0.8 (NOT the 0.5 default). xAI bills cached tokens at standard
    # rate -> cached_in_multiplier 1.0 (NOT 0.1). Keep in lock-step with migration 176's seed.
    "grok-4.5": (Decimal("2.0000"), Decimal("6.0000"), Decimal("1.0"), Decimal("1.0")),
    "grok-4.3": (Decimal("1.2500"), Decimal("2.5000"), Decimal("0.8"), Decimal("1.0")),
}

# Service tiers that get the ``discount_multiplier`` (migration 173: OpenAI Flex ==
# Batch == 50%, Anthropic Batches API == 50%, both input + output).
_DISCOUNTED_TIERS = frozenset({"flex", "batch"})

_CACHE_TTL_SECONDS = 300.0  # ~5 min
_MTOK = Decimal(1_000_000)
_PER_SEARCH = Decimal(1000)  # search_tool_pricing is priced per 1000 invocations

# Migration-176 search-tool cost: server-side web/X search is billed PER INVOCATION, separate from
# tokens. VTR-tunable in the ``search_tool_pricing`` table; this seed mirror is the DB-down /
# missing-row fallback (same fail-soft posture as _SEED_PRICING). Key: (provider, tool) ->
# usd_per_1000. VERIFIED where public (anthropic web $10, xai web/x $5); PLACEHOLDER for openai/google
# (no clear public per-search rate — VTR corrects the row). Keep in lock-step with migration 176.
_SEED_SEARCH_PRICING: dict[tuple[str, str], Decimal] = {
    ("anthropic", "web_search"): Decimal("10.0000"),
    ("xai", "web_search"): Decimal("5.0000"),
    ("xai", "x_search"): Decimal("5.0000"),
    ("openai", "web_search"): Decimal("10.0000"),
    ("google", "web_search"): Decimal("35.0000"),
}

# Anthropic returns fully-qualified, date-suffixed ids (`claude-haiku-4-5-20251001`)
# in ``response.model`` while the registry may key only the base alias. Strip a
# trailing `-YYYYMMDD` for a second lookup so a dated id prices off its base row
# instead of silently zeroing (same rationale as agent/cost.py's normalization).
# The event still RECORDS the raw model; only the price LOOKUP is normalized.
_MODEL_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")

_lock = threading.Lock()
_cache: dict[str, _PriceEntry] | None = None
_cache_loaded_at: float = 0.0  # 0.0 => never loaded from the live table (retry)

_search_lock = threading.Lock()
_search_cache: dict[tuple[str, str], Decimal] | None = None
_search_cache_loaded_at: float = 0.0  # 0.0 => never loaded from the live table (retry)


def _fetch_from_db() -> dict[str, _PriceEntry]:
    """Read the full ``model_pricing`` registry via the privileged pool.

    ``model_pricing`` is a GLOBAL registry (RLS policy ``USING (true)``), so it is
    read on the service/privileged pool connection — no tenant GUC. Imports are
    lazy so this module loads with no DBOS/graph dependency (dep-less smoke safe).
    """
    from orchestrator.graph import get_pool

    out: dict[str, _PriceEntry] = {}
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT model, usd_per_mtok_in, usd_per_mtok_out, "
            "       discount_multiplier, cached_in_multiplier "
            "FROM model_pricing"
        ).fetchall()
    for row in rows:
        model = row["model"] if isinstance(row, dict) else row[0]
        pin = row["usd_per_mtok_in"] if isinstance(row, dict) else row[1]
        pout = row["usd_per_mtok_out"] if isinstance(row, dict) else row[2]
        disc = row["discount_multiplier"] if isinstance(row, dict) else row[3]
        cached = row["cached_in_multiplier"] if isinstance(row, dict) else row[4]
        out[str(model)] = (
            Decimal(str(pin)), Decimal(str(pout)), Decimal(str(disc)), Decimal(str(cached))
        )
    return out


def _pricing() -> dict[str, _PriceEntry]:
    """Return the current price table — live cache if fresh, else refresh.

    Fresh cache (< TTL) is returned as-is. On a stale/absent cache we re-read the
    table; a read failure (DB blip) falls SOFT to the last good cache, or to the
    seed mirror if there was never a good load. Both success and failure stamp the
    load time, so an outage is negatively-cached for the TTL — no per-call DB
    hammering or traceback spam; recovery is picked up on the next TTL boundary.
    """
    global _cache, _cache_loaded_at
    now = time.monotonic()
    with _lock:
        if _cache is not None and (now - _cache_loaded_at) < _CACHE_TTL_SECONDS:
            return _cache
        try:
            loaded = _fetch_from_db()
            if loaded:
                _cache = loaded  # empty table (unexpected) → keep last good / seed below
        except Exception:  # noqa: BLE001 — costing must survive a registry read blip
            logger.warning(
                "173 model_pricing read failed; using %s",
                "last-good cache" if _cache else "seed mirror",
                exc_info=True,
            )
        if _cache is None:
            _cache = dict(_SEED_PRICING)
        _cache_loaded_at = now
        return _cache


def _lookup(table: dict[str, _PriceEntry], model: str) -> _PriceEntry | None:
    """Exact-match ``model`` in ``table``, else retry with the date suffix stripped."""
    entry = table.get(model)
    if entry is not None:
        return entry
    base = _MODEL_DATE_SUFFIX_RE.sub("", model)
    if base != model:
        return table.get(base)
    return None


def compute_cost_usd(
    model: str,
    service_tier: str,
    tokens_in: int,
    tokens_out: int,
    cached_tokens_in: int = 0,
) -> Decimal:
    """Compute the USD cost of one LLM call at the registry's recorded price.

    cost = (tokens_in * usd_in
            + cached_tokens_in * usd_in * cached_in_multiplier
            + tokens_out * usd_out) / 1e6,
    then × ``discount_multiplier`` when ``service_tier`` is a discounted tier
    (flex/batch — both providers price those at 0.5×).

    ``tokens_in`` is the FULL-PRICE (uncached) input count; ``cached_tokens_in`` is
    the cache-READ input count, priced at ``cached_in_multiplier`` (~0.1× — both
    providers bill cache hits at 10% of input). The caller splits total input into
    these two per its provider's usage shape; ``cached_tokens_in=0`` (the default)
    prices all input at full rate — the safe, unchanged behavior for cache-unaware
    callers. Cache-CREATION (write) tokens are out of scope here (no column, and the
    directive covers cache reads only) — they fall into ``tokens_in`` at full rate.

    Live registry first, then the seed mirror (a model the live table momentarily
    lacks is still priced if a known seed model). A truly unknown model logs a
    WARNING naming it and costs ``Decimal('0')`` — NEVER a crash.
    """
    entry = _lookup(_pricing(), model) or _lookup(_SEED_PRICING, model)
    if entry is None:
        logger.warning("173 compute_cost_usd: unknown model %r — costing 0 (seed the price row)", model)
        return Decimal("0")
    usd_in, usd_out, discount, cached_mult = entry
    cost = (
        Decimal(int(tokens_in or 0)) * usd_in
        + Decimal(int(cached_tokens_in or 0)) * usd_in * cached_mult
        + Decimal(int(tokens_out or 0)) * usd_out
    ) / _MTOK
    if (service_tier or "").strip().lower() in _DISCOUNTED_TIERS:
        cost = cost * discount
    return cost


# ---------------------------------------------------------------------------
# Migration-176 search-tool cost (server-side web/X search, billed per 1000 invocations)
# ---------------------------------------------------------------------------
def _fetch_search_from_db() -> dict[tuple[str, str], Decimal]:
    """Read the full ``search_tool_pricing`` registry via the privileged pool.

    Global registry (RLS ``USING (true)``) → service/privileged pool, no tenant GUC. Lazy imports
    keep this module dep-less at load (dep-less smoke safe), same as ``_fetch_from_db``.
    """
    from orchestrator.graph import get_pool

    out: dict[tuple[str, str], Decimal] = {}
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT provider, tool, usd_per_1000 FROM search_tool_pricing"
        ).fetchall()
    for row in rows:
        provider = row["provider"] if isinstance(row, dict) else row[0]
        tool = row["tool"] if isinstance(row, dict) else row[1]
        price = row["usd_per_1000"] if isinstance(row, dict) else row[2]
        out[(str(provider), str(tool))] = Decimal(str(price))
    return out


def _search_pricing() -> dict[tuple[str, str], Decimal]:
    """Current search-tool price table — live cache if fresh, else refresh; fail-SOFT to the last
    good cache, or the seed mirror if never loaded (mirrors ``_pricing``'s negative-caching)."""
    global _search_cache, _search_cache_loaded_at
    now = time.monotonic()
    with _search_lock:
        if _search_cache is not None and (now - _search_cache_loaded_at) < _CACHE_TTL_SECONDS:
            return _search_cache
        try:
            loaded = _fetch_search_from_db()
            if loaded:
                _search_cache = loaded  # empty table (unexpected) → keep last good / seed below
        except Exception:  # noqa: BLE001 — costing must survive a registry read blip
            logger.warning(
                "176 search_tool_pricing read failed; using %s",
                "last-good cache" if _search_cache else "seed mirror",
                exc_info=True,
            )
        if _search_cache is None:
            _search_cache = dict(_SEED_SEARCH_PRICING)
        _search_cache_loaded_at = now
        return _search_cache


def compute_search_cost(provider: str, tool: str, count: int) -> Decimal:
    """USD cost of ``count`` server-side ``tool`` (web_search | x_search) invocations for ``provider``.

    cost = usd_per_1000 * count / 1000, from the live ``search_tool_pricing`` registry (then the seed
    mirror for a row the live table momentarily lacks). ``count`` <= 0 costs 0 (no lookup). A truly
    unknown (provider, tool) logs a WARNING naming it and costs ``Decimal('0')`` — NEVER a crash
    (a mispriced search must never break a turn; VTR seeds the row later).
    """
    n = int(count or 0)
    if n <= 0:
        return Decimal("0")
    key = (str(provider), str(tool))
    price = _search_pricing().get(key) or _SEED_SEARCH_PRICING.get(key)
    if price is None:
        logger.warning(
            "176 compute_search_cost: unknown (provider, tool) %r — costing 0 (seed the price row)",
            key,
        )
        return Decimal("0")
    return price * Decimal(n) / _PER_SEARCH


__all__ = ["compute_cost_usd", "compute_search_cost"]
