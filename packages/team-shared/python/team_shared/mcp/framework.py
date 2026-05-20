"""MCP tool framework — base class, context, result envelope (VT-39).

This module is the structural contract every individual tool implements
against. It is intentionally small: the framework owns the call
lifecycle (input validation → execute → output validation → telemetry),
and tool subclasses own the actual work.

Pillar 3 (tenant scoping) — non-negotiable
-----------------------------------------
``ToolContext.tenant_id`` is set by the orchestrator at the dispatch
boundary. It is NEVER an agent-supplied input field; the framework's
registry validator refuses to register a tool whose ``input_schema``
declares a field named ``tenant_id``. Every DB read inside ``execute``
must pass ``ctx.tenant_id`` through to the wrapper / scoped query.

VT-8.1 bridge (typed wrappers)
-----------------------------
``ToolContext.db_handle`` is typed as a tenant-connection factory —
the function returning a context-managed psycopg ``Connection`` scoped
to a tenant id. On main today the factory is
``orchestrator.db.tenant_connection`` (CL-122 GUC-based RLS). When
VT-8.1 ships a richer typed wrapper, ``db_handle`` switches to that
factory and the tool surface stays the same.
"""

from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ValidationError

InputModel = TypeVar("InputModel", bound=BaseModel)
OutputModel = TypeVar("OutputModel", bound=BaseModel)


# ----------------------------------------------------------------------------
# Result envelope
# ----------------------------------------------------------------------------


class ToolStatus(str, Enum):
    """Terminal status on a single tool call."""

    OK = "ok"
    ERROR = "error"
    RATE_LIMITED = "rate_limited"
    UNAUTHORIZED = "unauthorized"
    TIMEOUT = "timeout"


class ErrorCode(str, Enum):
    """The framework's enumerated error codes. Per-tool errors must map
    into one of these — no free strings on ``ErrorEnvelope.code``."""

    INVALID_INPUT = "invalid_input"
    INVALID_OUTPUT = "invalid_output"
    EXECUTION_ERROR = "execution_error"
    RATE_LIMITED = "rate_limited"
    UNAUTHORIZED = "unauthorized"
    TIMEOUT = "timeout"
    DEPENDENCY_ERROR = "dependency_error"
    TENANT_SCOPE_VIOLATION = "tenant_scope_violation"


@dataclass(frozen=True)
class ErrorEnvelope:
    """Structured error. ``message`` is ≤200 chars and MUST NOT carry
    PII — tools are responsible for redaction at the construction site.
    ``retry_after_ms`` populated only for ``RATE_LIMITED``."""

    code: ErrorCode
    message: str
    retry_after_ms: int | None = None

    def __post_init__(self) -> None:
        if len(self.message) > 200:
            # Truncate with marker so the framework never persists an
            # overflowing envelope, even if a tool author forgets.
            object.__setattr__(self, "message", self.message[:197] + "...")


@dataclass
class ToolResult:
    """Uniform return shape for every tool call.

    On OK: ``data`` is the output schema's ``model_dump()``; ``error``
    is None. On any non-OK status: ``data`` is None; ``error`` is
    populated. ``metadata`` is a structured key-value map — NEVER a
    free-text dump.
    """

    status: ToolStatus
    data: dict[str, Any] | None = None
    error: ErrorEnvelope | None = None
    tokens_used: int = 0
    cost_paise: int = 0
    latency_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Context
# ----------------------------------------------------------------------------


# A tenant-connection factory: ``factory(tenant_id) -> ContextManager[Connection]``.
# The framework cares only about the shape; the concrete type lives in
# orchestrator.db (today: tenant_connection; tomorrow: VT-8.1 wrapper).
TenantConnectionFactory = Callable[[UUID | str], AbstractContextManager[Any]]


@dataclass(frozen=True)
class ToolContext:
    """Per-invocation context. Constructed by the orchestrator at the
    dispatch boundary; tools READ it.

    ``tenant_id`` is the load-bearing field — every read inside
    ``execute`` MUST flow through it. Agents cannot pass it: see the
    framework's registry validator.
    """

    tenant_id: UUID
    run_id: UUID
    agent_id: str
    parent_tool_call_id: UUID | None
    cost_budget_remaining_paise: int
    wallclock_remaining_ms: int
    db_handle: TenantConnectionFactory


# ----------------------------------------------------------------------------
# The tool base class
# ----------------------------------------------------------------------------


class _RegistryRejection(Exception):
    """Raised at class-definition time when a tool's schema declares a
    tenant_id field (the registry refuses to honour an agent-supplied
    tenant boundary). Surfaces a clear error at import time so the
    failure is impossible to miss."""


