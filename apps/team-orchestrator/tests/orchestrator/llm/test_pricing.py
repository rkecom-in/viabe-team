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


# --------------------------------------------------------------------------- gemini (migration 174)
def test_gemini_flash_standard_and_flex(monkeypatch):
    # gemini-3.5-flash seed = $1.50/M in, $9.00/M out (migration 174).
    _fix_table(monkeypatch, {})  # seed-mirror path
    # 1M in * $1.50 + 1M out * $9.00 = 10.5 standard.
    assert compute_cost_usd("gemini-3.5-flash", "standard", 1_000_000, 1_000_000) == Decimal("10.5")
    # flex 0.5x -> 5.25.
    assert compute_cost_usd("gemini-3.5-flash", "flex", 1_000_000, 1_000_000) == Decimal("5.25")


def test_gemini_flash_lite_and_pro_preview(monkeypatch):
    _fix_table(monkeypatch, {})  # seed-mirror path
    # gemini-3.1-flash-lite = $0.25/M in, $1.50/M out: 0.25 + 1.5 = 1.75 standard.
    assert compute_cost_usd("gemini-3.1-flash-lite", "standard", 1_000_000, 1_000_000) == Decimal("1.75")
    # gemini-3.1-pro-preview (<=200k rate) = $2/M in, $12/M out: 2 + 12 = 14 standard; batch 0.5x = 7.
    assert compute_cost_usd("gemini-3.1-pro-preview", "standard", 1_000_000, 1_000_000) == Decimal("14")
    assert compute_cost_usd("gemini-3.1-pro-preview", "batch", 1_000_000, 1_000_000) == Decimal("7")


# --------------------------------------------------------------------------- glm (migration 175)
def test_glm_standard_and_cache_and_no_discount(monkeypatch):
    # glm-5.2 seed = $1.40/M in, $4.40/M out; cached_in_multiplier 0.186; discount_multiplier 1.0.
    _fix_table(monkeypatch, {})  # seed-mirror path
    # standard: 1M in * $1.40 + 1M out * $4.40 = 5.8.
    assert compute_cost_usd("glm-5.2", "standard", 1_000_000, 1_000_000) == Decimal("5.8")
    # cache-read priced at 0.186x: 1M full in ($1.40) + 1M cache-read in ($1.40 * 0.186 = $0.2604) = 1.6604.
    assert compute_cost_usd("glm-5.2", "standard", 1_000_000, 0, 1_000_000) == Decimal("1.6604")
    # discount_multiplier is 1.0 -> a (mistaken) batch/flex tier does NOT under-cost: still 5.8.
    assert compute_cost_usd("glm-5.2", "batch", 1_000_000, 1_000_000) == Decimal("5.8")
    assert compute_cost_usd("glm-5.2", "flex", 1_000_000, 1_000_000) == Decimal("5.8")


def test_pricing_failsoft_to_seed_on_db_error(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(pricing_mod, "_fetch_from_db", _boom)
    monkeypatch.setattr(pricing_mod, "_cache", None, raising=False)
    monkeypatch.setattr(pricing_mod, "_cache_loaded_at", 0.0, raising=False)
    table = pricing_mod._pricing()
    # Seed mirror carries every migration-173 + migration-174 seed model.
    for model in (
        "claude-sonnet-5",
        "claude-opus-4-8",
        "claude-haiku-4-5",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-3.1-pro-preview",
        "glm-5.2",
    ):
        assert model in table


def test_seed_mirror_matches_migration_seed():
    # Guard against seed drift from the migration contract (spot-check the rows across 173 + 174).
    # The PG integration test asserts the FULL mirror-vs-DB equality authoritatively.
    seed = pricing_mod._SEED_PRICING
    assert seed["claude-sonnet-5"][:2] == (Decimal("2.0000"), Decimal("10.0000"))
    assert seed["claude-opus-4-8"][:2] == (Decimal("5.0000"), Decimal("25.0000"))
    assert seed["gpt-5.6-sol"][:2] == (Decimal("5.0000"), Decimal("30.0000"))
    # Migration 174 (Gemini) rows.
    assert seed["gemini-3.5-flash"][:2] == (Decimal("1.5000"), Decimal("9.0000"))
    assert seed["gemini-3.1-flash-lite"][:2] == (Decimal("0.2500"), Decimal("1.5000"))
    assert seed["gemini-3.1-pro-preview"][:2] == (Decimal("2.0000"), Decimal("12.0000"))
    # Migration 175 (GLM) — PER-MODEL multipliers that DIFFER from the 173/174 defaults.
    assert seed["glm-5.2"][:2] == (Decimal("1.4000"), Decimal("4.4000"))
    assert seed["glm-5.2"][2] == Decimal("1.0")  # discount_multiplier (no batch/flex tier)
    assert seed["glm-5.2"][3] == Decimal("0.186")  # cached_in_multiplier
    # Every OTHER row keeps the shared column defaults: discount_multiplier 0.5, cached_in_multiplier 0.1.
    defaulted = {m: e for m, e in seed.items() if m != "glm-5.2"}
    assert all(entry[2] == Decimal("0.5") for entry in defaulted.values())
    assert all(entry[3] == Decimal("0.1") for entry in defaulted.values())
