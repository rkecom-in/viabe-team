# Ad Composer Agent — design checkpoint

**Status:** CC-approved; VT-769 implementation included, inert/default-off.
**Implementation base:** stacked on VT-768 at `3e2ac7494515cd033cf0ff9c586feddf69c4dc55`.

## Job and constitutional boundary

The Ad Composer turns an owner objective, aggregate business inputs, approved Content/Branding
artifacts, and an owner-supplied budget boundary into a complete **campaign proposal** for Meta or
Google. The proposal includes objective, audience, placement/channel hypothesis, budget
recommendation, creative mapping, destination-link specification, success metric, measurement
plan, and kill criterion.

It is structurally incapable of publishing, editing, pausing, funding, or reading an advertising
account. The owner publishes manually. The specialist never receives Meta/Google credentials,
write-scoped SDKs, customer rows, uploaded audiences, phone numbers, or the send choke.

## Inputs and outputs

Required inputs are tenant ID, platform, business objective, offer/funnel facts, target geography,
owner-approved minimum/maximum budget, campaign dates, aggregate historical metrics (optional),
approved content artifact IDs, destination route, and locale/register. `owner_locale` follows the
canonical `en | hinglish | hi` convention: `hinglish` is Hindi/mixed copy in Latin script, never
accidental Devanagari.

The proposal is an inert, versioned artifact with:

1. one primary objective and one named success metric;
2. audience inclusion/exclusion hypotheses expressed without sensitive traits;
3. platform/campaign structure and manual setup checklist;
4. budget and pacing recommendation, with every arithmetic input shown;
5. creative placements that reference approved Content/Branding artifact versions;
6. destination-link request plus canonical UTM values;
7. attribution limits and data that are not yet connected;
8. an explicit kill criterion of the form “after ₹X spend with fewer than Y attributed events,
   stop/review”; and
9. owner publication state, initially `draft_only`.

A missing budget, destination, success metric, kill threshold, or creative artifact makes the
proposal invalid rather than “best-effort complete.” A budget recommendation may calculate from
provided CPC/CVR/AOV assumptions, but may not invent benchmarks or describe an absent metric as
measured.

## Tool belt: sendless and ads-write-free by construction

The resolved ACF tool belt is empty and is checked by `assert_agent_tools_safe` at import and
registration; this capability-level assertion is the primary sendless/ads-write-free guarantee.
Logical operations are reading already-supplied aggregate inputs and approved artifacts, composing,
validating and returning a proposal. The proposer receives no effect tool and performs no storage.

The module must not import or be injected with `customer_send`, `agent_send_draft`, Twilio, Resend,
Meta Marketing API, Google Ads API, OAuth token stores, customer/recipient repositories, audience
uploaders, or any generic HTTP client. The proposal store is separate from `agent_drafts`, because
`agent_drafts` is a send-adjacent lifecycle surface. Storage is tenant-scoped, RLS + FORCE-RLS,
DSR-registered in the same future migration, and cannot contain raw customer data.

Tests prove the guard RED with deliberately broken send/ads imports and a gated tool fixture. They
also reject customer-level data and outputs missing metric, kill criterion, destination request,
attribution caveat or the immutable manual-publication boundary.

## Destination links and the current seam

The composer does not mint or fabricate a URL. It emits a typed `TrackedDestinationRequest` with
an allowlisted destination route and canonical UTM fields (`utm_source`, `utm_medium`,
`utm_campaign`, `utm_content`); the control plane resolves that request to an owned link.
User-supplied full URLs and arbitrary query parameters are rejected.

The current `integrations/hook_links.py` seam on `origin/dev` is **not** the required ad-destination
seam: `/r/<token>` resolves to a tenant's WhatsApp number, and the customer-bound variant represents
an identified recipient. An ad click needs an aggregate, campaign-bound redirect to a Reports
landing route—no customer binding and no WhatsApp redirect. Reusing the existing hook mint would
misroute the click and muddle privacy classes. Therefore implementation must wait for, or consume,
CC's generalized tracked-destination interface. If that interface is not present at build time,
the agent may store only the unresolved request and must fail closed before calling a non-existent
mint.

## Claims, measurement and self-check

Claims about Viabe Reports, price, turnaround, customers, conversion, market size, or performance
must be supplied as source-bound input facts. The agent may rephrase an approved fact without
changing its predicate, time period or denominator. It may not manufacture social proof, a zero, a
percentage, or a “best-performing” label. Content whose evidence has expired is rejected for
refresh.

`validate_campaign_candidate` is the live runtime gate (not the eval-only advice helper). It checks:

- exactly one primary success metric and its event source;
- a kill rule with ₹ spend, event threshold and decision action;
- budget inside the owner-approved range and reproducible arithmetic;
- every creative ID/version exists and is approved for the chosen register;
- all quantitative claims bind to supplied facts;
- UTM components are normalized and contain no PII;
- no customer list, phone, email or sensitive targeting trait; and
- publication/effect authority are class constants, not model-populated instance fields.

Retrieval, content approval, a high self-check score, or an owner-approved proposal never authorizes
the advertising effect. Manual publication remains outside this agent and outside its tools.
