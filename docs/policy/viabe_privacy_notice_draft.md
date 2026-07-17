# Viabe.ai — Privacy Notice

**LAWYER-FACING WORKING DRAFT — NOT FOR PUBLICATION**

> **Status:** Draft v0.1, prepared by Viabe (AI-assisted) for review by DPDP-competent
> counsel. This document is **not** a final or signed privacy notice. It is a structured
> starting point that states Viabe's data flows as currently known, so counsel can harden
> the language and close the gaps.
>
> **How to read the annotations:**
> - `[CONFIRM]` — a factual claim that the reviewer/Fazal should verify against the system before sign-off.
> - `[UNVERIFIED — VT-4 pending]` — a data flow that is not yet built; description is provisional and will change.
> - `[LEGAL]` — a point flagged specifically for counsel judgment (legal basis, wording, regulatory choice).
> - `[DRAFTING NOTE]` — internal note explaining a choice; to be deleted before publication.
>
> **Regulatory scope of this draft:** India / Digital Personal Data Protection Act, 2023 (DPDP) only.
> GDPR and other regimes are deliberately out of scope per the locked Phase 1 India-only launch plan.
> `[LEGAL]` Confirm no EU/UK data subjects are expected at launch; if they are, scope must widen.

---

## 1. Who we are

Viabe.ai ("Viabe", "we", "us") provides software services to small and medium businesses,
including location-intelligence reports and an automated Sales Recovery Agent that
communicates with a business's own customers on the business's behalf.

`[CONFIRM]` Legal entity name, registered address, and the operator's role.
`[LEGAL]` Under DPDP, Viabe is likely a **Data Processor / Data Fiduciary** depending on
the product. For the Sales Recovery Agent, the SME customer is plausibly the Data Fiduciary
for their end-customers' data, and Viabe processes on their behalf. Counsel must determine
and state the controller/processor (Fiduciary/Processor) split precisely — this drives
every consent and DSR obligation below.

`[CONFIRM]` Grievance/Data Protection Officer contact details — name, email. DPDP requires
a published contact for the Data Fiduciary. This must be filled before publication.

---

## 2. What this notice covers

This notice describes how Viabe handles personal data across the **whole product**, not a
single feature. The components that touch personal data are:

- The **Orchestrator** and message pipeline that receive and route inbound messages.
- The **Sales Recovery Agent** that analyses customer history and drafts outreach. `[UNVERIFIED — VT-4 pending]`
- The **Composer** that produces message content.
- **owner_inputs**, an optional feature that records business-owner-supplied context.
- The **durability layer (DBOS)** that allows the system to recover from crashes.
- Third-party services that receive data to perform their function: **Anthropic, Twilio, Voyage.**

`[DRAFTING NOTE]` The notice is deliberately system-level. An earlier framing treated it as
a sub-feature of owner_inputs; that was corrected — DPDP attaches to the customer's message,
so every component handling it is in scope regardless of any feature flag.

---

## 3. What personal data we process

`[CONFIRM]` This list should be reconciled against the actual database schema before sign-off.

- **Message content** — the text of messages exchanged between a business and its customers,
  including inbound customer messages.
- **Contact identifiers** — phone numbers and message/thread identifiers (e.g. Twilio message SIDs).
- **Business-owner-supplied context** — information a business owner enters about their
  business, captured by the owner_inputs feature as structured intent.
  `[CONFIRM]` owner_inputs stores **structured intent only, not raw message bodies** — verify
  this still holds at launch (it is asserted by an automated test today).
- **Derived data** — vector embeddings and a knowledge graph generated from message content,
  used to retrieve relevant context. `[CONFIRM]` Confirm embedding/knowledge-graph tables in scope.

`[UNVERIFIED — VT-4 pending]` The Sales Recovery Agent's full input set is not yet finalised
in code. This section will need revision once VT-4 ships.

---

## 4. How we use personal data, and our third-party processors

