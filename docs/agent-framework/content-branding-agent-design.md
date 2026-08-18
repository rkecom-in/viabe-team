# Content / Branding Agent — design checkpoint

**Status:** CC-approved; VT-768 implementation included in this PR, inert/default-off.
**Implementation base:** `origin/dev` at `cfdffd61aa86eb2b11b91772d6b1f0b8b42b0978`.
**Role:** Marketing specialist that produces reviewable content artifacts for the owner. It is not
a publisher, campaign sender, customer communicator, website editor or ads operator.

## 1. Contract and outputs

The Manager delegates a business objective, channel, audience description, supplied facts,
brand-voice profile and owner locale. The agent returns one or more immutable draft artifacts:

- social post/caption/carousel outline;
- landing or launch copy for **Viabe Market Intelligence**;
- report-promotion creative brief;
- WhatsApp Status copy (an artifact the owner can copy, never a WhatsApp send/template).

Every result carries `artifact_type`, `locale`, `register`, `headline`, structured text blocks,
call-to-action, supplied-fact references, omitted-claim warnings and a revision lineage. The Manager
evaluates the result against its objective; the owner publishes manually or hands the artifact to a
separately gated future publisher. This agent never addresses or selects an individual customer.

## 2. Brand voice, locale and facts

`BrandVoiceProfile` is tenant-scoped owner input: positioning, permitted product names, tone,
forbidden phrases, vocabulary, audience and examples. Missing voice input produces a neutral draft
labelled as such; the model cannot infer a “premium”, “trusted”, “leading” or similar claim.

Locale uses the canonical `owner_surface.owner_locale` value space:

| locale | register rule |
|---|---|
| `en` | clear Indian English; preserve supplied Hindi product/brand terms |
| `hi` | Devanagari Hindi; keep unavoidable product/technical names unchanged |
| `hinglish` | Hindi in Latin script; never silently switch to Devanagari |

Content locale is an explicit assignment input and may differ from the owner's UI locale only when
the owner/Manager asks for another audience register. Generated translations are sibling artifacts
with shared lineage, not a claim that one is an approved legal translation.

Facts obey the judgment-vs-citation and fabricated-zero laws. A numeric or performance claim may be
used only from a provided `ContentFact(value, unit, period, source_ref, measured_at)` input. Missing,
stale, disconnected or partial data is omitted or stated as unavailable; it is never rendered as
zero, “up to”, a remembered result, a category benchmark or a synthetic testimonial. Report claims
must bind to the supplied Reports fact/section identifier. Arithmetic derived from supplied facts is
labelled derived and carries its inputs.

## 3. Structurally sendless tool belt

The module is a pure ACF proposer with **no gated capability**. Its resolved ACF tool belt is empty;
`assert_agent_tools_safe` runs over that belt at import and registration. That capability-level
assertion is the primary guarantee. The import graph test below remains defence in depth.

The two logical operations are:

1. `compose_content_artifact` — pure structured composition/validation;
2. `store_content_draft` — tenant-scoped artifact persistence only.

It does not receive `REQUEST_CUSTOMER_SEND`, `REQUEST_BUSINESS_ACTION`, a `GateFacade` effect door,
Twilio, Resend, Meta/Google publishing clients, customer-ledger queries, phone/email/contact fields,
or `agent_drafts`. `agent_drafts` is deliberately forbidden: that table feeds the customer-send
rail, so using it would turn “stored copy” into an accidental send-adjacent object.

Persistence uses one shared tenant/RLS/FORCE-RLS `tenant_draft_artifacts` estate, discriminated by
artifact kind for Content and Ad Composer, with no recipient columns and no delivery state. The SQL
remains a proposal until CC allocates a migration and lands `_PURGE_ORDER` in that same change.
Until then the module returns an `UnpersistedArtifact` and structurally cannot claim storage.

Structural tests parse the module import graph and fail if it imports `customer_send`,
`twilio_send`, Twilio, Resend, an ads SDK, webhook transport or the send choke. A tool-catalog test
requires both tools to be `PROPOSE_DRAFT`/tenant-artifact operations and rejects every gated
capability. `assert_agent_tools_safe` executes at import and registration. Tests also prove that
aggregate inputs are accepted while customer-level rows/contact fields are rejected.

## 4. Draft lifecycle and self-check

```text
Manager objective + brand voice + aggregate/supplied facts
        -> content plan
        -> locale/register draft
        -> deterministic fact binding + forbidden-claim scan
        -> self-check
        -> stored draft artifact / ModuleResult
        -> Manager review
        -> owner manually publishes
```

The self-check records: objective fit, audience/channel fit, locale/register match, brand terms,
fact coverage, unsupported claims removed, CTA present, and claims requiring owner confirmation.
Failure yields a revision request or honest refusal, never padded copy.

## 5. Registry proposal and review gates

Proposed identity: `content_branding`; category `Marketing`; tags `content`, `brand-voice`,
`social-copy`, `landing-copy`, `creative-brief`, `whatsapp-status`; role `PROPOSER` only;
capability `PROPOSE_DRAFT`; narrow Marketing retrieval plus
`specialist:content_branding` customisation. Activation requires journey completion, ownership
verification and a brand-voice input; it does not require a customer-data connector.

The implementation ships the sendless module, proposed registry entry, live fact-binding validator,
prompt quarantine, locale fixtures, bogus-input tests and shared artifact-store SQL proposal. Tests
prove the capability and import guards RED using deliberately broken fixtures. Nothing is registered
live, published, sent, migrated or activated by Codex.
