"""Migration-173 pricing math + fail-soft seed fallback.

Dep-less: ``orchestrator.llm.pricing`` imports only stdlib at load; the DB read is
lazy. Tests monkeypatch ``_pricing`` / ``_fetch_from_db`` so no live DB is touched.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

# The orchestrator.llm package __init__ eagerly imports provider.py (langchain_core),
# so importing ANY submodule transitively needs it — skip cleanly in the dep-less smoke.
pytest.importorskip("langchain_core")

from orchestrator.llm import pricing as pricing_mod  # noqa: E402
from orchestrator.llm.pricing import compute_cost_usd  # noqa: E402

# (usd_in, usd_out, discount_multiplier, cached_in_multiplier)
_SONNET = {"claude-sonnet-5": (Decimal("3.0000"), Decimal("15.0000"), Decimal("0.5"), Decimal("0.1"))}


def _fix_table(monkeypatch, table):
    monkeypatch.setattr(pricing_mod, "_pricing", lambda: table)


def test_standard_cost_is_tokens_times_rate(monkeypatch):
    _fix_table(monkeypatch, _SONNET)
    # 1M in * $3/M + 1M out * $15/M = 3 + 15 = 18.
    assert compute_cost_usd("claude-sonnet-5", "standard", 1_000_000, 1_000_000) == Decimal("18")


def test_asymmetric_token_split(monkeypatch):
    _fix_table(monkeypatch, _SONNET)
    # 200k in * $3/M + 100k out * $15/M = 0.6 + 1.5 = 2.1.
    assert compute_cost_usd("claude-sonnet-5", "standard", 200_000, 100_000) == Decimal("2.1")


def test_discounted_tiers_apply_multiplier(monkeypatch):
    _fix_table(monkeypatch, _SONNET)
    # standard 18 * discount 0.5 = 9, for BOTH discounted tiers (flex + batch).
    assert compute_cost_usd("claude-sonnet-5", "flex", 1_000_000, 1_000_000) == Decimal("9")
    assert compute_cost_usd("claude-sonnet-5", "batch", 1_000_000, 1_000_000) == Decimal("9")
    # tier is case-insensitive + whitespace-tolerant.
    assert compute_cost_usd("claude-sonnet-5", "  FLEX ", 1_000_000, 1_000_000) == Decimal("9")
    # a non-discounted tier is full price.
    assert compute_cost_usd("claude-sonnet-5", "standard", 1_000_000, 1_000_000) == Decimal("18")


def test_unknown_model_costs_zero_and_warns(monkeypatch, caplog):
    _fix_table(monkeypatch, {})  # empty live table; model also absent from seed
    with caplog.at_level("WARNING"):
        cost = compute_cost_usd("totally-made-up-model", "standard", 5000, 5000)
    assert cost == Decimal("0")
    assert any("totally-made-up-model" in r.getMessage() for r in caplog.records)


def test_zero_tokens_costs_zero(monkeypatch):
    _fix_table(monkeypatch, _SONNET)
    assert compute_cost_usd("claude-sonnet-5", "standard", 0, 0) == Decimal("0")


def test_cached_input_priced_at_cache_multiplier(monkeypatch):
    _fix_table(monkeypatch, _SONNET)
    # 1M full-price in ($3) + 1M cache-read in ($3 * 0.1 = $0.3) + 0 out = 3.3.
    assert compute_cost_usd("claude-sonnet-5", "standard", 1_000_000, 0, 1_000_000) == Decimal("3.3")
    # cached defaults to 0 → unchanged full-price behavior.
    assert compute_cost_usd("claude-sonnet-5", "standard", 1_000_000, 0) == Decimal("3")


def test_cached_input_stacks_with_flex_discount(monkeypatch):
    _fix_table(monkeypatch, _SONNET)
    # (1M*$3 + 1M*$3*0.1 + 0) = 3.3, then flex 0.5 = 1.65.
    assert compute_cost_usd("claude-sonnet-5", "flex", 1_000_000, 0, 1_000_000) == Decimal("1.65")


def test_dated_model_prices_off_base_alias(monkeypatch):
    # Live table keys only the base alias; a date-suffixed id must still price.
    _fix_table(monkeypatch, _SONNET)
    assert compute_cost_usd("claude-sonnet-5-20250101", "standard", 1_000_000, 0) == Decimal("3")


def test_falls_back_to_seed_when_live_table_lacks_model(monkeypatch):
    # Live table empty, but a known SEED model must still price (DB-blip safety net).
    _fix_table(monkeypatch, {})
    # opus-4.8 seed = $5/M input → 1M in = $5.
    assert compute_cost_usd("claude-opus-4-8", "standard", 1_000_000, 0) == Decimal("5")


def test_pricing_failsoft_to_seed_on_db_error(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(pricing_mod, "_fetch_from_db", _boom)
    monkeypatch.setattr(pricing_mod, "_cache", None, raising=False)
    monkeypatch.setattr(pricing_mod, "_cache_loaded_at", 0.0, raising=False)
    table = pricing_mod._pricing()
    # Seed mirror carries every migration-173 seed model.
    for model in (
        "claude-sonnet-5",
        "claude-opus-4-8",
        "claude-haiku-4-5",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ):
        assert model in table


def test_seed_mirror_matches_migration_173_seed():
    # Guard against seed drift from the migration contract (spot-check the rows).
    # The PG integration test asserts the FULL mirror-vs-DB equality authoritatively.
    seed = pricing_mod._SEED_PRICING
    assert seed["claude-sonnet-5"][:2] == (Decimal("2.0000"), Decimal("10.0000"))
    assert seed["claude-opus-4-8"][:2] == (Decimal("5.0000"), Decimal("25.0000"))
    assert seed["gpt-5.6-sol"][:2] == (Decimal("5.0000"), Decimal("30.0000"))
    # Column defaults for every row: discount_multiplier 0.5, cached_in_multiplier 0.1.
    assert all(entry[2] == Decimal("0.5") for entry in seed.values())
    assert all(entry[3] == Decimal("0.1") for entry in seed.values())
