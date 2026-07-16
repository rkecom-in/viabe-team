"""``GateFacade`` — the single chokepoint through which a module requests any GATED action.

THIS IS THE TRUST BOUNDARY. A module is handed a ``GateFacade`` (never the raw gate modules) and
can ONLY reach a consequential action THROUGH it. The facade:

  1. is CAPABILITY-SCOPED from the module's manifest — a gated method the manifest did not declare
     raises ``CapabilityNotDeclared`` (a proposer's facade has an EMPTY gated set, so EVERY gated
     method raises — structurally cannot send/spend);
  2. calls the EXISTING deterministic gate for each action (it adds no new gate and bypasses none):
       - ``request_customer_send`` -> ``customer_send.agent_send_draft`` (Gate 0..5, fail-closed);
       - ``gate_business_action``  -> ``business_impact_choke.assert_or_gate_business_action``;
  3. pins the tenant to the IDOR-resolved ``ModuleContext.tenant_id`` — a module can never send /
     spend for a tenant other than the one it was dispatched for.

Because a module NEVER imports ``customer_send`` / ``twilio_send`` / ``business_impact_choke``
directly (its only door is this facade), and the facade refuses undeclared capabilities, a plugin
— including a future third-party one — is structurally unable to perform an ungated side effect.
That is both the safety property today AND what makes third-party agents admissible later.

The gate modules are LAZY-imported inside each method (they pull DB/dbos/twilio) so the framework's
import surface stays dep-light for the dep-less smoke suite.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from orchestrator.agent_framework.capabilities import (
    GATED_CAPABILITIES,
    Capability,
    is_gated,
)

logger = logging.getLogger("orchestrator.agent_framework.gate_facade")


class CapabilityNotDeclared(RuntimeError):
    """Raised when a module invokes a gated facade method its manifest did not declare.

    The positive-capability enforcement point: the manifest is an upper bound, and the facade
    holds a module to it at CALL time (registration holds it at DECLARE time). A proposer — or any
    module that omitted the capability — hits this the instant it reaches for a gated action.
    """


@dataclass(frozen=True, slots=True)
class BusinessActionOutcome:
    """The whole-round-trip result of ``GateFacade.perform_business_action``.

    Exactly one arm of the gate fired, decided deterministically (never by the module):
      - AUTONOMOUS              -> ``performed=True``,  ``result=<effect_fn() return>``, ``armed=None``
      - REQUIRES_OWNER_APPROVAL -> ``performed=False``, ``result=None``, ``armed=<PauseRequestResult>``
    PII-safe: ``gate`` carries only ids + class + magnitude + reason code (see ``BusinessActionGate``).
    """

    gate: Any  # business_impact_choke.BusinessActionGate
    performed: bool
    result: Any = None  # effect_fn return — only when performed (AUTONOMOUS)
    armed: Any = None  # PauseRequestResult — only when approval-routed


class GateFacade:
    """The capability-scoped door to the platform's deterministic gates.

    Constructed by the framework (registration / the role adapters) from a ``ModuleContext`` +
    the module's declared ``capabilities`` — NOT by the module itself. The module receives an
    already-scoped instance.
    """

    def __init__(
        self,
        *,
        tenant_id: UUID,
        capabilities: Iterable[Capability],
        run_id: str | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._capabilities = frozenset(capabilities)
        self._run_id = run_id

    @property
    def tenant_id(self) -> UUID:
        """The IDOR-resolved tenant this facade is pinned to (read-only)."""
        return self._tenant_id

    @property
    def capabilities(self) -> frozenset[Capability]:
        """The capabilities this facade will service (its manifest scope)."""
        return self._capabilities

    def can(self, capability: Capability) -> bool:
        """True iff this facade will service ``capability`` (the manifest declared it)."""
        return capability in self._capabilities

    def _require(self, capability: Capability) -> None:
        """Fail-closed capability check. Raises ``CapabilityNotDeclared`` if undeclared."""
        if capability not in self._capabilities:
            raise CapabilityNotDeclared(
                f"module (tenant={self._tenant_id}) attempted a gated action requiring "
                f"{capability.value!r}, which its manifest does not declare. Declared: "
                f"{sorted(c.value for c in self._capabilities)!r}. The action is REFUSED — a module "
                "reaches a gated action only through a capability its manifest positively declared."
            )
        if not is_gated(capability):
            # Defensive: only gated capabilities are serviced by these methods. A non-gated
            # capability reaching here is a framework wiring bug, not a plugin action.
            logger.error(
                "gate_facade: _require called with non-gated capability %r — wiring bug",
                capability,
            )

    # --- Gated action: customer send -------------------------------------------------------------

    def request_customer_send(
        self,
        draft_id: UUID | str,
        *,
        autonomy_level: str = "L2",
        conn: Any = None,
        send_fn: Any = None,
    ) -> Any:
        """Request the send of ONE already-persisted draft through the full gate stack.

        Requires the ``REQUEST_CUSTOMER_SEND`` capability. Routes to
        ``customer_send.agent_send_draft`` with the facade's pinned tenant — the module names only
        a ``draft_id``; the platform runs Gate 0 (onboarded) + Gate 0b (WABA live) + gates 1..5
        (batch-state / signature / consent-allowlist / budget) and only then emits through
        ``customer_send_context``. The module holds no Twilio handle and cannot bypass a gate.

        Returns the ``AgentSendResult`` from the gate stack (a skip marker on any gate failure —
        the gate never raises for a gate decision).
        """
        self._require(Capability.REQUEST_CUSTOMER_SEND)
        from orchestrator.agents.customer_send import agent_send_draft

        logger.info(
            "gate_facade: request_customer_send tenant=%s draft=%s level=%s",
            self._tenant_id,
            draft_id,
            autonomy_level,
        )
        return agent_send_draft(
            self._tenant_id,
            draft_id,
            autonomy_level=autonomy_level,
            conn=conn,
            send_fn=send_fn,
        )

    # --- Gated action: consequential business action (SPEND / COMMITMENT / CONFIG) ----------------

    def gate_business_action(
        self,
        action_class: Any,
        magnitude_minor: int,
        *,
        action_attrs: dict[str, Any] | None = None,
        conn: Any = None,
    ) -> Any:
        """Request the deterministic gate decision for a consequential business action.

        Requires the ``REQUEST_BUSINESS_ACTION`` capability. Routes to
        ``business_impact_choke.assert_or_gate_business_action`` with the facade's pinned tenant —
        which DETERMINISTICALLY returns AUTONOMOUS vs REQUIRES_OWNER_APPROVAL (reading the tenant's
        per-class autonomy tier + the owner policy bound, fail-closed). The facade returns the
        ``BusinessActionGate``; the module still cannot PERFORM the effect — the effect itself is
        guarded by the ``business_action_context`` transport choke, entered only on the gate's word.
        """
        self._require(Capability.REQUEST_BUSINESS_ACTION)
        from orchestrator.agents.business_impact_choke import (
            assert_or_gate_business_action,
        )

        logger.info(
            "gate_facade: gate_business_action tenant=%s class=%s magnitude_minor=%d",
            self._tenant_id,
            action_class,
            magnitude_minor,
        )
        return assert_or_gate_business_action(
            self._tenant_id,
            action_class,
            magnitude_minor,
            action_attrs=action_attrs,
            conn=conn,
        )

    def perform_business_action(
        self,
        action_class: Any,
        magnitude_minor: int,
        effect_fn: Callable[[], Any],
        *,
        summary: str,
        action_attrs: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
        conn: Any = None,
        send_fn: Any = None,
        dry_run: bool = False,
    ) -> BusinessActionOutcome:
        """Perform a consequential business action END-TO-END through the gate — the WHOLE round-trip.

        Requires ``REQUEST_BUSINESS_ACTION``. This is the round-trip door (ARCHITECTURE §2: "the gated
        tool owns the whole round-trip — classify AND issue-inside-choke"), symmetric with
        ``request_customer_send`` (which fuses its 7 gates + the ``customer_send_context`` send). It
        classifies via ``assert_or_gate_business_action`` and then, ON THE GATE'S WORD:

          - **AUTONOMOUS** -> issues ``effect_fn()`` INSIDE ``business_action_context(action_class)``
            so the effect's own ``assert_in_business_action_context`` self-guard passes; returns
            ``BusinessActionOutcome(performed=True, result=<effect_fn return>)``.
          - **REQUIRES_OWNER_APPROVAL** -> arms the Pillar-7 owner approval via
            ``arm_business_action_approval`` and does NOT run ``effect_fn``; returns
            ``BusinessActionOutcome(performed=False, armed=<PauseRequestResult>)``.

        The module supplies ONLY the ``effect_fn`` payload + framing (``summary``/``details``); it
        never enters the choke or arms the approval itself, and — like ``gate_business_action`` — it
        cannot bypass a gate. This differs from ``gate_business_action`` (decision-ONLY, for advisory
        intent-checks that issue no effect): a module that actually PERFORMS an effect uses THIS door,
        so "facade decision + caller-issued effect outside the choke" can never be the end-state.

        The correctness gates (owner policy bound, per-class autonomy tier, negative-magnitude,
        frozen kill-switch) are UNCHANGED — they live entirely in the deterministic gate this calls.
        """
        self._require(Capability.REQUEST_BUSINESS_ACTION)
        from orchestrator.agents.business_impact_choke import (
            arm_business_action_approval,
            assert_or_gate_business_action,
            business_action_context,
        )

        gate = assert_or_gate_business_action(
            self._tenant_id,
            action_class,
            magnitude_minor,
            action_attrs=action_attrs,
            conn=conn,
        )
        if gate.requires_owner_approval:
            if self._run_id is None:
                raise ValueError(
                    "perform_business_action: the gate REQUIRES_OWNER_APPROVAL but this facade "
                    "carries no run_id to arm the approval against. Construct the GateFacade with "
                    "the dispatch run_id (ModuleContext.run_id) for any module that can perform a "
                    "business action."
                )
            logger.info(
                "gate_facade: perform_business_action tenant=%s class=%s magnitude_minor=%d -> "
                "REQUIRES_OWNER_APPROVAL (arming approval; effect NOT issued)",
                self._tenant_id,
                action_class,
                magnitude_minor,
            )
            armed = arm_business_action_approval(
                self._tenant_id,
                self._run_id,
                gate,
                summary=summary,
                details=details,
                conn=conn,
                send_fn=send_fn,
                dry_run=dry_run,
            )
            return BusinessActionOutcome(gate=gate, performed=False, armed=armed)

        logger.info(
            "gate_facade: perform_business_action tenant=%s class=%s magnitude_minor=%d -> "
            "AUTONOMOUS (issuing effect inside the choke)",
            self._tenant_id,
            action_class,
            magnitude_minor,
        )
        with business_action_context(action_class):
            result = effect_fn()
        return BusinessActionOutcome(gate=gate, performed=True, result=result)


#: The ``GateFacade`` method that SERVICES each gated capability — the single binding of a gated
#: ``Capability`` to the facade door that routes it to a real deterministic gate. Its keys MUST
#: equal ``GATED_CAPABILITIES`` (asserted at import below): a gated capability with no servicing
#: method is an "orphan" — a manifest could declare it but no door would ever service it. The
#: conformance harness reads this to prove (a) every declared gated capability has a real method
#: (no orphan) and (b) a proposer-scoped facade raises ``CapabilityNotDeclared`` on every one.
#: NOTE: ``REQUEST_BUSINESS_ACTION`` has TWO doors under the same capability — the decision-only
#: ``gate_business_action`` (advisory intent-checks that issue no effect) and the whole-round-trip
#: ``perform_business_action`` (classify AND issue-inside-choke; the ARCHITECTURE §2 door a module
#: uses to actually PERFORM an effect). The mapping names ONE canonical door per capability (the
#: no-orphan invariant); ``perform_business_action`` is ``_require``-guarded identically, so a
#: proposer still cannot reach it (covered by a dedicated unit test).
GATED_METHOD_BY_CAPABILITY: dict[Capability, str] = {
    Capability.REQUEST_CUSTOMER_SEND: "request_customer_send",
    Capability.REQUEST_BUSINESS_ACTION: "gate_business_action",
}

# Framework invariant (fail-loud at import): every gated capability is serviced by a real, callable
# facade method, and the mapping introduces no method that is not an actual GateFacade attribute.
# Cheap boot check — keeps "add a gated capability" and "wire its facade door" from drifting apart.
if set(GATED_METHOD_BY_CAPABILITY) != set(GATED_CAPABILITIES):  # pragma: no cover - invariant guard
    raise RuntimeError(
        "GATED_METHOD_BY_CAPABILITY must map exactly GATED_CAPABILITIES; "
        f"missing={sorted(c.value for c in GATED_CAPABILITIES - set(GATED_METHOD_BY_CAPABILITY))!r} "
        f"extra={sorted(c.value for c in set(GATED_METHOD_BY_CAPABILITY) - GATED_CAPABILITIES)!r}"
    )
for _cap, _method in GATED_METHOD_BY_CAPABILITY.items():  # pragma: no cover - invariant guard
    if not callable(getattr(GateFacade, _method, None)):
        raise RuntimeError(
            f"GATED_METHOD_BY_CAPABILITY[{_cap.value!r}] -> {_method!r} is not a GateFacade method"
        )


__all__ = [
    "GATED_METHOD_BY_CAPABILITY",
    "BusinessActionOutcome",
    "CapabilityNotDeclared",
    "GateFacade",
]
