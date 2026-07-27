# O11 judgment-evaluation datasets

This directory contains only the **visible development and validation sets** for
VT-705. They are evaluation fixtures, never retrieval knowledge and never agent
training material.

The sealed held-out set must not exist anywhere in this repository, a Codex
branch, a PR, or a Codex-visible artifact. The harness rejects `split=sealed`
when the dataset path resolves inside the repository.

## Scenario contract

Each JSON file contains:

- `case_id` and `family_id`: opaque identifiers; a family may occur in only one
  partition.
- `split`: `development`, `validation`, or `sealed`.
- `business_profile`: archetype, size, maturity, industry, and geography.
- `situation`, `owner_request`, `facts`, `constraints`: the complete **agent
  view**.
- `acceptable_characteristics`, `harmful_responses`, `risk_flags`,
  `required_specialists`, `cross_functional_considerations`, and
  `ground_truth_sources`: **judge-only answer key**.
- `deterministic_calculations`: auditable expressions, results, and the exact
  proposition each result supports. These are judge-only and their results are
  permitted by the numeric-grounding rail.
- `allowed_numeric_claims`: derived numeric values that may be stated even when
  the literal value is not present in `facts`.
- `hard_fail_phrases`: narrow, unambiguous effect assertions that
  deterministically violate the ground truth. Do not use phrases that a safe
  answer may repeat while negating; qualitative judgment belongs to the blind
  judge.

There is deliberately no `expected_answer`. O11 measures the quality of a
decision without scripting its wording or turning the dataset into a playbook.

## Commands

From `apps/team-orchestrator`:

```bash
# Validate visible partitions and prove family isolation.
uv run python canaries/o11_judgment_harness.py validate \
  --development-dir canaries/o11/development \
  --validation-dir canaries/o11/validation

# Hermetic scoring during development.
uv run python canaries/o11_judgment_harness.py score \
  --dataset-dir canaries/o11/development \
  --split development \
  --responses /path/to/response-bundle.json \
  --judge stub \
  --output /tmp/o11-development-report.json
```

For the real baseline, the custodian runs `--split sealed --judge llm` against
an external dataset path and a response bundle generated with
`knowledge_mode="off"`. Missing credentials, malformed judge output, incomplete
response coverage, or a sealed path inside the repository fail loud.

## Response-bundle contract

```json
{
  "schema_version": 1,
  "run_label": "no-o8-baseline",
  "knowledge_mode": "off",
  "agent_version": "git:<commit>",
  "responses": [
    {"case_id": "opaque-case-id", "decision": "agent decision text"}
  ]
}
```

The public sealed report contains aggregate scores and a dataset digest only.
If the custodian requests a detailed report, that output path must also be
outside the repository and remain unavailable to builders.