Viabe relies on a small number of third-party services to operate. These are **mandatory**
to the service — they are not optional add-ons, and the service cannot function without
them. Their use is disclosed here and is **consent-gated**: a business agrees to them as a
condition of using Viabe.

`[LEGAL]` Counsel to confirm DPDP legal basis. The locked product decision is that these
exchanges are consent-gated and mandatory. Counsel must confirm that "consent as a condition
of service" is valid under DPDP for each, or whether another lawful basis applies, and word
this section accordingly.

### 4.1 Anthropic — AI inference

Viabe sends message content to Anthropic's AI models to (a) classify incoming messages and
(b) generate the Sales Recovery Agent's analysis and drafted outreach.

`[CONFIRM]` Raw, un-redacted customer message content **is transmitted** to Anthropic — both
to the lighter classification model and to the agent model. The notice must say this plainly;
it must not imply only redacted or derived data is sent.

- **Purpose:** inference only — classification and text generation.
- **No training:** message content is **not** used to train Anthropic's models. `[CONFIRM]`
  This is supported by a Data Processing Agreement with Anthropic. Counsel should review the DPA.
- `[DRAFTING NOTE]` Do **not** use any "fine-tuned for your business" or "learns your business"
  phrasing anywhere in customer-facing copy. It is inaccurate and creates a false impression
  of training. This is a hard constraint.

### 4.2 Twilio — message transport

Twilio (including WhatsApp via Twilio) is the transport channel for messages between a
business and its customers.

- **Purpose:** delivering and receiving messages — transport only.
- `[CONFIRM]` Twilio's message "Body" field is dropped at ingress and not persisted by Viabe
  beyond transport. Verify this body-drop still holds.

### 4.3 Voyage — embeddings

Viabe sends message content to Voyage AI to generate vector embeddings, which power context
retrieval (the knowledge graph).

