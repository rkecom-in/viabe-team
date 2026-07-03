"""VT-577 — agent DATA-NEEDS registry: integrity + API (pure, no DB, no LLM).

Proves the registry contract the paced flow (VT-576) relies on:
  * every agent-need data class is suppliable by ≥1 declared integration;
  * every integration OFFERED today supplies ≥1 data class and carries owner instructions;
  * next_best_integration orders by enablement×effort, offers ONLY available_today, and drops
    already-connected / nothing-still-needed integrations;
  * readiness math: sales_recovery can_plan after a supplier lands, can_execute needs the channel.
"""

from __future__ import annotations

from orchestrator.onboarding import agent_data_needs as adn


# --- registry integrity -----------------------------------------------------------------


def test_every_agent_need_class_has_a_supplier():
    """Every data class any agent requires (to plan OR execute) is supplied by ≥1 integration —
    otherwise the need is unsatisfiable and the flow could never make an agent ready."""
    all_supplied = set().union(*(i.supplies for i in adn.INTEGRATIONS.values()))
    for need in adn.AGENT_NEEDS.values():
        for dc in need.plan_requires | need.execute_requires:
            assert dc in adn.DATA_CLASSES, f"{dc!r} not a declared DATA_CLASS"
            assert dc in all_supplied, f"no integration supplies {dc!r} (need of {need.agent})"


def test_available_today_integrations_are_well_formed():
    """Every integration offered today supplies ≥1 real data class + has non-placeholder instructions."""
    for integ in adn.INTEGRATIONS.values():
        if not integ.available_today:
            continue
        assert integ.supplies, f"{integ.id} available today but supplies nothing"
        assert integ.supplies <= set(adn.DATA_CLASSES), f"{integ.id} supplies an unknown class"
        assert integ.instructions and "coming soon" not in integ.instructions.lower()


def test_only_the_three_built_connectors_are_available_today():
    """CL-2026-07-03: only shopify + google_sheets + file_upload are built; the rest are coming_soon."""
    available = {i.id for i in adn.INTEGRATIONS.values() if i.available_today}
    assert available == {adn.SHOPIFY, adn.GOOGLE_SHEETS, adn.FILE_UPLOAD}


def test_data_classes_have_owner_labels():
    for dc in adn.DATA_CLASSES.values():
        assert dc.owner_label and not dc.owner_label.startswith("(")


# --- next_best_integration --------------------------------------------------------------


def test_next_best_offers_shopify_first_when_nothing_connected():
    """Nothing connected + sales_recovery priority → shopify leads (effort 1, supplies the most need)."""
    out = adn.next_best_integration(connected=set())
    assert out, "expected at least one suggestion"
    assert out[0].integration == adn.SHOPIFY
    # all offered are available_today and none coming_soon
    offered = {s.integration for s in out}
    assert offered <= {adn.SHOPIFY, adn.GOOGLE_SHEETS, adn.FILE_UPLOAD}
    assert adn.GSC not in offered and adn.WABA not in offered


def test_next_best_ordering_is_enablement_then_effort():
    out = adn.next_best_integration(connected=set())
    # shopify supplies {customers, transactions, catalog}; sheets/upload supply {customers, transactions}
    # sales_recovery needs customers+transactions (plan) — shopify contributes the most still-needed,
    # then sheets (effort 2) before file_upload (effort 3).
    ids = [s.integration for s in out]
    assert ids[0] == adn.SHOPIFY
    assert ids.index(adn.GOOGLE_SHEETS) < ids.index(adn.FILE_UPLOAD)


def test_next_best_drops_connected_and_nothing_still_needed():
    """Once shopify is connected, sales_recovery's plan classes are satisfied → no further
    supplier for the STILL-NEEDED plan/execute set except the messaging channel (which no
    available_today integration supplies), so no available suggestion remains."""
    out = adn.next_best_integration(connected={adn.SHOPIFY})
    ids = {s.integration for s in out}
    assert adn.SHOPIFY not in ids
    # sheets/upload only supply customers+transactions, both already satisfied by shopify → dropped.
    assert adn.GOOGLE_SHEETS not in ids and adn.FILE_UPLOAD not in ids


def test_suggestion_carries_why_and_instructions_no_citations():
    out = adn.next_best_integration(connected=set())
    top = out[0]
    assert top.why and "[F" not in top.why
    assert top.instructions and "myshopify.com" in top.instructions
    assert adn.SALES_RECOVERY in top.unlocks_agents


# --- readiness --------------------------------------------------------------------------


def test_readiness_no_data_cannot_plan():
    r = adn.readiness(adn.SALES_RECOVERY, connected=set())
    assert r.can_plan is False and r.can_execute is False
    assert adn.CUSTOMERS_CONTACTABLE in r.missing_for_plan
    assert adn.TRANSACTIONS_HISTORY in r.missing_for_plan


def test_readiness_shopify_unlocks_planning_but_not_execution():
    """Shopify supplies customers + transactions → can_plan True; can_execute still needs the
    messaging channel → False, missing_for_execute == {messaging_channel}."""
    r = adn.readiness(adn.SALES_RECOVERY, connected={adn.SHOPIFY})
    assert r.can_plan is True
    assert r.can_execute is False
    assert r.missing_for_plan == frozenset()
    assert r.missing_for_execute == frozenset({adn.MESSAGING_CHANNEL})


def test_readiness_sheets_also_unlocks_planning():
    r = adn.readiness(adn.SALES_RECOVERY, connected={adn.GOOGLE_SHEETS})
    assert r.can_plan is True  # sheets supplies customers + transactions


def test_supplied_classes_unions_and_ignores_unknown():
    got = adn.supplied_classes({adn.SHOPIFY, "not_a_real_integration"})
    assert adn.PRODUCT_CATALOG in got and adn.CUSTOMERS_CONTACTABLE in got
