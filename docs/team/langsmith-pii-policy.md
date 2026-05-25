# LangSmith PII policy (VT-101)

**Status:** Active. Enforced mechanically in `apps/team-orchestrator/src/orchestrator/observability/langsmith.py` and `pii.py`.

## Rule

PII never enters LangSmith in plaintext. Every value sent to a trace flows through `redact_for_langsmith()` first.

## What gets redacted

| Surface | Treatment | Where |
|---|---|---|
| Phone-shaped substrings in any string (`+91 98765 43210`, `9876543210`, `+1-415-555-0100`) | Replaced inline with `phone_tok_<sha256[:16]>` | `pii._redact_str` |
| Dict value at `phone` / `phone_e164` / `mobile` keys | Whole value → `phone_tok_<sha256[:16]>` | `pii._redact_pii_value` |
| Dict value at `body` / `message_body` / `raw_body` keys | Whole value → `body_tok_<sha256[:16]>` | `pii._redact_pii_value` |
| Dict value at `email` key | Whole value → `<redacted:email>` | `pii._redact_pii_value` |
| Dict value at `name` / `customer_name` / `owner_name` / `first_name` / `last_name` / `address` keys | Whole value → `<redacted:<key>:len=N>` (length only) | `pii._redact_pii_value` |
| Lists / tuples | Recursively redacted element-wise | `pii.redact_for_langsmith` |
| Nested dicts | Recursive, depth-capped at 32 | `pii.redact_for_langsmith` |

Salts come from `TEAM_PHONE_HASH_SALT` (same env var used by `utils.phone_token.hash_phone`). A test-only fallback salt exists so CI doesn't crash, but production MUST set the env var — orchestrator init checks at boot.

## Why bypass is mechanically blocked

Redaction happens inside the `traceable_node` / `traceable_tool` decorator wrapper, BEFORE the SDK's `@traceable` is applied to the function:

```python
# observability/langsmith.py — sketch
@wraps(fn)
def wrapper(*args, **kwargs):
    if not is_enabled():
        return fn(*args, **kwargs)
    safe_inputs = redact_for_langsmith(...)  # ← bypass point lives HERE
    traced = _ls_traceable(name=..., metadata={"run_id": ...})(fn)
    return traced(*args, **kwargs)
```

To send raw PII to LangSmith, a caller would have to replace the decorator with their own. Code review catches the addition of a parallel decorator; the rule "use only `traceable_node` / `traceable_tool`" is the bypass-block.

## What is NOT redacted

- `run_id` values — opaque UUIDs, no PII content. They're the cross-link to the LangSmith trace.
- `tenant_id` — internal identifier, not subject-identifiable.
- Status enums, count fields, timestamps, intent labels — non-identifying domain data.
- Any key not listed in `pii._PII_KEYS` and not containing a phone-shaped substring.

## Telegram footer

`format_run_id_footer(run_id)` returns `"run_id=<uuid>"`. Run IDs are opaque; including them in operator-facing Telegram alerts is the bridge from an alert to a LangSmith trace. No PII concern.

The orchestrator does not currently dispatch Telegram messages directly — Telegram is operator-only and lives in the daemon (`.viabe/daemon/`). The helper is shipped here for the eventual wire-up; until then, no orchestrator code calls it. A future VT row will roster the daemon-side integration when that becomes load-bearing (Cowork review note, 2026-05-25).

## Project separation (dev vs prod)

`LANGSMITH_PROJECT` resolves to:

- `viabe-team-dev` — default, used in development + CI.
- `viabe-team-prod` — production env MUST set this. Structural separation; mixing data is an audit failure.

`LANGCHAIN_TRACING_V2=true` + `LANGCHAIN_PROJECT=<same project>` wire up LangGraph's native node tracing. The two env-var families (`LANGSMITH_*` and `LANGCHAIN_*`) point at the same backend.

## Graceful degradation

Any exception from the LangSmith SDK is swallowed inside the wrapper. The wrapped function still returns its real value; only the span is dropped. A LangSmith outage cannot kill the pipeline. Verified by `test_langsmith_failure_does_not_crash_pipeline` and `test_trace_run_swallows_runtree_errors`.

## How to add a new redaction rule

1. Add the key to `_PII_KEYS` in `pii.py` and extend `_redact_pii_value` with the desired tokenization.
2. Add a test case in `test_langsmith.py::test_named_pii_keys_tokenized`.
3. If the value flows through a free-text path rather than a named key, extend `_redact_str`'s regex.
4. Update this doc.

## Future: VT-104 PII redactor

VT-104 will subsume `pii.redact_for_langsmith` with a richer, package-level redactor. The decorator wrapper's call signature won't change — `redact_for_langsmith(value)` stays as the contract; the implementation moves.
