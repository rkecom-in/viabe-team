"""VT-472 — TECH specialist lane tests.

The Tech lane is one of the manager's six specialists (design §8 charter): store/website/listing
HEALTH (GBP/Shopify/platform listings), integration setup help, connection diagnosis. v1 =
advise / act-within-policy. The load-bearing guarantees this pins:

  1. CAPABILITY BOUNDARY (VT-268) — the lane holds NO config-write / integration-mutate / send /
     ledger-write / spend tool. The exact tool surface is allowlist-pinned (a new tool fails →
     forces review) and the fail-CLOSED guard passes the real surface + raises on a synthetic
     config-write tool added to it.
  2. CONFIG-CHANGE GATING — a config/integration change routes through the DETERMINISTIC
     business-impact gate (``assert_or_gate_business_action`` for ``CONFIG``), NOT a direct write.
     A fail-closed (no-grant) tenant ⇒ REQUIRES_OWNER_APPROVAL (the owner-gated charter default);
     the lane only REPORTS the gate decision — it never performs the effect.
  3. REGISTRATION — the lane EXPORTS a roster ``SpecialistSpec`` (``SPECIALIST_SPEC``) the
     coordinator appends to ROSTER; it owns no edit to roster.py. Its node is a CompiledStateGraph
     (``wrap_node=False``), edge_to=None (→ END).
  4. REUSE — the health reads delegate to the EXISTING integration substrate
     (``tenant_connector_status`` / ``platform_listings``) + registry, not a parallel store.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("langchain")


def _names(tools):
    return {getattr(t, "name", type(t).__name__) for t in tools}


# The exact tech-lane tool surface (allowlist pin — a NEW tool, addition OR removal, fails this →
# forces a VT-268 review that the new capability is not a config-write / send / spend breach).
TECH_LANE_EXPECTED = {
    "read_integration_health",
    "read_listing_health",
    "advise_integration_setup",
    "read_tech_context",
    "propose_config_change",
    "check_config_change_intent",
    "tech_escalate_to_fazal",
}


# --- capability boundary (VT-268) -----------------------------------------


def test_tech_lane_tool_allowlist_pinned():
    from orchestrator.agent.tech_lane import TECH_LANE_TOOLS

    assert _names(TECH_LANE_TOOLS) == TECH_LANE_EXPECTED


def test_tech_lane_holds_no_config_write_or_send_tool():
    """No tool NAME on the surface matches a forbidden-capability substring (config-write / send /
    spend / ledger / accounts) — the lane proposes INTENTS; the rails own the effect."""
    from orchestrator.agent.tech_lane import TECH_LANE_TOOLS
    from orchestrator.agent.tool_guardrail import find_forbidden_tools

    assert find_forbidden_tools(TECH_LANE_TOOLS) == []


def test_tech_lane_guard_passes_real_surface():
    from orchestrator.agent.tech_lane import TECH_LANE_TOOLS
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    # No raise on the real surface.
    assert_agent_tools_safe(TECH_LANE_TOOLS, surface="tech_lane")


def test_build_tech_lane_agent_rejects_config_write_tool():
    """Runtime fail-CLOSED: handing the builder a config-write tool raises at build (VT-268), it does
    NOT silently wire it onto the lane surface."""
    from langchain_core.tools import tool

    from orchestrator.agent.tech_lane import _MODEL, build_tech_lane_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    @tool
    def apply_config_change_evil(tenant_id: str) -> str:
        """A would-be direct config-write tool that must never reach the tech lane."""
        return tenant_id

    with pytest.raises(ToolGuardrailViolation):
        build_tech_lane_agent(_MODEL, extra_tools=[apply_config_change_evil])


def test_build_tech_lane_agent_rejects_integration_mutate_tool():
    from langchain_core.tools import tool

    from orchestrator.agent.tech_lane import _MODEL, build_tech_lane_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    @tool
    def update_integration_config_evil(tenant_id: str) -> str:
        """A would-be integration-mutate tool — must be barred from the advise surface."""
        return tenant_id

    with pytest.raises(ToolGuardrailViolation):
        build_tech_lane_agent(_MODEL, extra_tools=[update_integration_config_evil])


# --- config-change routes through the gate, not a direct write ------------


def test_check_config_change_intent_routes_through_business_impact_gate(monkeypatch):
    """The CONFIG-change intent check delegates to ``assert_or_gate_business_action`` for the CONFIG
    class — it does NOT write anything. It reports the gate's deterministic decision."""
    import orchestrator.agents.business_impact_choke as choke
    from orchestrator.agent.tech_lane import check_config_change_intent

    captured: dict[str, object] = {}

    def _fake_gate(tenant_id, action_class, magnitude_minor, *, action_attrs=None, conn=None):
        captured["action_class"] = action_class
        captured["magnitude_minor"] = magnitude_minor
        captured["action_attrs"] = action_attrs
        return choke.BusinessActionGate(
            decision=choke.BusinessActionDecision.REQUIRES_OWNER_APPROVAL,
            reason=choke.REASON_ALWAYS_APPROVE_TIER,
            action_class=choke.BusinessImpactClass.CONFIG.value,
            magnitude_minor=magnitude_minor,
            tier=choke.TIER_ALWAYS_APPROVE,
        )

    monkeypatch.setattr(choke, "assert_or_gate_business_action", _fake_gate)

    tid = str(uuid4())
    out = check_config_change_intent.invoke({"tenant_id": tid, "target": "shopify"})

    # Routed through the CONFIG class with no money magnitude (a config change carries none).
    assert captured["action_class"] is choke.BusinessImpactClass.CONFIG
    assert captured["magnitude_minor"] == 0
    assert captured["action_attrs"] == {"target": "shopify"}
    # The lane REPORTS the gate decision — owner-gated by the fail-closed default.
    assert out["decision"] == "requires_owner_approval"
    assert out["action_class"] == "config"
    assert out["requires_owner_approval"] is True


