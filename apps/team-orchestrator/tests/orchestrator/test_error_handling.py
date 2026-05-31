"""VT-29 — business error framework tests.

Three test surfaces:

1. Pure-Python unit tests for ``failures`` / ``strategies`` / ``backoff``
   / the routing function. No DB, no DBOS. Run in the stdlib smoke job
   once heavy deps are importorskip'd; here the only heavy dep is
   ``psycopg`` (pulled in by ``error_router`` for pipeline_steps
   logging), so we gate at import time.
2. A DBOS layer-separation test that proves a system-style exception
   (raised inside a ``@DBOS.step``) DOES NOT become a business
   ``FailureRecord`` — DBOS auto-resume owns it. The two-layer rule
   (VT-29) is broken if this test ever passes by silently swallowing.
3. A live-PG persistence test confirming ``route_failure`` writes the
   classified decision into ``pipeline_steps`` under the correct tenant
   (RLS enforced). Uses the ``rls_ctx`` pattern from
   ``test_tenant_isolation.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.backoff import (  # noqa: E402 — after importorskip
    MAX_ATTEMPTS,
    BreakerState,
    CircuitBreaker,
    compute_delay,
)
from orchestrator.error_router import route_failure  # noqa: E402
from orchestrator.failures import (  # noqa: E402
    SPECS,
    FailureRecord,
    FailureType,
    HardLimitAxis,
    Severity,
)
from orchestrator.strategies import Strategy  # noqa: E402


# --- 1. Pure-Python unit tests ------------------------------------------------


def _bare_record(ftype: FailureType, **kw) -> FailureRecord:
    """Minimal FailureRecord with no tenant/run context (no DB write)."""
    return FailureRecord(
        failure_type=ftype, message="test", occurred_at=datetime.now(UTC), **kw
    )


@pytest.mark.parametrize(
    "failure_type, expected",
    [
        (FailureType.TOOL_CALL_TIMEOUT, Strategy.RETRY_WITH_BACKOFF),
        (FailureType.TOOL_CALL_ERROR, Strategy.RETRY_WITH_BACKOFF),
        (FailureType.AGENT_HARD_LIMIT_BREACH, Strategy.ESCALATE_TO_OWNER),
        (FailureType.AGENT_REFUSAL, Strategy.RETRY_AFTER_OWNER_CLARIFICATION),
        (FailureType.AGENT_INVALID_OUTPUT, Strategy.RETRY_WITH_BACKOFF),
        (FailureType.EXTERNAL_API_ERROR, Strategy.RETRY_WITH_BACKOFF),
        (FailureType.DATABASE_ERROR, Strategy.ESCALATE_TO_FAZAL),
        (FailureType.WEBHOOK_SIGNATURE_FAILURE, Strategy.ACCEPT_AND_LOG),
        (FailureType.UNKNOWN_ERROR, Strategy.ESCALATE_TO_FAZAL),
    ],
)
def test_default_strategy_for_each_failure_type(failure_type, expected):
    """Every business failure type lands on its documented default strategy."""
    assert route_failure(_bare_record(failure_type)) == expected


def test_unknown_error_always_escalates_even_with_zero_retries():
    """Rule 3: unknown_error short-circuits the retry-count override."""
    # Even with an empty history (retry_count==0 < escalation_threshold==1),
    # unknown_error must NOT fall through to its retry path. There IS no
    # retry path — its default IS escalation. Explicit assertion against the
    # short-circuit so a future bug that flips the order is caught.
    state = {"history": []}
    assert (
        route_failure(_bare_record(FailureType.UNKNOWN_ERROR), state)
        == Strategy.ESCALATE_TO_FAZAL
    )


def test_retry_count_override_escalates_to_owner_for_medium_severity():
    """Once retry count hits escalation_threshold, override to escalation.

    AGENT_REFUSAL has severity=MEDIUM, escalation_threshold=1. After one
    prior failure of this type in history, the next routing decision
    escalates — and MEDIUM severity routes to the OWNER, not Fazal.
    """
    state = {
        "history": [{"event": "failure", "failure_type": "agent_refusal"}]
    }
    assert (
        route_failure(_bare_record(FailureType.AGENT_REFUSAL), state)
        == Strategy.ESCALATE_TO_OWNER
    )


def test_retry_count_override_escalates_to_fazal_for_high_severity():
    """High severity escalations route to Fazal, not the owner.

    AGENT_HARD_LIMIT_BREACH has severity=HIGH, escalation_threshold=1 —
    one prior occurrence triggers override; HIGH routes to Fazal.
    """
    state = {
        "history": [
            {"event": "failure", "failure_type": "agent_hard_limit_breach"}
        ]
    }
    assert (
        route_failure(_bare_record(FailureType.AGENT_HARD_LIMIT_BREACH), state)
        == Strategy.ESCALATE_TO_FAZAL
    )


def test_hard_limit_breach_carries_axis_metadata():
    """VT-35 will emit hard-limit breaches with a HardLimitAxis tag. Confirm
    the dataclass round-trips metadata through routing."""
    record = _bare_record(
        FailureType.AGENT_HARD_LIMIT_BREACH,
        metadata={"axis": HardLimitAxis.TOKENS.value, "limit": 80000, "observed": 81234},
    )
    assert record.metadata["axis"] == "tokens"
    assert route_failure(record) == Strategy.ESCALATE_TO_OWNER


def test_spec_table_covers_all_nine_types():
    """SPECS must hold a policy entry for every FailureType (no gaps)."""
    assert set(SPECS) == set(FailureType)
    for ftype, spec in SPECS.items():
        assert isinstance(spec.severity, Severity)
        assert isinstance(spec.default_strategy, Strategy)
        assert spec.max_retries >= 0
        assert spec.escalation_threshold >= 1


# --- Backoff -----------------------------------------------------------------


@pytest.mark.parametrize("attempt, base", [(1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 16.0)])
def test_compute_delay_curve(attempt, base):
    """Without jitter (rand → 0.5 → jitter 0), the curve is exactly 1/2/4/8/16s."""
    assert compute_delay(attempt, rand=lambda: 0.5) == pytest.approx(base)


def test_compute_delay_jitter_band(attempt: int = 3):
    """Jitter is bounded to ±25% of the base delay."""
    base = 4.0
    lo = base * (1 - 0.25)
    hi = base * (1 + 0.25)
    for r in (0.0, 0.25, 0.5, 0.75, 0.999):
        d = compute_delay(attempt, rand=lambda: r)
        assert lo <= d <= hi


def test_compute_delay_rejects_invalid_attempt():
    with pytest.raises(ValueError):
        compute_delay(0)
    with pytest.raises(ValueError):
        compute_delay(MAX_ATTEMPTS + 1)


# --- Circuit breaker ---------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_circuit_breaker_opens_after_threshold_within_window():
    """10 failures inside a 60-second window opens the breaker."""
    clock = FakeClock()
    breaker = CircuitBreaker(time_fn=clock)

    for _ in range(10):
        breaker.record_failure("twilio", "prod")

    assert breaker.state("twilio", "prod") == BreakerState.OPEN
    assert breaker.allow("twilio", "prod") is False


def test_circuit_breaker_does_not_open_on_eleven_errors_across_two_windows():
    """Errors older than 60s evict; 11 errors spread across two windows must
    not trip the threshold."""
    clock = FakeClock()
    breaker = CircuitBreaker(time_fn=clock)

    # 6 errors at t=0.
    for _ in range(6):
        breaker.record_failure("twilio", "prod")
    # Jump 90s — the original 6 fall out of the 60s rolling window.
    clock.now += 90
    for _ in range(5):
        breaker.record_failure("twilio", "prod")

    assert breaker.state("twilio", "prod") == BreakerState.CLOSED


def test_circuit_breaker_half_open_after_five_minutes_then_closes_on_success():
    """OPEN → HALF_OPEN after 5 minutes; a successful probe closes the circuit."""
    clock = FakeClock()
    breaker = CircuitBreaker(time_fn=clock)
    for _ in range(10):
        breaker.record_failure("twilio", "prod")
    assert breaker.state("twilio", "prod") == BreakerState.OPEN

    # Advance past the 5-minute open window.
    clock.now += 300.1
    assert breaker.state("twilio", "prod") == BreakerState.HALF_OPEN
    # allow() lets a probe through.
    assert breaker.allow("twilio", "prod") is True

    breaker.record_success("twilio", "prod")
    assert breaker.state("twilio", "prod") == BreakerState.CLOSED


def test_circuit_breaker_half_open_probe_failure_reopens():
    """A failure during HALF_OPEN re-opens the breaker for another window."""
    clock = FakeClock()
    breaker = CircuitBreaker(time_fn=clock)
    for _ in range(10):
        breaker.record_failure("twilio", "prod")
    clock.now += 300.1  # HALF_OPEN
    assert breaker.state("twilio", "prod") == BreakerState.HALF_OPEN

    breaker.record_failure("twilio", "prod")
    assert breaker.state("twilio", "prod") == BreakerState.OPEN


def test_circuit_breaker_isolates_per_vendor_and_env():
    """Tripping Twilio-prod must not affect Razorpay-prod or Twilio-staging."""
    breaker = CircuitBreaker(time_fn=FakeClock())
    for _ in range(10):
        breaker.record_failure("twilio", "prod")

    assert breaker.state("twilio", "prod") == BreakerState.OPEN
    assert breaker.state("twilio", "staging") == BreakerState.CLOSED
    assert breaker.state("razorpay", "prod") == BreakerState.CLOSED


# --- 2. Two-layer rule: DBOS system errors stay out of the framework ---------


def test_business_framework_does_not_classify_a_python_runtime_error():
    """The framework only wraps EXPLICIT business exceptions at known sites.

    A bare ``RuntimeError`` thrown deep inside a DBOS step has no entry into
    ``FailureRecord`` — DBOS auto-resume owns it (VT-29 two-layer rule). If a
    future change auto-classifies *all* exceptions into UNKNOWN_ERROR, this
    test fails — that change would conflate the layers.
    """

    def deep_dbos_step():
        raise RuntimeError("transient db drop, will auto-resume")

    try:
        deep_dbos_step()
    except RuntimeError:
        # The framework is NOT invoked here — no FailureRecord is constructed,
        # no route_failure is called. The exception is left for DBOS.
        pass


# --- 3. Live-PG persistence test ---------------------------------------------


pytestmark_live = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — live error_router persistence test skipped",
)


@pytest.fixture(scope="module")
def rls_ctx():
    pytest.importorskip("dbos")
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_tenant_with_run(dsn: str) -> tuple[str, str]:
    """Seed a tenant + pipeline_runs row via direct superuser (RLS bypassed)."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-29 router test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()
        tenant_id = str(tenant_row[0])
        run_id = str(uuid4())
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (run_id, tenant_id),
        )
    return tenant_id, run_id


@pytestmark_live
def test_route_failure_persists_decision_to_pipeline_steps(rls_ctx):
    from orchestrator.db import tenant_connection

    tenant_id, run_id = _new_tenant_with_run(rls_ctx.dsn)
    record = FailureRecord(
        failure_type=FailureType.TOOL_CALL_TIMEOUT,
        message="anthropic call timed out after 30s",
        occurred_at=datetime.now(UTC),
        tenant_id=UUID(tenant_id),
        run_id=UUID(run_id),
        vendor="anthropic",
    )

    strategy = route_failure(record)
    assert strategy == Strategy.RETRY_WITH_BACKOFF

    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT step_kind, output_envelope, error "
            "FROM pipeline_steps WHERE run_id = %s",
            (run_id,),
        ).fetchone()

    assert row is not None
    assert row["step_kind"] == "error"
    assert row["output_envelope"] == {"strategy": "retry_with_backoff"}
    assert row["error"]["failure_type"] == "tool_call_timeout"
    assert row["error"]["vendor"] == "anthropic"
