"""VT-669 — the Onboarding-Conductor surface as an ``agent_framework`` MODULE (conductor TOOLS).

WHAT THIS IS
------------
The onboarding-conductor specialist (``agent/onboarding_conductor.py`` — VT-462/VT-609) expressed on
the framework contract, ADDITIVELY, so it can carry a self-describing ``required_tools`` manifest and
be verified by the ``required_tools_reachable`` conformance check (VT-669 piece 2/3). A pure
``{PROPOSER}`` module whose ``manifest.tools`` ARE the ten conductor ``@tool`` objects
(``agent/onboarding_conductor.ONBOARDING_CONDUCTOR_TOOLS``): the state read + answer extract/record/
skip/correct + the deterministic completion/activation checks + the business-policy proposal + the
escalate. It re-expresses that surface as a conforming module WITHOUT touching the conductor brain:
it EDITS ZERO existing files and delegates to the EXISTING tools verbatim.

  - ``propose`` is a THIN read entry (the conductor flow is Manager-driven tool-calling, not a single
    ``propose()`` computation): it best-effort reads the tenant's onboarding state and reports the
    conductor tool surface the Manager can drive. The TOOLS are the point; ``propose()`` is the honest
    "here is the onboarding state + the conductor tools available for this tenant" entry.

This is the DIRECT SIBLING of ``integration_tools_module`` — same shape, same import-light discipline,
same additive/inert stance — for the second launch specialist.

INERT / ADDITIVE
----------------
Importing this module wires NOTHING: it is NOT imported by ``agent_framework/__init__`` and it does
NOT register itself. The LIVE onboarding conductor keeps running through the roster
(``SpecialistSpec(name="onboarding_conductor")``) unchanged; this module only makes the surface EXIST
+ CONFORM (with a required-tools manifest). Live graduation is a deliberate, Fazal-gated later step.

IMPORT-LIGHT (mirrors ``integration_tools_module`` exactly)
-----------------------------------------------------------
Building the manifest LAZY-imports ``ONBOARDING_CONDUCTOR_TOOLS`` (which pulls ``langchain_core`` +
the conductor module) so the tuple is resolved at INSTANCE construction, not as a class attribute:
``import orchestrator.agent_framework.modules.onboarding_conductor_module`` pulls NO langchain — only
``OnboardingConductorModule()`` does. A ``state_reader=`` hook is injectable (the repo's
transport-injection convention) so the module unit-tests with no DB.

CAPABILITY MODELING (truthful, minimal, NON-GATED)
--------------------------------------------------
The manifest declares exactly the two NON-GATED capabilities the conductor tools exercise, and NO
gated (``REQUEST_*``) capability — consistent with the PROPOSER-only role:

  * ``READ_BUSINESS_CONTEXT`` — the READS: the onboarding-state read, the deterministic
    completion/activation checks, the next-question read, and the owner-answer extract (all read the
    owner's own onboarding/business context — no customer PII, CL-390).
  * ``PROPOSE_CONFIG_CHANGE`` — the answer RECORD/SKIP/CORRECT profile writes + the
    ``propose_business_policy`` proposal. These are non-gated by the framework's own model (only
    send/spend are ``REQUEST_*``) and VT-268-safe — the deny-list already clears
    ``ONBOARDING_CONDUCTOR_TOOLS`` as the live conductor surface — hence ``PROPOSE_CONFIG_CHANGE``
    (non-gated), never a ``REQUEST_*`` capability. There is NO customer send and NO money action on
    this surface (mirrors the integration module's modeling note). The ``gate`` argument to
    ``propose`` is therefore intentionally UNUSED (the proposer-lane facade is empty and would raise
    on any gated call).

REQUIRED-TOOLS SUFFICIENCY (VT-669 piece 2)
-------------------------------------------
``required_tools`` names the reads the conductor's job cannot function without: the onboarding-state
read (resume-where-you-left-off, called FIRST every turn) + the deterministic completion + activation
checks (the gate between "keep collecting" and "profile done / activate"). All three are on this
module's OWN ``tools`` surface, so ``required_tools_reachable`` verifies them via the own-surface path
(the contrast to SR, whose required reads are Manager-scoped common reads). No gated effect door is
required: the conductor performs no customer send / money action.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from orchestrator.agent_framework.capabilities import AgentRole, Capability
from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.manifest import AgentBrief, AgentManifest

logger = logging.getLogger("orchestrator.agent_framework.modules.onboarding_conductor")

#: The module's stable registry key. Deliberately DISTINCT from the still-live roster brain
#: (``SpecialistSpec(name="onboarding_conductor")``) so the two coexist unambiguously during the
#: additive phase — mirrors ``integration_tools_module``'s ``"integration_tools"`` naming call. The
#: live-cutover naming (align to ``"onboarding_conductor"``?) is deferred to that gated step.
MODULE_NAME = "onboarding_tools"

#: The reads the conductor's job REQUIRES to reach (VT-669 SUFFICIENCY). All three are on this
#: module's OWN tool surface (``ONBOARDING_CONDUCTOR_TOOLS``): the onboarding-state read (called FIRST
#: every turn to resume) + the deterministic completion + activation checks (the collect-vs-done gate).
REQUIRED_TOOLS: tuple[str, ...] = (
    "read_onboarding_state",
    "profile_completion_check",
    "activation_check",
)

#: Injectable state-reader signature: ``(tenant_id: str) -> onboarding-state dict``. Default ``None``
#: -> lazy-import the real ``read_onboarding_state``. Mirrors the integration module's ``state_reader=``.
StateReaderFn = Callable[[str], Any]


def _conductor_tools() -> tuple[Any, ...]:
    """The ten conductor tools, LAZY-imported (importing the conductor pulls ``langchain_core`` — kept
    OUT of this module's import surface, exactly like the integration module's ``_connector_tools``)."""
    from orchestrator.agent.onboarding_conductor import ONBOARDING_CONDUCTOR_TOOLS

    return tuple(ONBOARDING_CONDUCTOR_TOOLS)


class OnboardingConductorModule:
    """The Onboarding-Conductor surface as a pure ``{PROPOSER}`` framework module carrying the ten
    conductor tools on ``manifest.tools`` + a ``required_tools`` sufficiency manifest.

    Declares NO gated capability (no send / money action on the conductor surface). Its ``propose`` is
    a thin, side-effect-free read entry; the conductor TOOLS are the point, Manager-driven at the
    deferred live step. The manifest (with its tool surface) is built at construction so the module
    FILE stays import-light.
    """

    def __init__(self, *, state_reader: StateReaderFn | None = None) -> None:
        self._state_reader = state_reader
        #: Instance-level (not class-level) so importing this file pulls no langchain — only
        #: constructing the module resolves the conductor tools (see the module docstring).
        self.manifest = AgentManifest(
            name=MODULE_NAME,
            version="1.0.0",
            roles=frozenset({AgentRole.PROPOSER}),
            description=(
                "Onboarding-Conductor surface. PROPOSER: reads the tenant's onboarding state and "
                "reports the conductor tools (read-state/extract/record/skip/correct/next-question/"
                "completion-check/activation-check/propose-policy) the Manager drives to run the "
                "owner's profile-setup conversation. No customer send, no money action — reads + "
                "onboarding-profile writes + a business-policy proposal; owner's own data only (CL-390)."
            ),
            # Truthful, minimal, NON-GATED set (see the module docstring, "CAPABILITY MODELING").
            capabilities=frozenset(
                {Capability.READ_BUSINESS_CONTEXT, Capability.PROPOSE_CONFIG_CHANGE}
            ),
            # No activation bar carried in this additive stage (the roster brain still governs the
            # live journey gate). Mirrors the integration module's ``prerequisites=None``.
            prerequisites=None,
            # The ten conductor @tool objects — already deny-list-clean (the live conductor surface);
            # the conformance ``tool_surface_safe`` check re-proves it over all ten here.
            tools=_conductor_tools(),
            required_tools=REQUIRED_TOOLS,
            entitlement_key=None,
            # VT-686 — the agent taxonomy: category/tags/brief, written from this module's own
            # docstring above (accurate, no invention).
            category="Onboarding",
            tags=frozenset({"onboarding", "profile-setup", "activation", "journey"}),
            brief=AgentBrief(
                what_it_does=(
                    "Reads the tenant's onboarding state and drives the owner's profile-setup "
                    "conversation — extracting, recording, skipping, or correcting answers, and "
                    "running the deterministic profile-completion / activation checks."
                ),
                actions=(
                    "read_onboarding_state",
                    "extract_owner_answer",
                    "record_answer",
                    "skip_question",
                    "correct_answer",
                    "profile_completion_check",
                    "activation_check",
                    "propose_business_policy",
                ),
                business_activities=(
                    "collect the owner's business profile",
                    "guide the owner through onboarding step by step",
                    "determine when the account is ready to activate",
                ),
                when_to_use=(
                    "Route here when the owner is still completing onboarding/profile setup, or a "
                    "message reads as an answer to an outstanding onboarding question."
                ),
                limits=(
                    "no customer send, no money action",
                    "reads and writes only the owner's OWN onboarding/business data (CL-390) — "
                    "never customer PII",
                    "does not activate the account itself — it reports the deterministic "
                    "completion/activation check result for the Manager to act on",
                ),
            ),
        )

    # --- PROPOSER lane -------------------------------------------------------------------------

    def _read_state(self, tenant_id: str) -> Any:
        if self._state_reader is not None:
            return self._state_reader(tenant_id)
        # Lazy: pulls the DB-backed onboarding read — kept out of the import surface. This is the
        # SAME ``read_onboarding_state`` the conductor's own tool exposes; the module holds the
        # ALREADY-resolved authoritative tenant, so it invokes the tool's underlying callable with it.
        from orchestrator.agent.onboarding_conductor import read_onboarding_state

        # ``read_onboarding_state`` is a langchain ``@tool``; call its wrapped fn directly with the
        # resolved tenant (no re-run of lane-tenant resolution, which needs an ambient dispatch ctx).
        fn = getattr(read_onboarding_state, "func", read_onboarding_state)
        return fn(tenant_id)

    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        """Report the tenant's onboarding state + the conductor tool surface as a proposal.

        Modeled as a THIN read (the conductor flow is Manager-driven tool-calling, so there is no
        single ``propose()`` computation to run): best-effort read the current onboarding state for
        ``ctx.tenant_id`` and list the conductor tools the Manager can drive. ``gate`` is intentionally
        UNUSED — a proposer has no side effects and this facade is empty (would raise on any gated
        call). A read miss yields a ``None`` state (context is enrichment, not a failure), mirroring
        the integration module; the tool surface is ALWAYS reported.
        """
        tenant_id = str(ctx.tenant_id)
        tool_names = [getattr(t, "name", repr(t)) for t in self.manifest.tools]
        try:
            state = self._read_state(tenant_id)
        except Exception:  # noqa: BLE001 — onboarding-state read is enrichment; a miss is not a failure.
            logger.warning(
                "onboarding_tools: onboarding-state read miss tenant=%s (reporting tools only)",
                tenant_id,
            )
            return ModuleResult(
                role=AgentRole.PROPOSER,
                status="completed",
                proposal={"onboarding_state": None, "conductor_tools": tool_names},
                reason="onboarding_state_read_miss",
            )
        return ModuleResult(
            role=AgentRole.PROPOSER,
            status="completed",
            proposal={
                "onboarding_state": dict(state) if isinstance(state, dict) else state,
                "conductor_tools": tool_names,
            },
        )


__all__ = [
    "MODULE_NAME",
    "REQUIRED_TOOLS",
    "OnboardingConductorModule",
    "StateReaderFn",
]
