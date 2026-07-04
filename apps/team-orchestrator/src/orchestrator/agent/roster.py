"""VT-465 — the specialist ROSTER registry + the standard handoff protocol.

VT-604 Package 1 (2026-07-05): ``ROSTER`` is the RUNTIME SCOPE — exactly the three
Phase-1 specialists (sales_recovery / integration / onboarding_conductor). It makes
adding a FUTURE specialist CHEAP: a sub-graph + ONE ``SpecialistSpec`` entry — not 4
edits across 3 files (handoffs + routing + graph node + graph edge) — but that is no
longer done implicitly by importing a lane module. The six business-domain lane
modules (sales/marketing/finance/accounting/tech/cost_opt) are NOT on this roster;
their tools are Manager-held ADVISORY capabilities instead — see
``agent/advisory_registry.py``.

WHY A CODE REGISTRY (not a DB table) — same call as
``agents/activation_registry.py`` + ``integrations/registry.py``: the roster is
part of the PRODUCT's behavioral contract. Which specialists exist, their tool
sets, and their activation bars ship WITH the code and change only on a
deliberate, reviewed code change. Version-controlled + diffable + unit-testable
at boot; a DB table would add an RLS surface + a migration + a
deploy-vs-data-skew gap for zero live-ops benefit.

SHAPE — one ``SpecialistSpec`` (frozen dataclass) per specialist, collected in
``ROSTER``. The supervisor graph (``build_supervisor_graph``) ITERATES this:
derives the spawn tool (via ``make_spawn_tool``), the graph node (via the
spec's ``node_builder``), and the conditional-edge route-map entry (from the
spec's ``route_key``). Adding a future lane = append ONE entry here.

REUSE (no duplication, Fazal standing): ``handoffs.make_spawn_tool`` is the
generic ``Command(goto, graph=PARENT)`` handoff factory; this module wraps it,
it does not replace it. The two EXISTING specialists (sales_recovery,
integration) are registered here UNCHANGED — same agent_name / tool_name /
route_key / update_builder they had hand-wired before, so their behavior and
their tests are byte-for-byte identical.

---------------------------------------------------------------------------
THE STANDARD HANDOFF PROTOCOL (design §7 "Division of intelligence", 211500Z)
---------------------------------------------------------------------------

The manager (supervisor) and a specialist exchange a STANDARD payload:

  manager -> specialist : {situation, desired_outcome, context_slice, data}
      The manager reads the business situation + decides the OUTCOME that
      benefits the business + WHICH specialist. It does NOT prescribe the
      action. (See ``SpecialistHandoff``.)

  specialist -> manager : {action_taken, outcome}  OR  a PUSHBACK
      The specialist takes {situation, outcome, ...} + decides the ACTION
      using its domain expertise. The handoff is TWO-WAY: if the outcome is
      infeasible/unwise in-lane, the specialist PUSHES BACK + proposes a
      better outcome BEFORE acting (rail-gated). (See ``SpecialistReturn``.)

These envelopes standardize the SHAPE; they do not (and must not) bypass the
deterministic rails — a specialist still routes every side-effect through a
guarded tool (VT-460 / VT-467).

Backward-compat seam: the existing per-specialist context bundle (e.g.
``SalesRecoveryContext`` via ``handoffs._build_sales_recovery_update``) is the
``data`` slice of this protocol. The standard envelope is ADDITIVE — it carries
the situation/outcome framing the old bundle never had, without changing the
bundle the specialist already consumes. ``build_handoff_update`` composes both.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool

from orchestrator.handoffs import (
    _build_integration_update,
    _build_onboarding_conductor_update,
    _build_sales_recovery_update,
    make_spawn_tool,
)

# ---------------------------------------------------------------------------
# The standard manager <-> specialist handoff PROTOCOL (design §7)
# ---------------------------------------------------------------------------

# The state key under which the standard manager->specialist envelope rides.
# Additive alongside the per-specialist data bundle (e.g. sales_recovery_context)
# so existing specialists keep reading their own bundle key unchanged.
HANDOFF_STATE_KEY = "specialist_handoff"

# The state key under which a specialist's structured return rides back to the
# manager. Reserved seam for the two-way protocol (action-taken OR pushback);
# the manager reads it to monitor outcomes / accept a proposed-alternative.
RETURN_STATE_KEY = "specialist_return"


@dataclass(frozen=True)
class SpecialistHandoff:
    """The manager -> specialist handoff payload (design §7, 211500Z).

    The manager frames the WORK as a desired OUTCOME, NOT an action plan — the
    specialist owns the action (its domain expertise picks WHAT to do). Carries:

      - ``situation``       — the business situation/context the manager read
                              (from the KG / business-profile). Plain narrative.
      - ``desired_outcome`` — the outcome the manager decided benefits the
                              business (e.g. "re-engage lapsed high-value
                              customers"). NOT a prescribed action.
      - ``context_slice``   — the scoped slice of business context the
                              specialist is allowed to see (lane-scoped; the
                              manager holds cross-functional strategy, the
                              specialist does not).
      - ``data``            — structured per-lane data (the existing
                              specialist bundle plugs in HERE — e.g. the
                              SalesRecoveryContext payload, or {} when the
                              specialist self-fetches).

    Frozen + slots-free dataclass: a value object passed through graph state.
    All fields default to an empty/neutral value so a manager that has not yet
    been upgraded to populate situation/outcome still produces a valid (if
    sparse) envelope — backward-compat with the pre-VT-465 handoff.
    """

    desired_outcome: str = ""
    situation: str = ""
    context_slice: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpecialistReturn:
    """The specialist -> manager return payload (design §7 TWO-WAY seam).

    One of two modes (the manager branches on ``pushback``):

      - ACTION TAKEN (``pushback=False``): the specialist decided + (rail-gated)
        executed an action toward the outcome. ``action_taken`` describes WHAT
        it did; ``outcome`` describes the result the manager monitors.

      - PUSHBACK (``pushback=True``): the outcome was infeasible/unwise in-lane.
        The specialist did NOT act; ``proposed_outcome`` carries the better
        outcome it proposes, and ``reason`` explains why. The manager re-frames
        + re-dispatches (or escalates) — it does NOT force the action.

    This type is the documented SEAM for the two-way protocol. Wiring a
    specialist to EMIT it (and the manager to consume it) is per-lane work
    (VT-468..472); this spec ensures the shape exists + nothing precludes it.
    """

    pushback: bool = False
    action_taken: str = ""
    outcome: str = ""
    proposed_outcome: str = ""
    reason: str = ""


def _build_context_slice(
    *, spec: SpecialistSpec, state: dict[str, Any]
) -> dict[str, Any]:
    """Produce the lane-scoped ``context_slice`` for this handoff (VT-466).

    Reads the manager's business context for the turn's tenant (RLS-scoped) and
    narrows it to ``spec.name``'s lane via ``context_slice_for_lane``. Best-effort:
    a missing tenant_id or a read miss yields ``{}`` — the standard envelope still
    carries the per-lane ``data`` bundle the specialist consumed before VT-466, so
    a slice miss never blocks the handoff. Imported lazily to keep the roster's
    import surface (it's iterated at supervisor-graph build) free of the knowledge
    layer's DB deps until a handoff actually fires.
    """
    tenant_id = state.get("tenant_id")
    if not tenant_id:
        return {}
    try:
        from orchestrator.knowledge import (
            context_slice_for_lane,
            read_business_context,
        )

        ctx = read_business_context(tenant_id)
        return context_slice_for_lane(ctx, spec.name)
    except Exception:  # noqa: BLE001 — slice is enrichment; a miss yields {}
        import logging

        logging.getLogger(__name__).warning(
            "roster: context_slice build failed (lane=%s); handing off without slice",
            spec.name,
        )
        return {}


def build_handoff_update(
    *,
    spec: SpecialistSpec,
    state: dict[str, Any],
    situation: str = "",
    desired_outcome: str | None = None,
) -> dict[str, Any]:
    """Compose the Command.update for a specialist handoff (the standard payload).

    Merges, in order:
      1. The standard ``SpecialistHandoff`` envelope under ``HANDOFF_STATE_KEY``
         (situation/outcome/context_slice framing — additive, design §7).
      2. The spec's per-lane ``update_builder`` result (the existing data
         bundle — e.g. ``sales_recovery_context``), if any. This is the
         backward-compat path: existing specialists keep reading their own
         bundle key UNCHANGED.

    The per-lane bundle is ALSO surfaced as the ``data`` slice of the standard
    envelope, so a future generic consumer can read {situation, outcome,
    context_slice, data} uniformly without knowing the lane-specific key.

    VT-466: the ``context_slice`` is now populated with the LANE-SCOPED slice of
    the manager's business context (``context_slice_for_lane``) — the objective +
    the lane-relevant profile keys ONLY. So a Finance specialist gets the
    finance-relevant slice, a Sales specialist the sales-relevant slice, etc. The
    manager holds the WHOLE cross-functional context; the specialist sees only its
    slice (design §7 "specialists get scoped slices"). Built from the ONE tenant's
    ``BusinessContext`` (no cross-tenant data). Best-effort: a read miss yields an
    empty slice rather than blocking the handoff (the per-lane ``data`` bundle the
    specialist already consumed pre-VT-466 is unchanged).

    NOTE: this is the typed helper for the PAYLOAD. The actual
    ``Command(goto, graph=PARENT)`` routing stays in ``make_spawn_tool`` — this
    only assembles the ``update`` dict it carries, via ``update_builder``.
    """
    lane_bundle: dict[str, Any] = {}
    if spec.update_builder is not None:
        lane_bundle = spec.update_builder(state)

    context_slice = _build_context_slice(spec=spec, state=state)

    # VT-526 (B3): the manager may AUTHOR the situation + desired_outcome for this handoff (the
    # "empty framing" gap — design §7 wants a manager-framed situation, not a static default).
    # Backward-compatible: unset ⇒ the prior behaviour (empty situation, the spec's default
    # outcome), so an existing caller that doesn't frame is unchanged.
    envelope = SpecialistHandoff(
        desired_outcome=desired_outcome if desired_outcome is not None else spec.default_outcome,
        situation=situation,
        context_slice=context_slice,
        data=dict(lane_bundle),
    )
    update: dict[str, Any] = {HANDOFF_STATE_KEY: envelope}
    # Preserve the legacy per-lane key (e.g. 'sales_recovery_context') so the
    # specialist node reads exactly what it read before VT-465 — zero behavior
    # change for the existing specialists.
    update.update(lane_bundle)
    return update


# ---------------------------------------------------------------------------
# The SpecialistSpec — one declarative entry per specialist lane
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecialistSpec:
    """The declarative registration for ONE specialist lane.

    Everything ``build_supervisor_graph`` needs to wire a lane WITHOUT graph
    surgery lives here:

      - ``name``            — the lane's human name (logging / diagnostics).
      - ``agent_name``      — the parent-graph NODE name. ``make_spawn_tool``'s
                              ``Command(goto=agent_name)`` AND
                              ``graph.add_node(agent_name, ...)`` both key on
                              this exact string — they MUST match.
      - ``spawn_tool_name`` — the handoff tool name the manager's LLM calls
                              (e.g. ``spawn_sales_recovery``). Also the token
                              ``route_after_orchestrator`` matches in the last
                              AIMessage's tool_calls.
      - ``route_key``       — the conditional-edge key returned by
                              ``route_after_orchestrator`` and looked up in the
                              graph's path-map to reach ``agent_name``.
      - ``node_builder``    — a zero/one-arg factory returning the node to add
                              under ``agent_name`` (a CompiledStateGraph
                              sub-graph, or a callable node). Receives the
                              shared ``ChatAnthropic`` model when it accepts it.
      - ``description``     — the spawn tool's description (what the manager's
                              LLM reads to decide WHEN to hand off).
      - ``update_builder``  — the per-lane Command.update extension (the
                              existing data bundle; e.g.
                              ``_build_sales_recovery_update``). Optional.
      - ``prereq``          — the ``activation_registry`` key gating this lane
                              (None = no activation bar). Declarative link to
                              the prereq registry; the gate reads it.
      - ``edge_to``         — the parent-graph node this lane's node flows to
                              when done (e.g. sales_recovery -> 'collapse';
                              integration -> END). Keeps each lane's post-node
                              wiring declarative.
      - ``wrap_node``       — whether the node is a plain function that should
                              be wrapped with the state-transition observability
                              hook. CompiledStateGraph sub-graphs set this False
                              (LangGraph rejects function wrappers around them,
                              VT-183).
      - ``default_outcome`` — the default ``desired_outcome`` stamped into the
                              standard handoff envelope when the manager has not
                              framed one explicitly.
    """

    name: str
    agent_name: str
    spawn_tool_name: str
    route_key: str
    node_builder: Callable[..., Any]
    description: str
    update_builder: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    prereq: str | None = None
    edge_to: str | None = None
    wrap_node: bool = False
    default_outcome: str = ""

    def make_spawn(self) -> BaseTool:
        """Build this lane's handoff tool via the shared ``make_spawn_tool``.

        The ``update_builder`` is wrapped so the Command carries the STANDARD
        payload (``build_handoff_update``) — the standard envelope PLUS the
        legacy per-lane bundle key. ``make_spawn_tool`` keeps owning the
        ``Command(goto, graph=PARENT)`` routing itself.
        """
        spec = self

        def _update_builder(state: dict[str, Any]) -> dict[str, Any]:
            return build_handoff_update(spec=spec, state=state)

        return make_spawn_tool(
            agent_name=self.agent_name,
            tool_name=self.spawn_tool_name,
            description=self.description,
            update_builder=_update_builder,
        )


# ---------------------------------------------------------------------------
# Node builders for the two existing lanes — thin adapters so the roster owns a
# uniform ``node_builder`` callable for each. Imported lazily inside the
# builder to avoid an import cycle (sales_recovery node lives in supervisor.py).
# ---------------------------------------------------------------------------


def _build_sales_recovery_node(model: Any) -> Any:
    """Return the sales_recovery_agent node (the module-level dispatch node).

    REUSE: this is the exact ``_sales_recovery_node`` the supervisor already
    wired — calling ``run_sales_recovery_agent`` with the self-evaluate gate.
    Returned UNWRAPPED; ``spec.wrap_node=True`` so the graph wraps it with the
    state-transition hook (matching the pre-VT-465 wiring).
    """
    # Local import breaks the supervisor <-> roster cycle (supervisor imports
    # the roster to iterate it; the SR node lives in supervisor).
    from orchestrator.supervisor import _sales_recovery_node

    return _sales_recovery_node


def _build_integration_node(model: Any) -> Any:
    """Return the integration_agent sub-graph (a CompiledStateGraph).

    REUSE: ``build_integration_agent`` unchanged. ``spec.wrap_node=False`` —
    a compiled sub-graph must NOT be function-wrapped (VT-183 / VT-206).
    """
    from orchestrator.agent.integration_agent import build_integration_agent

    return build_integration_agent(model=model)


def _build_onboarding_conductor_node(model: Any) -> Any:
    """Return the onboarding_conductor sub-graph (a CompiledStateGraph).

    REUSE: ``build_onboarding_conductor_agent`` unchanged (VT-462). ``spec.wrap_node=False`` —
    a compiled sub-graph must NOT be function-wrapped (VT-183 / VT-206), same as integration.
    """
    from orchestrator.agent.onboarding_conductor import build_onboarding_conductor_agent

    return build_onboarding_conductor_agent(model=model)


# === The roster — one entry per specialist =================================
#
# The two existing specialists, registered UNCHANGED. Their agent_name /
# spawn_tool_name / route_key / update_builder / edge_to / wrap_node are exactly
# what build_supervisor_graph hard-wired before VT-465 — so behavior + tests are
# byte-for-byte identical. A future lane (VT-468..472) appends ONE entry below.
ROSTER: list[SpecialistSpec] = [
    SpecialistSpec(
        name="sales_recovery",
        agent_name="sales_recovery_agent",
        spawn_tool_name="spawn_sales_recovery",
        route_key="spawn",
        node_builder=_build_sales_recovery_node,
        description=(
            "Hand off to the Sales Recovery Agent for dormant-customer "
            "winback campaign work. Use when the conversation indicates "
            "the owner wants to recover sales from inactive customers."
        ),
        update_builder=_build_sales_recovery_update,
        prereq="sales_recovery",
        edge_to="collapse",
        wrap_node=True,
        default_outcome="recover sales from dormant customers",
    ),
    SpecialistSpec(
        name="integration",
        agent_name="integration_agent",
        spawn_tool_name="spawn_integration",
        route_key="spawn_integration",
        node_builder=_build_integration_node,
        description=(
            "Hand off to the Integration Agent for owner onboarding "
            "(connecting Shopify / Google Sheets / etc.). Use when the "
            "conversation indicates the owner wants to add or configure a "
            "data source."
        ),
        update_builder=_build_integration_update,
        prereq=None,
        edge_to=None,  # END — the sub-graph emits no campaign plan to collapse.
        wrap_node=False,
        default_outcome="connect the owner's data source",
    ),
    # VT-462 — the onboarding-conductor: dynamic, brain-conducted PROFILE-SETUP. The manager routes
    # an onboarding-incomplete owner HERE for the profile-collection conversation (confirm the
    # discovered draft + fill business-context gaps), BOUNDED by the prereq registry; the connect/
    # integration specialist above is the SUBSEQUENT step after the profile is collected. Mirrors the
    # integration entry: a CompiledStateGraph sub-graph (wrap_node=False), edge_to=None (-> END).
    SpecialistSpec(
        name="onboarding_conductor",
        agent_name="onboarding_conductor",
        spawn_tool_name="spawn_onboarding_conductor",
        route_key="spawn_onboarding_conductor",
        node_builder=_build_onboarding_conductor_node,
        description=(
            "Hand off to the Onboarding-Conductor for the owner's PROFILE-SETUP "
            "conversation (confirming the discovered business profile + collecting "
            "the missing business-context fields). Use when the owner is new or "
            "mid-onboarding and the next step is setting up their business profile "
            "— BEFORE connecting a data source. Connecting Shopify/Sheets is the "
            "separate Integration Agent, used AFTER the profile is collected."
        ),
        update_builder=_build_onboarding_conductor_update,
        prereq=None,
        edge_to=None,  # END — the sub-graph emits no campaign plan to collapse.
        wrap_node=False,
        default_outcome="collect the owner's business profile",
    ),
]


# ---------------------------------------------------------------------------
# VT-604 Package 1 — the six business lanes are NOT dynamically registered here.
# ---------------------------------------------------------------------------
#
# History: VT-465 introduced ``_register_lanes()``, which imported six lane modules
# (sales/marketing/finance/accounting/tech/cost_opt — VT-468..473) and appended their
# exported ``SPECIALIST_SPEC`` onto ROSTER at import time, making all nine "specialists"
# independently spawnable graph nodes. The verified Phase-1 baseline (2026-07-05,
# ``.viabe/manager-loop-program.md``) found this was NEVER the intended runtime scope:
# the six lanes hold no independent activation bar, no durable task/plan participation,
# and no specialist-return handling of their own — they were reachable as full spawns
# with none of the actual specialist machinery behind them.
#
# THE FIX (execution plan Package 1): ``SPECIALIST_ROSTER`` (this ``ROSTER`` list) is
# EXACTLY the three Phase-1 specialists above (sales_recovery / integration /
# onboarding_conductor) — no dynamic append, no lane-module import here. The six lane
# MODULES themselves are UNCHANGED and still exist on disk (their ``@tool`` functions,
# ``SPECIALIST_SPEC`` exports, and per-lane tests all still work) — they are simply no
# longer wired onto this roster. Their tools are exposed to the MANAGER directly as
# ADVISORY capabilities instead: see ``agent/advisory_registry.py`` (VT-604), which
# imports the six lane modules' tool objects (not their SPECIALIST_SPEC) and hands a
# filtered subset straight to ``build_orchestrator_agent`` via ``supervisor.py``. A lane
# module's ``SPECIALIST_SPEC`` therefore stays UNUSED-BUT-HARMLESS dead code on this
# roster spine — kept only because each lane's own test suite still validates it is a
# well-formed, constructible ``SpecialistSpec`` (a documented invariant for a future
# row, should a lane ever graduate to a real specialist under Package 3+).


def get_spec(agent_name: str) -> SpecialistSpec:
    """Look up a roster entry by its ``agent_name``. Raises ``KeyError`` if absent."""
    for spec in ROSTER:
        if spec.agent_name == agent_name:
            return spec
    raise KeyError(
        f"agent '{agent_name}' not in roster; "
        f"available: {sorted(s.agent_name for s in ROSTER)}"
    )


def spawn_tool_route_keys() -> dict[str, str]:
    """Map ``spawn_tool_name -> route_key`` for every roster member.

    ``route_after_orchestrator`` uses this to derive the conditional-edge key
    from whichever spawn tool the manager's LLM fired — registry-driven, so a
    new lane needs no edit to the routing function.
    """
    return {spec.spawn_tool_name: spec.route_key for spec in ROSTER}


def roster_spawn_tools() -> list[BaseTool]:
    """Build every roster member's handoff tool (passed as the manager's extra_tools)."""
    return [spec.make_spawn() for spec in ROSTER]


__all__ = [
    "HANDOFF_STATE_KEY",
    "RETURN_STATE_KEY",
    "ROSTER",
    "SpecialistHandoff",
    "SpecialistReturn",
    "SpecialistSpec",
    "build_handoff_update",
    "get_spec",
    "roster_spawn_tools",
    "spawn_tool_route_keys",
]
