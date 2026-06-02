# Data Processing Agreement (DPA) — Viabe Team

**DRAFT — NOT legally validated. Pending counsel review (Fazal).** Drafted by Cowork; not legal advice. Placeholders in `[…]`.

This DPA forms part of the Terms of Use between **the Business** ("Data Fiduciary"/Controller) and **RKeCom Services OPC Pvt Ltd** operating Viabe Team ("Data Processor", "we"). Governs processing of the Business's customers' personal data. **Effective:** on acceptance of the Terms · **Version:** v0-draft · **Law:** DPDP Act, 2023 (India).

## 1. Roles
The Business is the Data Fiduciary and determines the purposes/means of processing its customers' personal data. RKeCom processes that data **solely as the Business's Processor, on its documented instructions** (these Terms + in-product configuration). RKeCom is not a Fiduciary for that data.

## 2. Subject, nature, duration, purpose
See **Annex A**. In summary: processing customer phone numbers, messages, transaction/ledger and consent records, to operate WhatsApp messaging, records, and reporting for the Business, for the term of the Business's account.

## 3. Processor obligations
RKeCom shall: (a) process only on the Business's instructions and applicable law; (b) ensure persons authorised to process are bound by confidentiality; (c) implement the security measures in **Annex C**; (d) engage sub-processors only as in **Annex B**, with equivalent data-protection obligations flowed down, and notify the Business of changes; (e) assist the Business in responding to Data Principal requests (access/correction/erasure/grievance); (f) **notify the Business without undue delay on becoming aware of a personal-data breach** and assist with breach obligations; (g) on termination, delete or return customer personal data per the Business's choice, save where retention is legally required; (h) make available information needed to demonstrate compliance and allow reasonable audit [scope/frequency: counsel].

## 4. Data Fiduciary obligations
The Business shall: ensure it has a **lawful basis** (incl. customer opt-in for WhatsApp messaging) for the data it provides/processes; issue only lawful instructions; honour opt-outs; comply with DPDP, WhatsApp/Meta policy, and TRAI/DLT (SMS).

## 5. Cross-border transfer
Customer data may be processed by sub-processors outside India (**Annex B**), consistent with DPDP cross-border provisions; production hosting is India (Mumbai) [confirm VT-231]. [Counsel: confirm transfer mechanism + any restricted-country list.]

## 6. Liability & indemnity
[Counsel to set, consistent with the Terms' liability framework and Indian law.]

## 7. Precedence
On conflict regarding personal-data processing, this DPA prevails over the Terms.

---

## Annex A — Details of processing
- **Categories of Data Principals:** the Business's customers.
- **Categories of personal data:** phone number (tokenised where feasible), WhatsApp message content, transaction/ledger entries, consent/opt-out records.
- **Special categories:** none intended.
- **Nature/purpose:** WhatsApp messaging on the Business's behalf to opted-in customers; record-keeping; report generation.
- **Duration:** term of the Business's account + legally required retention.

## Annex B — Sub-processors
| Sub-processor | Purpose | Data reached | Location |
|---|---|---|---|
| Anthropic | AI assistant (reasoning, drafting) | message/context (per consent) | [US/region] |
| Sarvam | Indian-language voice/text (STT) | voice/text content | [India/region] |
| Twilio | WhatsApp + SMS delivery | phone number, message | [region] |
| Voyage *(PLANNED — not currently active)* | search / embeddings | none currently | — |
| Supabase | secure database/storage | all stored fields | dev: Seoul (ap-northeast-2); prod: Mumbai pending VT-231 |
| Apify | public-listing context (aggregate) | no customer PII stored | [region] |

[RKeCom/counsel to confirm each sub-processor's processing location + that DPAs are in place with each.]

## Annex C — Security measures
Encryption in transit; tokenisation of customer phone numbers in consent records; Fernet encryption-at-rest of phone numbers in the resolution store (`phone_token_resolutions`). Operational customer records store the phone number to enable messaging, protected by **tenant isolation (row-level security) and access controls**, not column-level encryption. Least-privilege access + audit logging throughout; secret management; consent-gated transmission to sub-processors; breach-response process. *(Column-level encryption of `customers.phone_e164` is a hardening candidate — see launch-tracker.)* [Security/counsel to validate the representations.]
