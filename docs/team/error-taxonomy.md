# Error Taxonomy (VT-29)

The nine *business* failure types the orchestrator recognises. Each maps to a
default recovery strategy via the policy table below. The router
(`orchestrator.error_router.route_failure`) reads these specs; call sites do
not hard-code values.

## Two-layer rule

This taxonomy is **business** errors only. System errors (Railway crash,
transient DB drop, network blip, Postgres restart) are owned by DBOS
auto-resume and never become a `FailureRecord`. The litmus test:

> If a fresh process restart, started against the same workflow_id, would
> make the problem go away — it is a system error. Hand it to DBOS.

If retry-with-backoff or owner clarification is required to resolve it — it
is a business error. Classify it.

## The nine types

| Type | Severity | Retryable | Default strategy | Max retries | Escalation threshold |
|---|---|---|---|---|---|
| `tool_call_timeout` | medium | yes | `retry_with_backoff` | 3 | 3 |
| `tool_call_error` | medium | yes | `retry_with_backoff` | 3 | 3 |
| `agent_hard_limit_breach` | high | no | `escalate_to_owner` | 0 | 1 |
| `agent_refusal` | medium | no | `retry_after_owner_clarification` | 1 | 1 |
| `agent_invalid_output` | medium | yes | `retry_with_backoff` | 2 | 2 |
| `external_api_error` | medium | yes | `retry_with_backoff` | 5 | 5 |
| `database_error` | high | no | `escalate_to_fazal` | 0 | 1 |
| `webhook_signature_failure` | high | no | `accept_and_log` | 0 | 1 |
| `unknown_error` | critical | no | `escalate_to_fazal` | 0 | 1 |

### Notes on individual types

- `agent_hard_limit_breach` — VT-29 *defines* this type and its five axes
  (tokens=80K, tool_calls=25, depth=8, wall_clock=5min, cost=₹50). VT-29 does
  NOT emit it. VT-35 owns detection / enforcement and is the sole producer.
  The axis appears in `FailureRecord.metadata["axis"]` as a `HardLimitAxis`
  value.
- `database_error` — a *classified* DB error (constraint violation, RLS
  policy rejection, deadlock) — distinct from a transient drop, which is
  system-layer and never reaches here.
- `webhook_signature_failure` — Twilio inbound where the internal secret
  fails constant-time compare. The 403 still ships; the framework logs the
  classified rejection. Tenant is unknown so no `pipeline_steps` write.
- `unknown_error` — the safety net. Anything that surfaces here is by
  definition a taxonomy gap; routing always escalates to Fazal and the
  retry-count override is short-circuited so it cannot retry by accident.

## Escalation routing

When a retry count hits `escalation_threshold`, the router overrides the
default strategy with an escalation:

- severity HIGH / CRITICAL → `escalate_to_fazal`
- severity MEDIUM → `escalate_to_owner`

`unknown_error` is exempt from the override — it always escalates from the
first occurrence.

## Persistence

`route_failure` writes the decision into `pipeline_steps` with:

- `step_kind = 'error_router_decision'`
- `output_envelope = {"strategy": <strategy value>}`
- `error_envelope = {"failure_type", "message", "vendor", "metadata", "occurred_at"}`

Writes go through `tenant_connection`. RLS is enforced (CL-122). Decisions
without tenant context (e.g. pre-auth signature failures) are logged via the
FastAPI logger only.
