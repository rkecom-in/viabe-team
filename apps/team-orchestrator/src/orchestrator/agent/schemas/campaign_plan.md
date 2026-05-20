# CampaignPlan v1.0 (VT-37)

Structural contract for the sales_recovery agent's output. Pydantic
discriminated union over `status`, with three variants — one actionable
campaign + two structured refusals/defers. Validators are mandatory; a
draft that fails validation never reaches the orchestrator.

## Variants

`CampaignPlan.status` is the discriminator. Exactly three values:

| Status | Variant | When |
|---|---|---|
| `proposed` | `CampaignPlanProposed` | Agent produced an actionable campaign. All campaign fields required. |
| `out_of_scope` | `CampaignPlanOutOfScope` | Input was outside Sales Recovery domain. Refusal reason carried; no campaign fields. |
| `insufficient_data` | `CampaignPlanInsufficientData` | In-scope but not enough context. Missing-data list carried; no campaign fields. |

The absence of campaign fields on the two non-`proposed` variants is a
**type guarantee**, not a convention — pydantic rejects payloads that
mix variant-foreign fields (e.g. an `out_of_scope` payload carrying
`campaign_window`).

## Agent-terminal vs lifecycle states (load-bearing split)

`CampaignPlan.status` carries ONLY the three agent-terminal states.
The lifecycle states from the v0.1 contract — `approved` / `rejected`
/ `sent` / `failed` — are NOT on this contract. They belong to a
**downstream lifecycle field** owned by campaigns-schema / owner-surface
(separate subtask). VT-37 does not "drop" those states; they live on
a different field.

Concretely:
- The agent emits `CampaignPlan` with `status ∈ {proposed, out_of_scope, insufficient_data}`.
- The owner-surface flow flips a separate `lifecycle_status` field from
  `pending → approved/rejected → sent/failed`.
- A test in this module (`test_proposed_has_no_lifecycle_fields`)
  locks the split so a future change adding `approved` to
  `CampaignStatus` breaks CI.

## Common fields (every variant)

- `version: Literal["1.0"]` — explicit schema version. Major bumps are
  Type 2 governance; adding optional fields is Type 1.
