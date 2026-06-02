# Privacy Policy — Viabe Team

**DRAFT — NOT legally validated. Pending counsel review (Fazal).** Placeholders in `[…]` are for RKeCom to supply. Drafted by Cowork; not legal advice.

**Entity:** RKeCom Services OPC Pvt Ltd ("RKeCom", "Viabe", "we"), operating the product **Viabe Team**, viabe.ai/team.
**Effective:** [date] · **Version:** v0-draft · **Governing law:** India (Digital Personal Data Protection Act, 2023 — "DPDP").

## 1. Scope & our two roles
This policy covers personal data we handle in two distinct capacities:
- **As a Data Fiduciary (controller)** — for the **business owners** who sign up for Viabe Team (account/contact data, usage).
- **As a Data Processor** — for the **customers of those businesses**, whose personal data we process **on the business's instructions**. For that data the **business is the Data Fiduciary**; we act only as its processor. (See the Data Processing Agreement.)

## 2. Data we collect
- **Owner/account data:** name, business name, WhatsApp/business contact number, email, login identifiers, billing details [if/when billing], usage logs.
- **Business-customer data (as processor):** customer phone number (stored tokenised where feasible), messages, transaction/ledger entries, consent records — only as the business provides or directs.
- We do **not** intentionally collect special-category data or data of children; the service is not directed to children.

## 3. Purposes
Operate the Viabe Team service; authenticate owners; generate reports/insights for the owner; send WhatsApp messages on the business's behalf to its opted-in customers; provide support; security, fraud-prevention, and legal compliance.

## 4. Lawful basis (DPDP)
- Owner data: the owner's **consent** at sign-up and our **legitimate uses** to provide the service.
- Business-customer data: processed under the **business's** lawful basis — the customer's **opt-in consent** (WhatsApp inbound/QR) and/or the business's existing customer relationship (owner-provided inputs). We process strictly on the business's instructions.

## 5. Sub-processors & cross-border transfer
We use vetted sub-processors to deliver the service; personal data may be processed by them, including outside India:
Anthropic (AI assistant), Sarvam (Indian-language voice/text), Twilio (WhatsApp/SMS delivery), Supabase (secure storage), Apify (public-listing context). *(Voyage — search/embeddings — is **planned, not currently active**; do not list as a live sub-processor until it actually processes data.)* Production data will be hosted in **India (Mumbai)**; **until VT-231 closes, no real customer data is processed** (dev is in Seoul under a synthetic-only constraint, CL-422). Update this statement to reflect live hosting at go-live. Transfers are made consistent with DPDP's cross-border provisions. The current sub-processor list is maintained in the DPA's sub-processor schedule.

## 6. Retention
Owner data: for the life of the account plus any period required by law. Business-customer data: for the duration of the business's relationship with the customer, until opt-out or an erasure request, then deleted/anonymised (per the DPA).

## 7. Security
Encryption in transit; tokenisation of customer phone numbers in consent records; Fernet encryption-at-rest of phone numbers in the resolution store (`phone_token_resolutions`). Operational customer records store the phone number to enable messaging, protected by **tenant isolation (row-level security) and access controls**, not column-level encryption. Access controls + audit logging throughout. *(Column-level encryption of `customers.phone_e164` is a hardening candidate — see launch-tracker.)* [Counsel/security to confirm the representations made here.]

## 8. Your rights (DPDP Data Principal rights)
Access, correction, completion, and erasure of your personal data; grievance redressal; nomination. **Business customers** exercise these against the **business** (the fiduciary); we assist the business in fulfilling them. To make a request: [contact].

## 9. Grievance Officer
[Name], [email], [address] — RKeCom to appoint and publish per DPDP. Response within statutory timelines.

## 10. Changes
We may update this policy; material changes will be posted here with a new effective date/version.

## 11. Contact
RKeCom Services OPC Pvt Ltd, [registered address], [email].
