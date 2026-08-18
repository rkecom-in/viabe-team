# Acquisition Prospector — first-proof design

**Status:** design and research artifact only; no activation or registry entry.
**Base:** `origin/dev` at `bf352beb984d4ad4de476f1796a93247206587c0` (2026-08-19).

## Purpose and boundary

The Acquisition Prospector finds public evidence of pre-launch Indian food-and-beverage businesses
that could benefit from a Viabe Feasibility report. It produces research artifacts: a scored list,
an evidence trail, an advertising-target brief, and draft cold-email copy. It cannot contact a
prospect, schedule outreach, create a send draft, or invoke a transport.

This is a research specialist, not an outreach agent. A public phone number is never a WhatsApp
consent basis. Phone values are therefore excluded from the first-proof artifact and must remain
`NOT_CONTACTABLE` unless a separate governed consent record exists.

## Inputs and output contract

Accepted public signals are:

- a recent official or founder-owned “opening soon,” pilot, waitlist, incorporation, hiring, or
  launch page;
- a government registration or licence grant when a lawful public lookup supports it;
- a commercial-kitchen, franchise, or pre-opening listing with an attributable operator; or
- a reputable report of a named opening, retained as secondary evidence and scored below an
  operator-owned signal.

Every prospect row contains: stable prospect key, business/operator, city, category, stage,
evidence URL, evidence class, access date, contact channel availability, why-now, score, and a
revalidation flag. “Opening soon” is not permanent truth: a signal older than 90 days is labelled
`launch_signal_revalidate`, never silently represented as current pre-launch state.

The score is a prioritisation aid, not a factual claim:

- 0–35 stage strength (live waitlist/pilot beats an old announcement);
- 0–25 match to a founder-led Indian F&B launch;
- 0–20 source directness and recency;
- 0–10 usable non-phone contact channel; and
- 0–10 evidence completeness.

Rows below 60 stay research-only. Scores never authorize contact.

## Tool belt: sendless by construction

Proposed tools are limited to `search_public_launch_signals`, `normalise_prospect_evidence`,
`score_prospect`, and `store_research_artifact`. The module must not import or receive
`customer_send`, `agent_send_draft`, Twilio, Resend, Meta Ads write clients, recipient ledgers, or
customer contact tables. Cold-email text is an inert text artifact for a future owner-approved
workflow; this agent cannot hand it to Resend.

The eventual implementation must include AST/import tests that fail if the module imports a send
choke or a network client with write scope, capability tests that its tool belt contains no gated
effect, and fixtures proving a discovered phone number is suppressed from the stored artifact.

## Provenance and honesty rules

Each row is claim-scoped to its evidence. Search snippets may locate a source, but the evidence URL
must resolve to the page carrying the signal. A company page establishes only what it says; it does
not establish revenue, budget, founder intent, or current launch state. Missing facts remain
`unknown`. No synthetic zeros, inferred contact permission, or guessed proprietor names.

Revalidation is a new read-only research pass. It may change a stage from pre-launch to launched,
stale, closed, or unknown; it must retain the prior evidence and timestamp rather than rewrite
history.

## CTWA and email hand-off

The CTWA brief is an aggregate persona proposal for the Ad Composer; it contains no prospect rows or
lookalike seed. Cold-email drafts are generic copy. Before any use, a separate owner-approved agent
must establish a lawful contact policy, suppression/unsubscribe handling, sender identity, rate
limits, and the applicable effect gates. Nothing retrieved or scored here authorizes that effect.

