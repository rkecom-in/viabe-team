# Recovery Strategies (VT-29)

The five strategies the router (`orchestrator.error_router.route_failure`) can
return. Strategies are values, not closures — the *executor* for each strategy
decides how to act on it. Adding a new strategy requires updating both ends.

## The five strategies

### `retry_with_backoff`

Jittered exponential backoff. Curve: 1, 2, 4, 8, 16 seconds (±25% uniform
jitter). Hard cap at 5 attempts. Computed by `orchestrator.backoff.compute_delay`.

The retry counter lives in `SubscriberState.history` (event=`"failure"`,
`failure_type=<value>`). Once the counter for that failure type reaches the
type's `escalation_threshold`, the router overrides this strategy to an
escalation.

A per-(vendor, env) circuit breaker (`orchestrator.backoff.CircuitBreaker`)
sits ahead of the retry loop for `external_api_error`. Opens at 10 failures
inside a 60-second rolling window; stays open for 5 minutes; the next call
after that goes through HALF_OPEN — success closes, failure re-opens for
another 5 minutes.

### `retry_after_owner_clarification`

Surfaces an owner-facing prompt asking for clarification, then re-enters the
agent loop with the owner's answer in context. Used for `agent_refusal` —
the agent declined to act and we genuinely need owner input to proceed.
`max_retries=1`: one clarification round, then escalate.

### `escalate_to_owner`

Send an owner-facing WhatsApp template. The owner sees a human-readable
explanation and a small action surface (approve / reject / ask for changes).
Used for medium-severity escalations and for `agent_hard_limit_breach` — the
agent didn't fail, it asked for human judgment by hitting a limit.

### `escalate_to_fazal`

Internal escalation: the orchestrator pages Fazal (mechanism TBD — likely a
Slack channel or dedicated alerting path). Used for high/critical severity
failures the owner cannot resolve: `database_error`, `unknown_error`, and any
medium-severity failure that exceeded its `escalation_threshold` while at
high severity.

### `accept_and_log`

No retry, no escalation, no owner contact. Write the classified failure to
`pipeline_steps` and let the request response carry whatever 4xx/5xx the
endpoint already returns. Used for hostile-by-design failures —
`webhook_signature_failure` is the canonical case.

## Strategy-to-failure mapping

The default strategy per failure type is documented in
[error-taxonomy.md](error-taxonomy.md). The mapping is policy and lives in
`orchestrator.failures.SPECS` — change there, not at call sites.

## Two-layer rule (repeated for emphasis)

System errors are DBOS auto-resume; these strategies do not apply. Strategies
only run on *classified business* failures that have come through
`FailureRecord` and `route_failure`. No silent swallowing — every caught
business exception must produce a `FailureRecord`.
