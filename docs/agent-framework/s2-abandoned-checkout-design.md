# S2 — abandoned-checkout recovery specialist: design checkpoint

**Status:** DESIGN ONLY — implementation waits for CC review.

**Base:** `origin/dev` at `c03878588e5ccfe0d9dfa608ae3c7b25d9172a8b` (2026-08-14).

**Boundary:** S2 creates grounded drafts and arms the existing approval flow. It never sends,
grants consent, or treats retrieval/Manager plan approval as effect authorization.

## 1. Shape and invariant

```text
Shopify read + verified checkout webhook
        │
        ▼
idempotent checkout state ── completion/order event cancels eligibility
        │  wait until abandonment threshold; newest Shopify state wins
        ▼
deterministic cohort gate
  phone present + incomplete + purpose-specific active consent + suppression-clear
        │
        ▼
S2 specialist: plan → grounded template parameters → persisted agent draft batch
        │
        ▼
existing owner approval → agent_send_draft → VT-45 transport choke
```

The implementation clones Sales Recovery's detector → frozen fact bundle → plain-LLM drafting →
post-LLM grounding → `agent_draft_batches`/`agent_drafts` → owner-approval arm shape. S2 launches
L2-only: it cannot create an `auto_send_pending` batch or use the L3 hold path without a later,
separately reviewed grant. The specialist has
`AGENT_TOOLS = ()` **and executes**
`assert_agent_tools_safe(AGENT_TOOLS, surface="agents.abandoned_checkout_recovery")` at import;
neither its connector reader nor its brain imports a transport. Every eventual send must continue
through `agent_send_draft`, the VT-740 per-recipient delivery-frequency veto, and the VT-45
transport choke. The current VT-741 read state and the now-populated
`campaign_messages.campaign_id` are outcome evidence only; neither permits another send.

## 2. S2–S4 consent surface (the shared build)

An abandoned checkout, order, phone number, `buyer_accepts_marketing`, or old QR/win-back consent
does **not** imply permission for checkout recovery. Eligibility requires an affirmative record for
the exact tenant, phone token, channel, and purpose. The finite initial purposes are:

| Purpose | Used by | Meaning |
|---|---|---|
| `checkout_recovery` | S2 | WhatsApp reminders for this merchant's incomplete checkout |
| `replenishment_reminder` | S3 | WhatsApp reminders when a purchased item may be due again |
| `cod_order_confirmation` | S4 | WhatsApp confirmation of a COD order |

`record_of_consent` is disqualified for S2–S4. Its uniqueness is only `(tenant_id, phone_token)`
and its conflict path clears `opted_out_at`; it cannot represent multiple purposes and could
resurrect a withdrawn marketing consent when checkout consent is captured. The purpose ledger
below is the **only** implementation path.

The proposed migration (number supplied by CC; SQL is not part of this checkpoint) adds two
tenant-scoped tables:

- `commerce_consent_grants`: current state keyed by `(tenant_id, phone_token, channel, purpose)`,
  with enum-constrained `channel` and `purpose`, `notice_version`, `locale`, `capture_surface`,
  `source_system`, opaque source reference, affirmative-action timestamp, state
  (`active`/`withdrawn`), withdrawal timestamp, latest event ID, evidence hash, created/updated
  timestamps and a check that active rows have no withdrawal timestamp while withdrawn rows do.
  No raw phone or checkout URL is stored.
- `commerce_consent_events`: append-only `grant`/`reaffirm`/`withdraw` history with the exact
  tenant, phone token, channel and purpose, actor/source, reason, timestamp, evidence hash and a
  tenant-scoped unique idempotency key. It is the proof trail; the current row is only the fast
  projection.

Both tables have tenant foreign keys, RLS + FORCE RLS and tenant-isolation policies. The same code
change that adds their migration also adds both table names to the Python
`dsr_purge._PURGE_ORDER` tuple; the migration itself does not “register” a Python constant. A
hard-delete canary must assert physical zero rows after DSR, because tenant deletion is not the
cleanup mechanism. Webhook/API replays are idempotent.

Initial grant is an INSERT; withdrawal and explicit re-consent are named transactional operations,
not a generic upsert. **No `ON CONFLICT` clause may clear a withdrawal.** Re-consent first appends
a new attributable affirmative event, then reactivates only the exact
`(tenant, phone, channel, purpose)` row in the same transaction. A grant for one purpose never
changes another purpose and never clears global opt-out state.

`privacy.consent.opt_out` remains the global STOP writer and is extended in the same implementation
to call `withdraw_all_commerce_purposes` on its existing transaction: every active purpose row is
marked withdrawn and receives an append-only `global_stop` event. Failure must roll back the STOP
transaction rather than leave the proof ledgers divergent. Send-time still re-reads the customer's
global opt-out as the stronger veto. Purpose re-consent alone cannot reverse that global veto.

