# Anthropic API rate limit (429)

## Symptom

- Agent runs returning `RateLimitError` from `anthropic` SDK
- VT-202 alert: `anthropic_429_rate`
- Logfire dashboard: spike of `anthropic.api_error`

## Detection

- VT-202 alert via Telegram
- `pipeline_steps` rows with `status='error'` + error envelope mentioning `429` or `rate_limit_exceeded`

## Triage

1. Check Anthropic Console → Usage tab — is the org-level limit hit, or per-model?
2. Confirm whether VT-194 prompt caching is active (cache_control blocks should produce ~6.9x cost reduction at the request-rate level; if missing, request rate is artificially high)
3. Check if a runaway agent is in tight tool-use loop (`pipeline_steps` for a single run with > 20 step rows)

## Resolution

1. If org-limit hit: request a limit increase from Anthropic (form on Console)
2. If runaway agent: surface in Ops Console; manually terminate the offending run via admin endpoint
3. If prompt caching missing: ship a fix to re-enable (per VT-194 substrate)
4. Short-term: increase backoff in `OrchestratorReasoningCallback` retry policy

## Postmortem

- Incident log
- If runaway agent shape was new: extend scope-discipline tests (VT-38) to cover the input shape

## Tabletop drill — last-run YYYY-MM-DD

- Outcome: NOT YET DRILLED