`[CONFIRM]` Raw customer message content **is sent** to Voyage. The resulting embedding
vectors are stored by Viabe and are treated as personal data (they are derived from, and
linkable to, the customer's message). The notice must disclose this transmission explicitly.

- **Purpose:** generating embeddings for context retrieval — inference only, no training. `[CONFIRM]`

### 4.4 DBOS — crash recovery (durability layer)

Viabe uses a durability layer (DBOS) so that if the system crashes mid-processing it can
resume without losing a message.

`[CONFIRM]` For crash recovery to work, the durability layer **temporarily retains the raw
message body**. This retention is **bounded**: approximately **2.5 hours worst-case**, after
which the record is purged automatically.

- `[DRAFTING NOTE]` The notice **must not** claim the message body is "never stored" or
  "never persisted." It is briefly persisted in the durability layer. The accurate statement
  is a short, bounded, automatically-purged retention window for crash recovery. Getting this
  wrong would make a signed legal document false.

---

## 5. Data retention

- **Durability layer (crash recovery):** raw message body retained ~2.5 hours worst-case,
  then automatically purged. `[CONFIRM]`
- **owner_inputs (structured business context):** retained for the lifetime of the business's
  relationship with Viabe; purged on offboarding or on a verified data-subject request. `[CONFIRM]`
- **Message content, embeddings, knowledge graph:** `[CONFIRM]` Retention period for the
  general pipeline and derived-data tables is **not yet specified**. Counsel and Fazal must
  set and state a definite retention period — DPDP requires data not be kept longer than
  necessary. This is an open gap.
- `[UNVERIFIED — VT-4 pending]` Sales Recovery Agent working data retention — to be specified
  once VT-4 ships.

`[LEGAL]` DPDP expects a stated, justifiable retention period for each category. The
"[CONFIRM]" gaps above are the main thing this draft cannot resolve on its own.

---

## 6. Consent

`[CONFIRM]` The consent-capture mechanism lives in the business-owner-facing flow and is
being built as a separate launch-gating item. This section must describe the **actual**
mechanism as built — what the owner is shown, what they agree to, and how agreement is recorded.

`[LEGAL]` DPDP requires consent to be free, specific, informed, unconditional, and
unambiguous, with a clear withdrawal path. Counsel must:
- Confirm the consent flow meets the DPDP standard.
- Address the end-customer dimension: the SME's own customers also have message data
  processed. Counsel must determine whose consent covers them and how that is represented
  (this is the Fiduciary/Processor question from Section 1, surfacing again here).

---

## 7. Data-subject rights and requests (DSR)

A business (and, as applicable, its customers) may request access to, correction of, or
erasure of personal data.

`[CONFIRM]` Viabe operates an automated purge process for erasure requests. As currently
built it covers 12 data tables, including owner_inputs and the knowledge-graph/embedding
tables. The tenant (business) record itself is **anonymised** rather than hard-deleted,
because other records (e.g. an append-only audit log required for compliance) reference it.

`[LEGAL]` Counsel to confirm:
- That **anonymisation** of the tenant record (rather than deletion) is an acceptable way to
  satisfy a DPDP erasure right, given the linked append-only audit log.
- That the append-only privacy audit log itself is defensible to retain after an erasure
  request, and on what basis.
- The response timeline Viabe commits to for DSRs, and how requests are made (the contact
  from Section 1).

`[DRAFTING NOTE]` The durability layer is deliberately **excluded** from the DSR purge scope —
its records self-purge within ~2.5 hours, so a separate erasure step is unnecessary. Counsel
should confirm this reasoning is sound to state.

---

## 8. Data security

`[CONFIRM]` This section is a placeholder. It should describe, at a level appropriate for a
customer-facing notice, the security measures actually in place — tenant data isolation
(row-level security), access controls, and so on. Fazal/counsel to supply accurate detail;
do not overstate.

---

## 9. Changes to this notice

`[LEGAL]` Standard clause — counsel to supply. Should state how customers are notified of
material changes.

---

## 10. Contact

`[CONFIRM]` Grievance officer / Data Protection contact — see Section 1. Must be a real,
monitored channel before publication.

---

### Appendix A — Data-flow summary (for counsel; not part of the published notice)

| Recipient | Data sent | Raw body? | Purpose | Training? | Retention | Status |
|-----------|-----------|-----------|---------|-----------|-----------|--------|
| Anthropic (classify) | Inbound message | Yes `[CONFIRM]` | Message classification | No `[CONFIRM]` | Per Anthropic DPA | Locked |
| Anthropic (agent) | Customer history + message | Yes `[CONFIRM]` | Agent analysis + drafting | No `[CONFIRM]` | Per Anthropic DPA | `[UNVERIFIED — VT-4 pending]` |
| Twilio / WhatsApp | Message + phone number | Transport | Message delivery/receipt | N/A | Body dropped at ingress `[CONFIRM]` | Locked |
| Voyage | Message content | Yes `[CONFIRM]` | Embeddings for retrieval | No `[CONFIRM]` | Embeddings stored by Viabe | Locked |
| DBOS (durability) | Full message body | Yes | Crash recovery | N/A | ~2.5h worst-case, auto-purge `[CONFIRM]` | Locked |
| Viabe owner_inputs | Structured business context | No (structured intent only) `[CONFIRM]` | Owner-supplied context | N/A | Lifetime of relationship; purge on offboarding/DSR | Locked |

### Appendix B — Open items counsel must close before sign-off

1. Controller/processor (Fiduciary/Processor) determination for each product, esp. the SME's end-customers' data.
2. Legal basis: validity of "consent as a condition of service" under DPDP for the mandatory third-party exchanges.
3. Retention periods for pipeline message content, embeddings, and knowledge graph — currently unspecified.
4. Acceptability of tenant-record anonymisation (vs deletion) as DPDP erasure compliance.
5. Defensibility of retaining the append-only privacy audit log post-erasure.
6. DSR response timeline commitment.
7. Grievance officer / DP contact appointment and publication.
8. Sales Recovery Agent (VT-4) data handling — re-review once built.
9. Confirm India-only scope; widen if EU/UK data subjects are expected.
10. Security section (Section 8) factual content.
