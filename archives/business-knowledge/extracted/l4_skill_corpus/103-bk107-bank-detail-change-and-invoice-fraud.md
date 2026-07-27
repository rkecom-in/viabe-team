---
title: "Payment fraud: verify beneficiary and credit changes outside the requesting channel"
authored_by: codex_public_source_extraction
tags: [business_email_compromise, invoice_fraud, beneficiary_change, dual_approval, vendor_fraud]
priority: 4
version: 1
---

*Executional scenario — extracted from archived public sources for RAG grounding. Validate mutable law/platform policy before deterministic action.*

## Applies to

Agents: finance_agent, compliance_agent, sales_agent, manager_coo

Domains: finance, compliance, sales, manager_coo

Source type: official_incident_scenarios_and_reporting

Trust level: high

## Situation

A familiar vendor, customer, executive, employee, lawyer, or new account sends urgent bank-detail, beneficiary, purchase-order, credit-term, refund, or payment instructions.

## Decision pressure

The message looks authentic and urgency discourages independent verification; replying or calling details inside the same message keeps the attacker inside the control loop.

## Mistake or risk

Do not rely on display name, thread history, attached letterhead, caller ID, invoice, or the requesting channel. IC3 cases are US incident intelligence, not Indian prevalence estimates.

## Recommended next action

Freeze the change; compare exact domain and master data; independently call a previously verified number; require maker-checker approval; apply a cooling period and low test limit for changed beneficiaries; verify new customer POs and credit through the organization's main contact; rehearse rapid bank recall, evidence preservation, and India-specific reporting escalation.

## Evidence needed

- Original message headers/domain and change request
- Previously verified contact and independent callback record
- Master-data change and maker-checker approvals
- Beneficiary age, test payment, limit, and cooling period
- PO/customer identity and credit verification
- Bank recall, incident, legal, insurer, and regulator escalation path

## Red flags / escalation triggers

- Secrecy or urgency from a senior-looking sender
- Slightly altered domain or reply-to address
- Bank change immediately before a large payment
- Verification uses a number supplied in the same request
- New customer seeks large Net-30/60 goods before independent verification

## Agent lesson

Finance controls beneficiary and payment changes; Sales verifies customer identity and credit; Compliance owns incident escalation; the Manager supports controls even under executive urgency.

## Hard-gate candidate

Bank, beneficiary, refund, PO, and material credit changes require independent known-channel verification, dual approval, and logged evidence.

## Retrieval triggers

- vendor changed bank account
- CEO urgent payment email
- invoice redirection fraud
- fake purchase order
- business email compromise

## Provenance

Source URL: https://www.ic3.gov/PSA/2023/PSA230324

Local source file: `archives/business-knowledge/research/compliance/ic3_vendor_invoice_fraud_2023.html`

Retrieved at: 2026-07-26
