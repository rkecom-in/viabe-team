# S2 — abandoned-checkout recovery specialist: design checkpoint

**Status:** DESIGN ONLY — implementation waits for CC review.

**Base:** `origin/dev` at `a2a89ffd53bee9544a52b659ea60fd193a6a392c` (2026-08-13).

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
`AGENT_TOOLS = ()`; neither its connector reader nor its brain imports a transport. Every eventual
send must continue through `agent_send_draft`, the VT-740 per-recipient delivery-frequency veto,
and the VT-45 transport choke. The current VT-741 read state and the now-populated
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

The proposed migration (number supplied by CC; SQL is not part of this checkpoint) adds two
tenant-scoped tables in the same migration:

- `commerce_consent_grants`: current state keyed by `(tenant_id, phone_token, channel, purpose)`,
  with `notice_version`, `locale`, `capture_surface`, `source_system`, opaque source reference,
  affirmative-action timestamp, withdrawal timestamp, and evidence hash. No raw phone or checkout
  URL is stored.
- `commerce_consent_events`: append-only grant/reaffirm/withdraw history with actor/source, reason,
  timestamp and idempotency key. It is the proof trail; the current row is the fast gate.

Both tables require `tenant_id`, RLS + FORCE RLS, same-migration `_PURGE_ORDER` registration, and a
hard-delete canary asserting physical zero after DSR. Webhook/API replays are idempotent. A later
event may withdraw a purpose; a global customer STOP remains a stronger veto across all purposes.
Re-consent creates an attributable event and may reactivate only the purpose explicitly selected.

The checkout capture contract is an unticked, purpose-specific merchant checkout control whose
copy/version is registry-owned. Its signed payload supplies the phone, purpose, notice version,
locale, source reference and affirmative timestamp to a Viabe capture endpoint. That endpoint
verifies the Shopify shop/tenant binding and signature before tokenising the phone and writing the
ledger. If the merchant's Shopify surface cannot supply verifiable affirmative evidence, S2 stays
ineligible for that checkout; there is no fallback inference.

At both cohort time **and send time**, a policy registry maps the persisted draft agent to its exact
purpose and allowed notice versions. Unknown agent, purpose, or empty production allowlist fails
closed. This replaces the current Sales-Recovery-specific Gate-4 lookup without weakening it.

## 3. Trigger, state, and cohort

The connector gains a dedicated paginated abandoned-checkout read using the existing offline OAuth
token and read scopes; it does not reuse `pull_full`, which currently returns customers only. The
verified `checkouts/create`/`checkouts/update` webhook stops acknowledging-and-discarding and instead
upserts the same normalized checkout state. Required state is minimal: tenant, Shopify checkout ID,
customer link/phone token, created/updated/completed timestamps, currency, total paise, item count,
opaque recovery-link ciphertext/reference, and source event ID. Line-item prose, addresses, and raw
payloads are not retained.

Eligibility is deterministic and re-checked immediately before drafting:

1. checkout is incomplete and older than the configured abandonment delay;
2. phone/customer identity resolves inside the tenant;
3. active `checkout_recovery` consent predates the proposed contact;
4. no prior S2 draft/contact exists for this checkout attempt;
5. customer is subscribed, complaint-clear, and not globally opted out;
6. VT-740 universal delivery-frequency and existing agent contact caps can only veto.

An order/completion webhook tombstones the opportunity. A late or out-of-order checkout event cannot
resurrect it: Shopify `updated_at` plus terminal completion state wins. Detection and persistence are
idempotent on `(tenant_id, shopify_checkout_id, checkout_attempt_version)`; re-drive creates no new
customer contact opportunity for the same attempt.

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

Signature: `(customer_name, business_name, recovery_link)`. The recovery link is used only at
draft/send time and must be redacted after delivery under the existing draft redaction path.
No offer is implied. A discount requires a separate owner-approved campaign policy and template.

Attribution joins the checkout attempt, draft/contact, `campaign_id` where present, delivery state,
VT-741 read state, and a later completed order. Delivered/read/completed are separate facts; a read
is not a conversion, and a completion is attributed only inside a predeclared window without an
intervening conflicting campaign.

## 6. Verification and real-API canary plan

Unit/real-PG coverage must prove: no consent means zero cohort; wrong-purpose/withdrawn/version-
unapproved consent means zero; duplicate/out-of-order webhooks do not duplicate or resurrect;
completion cancels; cross-tenant reads/writes fail; DSR leaves zero physical rows; specialist source
has no sender import/tool; Gate 0 uses the draft agent; Gate 4 uses the exact purpose; direct or
Manager-approved plans still cannot bypass owner approval, VT-740, or VT-45.

CC runs the authored fail-not-skip canary against the synthetic Shopify dev store: verify OAuth
scope, create a bogus abandoned checkout, observe the real signed webhook, page it back through the
Admin API, prove **no draft without explicit S2 consent**, add synthetic purpose consent through the
capture seam, produce one grounded awaiting-approval draft, replay webhook/read and prove one draft,
complete the bogus order and prove cancellation. The canary performs real Shopify API egress but no
Twilio call, no customer send, no migration application by Codex, and no real customer data.

## 7. Review gates before implementation

CC should decide only these design seams before code begins: table/event shape versus extending
`record_of_consent`; exact checkout capture mechanism supported across the launch Shopify plans;
the abandonment delay and attribution window; whether S2 drafts use agent batches or campaigns for
the canonical `campaign_id`; and the production notice-version/Meta-category approval owner. All
safety defaults remain off/empty until those decisions and Fazal's activation grant.