The checkout capture contract is an unticked, purpose-specific merchant checkout control whose
copy/version is registry-owned. It posts through a Shopify App Proxy; the browser holds no secret.
The Viabe endpoint recomputes and constant-time compares Shopify's proxy HMAC using the same
tenant-bound Shopify app secret family already used for verified webhooks, then verifies the
shop-to-tenant binding. Only after that does it accept the phone, purpose, notice version, locale,
opaque source reference and affirmative timestamp, tokenise the phone and write the ledger. If the
merchant's Shopify plan/surface cannot supply this verifiable affirmative evidence, S2 stays off
for that shop; there is no fallback inference or merchant-forgeable “signed payload.”

At both cohort time **and send time**, one reviewed consent-policy registry maps the persisted draft
agent to its ledger, exact purpose and allowed notice versions. `sales_recovery` keeps its existing
`record_of_consent` policy and counsel-cleared allowlist; `abandoned_checkout_recovery` uses only
the new ledger and `checkout_recovery` allowlist. `has_marketing_consent_for_phone` becomes an
agent/purpose-aware dispatcher rather than consulting one global default. Unknown agent, purpose,
ledger, or empty production allowlist fails closed. This replaces the current
Sales-Recovery-specific Gate-4 lookup without weakening its existing two production-safety tests.

## 3. Trigger, state, and cohort

The connector gains a dedicated paginated abandoned-checkout read using the existing offline OAuth
token and read scopes; it does not reuse `pull_full`, which currently returns customers only. The
verified `checkouts/create`/`checkouts/update` webhook stops acknowledging-and-discarding and instead
upserts the same normalized checkout state. Required state is minimal: tenant, Shopify checkout ID,
Shopify checkout token, customer link/phone token, created/updated/completed timestamps, currency,
total paise, item count, opaque recovery-destination ciphertext/reference, and source event ID.
Line-item prose, addresses, and raw payloads are not retained. `orders/create` and `orders/paid`
join back on Shopify's `checkout_id`, with `checkout_token` as the guarded fallback; a conflicting
pair fails closed and is quarantined rather than cancelling a different attempt.

The delayed wake is owned by the existing `scheduled_triggers.py` substrate. Implementation adds a
plain, testable `run_abandoned_checkout_recovery_sweep_body` plus
`abandoned_checkout_recovery_sweep_scheduled`, registered by `register_scheduled_triggers()` before
DBOS launch. It scans aged incomplete checkouts and emits the existing coordinator/work-item shape
only when `TEAM_S2_ABANDONED_CHECKOUT_ROUTING_ENABLED=true`; the flag defaults false. The durable
workflow/work-item idempotency key is
`s2-abandoned-checkout:{tenant_id}:{shopify_checkout_id}:{checkout_attempt_version}`. Webhooks own
state freshness; the scheduler alone owns “t+N is now old enough” and drafting eligibility is
re-checked after the wake.

Eligibility is deterministic and re-checked immediately before drafting:

1. checkout is incomplete and older than the configured abandonment delay;
2. phone/customer identity resolves inside the tenant;
3. active `checkout_recovery` consent predates the proposed contact;
4. no prior S2 draft/contact exists for this checkout attempt;
5. customer is subscribed, complaint-clear, and not globally opted out;
6. VT-740 universal delivery-frequency always vetoes; the cross-agent contact-budget decision
   below determines which existing customer caps S2 also applies.

An order/completion webhook tombstones the opportunity. A late or out-of-order checkout event cannot
resurrect it: Shopify `updated_at` plus terminal completion state wins. Detection and persistence are
idempotent on `(tenant_id, shopify_checkout_id, checkout_attempt_version)`; re-drive creates no new
customer contact opportunity for the same attempt.

### Cross-agent contact budget — CEO decision required

Today `check_agent_send_caps` counts customer contacts without an agent filter. That means any
Sales-Recovery contact in the prior 30 days suppresses S2 and may make checkout recovery
near-unfireable for the cohort it targets. This is not accepted silently as the default. VT-740,
global opt-out/complaint gates and the tenant-wide daily cap remain binding under every option;
Fazal must choose one customer-level policy before routing can be enabled:

| Option | Rule | Benefit | Cost/risk |
|---|---|---|---|
| Per-purpose caps | Weekly/30-day/90-day counts are scoped to the consent purpose; global tenant cap remains shared | S2 is not starved by a prior win-back | A customer can receive multiple agent messages close together unless a separate aggregate budget also vetoes |
| Shared budget with priority | One customer budget is shared, but time-critical purposes receive an explicit priority order when opportunities collide | Preserves one contact budget and makes suppression intentional | Requires deterministic arbitration; the chosen priority can starve lower-ranked agents |
| Time-bound S2 exemption | S2 ignores only the customer recontact suppression for `N` hours after abandonment; all other gates/caps remain | Protects the narrow conversion window with the smallest exception | Creates a deliberate extra-contact window and requires Fazal to set `N` and the exact exempt caps |

Until Fazal records the choice and parameters, S2 routing stays off. Implementation builds and
tests only the chosen policy; it does not carry three dormant cap engines.

## 4. Agent-framework and send-rail changes

