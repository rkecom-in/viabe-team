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
    # Seed mirror carries every migration-173 + 174 + 175 + 176 seed model.
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
        "grok-4.5",
        "grok-4.3",
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
    # Migration 176 (Grok) — also PER-MODEL non-default multipliers.
    assert seed["grok-4.5"][:2] == (Decimal("2.0000"), Decimal("6.0000"))
    assert seed["grok-4.5"][2] == Decimal("1.0")  # discount_multiplier (no batch tier)
    assert seed["grok-4.5"][3] == Decimal("1.0")  # cached_in_multiplier (xai bills cache at std)
    assert seed["grok-4.3"][:2] == (Decimal("1.2500"), Decimal("2.5000"))
    assert seed["grok-4.3"][2] == Decimal("0.8")  # discount_multiplier (20% batch)
    assert seed["grok-4.3"][3] == Decimal("1.0")  # cached_in_multiplier
    # Every OTHER row keeps the shared column defaults: discount_multiplier 0.5, cached_in_multiplier
    # 0.1. GLM + Grok carry their own per-model multipliers, so exclude them from the default check.
    _non_default = {"glm-5.2", "grok-4.5", "grok-4.3"}
    defaulted = {m: e for m, e in seed.items() if m not in _non_default}
    assert all(entry[2] == Decimal("0.5") for entry in defaulted.values())
    assert all(entry[3] == Decimal("0.1") for entry in defaulted.values())


# --------------------------------------------------------------------------- grok (migration 176)
def test_grok_standard_and_batch_and_cache(monkeypatch):
    _fix_table(monkeypatch, {})  # seed-mirror path
    # grok-4.5 = $2/M in, $6/M out; discount 1.0 (no batch), cached 1.0 (xai bills cache at std).
    assert compute_cost_usd("grok-4.5", "standard", 1_000_000, 1_000_000) == Decimal("8")
    # discount 1.0 -> a (mistaken) batch tier does NOT under-cost: still 8.
    assert compute_cost_usd("grok-4.5", "batch", 1_000_000, 1_000_000) == Decimal("8")
    # cached priced at 1.0x (NOT 0.1): 1M full in ($2) + 1M cache-read in ($2 * 1.0 = $2) = 4.
    assert compute_cost_usd("grok-4.5", "standard", 1_000_000, 0, 1_000_000) == Decimal("4")
    # grok-4.3 = $1.25/M in, $2.50/M out; discount 0.8 (20% batch).
    assert compute_cost_usd("grok-4.3", "standard", 1_000_000, 1_000_000) == Decimal("3.75")
    # batch 0.8x: 3.75 * 0.8 = 3.0.
    assert compute_cost_usd("grok-4.3", "batch", 1_000_000, 1_000_000) == Decimal("3.0")


# --------------------------------------------------------------------------- search-tool cost (176)
def _fix_search_table(monkeypatch, table):
    monkeypatch.setattr(pricing_mod, "_search_pricing", lambda: table)


def test_compute_search_cost_verified_rates(monkeypatch):
    from orchestrator.llm.pricing import compute_search_cost

    _fix_search_table(monkeypatch, {})  # seed-mirror path
    # anthropic web = $10/1000: 3 invocations = 0.03.
    assert compute_search_cost("anthropic", "web_search", 3) == Decimal("0.03")
    # xai web + x = $5/1000: 2 invocations = 0.01 each.
    assert compute_search_cost("xai", "web_search", 2) == Decimal("0.01")
    assert compute_search_cost("xai", "x_search", 2) == Decimal("0.01")
    # placeholders: openai web $10/1000, google web $35/1000.
    assert compute_search_cost("openai", "web_search", 1000) == Decimal("10")
    assert compute_search_cost("google", "web_search", 1000) == Decimal("35")


def test_compute_search_cost_zero_and_unknown(monkeypatch, caplog):
    from orchestrator.llm.pricing import compute_search_cost

    _fix_search_table(monkeypatch, {})
    # zero / negative count costs 0 with NO lookup + NO warning.
    assert compute_search_cost("anthropic", "web_search", 0) == Decimal("0")
    assert compute_search_cost("anthropic", "web_search", -1) == Decimal("0")
    # unknown (provider, tool) costs 0 and WARNS.
    with caplog.at_level("WARNING"):
        cost = compute_search_cost("nobody", "web_search", 5)
    assert cost == Decimal("0")
    assert any("nobody" in r.getMessage() for r in caplog.records)


def test_compute_search_cost_live_table_first(monkeypatch):
    from orchestrator.llm.pricing import compute_search_cost

    # A live-table override is honoured before the seed mirror (VTR tuning path).
    _fix_search_table(monkeypatch, {("xai", "x_search"): Decimal("7.0000")})
    assert compute_search_cost("xai", "x_search", 1000) == Decimal("7")
    # a (provider,tool) absent from the live table still prices off the seed mirror.
    assert compute_search_cost("anthropic", "web_search", 1000) == Decimal("10")


def test_search_pricing_failsoft_to_seed_on_db_error(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(pricing_mod, "_fetch_search_from_db", _boom)
    monkeypatch.setattr(pricing_mod, "_search_cache", None, raising=False)
    monkeypatch.setattr(pricing_mod, "_search_cache_loaded_at", 0.0, raising=False)
    table = pricing_mod._search_pricing()
    for key in (
        ("anthropic", "web_search"),
        ("xai", "web_search"),
        ("xai", "x_search"),
        ("openai", "web_search"),
        ("google", "web_search"),
    ):
        assert key in table


def test_search_seed_mirror_matches_migration_seed():
    # Guard against seed drift from migration 176's search_tool_pricing seed.
    seed = pricing_mod._SEED_SEARCH_PRICING
    assert seed[("anthropic", "web_search")] == Decimal("10.0000")
    assert seed[("xai", "web_search")] == Decimal("5.0000")
    assert seed[("xai", "x_search")] == Decimal("5.0000")
    assert seed[("openai", "web_search")] == Decimal("10.0000")
    assert seed[("google", "web_search")] == Decimal("35.0000")
