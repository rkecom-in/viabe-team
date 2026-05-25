# Canaries (Rule #15)

Standalone Python scripts that exercise real vendor APIs. One file per VT-N that touches an external vendor; pytest does **not** discover or run these (no `test_*.py` names, no `@pytest.mark.*`).

## Why a separate directory

`docs/clau/discipline-rules.md` §Rule #15 (Fazal-Standing 2026-05-25): every PR touching an external API merges only after a canary that:

1. Hits the real vendor with real credentials.
2. Asserts the response shape + content matches expectations.
3. Captures the response as an audit artifact in the `pre-merge-result` signal body.
4. Never skips. A skip is a Rule #15 violation.

Pytest is for fast, hermetic, mock-driven correctness — different surface area.

## How to run

Subshell-source the vendor's env file, never export into the parent shell:

```bash
cd /Users/fazalkhan/development/viabe-team/apps/team-orchestrator
(
  set -a
  source ../../.viabe/secrets/<vendor>.env
  set +a
  ./.venv/bin/python canaries/vtN_<vendor>.py
)
```

Multi-vendor canaries source multiple env files inside the same subshell:

```bash
(
  set -a
  source ../../.viabe/secrets/langsmith-dev.env
  source ../../.viabe/secrets/anthropic.env
  set +a
  ./.venv/bin/python canaries/vtN_<feature>.py
)
```

## File naming

`vtN_<vendor-or-feature>.py` — e.g., `vt101_langsmith.py`, `vt32_anthropic_sales_recovery.py`.

## Output contract

A canary exits non-zero on any failed assertion. It prints to stdout the actual observed values for each assertion + a captured JSON dump of the vendor's response (the audit artifact). The pre-merge-result signal pastes that output verbatim into its body.