class MCPTool(ABC, Generic[InputModel, OutputModel]):
    """Abstract base for every MCP tool the agent calls.

    Subclasses declare:

      - ``name`` / ``description`` — class-level constants
      - ``input_schema`` / ``output_schema`` — pydantic ``BaseModel`` types
      - ``execute(ctx, inputs)`` — actual work; receives validated input,
        returns validated output

    The framework wraps ``execute`` with:

      1. input validation (returns ``invalid_input`` envelope on failure;
         ``execute`` is NEVER called on invalid input)
      2. tenant-scope check (``input_schema`` MUST NOT declare ``tenant_id``
         — that field belongs to ``ToolContext``)
      3. timing + telemetry (single row per call into ``pipeline_steps``)
      4. output validation (returns ``invalid_output`` envelope on failure;
         the agent NEVER sees malformed output)
      5. exception trap (any ``execute`` exception → ``execution_error``
         envelope; framework does not re-raise into the agent)

    ``is_llm_backed`` defaults to ``False``. LLM-backed tools (the rare,
    justified case — see docs/team/llm-backed-tools-rationale.md)
    override it AND include a written rationale at the override site.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[type[BaseModel]]
    output_schema: ClassVar[type[BaseModel]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Class-definition-time enforcement of Pillar 3.

        A tool whose ``input_schema`` declares a ``tenant_id`` field is
        refused — the registry will not honour an agent-supplied tenant
        boundary (CL-122 / CL-202). The check runs at subclass creation
        so the failure is loud at import.
        """
        super().__init_subclass__(**kwargs)
        # Skip abstract intermediate classes — only concrete tools need
        # the full set of class attrs.
        if getattr(cls, "__abstractmethods__", None):
            return
        for attr in ("name", "description", "input_schema", "output_schema"):
            if not hasattr(cls, attr):
                raise TypeError(
                    f"{cls.__name__} missing required MCPTool class attribute "
                    f"{attr!r}"
                )
        schema_fields = set(cls.input_schema.model_fields.keys())
        if "tenant_id" in schema_fields:
            raise _RegistryRejection(
                f"{cls.__name__}.input_schema declares 'tenant_id' — tenant "
                "scope is set by the orchestrator via ToolContext, never by "
                "the agent (Pillar 3 / CL-122)."
            )

    @classmethod
    def is_llm_backed(cls) -> bool:
        """Override to ``True`` in LLM-backed tool subclasses. See
        docs/team/llm-backed-tools-rationale.md for the criterion. Each
        override site MUST carry a one-line rationale comment."""
        return False

    @abstractmethod
    def execute(self, ctx: ToolContext, inputs: InputModel) -> OutputModel:
        """The actual work. Receives validated inputs; returns an
        instance of ``output_schema``. The framework validates the
        return value before it reaches the agent."""

    # ------------------------------------------------------------------
    # The lifecycle entrypoint — what the dispatch calls
    # ------------------------------------------------------------------

    def call(self, ctx: ToolContext, raw_inputs: dict[str, Any]) -> ToolResult:
        """One full tool-call lifecycle. ``execute`` may raise; the
        framework converts to an ``ErrorEnvelope`` — the agent never
        sees a Python exception.

        Telemetry is the caller's responsibility (the caller wraps this
        in its telemetry sink); ``ToolResult.latency_ms`` is populated
        here for the caller's convenience.
        """
        start = time.monotonic()

        try:
            inputs = self.input_schema.model_validate(raw_inputs)
        except ValidationError as exc:
            return ToolResult(
                status=ToolStatus.ERROR,
                error=ErrorEnvelope(
                    code=ErrorCode.INVALID_INPUT,
                    message=_short_error(exc),
                ),
                latency_ms=int((time.monotonic() - start) * 1000),
            )

        try:
            raw_output = self.execute(ctx, inputs)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 — framework traps all
            return ToolResult(
                status=ToolStatus.ERROR,
                error=ErrorEnvelope(
                    code=ErrorCode.EXECUTION_ERROR,
                    message=_short_error(exc),
                ),
                latency_ms=int((time.monotonic() - start) * 1000),
            )

        try:
            if isinstance(raw_output, BaseModel):
                validated = self.output_schema.model_validate(
                    raw_output.model_dump()
                )
            else:
                validated = self.output_schema.model_validate(raw_output)
        except ValidationError as exc:
            return ToolResult(
                status=ToolStatus.ERROR,
                error=ErrorEnvelope(
                    code=ErrorCode.INVALID_OUTPUT,
                    message=_short_error(exc),
                ),
                latency_ms=int((time.monotonic() - start) * 1000),
            )

        return ToolResult(
            status=ToolStatus.OK,
            data=validated.model_dump(mode="json"),
            latency_ms=int((time.monotonic() - start) * 1000),
        )


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _short_error(exc: Exception) -> str:
    """Truncate exception text for an ErrorEnvelope. Tools are still
    responsible for not leaking PII into the exception message — this
    is just a length guard."""
    msg = str(exc)
    return msg if len(msg) <= 200 else msg[:197] + "..."


def input_hash(raw_inputs: dict[str, Any]) -> str:
    """Stable hash of an input dict — for telemetry's input_envelope so
    a small JSONB column carries deterministic identity, not the full
    payload (PII risk + size)."""
    blob = json.dumps(raw_inputs, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


__all__ = [
    "ErrorCode",
    "ErrorEnvelope",
    "InputModel",
    "MCPTool",
    "OutputModel",
    "TenantConnectionFactory",
    "ToolContext",
    "ToolResult",
    "ToolStatus",
    "_RegistryRejection",
    "input_hash",
]
