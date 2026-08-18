# S2 abandoned-checkout recovery — implementation conformance

**Status:** implementation note for the approved PR #552 design, adapted to tenant-zero Reports.  
**Base:** `origin/dev` at `bf352beb984d4ad4de476f1796a93247206587c0` (2026-08-19).  
**Activation:** off. This change may create source-normalised opportunities and drafts only; it
does not authorise or perform a customer send.

## What remains unchanged from the approved design

- An incomplete checkout is not consent. Eligibility requires an active, purpose-specific
  `checkout_recovery` consent record for the exact tenant, tokenised contact, channel, notice
  version and source evidence.
- `record_of_consent` remains disqualified. The proposed purpose ledger retains per-purpose rows,
  append-only evidence, RLS + FORCE RLS, DSR purge registration, explicit withdrawal and explicit
  re-consent. No conflict clause can clear an opt-out.
- S2 is L2-only and defaults off. It may persist an awaiting-approval draft; the eventual send must
  traverse the existing approval, consent, opt-out, complaint, cap, delivery-frequency and
  transport gates. Manager plan approval and retrieved knowledge never authorise the effect.
- Facts are frozen before drafting. Customer/cart values can only be copied from the source bundle;
  discounts, scarcity, stock and delivery promises are not inventable.
- Specialist memory is narrow: `specialist:abandoned_checkout_recovery` task customisations only.

## Adaptation: source-pluggable checkout attempts

The cohort core consumes one `CheckoutSignalSource` contract and has no Shopify or Reports branch:

```text
ShopifyAbandonedCheckoutSource ─┐
                               ├─> CheckoutAttempt ─> deterministic eligibility ─> grounded draft
ReportsFunnelSource ────────────┘
```

Both adapters must emit the same minimal attempt: tenant, source kind, source attempt ID, attempt
version, created/updated/completed timestamps, total paise, currency, item count, tokenised contact,
opaque destination reference and evidence reference. The core treats terminal completion as
monotonic and deduplicates on `(tenant, source, attempt_id, attempt_version)`.

### Shopify adapter

The Shopify adapter preserves PR #552's design: paginated abandoned-checkout reads plus verified
checkout/order webhooks, terminal completion winning over late updates, and no retained addresses,
line-item prose or raw payloads. The real-Shopify canary remains fail-not-skip and CC-run.

### Reports-funnel adapter (tenant zero, first deployment target)

The Reports bridge may supply either:

1. a tracked-link checkout-intent signal joined to a Reports purchase, where no purchase has
   occurred after the configured abandonment delay; or
2. a daily export containing the same attempt identity, timestamps, amount/currency, item count,
   opaque destination and purchase/completion state.

The adapter does not infer consent from a click, email, phone field, report interest or absence of a
purchase. Purpose consent is an independent eligibility input. A purchase/completion event
tombstones the opportunity. Missing or conflicting attempt identity, contact token, event time,
currency, completion state or evidence reference is quarantined rather than guessed.

The tracked-link path reuses the current attribution rule: a click is evidence of a click, not a
purchase. Purchase completion must arrive from the Reports purchase record. A daily export is not
treated as current until its source timestamp and access timestamp are both recorded.

## Current integration boundary at the base SHA

The rails-generalisation seam is **not present** at `bf352beb`:

- `customer_send.agent_send_draft` still calls `is_agent_eligible(..., "sales_recovery")` and
  resolves consent versions and template signatures by importing `sales_recovery_executor`.
- The Reports checkout bridge described in the task is not present on `origin/dev`.
- The current tracked-link substrate records clicks, but the mint caller / Reports checkout-attempt
  producer needed by this adapter is not present at this base.

Therefore this branch builds the source-neutral contract, both adapters behind injected readers,
deterministic cohort/grounding logic, sendless ACF module, SQL and registry proposals, tests and
canary plan. It deliberately stops before modifying the hard-coded send gate or inventing the
Reports bridge. CC's generalised rails and bridge must land first; integration then binds to those
seams without a second send path.

## Proposed persistence (not a migration)

`docs/agent-framework/sql-proposals/s2-commerce-consent-and-checkout-state.sql` is reviewable SQL
only. No migration number is claimed and nothing is run. When CC allocates the migration, the
tenant-scoped consent/current-state/event tables, RLS + FORCE RLS and `_PURGE_ORDER` code change must
land atomically under the standing DSR rule.

## Activation gates still intentionally unset

- abandonment delay and attribution window;
- the CEO-selected cross-agent contact-budget policy;
- approved notice-version and template allowlists;
- Reports bridge endpoint/export contract and production evidence mapping;
- Meta template SIDs and routing flag.

Unset means no cohort reaches a live send path. It does not mean the implementation may choose a
value silently.