- Register `abandoned_checkout_recovery` in `activation_registry.py`: journey complete,
  GSTIN-verified, ownership verified, Shopify enabled and successfully read, WABA live, consent
  capture configured, and at least one eligible checkout. Connector-specific prerequisites should
  be new declarative fields/codes, not branches buried in the gate.
- Register a dual-role ACF module patterned on `sales_recovery_module.py`, default routing **off**.
  It declares narrow Sales/Marketing retrieval plus
  `specialist:abandoned_checkout_recovery` task customisations only. It does not receive Manager
  breadth. `read_specialist_customizations` is scoped to the concrete recovery task.
- Generalize `agent_send_draft` Gate 0 to evaluate the immutable persisted draft's `agent` rather
  than hard-coded `sales_recovery`; reject unknown/mismatched agents. Generalize Gate 4 and template
  signature resolution through reviewed registries keyed by agent. All other gates and ordering
  remain unchanged: WABA, L2 batch/owner approval, approved template + opt-out line, live customer
  opt-out/complaint re-read, purpose consent, caps, VT-740, then VT-45.
- Reuse `mint_customer_hook_link` as the **only** recovery-link mint and keep the existing
  `/r/<opaque-token>` click path that VT-741 reads. Extend that existing record with a short-lived,
  encrypted `shopify_checkout` destination reference and target kind; do not create a second token
  or redirect scheme. Resolution records the customer click first, validates tenant/expiry, then
  redirects to the checkout. The customer binding remains in `customer_hook_links`, so DSR and
  send-frequency attribution keep working.
- Draft facts are fixed before the LLM: customer display name (optional), business name, cart value,
  item count and recovery-link reference. The LLM may choose only registered parameter values; it
  cannot invent discounts, scarcity, delivery promises, or cart contents. Grounding failure drops
  the candidate.

## 5. Templates and outcome evidence

Proposed registry stubs (no SID; Fazal owns Meta submission):

- `team_checkout_recovery_simple`, `en`: “Hi {{1}}, you left items in your checkout at {{2}}.
  Complete it here: {{3}}. Reply STOP to opt out.”
- `team_checkout_recovery_simple`, `hi`: “नमस्ते {{1}}, {{2}} पर आपका चेकआउट अधूरा रह गया है।
  इसे यहाँ पूरा करें: {{3}}। संदेश बंद करने के लिए STOP लिखें।”

Signature: `(customer_name, business_name, recovery_link)`. The recovery link is a short-lived
customer-bound `/r/<token>` minted by `mint_customer_hook_link`, not the Shopify checkout URL. Its
encrypted destination is expired/redacted under the same post-delivery/retention policy; the
non-sensitive click fact remains available to VT-741.
No offer is implied. A discount requires a separate owner-approved campaign policy and template.

Attribution joins the checkout attempt, draft/contact, `campaign_id` where present, delivery state,
VT-741 read state, and a later completed order. Delivered/read/completed are separate facts; a read
is not a conversion, and a completion is attributed only inside a predeclared window without an
intervening conflicting campaign.

## 6. Verification and real-API canary plan

Unit/real-PG coverage must prove: no consent means zero cohort; wrong-purpose/withdrawn/version-
unapproved consent means zero; duplicate/out-of-order webhooks do not duplicate or resurrect;
completion cancels; cross-tenant reads/writes fail; DSR leaves zero physical rows; specialist source
has no sender import/tool and executes `assert_agent_tools_safe`; Gate 0 uses the draft agent; Gate
4 uses the exact purpose/ledger/version; a global STOP withdraws every purpose and appends proof
events; `record_of_consent` can never be mutated by S2 capture; order-to-checkout cancellation uses
the stored Shopify join keys; the existing `hook_links` click reaches VT-741; direct or
Manager-approved plans still cannot bypass owner approval, VT-740, or VT-45.

CC runs the authored fail-not-skip canary against the synthetic Shopify dev store: verify OAuth
scope, create a bogus abandoned checkout, observe the real signed webhook, page it back through the
Admin API, prove **no draft without explicit S2 consent**, add synthetic purpose consent through the
capture seam, produce one grounded awaiting-approval draft, replay webhook/read and prove one draft,
complete the bogus order and prove cancellation. The canary performs real Shopify API egress but no
Twilio call, no customer send, no migration application by Codex, and no real customer data.

## 7. Review gates before implementation

The purpose-specific ledger is settled; extending `record_of_consent` is not an option. The wake is
owned by the existing DBOS scheduled-trigger substrate and defaults off. Agent draft batches remain
the canonical approval unit; `campaign_id` is attached only when an actual campaign exists. Fazal
owns Meta submission, the production notice-version allowlist and activation; the Compliance
specialist supplies the reviewed notice recommendation.

Before code begins, the remaining decisions are **Fazal's cross-agent contact-budget choice above**
and the abandonment-delay/attribution-window values. The real-API canary must also prove that the
App Proxy capture surface works on every Shopify plan admitted at launch; a plan that cannot prove
affirmative evidence remains ineligible. All safety defaults remain off/empty until those decisions
and Fazal's activation grant.