def test_config_change_owner_gated_for_no_grant_tenant(monkeypatch):
    """End-to-end through the REAL gate (no DB): a tenant with no autonomy grant + no policy is
    REQUIRES_OWNER_APPROVAL — a config change is owner-gated by charter, fail-closed."""
    import orchestrator.agents.business_impact_choke as choke
    import orchestrator.agents.business_policy as policy
    from orchestrator.agent.tech_lane import check_config_change_intent

    # No policy row → out_of_policy (fail-closed); no autonomy row → always_approve. Either path the
    # deterministic gate returns REQUIRES_OWNER_APPROVAL. Stub the two DB reads to the empty default
    # (the _DENY_ALL policy + the always-approve autonomy floor) — no DB needed.
    from uuid import UUID

    monkeypatch.setattr(
        policy, "get_business_policy", lambda tenant_id, *, conn=None: policy.BusinessPolicy()
    )
    monkeypatch.setattr(
        choke,
        "get_business_autonomy",
        lambda tenant_id, action_class, *, conn=None: choke.BusinessAutonomyState(
            tenant_id=UUID(str(tenant_id)), action_class="config"
        ),
    )

    out = check_config_change_intent.invoke({"tenant_id": str(uuid4()), "target": "gbp_listing"})
    assert out["requires_owner_approval"] is True
    assert out["action_class"] == "config"


def test_propose_config_change_is_intent_only():
    """``propose_config_change`` returns a structured INTENT — no DB call, no write, no secret."""
    from orchestrator.agent.tech_lane import propose_config_change

    tid = str(uuid4())
    out = propose_config_change.invoke(
        {
            "tenant_id": tid,
            "target": "shopify",
            "change_summary": "re-connect the Shopify sync",
        }
    )
    assert out["kind"] == "config_change"
    assert out["tenant_id"] == tid
    assert out["target"] == "shopify"


# --- read-only health reads delegate to the existing substrate (REUSE) ----


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params):
        self._last_sql = sql
        return self

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_read_integration_health_reads_connector_status(monkeypatch):
    """``read_integration_health`` reads ``tenant_connector_status`` under tenant_connection — the
    EXISTING VT-210 substrate, not a parallel store. Returns status codes + counts, no PII."""
    import sys
    from datetime import UTC, datetime

    from orchestrator.agent import tech_lane

    rows = [
        {
            "connector_id": "shopify",
            "enabled": True,
            "last_status": "error",
            "last_sync_at": datetime(2026, 6, 1, tzinfo=UTC),
            "consecutive_fails": 3,
            "last_error_message": "boom" * 80,  # long → truncated to 200
            "next_scheduled_run": datetime(2026, 6, 28, tzinfo=UTC),
        }
    ]

    def _fake_tenant_connection(tenant_id, **kw):
        return _FakeConn(rows)

    # The tool lazily imports tenant_connection inside it; resolve the real submodule via sys.modules
    # (the package namespace shadows it with the re-exported function) and patch its attribute.
    import orchestrator.db.tenant_connection  # noqa: F401 — ensure the submodule is loaded

    tc_mod = sys.modules["orchestrator.db.tenant_connection"]
    monkeypatch.setattr(tc_mod, "tenant_connection", _fake_tenant_connection)

    out = tech_lane.read_integration_health.invoke({"tenant_id": str(uuid4())})
    assert out["count"] == 1
    c = out["connectors"][0]
    assert c["connector_id"] == "shopify"
    assert c["last_status"] == "error"
    assert c["consecutive_fails"] == 3
    assert len(c["last_error_message"]) <= 200


