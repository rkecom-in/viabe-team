"""VT-179 envelope: ``tenant_isolation_breach`` (RLS guard violation event).

High-severity security event — ``_tenant_guard.assert_tenant_scoped`` raised
because a tenant-scoped query returned rows with a mismatched tenant_id.
The breach event is emitted BEFORE the `TenantIsolationError` propagates,
so downstream alerting can fire on this step_kind regardless of error
handling decisions higher in the call stack.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class TenantIsolationBreachInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected: str
    breach_count: int


class TenantIsolationBreachEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "tenant_isolation_breach"

    input_envelope: TenantIsolationBreachInput
    output_envelope: None = None


__all__ = ["TenantIsolationBreachInput", "TenantIsolationBreachEnvelope"]
