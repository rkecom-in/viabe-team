"""VT-659 — Sales Recovery as an ``agent_framework`` MODULE (the thin dual-role adapter).

WHAT THIS IS
------------
The Sales-Recovery shape expressed on the framework contract (`agent_framework/README.md` §"a
dual-role module"): ONE module declaring BOTH roles — it PROPOSES in the conversational lane AND
EXECUTES a coordinator work item — registered once, dispatched by ``ctx.role``. It is a THIN
ADAPTER: every method DELEGATES to the EXISTING Sales-Recovery implementation and maps its result
onto the framework's ``ModuleResult``. It EDITS ZERO existing SR files.

  - ``propose`` -> ``agent.sales_recovery.run_sales_recovery_agent`` (the pure Tier-2 proposer:
    no DB, no send, no mutation) -> ``AgentResult`` mapped to ``ModuleResult(role=PROPOSER, ...)``.
  - ``execute`` -> ``agents.sales_recovery_executor.SalesRecoveryAgent.execute_item`` (the
    coordinator-swept executor) -> ``ItemExecutionResult`` mapped to ``ModuleResult(role=EXECUTOR,
    ...)``.

INERT / ADDITIVE
----------------
Importing this module wires NOTHING live: it is NOT imported by ``agent_framework/__init__`` and it
does NOT register itself. The live cutover (repoint ``supervisor._sales_recovery_node`` ->
``propose``; wire a ``CoordinatorAgentAdapter`` into ``coordinator.get_registry`` for the executor;
``register_activation_prereqs``) is a deliberate, Fazal-authorized, money-path-reviewed later step
(VT-659 §5 step 6). This file only makes the module EXIST + CONFORM.

CAPABILITY MODELING — "arm != send" (VT-659 §7 Open-Q #1, Option A: the ratified default)
-----------------------------------------------------------------------------------------
The manifest declares ONLY the NON-GATED capabilities SR actually exercises —
``{READ_CUSTOMER_LEDGER, PROPOSE_CAMPAIGN}`` — and NO gated (``REQUEST_*``) capability. This is a
deliberate, load-bearing choice, NOT an omission:

  * The proposer is PURE (proposes a campaign; no side effect) -> ``PROPOSE_CAMPAIGN`` + it reads
    the lapsed-customer ledger to frame the proposal -> ``READ_CUSTOMER_LEDGER``.
  * The executor's consequential act is the ARM (``arm_agent_send_approval`` / ``enter_l3_hold``),
    NOT a send. It returns ``awaiting_approval`` + a ``batch_id``; the actual customer send happens
    DOWNSTREAM and module-external (``approval_resume`` / ``l3_hold`` -> ``customer_send.
    agent_send_draft``, the Gate 0..5 stack). There is therefore NO send effect inside
    ``execute_item`` to route through ``GateFacade.request_customer_send`` — and the arm path is
    itself the platform's deterministic money gate, preserved BYTE-FOR-BYTE.

Consequences that make Option A the truthful model (manifest == behavior):

  * The module does NOT declare ``REQUEST_CUSTOMER_SEND``. Declaring it would be either (a) a
    declared-but-unused capability = manifest/behavior drift, or (b) a lie that forces routing the
    executor's ARM through ``request_customer_send`` — which maps to ``agent_send_draft`` (an
    IMMEDIATE send) and would CONVERT arm->approve->send into an immediate send = a money-path
    semantic change. Both are wrong; the VT-659 STOP-GUARDRAIL forbids the module calling
    ``gate.request_customer_send`` in phases 1-5.
  * The ``gate`` argument is therefore intentionally UNUSED by both methods in this phase. The
    proposer facade is empty (a proposal has no side effects). The executor facade, scoped to a set
    with NO gated capability, would also raise ``CapabilityNotDeclared`` on any gated call — the
    executor reaches its effect ONLY through the existing deterministic arm path it delegates to,
    never a direct transport. If a future step wants SR structurally facade-gated, that is Option C
    (extend the framework with a gated ``REQUEST_ARM_APPROVAL`` capability routing to
    ``arm_agent_send_approval`` / ``enter_l3_hold``) — a money-path framework change, out of this
    additive scope.

The delegates are LAZY-imported inside each method so this module's import surface stays dep-light
(the dep-less smoke collects it); ``run_sales_recovery_agent`` pulls the Anthropic SDK and the
executor/coordinator pull ``dbos``. They are also INJECTABLE (``proposer=`` / ``executor_factory=``,
the repo's transport-injection convention, cf. the reference plugin's ``reader=``) so the module
unit-tests with no LLM and no DB.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from orchestrator.agent_framework.capabilities import AgentRole, Capability
from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.manifest import AgentBrief, AgentManifest
from orchestrator.agents.activation_registry import REGISTRY as _ACTIVATION_REGISTRY

logger = logging.getLogger("orchestrator.agent_framework.modules.sales_recovery")

#: The module's stable key. MUST equal the coordinator ``SpecialistAgent`` name for Sales Recovery
#: (``sales_recovery_executor.AGENT_NAME`` == ``coordinator._REGISTRY_SPEC`` key == the registered
#: ``SalesRecoveryAgent.name``) — the ``CoordinatorAgentAdapter`` requires
#: ``adapter.name == manifest.name == coordinator SpecialistAgent.name`` at the deferred cutover.
MODULE_NAME = "sales_recovery"

#: The key under which the manager pre-builds and hands in the ``SalesRecoveryContext`` for the
#: PROPOSER lane. The proposer is PURE (no DB) — building the Composer bundle (DB reads) is the
#: caller's job (today ``supervisor._sales_recovery_node`` does it upstream), so the module receives
#: the already-built context via ``ctx.data`` rather than constructing it.
SR_CONTEXT_KEY = "sales_recovery_context"
#: Optional key for the VT-36 self-evaluate ``evaluator`` (``None`` = gate skipped, the production
#: default until VT-50 lands). Read from ``ctx.data`` so the manager can opt a run into the gate.
EVALUATOR_KEY = "evaluator"

#: Injectable delegate signatures (default ``None`` -> lazy-import the real implementation).
ProposerFn = Callable[..., Any]  # (context, *, evaluator) -> agent.types.AgentResult
ExecutorFactory = Callable[[], Any]  # () -> object with .execute_item(AgentItemContext)

#: VT-101 — the SINGLE SOURCE OF TRUTH for whether Sales Recovery routes through the agent_framework
#: contract (both the supervisor PROPOSER path and the coordinator EXECUTOR path read it). Default
#: OFF: the pre-VT-101 direct paths run byte-identically. Dev sets it to validate the cutover; prod
#: stays unset until Fazal promotes.
FRAMEWORK_ROUTING_FLAG = "TEAM_SR_VIA_FRAMEWORK"


def sr_via_framework() -> bool:
    """True iff SR should route through the agent_framework contract (default OFF).

    Read at CALL TIME (never cached at import) so dev can flip it per-process; prod stays unset
    until Fazal promotes."""
    return os.environ.get(FRAMEWORK_ROUTING_FLAG, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class SalesRecoveryModule:
    """Sales Recovery on the framework contract: a dual-role ``{PROPOSER, EXECUTOR}`` thin adapter.

    Holds NO tool surface and NO gated capability (see the module docstring, "arm != send"). Both
    role methods delegate to the existing SR implementation and never reach an effect except through
    that implementation's own (unchanged) deterministic gates.
    """

    manifest = AgentManifest(
        name=MODULE_NAME,
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER, AgentRole.EXECUTOR}),
        description=(
            "Sales Recovery specialist. PROPOSER: reads the lapsed-customer ledger and proposes a "
            "win-back campaign for the manager to frame against the owner's goal (pure, no side "
            "effect). EXECUTOR: on a coordinator sweep, detects lapsed customers, drafts + grounds, "
            "persists, and ARMS the Pillar-7 send approval (L3 hold or L2 owner-gated) — it does NOT "
            "send; the gated customer send is downstream and platform-owned."
        ),
        # Option A (VT-659 §7): the truthful, minimal, NON-GATED set. No REQUEST_CUSTOMER_SEND — the
        # executor ARMS via the existing deterministic path; there is no send inside it to gate.
        capabilities=frozenset(
            {Capability.READ_CUSTOMER_LEDGER, Capability.PROPOSE_CAMPAIGN}
        ),
        # Reuse SR's EXISTING activation bar verbatim (the single source; VT-421). Its
        # ``.agent == "sales_recovery" == manifest.name``, so ``manifest.validate()`` accepts it.
        prerequisites=_ACTIVATION_REGISTRY[MODULE_NAME],
        tools=(),  # works purely through the context contract + its delegates; holds no tool.
        # VT-669 SUFFICIENCY (the SR ``tools=()`` resolution): SR's job REQUIRES the two Manager-
        # scoped common READ tools to frame a win-back — the lapsed-customer ledger counts + the
        # business context (ARCHITECTURE §1.1). It records them as REQUIRED here while keeping
        # ``tools=()`` because those reads are Manager-scoped (the common set the specialist reaches
        # through the Manager), NOT tools SR holds on its own surface. Its send EFFECT is likewise
        # NOT a required gated tool: SR reaches the send through the deterministic ARM path
        # (arm != send, VT-659 Option A) downstream, so there is no ``REQUEST_CUSTOMER_SEND`` tool
        # to require on the manifest. The ``required_tools_reachable`` check verifies both reads are
        # cataloged + reachable via the Manager-scoped common READ set.
        required_tools=(
            "read_customer_ledger_summary",
            "read_business_context",
            # VT-675: the promoted richer reads — a win-back needs prior-campaign history +
            # attribution + per-customer ledger detail; now common-reachable, so declaring them
            # is verified (not aspirational) by required_tools_reachable.
            "get_recent_campaigns",
            "get_attribution_data",
            "query_customer_ledger",
        ),
        # VT-686 — the agent taxonomy: category/tags/brief, written from this module's own
        # docstring above (accurate, no invention) so the Manager knows WHAT this agent does and
        # WHEN to delegate to it, instead of inferring both from a spawn-tool description.
        category="Sales",
        tags=frozenset({"winback", "lapsed", "campaigns", "sales-recovery"}),
        brief=AgentBrief(
            what_it_does=(
                "Detects lapsed customers, drafts and grounds a win-back campaign, and ARMS the "
                "Pillar-7 send approval (L3 hold or L2 owner-gated). Proposes a win-back campaign "
                "in conversation and executes as a coordinator-swept daily-sweep target."
            ),
            actions=(
                "read_lapsed_customer_ledger",
                "propose_winback_campaign",
                "draft_campaign_message",
                "arm_send_approval",
            ),
            business_activities=(
                "win back lapsed customers",
                "recover at-risk revenue",
                "run automated win-back campaigns",
            ),
            when_to_use=(
                "Route here when the owner asks about lapsed/inactive customers, wants to win back "
                "customers who haven't purchased recently, or asks for a win-back / re-engagement "
                "campaign. Also the coordinator's daily-sweep target for automated win-back."
            ),
            limits=(
                "does NOT send the customer message itself — it ARMS the approval; the actual "
                "send is a downstream, platform-owned gate (arm != send)",
                "does not choose which customers to target — the win-back cohort is SERVER-owned, "
                "never an LLM pick (VT-651); it only drafts the message",
                "does not talk to the owner directly — the Manager renders every word the owner "
                "reads",
            ),
        ),
    )

    def __init__(
        self,
        *,
        proposer: ProposerFn | None = None,
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        self._proposer = proposer
        self._executor_factory = executor_factory

    # --- PROPOSER lane ---------------------------------------------------------------------------

    def _run_proposer(self, context: Any, *, evaluator: Any) -> Any:
        if self._proposer is not None:
            return self._proposer(context, evaluator=evaluator)
        # Lazy: pulls the Anthropic SDK — kept out of import surface for dep-less smoke.
        from orchestrator.agent.sales_recovery import run_sales_recovery_agent

        return run_sales_recovery_agent(context, evaluator=evaluator)

    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        """Run the SR proposer for ``ctx`` and return its ``AgentResult`` as a ``ModuleResult``.

        ``gate`` is intentionally UNUSED — a proposal has no side effects and the proposer-lane
        facade is empty (would raise on any gated call). The manager MUST have pre-built the
        ``SalesRecoveryContext`` and placed it at ``ctx.data[SR_CONTEXT_KEY]`` (the module is pure —
        it does not do the Composer's DB reads); a missing context is a caller/wiring error and
        fails loud, mirroring ``run_sales_recovery_agent``'s own fail-loud on an empty request.
        """
        context = ctx.data.get(SR_CONTEXT_KEY)
        if context is None:
            raise ValueError(
                f"SalesRecoveryModule.propose: ctx.data[{SR_CONTEXT_KEY!r}] must carry a pre-built "
                "SalesRecoveryContext (the proposer is pure — the manager builds the Composer bundle "
                "upstream and hands it in). None was supplied (fail-closed)."
            )
        evaluator = ctx.data.get(EVALUATOR_KEY)  # None -> VT-36 gate skipped (production default).
        agent_result = self._run_proposer(context, evaluator=evaluator)
        # VT-101 money-path faithfulness: preserve ``output`` VERBATIM. It may be ``None`` — a legal
        # ``ModuleResult.proposal`` value (typed ``Mapping | None``). The live proposer node
        # (``supervisor._sales_recovery_node``) relies on ``proposal is None`` to fire
        # ``SpecialistNoOutputError``; collapsing None -> {} (the old ``dict(output or {})``) would
        # mask a no-output terminal as an empty-but-present proposal and break that detection.
        # ``status`` passes through verbatim too (do NOT normalize it here).
        return ModuleResult(
            role=AgentRole.PROPOSER,
            status=agent_result.status,
            proposal=agent_result.output,
            reason=agent_result.terminated_reason or "",
        )

    # --- EXECUTOR lane ---------------------------------------------------------------------------

    def _new_executor(self) -> Any:
        if self._executor_factory is not None:
            return self._executor_factory()
        # Lazy: the executor + coordinator pull ``dbos`` — kept out of the import surface.
        from orchestrator.agents.sales_recovery_executor import SalesRecoveryAgent

        return SalesRecoveryAgent()

    def execute(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        """Run the SR executor for ``ctx`` and return its ``ItemExecutionResult`` as a ``ModuleResult``.

        Translates the framework ``ModuleContext`` (server-derived, IDs-only) into the coordinator's
        ``AgentItemContext`` and delegates to ``SalesRecoveryAgent.execute_item``. ``gate`` is
        intentionally UNUSED in this phase: the executor's consequential act is the ARM, reached
        through the delegate's EXISTING deterministic gate — never a direct transport (see the
        module docstring, "arm != send"). PII stays inside the delegate; IDs-only cross this seam.
        """
        # Lazy: ``AgentItemContext`` lives in the coordinator, which imports ``dbos`` at top level.
        from orchestrator.agents.coordinator import AgentItemContext

        item_ctx = AgentItemContext(
            tenant_id=str(ctx.tenant_id),
            item_id=ctx.item_id or "",
            agent=self.manifest.name,
            work_item_id=ctx.work_item_id or "",
            run_id=ctx.run_id or "",
        )
        result = self._new_executor().execute_item(item_ctx)
        return ModuleResult(
            role=AgentRole.EXECUTOR,
            status=result.work_item_status,
            work_item_status=result.work_item_status,
            batch_id=result.batch_id,
            counters=dict(result.counters),
        )


__all__ = [
    "EVALUATOR_KEY",
    "FRAMEWORK_ROUTING_FLAG",
    "MODULE_NAME",
    "SR_CONTEXT_KEY",
    "ExecutorFactory",
    "ProposerFn",
    "SalesRecoveryModule",
    "sr_via_framework",
]