def test_read_listing_health_flags_stale_and_closed(monkeypatch):
    """``read_listing_health`` reads ``platform_listings`` (VT-325) and flags a stale (old fetch) +
    permanently-closed listing from the structured non-PII attributes.

    VT-465: the read now routes through the SANCTIONED ``PlatformListingsWrapper.list_for_tenant``
    seam (the no-direct-tenant-db-access wrapper layer), so the wrapper read is what we stub — the
    rows the wrapper would return (tenant_id included, as the wrapper's tenant-scope validation
    requires) drive the same stale/closed flagging."""
    from datetime import UTC, datetime

    from orchestrator.agent import tech_lane
    from orchestrator.db import wrappers as wrappers_mod

    rows = [
        {
            "tenant_id": str(uuid4()),
            "platform": "gbp",
            "external_listing_id": "place123",
            "rating": 4.2,
            "attributes": {"gbp_title": "Test Cafe", "category": "Cafe", "permanently_closed": True},
            "fetched_at": datetime(2020, 1, 1, tzinfo=UTC),  # very old → stale
        }
    ]

    monkeypatch.setattr(
        wrappers_mod.PlatformListingsWrapper,
        "list_for_tenant",
        lambda self, tenant_id, **kw: rows,
    )

    out = tech_lane.read_listing_health.invoke({"tenant_id": str(uuid4())})
    assert out["count"] == 1
    listing = out["listings"][0]
    assert listing["platform"] == "gbp"
    assert listing["rating"] == 4.2
    assert listing["stale"] is True
    assert listing["permanently_closed"] is True
    assert listing["name"] == "Test Cafe"


def test_advise_integration_setup_reads_registry():
    """``advise_integration_setup`` reads the EXISTING connector registry (REUSE) — no rebuild."""
    from orchestrator.agent.tech_lane import advise_integration_setup

    out = advise_integration_setup.invoke({"category": "digital"})
    assert out["count"] >= 1
    ids = {c["connector_id"] for c in out["connectors"]}
    assert "shopify" in ids  # a known digital connector
    # every entry is digital (category filter applied)
    assert all(c["category"] == "digital" for c in out["connectors"])


# --- roster registration (SPECIALIST_SPEC) --------------------------------


def test_specialist_spec_shape():
    """The lane exports a roster ``SpecialistSpec`` the coordinator appends to ROSTER — a compiled
    sub-graph node (wrap_node=False), edge_to=None (→ END), CONFIG-gated by charter (no prereq bar
    of its own; the config change is gated downstream by the business-impact CONFIG gate)."""
    from orchestrator.agent.roster import SpecialistSpec
    from orchestrator.agent.tech_lane import SPECIALIST_SPEC

    assert isinstance(SPECIALIST_SPEC, SpecialistSpec)
    assert SPECIALIST_SPEC.name == "tech"
    assert SPECIALIST_SPEC.agent_name == "tech_lane"
    assert SPECIALIST_SPEC.spawn_tool_name == "spawn_tech"
    assert SPECIALIST_SPEC.route_key == "spawn_tech"
    assert SPECIALIST_SPEC.wrap_node is False
    assert SPECIALIST_SPEC.edge_to is None
    assert SPECIALIST_SPEC.update_builder is None


def test_specialist_spec_node_builder_builds_subgraph():
    """The node_builder returns the compiled sub-graph (REUSE build_tech_lane_agent)."""
    from orchestrator.agent.tech_lane import SPECIALIST_SPEC, _MODEL

    node = SPECIALIST_SPEC.node_builder(_MODEL)
    assert node is not None
    # A compiled langgraph exposes invoke (sub-graph contract).
    assert hasattr(node, "invoke")