- `tenant_id: UUID` — tenant scope. (Cross-context validation against
  the runtime tenant_id is the calling layer's job, not VT-37's — that
  validator needs runtime context the schema doesn't have.)
- `run_id: UUID` — trace correlation.
- `generated_at: datetime` — when the agent produced the draft. Must
  be timezone-aware.
- `self_evaluate_status: SelfEvaluateStatus` — default
  `not_yet_evaluated`. **VT-37 does NOT enforce this field.** The gate
  that forces the self-evaluate call is **VT-4.5**; this enum exists
  on the contract so VT-4.5 has somewhere to write its verdict.
- `status` — the discriminator.

## `proposed` variant fields

| Field | Type | Notes |
|---|---|---|
| `campaign_window` | `CampaignWindow` | `start`, `end` (both tz-aware). End > start; start not in the past. |
| `target_cohort` | `TargetCohort` | `customer_ids` (literal UUID list), `cohort_label`, `cohort_size`, `selection_reason`. `cohort_size == len(customer_ids)` enforced. |
| `expected_arrr` | `ExpectedARRR` | `low_paise` (int, ≥0), `high_paise` (int, ≥0), `confidence` (low/medium/high), `basis` (prose). `low_paise ≤ high_paise` enforced. **Point estimates forbidden** — the range itself is the Pillar-7-honest output. |
| `evidence_refs` | `list[EvidenceRef]` | Non-empty. See [Evidence-ref marker matching](#evidence-ref-marker-matching). |
| `message_plan` | `MessagePlan` | `template_id`, `template_params`, `language` (en/hi), `personalization` (prose). |
| `exclusion_list` | `list[UUID]` | Customers explicitly excluded from the cohort. |
| `exclusion_reasons` | `dict[UUID, str]` | Reason per excluded customer. Keys must equal `set(exclusion_list)` — no orphan reasons, no missing reasons. |
| `escalation_conditions` | `list[EscalationCondition]` | Structured triggers (`trigger`, `severity`, optional `threshold`) that route to Fazal before send. |

### Money

**All currency is integer paise.** Never float. 1 INR = 100 paise.

### Evidence-ref marker matching

Every prose-bearing field on the `proposed` variant
(`target_cohort.selection_reason`, `expected_arrr.basis`) is scanned
for `[E\d+]` markers (e.g. `[E1]`, `[E2]`). The validator requires
TWO-WAY consistency:

1. Every marker in the prose must resolve to an `EvidenceRef` with the
   matching `claim_id`. A `[E3]` in `basis` without an `EvidenceRef`
   carrying `claim_id="E3"` is rejected.
2. Every declared `EvidenceRef` must be cited by at least one marker
   in the prose. An `EvidenceRef` with `claim_id="E9"` that no prose
   field references is rejected.

`claim_id` is constrained to `^E\d+$` by a pydantic field-level regex
so off-pattern ids can't defeat the marker scan.

`source_kind` is a typed enum — no free strings:
- `tool_call` — a tool-call result captured during the run
- `l4_skill_corpus` — a document id in the L4 skills corpus
- `l2_episodic_memory` — an entry in L2 episodic memory

## `out_of_scope` variant fields

- `out_of_scope_reason: str` (1..500 chars). Explains why the request
  was outside Sales Recovery scope.
- `suggested_specialist: SuggestedSpecialist | None` — optional hint
  at which Phase-2 specialist would handle it (`reputation`,
  `marketing`, `operations`). The orchestrator routes Phase-2 to a
  holding queue today (Phase 2 disabled).

## `insufficient_data` variant fields

- `missing_data: list[MissingDataItem]` — non-empty. Each item carries
  `category`, `description`, `suggested_remediation`.

## Validators (full list)

| Validator | Behaviour |
|---|---|
| `CampaignWindow.end > start` | Reject equal or reversed window |
| `CampaignWindow.start ≥ now` | Reject backdated start (window not in past) |
| `CampaignWindow` tz-awareness | Reject naive datetimes |
| `TargetCohort.cohort_size == len(customer_ids)` | Reject mismatch |
| `ExpectedARRR.low_paise ≤ high_paise` | Reject reversed range |
| `ExpectedARRR.low_paise / high_paise ≥ 0` | Reject negatives |
| `EvidenceRef.claim_id ~= /^E\d+$/` | Reject off-pattern ids |
| `proposed.evidence_refs non-empty` | Reject Pillar-7 violation |
| Evidence marker ↔ claim_id two-way consistency | Reject unbacked prose AND uncited evidence |
| `proposed.exclusion_reasons.keys() == set(exclusion_list)` | Reject orphan reasons + missing reasons |
| Discriminator | Reject variant-foreign fields (e.g. `campaign_window` on `out_of_scope`) |

## What lives elsewhere

The Notion VT-37 page listed several items that are explicitly OUT of
this PR's scope:

- **Approved-template registry + `template_id` validator** —
  `apps/team-orchestrator/config/approved_templates.yaml` and the
  validator that confirms `MessagePlan.template_id` is in it. Owner-
  surface / Meta-template-approval subtask.
- **Serializer module** (`to_orchestrator_dict`, `from_agent_output`)
  — agent↔orchestrator wire format. Separate subtask.
- **`attribution_close_at == send_at + 7d`** — `send_at` doesn't exist
  at agent-output time. That validator belongs wherever `send_at` is
  set (Owner Surface / send queue).
- **Cross-context `tenant_id` validator** — the validator that rejects
  a draft where the agent's `tenant_id` disagrees with the runtime
  context's. Needs runtime context the schema doesn't have. Lives at
  the calling layer.

## Supersession of v0.1

This contract supersedes the v0.1 7-field model at
`apps/team-orchestrator/src/orchestrator/types/campaign_plan.py`
(CL-177). The v0.1 model is **left intact** on `main` because VT-3.4
plumbing code still imports it; migrating those import sites to v1.0
is a **separate follow-up subtask**. The PR body enumerates the import
sites that need migration.

## Evolution contract

- Adding an optional field on an existing variant — **Type 1**.
- Removing a field, making an optional field required, or changing a
  validator's semantics — **Type 2** (joint Clau + Fazal).
- Bumping the `version` literal — **Type 2**.
