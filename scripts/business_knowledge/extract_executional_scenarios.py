#!/usr/bin/env python3
"""Generate executional business-knowledge scenario cards for L4/RAG ingestion.

This is intentionally stdlib-only. The repo's current RAG seam is the L4 skill
corpus loader (`orchestrator.knowledge.l4_corpus`): one Markdown file with YAML
frontmatter + a body, then `scripts/l4_seed.py <dir>` embeds/UPSERTs the docs
into `l4_documents`.

We do NOT write into `apps/team-orchestrator/skill_corpus` here because that
directory is a locked Fazal-authored corpus with exact-count tests. Instead this
script writes a parallel L4-compatible corpus under:

  archives/business-knowledge/extracted/l4_skill_corpus/

and a structured JSONL copy under:

  archives/business-knowledge/extracted/scenario_cards/executional_scenarios.jsonl

The scenario text is extracted/summarised from archived public sources. It is
RAG grounding, not deterministic legal enforcement.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

try:
    from scripts.business_knowledge.historical_business_cases import HISTORICAL_BUSINESS_CASES
except ModuleNotFoundError:  # Direct `python scripts/business_knowledge/...` execution.
    from historical_business_cases import HISTORICAL_BUSINESS_CASES


REPO_ROOT = Path(__file__).resolve().parents[2]
L4_OUT = REPO_ROOT / "archives/business-knowledge/extracted/l4_skill_corpus"
JSONL_OUT = REPO_ROOT / "archives/business-knowledge/extracted/scenario_cards/executional_scenarios.jsonl"
AGENT_INDEX_OUT = REPO_ROOT / "archives/business-knowledge/extracted/agent_indexes"
RETRIEVED_AT = "2026-07-26"


@dataclass(frozen=True)
class ScenarioCard:
    slug: str
    title: str
    source_type: str
    trust_level: str
    domains: list[str]
    agent_targets: list[str]
    tags: list[str]
    source_url: str
    local_file: str
    situation: str
    decision_pressure: str
    mistake_or_risk: str
    recommended_next_action: str
    evidence_needed: list[str]
    red_flags: list[str]
    agent_lesson: str
    hard_gate_candidate: str
    retrieval_triggers: list[str]


SCENARIOS: list[ScenarioCard] = [
    ScenarioCard(
        slug="bk001-gst-credit-note-notice-evidence-pack",
        title="GST credit-note notice: build the evidence pack before arguing law",
        source_type="forum_operator_voice",
        trust_level="medium",
        domains=["compliance", "finance", "manager_coo"],
        agent_targets=["compliance_agent", "finance_agent", "manager_coo"],
        tags=["gst", "credit_note", "notice", "evidence", "compliance", "manager_coo"],
        source_url="https://www.caclubindia.com/forum/supporting-documents-required-for-gst-notice-on-credit-notes-8211-fy-2021-8211-22-613780.asp",
        local_file="archives/business-knowledge/executional/forums/caclubindia_gst_notice_credit_notes_2025.html",
        situation=(
            "A business receives a GST notice questioning reduction of output tax liability through "
            "credit notes. The department asks whether the credit notes match the correct financial "
            "year, comply with CGST credit-note rules, satisfy post-supply discount conditions, and "
            "whether recipients reversed ITC where applicable."
        ),
        decision_pressure=(
            "The owner wants a fast reply and may treat the issue as a simple explanation problem. "
            "The real operating issue is evidence quality: invoice linkage, month-wise reporting, "
            "buyer-side treatment, and whether a later circular is being applied to an earlier year."
        ),
        mistake_or_risk=(
            "Do not submit a bare narrative. A weak response without original invoices, credit-note "
            "copies, GSTR-1 disclosure proof, output-tax reconciliation, and buyer ITC reversal proof "
            "can convert a documentation gap into a demand risk."
        ),
        recommended_next_action=(
            "Ask for the notice, original invoice list, credit-note register, GSTR-1 months, GSTR-3B "
            "adjustment trail, buyer confirmations/debit notes, and discount/return correspondence. "
            "Prepare a reconciliation table before drafting the reply."
        ),
        evidence_needed=[
            "GST notice copy and section/category of notice",
            "Original invoices mapped one-to-one to credit notes",
            "Credit-note copies with GSTIN, tax value, reason, and date",
            "GSTR-1 credit-note disclosures for each month",
            "GSTR-3B output-tax adjustment reconciliation",
            "Buyer debit notes or written acceptance where available",
            "Recipient ITC reversal confirmation where relevant",
            "Post-supply discount agreement/correspondence if discount-related",
        ],
        red_flags=[
            "Notice is a show-cause notice rather than a scrutiny clarification",
            "Credit notes relate to a prior financial year or after statutory time limits",
            "Buyer has not reversed ITC where required",
            "Credit notes are being used to mask bad debt instead of return/discount/deficiency",
        ],
        agent_lesson=(
            "The seasoned move is to turn a GST notice into an evidence-table exercise first. "
            "Legal interpretation comes after the facts reconcile."
        ),
        hard_gate_candidate=(
            "If an owner asks the agent to draft a GST notice response, require notice upload and "
            "supporting document checklist before producing a final submission."
        ),
        retrieval_triggers=[
            "GST notice credit note",
            "output tax liability reduction",
            "buyer ITC reversal",
            "post supply discount",
            "GSTR-1 credit note evidence",
        ],
    ),
    ScenarioCard(
        slug="bk002-gstr2b-itc-late-supplier-cashflow",
        title="GSTR-2B ITC not reflected: cashflow pressure is not permission to overclaim",
        source_type="forum_operator_voice",
        trust_level="medium",
        domains=["compliance", "finance", "manager_coo"],
        agent_targets=["compliance_agent", "finance_agent", "manager_coo"],
        tags=["gst", "itc", "gstr2b", "cashflow", "supplier_followup", "compliance"],
        source_url="https://www.caclubindia.com/forum/gst-3b-input-tax-credit-612404.asp",
        local_file="archives/business-knowledge/executional/forums/caclubindia_gstr3b_itc_supplier_late_filing_2025.html",
        situation=(
            "A buyer has purchase invoices, but the supplier files late, so the ITC is visible in "
            "dynamic GSTR-2A or expected soon but is not available in GSTR-2B for the current 3B filing."
        ),
        decision_pressure=(
            "The owner has cashflow stress and wants to reduce cash payment by claiming expected ITC. "
            "The compliance risk is that ITC not reflected in GSTR-2B can trigger notices/litigation."
        ),
        mistake_or_risk=(
            "Do not advise claiming ITC merely because the supplier promised to file later or because "
            "GSTR-2A appears higher. Treat supplier delay as a vendor-control and cashflow issue, not a "
            "return-filing shortcut."
        ),
        recommended_next_action=(
            "Reconcile purchase register against GSTR-2B, pay the uncovered liability through cash if "
            "needed, and move the missing ITC to next-period follow-up. Escalate supplier payment holds "
            "or vendor scorecards for repeat offenders."
        ),
        evidence_needed=[
            "Purchase register with invoice dates and GSTINs",
            "Current GSTR-2B snapshot",
            "GSTR-2A only as diagnostic support, not claim basis",
            "Supplier filing status and written commitment",
            "Cashflow impact amount",
        ],
        red_flags=[
            "Owner asks to claim non-2B ITC because cash is tight",
            "Large vendor repeatedly files late",
            "Material ITC gap could create working-capital stress",
            "The filing deadline is near and no CA has reviewed the position",
        ],
        agent_lesson=(
            "A finance-grade answer separates tax entitlement timing from cashflow pain. The agent "
            "should propose collections/vendor controls, not risky return positions."
        ),
        hard_gate_candidate=(
            "Before suggesting GSTR-3B ITC claim amounts, require GSTR-2B reconciliation; flag non-2B "
            "ITC as CA-review-only."
        ),
        retrieval_triggers=[
            "supplier filed GST late",
            "GSTR-2B not showing ITC",
            "can I claim ITC in 3B",
            "cash payment due to missing ITC",
        ],
    ),
    ScenarioCard(
        slug="bk003-bad-debt-credit-note-wrong-tool",
        title="Bad debt recovery: a credit note is usually the wrong tool",
        source_type="forum_operator_voice",
        trust_level="medium",
        domains=["finance", "compliance", "manager_coo"],
        agent_targets=["finance_agent", "compliance_agent", "manager_coo"],
        tags=["bad_debt", "gst", "credit_note", "collections", "msme_samadhaan", "manager_coo"],
        source_url="https://www.caclubindia.com/forum/bad-debt-recovery-615727.asp",
        local_file="archives/business-knowledge/executional/forums/caclubindia_bad_debt_recovery_2026.html",
        situation=(
            "A customer has not paid an outstanding invoice. The supplier asks whether they can issue "
            "a credit note for the whole invoice amount to close the problem."
        ),
        decision_pressure=(
            "The owner wants to reduce tax/accounting pain and recover or write off the receivable. "
            "The temptation is to use GST credit-note mechanics as a collections shortcut."
        ),
        mistake_or_risk=(
            "Do not treat non-payment as a standard ground for GST credit note. A credit note reduces "
            "liability and may be appropriate for returns, deficiency, or valid discount/waiver cases, "
            "not merely because the debtor is refusing to pay."
        ),
        recommended_next_action=(
            "Classify the case: commercial dispute, goods/service deficiency, negotiated waiver, or "
            "true bad debt. For recovery, move to demand notice, payment plan, MSME Samadhaan if eligible, "
            "or legal escalation. For write-off, keep GST/tax/accounting treatment separate and CA-reviewed."
        ),
        evidence_needed=[
            "Invoice and delivery/service proof",
            "Customer acknowledgement or dispute trail",
            "Ledger aging and outstanding amount",
            "Udyam/MSME status if supplier is MSME",
            "Any agreement to waive or discount the debt",
            "Collections attempts and legal notice history",
        ],
        red_flags=[
            "Owner wants to issue credit note only because customer defaulted",
            "Customer has taken ITC but not paid within 180 days",
            "Large receivable threatens payroll/vendor payments",
            "Supply quality is disputed and may justify a different treatment",
        ],
        agent_lesson=(
            "The seasoned COO separates recovery strategy, accounting write-off, and GST treatment. "
            "One instrument should not be forced to solve all three."
        ),
        hard_gate_candidate=(
            "Block automated credit-note advice for bad debts unless the reason is return, deficiency, "
            "post-supply discount, or documented waiver and CA review is requested."
        ),
        retrieval_triggers=[
            "customer not paying invoice",
            "bad debt GST credit note",
            "recover outstanding amount",
            "MSME Samadhaan debtor",
        ],
    ),
    ScenarioCard(
        slug="bk004-output-gst-paid-customer-default",
        title="Output GST paid but customer defaulted: tax liability and receivable recovery are separate",
        source_type="forum_operator_voice",
        trust_level="medium",
        domains=["finance", "compliance", "manager_coo"],
        agent_targets=["finance_agent", "compliance_agent", "manager_coo"],
        tags=["gst", "bad_debt", "cashflow", "collections", "receivables"],
        source_url="https://www.caclubindia.com/forum/output-gst-claim-on-bad-debts-514602.asp",
        local_file="archives/business-knowledge/executional/forums/caclubindia_output_gst_bad_debts_2019.html",
        situation=(
            "The business supplied goods/services, paid output GST, but the customer did not pay for "
            "more than a year. The owner asks how to get the output GST back."
        ),
        decision_pressure=(
            "This is emotionally painful because cash never came in but tax was already paid. The owner "
            "may ask for a portal workaround or reversal."
        ),
        mistake_or_risk=(
            "Do not imply that unpaid customer debt automatically reverses output GST. Unless a valid "
            "credit-note/refund route exists for a permitted reason and within applicable limits, the "
            "path is collections/accounting, not tax reversal."
        ),
        recommended_next_action=(
            "Classify the case as a commercial dispute, return/deficiency, negotiated waiver or discount, "
            "or true bad debt. Keep three workstreams separate: receivables recovery through aging review, "
            "demand/payment plan and MSME Samadhaan or legal escalation where eligible; accounting write-off; "
            "and CA-reviewed GST treatment based on a valid statutory reason rather than non-payment alone."
        ),
        evidence_needed=[
            "Original invoice and GST payment month",
            "Proof of supply/delivery",
            "Customer ledger and aging",
            "Customer dispute/cancellation correspondence",
            "Credit-note eligibility reason, if any",
            "MSME/Udyam registration if available",
            "Collections attempts, demand notices, and any negotiated waiver",
        ],
        red_flags=[
            "Owner asks to reverse sale only because payment did not arrive",
            "Amount is material to cashflow",
            "Customer has vanished or GSTIN cancelled",
            "Old invoice exceeds credit-note timing limits",
            "A quality dispute, return, or documented waiver may require different treatment",
        ],
        agent_lesson=(
            "The Finance agent should be honest about bad news: GST cash leakage may not be recoverable "
            "from government just because the debtor defaulted. The COO move is prevention: credit checks, "
            "advance terms, milestone billing, and receivables discipline."
        ),
        hard_gate_candidate=(
            "Any advice to reverse output GST on non-payment requires CA-review gate and a valid statutory reason."
        ),
        retrieval_triggers=[
            "GST paid customer not paid",
            "output GST bad debt",
            "reverse sales bill unpaid invoice",
            "claim GST back debtor default",
            "bad debt GST credit note",
            "MSME Samadhaan debtor",
        ],
    ),
    ScenarioCard(
        slug="bk005-msme-delayed-payment-interest-and-relationship-pressure",
        title="MSME delayed payment: 45-day law collides with buyer relationship pressure",
        source_type="forum_operator_voice",
        trust_level="medium",
        domains=["finance", "compliance", "manager_coo"],
        agent_targets=["finance_agent", "compliance_agent", "manager_coo"],
        tags=["msme", "delayed_payment", "working_capital", "interest", "collections"],
        source_url="https://www.caclubindia.com/forum/urgent-interest-on-msme-266538.asp",
        local_file="archives/business-knowledge/executional/forums/caclubindia_msme_delayed_payment_interest_2013.html",
        situation=(
            "A company pays MSME creditors after the statutory outer window while purchase orders may "
            "show longer commercial credit terms. The question is whether interest should be provided "
            "or paid, even if suppliers do not demand it."
        ),
        decision_pressure=(
            "Finance wants clean books; procurement wants longer terms; the COO fears damaging supplier "
            "relationships or exposing historical non-compliance."
        ),
        mistake_or_risk=(
            "Do not assume a 60-day PO overrides MSME delayed-payment discipline. Also do not ignore "
            "interest exposure merely because the vendor has not demanded it."
        ),
        recommended_next_action=(
            "Identify MSME vendors, map invoice acceptance dates, calculate aging beyond the applicable "
            "window, disclose/provide as advised by CA, and renegotiate payment process. For key vendors, "
            "set an escalation workflow before day 30/40 instead of discovering exposure at year-end."
        ),
        evidence_needed=[
            "Vendor Udyam/MSME status",
            "Invoice date and acceptance/deemed acceptance date",
            "PO payment terms",
            "Actual payment date",
            "Interest computation basis",
            "Year-end MSME disclosure working",
        ],
        red_flags=[
            "Large accumulated unpaid MSME interest exposure",
            "Critical supplier is MSME and payments are routinely late",
            "Auditor asks for MSME disclosure",
            "Vendor threatens MSEFC/Samadhaan filing",
        ],
        agent_lesson=(
            "The COO should not treat MSME delayed payment as an accounts-only issue. It is a working-capital, "
            "supplier-trust, audit-disclosure, and legal-escalation issue."
        ),
        hard_gate_candidate=(
            "For vendors marked MSME, trigger receivables/payables alerts before the statutory danger zone."
        ),
        retrieval_triggers=[
            "MSME payment after 45 days",
            "interest on delayed MSME payment",
            "MSMED Act supplier payment",
            "MSEFC risk",
        ],
    ),
    ScenarioCard(
        slug="bk006-msefc-notice-response-playbook",
        title="MSEFC notice: respond with facts, proof, and settlement posture",
        source_type="forum_operator_voice",
        trust_level="medium",
        domains=["finance", "compliance", "manager_coo"],
        agent_targets=["finance_agent", "compliance_agent", "manager_coo"],
        tags=["msme", "msefc", "samadhaan", "notice", "dispute_response"],
        source_url="https://www.caclubindia.com/experts/notice-for-delayed-payment-of-mse-by-msefc-2857399.asp",
        local_file="archives/business-knowledge/executional/forums/caclubindia_msefc_delayed_payment_notice_2021.html",
        situation=(
            "A buyer receives a notice from the MSE council because a supplier filed a delayed-payment "
            "complaint through MSME Samadhaan/MSEFC."
        ),
        decision_pressure=(
            "The buyer wants to avoid penalty/interest and may blame downstream non-payment, pandemic, "
            "cashflow, or internal process delays."
        ),
        mistake_or_risk=(
            "Do not ignore the notice or send an informal reply without documentary evidence. Do not rely "
            "only on relationship narratives if invoices, acceptance, and payment delay are clear."
        ),
        recommended_next_action=(
            "Prepare a formal response with invoice-wise facts, acceptance/dispute status, payment history, "
            "reason for delay, settlement proposal where appropriate, and proof of submission/receipt through "
            "the correct channel. Escalate to counsel/CA for drafting if amount is material."
        ),
        evidence_needed=[
            "MSEFC/Samadhaan notice",
            "Supplier Udyam/MSME certificate",
            "Invoice-wise ledger",
            "Goods/service acceptance or dispute proof",
            "Payment proof and pending amount",
            "Correspondence showing reason for delay",
            "Response submission proof",
        ],
        red_flags=[
            "Notice deadline is near",
            "No documented dispute was raised before the complaint",
            "Multiple MSME vendors have similar aging",
            "Claim includes significant interest",
        ],
        agent_lesson=(
            "The seasoned response is procedural discipline: never wing a statutory-payment notice. "
            "Build an invoice-wise brief, decide whether to contest or settle, and preserve proof of reply."
        ),
        hard_gate_candidate=(
            "If owner mentions MSEFC, MSME Samadhaan, or statutory delayed-payment notice, route to Finance + Compliance and request documents before drafting."
        ),
        retrieval_triggers=[
            "MSEFC notice",
            "MSME Samadhaan complaint",
            "delayed payment notice supplier",
            "reply to MSE council",
        ],
    ),
    ScenarioCard(
        slug="bk007-treds-receivables-discounting-fit",
        title="TReDS fit: use invoice discounting when buyer quality is stronger than seller cashflow",
        source_type="official_guidance",
        trust_level="authoritative",
        domains=["finance", "sales", "manager_coo"],
        agent_targets=["finance_agent", "sales_agent", "manager_coo"],
        tags=["treds", "receivables", "working_capital", "msme", "invoice_discounting"],
        source_url="https://www.rbi.org.in/scripts/FAQView.aspx/FAQView.aspx/FAQView.aspx?Id=132",
        local_file="archives/business-knowledge/finance/rbi_treds_faq.html",
        situation=(
            "An MSME seller has invoices due from corporates, government departments, PSUs, or other "
            "buyers, but needs cash before the buyer pays."
        ),
        decision_pressure=(
            "The owner may chase expensive informal credit or over-discount sales. TReDS can convert "
            "approved receivables into working capital if buyer/onboarding conditions fit."
        ),
        mistake_or_risk=(
            "Do not treat TReDS as generic lending. It is receivables discounting with seller, buyer, "
            "and financier participation, invoice/factoring-unit acceptance, financier bidding, and settlement flow."
        ),
        recommended_next_action=(
            "Check MSME status, buyer eligibility/willingness, invoice acceptance, platform onboarding, "
            "and whether the receivable is clean and undisputed. Use TReDS as a cashflow lever especially "
            "for strong buyers with slow payment cycles."
        ),
        evidence_needed=[
            "Udyam/MSME status",
            "Buyer identity and buyer onboarding status",
            "Invoice and delivery/service proof",
            "Buyer acceptance of invoice/factoring unit",
            "Expected due date and discount cost comparison",
            "Current working-capital need",
        ],
        red_flags=[
            "Buyer disputes delivery/service quality",
            "Invoice is not accepted by buyer",
            "Seller is not MSME",
            "Owner wants to use TReDS for weak/private receivables without buyer acceptance",
        ],
        agent_lesson=(
            "The Finance agent should spot when a cashflow problem is actually a receivables-financing problem. "
            "For B2B sellers, buyer quality can unlock cheaper capital than the seller could get alone."
        ),
        hard_gate_candidate=(
            "When receivables aging crosses threshold for MSME B2B sellers, suggest TReDS eligibility check before high-cost borrowing."
        ),
        retrieval_triggers=[
            "invoice discounting",
            "TReDS",
            "corporate buyer not paying yet",
            "MSME receivables finance",
            "working capital stuck in invoices",
        ],
    ),
    ScenarioCard(
        slug="bk008-msme-bank-credit-rights-and-document-discipline",
        title="MSME bank credit: ask for the bank’s process, not a favour",
        source_type="official_guidance",
        trust_level="authoritative",
        domains=["finance", "manager_coo"],
        agent_targets=["finance_agent", "manager_coo"],
        tags=["msme_lending", "bank_credit", "collateral", "working_capital", "documentation"],
        source_url="https://systemhealth.rbi.org.in/Scripts/BS_ViewMasDirections.aspx_id%3D11060.html",
        local_file="archives/business-knowledge/finance/rbi_msme_lending_master_direction_2024.html",
        situation=(
            "An MSME owner needs working capital/term finance and is negotiating with a bank branch. "
            "The owner may not know what process obligations and borrower-facing disclosures banks are expected to maintain."
        ),
        decision_pressure=(
            "The owner may accept vague delays, informal collateral demands, or unclear document requests. "
            "The Finance agent should convert the conversation into a documented credit process."
        ),
        mistake_or_risk=(
            "Do not tell the owner only to 'try another bank'. First ask for acknowledgement, document checklist, "
            "application tracking, written reasons for rejection, and correct MSME classification."
        ),
        recommended_next_action=(
            "Prepare an application pack, confirm Udyam/PSL classification, ask for the bank's indicative checklist, "
            "application acknowledgement/tracking number, sanction timeline, collateral requirement basis, and written rejection reasons if declined."
        ),
        evidence_needed=[
            "Udyam Registration Certificate or Udyam Assist certificate",
            "Financial statements / bank statements",
            "GST returns where applicable",
            "Loan purpose and working-capital calculation",
            "Existing debt and repayment record",
            "Bank application acknowledgement",
            "Written sanction/rejection communication",
        ],
        red_flags=[
            "Bank asks for collateral on small MSE loan without explaining basis",
            "No acknowledgement or tracking number is provided",
            "Application is pending beyond stated norms",
            "Owner lacks clean books/GST/bank statements",
        ],
        agent_lesson=(
            "A seasoned Finance agent does not merely recommend loans; it makes the owner bankable and procedural. "
            "Documentation and written bank responses are leverage."
        ),
        hard_gate_candidate=(
            "For financing advice, require Udyam status and basic financial document readiness before recommending lender route."
        ),
        retrieval_triggers=[
            "MSME loan collateral",
            "bank delaying loan application",
            "working capital loan documents",
            "Udyam loan bank rejection",
        ],
    ),
    ScenarioCard(
        slug="bk009-dark-patterns-growth-pressure",
        title="Dark patterns: conversion pressure cannot override consumer choice",
        source_type="official_guidance",
        trust_level="authoritative",
        domains=["marketing", "compliance", "manager_coo"],
        agent_targets=["marketing_agent", "compliance_agent", "manager_coo"],
        tags=["dark_patterns", "marketing", "ecommerce", "consumer_protection", "conversion"],
        source_url="https://www.pib.gov.in/Pressreleaseshare.aspx?PRID=1955344&lang=2&reg=48",
        local_file="archives/business-knowledge/compliance/pib_dark_patterns_consultation_2023.html",
        situation=(
            "A marketing or ecommerce team wants to improve conversion using urgency banners, checkout add-ons, "
            "cancellation friction, confusing buttons, disguised ads, drip pricing, or repeated nudges."
        ),
        decision_pressure=(
            "Short-term conversion metrics may improve, but regulatory and reputation risk rises if users are "
            "misled, manipulated, or pushed into actions they did not intend."
        ),
        mistake_or_risk=(
            "Do not recommend tactics that create false urgency/scarcity, sneak items into the basket, shame users, "
            "force unrelated actions, trap subscriptions, obscure important information, bait-and-switch outcomes, "
            "hide price components, disguise ads, or nag users during a transaction."
        ),
        recommended_next_action=(
            "For every growth experiment, ask: is the choice clear, is the price complete upfront, can the user decline, "
            "is cancellation as easy as signup, and would the same copy survive a regulator/customer complaint screenshot?"
        ),
        evidence_needed=[
            "Screenshot or copy of proposed UI/message",
            "Full price breakdown",
            "Consent/opt-in flow",
            "Cancellation/refund flow",
            "A/B test hypothesis and success metric",
            "Customer complaint history",
        ],
        red_flags=[
            "Fake countdown or scarcity claim",
            "Pre-ticked add-on or hidden charge",
            "Cancellation requires excessive steps",
            "Ad is disguised as organic/user content",
            "Owner says competitors do it so we should too",
        ],
        agent_lesson=(
            "A strong Marketing agent grows without contaminating trust. The COO should reject conversion tactics "
            "that create future complaints, refunds, platform penalties, or regulator exposure."
        ),
        hard_gate_candidate=(
            "Run dark-pattern checks before approving campaign copy, checkout changes, subscription flows, or price experiments."
        ),
        retrieval_triggers=[
            "false urgency",
            "dark pattern",
            "checkout add-on",
            "subscription cancellation",
            "drip pricing",
            "conversion tactic risky",
        ],
    ),
    ScenarioCard(
        slug="bk010-dpdp-readiness-for-smb-digital-operations",
        title="DPDP readiness: privacy is an operating system, not a footer policy",
        source_type="official_guidance",
        trust_level="authoritative",
        domains=["compliance", "marketing", "manager_coo"],
        agent_targets=["compliance_agent", "marketing_agent", "manager_coo"],
        tags=["dpdp", "privacy", "consent", "data_breach", "marketing"],
        source_url="https://www.pib.gov.in/Pressreleaseshare.aspx?PRID=2090048&lang=2&reg=48",
        local_file="archives/business-knowledge/compliance/pib_draft_dpdp_rules_2025.html",
        situation=(
            "A small business collects customer phone numbers, order history, addresses, preferences, or marketing opt-ins "
            "through WhatsApp, forms, sheets, ecommerce, or POS systems."
        ),
        decision_pressure=(
            "The team wants to use data for marketing and operations quickly. Privacy obligations can feel abstract, "
            "but failures show up as bad consent, unclear notices, breach mishandling, child-data risk, and weak access/deletion handling."
        ),
        mistake_or_risk=(
            "Do not treat consent as a one-time checkbox or privacy policy as decoration. Data use, notice, security, "
            "breach escalation, consent withdrawal, and individual rights handling need operational ownership."
        ),
        recommended_next_action=(
            "Map what personal data is collected, why it is used, where it is stored, who can access it, how consent/notice is captured, "
            "how opt-out/deletion is handled, and what happens if data is breached. Keep marketing lists tied to consent source."
        ),
        evidence_needed=[
            "Data inventory by source",
            "Consent/notice text and timestamp source",
            "Marketing opt-in/opt-out log",
            "Data access list",
            "Vendor/tool list",
            "Breach response owner and process",
            "Deletion/access request workflow",
        ],
        red_flags=[
            "Customer data copied across personal devices or unmanaged sheets",
            "Marketing list has no opt-in source",
            "Owner asks to message all contacts without consent basis",
            "Child/minor data or sensitive context appears",
            "Possible data breach or accidental disclosure",
        ],
        agent_lesson=(
            "The Compliance agent should make privacy concrete: data map, consent trail, access controls, opt-out handling, "
            "and breach playbook. The Manager should see privacy as operational reliability."
        ),
        hard_gate_candidate=(
            "Before bulk marketing, require consent/opt-in source and opt-out suppression checks."
        ),
        retrieval_triggers=[
            "DPDP",
            "customer data consent",
            "WhatsApp marketing opt in",
            "data breach",
            "delete customer data",
            "privacy compliance",
        ],
    ),
    ScenarioCard(
        slug="bk011-whatsapp-business-policy-send-discipline",
        title="WhatsApp business messaging: consent, relevance, and escalation beat blast volume",
        source_type="platform_policy",
        trust_level="high",
        domains=["marketing", "sales", "compliance", "manager_coo"],
        agent_targets=["marketing_agent", "sales_agent", "compliance_agent", "manager_coo"],
        tags=["whatsapp", "marketing", "consent", "templates", "customer_support"],
        source_url="https://whatsappbusiness.com/policy/",
        local_file="archives/business-knowledge/marketing/whatsapp_business_messaging_policy.html",
        situation=(
            "The business wants to send WhatsApp campaigns, reminders, winbacks, or support messages to customers."
        ),
        decision_pressure=(
            "WhatsApp is high-conversion and owner-facing, so teams may push for broad blasts. But user consent, relevance, "
            "policy compliance, template quality, and support escalation determine deliverability and trust."
        ),
        mistake_or_risk=(
            "Do not send broad campaigns to contacts without a clear opt-in/relationship basis, ignore opt-outs, use misleading templates, "
            "or pretend automated messaging is human support when escalation is needed."
        ),
        recommended_next_action=(
            "Segment by consent/source and business purpose, use approved templates where required, keep message promises specific, "
            "respect opt-outs immediately, and route complaints/refund/service issues to a human or owner approval path."
        ),
        evidence_needed=[
            "Contact source and opt-in basis",
            "Template name/category/status",
            "Campaign purpose and customer segment",
            "Suppression/opt-out list",
            "Recent complaint or block signals",
            "Escalation owner for replies",
        ],
        red_flags=[
            "Owner uploads scraped or purchased contacts",
            "Campaign includes regulated/prohibited goods or deceptive claims",
            "Customer has opted out or complained",
            "Message makes unverifiable discount/urgency claim",
            "No one can handle replies/escalations",
        ],
        agent_lesson=(
            "The Marketing agent should optimize for trusted conversations, not blast count. The Manager should enforce consent, "
            "suppression, template quality, and support capacity."
        ),
        hard_gate_candidate=(
            "All outbound WhatsApp campaigns must pass opt-in/suppression and template-policy checks before send."
        ),
        retrieval_triggers=[
            "WhatsApp campaign",
            "bulk message customers",
            "template approval",
            "opt out",
            "winback message",
            "customer support WhatsApp",
        ],
    ),
    ScenarioCard(
        slug="bk012-google-business-profile-representation-discipline",
        title="Google Business Profile: local growth starts with accurate representation",
        source_type="platform_policy",
        trust_level="high",
        domains=["marketing", "sales", "manager_coo"],
        agent_targets=["marketing_agent", "sales_agent", "manager_coo"],
        tags=["google_business_profile", "local_marketing", "reviews", "representation", "seo"],
        source_url="https://support.google.com/business/answer/3038177?hl=en",
        local_file="archives/business-knowledge/marketing/google_business_profile_guidelines.html",
        situation=(
            "A local business wants better discovery on Google Search/Maps and may be tempted to stuff keywords, duplicate listings, "
            "misstate service areas, or add promotional text into identity fields."
        ),
        decision_pressure=(
            "Local visibility matters, but inaccurate profile representation can cause suspension, customer confusion, duplicate-profile problems, "
            "and weak trust signals."
        ),
        mistake_or_risk=(
            "Do not keyword-stuff the business name, create duplicate profiles, misrepresent address/service area, use categories as tag spam, "
            "or add promotional claims where representation rules require real-world accuracy."
        ),
        recommended_next_action=(
            "Align profile name, address/service area, phone, hours, category, website, and photos with the real-world business. Then improve discovery through "
            "reviews, posts, photos, category fit, service/menu/product detail, and response discipline."
        ),
        evidence_needed=[
            "Real-world business name/signage",
            "Address or service-area operating model",
            "Primary phone/website",
            "Business category and service list",
            "Hours and holiday hours",
            "Review response SOP",
            "Duplicate profile check",
        ],
        red_flags=[
            "Owner wants to add city/service keywords to business name",
            "Multiple listings for same location/business",
            "Virtual office or fake address risk",
            "Profile suspended or verification failed",
            "Reviews mention wrong hours/location/service",
        ],
        agent_lesson=(
            "The Marketing agent should win local discovery through completeness and trust, not representation hacks. The COO should treat GBP as an operating asset."
        ),
        hard_gate_candidate=(
            "Before GBP optimization, check for representation-rule violations and duplicate listing risk."
        ),
        retrieval_triggers=[
            "Google Business Profile",
            "local SEO",
            "business name keywords",
            "Google Maps listing",
            "duplicate profile",
            "reviews local business",
        ],
    ),
    ScenarioCard(
        slug="bk013-first-customers-prospect-list-referral-loop",
        title="First customers: build a named prospect list before buying attention",
        source_type="official_guidance",
        trust_level="high",
        domains=["sales", "marketing", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "manager_coo"],
        tags=["first_customers", "prospecting", "referrals", "sales", "low_budget"],
        source_url="https://www.sba.gov/blog/8-ways-find-your-first-customers",
        local_file="archives/business-knowledge/sales/executional/sba_8_ways_find_first_customers.html",
        situation=(
            "A new or small business needs its first customers and is tempted to jump straight into ads, "
            "discounts, or broad social posting without a concrete prospect list."
        ),
        decision_pressure=(
            "The owner wants revenue quickly, has limited budget, and may not yet know which channel works. "
            "The Sales agent must force a named pipeline before spend."
        ),
        mistake_or_risk=(
            "Do not treat 'marketing' as posting into the void. Early sales needs names, sources, referral asks, "
            "warm introductions, and a measurable follow-up cadence."
        ),
        recommended_next_action=(
            "Create a first-100 prospect list from previous conversations, local contacts, social connections, "
            "past enquiries, nearby businesses, and likely buyers. Ask every warm contact for one referral, run "
            "direct outreach, and track response/source before paid spend."
        ),
        evidence_needed=[
            "Named prospect list with source/reason",
            "Offer and first-pitch script",
            "Referral ask script",
            "Follow-up cadence",
            "Lead source and response tracker",
            "Owner's existing network/customer conversations",
        ],
        red_flags=[
            "Owner wants ads without defining target buyer",
            "No list of actual people/businesses to contact",
            "No follow-up cadence after first message",
            "Discounting before learning buyer objections",
        ],
        agent_lesson=(
            "A seasoned Sales agent starts with a controlled pipeline experiment: named prospects, warm referrals, "
            "specific ask, and measured follow-up. Paid attention comes after signal."
        ),
        hard_gate_candidate=(
            "Before recommending ad spend for a new business, require target segment, prospect list, offer, and tracking plan."
        ),
        retrieval_triggers=[
            "find first customers",
            "new business no customers",
            "who should I contact first",
            "low budget sales",
            "referral strategy",
        ],
    ),
    ScenarioCard(
        slug="bk014-marketing-plan-roi-and-operations-loop",
        title="Marketing plan: sales, operations, and ROI must be reviewed together",
        source_type="official_guidance",
        trust_level="high",
        domains=["sales", "marketing", "manager_coo", "finance"],
        agent_targets=["sales_agent", "marketing_agent", "finance_agent", "manager_coo"],
        tags=["marketing_plan", "roi", "operations", "sales_process", "customer_experience"],
        source_url="https://www.sba.gov/business-guide/manage-your-business/marketing-sales",
        local_file="archives/business-knowledge/sales/executional/sba_marketing_and_sales.html",
        situation=(
            "A business wants more customers but has not connected target market, competitive advantage, sales method, "
            "goals, action plan, budget, payment method, and post-sale support into one operating plan."
        ),
        decision_pressure=(
            "The owner may think marketing is only channel/copy. The Manager must connect marketing promises to payment, "
            "delivery, returns, staff behavior, and after-sale experience."
        ),
        mistake_or_risk=(
            "Do not optimise campaigns in isolation. If operations cannot deliver what marketing promises, growth creates "
            "refunds, complaints, bad reviews, and repeat-purchase loss."
        ),
        recommended_next_action=(
            "Build a one-page plan: target customer, advantage, sales path, annual goal, channel actions, budget, measurement method, "
            "payment friction, and post-sale support risks. Review ROI and operational bottlenecks on a fixed cadence."
        ),
        evidence_needed=[
            "Target segment",
            "Offer and advantage",
            "Sales path from discovery to payment",
            "Channel budget",
            "Revenue attribution method",
            "Payment options and costs",
            "Delivery/return/support SOP",
            "Review cadence",
        ],
        red_flags=[
            "Campaign promises faster delivery than operations can support",
            "No ROI measurement for offline/word-of-mouth spend",
            "Payment method is costly or inconvenient for customers",
            "Support load after campaign is unassigned",
        ],
        agent_lesson=(
            "The COO move is to treat marketing as an operating system: acquisition, conversion, payment, fulfilment, support, "
            "and measurement must be tied together."
        ),
        hard_gate_candidate=(
            "Before approving large campaign spend, require budget, expected revenue, measurement plan, and fulfilment/support readiness."
        ),
        retrieval_triggers=[
            "make marketing plan",
            "marketing ROI",
            "sales plan",
            "operations affects marketing",
            "campaign budget",
        ],
    ),
    ScenarioCard(
        slug="bk015-market-research-competitive-analysis-before-channel-choice",
        title="Market research: decide channel after demand, competition, and customer proof",
        source_type="official_guidance",
        trust_level="high",
        domains=["sales", "marketing", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "manager_coo"],
        tags=["market_research", "competitive_analysis", "positioning", "pricing", "sales"],
        source_url="https://www.sba.gov/business-guide/plan-your-business/market-research-competitive-analysis?toc-variant-a=undefined",
        local_file="archives/business-knowledge/sales/executional/sba_market_research_competitive_analysis.html",
        situation=(
            "The owner wants to choose sales channels or pricing without first checking demand, market size, buyer location, "
            "saturation, alternatives, and competitor strengths."
        ),
        decision_pressure=(
            "Quick action feels productive, but wrong-channel growth burns time and cash. The Sales/Marketing agents must gather "
            "just enough market proof before prescribing tactics."
        ),
        mistake_or_risk=(
            "Do not recommend Instagram, WhatsApp, exhibitions, marketplaces, or outbound sales as a default. Channel choice should follow "
            "buyer behavior, competitive gaps, and where the business can win."
        ),
        recommended_next_action=(
            "Answer demand, market size, economic indicators, location, saturation, and pricing. Then run direct customer research: objections, "
            "buying trigger, alternatives, and buying experience gaps. Convert that into positioning and channel choice."
        ),
        evidence_needed=[
            "Target buyer profile",
            "Competitor list and strengths/weaknesses",
            "Current alternatives and price points",
            "Customer interviews or enquiry logs",
            "Demand/location indicators",
            "Observed objections",
            "Channel test results",
        ],
        red_flags=[
            "Owner copied competitor channel without knowing economics",
            "No clarity on who buys and why",
            "Price is set only from cost-plus instinct",
            "Market is saturated and differentiation is vague",
        ],
        agent_lesson=(
            "A seasoned agent does not ask 'which platform should we use?' first. It asks: who has the pain, where do they search, "
            "what do they compare against, and why will they switch?"
        ),
        hard_gate_candidate=(
            "For new-channel recommendations, require target buyer, competitor alternatives, and a measurable channel-test hypothesis."
        ),
        retrieval_triggers=[
            "which marketing channel",
            "market research",
            "competitive analysis",
            "target customer",
            "pricing competitors",
        ],
    ),
    ScenarioCard(
        slug="bk016-local-marketing-flywheel-listings-reviews-community",
        title="Local marketing flywheel: listings, reviews, community, and nearby reach",
        source_type="official_guidance",
        trust_level="high",
        domains=["sales", "marketing", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "manager_coo"],
        tags=["local_marketing", "reviews", "google_business_profile", "community", "offline"],
        source_url="https://www.sba.gov/blog/10-local-marketing-strategies-work",
        local_file="archives/business-knowledge/sales/executional/sba_10_local_marketing_strategies.html",
        situation=(
            "A local business depends on customers within a service radius but is underinvesting in basic local discovery and trust assets."
        ),
        decision_pressure=(
            "The owner may chase broad digital ads while nearby ready-to-buy customers cannot find accurate hours, location, photos, reviews, "
            "or a clear reason to visit/call."
        ),
        mistake_or_risk=(
            "Do not start with broad paid campaigns if the local presence is broken. Missing/incorrect listings, weak reviews, poor photos, "
            "and no community presence leak high-intent demand."
        ),
        recommended_next_action=(
            "Audit local listings, hours, phone, service radius, photos, reviews, local offers, community partnerships, local events, and nearby social targeting. "
            "Build a weekly operating rhythm: ask for reviews, post updates, respond to reviews, refresh photos, and track calls/walk-ins. "
            "Before paid reach, calculate ticket-level contribution, repeat interval, customer-capture method, geographic radius, and CAC ceiling."
        ),
        evidence_needed=[
            "Google Business Profile status",
            "NAP consistency across listings",
            "Review count/rating/recent responses",
            "Photo freshness",
            "Local partnership/event list",
            "Walk-in/call tracking method",
            "Service radius and nearby customer clusters",
            "Ticket contribution, repeat interval, and paid CAC ceiling",
        ],
        red_flags=[
            "Business hours or phone number are wrong online",
            "Competitors have stronger recent review velocity",
            "Owner wants city-wide ads before fixing local trust assets",
            "No process to ask happy customers for reviews",
            "A thin-margin one-time purchase cannot support projected acquisition cost",
        ],
        agent_lesson=(
            "Local sales growth is often operational discipline disguised as marketing: accurate presence, proof, reviews, and community touchpoints."
        ),
        hard_gate_candidate=(
            "For local businesses, run a local-presence audit before recommending paid acquisition."
        ),
        retrieval_triggers=[
            "local marketing",
            "walk in customers",
            "Google reviews",
            "nearby customers",
            "offline marketing",
            "community promotion",
        ],
    ),
    ScenarioCard(
        slug="bk017-msme-pms-trade-fairs-buyer-seller-meets",
        title="MSME sales expansion: use trade fairs and buyer-seller meets as structured pipeline, not vanity stalls",
        source_type="official_guidance",
        trust_level="authoritative",
        domains=["sales", "marketing", "finance", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "finance_agent", "manager_coo"],
        tags=["msme", "trade_fair", "buyer_seller_meet", "offline_sales", "subsidy"],
        source_url="https://www.msme.gov.in/offerings/schemes-and-services/details/marketing-promotion-schemes-1-QzMzETMtQWa",
        local_file="archives/business-knowledge/sales/executional/msme_procurement_marketing_support_scheme.html",
        situation=(
            "An MSE with Udyam registration wants market access beyond local referrals and online posts. Government PMS support can subsidize trade fairs, "
            "exhibitions, vendor development programs, e-commerce adoption, bar code adoption, workshops, and retail-outlet development in defined cases."
        ),
        decision_pressure=(
            "Owners may treat exhibitions as branding expenses. The Sales agent should turn them into buyer pipeline systems with pre-event targeting, "
            "sample strategy, lead capture, quote follow-up, and reimbursement documentation."
        ),
        mistake_or_risk=(
            "Do not recommend trade-fair participation without ROI plan, eligible scheme check, required documents, stall economics, and post-event follow-up."
        ),
        recommended_next_action=(
            "Check Udyam eligibility and event fit; define buyer personas; prepare catalog/pricing/sample/QR lead form; schedule pre-event meetings; capture leads by quality; "
            "follow up within 48 hours; preserve invoices/receipts/photos/participation proof for subsidy/claim where applicable."
        ),
        evidence_needed=[
            "Udyam Registration Certificate",
            "Event approval/eligibility",
            "Target buyer list",
            "Catalog, rate card, samples",
            "Lead capture sheet/QR form",
            "Space rent/travel/publicity/freight receipts",
            "Post-event enquiry and order tracker",
        ],
        red_flags=[
            "No target buyer list before event",
            "Owner only wants footfall, not lead quality",
            "No reimbursement document discipline",
            "Product packaging/catalog is not ready",
            "Follow-up owner is not assigned",
        ],
        agent_lesson=(
            "Offline sales can be a moat when systematised. A trade fair is not a stall; it is a concentrated buyer-acquisition sprint."
        ),
        hard_gate_candidate=(
            "Before recommending an exhibition, require event objective, target buyer list, lead-capture plan, and claim-document checklist."
        ),
        retrieval_triggers=[
            "trade fair for MSME",
            "buyer seller meet",
            "offline B2B sales",
            "MSME marketing subsidy",
            "exhibition lead follow up",
        ],
    ),
    ScenarioCard(
        slug="bk018-corporate-gifting-b2b-outbound-pipeline",
        title="Corporate gifting B2B: decision-maker mapping beats generic pitching",
        source_type="forum_operator_voice",
        trust_level="medium",
        domains=["sales", "marketing", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "manager_coo"],
        tags=["b2b_sales", "corporate_gifting", "outbound", "samples", "pipeline"],
        source_url="https://fi.reddit.com/r/smallbusinessindia/comments/1ujeky8/how_do_you_guys_pitch_to_corporates_and_land_b2b/",
        local_file="archives/business-knowledge/executional/SOURCE_INDEX.md",
        situation=(
            "A premium product brand has a few corporate gifting orders from referrals/inbound and wants to proactively build B2B pipeline."
        ),
        decision_pressure=(
            "The owner may send generic cold messages to company inboxes. B2B gifting needs role mapping, seasonality, sample economics, proof, and multi-touch follow-up."
        ),
        mistake_or_risk=(
            "Do not pitch 'corporates' as one blob. Decision-makers vary: HR, admin, procurement, founder office, event teams, office managers, agencies, and department heads."
        ),
        recommended_next_action=(
            "Define gifting occasions and order bands; build account list; map likely buyers; prepare a corporate catalog with MOQ, customization, delivery SLA, GST invoice, and sample policy; "
            "use LinkedIn/email/calls/referrals/agencies; track touch count, sample-to-order conversion, and seasonal buying windows."
        ),
        evidence_needed=[
            "Target company/account list",
            "Decision-maker role map",
            "Corporate catalog and price tiers",
            "MOQ/customization options",
            "Sample cost and approval process",
            "Delivery SLA and packaging proof",
            "GST invoice/payment terms",
            "Outreach and follow-up tracker",
        ],
        red_flags=[
            "Pitch has no occasion/use-case",
            "No clarity on MOQ, customization, GST invoice, or delivery timeline",
            "Samples are sent without qualification",
            "Owner cannot handle credit terms or delayed corporate payment",
        ],
        agent_lesson=(
            "A Sales agent should convert B2B ambition into account-based selling: right role, right occasion, proof, sample discipline, and follow-up math."
        ),
        hard_gate_candidate=(
            "Before B2B outreach, require account list, buyer role, pitch angle, catalog, sample policy, and payment-term stance."
        ),
        retrieval_triggers=[
            "corporate gifting",
            "B2B clients",
            "pitch to corporates",
            "send sample before call",
            "LinkedIn cold email B2B",
        ],
    ),
    ScenarioCard(
        slug="bk019-bulky-product-shipping-unit-economics-local-first",
        title="Bulky low-AOV products: solve shipping economics before scaling geography",
        source_type="forum_operator_voice",
        trust_level="medium",
        domains=["sales", "finance", "manager_coo"],
        agent_targets=["sales_agent", "finance_agent", "manager_coo"],
        tags=["shipping", "unit_economics", "local_sales", "pricing", "fulfilment"],
        source_url="https://dd.reddit.com/r/smallbusinessindia/comments/1qzz5tf/started_a_forever_flowers_business_advice_needed/",
        local_file="archives/business-knowledge/executional/SOURCE_INDEX.md",
        situation=(
            "A handmade/lightweight product looks cheap to ship by weight but is bulky, so volumetric weight makes outstation delivery expensive and destroys margin."
        ),
        decision_pressure=(
            "The owner sees demand outside the city and wants to ship pan-India, but every order may lose money unless packaging, courier, price, and channel are redesigned."
        ),
        mistake_or_risk=(
            "Do not advise geographic scaling only because enquiries exist. If shipping cost exceeds contribution margin, growth amplifies losses."
        ),
        recommended_next_action=(
            "Measure packed dimensions and volumetric weight; test multiple couriers/transports; negotiate regular-shipping rates; redesign packaging; separate local and outstation pricing; "
            "build local channels first through exhibitions, gifting shops, event planners, decorators, and social proof."
        ),
        evidence_needed=[
            "Product gross margin",
            "Packed dimensions and actual weight",
            "Courier quotes by city/zone",
            "Damage rate and packaging cost",
            "Local delivery cost",
            "Outstation willingness-to-pay",
            "Repeat/bulk order potential",
        ],
        red_flags=[
            "Shipping cost is hidden inside product price and erases margin",
            "Owner accepts outstation orders without zone pricing",
            "High damage risk product has no packaging SOP",
            "No local channel has been saturated yet",
        ],
        agent_lesson=(
            "The Manager must protect unit economics. Sometimes the right sales tactic is local depth before national reach."
        ),
        hard_gate_candidate=(
            "For physical products, require contribution-margin check including packaging, shipping, damage, and payment costs before recommending pan-India expansion."
        ),
        retrieval_triggers=[
            "shipping too expensive",
            "volumetric weight",
            "pan India delivery margin",
            "local business shipping",
            "unit economics ecommerce",
        ],
    ),
    ScenarioCard(
        slug="bk020-community-listening-for-demand-discovery",
        title="Community listening: find demand language before writing ads",
        source_type="platform_guidance",
        trust_level="high",
        domains=["sales", "marketing", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "manager_coo"],
        tags=["community_listening", "reddit", "demand_research", "ads", "copywriting"],
        source_url="https://www.business.reddit.com/learning-hub/articles/how-to-find-customers-on-reddit",
        local_file="archives/business-knowledge/sales/executional/reddit_business_how_to_find_customers.html",
        situation=(
            "The business wants campaign ideas but has not mined public customer conversations for buying questions, objections, comparisons, and vocabulary."
        ),
        decision_pressure=(
            "It is faster to write ads from the seller's perspective. But communities reveal how buyers describe pain, alternatives, doubts, and decision triggers."
        ),
        mistake_or_risk=(
            "Do not enter communities only to promote. Treat them as research and trust-building environments. Spammy posting damages reputation and can get removed."
        ),
        recommended_next_action=(
            "Search communities for problem phrases, competitor names, buying questions, complaint patterns, and review language. Build a swipe file of buyer words, then convert it into FAQs, "
            "content, landing-page copy, outreach hooks, and targeted ad hypotheses. Engage organically only where genuinely useful."
        ),
        evidence_needed=[
            "Target subreddit/community list",
            "Repeated buyer questions",
            "Competitor mentions",
            "Objection themes",
            "Exact customer language snippets",
            "Content/ad hypothesis list",
            "Community rules",
        ],
        red_flags=[
            "Owner wants to post promotional links immediately",
            "Community rules ban self-promotion",
            "Research is based on one anecdote",
            "Ad copy uses seller jargon instead of buyer language",
        ],
        agent_lesson=(
            "A sharp Sales/Marketing agent uses public communities to hear market language before spending money. The moat is better questions, not louder ads."
        ),
        hard_gate_candidate=(
            "Before community-based promotion, require a research-first pass and community-rule check."
        ),
        retrieval_triggers=[
            "where to find customers online",
            "Reddit marketing",
            "customer research",
            "buyer objections",
            "ad copy ideas",
        ],
    ),
    ScenarioCard(
        slug="bk021-sales-method-selector-and-innovation-matrix",
        title="Sales method selector: choose the motion from buyer, ticket, trust, and margin",
        source_type="local_synthesis",
        trust_level="high",
        domains=["sales", "marketing", "finance", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "finance_agent", "manager_coo"],
        tags=["sales_methods", "sales_strategy", "method_selection", "innovation", "manager_coo"],
        source_url="archives/business-knowledge/sales/methods/SALES_METHODS_TAXONOMY.md",
        local_file="archives/business-knowledge/sales/methods/SALES_METHODS_TAXONOMY.md",
        situation=(
            "The owner asks how to increase sales, but the business context is incomplete: product type, buyer type, "
            "ticket size, margin, trust gap, urgency, channel readiness, and fulfilment capacity are not yet mapped."
        ),
        decision_pressure=(
            "The Sales Agent may be tempted to suggest fashionable tactics such as ads, LinkedIn outreach, influencer marketing, "
            "or discounts. The seasoned move is to select the sales motion from constraints and buyer behavior."
        ),
        mistake_or_risk=(
            "Do not recommend a sales tactic because it is popular. A tactic that works for enterprise SaaS may fail for local retail; "
            "a marketplace tactic may destroy margin; a trade-fair tactic without follow-up becomes vanity; a WhatsApp blast without consent becomes trust damage."
        ),
        recommended_next_action=(
            "Classify the sale first: low-ticket local, high-ticket service, B2B SMB, corporate/enterprise, marketplace/ecommerce, new category, or cashflow-constrained. "
            "Then combine one acquisition motion, one trust asset, one offer, one channel, one risk control, and one follow-up cadence."
        ),
        evidence_needed=[
            "Buyer type and decision-maker",
            "Ticket size and gross margin",
            "Sales cycle and urgency",
            "Current lead sources and conversion rates",
            "Trust assets available: reviews, samples, proof, certifications",
            "Fulfilment and support capacity",
            "Consent/channel constraints",
            "Payment terms and working-capital risk",
        ],
        red_flags=[
            "Owner wants ad spend without a target buyer or measurement plan",
            "The tactic increases orders but destroys contribution margin",
            "Sales method requires follow-up capacity the business does not have",
            "Compliance/privacy/platform-policy risks are ignored",
            "The buyer is a committee but the pipeline is single-threaded",
        ],
        agent_lesson=(
            "The Sales Agent should be a tactic composer, not a tactic repeater. It should choose from named methodologies "
            "and route-to-market motions, then innovate by recombining buyer slice, trigger, proof, offer, channel, risk control, and follow-up."
        ),
        hard_gate_candidate=(
            "Before recommending any sales tactic, require buyer type, margin, trust asset, operating capacity, and measurement method."
        ),
        retrieval_triggers=[
            "increase sales",
            "sales tactic",
            "which sales method",
            "how to get customers",
            "B2B sales strategy",
            "local sales strategy",
            "new sales idea",
        ],
    ),
    ScenarioCard(
        slug="bk022-digital-marketing-amplifies-business-model",
        title="Digital marketing amplifies the business model; it rarely rescues weak unit economics",
        source_type="discussion_operator_voice",
        trust_level="medium",
        domains=["marketing", "sales", "finance", "manager_coo"],
        agent_targets=["marketing_agent", "sales_agent", "finance_agent", "manager_coo"],
        tags=["digital_marketing", "unit_economics", "ecommerce", "cac", "campaign_strategy"],
        source_url="https://id.reddit.com/r/Entrepreneur/comments/125wuek/lets_talk_ecommerce_the_numbers_today_and_how_to/",
        local_file="archives/business-knowledge/marketing/discussions/reddit_ecommerce_marketing_operator_thread.html",
        situation=(
            "An ecommerce or DTC owner wants more sales and assumes the answer is better ads, influencers, or a new channel. "
            "Operators in the discussion repeatedly point back to product quality, offer strength, margin, conversion, and the cost of paid acquisition."
        ),
        decision_pressure=(
            "The owner sees competitors advertising and wants to scale immediately. The Manager has to decide whether to spend, fix the offer, improve retention, "
            "test creatives, or narrow the buyer segment."
        ),
        mistake_or_risk=(
            "Do not recommend ad scaling before contribution margin, landing-page conversion, average order value, repeat rate, and fulfilment reliability are visible. "
            "Paid reach can make a bad offer fail faster."
        ),
        recommended_next_action=(
            "Run a unit-economics and funnel diagnostic first: gross margin, AOV, repeat purchase, conversion rate, CAC ceiling, creative promise, proof assets, and fulfilment constraints. "
            "Only then choose between paid ads, micro-influencers, community, SEO/content, marketplace, or retention campaigns."
        ),
        evidence_needed=[
            "Gross margin and contribution margin after shipping/returns/discounts",
            "Average order value and repeat purchase rate",
            "Current conversion rate by traffic source",
            "Customer reviews, UGC, founder story, or proof assets",
            "Ad spend, CAC, ROAS, and payback period if campaigns already ran",
            "Fulfilment capacity and refund/return issues",
        ],
        red_flags=[
            "Owner wants to increase budget because impressions are cheap but sales are weak",
            "ROAS is reported without margin or returns",
            "No repeat-purchase or retention loop exists",
            "Creative promise is stronger than product proof",
        ],
        agent_lesson=(
            "A seasoned Marketing agent should treat digital as an amplifier. First diagnose the offer and economics, then use channels to compound what already has pull."
        ),
        hard_gate_candidate=(
            "Before recommending paid scaling, require CAC ceiling, gross margin, conversion rate, and proof/retention assets."
        ),
        retrieval_triggers=[
            "digital marketing not working",
            "ecommerce ads not profitable",
            "ROAS but no profit",
            "scale ads",
            "DTC marketing",
        ],
    ),
    ScenarioCard(
        slug="bk023-low-ticket-local-business-paid-ads-vs-local-trust",
        title="Low-ticket local business: local trust often beats broad paid ads",
        source_type="discussion_operator_voice",
        trust_level="medium",
        domains=["marketing", "sales", "finance", "manager_coo"],
        agent_targets=["marketing_agent", "sales_agent", "finance_agent", "manager_coo"],
        tags=["local_marketing", "google_ads", "reviews", "repeat_business", "small_business"],
        source_url="https://ja.reddit.com/r/smallbusinessesowners/comments/1ru2hnk/google_ads_has_become_suicide_for_small/",
        local_file="archives/business-knowledge/marketing/discussions/reddit_small_business_google_ads_local_bakery.html",
        situation=(
            "A local low-ticket business such as a bakery, salon, repair shop, tuition class, or small service provider is considering Google Ads or social ads because sales feel slow."
        ),
        decision_pressure=(
            "The owner wants visible action and believes paid traffic is the fastest lever. But low ticket size, local radius, and limited repeat capture can make paid CAC uneconomic."
        ),
        mistake_or_risk=(
            "Do not push broad paid campaigns when the business has weak local listings, few reviews, poor photos, no referral/repeat loop, or no way to capture walk-in/online interest."
        ),
        recommended_next_action=(
            "Build the local trust stack first: Google Business Profile completeness, photos, menus/services, review requests, customer tags/UGC, nearby partnerships, local offers, "
            "WhatsApp/email capture, loyalty, and referral prompts. Test ads only with a tight radius, offer, and source tracking."
        ),
        evidence_needed=[
            "Ticket size and gross margin",
            "Repeat purchase interval",
            "Google Business Profile completeness and review count/rating",
            "Local competitor listings and photos",
            "Customer contact capture method",
            "Existing referral/loyalty mechanics",
            "Ad budget and CAC ceiling",
        ],
        red_flags=[
            "No Google Business Profile or weak listing hygiene",
            "No review-generation process",
            "Ad campaign targets too wide a geography",
            "One-time buyers are not captured for repeat purchase",
        ],
        agent_lesson=(
            "For thin-margin local SMBs, the first marketing job is becoming the obvious trusted nearby choice. Paid ads come after local proof and repeat economics."
        ),
        hard_gate_candidate=(
            "Before recommending local paid ads, require GBP/review audit, ticket size, repeat loop, and geo radius."
        ),
        retrieval_triggers=[
            "Google ads not working",
            "bakery marketing",
            "local business marketing",
            "low budget marketing",
            "increase walk-ins",
        ],
    ),
    ScenarioCard(
        slug="bk024-first-ten-customers-founder-led-dream-list",
        title="First 10 customers: use founder-led ICP discovery and a Dream-10 list",
        source_type="discussion_operator_voice",
        trust_level="medium",
        domains=["sales", "marketing", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "manager_coo"],
        tags=["first_customers", "icp", "founder_sales", "customer_discovery", "outbound"],
        source_url="https://news.ycombinator.com/item?id=44544542",
        local_file="archives/business-knowledge/sales/discussions/hn_first_10_customers_2026.html",
        situation=(
            "A founder has a product but no audience, weak response from email/LinkedIn, and asks how to get the first 10 customers."
        ),
        decision_pressure=(
            "The easy answer is to buy lead lists or increase outreach volume. The better first move is specificity: exact buyer, exact pain, exact trigger, and real named prospects."
        ),
        mistake_or_risk=(
            "Do not treat first-10 acquisition as a volume game. If 20 likely buyers will not even talk about the problem, the market/ICP/pain may be wrong."
        ),
        recommended_next_action=(
            "Build a Dream-10 list of named accounts/people, map their pains and communities, run founder-led learning outreach, and ask for conversations before pitching. "
            "Use replies and silence to refine ICP and offer language."
        ),
        evidence_needed=[
            "Narrow ICP definition",
            "Named Dream-10 account/person list",
            "Pain hypothesis and why-now trigger",
            "Founder credibility or unfair advantage",
            "Outreach message and response data",
            "Community/association/event where buyers already gather",
        ],
        red_flags=[
            "The target buyer is described as everyone",
            "Founder wants to outsource before doing discovery",
            "Outreach message is product-led rather than buyer-problem-led",
            "No response even to learning conversations",
        ],
        agent_lesson=(
            "The first 10 customers teach the business what it is really selling. Sales should be narrow, founder-led, and diagnostic before it becomes scalable."
        ),
        hard_gate_candidate=(
            "Before suggesting volume outbound, require ICP, Dream-10 list, pain hypothesis, and learning-call script."
        ),
        retrieval_triggers=[
            "first 10 customers",
            "no audience",
            "cold email no response",
            "founder sales",
            "ICP discovery",
        ],
    ),
    ScenarioCard(
        slug="bk025-high-trust-b2b-free-diagnostic-to-paid-pilot",
        title="High-trust B2B: free diagnostic work can unlock proof, but needs a boundary",
        source_type="discussion_operator_voice",
        trust_level="medium",
        domains=["sales", "marketing", "finance", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "finance_agent", "manager_coo"],
        tags=["b2b_sales", "pilot", "proof_of_concept", "consultative_sales", "trust"],
        source_url="https://news.ycombinator.com/item?id=44544542",
        local_file="archives/business-knowledge/sales/discussions/hn_first_10_customers_2026.html",
        situation=(
            "A new B2B product solves an expensive or unfamiliar problem, but buyers do not yet trust the company or understand the outcome."
        ),
        decision_pressure=(
            "The seller needs proof and learning, but unpaid custom work can consume weeks and train buyers to expect free service."
        ),
        mistake_or_risk=(
            "Do not offer unlimited free consulting. Without a scope, success metric, paid-conversion trigger, and decision date, the tactic becomes free labor instead of sales."
        ),
        recommended_next_action=(
            "Offer a bounded diagnostic, audit, sample analysis, or custom demo using the prospect's real context. Define deliverable, timeline, mutual commitments, success metric, "
            "paid-pilot price, and conversion decision before starting."
        ),
        evidence_needed=[
            "Buyer problem severity and current cost",
            "Data/access needed for diagnostic",
            "Defined deliverable and time cap",
            "Decision-maker participation",
            "Paid-pilot trigger and price",
            "Proof that learning from the diagnostic improves product/positioning",
        ],
        red_flags=[
            "Prospect will not include a decision-maker",
            "Diagnostic scope keeps expanding",
            "No paid next step is agreed",
            "The work exposes confidential data without safeguards",
        ],
        agent_lesson=(
            "For high-trust sales, proof is often built before the contract. The COO move is to bound the proof so it creates learning and pipeline, not leakage."
        ),
        hard_gate_candidate=(
            "Before recommending a free pilot/diagnostic, require scope, time cap, success metric, data-safety check, and paid next step."
        ),
        retrieval_triggers=[
            "free consulting to get customers",
            "custom demo",
            "B2B pilot",
            "proof of concept",
            "high trust sales",
        ],
    ),
    ScenarioCard(
        slug="bk026-stage-and-acv-decide-channel-migration",
        title="Channel migration: the sales motion must change with stage and annual contract value",
        source_type="discussion_operator_voice",
        trust_level="medium",
        domains=["sales", "marketing", "finance", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "finance_agent", "manager_coo"],
        tags=["channel_strategy", "acv", "low_ticket", "seo", "word_of_mouth", "sales_efficiency"],
        source_url="https://news.ycombinator.com/item?id=41862332",
        local_file="archives/business-knowledge/sales/discussions/hn_first_100_users_2024.html",
        situation=(
            "A product gets early customers through personal network, manual help, or cold outreach. The owner then asks how to keep scaling the same way."
        ),
        decision_pressure=(
            "The early channel worked, so the owner assumes it should continue. But low annual value cannot carry heavy human sales time forever, while high-ticket deals may require it."
        ),
        mistake_or_risk=(
            "Do not judge a channel only by early wins. Evaluate CAC, sales time, payback, ACV, churn, and whether the channel is exhaustible."
        ),
        recommended_next_action=(
            "Separate validation channels from scale channels. Use network/outbound/manual onboarding to learn and close early; then migrate low-ACV products toward SEO, referrals, product-led loops, "
            "content, marketplace, or community compounding. Keep high-ACV/complex products in consultative or account-based motions."
        ),
        evidence_needed=[
            "Annual contract value or annual gross profit per customer",
            "Founder/sales hours per closed customer",
            "Conversion rate by channel",
            "Churn and repeat/renewal rate",
            "Whether current channel has enough reachable prospects",
            "Self-serve/onboarding readiness",
        ],
        red_flags=[
            "Low-ticket product depends on long founder calls",
            "Personal network is nearly exhausted",
            "Outbound appears cheap because founder time is not costed",
            "Scale plan ignores onboarding/support capacity",
        ],
        agent_lesson=(
            "Channels are stage-specific. The Sales Agent should ask: is this a learning channel, a repeatable channel, or a compounding channel?"
        ),
        hard_gate_candidate=(
            "Before recommending continued outbound/manual selling, require ACV, gross profit, sales hours, and payback."
        ),
        retrieval_triggers=[
            "first 100 users",
            "low ACV sales",
            "cold email not scalable",
            "SEO word of mouth",
            "sales channel migration",
        ],
    ),
    ScenarioCard(
        slug="bk027-niche-community-launch-works-when-you-belong",
        title="Niche community launch: Reddit/forums work when the seller is already useful there",
        source_type="discussion_operator_voice",
        trust_level="medium",
        domains=["sales", "marketing", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "manager_coo"],
        tags=["community_marketing", "reddit", "forums", "niche_launch", "content"],
        source_url="https://news.ycombinator.com/item?id=41862332",
        local_file="archives/business-knowledge/sales/discussions/hn_first_100_users_2024.html",
        situation=(
            "A niche product has a concentrated buyer community on Reddit, forums, Facebook groups, Discord, YouTube, or local associations."
        ),
        decision_pressure=(
            "The owner wants to post a launch link. The community may punish obvious promotion, but can reward real tutorials, problem-solving, and insider usefulness."
        ),
        mistake_or_risk=(
            "Do not enter a community as a drive-by advertiser. Link drops, fake enthusiasm, and non-native copy destroy trust and may get the account banned."
        ),
        recommended_next_action=(
            "Map community norms, become useful before pitching, publish educational/problem-solving content, show the product naturally inside the workflow, and capture demand with a respectful next step."
        ),
        evidence_needed=[
            "Specific community and rules",
            "Evidence that target buyers are active there",
            "Common problems/language from posts",
            "Useful tutorial/demo/content idea",
            "Disclosure plan if promotion is involved",
            "Capture mechanism: waitlist, DM, landing page, coupon, WhatsApp",
        ],
        red_flags=[
            "Community rules prohibit promotion",
            "The account has no prior contribution",
            "Post is framed around the product instead of the user problem",
            "The owner asks for fake reviews/upvotes/comments",
        ],
        agent_lesson=(
            "Community distribution is earned distribution. The moat is fluency in the community's pain, language, norms, and proof expectations."
        ),
        hard_gate_candidate=(
            "Before recommending a Reddit/forum launch, require community-rule check, native-value angle, disclosure decision, and no-astroturfing constraint."
        ),
        retrieval_triggers=[
            "Reddit launch",
            "community marketing",
            "niche product marketing",
            "forums for sales",
            "how to post without self promotion",
        ],
    ),
    ScenarioCard(
        slug="bk028-comment-sample-loop-for-service-demand",
        title="Comment-to-sample loops can create fast demand when fulfilment is simple and visible",
        source_type="discussion_operator_voice",
        trust_level="medium",
        domains=["sales", "marketing", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "manager_coo"],
        tags=["lead_generation", "sampling", "social_post", "viral_loop", "service_business"],
        source_url="https://news.ycombinator.com/item?id=41862332",
        local_file="archives/business-knowledge/sales/discussions/hn_first_100_users_2024.html",
        situation=(
            "A service or product can be demonstrated quickly with a free sample, mini-audit, estimate, design, trial, or personalized preview."
        ),
        decision_pressure=(
            "The owner wants leads fast and has an audience or group where prospects can visibly respond. Engagement can create more reach, but fulfilment and lead quality can break the loop."
        ),
        mistake_or_risk=(
            "Do not offer samples without capacity, qualification, or follow-up. A viral lead post can create operational chaos and many low-intent requests."
        ),
        recommended_next_action=(
            "Use a specific post or offline prompt: 'comment/message for a free sample/audit'. Cap the number, qualify by fit, fulfil quickly, then follow up with a paid offer, testimonial ask, and referral prompt."
        ),
        evidence_needed=[
            "Sample fulfilment time per lead",
            "Audience/community where target buyers are present",
            "Qualification criteria",
            "Follow-up offer and price",
            "Capacity cap",
            "Tracking code/source tag",
        ],
        red_flags=[
            "Sample takes too long or costs too much",
            "No paid next step exists",
            "The audience is broad and unqualified",
            "The owner cannot respond within the promised window",
        ],
        agent_lesson=(
            "Sampling is not a giveaway; it is a trust-building conversion step. It works best when the sample is cheap to fulfil and visibly demonstrates value."
        ),
        hard_gate_candidate=(
            "Before recommending a free-sample/comment-loop campaign, require capacity cap, qualification rule, fulfilment time, and paid follow-up."
        ),
        retrieval_triggers=[
            "free sample campaign",
            "Facebook post leads",
            "comment below for sample",
            "service business leads",
            "lead generation tactic",
        ],
    ),
    ScenarioCard(
        slug="bk029-campaign-success-operating-system",
        title="Campaign success: combine buyer insight, offer, proof, channel fit, follow-up, and measurement",
        source_type="platform_guidance_plus_local_synthesis",
        trust_level="high",
        domains=["marketing", "sales", "finance", "manager_coo"],
        agent_targets=["marketing_agent", "sales_agent", "finance_agent", "manager_coo"],
        tags=["campaign_strategy", "marketing_measurement", "offer", "creative", "kpi"],
        source_url="https://www.business.reddit.com/smb/grow-business-with-digital-marketing",
        local_file="archives/business-knowledge/marketing/discussions/reddit_business_digital_marketing.html",
        situation=(
            "The owner asks for a marketing campaign idea, but has not specified audience, trigger, offer, trust proof, channel, follow-up, or measurement."
        ),
        decision_pressure=(
            "The Marketing Agent may jump to creative ideas. The COO-grade answer is to design the operating system of the campaign before the ad/post/message."
        ),
        mistake_or_risk=(
            "Do not define success as reach, likes, or impressions unless the business goal is awareness and that choice is explicit. For sales campaigns, measurement must connect to leads, orders, margin, or repeat behavior."
        ),
        recommended_next_action=(
            "Create a one-page campaign brief: target buyer and situation, insight, promise, offer, proof, channel, creative format, landing/capture path, follow-up cadence, KPI, budget, CAC/payback target, and learning question."
        ),
        evidence_needed=[
            "Business objective: awareness, leads, walk-ins, orders, repeat, referrals",
            "Target buyer and trigger",
            "Offer and next step",
            "Trust proof: reviews, samples, case study, guarantee, certification, demo",
            "Channel and creative format",
            "Tracking: UTM, coupon, QR, phone, WhatsApp tag, CRM source",
            "Budget, CAC ceiling, and payback period",
        ],
        red_flags=[
            "Campaign idea has no capture/follow-up mechanism",
            "KPI is vanity-only for a revenue campaign",
            "Offer is unclear or too hard to redeem",
            "Creative promise is not backed by proof",
        ],
        agent_lesson=(
            "Great campaigns are not just clever creatives. They are full conversion systems that match buyer moment, proof, channel, and economics."
        ),
        hard_gate_candidate=(
            "Before producing campaign copy, require objective, buyer, offer, proof, channel, follow-up, and KPI."
        ),
        retrieval_triggers=[
            "marketing campaign idea",
            "campaign success",
            "marketing KPI",
            "campaign brief",
            "brand campaign",
        ],
    ),
    ScenarioCard(
        slug="bk030-digital-physical-hybrid-marketing-loop",
        title="Digital vs physical marketing: use physical trust and digital compounding together",
        source_type="local_synthesis",
        trust_level="high",
        domains=["marketing", "sales", "manager_coo"],
        agent_targets=["marketing_agent", "sales_agent", "manager_coo"],
        tags=["physical_marketing", "digital_marketing", "offline_to_online", "local_marketing", "hybrid_campaign"],
        source_url="archives/business-knowledge/sales/discussions/DISCUSSION_SIGNAL_INDEX.md",
        local_file="archives/business-knowledge/sales/discussions/DISCUSSION_SIGNAL_INDEX.md",
        situation=(
            "A business serves local or relationship-heavy customers and is unsure whether to invest in digital marketing, physical marketing, events, signage, WhatsApp, or local partnerships."
        ),
        decision_pressure=(
            "Digital feels modern and measurable; physical feels trusted but harder to track. The wrong answer is to choose one by ideology instead of buyer behavior."
        ),
        mistake_or_risk=(
            "Do not run offline activity without a capture path, and do not run digital ads when the buyer's trust is built through physical proof, local presence, or personal relationship."
        ),
        recommended_next_action=(
            "Design a hybrid loop: offline proof or presence creates trust; QR/WhatsApp/coupon/review request captures the relationship; digital follow-up compounds through reminders, referrals, retargeting, and repeat orders."
        ),
        evidence_needed=[
            "Buyer geography and purchase context",
            "Trust assets available offline and online",
            "Footfall/event/partner touchpoints",
            "QR/WhatsApp/landing-page capture path",
            "Review/referral/repeat-purchase workflow",
            "Source tracking method",
        ],
        red_flags=[
            "Flyers/events/signage have no source code or contact capture",
            "Ads target people who need in-person trust first",
            "No one owns follow-up after offline interaction",
            "Offline campaign cannot be tied to orders/leads even roughly",
        ],
        agent_lesson=(
            "The mature SMB move is not digital versus physical. It is physical trust plus digital memory, follow-up, and measurement."
        ),
        hard_gate_candidate=(
            "Before recommending offline spend, require capture path and follow-up owner; before recommending digital spend, require trust/proof assets."
        ),
        retrieval_triggers=[
            "digital vs physical marketing",
            "offline marketing",
            "flyers to WhatsApp",
            "local event marketing",
            "hybrid marketing",
        ],
    ),
    ScenarioCard(
        slug="bk031-partner-network-co-selling-borrows-trust",
        title="Partner networks borrow trust when direct selling is slow",
        source_type="discussion_operator_voice",
        trust_level="medium",
        domains=["sales", "marketing", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "manager_coo"],
        tags=["partnerships", "channel_sales", "co_selling", "b2b_sales", "distribution"],
        source_url="https://news.ycombinator.com/item?id=41862332",
        local_file="archives/business-knowledge/sales/discussions/hn_first_100_users_2024.html",
        situation=(
            "A B2B product has clear value but direct access to buyers is slow. Adjacent service providers, integrators, consultants, associations, or suppliers already serve those buyers."
        ),
        decision_pressure=(
            "The owner can keep building a direct pipeline or recruit partners who already have trust, but partners need a reason to care and a clear role."
        ),
        mistake_or_risk=(
            "Do not ask partners to promote a vague product. Without a tight USP, enablement, commercial model, and customer handoff, partnerships become polite conversations."
        ),
        recommended_next_action=(
            "Map 10 partner types that already influence the buyer, define the mutual win, create a one-page enablement kit, run co-selling pilots, and keep direct involvement in early customer discovery."
        ),
        evidence_needed=[
            "Buyer decision journey and trusted advisors",
            "Partner category list",
            "Mutual incentive or revenue share",
            "Non-competing/complementary positioning",
            "Enablement assets and demo",
            "Handoff/follow-up workflow",
        ],
        red_flags=[
            "Partner sells a directly competitive offer",
            "USP is not crisp enough for a partner to repeat",
            "No commercial incentive exists",
            "Customer ownership and support expectations are unclear",
        ],
        agent_lesson=(
            "Partnership is a trust and distribution tactic, not a logo-collection exercise. It works when the partner already owns attention and the business makes them look good."
        ),
        hard_gate_candidate=(
            "Before recommending partnerships, require partner-buyer map, mutual win, USP, enablement asset, and handoff workflow."
        ),
        retrieval_triggers=[
            "partner channel",
            "co selling",
            "B2B distribution",
            "integrator partners",
            "borrow trust",
        ],
    ),
    ScenarioCard(
        slug="bk032-brand-building-memory-proof-and-promise",
        title="Brand building: create memory around a promise, then prove it repeatedly",
        source_type="platform_guidance_plus_local_synthesis",
        trust_level="high",
        domains=["marketing", "sales", "manager_coo"],
        agent_targets=["marketing_agent", "sales_agent", "manager_coo"],
        tags=["brand_building", "trust", "campaign_strategy", "memory_assets", "positioning"],
        source_url="https://www.business.reddit.com/learning-hub/articles/viral-marketing",
        local_file="archives/business-knowledge/marketing/discussions/reddit_business_viral_marketing.html",
        situation=(
            "The owner asks how to build a brand, often thinking first about logo, colors, reels, or viral campaigns."
        ),
        decision_pressure=(
            "A visible campaign can create attention, but brand value comes from repeated memory and trust. The Manager has to connect promise, proof, delivery, and customer experience."
        ),
        mistake_or_risk=(
            "Do not define brand as creative assets alone. A campaign that gets attention but creates a promise the business cannot deliver damages trust."
        ),
        recommended_next_action=(
            "Define the brand promise, the buyer's category cue, proof assets, memory assets, tone, customer-experience standards, and repeatable content/event themes. Build campaigns around proof of the promise."
        ),
        evidence_needed=[
            "Target buyer and buying trigger",
            "Brand promise in one sentence",
            "Proof: reviews, outcomes, case studies, demos, certifications, founder story",
            "Distinctive memory assets: name, phrase, visual cue, mascot, ritual, packaging, offer",
            "Customer-experience behaviors that prove the promise",
            "Campaign calendar and consistency plan",
        ],
        red_flags=[
            "Brand promise is generic",
            "Creative claims exceed operational reality",
            "No distinctive memory cue exists",
            "Campaign is optimized for virality but not trust or sales",
        ],
        agent_lesson=(
            "A great SMB brand is a reliable mental shortcut: when this need arises, the customer remembers the business and trusts the promise."
        ),
        hard_gate_candidate=(
            "Before recommending a brand campaign, require promise, proof, memory asset, and delivery behavior."
        ),
        retrieval_triggers=[
            "brand building",
            "viral marketing",
            "brand campaign",
            "build trust",
            "marketing campaign successful",
        ],
    ),
    ScenarioCard(
        slug="bk033-presell-commitments-before-build-or-scale",
        title="Pre-sell and commitment tests: demand proof should precede build or scale",
        source_type="discussion_operator_voice",
        trust_level="medium",
        domains=["sales", "marketing", "finance", "manager_coo"],
        agent_targets=["sales_agent", "marketing_agent", "finance_agent", "manager_coo"],
        tags=["presell", "validation", "demand_testing", "pilot", "product_market_fit"],
        source_url="https://news.ycombinator.com/item?id=44544542",
        local_file="archives/business-knowledge/sales/discussions/hn_first_10_customers_2026.html",
        situation=(
            "A founder or SMB wants to build a new product/service line or spend on promotion before proving that the intended buyer will commit."
        ),
        decision_pressure=(
            "Building feels productive and marketing spend feels like progress. The safer COO move is to test real commitment: deposits, LOIs, pilot agreements, waitlist with action, or paid discovery."
        ),
        mistake_or_risk=(
            "Do not confuse compliments, survey interest, or social likes with buying intent. Also avoid overselling future capability in a way that creates reputational or delivery risk."
        ),
        recommended_next_action=(
            "Design a commitment test appropriate to the offer: refundable deposit, paid pilot, booked consultation, pre-order, signed LOI, waitlist plus qualification call, or partner-introduction target. "
            "Use the result to decide build, reposition, or stop."
        ),
        evidence_needed=[
            "Buyer pain and urgency",
            "Price or deposit threshold",
            "Commitment mechanism",
            "Delivery feasibility and risk",
            "Refund/terms clarity",
            "Number of qualified commitments needed for go/no-go",
        ],
        red_flags=[
            "Only verbal enthusiasm exists",
            "Founder promises features or timelines they cannot deliver",
            "No refund/terms clarity",
            "Commitments come from non-target buyers",
        ],
        agent_lesson=(
            "The strongest market signal is behavior with cost: money, time, data access, introduction, or signed commitment. The agent should look for that before recommending scale."
        ),
        hard_gate_candidate=(
            "Before recommending a new build or campaign scale-up, require a defined commitment test and success threshold."
        ),
        retrieval_triggers=[
            "presell",
            "validate demand",
            "people say interested but do not buy",
            "before building",
            "paid pilot",
        ],
    ),
    ScenarioCard(
        slug="bk034-coo-role-strategy-to-execution-not-super-specialist",
        title="COO role boundary: translate strategy into execution without becoming every specialist",
        source_type="deep_research_synthesis",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["coo_role", "strategy_execution", "delegation", "manager_judgment", "operating_model"],
        source_url="https://www.mckinsey.com/capabilities/operations/our-insights/delivering-the-strategy-the-coo-agenda",
        local_file="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        situation=(
            "The Manager is asked to make or approve decisions across Sales, Marketing, Finance, Compliance, and Operations, but cannot and should not become the deepest expert in each domain."
        ),
        decision_pressure=(
            "The business needs one accountable coordinator who sees the whole operating picture. The risk is that the Manager either overrules specialists without understanding, or blindly accepts specialist recommendations without judging business impact."
        ),
        mistake_or_risk=(
            "Do not make the Manager a pseudo-specialist. Its job is to understand enough to ask the right questions, test whether the recommendation benefits the business, identify risk, assign owners, and escalate when expert judgment is required."
        ),
        recommended_next_action=(
            "Frame the Manager as strategy-to-execution owner: clarify the business objective, ask the relevant specialist for domain options, require assumptions/evidence/risks, choose the path that protects business value, and track execution."
        ),
        evidence_needed=[
            "Business objective and constraint",
            "Specialist recommendation with assumptions",
            "Expected business impact",
            "Risks to cashflow, trust, compliance, delivery, and reputation",
            "Decision owner and execution owner",
            "Metric and review date",
        ],
        red_flags=[
            "Manager gives tactical domain advice without specialist input",
            "Specialist optimizes one metric while harming cashflow/trust/compliance",
            "No owner exists for execution",
            "Decision has cross-functional impact but only one function was consulted",
        ],
        agent_lesson=(
            "The Manager's expertise is decision quality and business protection. Specialists own depth; the Manager owns trade-offs, ownership, sequencing, and whether the decision helps the business."
        ),
        hard_gate_candidate=(
            "For cross-functional or high-risk decisions, require specialist recommendation plus Manager decision summary before execution."
        ),
        retrieval_triggers=[
            "Manager role",
            "COO decision",
            "who should decide",
            "specialist recommendation",
            "business manager judgment",
        ],
    ),
    ScenarioCard(
        slug="bk035-decision-triage-impact-reversibility-uncertainty",
        title="Decision triage: match process depth to impact, reversibility, urgency, and uncertainty",
        source_type="deep_research_synthesis",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["decision_triage", "reversibility", "uncertainty", "risk", "speed"],
        source_url="https://www.mckinsey.com/featured-insights/mckinsey-explainers/what-is-decision-making",
        local_file="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        situation=(
            "A decision arrives and the team is unsure whether to act quickly, analyze deeply, run a test, or escalate."
        ),
        decision_pressure=(
            "Too much analysis slows reversible choices; too little analysis can damage the business when decisions are expensive, regulated, irreversible, or reputation-sensitive."
        ),
        mistake_or_risk=(
            "Do not use the same decision process for every issue. A ₹5,000 reversible ad test and a regulatory-risk WhatsApp campaign should not receive the same review depth."
        ),
        recommended_next_action=(
            "Classify the decision on four dimensions: business impact, reversibility, urgency, and uncertainty. Fast-track low-impact reversible choices; run bounded experiments for uncertain choices; require specialist review and controls for high-impact or irreversible choices."
        ),
        evidence_needed=[
            "Estimated upside and downside",
            "Is the decision reversible or hard to unwind?",
            "Time sensitivity",
            "Uncertainty level and missing evidence",
            "Compliance/customer/reputation exposure",
            "Cost of delay versus cost of being wrong",
        ],
        red_flags=[
            "Decision could violate law/platform policy",
            "Decision can materially harm cashflow or customer trust",
            "Decision is irreversible but treated as routine",
            "Urgency is asserted without evidence",
        ],
        agent_lesson=(
            "Good COO judgment is process matching. The Manager should spend decision effort where the downside and irreversibility justify it."
        ),
        hard_gate_candidate=(
            "Before approving any high-impact or irreversible decision, require impact/reversibility/urgency/uncertainty classification."
        ),
        retrieval_triggers=[
            "how much analysis",
            "reversible decision",
            "urgent decision",
            "decision triage",
            "when to escalate",
        ],
    ),
    ScenarioCard(
        slug="bk036-context-matching-cynefin-decision-mode",
        title="Context matching: do not force one decision style onto obvious, complicated, complex, and chaotic situations",
        source_type="deep_research_synthesis",
        trust_level="medium",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["cynefin", "sensemaking", "complexity", "decision_context", "uncertainty"],
        source_url="https://hbr.org/2007/11/a-leaders-framework-for-decision-making",
        local_file="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        situation=(
            "A business problem may be routine, expert-solvable, emergent/uncertain, or actively chaotic, but the team is applying the same familiar leadership response."
        ),
        decision_pressure=(
            "Managers often over-process routine work and under-experiment in complex work. In a crisis, they may continue debating when stabilizing action is needed."
        ),
        mistake_or_risk=(
            "Do not apply SOPs to complex situations where cause/effect is not yet knowable, and do not run open-ended experiments in obvious situations that already have proven rules."
        ),
        recommended_next_action=(
            "Classify context: obvious/routine needs standard practice; complicated needs expert analysis; complex needs safe-to-fail experiments and sensing; chaotic needs immediate stabilizing action, then learning."
        ),
        evidence_needed=[
            "Is cause/effect known, knowable by experts, emergent, or absent?",
            "Existing SOP or standard",
            "Expert input needed",
            "Safe-to-fail test options",
            "Immediate harm requiring containment",
        ],
        red_flags=[
            "A new market/customer behavior is treated as routine",
            "A routine compliance step is reinvented",
            "Crisis response is stuck in analysis",
            "The team claims certainty in a complex situation without evidence",
        ],
        agent_lesson=(
            "The Manager should first ask what kind of situation this is. The right decision process changes with context."
        ),
        hard_gate_candidate=(
            "For uncertain or crisis decisions, require context classification before recommending action."
        ),
        retrieval_triggers=[
            "complex decision",
            "uncertain market",
            "crisis decision",
            "SOP or experiment",
            "Cynefin",
        ],
    ),
    ScenarioCard(
        slug="bk037-decision-rights-one-d-prevents-drift",
        title="Decision rights: clarify who recommends, agrees, decides, performs, and informs",
        source_type="consulting_framework",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["rapid", "decision_rights", "accountability", "governance", "execution"],
        source_url="https://preprodcms.bain.com/insights/who-has-d-how-clear-decision-roles-enhance-organizational-performance/",
        local_file="archives/business-knowledge/manager/decision-making/bain_who_has_the_d_rapid.html",
        situation=(
            "A decision stalls because multiple people think they can approve, veto, revise, or reopen it."
        ),
        decision_pressure=(
            "Everyone wants input, nobody wants accountability, and execution waits while meetings multiply."
        ),
        mistake_or_risk=(
            "Do not allow ambiguous decision ownership. A decision without one accountable decider becomes delay, politics, or rework."
        ),
        recommended_next_action=(
            "Assign RAPID-style roles before debating details: who recommends, who must agree, who decides, who performs, and who is informed. Keep veto/agreement roles sparse and publish the decision owner."
        ),
        evidence_needed=[
            "Decision statement",
            "Recommendation owner",
            "Required agree/veto parties",
            "Single decider",
            "Execution owner",
            "People who need input or information only",
        ],
        red_flags=[
            "More than one person claims final say",
            "Compliance/legal signoff is missing where required",
            "People with input behave like veto holders",
            "Decision is repeatedly reopened after closure",
        ],
        agent_lesson=(
            "The Manager protects execution speed by clarifying decision rights. One D prevents drift."
        ),
        hard_gate_candidate=(
            "For cross-functional decisions, require explicit Recommend/Agree/Decide/Perform/Informed roles."
        ),
        retrieval_triggers=[
            "who decides",
            "decision stuck",
            "too many approvals",
            "RAPID",
            "decision rights",
        ],
    ),
    ScenarioCard(
        slug="bk038-business-case-quality-before-approval",
        title="Business case quality: approve only when objective, options, assumptions, economics, and risks are visible",
        source_type="deep_research_synthesis",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["business_case", "decision_quality", "tradeoffs", "assumptions", "roi"],
        source_url="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        local_file="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        situation=(
            "A specialist or team asks the Manager to approve a spend, campaign, process change, hiring move, tool, partnership, or operational decision."
        ),
        decision_pressure=(
            "The recommendation may sound persuasive, but the Manager cannot judge business benefit unless the alternatives, assumptions, economics, risks, and owner are explicit."
        ),
        mistake_or_risk=(
            "Do not approve a proposal because it is polished, urgent, or favored by a confident specialist. Good decision quality requires comparison and trade-offs."
        ),
        recommended_next_action=(
            "Ask for a one-page business case: objective, options including do-nothing, expected upside, costs, assumptions, risks, reversibility, owner, metric, timeline, and stop/continue threshold."
        ),
        evidence_needed=[
            "Decision objective",
            "Alternatives considered",
            "Assumptions and evidence",
            "Expected upside and downside",
            "Cost, cashflow, and resource impact",
            "Risks and mitigations",
            "Owner, timeline, KPI, stop condition",
        ],
        red_flags=[
            "Only one option is presented",
            "Do-nothing alternative is not considered",
            "Assumptions are unstated",
            "No KPI or stop condition exists",
            "Recommendation optimizes one function while harming the business",
        ],
        agent_lesson=(
            "A COO should not be swayed by confidence. It should approve the decision process and business case, not the charisma of the recommendation."
        ),
        hard_gate_candidate=(
            "Before approving spend/process/hiring/tool decisions above a threshold, require a business-case card."
        ),
        retrieval_triggers=[
            "approve proposal",
            "business case",
            "ROI decision",
            "should we spend",
            "decision assumptions",
        ],
    ),
    ScenarioCard(
        slug="bk039-bias-and-incentive-distortion-check",
        title="Bias check: protect decisions from confirmation, groupthink, sunk cost, overconfidence, and incentive distortion",
        source_type="deep_research_synthesis",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["bias", "debiasing", "groupthink", "sunk_cost", "incentives"],
        source_url="https://www.mckinsey.com/capabilities/risk-and-resilience/our-insights/the-business-logic-in-debiasing",
        local_file="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        situation=(
            "A team is confident in a decision, but the evidence may be selectively chosen, the sponsor may have incentives, or dissenting views are absent."
        ),
        decision_pressure=(
            "Fast agreement feels efficient. But big mistakes often come from biased inputs, political incentives, or teams aligning with the loudest/senior person."
        ),
        mistake_or_risk=(
            "Do not rely on awareness of bias alone. Build debiasing into the decision process through contrary evidence, base rates, pre-mortems, red teams, and explicit assumptions."
        ),
        recommended_next_action=(
            "Run a bias check: what evidence would disprove this, what comparable cases say, what the pre-mortem failure story is, who benefits personally, and who disagrees with reasons."
        ),
        evidence_needed=[
            "Disconfirming evidence",
            "Base rate or comparable cases",
            "Pre-mortem failure modes",
            "Sponsor incentives",
            "Dissenting viewpoint",
            "Assumptions list",
        ],
        red_flags=[
            "Only supporting evidence is presented",
            "No one disagrees in a high-stakes decision",
            "Sponsor personally benefits from approval",
            "Prior spend is used as the reason to continue",
            "Forecasts are optimistic without sensitivity analysis",
        ],
        agent_lesson=(
            "The Manager should design bias out of the process. It is not enough to tell people to be objective."
        ),
        hard_gate_candidate=(
            "For high-stakes decisions, require pre-mortem, disconfirming evidence, and incentive/conflict check."
        ),
        retrieval_triggers=[
            "decision bias",
            "confirmation bias",
            "groupthink",
            "sunk cost",
            "pre mortem",
        ],
    ),
    ScenarioCard(
        slug="bk040-a3-problem-definition-before-solution",
        title="A3 discipline: define the real problem before approving solutions",
        source_type="lean_management",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["a3", "problem_solving", "root_cause", "pdca", "continuous_improvement"],
        source_url="https://www.lean.org/lexicon-terms/a3-report/",
        local_file="archives/business-knowledge/manager/decision-making/lean_a3_report.html",
        situation=(
            "The team jumps to a solution—hire, automate, discount, advertise, add a tool, change vendor—before proving the real problem and root cause."
        ),
        decision_pressure=(
            "Action feels better than analysis, especially during firefighting. But a wrong solution wastes time and may hide the true constraint."
        ),
        mistake_or_risk=(
            "Do not approve a countermeasure when the current condition, performance gap, root cause, owner, and success measure are missing."
        ),
        recommended_next_action=(
            "Use A3 thinking: background, current condition, target condition, gap, root cause, countermeasures, implementation owner, expected result, check date, and follow-up standard."
        ),
        evidence_needed=[
            "Problem statement in measurable terms",
            "Current condition and target condition",
            "Root-cause analysis",
            "Countermeasure options",
            "Implementation plan",
            "Success metric",
            "Follow-up cadence",
        ],
        red_flags=[
            "Problem is framed as a solution",
            "No measurable performance gap exists",
            "Root cause is assumed",
            "No check/adjust loop is planned",
            "Stakeholders disagree on what the problem is",
        ],
        agent_lesson=(
            "The Manager should slow down bad action by insisting on problem clarity. The right problem statement is often half the decision."
        ),
        hard_gate_candidate=(
            "Before approving process fixes, require an A3-style problem statement and success metric."
        ),
        retrieval_triggers=[
            "root cause",
            "problem solving",
            "A3",
            "team jumped to solution",
            "process improvement",
        ],
    ),
    ScenarioCard(
        slug="bk041-operating-model-fit-before-transformation",
        title="Operating model fit: strategy must translate into capabilities, process, governance, data, and measurement",
        source_type="consulting_framework",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["operating_model", "strategy_execution", "governance", "capabilities", "transformation"],
        source_url="https://www.deloitte.com/us/en/services/consulting/articles/operating-model-transformation.html",
        local_file="archives/business-knowledge/manager/decision-making/deloitte_operating_model_advantage.html",
        situation=(
            "The business has a strategic intent—growth, better service, lower cost, automation, market expansion—but execution keeps falling short."
        ),
        decision_pressure=(
            "Leaders may blame people or tools while the real issue is misalignment between strategy and the operating model."
        ),
        mistake_or_risk=(
            "Do not approve transformations as isolated projects. If capabilities, processes, technology, data, roles, governance, incentives, and measurement do not align, value leaks before execution lands."
        ),
        recommended_next_action=(
            "Map the operating model needed for the strategy: value streams, capabilities, processes, roles, data, tools, governance, decision rights, metrics, talent, and change plan. Prioritize bottlenecks that block value."
        ),
        evidence_needed=[
            "Strategic objective",
            "Current value stream and bottlenecks",
            "Required capabilities",
            "Process and role gaps",
            "Technology/data readiness",
            "Governance and decision rights",
            "Value tracking metric",
        ],
        red_flags=[
            "Tool purchase is treated as transformation",
            "No process owner exists",
            "Teams optimize silos instead of end-to-end value",
            "Metrics do not track strategic outcome",
            "Incentives conflict with the new operating model",
        ],
        agent_lesson=(
            "The Manager should ask whether the business is designed to deliver the strategy. Intent without operating-model fit becomes yield loss."
        ),
        hard_gate_candidate=(
            "Before approving transformation work, require operating-model impact map and value-tracking plan."
        ),
        retrieval_triggers=[
            "operating model",
            "strategy not executed",
            "transformation failing",
            "process governance",
            "value stream",
        ],
    ),
    ScenarioCard(
        slug="bk042-risk-based-authorization-before-sensitive-action",
        title="Risk-based authorization: prepare, categorize, control, assess, authorize, and monitor sensitive decisions",
        source_type="official_risk_framework",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["risk_management", "controls", "authorization", "monitoring", "governance"],
        source_url="https://csrc.nist.gov/Projects/risk-management/about-rmf",
        local_file="archives/business-knowledge/manager/decision-making/nist_risk_management_framework.html",
        situation=(
            "The business wants to launch an action involving data, customer communication, automation, regulated activity, third-party tooling, or operational exposure."
        ),
        decision_pressure=(
            "The action may create growth or efficiency, but it can also expose the business to security, privacy, compliance, service, or reputational harm."
        ),
        mistake_or_risk=(
            "Do not approve sensitive action without risk categorization, controls, assessment, authorization owner, and monitoring plan."
        ),
        recommended_next_action=(
            "Use a lightweight risk authorization loop: prepare context, categorize impact, select controls, implement controls, assess readiness, authorize with accountable owner, then monitor."
        ),
        evidence_needed=[
            "Activity description and data/customer/process touched",
            "Impact category",
            "Controls needed",
            "Implementation proof",
            "Assessment result",
            "Authorizing owner",
            "Monitoring cadence and incident trigger",
        ],
        red_flags=[
            "Customer data or regulated communication is involved",
            "Third-party tool has unclear data handling",
            "No rollback/incident plan exists",
            "Controls are assumed but not verified",
            "Owner asks to launch before assessment",
        ],
        agent_lesson=(
            "Risk is not a reason to freeze; it is a reason to authorize deliberately. The Manager protects the business by making risk visible and controlled."
        ),
        hard_gate_candidate=(
            "For data/compliance/customer-sensitive actions, require risk authorization fields before execution."
        ),
        retrieval_triggers=[
            "risk approval",
            "sensitive launch",
            "customer data",
            "controls",
            "authorize decision",
        ],
    ),
    ScenarioCard(
        slug="bk043-after-action-review-turn-decisions-into-learning",
        title="After Action Review: every meaningful decision should become reusable learning",
        source_type="official_learning_framework",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["after_action_review", "learning_loop", "sop", "continuous_improvement", "decision_log"],
        source_url="https://www.army.mil/article/283214/tmd_publishes_training_circular_to_augment_fm_and_adp_7_0",
        local_file="archives/business-knowledge/manager/decision-making/us_army_after_action_reviews_2025.html",
        situation=(
            "A campaign, operational change, customer issue, compliance incident, hiring decision, or process experiment has completed or failed."
        ),
        decision_pressure=(
            "The team wants to move on quickly. Without a learning loop, the business repeats mistakes and loses the value of experience."
        ),
        mistake_or_risk=(
            "Do not let outcomes disappear into memory. Uncaptured lessons create repeated failure, inconsistent SOPs, and dependency on individual recollection."
        ),
        recommended_next_action=(
            "Run a short AAR: what was expected, what actually happened, why it happened, what worked, what failed, what changes now, who owns the SOP/update, and where the lesson is stored."
        ),
        evidence_needed=[
            "Original goal and assumptions",
            "Actual result",
            "Variance between expected and actual",
            "Observed strengths and weaknesses",
            "Root causes or contributing factors",
            "SOP/process/card update owner",
            "Follow-up action and due date",
        ],
        red_flags=[
            "Same failure has occurred before",
            "No one owns the lesson",
            "Result is judged only by outcome, not process quality",
            "SOPs remain unchanged after a material event",
        ],
        agent_lesson=(
            "The Manager turns experience into organizational memory. A decision that teaches nothing after execution is wasted twice."
        ),
        hard_gate_candidate=(
            "For meaningful campaigns/incidents/experiments, require AAR capture before closing the work."
        ),
        retrieval_triggers=[
            "after action review",
            "post mortem",
            "learn from decision",
            "SOP update",
            "campaign failed",
        ],
    ),
    ScenarioCard(
        slug="bk044-operating-cadence-metrics-and-leading-indicators",
        title="Operating cadence: decisions need metrics, review rhythm, and leading indicators",
        source_type="deep_research_synthesis",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["operating_cadence", "metrics", "leading_indicators", "reviews", "execution"],
        source_url="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        local_file="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        situation=(
            "A decision is approved, but execution success depends on tracking the right signals and reviewing them before failure becomes expensive."
        ),
        decision_pressure=(
            "Teams often measure late outcomes or vanity metrics. The Manager needs enough visibility to intervene early without micromanaging specialists."
        ),
        mistake_or_risk=(
            "Do not approve work with only lagging or vanity metrics. Without leading indicators and review cadence, the business discovers problems too late."
        ),
        recommended_next_action=(
            "Define one outcome metric, two to three leading indicators, owner, review rhythm, escalation threshold, and decision date for continue/change/stop."
        ),
        evidence_needed=[
            "Outcome metric tied to business value",
            "Leading indicators",
            "Owner",
            "Review cadence",
            "Escalation threshold",
            "Decision date",
            "Source of truth for measurement",
        ],
        red_flags=[
            "Metric is likes/impressions/activity without business link",
            "No review date exists",
            "Owner cannot access the data",
            "Team cannot explain what early signal predicts success/failure",
        ],
        agent_lesson=(
            "The Manager should govern through cadence and signals. Good metrics let specialists execute while the COO protects the business."
        ),
        hard_gate_candidate=(
            "Before marking a decision approved, require outcome metric, leading indicators, review cadence, and stop/change threshold."
        ),
        retrieval_triggers=[
            "operating review",
            "leading indicators",
            "KPI cadence",
            "how to track decision",
            "vanity metrics",
        ],
    ),
    ScenarioCard(
        slug="bk045-business-benefit-tradeoff-guardrail",
        title="Business-benefit guardrail: no decision should win one metric while harming the business",
        source_type="codex_owned_business_synthesis",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["business_benefit", "tradeoffs", "cashflow", "trust", "risk"],
        source_url="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        local_file="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        situation=(
            "A specialist recommends an action that improves their domain metric—sales volume, reach, cost reduction, tax position, automation speed—but may hurt another part of the business."
        ),
        decision_pressure=(
            "Domain metrics can seduce the business into bad local optimization: more sales with poor margin, more leads with low trust, lower cost with worse service, faster automation with compliance exposure."
        ),
        mistake_or_risk=(
            "Do not approve decisions that are good for a dashboard but bad for the business. The Manager must judge whole-business benefit."
        ),
        recommended_next_action=(
            "Run a trade-off check across cashflow, margin, customer trust, compliance, delivery capacity, team workload, strategic fit, and reversibility. Ask specialists to revise the plan if any critical dimension is harmed."
        ),
        evidence_needed=[
            "Primary metric improved",
            "Secondary impacts on cashflow/margin/trust/compliance/delivery/team",
            "Strategic fit",
            "Risk mitigations",
            "Reversibility",
            "Net business-benefit statement",
        ],
        red_flags=[
            "Growth improves but cashflow worsens",
            "Marketing reach improves but customer trust weakens",
            "Cost falls but service quality drops",
            "Compliance risk is accepted for short-term gain",
            "Specialist cannot explain trade-offs",
        ],
        agent_lesson=(
            "The Manager is the anti-local-optimization layer. Its job is to ensure every decision serves the business as a system."
        ),
        hard_gate_candidate=(
            "For any specialist recommendation, require a net business-benefit and trade-off statement."
        ),
        retrieval_triggers=[
            "is this good for business",
            "trade off decision",
            "growth vs margin",
            "cashflow risk",
            "local optimization",
        ],
    ),
    ScenarioCard(
        slug="bk046-specialist-arbitration-and-escalation",
        title="Specialist arbitration: resolve conflicting expert advice through objective, risk, evidence, and escalation",
        source_type="codex_owned_business_synthesis",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["specialist_arbitration", "escalation", "cross_functional", "manager_judgment", "decision_process"],
        source_url="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        local_file="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        situation=(
            "Two or more specialist agents disagree: Sales wants speed, Marketing wants spend, Finance wants cash protection, Compliance wants restraint, Operations flags capacity."
        ),
        decision_pressure=(
            "The Manager must prevent deadlock without pretending to be the deeper expert in each field."
        ),
        mistake_or_risk=(
            "Do not average expert advice or choose the loudest specialist. Also do not let one specialist veto business progress without explaining risk magnitude and mitigations."
        ),
        recommended_next_action=(
            "Arbitrate by restating objective, constraints, options, evidence quality, risk severity, reversibility, customer impact, and business benefit. If regulated/legal/accounting uncertainty remains material, escalate to a human expert."
        ),
        evidence_needed=[
            "Each specialist's recommendation",
            "Objective and non-negotiable constraints",
            "Evidence quality by recommendation",
            "Risk severity and mitigations",
            "Reversibility and fallback",
            "Escalation need",
        ],
        red_flags=[
            "Compliance/legal uncertainty is material",
            "Finance cannot quantify cashflow impact",
            "Sales/Marketing claim urgency without downside analysis",
            "Operations cannot fulfill the proposed demand",
            "Customer trust exposure is unresolved",
        ],
        agent_lesson=(
            "The Manager does not need to be the best specialist; it needs to be the best arbiter of objective, risk, evidence, trade-off, and escalation."
        ),
        hard_gate_candidate=(
            "When specialist recommendations conflict, require a Manager arbitration note before action."
        ),
        retrieval_triggers=[
            "specialists disagree",
            "sales vs compliance",
            "finance vs marketing",
            "arbitrate decision",
            "escalate to human expert",
        ],
    ),
    ScenarioCard(
        slug="bk047-kill-pause-or-continue-decision-discipline",
        title="Kill / pause / continue discipline: protect resources from sunk-cost and zombie work",
        source_type="deep_research_synthesis",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["portfolio_management", "sunk_cost", "resource_allocation", "stop_condition", "focus"],
        source_url="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        local_file="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
        situation=(
            "A campaign, project, channel, vendor, tool, or initiative keeps consuming time/money despite weak results."
        ),
        decision_pressure=(
            "Teams defend work because they have already invested effort, fear embarrassment, or hope results will improve."
        ),
        mistake_or_risk=(
            "Do not continue work because of sunk cost or emotional ownership. Scarce attention and cash should move toward strategic value and learning."
        ),
        recommended_next_action=(
            "Run a kill/pause/continue review: original goal, current evidence, remaining upside, cost to continue, opportunity cost, learning gained, strategic fit, and stop/change threshold."
        ),
        evidence_needed=[
            "Original objective and success threshold",
            "Actual performance",
            "Cost spent and cost to continue",
            "Opportunity cost",
            "Remaining uncertainty",
            "Learning captured",
            "Revised recommendation",
        ],
        red_flags=[
            "Team says continue because much has already been spent",
            "No improvement after agreed check date",
            "Metrics are changed to justify continuation",
            "Initiative no longer fits strategy",
            "Owner cannot state what will be learned by continuing",
        ],
        agent_lesson=(
            "A COO protects the business by making stop decisions. Focus is created as much by stopping as by starting."
        ),
        hard_gate_candidate=(
            "Every approved experiment/project should have a stop/change threshold and review date."
        ),
        retrieval_triggers=[
            "should we stop",
            "sunk cost",
            "project not working",
            "pause campaign",
            "kill decision",
        ],
    ),
    ScenarioCard(
        slug="bk048-change-adoption-risk-is-execution-risk",
        title="Change adoption risk: a logically correct decision can still fail in execution",
        source_type="consulting_framework",
        trust_level="high",
        domains=["manager_coo"],
        agent_targets=["manager_coo"],
        tags=["change_management", "adoption", "stakeholders", "execution_risk", "transformation"],
        source_url="https://www.bain.com/insights/pulling-away-managing-sustaining-change/",
        local_file="archives/business-knowledge/manager/decision-making/bain_pulling_away_change_rapid.html",
        situation=(
            "The business approves a change—new process, policy, technology, sales motion, pricing, reporting cadence, or operating model—but adoption is uncertain."
        ),
        decision_pressure=(
            "The plan may be rational on paper, but people most affected by the change can resist, ignore, reinterpret, or overload the implementation."
        ),
        mistake_or_risk=(
            "Do not treat approval as execution. Change fails when ownership, incentives, communication, training, capacity, and feedback loops are missing."
        ),
        recommended_next_action=(
            "Create an adoption plan: affected stakeholders, behavior changes required, resistance reasons, training/support, owner, communication, pilot path, feedback loop, and performance review."
        ),
        evidence_needed=[
            "Stakeholders affected",
            "Behavior/process change required",
            "Expected resistance or overload",
            "Training/support plan",
            "Owner and champions",
            "Pilot or rollout plan",
            "Feedback and review cadence",
        ],
        red_flags=[
            "People doing the work were not consulted",
            "No training/support exists",
            "Incentives conflict with the change",
            "Change adds work without removing work",
            "Decision owner and adoption owner are unclear",
        ],
        agent_lesson=(
            "The Manager should judge execution risk, not just idea quality. A good decision that cannot be adopted is not yet a good business decision."
        ),
        hard_gate_candidate=(
            "For process/technology/policy changes, require adoption-risk plan before rollout."
        ),
        retrieval_triggers=[
            "change management",
            "adoption risk",
            "new process not followed",
            "transformation execution",
            "stakeholder resistance",
        ],
    ),
]

# Round 2 COO deep research: formal decision science, high-reliability
# operations, and primary operator doctrine. These cards teach the Manager how
# to govern decisions and trade-offs; specialist agents still own domain depth.
SCENARIOS.extend(
    [
        ScenarioCard(
            slug="bk049-multi-objective-decision-analysis",
            title="Multi-objective decision analysis: define ends, constraints, measures, alternatives, and trade-offs before choosing",
            source_type="public_decision_science",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["decision_analysis", "objectives", "alternatives", "tradeoffs", "uncertainty"],
            source_url="https://www.nationalacademies.org/read/29265/chapter/8",
            local_file="archives/business-knowledge/manager/decision-making/national_academies_decision_guide_common_steps.html",
            situation=(
                "A consequential decision has several competing goals, interested stakeholders, mandatory constraints, and no obviously dominant option."
            ),
            decision_pressure=(
                "Teams often debate preferred solutions before agreeing on what success means, which constraints are non-negotiable, or which trade-offs the business is willing to make."
            ),
            mistake_or_risk=(
                "Do not compare options against a blurred objective or collapse legal constraints, customer values, costs, and strategic preferences into one unexplained score."
            ),
            recommended_next_action=(
                "Write the decision statement; identify experts, implementers, and affected parties; separate facts from values; define fundamental objectives, mandatory constraints, and measurable criteria; generate multiple and hybrid alternatives; assess consequences and uncertainty; then document the trade-off and decision."
            ),
            evidence_needed=[
                "Decision statement and scope",
                "Fundamental objectives and value priorities",
                "Mandatory constraints versus preferences",
                "At least two feasible alternatives plus do-nothing",
                "Consequences, uncertainty, and trade-offs by objective",
                "Implementation owner and review point",
            ],
            red_flags=[
                "The preferred solution appears inside the problem statement",
                "Only one feasible option is presented",
                "A weighted score hides a mandatory constraint",
                "Affected implementers or customers were excluded",
                "Trade-offs are described as if every objective can be maximized",
            ],
            agent_lesson=(
                "The Manager improves decision quality by structuring the choice before judging it. Clear objectives and alternatives expose disagreements that a persuasive proposal can hide."
            ),
            hard_gate_candidate=(
                "For high-impact multi-objective choices, require an objectives/constraints/alternatives/consequences matrix before approval."
            ),
            retrieval_triggers=[
                "multiple objectives", "compare alternatives", "decision criteria", "trade offs", "complex business decision"
            ],
        ),
        ScenarioCard(
            slug="bk050-outside-view-reference-class-forecast",
            title="Outside-view forecasting: correct project optimism with actual outcomes from comparable work",
            source_type="government_appraisal_guidance",
            trust_level="high",
            domains=["manager_coo", "finance"],
            agent_targets=["manager_coo", "finance_agent"],
            tags=["forecasting", "reference_class", "optimism_bias", "cost", "schedule"],
            source_url="https://www.gov.uk/government/publications/the-green-book-appraisal-and-evaluation-in-central-government/the-green-book-2026",
            local_file="archives/business-knowledge/manager/decision-making/uk_green_book_2026.html",
            situation=(
                "A project, campaign, integration, hiring plan, or launch has an internally produced cost, benefit, adoption, or completion forecast."
            ),
            decision_pressure=(
                "The proposing team knows the plan in detail but is systematically tempted to understate cost and duration while overstating benefits and adoption."
            ),
            mistake_or_risk=(
                "Do not accept an inside-view plan because its task breakdown looks detailed. Detail does not remove optimism bias, strategic understatement, or unknown unknowns."
            ),
            recommended_next_action=(
                "Build a reference class of similar completed work; compare forecast versus actual cost, time, benefit, and adoption; apply the observed error distribution to the new proposal; state whether the new work is genuinely less or more risky than the reference class."
            ),
            evidence_needed=[
                "Comparable completed initiatives",
                "Original forecast and actual outcome for each comparable",
                "Median and downside forecast error",
                "Reasons the current case differs",
                "Adjusted cost, duration, and benefit range",
                "Contingency and trigger for reforecast",
            ],
            red_flags=[
                "The team says this project is unique",
                "Only successful comparables are selected",
                "Forecast errors are never stored",
                "Contingency is used to preserve an unrealistic base estimate",
                "Benefit estimates remain point values while costs receive downside analysis",
            ],
            agent_lesson=(
                "The outside view is an institutional antidote to optimism. The Manager should make forecast accuracy improve from the business's own history."
            ),
            hard_gate_candidate=(
                "Material projects require at least a lightweight reference-class check or an explicit statement that no reliable class exists."
            ),
            retrieval_triggers=[
                "project estimate", "schedule overrun", "optimism bias", "reference class", "forecast accuracy"
            ],
        ),
        ScenarioCard(
            slug="bk051-ranges-sensitivity-and-confidence-not-point-estimates",
            title="Estimate discipline: use ranges, sensitivity, confidence, and independent cross-checks instead of point estimates",
            source_type="government_program_management_guide",
            trust_level="high",
            domains=["manager_coo", "finance"],
            agent_targets=["manager_coo", "finance_agent"],
            tags=["estimation", "sensitivity", "uncertainty", "contingency", "confidence"],
            source_url="https://www.gao.gov/products/gao-20-195g",
            local_file="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
            situation=(
                "Management receives a single revenue, cost, cash, delivery, or capacity number and is asked to approve a plan around it."
            ),
            decision_pressure=(
                "A single number is easy to communicate and budget against, but it conceals uncertainty and makes the business brittle when a key assumption moves."
            ),
            mistake_or_risk=(
                "Do not confuse precision with accuracy. A detailed ₹12.43 lakh forecast may be less decision-useful than a defensible range with known drivers."
            ),
            recommended_next_action=(
                "Require best/base/worst or probability ranges, list ground rules and assumptions, identify the variables that drive the result, vary them in sensitivity analysis, cross-check with an independent method, choose a confidence level, and update with actuals."
            ),
            evidence_needed=[
                "Technical or operational baseline",
                "Ground rules and assumptions",
                "Data source and quality",
                "Estimate range and confidence level",
                "Sensitivity-ranked cost/benefit drivers",
                "Independent cross-check",
                "Actual-versus-estimate update cadence",
            ],
            red_flags=[
                "Only a point estimate is shown",
                "No variable materially changes the answer",
                "The same person built and approved the estimate",
                "Uncertainty is represented by an arbitrary blanket percentage",
                "Actuals do not update the model",
            ],
            agent_lesson=(
                "A COO does not ask whether the number is correct; it asks how wrong it can be, what makes it wrong, and whether the business survives that range."
            ),
            hard_gate_candidate=(
                "Cash-, capacity-, or launch-critical forecasts require ranges, key sensitivities, and an update rule."
            ),
            retrieval_triggers=[
                "point estimate", "forecast range", "sensitivity analysis", "confidence level", "contingency budget"
            ],
        ),
        ScenarioCard(
            slug="bk052-robust-decisions-under-deep-uncertainty",
            title="Robust decisions: choose plans that survive many plausible futures when prediction is unreliable",
            source_type="public_policy_research",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["robust_decision_making", "deep_uncertainty", "scenario_stress_test", "resilience", "strategy"],
            source_url="https://www.rand.org/pubs/research_briefs/RB9701.html",
            local_file="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
            situation=(
                "The decision depends on uncertain customer demand, regulation, technology, competitor action, platform access, funding, or supply conditions that cannot be forecast reliably."
            ),
            decision_pressure=(
                "Teams still want one base-case forecast and may optimize the entire plan for it, creating excellent performance in one imagined future and failure in many others."
            ),
            mistake_or_risk=(
                "Do not force a false probability distribution onto deep uncertainty or select the option with the highest modeled average while ignoring unacceptable failure states."
            ),
            recommended_next_action=(
                "Define unacceptable outcomes, generate a range of plausible futures, stress-test each option across them, identify the conditions under which each fails, and prefer robust/no-regret, reversible, diversified, or adaptive choices."
            ),
            evidence_needed=[
                "Critical uncertainties outside management control",
                "Plausible future states",
                "Minimum acceptable outcomes and failure thresholds",
                "Performance of each option across futures",
                "Vulnerability conditions",
                "Adaptation or exit path",
            ],
            red_flags=[
                "One most-likely scenario drives the full commitment",
                "Option performs extremely well only under optimistic assumptions",
                "Failure conditions are not stated",
                "No adaptation path exists",
                "Probabilities imply confidence the team does not possess",
            ],
            agent_lesson=(
                "When prediction is weak, the Manager should optimize for survivability and adaptability, not spreadsheet perfection in one future."
            ),
            hard_gate_candidate=(
                "Strategy exposed to multiple uncontrollable uncertainties requires scenario stress testing and explicit failure thresholds."
            ),
            retrieval_triggers=[
                "deep uncertainty", "scenario stress test", "robust strategy", "cannot forecast", "resilient decision"
            ],
        ),
        ScenarioCard(
            slug="bk053-foresight-signposts-and-adaptive-actions",
            title="Strategic foresight: turn plausible futures into monitored signposts and preplanned actions",
            source_type="oecd_research",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["strategic_foresight", "signposts", "adaptive_strategy", "weak_signals", "scenario_planning"],
            source_url="https://www.oecd.org/content/dam/oecd/en/publications/reports/2023/09/supporting-decision-making-with-strategic-foresight_19fb1036/1d78c791-en.pdf",
            local_file="archives/business-knowledge/manager/decision-making/oecd_supporting_decisions_strategic_foresight.pdf",
            situation=(
                "A strategic plan may need to change if a market, platform, policy, supplier, financing, or customer-behavior condition evolves."
            ),
            decision_pressure=(
                "Scenario workshops can generate imaginative narratives yet have no effect on budgets, monitoring, or decisions."
            ),
            mistake_or_risk=(
                "Do not treat foresight as prediction or a presentation exercise. A scenario without a signpost, owner, and response is not an operating tool."
            ),
            recommended_next_action=(
                "Identify driving uncertainties; build a small set of distinct plausible futures; challenge current assumptions; find no-regret moves; define observable signposts for each material shift; assign signal owners and pre-agree the decision that each threshold triggers."
            ),
            evidence_needed=[
                "Driving uncertainties",
                "Distinct plausible scenarios",
                "Assumptions challenged",
                "No-regret and option-preserving moves",
                "Observable signposts and data sources",
                "Signal owner, review frequency, and triggered action",
            ],
            red_flags=[
                "Scenarios differ only by optimistic/base/pessimistic growth",
                "No external signal changes the plan",
                "Signposts are vague or lagging",
                "No one owns monitoring",
                "The business waits for certainty before adapting",
            ],
            agent_lesson=(
                "Foresight creates advantage when the business recognizes change early and has already decided what to do about it."
            ),
            hard_gate_candidate=(
                "Long-horizon strategy must name its critical assumptions, signposts, owners, and adaptation actions."
            ),
            retrieval_triggers=[
                "strategic foresight", "early warning sign", "scenario planning", "market signpost", "adaptive strategy"
            ],
        ),
        ScenarioCard(
            slug="bk054-high-velocity-reversible-decision-loop",
            title="High-velocity decisions: move fast only when reversibility, instrumentation, and correction are real",
            source_type="primary_operator_letter",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["reversible_decisions", "decision_speed", "rollback", "experimentation", "type_two"],
            source_url="https://www.aboutamazon.com/news/company-news/2016-letter-to-shareholders",
            local_file="archives/business-knowledge/manager/decision-making/amazon_2016_high_velocity_decisions.html",
            situation=(
                "The team needs to act before it has complete information and argues that the choice is reversible."
            ),
            decision_pressure=(
                "Waiting can be expensive, but labeling a decision reversible can hide data migration, customer trust, contract, compliance, or dependency costs that make rollback fictional."
            ),
            mistake_or_risk=(
                "Do not equate fast approval with high-velocity management. Speed is justified only when the business can observe failure early and restore an acceptable state cheaply."
            ),
            recommended_next_action=(
                "Verify reversibility; cap exposure; instrument leading signals; name the rollback owner and exact recovery action; decide with sufficient rather than perfect information; review quickly and correct without defending the original choice."
            ),
            evidence_needed=[
                "What is reversible and what is not",
                "Maximum loss/exposure during the test",
                "Leading success and harm signals",
                "Rollback steps, owner, and time",
                "Decision review threshold",
                "Residual customer/compliance/data impact after rollback",
            ],
            red_flags=[
                "Rollback has never been tested",
                "Customer data or legal obligations cannot be undone",
                "The experiment has no exposure cap",
                "Failure becomes visible only after material damage",
                "The decision owner cannot stop the rollout",
            ],
            agent_lesson=(
                "Fast decisions are a system: bounded exposure, observability, authority, correction, and learning. Without those, speed merely transfers risk to the business."
            ),
            hard_gate_candidate=(
                "Any fast-track decision must document reversibility, exposure cap, monitoring, rollback, and review threshold."
            ),
            retrieval_triggers=[
                "two way door", "move fast", "70 percent information", "rollback plan", "reversible experiment"
            ],
        ),
        ScenarioCard(
            slug="bk055-disagree-commit-versus-true-misalignment",
            title="Disagree and commit: close ordinary debate quickly while escalating true misalignment and protected risks",
            source_type="primary_operator_letter",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["disagree_and_commit", "alignment", "escalation", "decision_rights", "dissent"],
            source_url="https://www.aboutamazon.com/news/company-news/2016-letter-to-shareholders",
            local_file="archives/business-knowledge/manager/decision-making/amazon_2016_high_velocity_decisions.html",
            situation=(
                "Capable specialists disagree after evidence and arguments have been heard, and continued consensus-building is delaying execution."
            ),
            decision_pressure=(
                "The Manager must close debate without turning commitment into forced silence or allowing a real objective/authority conflict to remain hidden."
            ),
            mistake_or_risk=(
                "Do not use disagree-and-commit to suppress compliance, fraud, safety, solvency, privacy, or customer-harm concerns, or when parties disagree about the actual objective."
            ),
            recommended_next_action=(
                "Restate the decision, objective, constraints, decider, and dissent; distinguish uncertainty-based disagreement from true objective/authority misalignment; escalate the latter early; otherwise decide, record the dissent and trigger, and require sincere execution with a review point."
            ),
            evidence_needed=[
                "Decision and accountable decider",
                "Dissenting view and evidence",
                "Shared objective and constraints",
                "Protected-risk or authority conflict check",
                "Review trigger that could reopen the decision",
                "Execution owner commitment",
            ],
            red_flags=[
                "The dissenter alleges illegality, deception, unsafe conduct, or insolvency",
                "Teams optimize different objectives",
                "The decider lacks authority",
                "Dissent is punished or omitted from the record",
                "Commitment is requested before evidence is heard",
            ],
            agent_lesson=(
                "A COO needs both closure and dissent. Ordinary uncertainty should not create endless meetings; protected risks and true misalignment must not be buried for speed."
            ),
            hard_gate_candidate=(
                "Disagree-and-commit is blocked when protected risk, unclear authority, or objective misalignment remains unresolved."
            ),
            retrieval_triggers=[
                "disagree and commit", "team cannot agree", "true misalignment", "close debate", "dissent escalation"
            ],
        ),
        ScenarioCard(
            slug="bk056-dynamic-resource-allocation-portfolio",
            title="Dynamic resource allocation: move cash, talent, and attention toward evidence and strategy—not history",
            source_type="empirical_strategy_research",
            trust_level="high",
            domains=["manager_coo", "finance"],
            agent_targets=["manager_coo", "finance_agent"],
            tags=["resource_allocation", "capital_allocation", "portfolio", "opportunity_cost", "strategy_execution"],
            source_url="https://www.mckinsey.com/capabilities/strategy-and-corporate-finance/our-insights/how-to-put-your-money-where-your-strategy-is",
            local_file="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
            situation=(
                "Annual budgets, headcount, founder attention, and tooling spend are largely inherited from last year although opportunity and performance have changed."
            ),
            decision_pressure=(
                "Existing owners defend resources, while new opportunities are easy to seed but politically difficult to fund by pruning mature or underperforming work."
            ),
            mistake_or_risk=(
                "Do not call a plan strategic if resource allocation remains last-year-plus. Strategy becomes real only when scarce money, talent, and attention move. Also do not churn long-horizon investments merely to appear dynamic."
            ),
            recommended_next_action=(
                "Map resources below broad departments; classify initiatives as seed, nurture, maintain, prune, harvest, or stop; compare marginal future value, strategic fit, risk, learning, switching cost, and time horizon; include management attention and top talent; maintain a small reallocation reserve; run a quarterly review rather than budgeting annually only."
            ),
            evidence_needed=[
                "Current cash, talent, time, and leadership-attention map",
                "Future marginal value rather than historical spend",
                "Strategic fit and capability dependencies",
                "Performance and learning evidence",
                "Switching and shutdown cost",
                "Resources released and destination",
            ],
            red_flags=[
                "Every team receives roughly last year's allocation",
                "New work is funded without stopping anything",
                "Sunk cost is treated as future value",
                "Senior attention is omitted from capacity planning",
                "A profitable activity is protected despite structural decline or weak fit",
                "Short-term volatility repeatedly reverses a coherent long-horizon thesis",
            ],
            agent_lesson=(
                "The Manager is the allocator of the whole operating portfolio. Protecting legacy budgets is often a larger strategic risk than making a small new bet."
            ),
            hard_gate_candidate=(
                "Quarterly portfolio review must include explicit seed/nurture/prune/harvest decisions and opportunity-cost transfers."
            ),
            retrieval_triggers=[
                "resource allocation", "budget inertia", "move headcount", "capital allocation", "portfolio review"
            ],
        ),
        ScenarioCard(
            slug="bk057-independent-red-team-assumption-challenge",
            title="Independent challenge: red-team the framing, evidence, and lowest-confidence assumptions before commitment",
            source_type="military_decision_doctrine",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["red_team", "assumptions", "critical_thinking", "independent_review", "premortem"],
            source_url="https://rdl.train.army.mil/catalog-ws/view/arimcpii/downloads/Red_Team_Handbook_v9.pdf",
            local_file="archives/business-knowledge/manager/decision-making/COO_DECISION_RESEARCH_INDEX.md",
            situation=(
                "A high-stakes proposal is internally coherent and strongly sponsored, but its assumptions, evidence quality, and alternative explanations have not been independently tested."
            ),
            decision_pressure=(
                "The proposal owner controls information and social dynamics discourage junior or cross-functional dissent."
            ),
            mistake_or_risk=(
                "Do not ask the proposal owner to perform a cosmetic self-critique. Independence and permission to challenge the sponsor are the value of the exercise."
            ),
            recommended_next_action=(
                "Give a non-owning reviewer a written challenge charter: test whether the right issue is defined, separate claims/reasons/conclusions, assess evidence reliability, expose value conflicts and fallacies, anonymously rate assumption confidence, and attack the weakest load-bearing assumptions."
            ),
            evidence_needed=[
                "Claim/reason/conclusion map",
                "Explicit load-bearing assumptions",
                "Anonymous confidence rating",
                "Contrary evidence and alternative explanations",
                "Failure paths and adversarial incentives",
                "Proposal owner's response and plan change",
            ],
            red_flags=[
                "Reviewer reports to the proposal owner",
                "Challenge occurs after irreversible commitment",
                "Only wording changes result",
                "Low-confidence assumptions have high consequence",
                "Sponsor treats challenge as disloyalty",
            ],
            agent_lesson=(
                "Independent challenge is not pessimism; it is a control against sponsor confidence, group hierarchy, and internally consistent fiction."
            ),
            hard_gate_candidate=(
                "High-stakes irreversible decisions require a named independent challenger and disposition of load-bearing assumptions."
            ),
            retrieval_triggers=[
                "red team", "challenge assumptions", "independent review", "premortem", "proposal too confident"
            ],
        ),
        ScenarioCard(
            slug="bk058-production-pressure-weak-signal-drift",
            title="Drift toward failure: treat recurring anomalies and silenced dissent as growing risk, not proof of safety",
            source_type="independent_accident_investigation",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["weak_signals", "schedule_pressure", "normalization", "near_miss", "independent_assurance"],
            source_url="https://www.nasa.gov/history/columbia-accident-investigation-board-synopsis/",
            local_file="archives/business-knowledge/manager/decision-making/nasa_columbia_organizational_culture.html",
            situation=(
                "An anomaly, exception, manual workaround, control override, complaint, or near miss has occurred repeatedly without an obvious disaster."
            ),
            decision_pressure=(
                "Schedule, revenue, utilization, or efficiency pressure encourages teams to reinterpret the signal as normal and avoid slowing production."
            ),
            mistake_or_risk=(
                "Do not use past survival as evidence that a recurring anomaly is safe. Repetition may indicate eroding defenses, hidden coupling, and normalization of deviance."
            ),
            recommended_next_action=(
                "Maintain an anomaly/exception ledger; trend recurrence and severity; preserve dissent in the decision record; require independent risk review when production pressure conflicts with control evidence; stop or reduce exposure when uncertainty and consequence are both material."
            ),
            evidence_needed=[
                "History of anomalies, exceptions, and near misses",
                "Production/schedule incentives affecting judgment",
                "Dissenting technical or frontline views",
                "Defense/control degradation",
                "Independent assessment",
                "Exposure-reduction or stop condition",
            ],
            red_flags=[
                "It happened before and nothing bad happened",
                "Warnings are relabeled as maintenance or turnaround issues",
                "Management filters uncomfortable frontline evidence",
                "Safety/compliance assurance reports through delivery ownership only",
                "Deadlines justify waivers without cumulative-risk review",
            ],
            agent_lesson=(
                "A seasoned COO listens hardest when operating pressure is highest. Near misses and recurring exceptions are advance information bought cheaply."
            ),
            hard_gate_candidate=(
                "Recurring high-consequence anomalies require independent review and explicit risk acceptance before continued operation."
            ),
            retrieval_triggers=[
                "near miss", "recurring anomaly", "schedule pressure", "we got away with it", "normalization of deviance"
            ],
        ),
        ScenarioCard(
            slug="bk059-crisis-incident-command-operating-periods",
            title="Crisis command: establish one incident owner, objectives, roles, factual cadence, and short operating periods",
            source_type="emergency_management_doctrine",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["crisis_management", "incident_command", "containment", "operational_period", "span_of_control"],
            source_url="https://emilms.fema.gov/_is0200c/groups/288.html",
            local_file="archives/business-knowledge/manager/decision-making/fema_ics_command_summary.html",
            situation=(
                "The business faces a live outage, fraud event, regulatory issue, cash emergency, vendor failure, reputational incident, or customer-harm event."
            ),
            decision_pressure=(
                "Many people act at once, terminology differs, facts and rumors mix, executives bypass owners, and root-cause debate competes with urgent containment."
            ),
            mistake_or_risk=(
                "Do not manage a crisis through an unstructured group chat or multiple competing commanders. Freelancing and unclear authority compound the incident."
            ),
            recommended_next_action=(
                "Name one incident commander; define immediate measurable objectives; separate operations, planning/intelligence, communications, logistics, and finance/admin roles; maintain a timestamped fact/decision log; set communication and operational-period cadence; contain first, then eradicate/recover and review."
            ),
            evidence_needed=[
                "Incident definition and current impact",
                "Named commander and delegated authority",
                "Objectives for the current operational period",
                "Role and task assignments",
                "Verified facts, unknowns, decisions, and timestamps",
                "Next update and handover time",
            ],
            red_flags=[
                "More than one person issues conflicting direction",
                "No source of truth exists",
                "Executives assign tasks around the incident commander",
                "Root-cause speculation delays containment",
                "One manager has an unmanageable number of direct responders",
            ],
            agent_lesson=(
                "Crisis leadership is temporary operating-system design. Clarity of command, objectives, information, and cadence is more valuable than executive activity."
            ),
            hard_gate_candidate=(
                "Severity-threshold incidents automatically activate an incident commander, log, role map, and update cadence."
            ),
            retrieval_triggers=[
                "business crisis", "incident commander", "live outage", "containment", "war room"
            ],
        ),
        ScenarioCard(
            slug="bk060-risk-triggers-and-precommitted-contingencies",
            title="Risk triggers: precommit mitigation and contingency actions before the threshold is crossed",
            source_type="nasa_risk_management",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["risk_triggers", "contingency", "mitigation", "threshold", "early_warning"],
            source_url="https://www.nasa.gov/reference/6-4-technical-risk-management/",
            local_file="archives/business-knowledge/manager/decision-making/nasa_risk_informed_decision_handbook.pdf",
            situation=(
                "A known risk is being monitored, but the team has not agreed when to act, who acts, or whether the response is mitigation before occurrence or contingency after occurrence."
            ),
            decision_pressure=(
                "Owners defer the uncomfortable commitment and expect to decide once evidence is clearer—when time, options, and attention may be worse."
            ),
            mistake_or_risk=(
                "Do not create a risk register that only scores risks. A monitored risk without triggers, funded actions, and authority is passive documentation."
            ),
            recommended_next_action=(
                "For material risks, define the causal scenario, likelihood/impact/timeframe, leading indicator, mitigation trigger, contingency trigger, named owner, funded action, communication path, residual-risk acceptance, and closure evidence."
            ),
            evidence_needed=[
                "Risk scenario and affected objective",
                "Leading indicator and data source",
                "Mitigation and contingency thresholds",
                "Action, owner, authority, and resources",
                "Residual risk after action",
                "Review frequency and closure evidence",
            ],
            red_flags=[
                "Risk is high but has no funded action",
                "Trigger is subjective",
                "Owner lacks authority to act",
                "Contingency depends on unavailable capacity or supplier",
                "The business will decide when it happens",
            ],
            agent_lesson=(
                "Precommitted triggers convert risk awareness into timely action and prevent delay, denial, and politics under pressure."
            ),
            hard_gate_candidate=(
                "High or time-critical risks require objective triggers and pre-authorized actions, not score-only register entries."
            ),
            retrieval_triggers=[
                "risk trigger", "contingency plan", "when should we act", "risk register", "mitigation threshold"
            ],
        ),
        ScenarioCard(
            slug="bk061-lessons-must-change-the-operating-system",
            title="Institutional learning: a lesson is complete only when it changes future behavior or controls",
            source_type="high_reliability_learning_system",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["lessons_learned", "knowledge_management", "sop", "continuous_improvement", "control_update"],
            source_url="https://www.nasa.gov/learning-resources/for-professionals/appel-lessons-learned/",
            local_file="archives/business-knowledge/manager/decision-making/nasa_lessons_learned.html",
            situation=(
                "A project, incident, negotiation, campaign, launch, or decision review produces a useful lesson."
            ),
            decision_pressure=(
                "Teams write a retrospective and move on; the same failure later recurs because the lesson was stored but not infused into work."
            ),
            mistake_or_risk=(
                "Do not count a document or meeting as learning. Knowledge that does not change a decision rule, artifact, capability, or control remains optional memory."
            ),
            recommended_next_action=(
                "Run the collect-record-disseminate-apply lifecycle: validate the lesson and scope; record event, mechanism, conditions, and recommendation; route it to future users; update the checklist, SOP, training, alert, template, authority rule, or automated gate; verify adoption."
            ),
            evidence_needed=[
                "Observed event and causal mechanism",
                "Conditions under which the lesson applies",
                "Validated recommendation",
                "Future users and retrieval triggers",
                "Specific artifact/control changed",
                "Adoption owner and verification date",
            ],
            red_flags=[
                "Retrospective has no assigned changes",
                "Lesson is too generic to retrieve",
                "No one validates applicability",
                "Same class of incident recurs",
                "The changed process is not measured",
            ],
            agent_lesson=(
                "The Manager owns learning conversion: experience becomes moat only when it is retrievable and embedded in how the next decision is made."
            ),
            hard_gate_candidate=(
                "Material incidents and failed bets cannot close until each accepted lesson has an applied change or explicit no-change rationale."
            ),
            retrieval_triggers=[
                "lessons learned", "retrospective action", "same mistake again", "update SOP", "institutional memory"
            ],
        ),
        ScenarioCard(
            slug="bk062-batna-reservation-and-walkaway",
            title="Negotiation governance: quantify BATNA, reservation point, dependencies, and walk-away before concessions",
            source_type="negotiation_research",
            trust_level="high",
            domains=["manager_coo", "sales", "finance"],
            agent_targets=["manager_coo", "sales_agent", "finance_agent"],
            tags=["negotiation", "batna", "walkaway", "deal_economics", "dependency"],
            source_url="https://www.pon.harvard.edu/daily/batna/translate-your-batna-to-the-current-deal/",
            local_file="archives/business-knowledge/manager/decision-making/harvard_pon_batna.html",
            situation=(
                "The business is negotiating a customer, vendor, partner, landlord, lender, hire, platform, or acquisition agreement."
            ),
            decision_pressure=(
                "The visible deal becomes the entire frame; sunk effort, urgency, relationship pressure, or headline value causes concessions below the value of available alternatives."
            ),
            mistake_or_risk=(
                "Do not accept a deal because it is better than the counterparty's opening offer. It must beat the business's credible alternative after switching, delay, implementation, risk, and dependency costs."
            ),
            recommended_next_action=(
                "Ask the relevant specialist to develop and improve BATNA; calculate reservation point and total economics; map implementation burden, concentration, information, compliance, and exit risk; pre-authorize concession limits; walk away if the final deal is worse than the adjusted alternative."
            ),
            evidence_needed=[
                "Credible BATNA and steps to execute it",
                "Reservation point",
                "Total value and cost over the relationship",
                "Switching, delay, implementation, and exit costs",
                "Dependency/concentration and compliance risks",
                "Concession authority and walk-away conditions",
            ],
            red_flags=[
                "No credible alternative has been developed",
                "Deadline is controlled only by the counterparty",
                "Headline price excludes implementation or exit cost",
                "Deal creates single-party dependency",
                "Negotiator can concede without a limit",
            ],
            agent_lesson=(
                "The Manager governs whether a deal benefits the business; Sales or Finance owns negotiation depth. BATNA prevents activity, ego, and urgency from becoming value destruction."
            ),
            hard_gate_candidate=(
                "Material agreements require BATNA, reservation point, total-economics, dependency, and walk-away review before signature."
            ),
            retrieval_triggers=[
                "BATNA", "walk away from deal", "vendor negotiation", "customer contract", "reservation point"
            ],
        ),
        ScenarioCard(
            slug="bk063-incentives-risk-and-metric-gaming",
            title="Incentive governance: test how targets can be gamed and which risks they externalize",
            source_type="oecd_governance_research",
            trust_level="high",
            domains=["manager_coo", "finance", "compliance"],
            agent_targets=["manager_coo", "finance_agent", "compliance_agent"],
            tags=["incentives", "metric_gaming", "risk_governance", "controls", "compensation"],
            source_url="https://www.oecd.org/en/publications/risk-management-and-corporate-governance_9789264208636-en.html",
            local_file="archives/business-knowledge/manager/decision-making/oecd_risk_management_corporate_governance.pdf",
            situation=(
                "The business introduces a sales target, response SLA, collection target, cost reduction, growth bonus, utilization goal, or agent KPI."
            ),
            decision_pressure=(
                "Targets focus attention and improve performance, but people and agents optimize what is measured—even by transferring risk or cost elsewhere."
            ),
            mistake_or_risk=(
                "Do not approve a target without asking how a rational actor could hit it while harming margin, quality, compliance, cash, trust, or future performance."
            ),
            recommended_next_action=(
                "Define the intended business outcome; enumerate gaming paths and externalized risks; pair the target with counter-metrics and non-negotiable boundaries; separate performance ownership from independent assurance; monitor behavior distribution and exceptions, not only the headline KPI."
            ),
            evidence_needed=[
                "Intended outcome and causal link from metric",
                "Gaming and shortcut scenarios",
                "Risks transferred to other functions or future periods",
                "Counter-metrics and hard boundaries",
                "Independent assurance owner",
                "Behavior and exception monitoring",
            ],
            red_flags=[
                "Volume target has no quality or margin floor",
                "Cost target shifts work or risk off-book",
                "Collectors or sellers can misrepresent to earn incentive",
                "Control owner is rewarded by the activity being controlled",
                "Metric improves while customer complaints or exceptions rise",
            ],
            agent_lesson=(
                "Incentives are operational code for humans and agents. The COO must model their adversarial behavior before deployment."
            ),
            hard_gate_candidate=(
                "New high-powered targets require gaming analysis, counter-metrics, boundaries, and independent assurance."
            ),
            retrieval_triggers=[
                "sales incentive", "KPI gaming", "bonus target", "Goodhart", "risk taking incentive"
            ],
        ),
        ScenarioCard(
            slug="bk064-stop-the-line-quality-at-source",
            title="Quality at source: expose abnormalities immediately and stop harmful propagation",
            source_type="primary_operating_system_history",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["jidoka", "andon", "quality_at_source", "stop_the_line", "frontline_authority"],
            source_url="https://www.toyota-global.com/company/history_of_toyota/75years/text/entering_the_automotive_business/chapter1/section4/item4.html",
            local_file="archives/business-knowledge/manager/decision-making/toyota_tps_jidoka.html",
            situation=(
                "A defect, suspicious transaction, wrong message, compliance exception, bad data, or process abnormality appears early but can propagate through downstream work."
            ),
            decision_pressure=(
                "Stopping work reduces short-term throughput, so teams route the issue downstream for inspection, cleanup, reconciliation, or customer support."
            ),
            mistake_or_risk=(
                "Do not optimize throughput by allowing known defects to continue. Downstream detection multiplies rework, customer harm, evidence loss, and root-cause distance."
            ),
            recommended_next_action=(
                "Detect and visibly signal abnormality at the point of work; give the frontline actor authority to pause propagation within defined severity rules; contain affected work; identify and remove the cause; verify the countermeasure; resume and monitor."
            ),
            evidence_needed=[
                "Abnormal condition and detection point",
                "Potential propagation and consequence",
                "Pause authority and severity threshold",
                "Containment scope",
                "Root cause and countermeasure",
                "Verification before restart",
            ],
            red_flags=[
                "Operators fear punishment for stopping",
                "Known defects are queued for downstream cleanup",
                "Throughput target outweighs quality boundary",
                "Restart occurs before cause verification",
                "Alerts exist but no one can halt propagation",
            ],
            agent_lesson=(
                "The Manager should design systems where bad work becomes visible and stoppable early. Quality is produced, not inspected in later."
            ),
            hard_gate_candidate=(
                "Material customer, money, data, or compliance abnormalities must fail closed or pause propagation until verified."
            ),
            retrieval_triggers=[
                "stop the line", "jidoka", "known defect", "quality at source", "frontline pause"
            ],
        ),
        ScenarioCard(
            slug="bk065-owner-economics-capital-stewardship",
            title="Owner economics: judge retained cash and management attention by long-term business value, not accounting optics",
            source_type="primary_owner_manual",
            trust_level="high",
            domains=["manager_coo", "finance"],
            agent_targets=["manager_coo", "finance_agent"],
            tags=["owner_economics", "capital_stewardship", "intrinsic_value", "retained_earnings", "long_term"],
            source_url="https://www.berkshirehathaway.com/owners.html",
            local_file="archives/business-knowledge/manager/decision-making/berkshire_owners_manual.html",
            situation=(
                "Management can retain cash, reinvest in the business, acquire capability, reduce debt, build resilience, or return/distribute capital, and near-term accounting metrics favor one path."
            ),
            decision_pressure=(
                "Reported profit, growth, utilization, or budget absorption can reward deployment even when the incremental return and strategic value are weak."
            ),
            mistake_or_risk=(
                "Do not treat retained cash as free management capital or judge decisions only by reported earnings. Capital and executive attention belong to owners and must earn an adequate risk-adjusted long-term return."
            ),
            recommended_next_action=(
                "Compare uses of capital on incremental after-tax cash economics, durability, risk, capability fit, opportunity cost, and reversibility; include the value of liquidity and management attention; communicate assumptions and mistakes candidly."
            ),
            evidence_needed=[
                "Available uses of capital",
                "Incremental cash return and timing",
                "Durability and downside risk",
                "Strategic/capability fit",
                "Liquidity and resilience requirement",
                "Management-attention cost",
                "Post-decision actual return",
            ],
            red_flags=[
                "Spend is justified because budget exists",
                "Growth is measured without incremental cash economics",
                "Acquisition/project absorbs disproportionate management attention",
                "Liquidity value is ignored",
                "Management hides allocation errors behind aggregate results",
            ],
            agent_lesson=(
                "A COO is a steward of cash and attention. Every retained rupee and leadership hour competes with another use and should be evaluated from the owner's long-term perspective."
            ),
            hard_gate_candidate=(
                "Material capital deployment requires alternatives, incremental cash economics, opportunity cost, resilience impact, and review of actual return."
            ),
            retrieval_triggers=[
                "capital stewardship", "retain cash", "owner earnings", "capital allocation", "long term business value"
            ],
        ),
        ScenarioCard(
            slug="bk066-resist-process-and-metric-proxies",
            title="Resist proxies: process compliance and dashboard movement are evidence—not the customer or business outcome",
            source_type="primary_operator_letter",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["proxies", "customer_outcomes", "process", "metrics", "management_system"],
            source_url="https://www.aboutamazon.com/news/company-news/2016-letter-to-shareholders",
            local_file="archives/business-knowledge/manager/decision-making/amazon_2016_high_velocity_decisions.html",
            situation=(
                "A team explains a weak business or customer result by showing that the process was followed or its internal KPI improved."
            ),
            decision_pressure=(
                "Processes and metrics are scalable and auditable, so the organization gradually optimizes them while losing contact with the outcome they were meant to serve."
            ),
            mistake_or_risk=(
                "Do not accept process completion, activity volume, model score, survey average, or SLA compliance as proof that the customer or business objective was achieved."
            ),
            recommended_next_action=(
                "Trace each process and KPI to the outcome it proxies; inspect real customer cases and exceptions; compare leading proxy movement with lagging outcome and harm measures; revise or remove proxies whose causal relationship has weakened."
            ),
            evidence_needed=[
                "Fundamental customer/business outcome",
                "Causal reason the proxy should predict it",
                "Real case-level evidence",
                "Lagging outcome and harm metrics",
                "Exceptions hidden by averages",
                "Proxy review or retirement rule",
            ],
            red_flags=[
                "We followed the process",
                "Activity rises while conversion, retention, margin, or trust falls",
                "Average hides severe customer cases",
                "No one can explain why the KPI predicts value",
                "Teams optimize eligibility rather than outcome",
            ],
            agent_lesson=(
                "The Manager owns the link between operating mechanisms and real business value. When the proxy becomes the goal, the operating system needs correction."
            ),
            hard_gate_candidate=(
                "Executive KPI reviews must include the underlying outcome, harm measure, and exception cases—not proxy movement alone."
            ),
            retrieval_triggers=[
                "process followed but failed", "vanity metric", "proxy metric", "customer outcome", "dashboard looks good"
            ],
        ),
        ScenarioCard(
            slug="bk067-decision-quality-versus-outcome-quality",
            title="Decision audit: distinguish process quality from luck before rewarding, blaming, or changing policy",
            source_type="decision_science_synthesis",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["decision_quality", "outcome_bias", "learning", "accountability", "uncertainty"],
            source_url="https://www.nationalacademies.org/read/29265/chapter/8",
            local_file="archives/business-knowledge/manager/decision-making/national_academies_decision_guide_common_steps.html",
            situation=(
                "A decision produced an unusually good or bad result, and the business must decide whether to reward, blame, repeat, stop, or change its policy."
            ),
            decision_pressure=(
                "Outcome bias makes a lucky gamble look wise and a sound uncertain decision look incompetent, corrupting incentives and future learning."
            ),
            mistake_or_risk=(
                "Do not infer decision quality from one outcome. Judge whether the process used information, values, alternatives, uncertainty, authority, and controls appropriately at the time."
            ),
            recommended_next_action=(
                "Reconstruct the pre-decision record without hindsight; score framing, evidence, alternatives, forecast calibration, risk treatment, authority, and execution; then separately analyze how uncertainty resolved and what new evidence should update the policy."
            ),
            evidence_needed=[
                "Information available at decision time",
                "Forecast and confidence stated before outcome",
                "Alternatives and risk controls considered",
                "Decision rights and rationale",
                "Actual outcome and variance mechanism",
                "Repeatable process lesson versus luck",
            ],
            red_flags=[
                "Success is used to excuse a policy violation",
                "Failure is blamed despite a calibrated risk",
                "Pre-decision forecast was not recorded",
                "Team rewrites its original confidence after the result",
                "One anecdote overturns a sound base rate",
            ],
            agent_lesson=(
                "Accountability should reward sound process and honest calibration, while outcomes update evidence. This produces better long-run judgment than hero/blame cycles."
            ),
            hard_gate_candidate=(
                "Material decision reviews must score decision process and outcome variance separately."
            ),
            retrieval_triggers=[
                "bad outcome good decision", "outcome bias", "decision audit", "lucky success", "accountability under uncertainty"
            ],
        ),
    ]
)

# Round 3: consulting-firm research and field experiments. Consulting findings
# are treated as contextual research, not universal causal laws. Cards retain
# the decision context, local evidence required, and limits on transfer.
SCENARIOS.extend(
    [
        ScenarioCard(
            slug="bk068-independent-decision-process-and-implementation-owner",
            title="Material decisions: separate challenge from sponsorship and involve the implementation owner",
            source_type="consulting_global_survey",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["decision_process", "independent_challenge", "implementation", "decision_rights"],
            source_url="https://www.mckinsey.com/capabilities/strategy-and-corporate-finance/our-insights/how-companies-make-good-decisions-mckinsey-global-survey-results",
            local_file="archives/business-knowledge/research/CONSULTING_ACADEMIC_RESEARCH_INDEX.md",
            situation=(
                "A material capital, hiring, product, market, or operating decision is sponsored by a senior person and will be executed by another team."
            ),
            decision_pressure=(
                "Hierarchy and sponsor conviction can turn review into approval theatre, while implementers discover feasibility problems only after commitment."
            ),
            mistake_or_risk=(
                "Do not let the same person originate, frame, approve, and judge a material proposal without independent challenge. Do not exclude the person accountable for implementation."
            ),
            recommended_next_action=(
                "Name proposer, independent challenger, Decider, and implementation owner; publish criteria before debate; include people for relevant experience rather than rank; record dissent, dependencies, and the execution commitment."
            ),
            evidence_needed=[
                "Decision statement and value at stake",
                "Proposer, challenger, Decider, and implementation owner",
                "Transparent criteria and alternatives",
                "Relevant operating expertise and dissent",
                "Dependencies, capacity, and implementation commitment",
                "Decision record and review date",
            ],
            red_flags=[
                "Sponsor is also the only approver",
                "Implementation owner first sees the decision after approval",
                "Participants are selected by hierarchy or loyalty",
                "Criteria change to favour the preferred option",
                "No one owns realization after the meeting",
            ],
            agent_lesson=(
                "A COO designs a decision process that can challenge power and still convert the choice into accountable execution."
            ),
            hard_gate_candidate=(
                "Material decisions require a named independent challenger and implementation owner before approval."
            ),
            retrieval_triggers=[
                "who should approve", "decision governance", "independent challenge", "implementation owner", "executive sponsor"
            ],
        ),
        ScenarioCard(
            slug="bk069-resource-allocation-inertia-review",
            title="Strategy-to-resources review: move cash, talent, and attention when evidence changes",
            source_type="consulting_longitudinal_analysis",
            trust_level="high",
            domains=["manager_coo", "finance"],
            agent_targets=["manager_coo", "finance_agent"],
            tags=["resource_allocation", "portfolio", "strategy", "capital", "talent"],
            source_url="https://www.mckinsey.com/capabilities/strategy-and-corporate-finance/our-insights/how-to-put-your-money-where-your-strategy-is",
            local_file="archives/business-knowledge/research/CONSULTING_ACADEMIC_RESEARCH_INDEX.md",
            situation=(
                "The business has changed its priorities, but budgets, senior attention, headcount, and operating spend still resemble last year."
            ),
            decision_pressure=(
                "Historical ownership and politics make small across-the-board adjustments easier than taking resources from a familiar activity and funding a stronger opportunity."
            ),
            mistake_or_risk=(
                "Do not call a plan strategic when resources remain fixed by history. Also do not churn investments merely to appear dynamic; reallocation must serve a coherent thesis and time horizon."
            ),
            recommended_next_action=(
                "Run a quarterly portfolio review across cash, talent, operating spend, capacity, and leadership attention; rank initiatives by strategic fit, evidence, return range, risk, and learning; explicitly increase, maintain, stage, pause, or stop each allocation."
            ),
            evidence_needed=[
                "Current strategic priorities",
                "Resource allocation by initiative over time",
                "Expected and realized value ranges",
                "Capacity and dependency constraints",
                "Evidence gained since the last review",
                "Increase, hold, stage, pause, or stop recommendation",
            ],
            red_flags=[
                "Every unit receives last year's budget plus or minus a small percentage",
                "Leadership attention is omitted from resource reviews",
                "Sunk cost protects weak work",
                "Promising work has no capacity despite strategic priority",
                "Short-term volatility causes repeated strategic reversals",
            ],
            agent_lesson=(
                "Strategy becomes real through resource movement. The Manager sets the portfolio logic; Finance validates economics and actuals."
            ),
            hard_gate_candidate=(
                "Quarterly strategy reviews must show how cash, people, capacity, and leadership time changed."
            ),
            retrieval_triggers=[
                "budget inertia", "reallocate resources", "stop project", "strategic priority funding", "portfolio review"
            ],
        ),
        ScenarioCard(
            slug="bk070-organizational-health-power-practices",
            title="Execution health: diagnose clarity, ownership, and market insight before adding more process",
            source_type="consulting_longitudinal_research",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["organizational_health", "role_clarity", "ownership", "strategic_clarity", "execution"],
            source_url="https://www.mckinsey.com/capabilities/people-and-organizational-performance/our-insights/organizational-health-is-still-the-key-to-long-term-performance/",
            local_file="archives/business-knowledge/research/CONSULTING_ACADEMIC_RESEARCH_INDEX.md",
            situation=(
                "Performance is inconsistent even though the business has added meetings, dashboards, policies, and improvement initiatives."
            ),
            decision_pressure=(
                "Leaders copy more best practices because the failure looks like insufficient process, while employees cannot state the priority, their decision rights, or what they personally own."
            ),
            mistake_or_risk=(
                "Do not launch a broad culture programme or a collection of fashionable practices before diagnosing the few behaviours blocking the strategy."
            ),
            recommended_next_action=(
                "Test four foundations with real cases: can teams translate strategy into measurable priorities, make decisions without role confusion, take personal ownership, and use current customer/competitor evidence? Select a coherent small set of behaviour and system changes."
            ),
            evidence_needed=[
                "Employee articulation of current priorities",
                "Recent decisions delayed by role ambiguity",
                "Examples of ownership versus escalation",
                "Customer and competitor insight reaching decisions",
                "Three to five reinforcing behaviour/system changes",
                "Health and business outcome measures",
            ],
            red_flags=[
                "Different leaders promote conflicting management practices",
                "Everything is a priority",
                "Accountability exists without authority",
                "Employees wait for permission on routine exceptions",
                "Culture metrics are disconnected from business outcomes",
            ],
            agent_lesson=(
                "The COO needs enough organizational knowledge to diagnose execution conditions, then assign HR or domain specialists to design interventions."
            ),
            hard_gate_candidate=(
                "Organization-wide improvement programmes require a diagnosed behaviour-to-business causal hypothesis."
            ),
            retrieval_triggers=[
                "organization not executing", "role confusion", "culture problem", "too many processes", "personal ownership"
            ],
        ),
        ScenarioCard(
            slug="bk071-transformation-value-office",
            title="Transformation execution: protect value with one baseline, accountable initiatives, stage gates, and Finance validation",
            source_type="consulting_transformation_research",
            trust_level="high",
            domains=["manager_coo", "finance"],
            agent_targets=["manager_coo", "finance_agent"],
            tags=["transformation", "value_capture", "stage_gate", "benefits_realization", "transformation_office"],
            source_url="https://www.bcg.com/publications/2024/how-to-create-a-transformation-that-lasts",
            local_file="archives/business-knowledge/research/consulting/bcg_transformation_office.html",
            situation=(
                "A cross-functional transformation has many workstreams, optimistic benefits, shared dependencies, and pressure to declare green status."
            ),
            decision_pressure=(
                "Activity can look healthy while value leaks through weak baselines, double-counting, delayed decisions, adoption failure, and unresolved resource conflicts."
            ),
            mistake_or_risk=(
                "Do not treat a transformation as an extra reporting layer or judge initiatives by task completion alone. Multiple competing success measures can also blur accountability."
            ),
            recommended_next_action=(
                "Create a temporary transformation office with one value baseline; one accountable owner and primary outcome per initiative; Finance-validated benefits; stage gates; dependency and issue escalation; adoption measures; and a path to embed controls into business-as-usual."
            ),
            evidence_needed=[
                "Fact-based value baseline",
                "Initiative owner and primary outcome",
                "Finance-validated benefit logic",
                "Stage-gate evidence and stop criteria",
                "Dependencies, resources, and unresolved issues",
                "Adoption and business-as-usual handoff plan",
            ],
            red_flags=[
                "Benefits are double-counted across initiatives",
                "Status is green but value has not reached P&L or customer outcome",
                "Workstream has several measures and no primary accountability",
                "Transformation work is added without freeing capacity",
                "Office has no sunset or handoff design",
            ],
            agent_lesson=(
                "The Manager governs value and cross-functional execution; Finance independently validates whether promised benefits became real."
            ),
            hard_gate_candidate=(
                "Material transformations require a single baseline, Finance benefit validation, stage gates, and named value owners."
            ),
            retrieval_triggers=[
                "transformation office", "benefits leakage", "programme green no value", "stage gate", "transformation governance"
            ],
        ),
        ScenarioCard(
            slug="bk072-diagnose-change-method",
            title="Change strategy: diagnose urgency and uncertainty before choosing plan, experiment, negotiation, or mandate",
            source_type="consulting_change_synthesis",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["change_strategy", "diagnosis", "transformation", "uncertainty", "capability"],
            source_url="https://www.bcg.com/publications/2018/your-change-needs-strategy",
            local_file="archives/business-knowledge/research/consulting/bcg_change_strategy.html",
            situation=(
                "The business must change a process, structure, product, channel, or operating model, but the destination or path may not be fully known."
            ),
            decision_pressure=(
                "Leaders default to a linear project plan even when the solution must be discovered, stakeholders must negotiate it, or several approaches must compete."
            ),
            mistake_or_risk=(
                "Do not use one change method for every situation. A mandate fails when commitment is required; open experimentation wastes time in a true emergency; a fixed plan creates false certainty in a complex problem."
            ),
            recommended_next_action=(
                "Diagnose urgency, clarity of the end state, clarity of the means, need to collaborate or compete, stages of change, and available capability. Choose the method for each stage and define when evidence triggers a method change."
            ),
            evidence_needed=[
                "Urgency and cost of delay",
                "Clarity of destination",
                "Clarity of path and causal mechanism",
                "Stakeholder alignment and power",
                "Capabilities available or missing",
                "Stage-specific method and transition trigger",
            ],
            red_flags=[
                "Detailed plan exists for an untested solution",
                "Emergency response is delayed for consensus",
                "Stakeholder conflict is treated as a training problem",
                "Experiment has no learning question",
                "Method remains fixed after assumptions fail",
            ],
            agent_lesson=(
                "A seasoned COO first diagnoses the type of change, then chooses the operating method and domain specialists needed."
            ),
            hard_gate_candidate=(
                "Cross-functional changes must state urgency, destination/path certainty, stakeholder mode, and method-transition triggers."
            ),
            retrieval_triggers=[
                "how to implement change", "unclear target state", "change resistance", "transformation method", "mandate or experiment"
            ],
        ),
        ScenarioCard(
            slug="bk073-integrated-commercial-excellence-system",
            title="Commercial excellence: run Marketing, Sales, Pricing, and Service as one opportunity system",
            source_type="consulting_industry_survey",
            trust_level="high",
            domains=["sales", "marketing", "finance", "manager_coo"],
            agent_targets=["sales_agent", "marketing_agent", "finance_agent", "manager_coo"],
            tags=["commercial_excellence", "sales_marketing_alignment", "pricing", "service", "customer_data"],
            source_url="https://www.bcg.com/publications/2024/commercial-excellence-in-machinery-and-automation",
            local_file="archives/business-knowledge/research/consulting/bcg_commercial_excellence.html",
            situation=(
                "Marketing produces leads, Sales pursues accounts, Finance controls discounts, and Service sees installed-customer needs—but each function uses different priorities and data."
            ),
            decision_pressure=(
                "Each function can improve its own metric while leads leak, sellers pursue low-fit work, discounts vary, and renewal/service opportunities remain invisible."
            ),
            mistake_or_risk=(
                "Do not optimize campaign volume, sales activity, price approval, or service utilization separately from profitable customer value. BCG's measured results are industrial-context evidence, not a universal uplift promise."
            ),
            recommended_next_action=(
                "Define common segments and opportunity priorities; connect lead, account, quote, order, usage/service, renewal, margin, and loss-reason data; establish shared handoff SLAs; review funnel leakage and customer economics cross-functionally."
            ),
            evidence_needed=[
                "Shared ICP and segment priorities",
                "Lead-to-revenue and service/renewal journey",
                "Handoff definitions and ownership",
                "Price realization and contribution margin",
                "Installed-base or post-sale usage signals",
                "Lost-opportunity reasons by function and segment",
            ],
            red_flags=[
                "Marketing optimizes MQLs without revenue quality",
                "Sales cannot see service or product-usage signals",
                "Finance sees discounts only after commitment",
                "Functions use conflicting customer definitions",
                "Management copies industrial benchmarks as SMB targets",
            ],
            agent_lesson=(
                "Specialists own their craft, but the Manager ensures their operating systems join into profitable customer outcomes."
            ),
            hard_gate_candidate=(
                "Commercial reviews require one shared funnel, customer economics, leakage reasons, and cross-functional action owners."
            ),
            retrieval_triggers=[
                "sales marketing alignment", "commercial excellence", "lead leakage", "service upsell", "shared revenue funnel"
            ],
        ),
        ScenarioCard(
            slug="bk074-pricing-realization-governance",
            title="Pricing discipline: govern realized price and exception patterns, not just the list price",
            source_type="consulting_industry_survey",
            trust_level="high",
            domains=["sales", "finance"],
            agent_targets=["sales_agent", "finance_agent"],
            tags=["pricing", "discounts", "price_realization", "approval", "margin"],
            source_url="https://www.bcg.com/publications/2024/commercial-excellence-in-machinery-and-automation",
            local_file="archives/business-knowledge/research/consulting/bcg_commercial_excellence.html",
            situation=(
                "Sales has broad discount discretion or new prices have been announced, but actual realized prices vary by rep, customer, product, and urgency."
            ),
            decision_pressure=(
                "Closing the deal feels immediate while margin leakage appears later in aggregates, making exceptions easy to justify and hard to reverse."
            ),
            mistake_or_risk=(
                "Do not judge pricing by list-price change or approval compliance alone. Excessive central control can also slow legitimate competitive responses."
            ),
            recommended_next_action=(
                "Create segment/product price corridors; require reason-coded exceptions with deal context; expose conversion probability and stage before approval; track realized price, margin, win rate, and exception concentration promptly; coach outliers and revise corridors from evidence."
            ),
            evidence_needed=[
                "List, target, floor, and realized price",
                "Contribution margin and cost-to-serve",
                "Discount reason, approver, and deal stage",
                "Win/loss and competitor evidence",
                "Rep/customer/product exception distribution",
                "Post-change realization trend",
            ],
            red_flags=[
                "Discount is used to compensate for weak qualification",
                "Exception reason is free text or missing",
                "Top-line growth hides margin decline",
                "Approval is slow but adds no decision insight",
                "One customer or rep receives persistent unexplained concessions",
            ],
            agent_lesson=(
                "Sales protects deal quality and Finance protects economics; both need fast, evidence-based exception governance."
            ),
            hard_gate_candidate=(
                "Below-corridor prices require reason, economics, approval, and later realization review."
            ),
            retrieval_triggers=[
                "discount approval", "price realization", "sales margin leakage", "pricing corridor", "discount outlier"
            ],
        ),
        ScenarioCard(
            slug="bk075-b2b-omnichannel-continuity",
            title="B2B omnichannel: preserve buyer context across human, remote, website, marketplace, and ecommerce touchpoints",
            source_type="consulting_global_buyer_survey",
            trust_level="high",
            domains=["sales", "marketing"],
            agent_targets=["sales_agent", "marketing_agent"],
            tags=["b2b", "omnichannel", "buyer_journey", "personalization", "channel_handoff"],
            source_url="https://www.mckinsey.com/capabilities/growth-marketing-and-sales/our-insights/the-multiplier-effect-how-b2b-winners-grow",
            local_file="archives/business-knowledge/research/CONSULTING_ACADEMIC_RESEARCH_INDEX.md",
            situation=(
                "A B2B buyer researches online, speaks to a seller, requests a demo remotely, visits a marketplace, and expects the next interaction to continue rather than restart the journey."
            ),
            decision_pressure=(
                "Teams launch more channels, but fragmented ownership and inconsistent information create duplicate outreach, lost context, and supplier switching."
            ),
            mistake_or_risk=(
                "Do not confuse channel count with omnichannel capability. Do not copy global-enterprise channel investments before proving where the SMB's buyers actually switch or stall."
            ),
            recommended_next_action=(
                "Map the buyer's real journey; choose the few relevant channels; maintain one account/context record; make price, proof, availability, and claims consistent; define human-to-digital handoffs; measure journey conversion, time, abandonment, and retention by segment."
            ),
            evidence_needed=[
                "ICP channel usage by buying stage",
                "Buyer questions and context captured at each touchpoint",
                "Cross-channel content and offer consistency",
                "Handoff ownership and response time",
                "Conversion, abandonment, and switching reasons",
                "Channel cost and customer lifetime value",
            ],
            red_flags=[
                "Buyer must repeat requirements",
                "Website and seller quote conflict",
                "Channel attribution fights replace journey analysis",
                "Personalization uses weak or non-consensual data",
                "Every new channel is treated as mandatory",
            ],
            agent_lesson=(
                "Marketing designs discoverability and continuity; Sales uses accumulated context to advance the buying decision rather than restart discovery."
            ),
            hard_gate_candidate=(
                "New B2B channels require a context-handoff contract, consistent claims, and journey-level success measures."
            ),
            retrieval_triggers=[
                "B2B omnichannel", "buyer switches channels", "digital and field sales", "customer repeats information", "channel journey"
            ],
        ),
        ScenarioCard(
            slug="bk076-core-adjacency-repeatable-growth",
            title="Growth adjacency: expand one step from a proven core using repeatable capabilities and staged evidence",
            source_type="consulting_strategy_synthesis",
            trust_level="medium",
            domains=["manager_coo", "sales", "marketing"],
            agent_targets=["manager_coo", "sales_agent", "marketing_agent"],
            tags=["growth", "core_business", "adjacency", "repeatable_model", "expansion"],
            source_url="https://media.bain.com/bain-beliefs-in-strategy/",
            local_file="archives/business-knowledge/research/consulting/bain_strategy_beliefs.html",
            situation=(
                "The business wants a new product, customer segment, geography, or channel to accelerate growth beyond its current core."
            ),
            decision_pressure=(
                "A large theoretical market can hide the loss of customer knowledge, channel access, delivery capability, brand permission, and economic advantage."
            ),
            mistake_or_risk=(
                "Do not approve adjacency from TAM alone or treat Bain's synthesis as a universal causal rule. Staying too close to a declining core can also be dangerous."
            ),
            recommended_next_action=(
                "Define the proven core and its repeatable capabilities; score the adjacency's distance in customer, need, product, channel, geography, operations, and economics; run a bounded commercial test; stage resources against adoption, margin, retention, and repeatability."
            ),
            evidence_needed=[
                "Core customer problem and advantage",
                "Capabilities reused versus newly required",
                "Adjacency-distance assessment",
                "Customer willingness-to-pay evidence",
                "Delivery and channel feasibility",
                "Pilot economics, retention, and scale gate",
            ],
            red_flags=[
                "Only market size supports the move",
                "New customer, product, channel, and geography change together",
                "Core is weakened before adjacency proves itself",
                "Pilot success depends on founder heroics",
                "No exit or learning threshold exists",
            ],
            agent_lesson=(
                "The Manager governs strategic distance and staged exposure; Sales and Marketing test demand, channel access, and repeatability."
            ),
            hard_gate_candidate=(
                "Adjacency investments require capability-distance, bounded-test, and scale/stop evidence."
            ),
            retrieval_triggers=[
                "new market expansion", "adjacent product", "grow beyond core", "repeatable model", "TAM opportunity"
            ],
        ),
        ScenarioCard(
            slug="bk077-customer-feedback-closed-loop",
            title="Customer feedback: close the loop from individual recovery to structural correction",
            source_type="consulting_operator_cases",
            trust_level="medium",
            domains=["marketing", "sales", "manager_coo"],
            agent_targets=["marketing_agent", "sales_agent", "manager_coo"],
            tags=["customer_feedback", "retention", "service_recovery", "root_cause", "nps"],
            source_url="https://media.bain.com/Images/LOYALTY_INSIGHTS_Closing_the_loop.pdf",
            local_file="archives/business-knowledge/research/consulting/bain_closing_customer_feedback_loop.pdf",
            situation=(
                "Recent customer feedback or an NPS response reveals dissatisfaction, praise, friction, or a recurring failure."
            ),
            decision_pressure=(
                "The company can celebrate or debate the score while the affected customer receives no response and the underlying process remains unchanged."
            ),
            mistake_or_risk=(
                "Do not make the score the objective, pressure customers for a higher rating, or treat recovery as a substitute for structural prevention."
            ),
            recommended_next_action=(
                "Route feedback quickly to the responsible frontline owner; contact selected customers to listen, recover, and clarify cause; code root causes; escalate systemic themes; assign product/process changes; record action, cost, customer outcome, and recurrence."
            ),
            evidence_needed=[
                "Recent customer feedback and journey context",
                "Severity and recovery priority",
                "Frontline owner and customer contact outcome",
                "Root-cause category and recurrence",
                "Structural action owner",
                "Cost, retention, and recurrence result",
            ],
            red_flags=[
                "Employee asks customer to change a score",
                "Feedback arrives too late to recover",
                "Same root cause is repeatedly handled one customer at a time",
                "Recovery spend ignores customer economics or fairness",
                "NPS moves while complaints or churn worsen",
            ],
            agent_lesson=(
                "Marketing owns learning from the customer system, Sales helps recover relationships, and the Manager ensures recurring causes change operations."
            ),
            hard_gate_candidate=(
                "Severe and repeated feedback requires owner, contact decision, root cause, structural action, and outcome tracking."
            ),
            retrieval_triggers=[
                "customer feedback loop", "NPS detractor", "service recovery", "customer complaint root cause", "retention feedback"
            ],
        ),
        ScenarioCard(
            slug="bk078-structured-sales-peer-learning",
            title="Sales coaching: use short structured peer exchanges to transfer proven techniques",
            source_type="peer_reviewed_field_experiment",
            trust_level="high",
            domains=["sales"],
            agent_targets=["sales_agent"],
            tags=["sales_coaching", "peer_learning", "knowledge_transfer", "field_experiment", "playbook"],
            source_url="https://www.nber.org/papers/w26660",
            local_file="archives/business-knowledge/research/academic/nber_workplace_knowledge_flows_w26660.pdf",
            situation=(
                "Sales performance varies widely and high performers possess useful tacit techniques that are not captured in CRM fields or formal training."
            ),
            decision_pressure=(
                "Management may add team incentives or generic training while sellers lack a safe, structured way to discuss how they handle live situations."
            ),
            mistake_or_risk=(
                "Do not assume incentives automatically transfer knowledge or copy a top seller's style without testing context. The field experiment supports the mechanism in one company, not a guaranteed lift everywhere."
            ),
            recommended_next_action=(
                "Pair sellers for brief recurring sessions around one live scenario; ask what signal was noticed, what wording/action was used, why, and what happened; rotate access to strong performers; convert validated patterns into testable plays and measure post-session behaviour and results."
            ),
            evidence_needed=[
                "Performance variation by comparable seller/context",
                "Specific technique and situation discussed",
                "Partner matching and session cadence",
                "Behaviour adoption after exchange",
                "Revenue, conversion, or cycle outcome",
                "Conditions where the technique failed",
            ],
            red_flags=[
                "Session becomes motivational talk",
                "High performer cannot explain context or mechanism",
                "Confidential customer data is shared improperly",
                "Only output incentives are added",
                "No behaviour or outcome change is measured",
            ],
            agent_lesson=(
                "Tacit sales knowledge spreads through structured case conversation and testing, not merely documentation or team bonuses."
            ),
            hard_gate_candidate=(
                "Sales teams with material performance dispersion should test structured peer learning before buying broad generic training."
            ),
            retrieval_triggers=[
                "sales peer coaching", "top seller techniques", "sales knowledge sharing", "improve rep performance", "sales training"
            ],
        ),
        ScenarioCard(
            slug="bk079-target-setting-with-metric-safety",
            title="Performance management: combine monitoring and controllable targets with quality and gaming safeguards",
            source_type="peer_reviewed_field_experiment",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["performance_management", "target_setting", "monitoring", "feedback", "metric_safety"],
            source_url="https://www.nber.org/papers/w25620",
            local_file="archives/business-knowledge/research/academic/nber_management_practices_airline_w25620.pdf",
            situation=(
                "A skilled workforce has a measurable improvement opportunity, but leaders are unsure whether monitoring, feedback, targets, or incentives will change behaviour."
            ),
            decision_pressure=(
                "A visible metric makes control tempting even when workers cannot fully influence it or optimizing it could damage safety, quality, customers, or other work."
            ),
            mistake_or_risk=(
                "Do not transfer an airline field result mechanically to another role. Monitoring without trust or a controllable metric can create surveillance, gaming, and local optimization."
            ),
            recommended_next_action=(
                "Select a controllable behaviour linked to value; establish baseline and quality/harm counter-metrics; pilot monitoring, feedback, and a calibrated target; explain purpose; review distribution and spillovers; keep, redesign, or stop based on causal evidence."
            ),
            evidence_needed=[
                "Controllable behaviour and value hypothesis",
                "Baseline and comparison group where feasible",
                "Target and feedback design",
                "Quality, safety, fairness, and customer counter-metrics",
                "Worker response and unintended spillovers",
                "Pilot effect and continuation decision",
            ],
            red_flags=[
                "Metric is mostly outside employee control",
                "Only the target outcome is monitored",
                "Workers do not understand the business purpose",
                "Improvement shifts cost or risk elsewhere",
                "Pilot has no comparison or pre-period baseline",
            ],
            agent_lesson=(
                "The Manager uses targets as testable management interventions, while specialists define domain-safe measures and boundaries."
            ),
            hard_gate_candidate=(
                "New performance targets require controllability, counter-metrics, gaming analysis, and a pilot review."
            ),
            retrieval_triggers=[
                "set employee targets", "performance monitoring", "feedback productivity", "KPI pilot", "target unintended consequences"
            ],
        ),
        ScenarioCard(
            slug="bk080-risk-bounded-online-experiment",
            title="Online experiments: predefine harm limits and use valid sequential stopping rather than unsafe peeking",
            source_type="academic_working_paper",
            trust_level="high",
            domains=["marketing", "sales", "manager_coo"],
            agent_targets=["marketing_agent", "sales_agent", "manager_coo"],
            tags=["ab_testing", "experimentation", "guardrails", "sequential_testing", "stop_condition"],
            source_url="https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4472576",
            local_file="archives/business-knowledge/research/CONSULTING_ACADEMIC_RESEARCH_INDEX.md",
            situation=(
                "A campaign, offer, funnel, message, product, or pricing experiment may improve conversion but can also harm customers, revenue, trust, or compliance while it runs."
            ),
            decision_pressure=(
                "Teams either wait for a fixed sample while harm accumulates or repeatedly peek at ordinary significance tests and stop when the result looks favourable."
            ),
            mistake_or_risk=(
                "Do not run a test without exposure and harm limits. Repeatedly checking a fixed-horizon p-value invalidates the claimed error rate; business urgency does not repair the statistics."
            ),
            recommended_next_action=(
                "Write hypothesis, primary metric, guardrail/harm metrics, maximum exposure, minimum detectable effect, segment exclusions, and decision rule before launch; use a valid sequential method if early stopping is needed; preserve assignment and analyze both benefit and harm."
            ),
            evidence_needed=[
                "Pre-registered hypothesis and causal decision",
                "Primary metric and minimum useful effect",
                "Guardrail and harm thresholds",
                "Maximum customer/revenue exposure",
                "Randomization and sample plan",
                "Valid fixed-horizon or sequential stopping rule",
            ],
            red_flags=[
                "Test is stopped when p-value first crosses a threshold",
                "Only conversion is measured",
                "Vulnerable or regulated segments are included without review",
                "Sample ratio or assignment integrity is not checked",
                "Novelty or carryover effects are ignored",
            ],
            agent_lesson=(
                "Specialists design the experiment; the Manager ensures downside is bounded and that the result can support the business decision claimed."
            ),
            hard_gate_candidate=(
                "Customer-facing experiments require predeclared harm limits, exposure caps, and a statistically valid stop rule."
            ),
            retrieval_triggers=[
                "A/B test stop early", "experiment guardrails", "peeking p value", "campaign experiment", "conversion test harm"
            ],
        ),
    ]
)

# Round 4: deeper executional research. These cards come from original,
# locally archived papers with a disclosed research design. Numerical results
# remain evidence about the studied context, never promises for another firm.
SCENARIOS.extend(
    [
        ScenarioCard(
            slug="bk081-management-practices-as-operating-technology",
            title="Management adoption: install measurable operating practices as a tested system, not a training event",
            source_type="peer_reviewed_randomized_field_experiment",
            trust_level="high",
            domains=["manager_coo", "finance"],
            agent_targets=["manager_coo", "finance_agent"],
            tags=["management_practices", "india", "productivity", "delegation", "operating_system"],
            source_url="https://www.nber.org/papers/w16658",
            local_file="archives/business-knowledge/research/management/nber_does_management_matter_india_w16658.pdf",
            situation=(
                "An Indian owner-led business has recurring quality, inventory, information-flow, and delegation problems, but treats them as isolated employee failures."
            ),
            decision_pressure=(
                "Training or software is easier to buy than changing routines, collecting reliable data, and proving that new practices are actually used."
            ),
            mistake_or_risk=(
                "Do not promise the textile-study's 11% average productivity result in another sector. Do not digitize an undefined process or install dashboards without daily operating routines and ownership."
            ),
            recommended_next_action=(
                "Diagnose one value stream; baseline defects, rework, delay, inventory, and cash; install a small linked practice set for measurement, review, ownership, and escalation; coach adoption in live work; compare results; then standardize, delegate, and digitize only what proves useful."
            ),
            evidence_needed=[
                "Baseline quality, efficiency, inventory, and cash measures",
                "Specific linked practices and responsible owners",
                "Adoption evidence from real operating records",
                "Comparison over time or between comparable units",
                "Information required for safe delegation",
                "Sustainment cost and post-support adherence",
            ],
            red_flags=[
                "Software purchase is called transformation",
                "Practice adoption is self-reported only",
                "Owner remains the approval bottleneck despite better data",
                "Many practices launch simultaneously without causal visibility",
                "Productivity rises while quality, inventory, or cash worsens",
            ],
            agent_lesson=(
                "Management practices behave like operating technology: their value comes from a connected routine of facts, decisions, and follow-through, not management vocabulary."
            ),
            hard_gate_candidate=(
                "Management-system rollouts require a baseline, a limited pilot, adoption evidence, and quality/cash counter-metrics before scale."
            ),
            retrieval_triggers=[
                "improve Indian SME productivity", "owner bottleneck", "management practices", "delegate operations", "inventory and quality problem"
            ],
        ),
        ScenarioCard(
            slug="bk082-hybrid-work-by-role-and-workflow",
            title="Hybrid work: decide by role, workflow, communication load, and retention—not executive preference",
            source_type="randomized_field_experiment",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["hybrid_work", "retention", "productivity", "communication", "role_design"],
            source_url="https://www.nber.org/papers/w30292",
            local_file="archives/business-knowledge/research/management/nber_hybrid_work_w30292.pdf",
            situation=(
                "Leaders must set a hybrid-work policy for professional employees while managers and individual contributors report different experiences."
            ),
            decision_pressure=(
                "A universal mandate is simple, and managers may generalize from visibility or coordination discomfort while employees emphasize flexibility and commuting benefits."
            ),
            mistake_or_risk=(
                "Do not apply the study's 33% attrition reduction as a universal forecast. The experiment covered graduate employees at one large technology firm and found materially different manager and non-manager responses."
            ),
            recommended_next_action=(
                "Segment roles by interdependence, customer presence, supervision, concentration work, and security needs; pilot a fixed cadence; track output quality, cycle time, attrition, communication load, inclusion, and manager burden; revise by workflow rather than status."
            ),
            evidence_needed=[
                "Role-level task and collaboration map",
                "Baseline output, quality, and attrition",
                "Office and home-day work pattern",
                "Communication volume and meeting burden",
                "Manager versus non-manager experience",
                "Security, customer, and inclusion constraints",
            ],
            red_flags=[
                "Policy is based only on preference surveys",
                "Presence is treated as productivity",
                "Managers absorb invisible coordination work",
                "Remote employees lose access to decisions or advancement",
                "Output gains hide longer working hours or weekend spillover",
            ],
            agent_lesson=(
                "Hybrid work is an operating-model choice with heterogeneous effects; the COO needs role-level evidence and counter-metrics, not ideology."
            ),
            hard_gate_candidate=(
                "Company-wide workplace mandates require role segmentation and measured effects on output, attrition, communication, inclusion, and customer obligations."
            ),
            retrieval_triggers=[
                "hybrid work policy", "return to office", "remote productivity", "employee attrition", "manager remote work concerns"
            ],
        ),
        ScenarioCard(
            slug="bk083-recognition-design-changes-culture",
            title="Recognition systems: choose deliberately between attendance, output, agency, and knowledge-sharing effects",
            source_type="randomized_field_experiment",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["recognition", "incentives", "worker_agency", "productivity", "knowledge_sharing", "india"],
            source_url="https://www.nber.org/papers/w35138",
            local_file="archives/business-knowledge/research/management/nber_employee_recognition_india_w35138.pdf",
            situation=(
                "A business is designing employee recognition and must decide whether winners are chosen by peers, managers, or an objective/random mechanism."
            ),
            decision_pressure=(
                "Recognition looks inexpensive and symbolic, so leaders ignore how the allocation method teaches employees what behavior and relationships the organization values."
            ),
            mistake_or_risk=(
                "Do not assume one recognition system improves every outcome. In the India firm RCT, worker voting raised attendance, managerial discretion improved productivity, and the manager arm reduced work-related discussion."
            ),
            recommended_next_action=(
                "State the primary behavior sought; define eligibility and evidence; test allocation methods in comparable units; measure attendance, productivity, fairness, discussion, knowledge transfer, and coalition behavior; preserve an appeal path and redesign when one gain damages culture or learning."
            ),
            evidence_needed=[
                "Primary behavior and business outcome sought",
                "Allocation authority and observable criteria",
                "Attendance, productivity, and quality effects",
                "Work-related discussion and knowledge-sharing effects",
                "Fairness perceptions and informal exchanges",
                "Appeal, audit, and review mechanism",
            ],
            red_flags=[
                "Recognition criteria are hidden",
                "Manager favoritism cannot be challenged",
                "Peer voting becomes coalition trading",
                "Output rises while useful discussion falls",
                "Recognition is used as a substitute for fair base conditions",
            ],
            agent_lesson=(
                "Recognition is culture design. Who chooses and what is rewarded can change attendance, output, social behavior, and knowledge spillovers in different directions."
            ),
            hard_gate_candidate=(
                "Material recognition programmes require explicit behavior goals, fairness controls, and culture/knowledge-sharing counter-metrics."
            ),
            retrieval_triggers=[
                "employee recognition programme", "peer voting award", "manager discretionary bonus", "recognition fairness", "knowledge sharing incentive"
            ],
        ),
        ScenarioCard(
            slug="bk084-manager-allocation-under-customer-constraints",
            title="Manager allocation: expose the productivity cost of protecting concentrated customer relationships",
            source_type="administrative_data_and_structural_analysis",
            trust_level="high",
            domains=["manager_coo", "sales"],
            agent_targets=["manager_coo", "sales_agent"],
            tags=["manager_allocation", "customer_concentration", "productivity", "capacity", "key_accounts"],
            source_url="https://www.nber.org/papers/w27006",
            local_file="archives/business-knowledge/research/management/nber_manager_worker_matching_w27006.pdf",
            situation=(
                "The strongest managers repeatedly rescue weak teams or customer-critical work, while other units lose development and productivity opportunities."
            ),
            decision_pressure=(
                "A few powerful customers impose minimum performance constraints, making defensive talent allocation rational even when it reduces total system productivity."
            ),
            mistake_or_risk=(
                "Do not optimize theoretical matching while breaching a key account obligation. Also do not let customer concentration permanently hide weak processes, fragile staffing, or the opportunity cost of rescue allocation."
            ),
            recommended_next_action=(
                "Map manager capability, team capability, customer constraints, and contribution at risk; quantify rescue allocation and opportunity cost; protect contractual minimums; build backups and weaker-team capability; reduce concentration; then test alternative matching without risking delivery."
            ),
            evidence_needed=[
                "Manager and team performance estimates",
                "Customer-specific minimum service or output constraints",
                "Revenue, margin, and relationship concentration",
                "Current rescue assignments and duration",
                "Counterfactual allocation and productivity range",
                "Backup, capability-building, and diversification plan",
            ],
            red_flags=[
                "Top managers are permanent firefighters",
                "Customer concentration is absent from staffing decisions",
                "Weak teams receive rescue but no capability plan",
                "Account value is measured only as revenue",
                "Reallocation is attempted without delivery safeguards",
            ],
            agent_lesson=(
                "Suboptimal-looking talent allocation may reflect a real commercial constraint. A COO surfaces the constraint, protects the account, and removes the structural dependency over time."
            ),
            hard_gate_candidate=(
                "Critical-manager allocation reviews require customer concentration, contribution at risk, opportunity cost, and a dependency-reduction plan."
            ),
            retrieval_triggers=[
                "best manager on worst team", "key account staffing", "customer concentration operations", "manager allocation", "constant firefighting"
            ],
        ),
        ScenarioCard(
            slug="bk085-decision-information-load-design",
            title="Decision packets: reduce overload by structuring relevance, contradiction, uncertainty, and action",
            source_type="peer_reviewed_systematic_literature_review",
            trust_level="medium",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["information_overload", "decision_design", "executive_brief", "attention", "uncertainty"],
            source_url="https://link.springer.com/article/10.1007/s40685-018-0069-z",
            local_file="archives/business-knowledge/research/management/springer_information_overload_2019.pdf",
            situation=(
                "A manager receives long reports, dashboards, messages, and research but still cannot identify the decision, conflict, or next action."
            ),
            decision_pressure=(
                "Adding information feels safer and more rigorous, while cognitive capacity and decision time remain limited."
            ),
            mistake_or_risk=(
                "Do not equate document volume with decision quality. This source is a broad literature review with heterogeneous methods, not a field-tested universal brief format."
            ),
            recommended_next_action=(
                "Begin with the decision and deadline; separate must-know facts from context; show alternatives, base rates, contradictions, uncertainty, and missing evidence; name owner and next action; attach detail for drill-down; test whether the decision-maker can restate the choice and risks."
            ),
            evidence_needed=[
                "Decision, owner, deadline, and consequence of delay",
                "Few decision-relevant facts and their sources",
                "Alternatives and trade-offs",
                "Contradictions and uncertainty",
                "Missing evidence that could change the choice",
                "Action and review trigger",
            ],
            red_flags=[
                "Executive summary has no decision request",
                "Dashboard contains many metrics without thresholds",
                "Conflicting evidence is averaged away",
                "Detail cannot be traced to source",
                "More research is requested without a value-of-information test",
            ],
            agent_lesson=(
                "The Manager's information system should preserve material uncertainty and dissent while compressing everything that does not change the decision."
            ),
            hard_gate_candidate=(
                "Material decision packets must state the decision, alternatives, decisive evidence, uncertainty, contradictions, owner, and review trigger."
            ),
            retrieval_triggers=[
                "too much information", "executive decision brief", "dashboard overload", "decision packet", "research paralysis"
            ],
        ),
        ScenarioCard(
            slug="bk086-quota-frequency-by-performer-and-product",
            title="Sales quotas: tune frequency by performer behavior and product economics, not volume alone",
            source_type="peer_reviewed_sales_field_experiment",
            trust_level="high",
            domains=["sales", "finance", "manager_coo"],
            agent_targets=["sales_agent", "finance_agent", "manager_coo"],
            tags=["sales_quota", "sales_compensation", "performer_segments", "product_mix", "profit"],
            source_url="https://pubsonline.informs.org/doi/10.1287/mnsc.2020.3648",
            local_file="archives/business-knowledge/research/sales/harvard_quota_frequency_sales.pdf",
            situation=(
                "Sales leaders are considering daily, weekly, or monthly quotas to prevent sellers from giving up when a period target looks unreachable."
            ),
            decision_pressure=(
                "A more frequent quota can increase unit sales and visible effort, but it may change timing, product mix, returns, margin, and the behavior of high versus low performers."
            ),
            mistake_or_risk=(
                "Do not optimize quota attainment or units alone. In one Swedish retailer experiment, daily quotas helped low performers but induced high performers to give up earlier and shifted selling toward low-ticket items, hurting profits."
            ),
            recommended_next_action=(
                "Segment sellers by comparable baseline performance; model quota frequency against deal cycle and product mix; pilot with a control; measure revenue, contribution margin, returns, high-ticket mix, timing, customer outcome, and post-period pull-forward; retain different cadences where justified."
            ),
            evidence_needed=[
                "Baseline performance distribution",
                "Deal cycle and quota period fit",
                "Unit, revenue, margin, returns, and product mix",
                "Effort and give-up timing within the period",
                "High- and low-performer treatment effects",
                "Pull-forward and post-period effects",
            ],
            red_flags=[
                "Quota lift is reported without profit",
                "All performers receive the same cadence by default",
                "Low-ticket activity crowds out strategic sales",
                "Returns or cancellations rise after the period",
                "A short pilot rewards demand pull-forward as growth",
            ],
            agent_lesson=(
                "Quota frequency changes seller time horizons and product choice. Sales owns behavior design; Finance validates realized contribution, not just attainment."
            ),
            hard_gate_candidate=(
                "Quota-frequency changes require performer-level analysis plus margin, mix, returns, and post-period counter-metrics."
            ),
            retrieval_triggers=[
                "daily sales quota", "salespeople give up", "quota frequency", "high performer incentive", "sales target hurts profit"
            ],
        ),
        ScenarioCard(
            slug="bk087-conditional-versus-reciprocal-sales-rewards",
            title="Sales rewards: distinguish conditional incentives from genuine delayed reciprocity",
            source_type="peer_reviewed_sales_field_experiment",
            trust_level="high",
            domains=["sales", "finance", "manager_coo"],
            agent_targets=["sales_agent", "finance_agent", "manager_coo"],
            tags=["sales_compensation", "bonus", "reciprocity", "heterogeneity", "future_performance"],
            source_url="https://dash.harvard.edu/bitstreams/7312037e-73ce-6bd4-e053-0100007fdf3b/download",
            local_file="archives/business-knowledge/research/sales/harvard_incentives_reciprocity_sales.pdf",
            situation=(
                "A sales team is choosing between quota bonuses, penalty-framed bonuses, and unconditional rewards intended to create reciprocity."
            ),
            decision_pressure=(
                "A short-term response can make any reward appear successful even when performance falls later, repeated gifts lose meaning, or only some seller types respond."
            ),
            mistake_or_risk=(
                "Do not relabel an expected payment as a gift or assume penalty framing adds power. The Asian consumer-durables field experiment found quota bonuses improved performance but could lower future performance; delayed reciprocal rewards worked selectively and weakened with repetition."
            ),
            recommended_next_action=(
                "Define whether the mechanism is conditional pay or an infrequent genuine recognition reward; segment by baseline performance; randomize or stagger a pilot; track current and future sales, margin, gaming, trust, and repeated-exposure decay; stop mechanisms whose temporary lift borrows from the future."
            ),
            evidence_needed=[
                "Compensation mechanism and employee expectation",
                "Comparable baseline seller performance",
                "Current-period revenue and contribution",
                "Future-period performance and pull-forward",
                "Repeated-exposure response",
                "Trust, fairness, and gaming signals",
            ],
            red_flags=[
                "Ordinary variable pay is marketed as generosity",
                "Penalty framing is adopted without incremental evidence",
                "Future-period decline is ignored",
                "Reward is repeated until it becomes entitlement",
                "Average effect hides seller heterogeneity",
            ],
            agent_lesson=(
                "Conditional incentives and reciprocity are different behavioral contracts. Use each honestly, test heterogeneity, and account for future performance."
            ),
            hard_gate_candidate=(
                "Sales-compensation pilots require future-period, margin, gaming, and performer-segment analysis before scale."
            ),
            retrieval_triggers=[
                "sales bonus or gift", "punitive bonus", "employee reciprocity", "sales incentive future decline", "reward top salespeople"
            ],
        ),
        ScenarioCard(
            slug="bk088-map-ai-frontier-by-task",
            title="AI deployment: map and test the capability frontier at task level before trusting polished output",
            source_type="peer_reviewed_preregistered_field_experiment",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["generative_ai", "human_ai", "task_design", "quality", "verification"],
            source_url="https://doi.org/10.1287/orsc.2025.21838",
            local_file="archives/business-knowledge/research/management/hbs_jagged_ai_frontier.pdf",
            situation=(
                "A business wants agents or employees to use generative AI across a knowledge workflow containing apparently similar research, analysis, writing, and judgment tasks."
            ),
            decision_pressure=(
                "Strong speed and quality on many tasks creates generalized trust, while failures outside the model's capability frontier can remain fluent and difficult to detect."
            ),
            mistake_or_risk=(
                "Do not infer workflow-wide reliability from average gains. In the preregistered experiment with 758 consultants, AI improved completion, speed, and quality on in-frontier tasks but users were 19% less likely to solve the selected outside-frontier managerial task correctly."
            ),
            recommended_next_action=(
                "Decompose workflows into decisions and subtasks; create representative gold cases including adversarial boundary cases; compare human, AI, and human-plus-AI performance; require source/evidence checks and human ownership for uncertain or consequential tasks; re-evaluate after model or workflow changes."
            ),
            evidence_needed=[
                "Task inventory and consequence classification",
                "Representative gold-standard cases",
                "Human, AI, and combined accuracy/quality/time",
                "Boundary and deceptive-failure examples",
                "Verification method and accountable human",
                "Model/version and re-evaluation trigger",
            ],
            red_flags=[
                "One benchmark score authorizes an entire workflow",
                "Fluency is treated as correctness",
                "Employees cannot identify when independent verification is required",
                "High-consequence decisions lack source checks",
                "Model upgrades silently change behavior",
            ],
            agent_lesson=(
                "The COO governs AI as a changing task-level capability map, not a binary adoption decision. Strong performance in one step can increase overreliance in the next."
            ),
            hard_gate_candidate=(
                "Consequential AI-assisted workflows require task-level evals, boundary cases, accountable verification, and version-triggered revalidation."
            ),
            retrieval_triggers=[
                "where to use AI", "AI hallucination business", "human in the loop", "AI workflow evaluation", "jagged frontier"
            ],
        ),
        ScenarioCard(
            slug="bk089-paid-digital-ads-amplify-readiness",
            title="Digital advertising: test paid reach against organic proof and business readiness",
            source_type="large_scale_randomized_field_experiment",
            trust_level="high",
            domains=["marketing", "sales", "finance"],
            agent_targets=["marketing_agent", "sales_agent", "finance_agent"],
            tags=["digital_advertising", "small_business", "organic_traffic", "reviews", "incrementality"],
            source_url="https://www.nber.org/papers/w30925",
            local_file="archives/business-knowledge/research/marketing/nber_digital_advertising_w30925.pdf",
            situation=(
                "A small business is deciding whether paid platform advertising will create incremental demand or merely buy visibility for an unready listing and weak offer."
            ),
            decision_pressure=(
                "Platforms report clicks and intention signals quickly, encouraging uniform spend before the business has proof, reviews, conversion capacity, or a causal holdout."
            ),
            mistake_or_risk=(
                "Do not promise the Yelp restaurant experiment's 7–19% purchase-intention lift or 5% review lift elsewhere. Effects varied: independent, higher-rated, more-reviewed, and organically visited firms gained more."
            ),
            recommended_next_action=(
                "Audit listing accuracy, rating/review proof, offer, landing conversion, average order value, contribution after shipping/returns/discounts, repeat purchase, fulfilment capacity, and CAC ceiling; use randomized geo/audience/time holdouts where feasible; start with high-readiness segments; measure incremental qualified demand, orders, contribution, reviews, repeat behavior, and saturation."
            ),
            evidence_needed=[
                "Organic traffic, rating, review count, and listing completeness",
                "Customer segment and purchase-intent baseline",
                "Randomized or credible holdout design",
                "Qualified leads, orders, contribution, and repeat behavior",
                "Average order value, returns, fulfilment cost, and CAC ceiling",
                "Capacity and customer-experience effects",
                "Incremental return by business/segment attribute",
            ],
            red_flags=[
                "Clicks are treated as revenue",
                "Ads launch before listing and conversion hygiene",
                "Platform-attributed sales have no holdout",
                "Poor reviews or capacity constraints are ignored",
                "Average ROAS hides segment losses",
                "Creative promise exceeds product or fulfilment proof",
            ],
            agent_lesson=(
                "Paid reach often complements credible organic proof rather than substituting for it. Marketing tests incrementality; Sales checks lead quality; Finance validates contribution."
            ),
            hard_gate_candidate=(
                "Scaled ad spend requires a readiness audit, incremental measurement, contribution economics, and capacity/customer-experience guardrails."
            ),
            retrieval_triggers=[
                "should small business run ads", "digital advertising ROI", "Yelp ads", "paid ads no sales", "organic proof before ads"
            ],
        ),
        ScenarioCard(
            slug="bk090-local-listing-minimum-marketing-infrastructure",
            title="Local discovery: establish a verified online listing before sophisticated acquisition campaigns",
            source_type="natural_experiment_and_administrative_data",
            trust_level="high",
            domains=["marketing", "sales"],
            agent_targets=["marketing_agent", "sales_agent"],
            tags=["local_marketing", "online_listing", "small_business", "discoverability", "reputation"],
            source_url="https://www.nber.org/papers/w30810",
            local_file="archives/business-knowledge/research/marketing/nber_online_listings_w30810.pdf",
            situation=(
                "A local business is absent, duplicated, inaccurate, or unmanaged on major discovery platforms while considering paid media and advanced digital tactics."
            ),
            decision_pressure=(
                "Campaigns feel strategic, while claiming a free listing, fixing hours, and building evidence looks basic and can expose weak ratings."
            ),
            mistake_or_risk=(
                "Do not guarantee the restaurant study's estimated 5% revenue increase or infer that every platform fits every business. Businesses that stayed offline also tended to have lower ratings, so presence and reputation must be managed together."
            ),
            recommended_next_action=(
                "Identify where the ICP searches; claim and verify the listing; standardize name, category, address, phone, hours, offer, photos, and proof; route enquiries; request authentic reviews after real service; respond to issues; track discovery-to-contact-to-revenue before adding paid reach."
            ),
            evidence_needed=[
                "ICP discovery platforms and queries",
                "Listing ownership, accuracy, and duplication status",
                "Contact/booking route and response owner",
                "Authentic rating, review, and complaint pattern",
                "Discovery, contact, conversion, and revenue baseline",
                "Platform policy and privacy requirements",
            ],
            red_flags=[
                "Ads point to an inaccurate or unclaimed listing",
                "Reviews are purchased or fabricated",
                "Phone/messages have no response owner",
                "Only ranking is tracked",
                "Offline reputation problems are hidden rather than fixed",
            ],
            agent_lesson=(
                "For many local SMBs, accurate discovery and response infrastructure is the first digital marketing capability, not an administrative afterthought."
            ),
            hard_gate_candidate=(
                "Local paid campaigns require verified listings, authentic proof, working response paths, and conversion tracking."
            ),
            retrieval_triggers=[
                "business not on Google", "local business listing", "online presence SMB", "local marketing basics", "claim business profile"
            ],
        ),
        ScenarioCard(
            slug="bk091-tradeoff-transparency-for-customer-fit",
            title="Offer design: disclose meaningful trade-offs to improve customer fit, usage, and retention",
            source_type="large_scale_randomized_field_experiment",
            trust_level="high",
            domains=["marketing", "sales", "compliance", "manager_coo"],
            agent_targets=["marketing_agent", "sales_agent", "compliance_agent", "manager_coo"],
            tags=["tradeoff_transparency", "customer_fit", "retention", "offer_design", "disclosure"],
            source_url="https://www.hbs.edu/ris/Publication%20Files/20-013_Jan_2021_revision_5e19ba8f-9ced-4fdc-b69f-fc3e19fdbd23.pdf",
            local_file="archives/business-knowledge/research/marketing/hbs_tradeoff_transparency.pdf",
            situation=(
                "Several products or plans suit different customers, and acquisition messaging can simplify the choice by emphasizing benefits while obscuring meaningful trade-offs."
            ),
            decision_pressure=(
                "Teams fear that honest comparison will reduce conversion, so they optimize acquisition while poor-fit customers underuse, complain, cancel, or suffer avoidable harm."
            ),
            mistake_or_risk=(
                "Do not claim transparency always lifts acquisition: the 393,036-prospect bank experiment found no significant acquisition effect, but changed choices and improved later engagement and retention among account openers; promotion weakened some benefits."
            ),
            recommended_next_action=(
                "Identify customer jobs and material trade-offs; present a balanced comparison at the decision point; test comprehension and choice quality; track acquisition separately from product fit, usage, cancellation, complaints, late payment/harm, and promotion interaction; let Compliance validate completeness and prominence."
            ),
            evidence_needed=[
                "Material benefits, limits, fees, and customer-fit criteria",
                "Disclosure prominence and comprehension",
                "Choice distribution and acquisition",
                "Usage, retention, complaints, and harm",
                "Results by prior category experience",
                "Promotion interaction and compliance review",
            ],
            red_flags=[
                "Only advantages are compared",
                "Disclosure exists in fine print but not the choice interface",
                "Conversion is measured without downstream fit",
                "Promotion steers buyers away from suitable options",
                "Transparency is used to excuse a fundamentally harmful offer",
            ],
            agent_lesson=(
                "Good selling is not maximum persuasion; it is helping the right customer select the right trade-off, then measuring the relationship that follows."
            ),
            hard_gate_candidate=(
                "Multi-option offers require material-tradeoff disclosure, comprehension checks, downstream fit metrics, and Compliance review."
            ),
            retrieval_triggers=[
                "show product disadvantages", "customer fit", "reduce cancellations", "transparent sales", "compare plans honestly"
            ],
        ),
        ScenarioCard(
            slug="bk092-operational-transparency-as-service-proof",
            title="Service marketing: reveal selected real work to make effort credible and connect employees to customer impact",
            source_type="multi_study_field_and_laboratory_experiments",
            trust_level="high",
            domains=["marketing", "sales", "manager_coo"],
            agent_targets=["marketing_agent", "sales_agent", "manager_coo"],
            tags=["operational_transparency", "service_marketing", "trust", "employee_motivation", "process_proof"],
            source_url="https://www.hbs.edu/ris/Publication%20Files/14-115_aee7737a-a405-46f1-85e9-67882dd95435.pdf",
            local_file="archives/business-knowledge/research/marketing/hbs_operational_transparency.pdf",
            situation=(
                "Customers cannot see the work behind a service and undervalue effort, while employees are disconnected from the beneficiary of their work."
            ),
            decision_pressure=(
                "Marketing can make unsupported claims, or operations can expose everything without considering privacy, safety, distraction, or whether the visible process is actually healthy."
            ),
            mistake_or_risk=(
                "Do not generalize the food-service studies' 22.2% perceived-quality and 19.2% throughput results to every setting. Visibility can backfire when work is unsafe, private, confusing, staged, or burdens employees."
            ),
            recommended_next_action=(
                "Select a safe, truthful process moment that demonstrates relevant effort or progress; protect customer/employee privacy; pilot one-way or mutual visibility; measure perceived value, objective quality, speed, employee effort/satisfaction, and complaints; remove theatrical steps with no customer value."
            ),
            evidence_needed=[
                "Customer uncertainty the visibility should resolve",
                "Real process step and value mechanism",
                "Privacy, safety, and employee-consent controls",
                "Perceived and objective quality",
                "Cycle time and employee response",
                "Complaint and adverse-behavior monitoring",
            ],
            red_flags=[
                "Backstage is staged for appearance",
                "Sensitive customer or employee information is exposed",
                "Visibility adds work without value",
                "Perception rises while objective quality falls",
                "Employees feel surveilled rather than connected to impact",
            ],
            agent_lesson=(
                "Operational transparency can be credible marketing when it reveals genuine value creation; it is an operating-design intervention, not content decoration."
            ),
            hard_gate_candidate=(
                "Operational-transparency campaigns require truthful process proof, privacy/safety review, employee impact checks, and objective quality metrics."
            ),
            retrieval_triggers=[
                "show behind the scenes", "service trust", "prove service quality", "operational transparency", "customers cannot see effort"
            ],
        ),
        ScenarioCard(
            slug="bk093-review-authenticity-and-manipulation-risk",
            title="Review strategy: build verified customer proof and detect incentive-driven manipulation",
            source_type="peer_reviewed_empirical_platform_comparison",
            trust_level="high",
            domains=["marketing", "compliance", "manager_coo"],
            agent_targets=["marketing_agent", "compliance_agent", "manager_coo"],
            tags=["online_reviews", "reputation", "fraud", "platform_policy", "verification"],
            source_url="https://www.nber.org/papers/w18340",
            local_file="archives/business-knowledge/research/marketing/nber_promotional_reviews_w18340.pdf",
            situation=(
                "Online reviews materially affect discovery and trust, creating pressure to manufacture positive proof or attack competitors."
            ),
            decision_pressure=(
                "Fake-review tactics can appear cheap and competitors may seem to use them, while detection, platform removal, consumer deception, and lasting reputation damage arrive later."
            ),
            mistake_or_risk=(
                "Do not infer that every unusual review is fake. The study found patterns consistent with manipulation by comparing open and transaction-verified hotel platforms and incentives; it did not directly observe each review's author."
            ),
            recommended_next_action=(
                "Request reviews neutrally after verified transactions; prohibit staff/vendor fabrication and competitor attacks; preserve consent and platform rules; monitor bursts, duplication, extreme-rating patterns, unverifiable transactions, and vendor access; investigate before action; fix root-cause service problems."
            ),
            evidence_needed=[
                "Verified transaction or service relationship",
                "Review solicitation wording and timing",
                "Platform policy and applicable consumer law",
                "Rating/time/text/device/vendor anomaly pattern",
                "Access logs and third-party instructions",
                "Underlying service and complaint evidence",
            ],
            red_flags=[
                "Vendor guarantees a rating or review volume",
                "Employees review their own business",
                "Negative competitor reviews originate near campaign activity",
                "Incentives are conditional on positive sentiment",
                "Anomaly is declared fraud without investigation",
            ],
            agent_lesson=(
                "Authentic reputation is a compounding asset. Marketing grows verified proof; Compliance prevents deceptive collection; the Manager rejects shortcuts that poison trust."
            ),
            hard_gate_candidate=(
                "Review-generation programmes require verified-customer eligibility, neutral solicitation, platform/legal review, vendor controls, and anomaly monitoring."
            ),
            retrieval_triggers=[
                "get more reviews", "buy Google reviews", "fake competitor reviews", "review manipulation", "online reputation strategy"
            ],
        ),
    ]
)

# Round 5: specialist execution and COO control points. These cards add
# mechanisms, failure conditions, and explicit transfer boundaries from field
# experiments, Indian regulators, cyber standards, and Indian business surveys.
SCENARIOS.extend(
    [
        ScenarioCard(
            slug="bk094-recipient-benefiting-referral-incentives",
            title="Customer referrals: reward the action bottleneck, not automatically the referrer",
            source_type="multi_study_field_and_controlled_experiments",
            trust_level="high",
            domains=["marketing", "sales", "finance"],
            agent_targets=["marketing_agent", "sales_agent", "finance_agent"],
            tags=["referrals", "incentives", "word_of_mouth", "acquisition", "unit_economics"],
            source_url="https://www.hbs.edu/ris/download.aspx?name=GershonCryderJohn+-+Why+Prosocial+Incentives+Work.pdf",
            local_file="archives/business-knowledge/research/hbs/GershonCryderJohn - Why Prosocial Incentives Work_3a65737a-0749-4008-86f6-70aa9945db97.pdf",
            situation=(
                "A referral programme produces links or introductions, but too few referred prospects take the costly next step, such as registering, attending, buying, or activating."
            ),
            decision_pressure=(
                "The default is to raise the referrer's reward because the sender is visible, even when recipient effort—not sender willingness—is the binding constraint."
            ),
            mistake_or_risk=(
                "Do not optimize referral messages sent or copy the study's consumer effect sizes. Incentives can attract low-fit customers, create gaming, weaken trust, or violate consent, tax, anti-spam, and platform rules."
            ),
            recommended_next_action=(
                "Map the sender and recipient actions separately; test no reward, sender reward, recipient reward, shared reward, and a prosocial non-cash variant where appropriate; keep total value comparable; measure verified activation, contribution, fraud, repeat use, and sender reputation."
            ),
            evidence_needed=[
                "Sender willingness and referral completion by variant",
                "Recipient view, activation, and purchase by variant",
                "Incentive and fulfilment cost",
                "Incremental contribution and retention",
                "Duplicate, self-referral, and low-quality patterns",
                "Consent, tax, platform, and communication-policy review",
            ],
            red_flags=[
                "Referral volume rises while activation falls",
                "Reward value differs across test cells",
                "Existing organic referrals are paid without an incrementality test",
                "Customers can self-refer or create duplicate accounts",
                "Sales pressure damages the referrer's relationship",
            ],
            agent_lesson=(
                "Marketing designs the mechanism, Sales validates referred-customer quality, and Finance validates incremental contribution. Reward the scarce action rather than the most visible participant."
            ),
            hard_gate_candidate=(
                "Scaled referral incentives require verified activation, fraud controls, contribution economics, retention, and communication-policy review."
            ),
            retrieval_triggers=[
                "referral program not working", "refer a friend incentive", "reward customer or friend", "word of mouth growth", "referral fraud"
            ],
        ),
        ScenarioCard(
            slug="bk095-bounded-learn-then-earn-pricing",
            title="Pricing experiments: buy demand learning with a bounded loss budget",
            source_type="field_experiment_and_pricing_algorithm",
            trust_level="high",
            domains=["sales", "marketing", "finance", "manager_coo"],
            agent_targets=["sales_agent", "marketing_agent", "finance_agent", "manager_coo"],
            tags=["pricing", "experimentation", "demand_learning", "margin", "governance"],
            source_url="https://www.hbs.edu/ris/download.aspx?name=Demand+Learning+and+Pricing+for+Varying+Assortments.pdf",
            local_file="archives/business-knowledge/research/hbs/Demand Learning and Pricing for Varying Assortments_d1e6413b-cfb4-4a7a-94d7-0d8534287f20.pdf",
            situation=(
                "The business lacks reliable price-response evidence because products, inventory, segments, or seasons change and historical prices barely vary."
            ),
            decision_pressure=(
                "Teams either avoid learning to protect this month's revenue or change prices opportunistically without a design that can separate price response from product and time effects."
            ),
            mistake_or_risk=(
                "Do not expose vulnerable customers to arbitrary discrimination, breach quoted or regulated prices, or allow an algorithm to explore without price, margin, volume, reputation, and cash limits."
            ),
            recommended_next_action=(
                "Define the decision and reusable product attributes; estimate downside scenarios; preapprove a small exploration budget, price band, margin floor, customer-fairness rule, sample requirement, and stop conditions; randomize where feasible; then use the learning only in comparable contexts and continue monitoring."
            ),
            evidence_needed=[
                "Price decision and comparable product attributes",
                "Baseline demand, margin, capacity, and seasonality",
                "Randomization or credible comparison design",
                "Maximum revenue/margin learning budget",
                "Customer fairness, contract, and legal constraints",
                "Demand response uncertainty and transfer test",
            ],
            red_flags=[
                "Price changes coincide with promotion or assortment changes without controls",
                "No maximum downside or stopping rule",
                "Revenue is optimized while contribution or capacity is ignored",
                "Different customers discover unexplained unfair prices",
                "One experiment is applied to a materially different segment",
            ],
            agent_lesson=(
                "Sales and Marketing frame willingness-to-pay hypotheses; Finance sets economic guardrails; the Manager approves the learning budget and fairness envelope, not individual prices."
            ),
            hard_gate_candidate=(
                "Adaptive pricing requires an approved learning budget, price and margin bounds, fairness review, causal design, and explicit transfer limits."
            ),
            retrieval_triggers=[
                "how to test price", "pricing experiment", "unknown demand curve", "dynamic pricing SMB", "learn then earn"
            ],
        ),
        ScenarioCard(
            slug="bk096-promotion-incrementality-experiment-library",
            title="Promotion targeting: learn incrementality from a consistent experiment library",
            source_type="large_multi_experiment_machine_learning_study",
            trust_level="high",
            domains=["marketing", "finance", "sales"],
            agent_targets=["marketing_agent", "finance_agent", "sales_agent"],
            tags=["promotions", "incrementality", "experimentation", "targeting", "machine_learning"],
            source_url="https://www.hbs.edu/ris/download.aspx?name=24-076.pdf",
            local_file="archives/business-knowledge/research/hbs/24-076_c3424b9b-adbb-4aa9-897f-c29ba27687aa.pdf",
            situation=(
                "The business has run many randomized offers and wants to decide which customers should receive a new promotion without discounting customers who would buy anyway."
            ),
            decision_pressure=(
                "Response propensity is easier to predict than causal lift, so high-propensity customers receive margin giveaways even when the promotion did not change their behaviour."
            ),
            mistake_or_risk=(
                "Do not deploy an incrementality model when experiment definitions, outcomes, eligibility, or randomization are inconsistent. Historical patterns can fail after a new channel, product, segment, or offer concept."
            ),
            recommended_next_action=(
                "Create an experiment registry with treatment, control, eligibility, exposure, cost, outcome window, and contribution; train only on comparable randomized history; reserve a holdout for the new campaign; compare model targeting with simple policies; monitor uplift, margin, retention, complaints, and drift."
            ),
            evidence_needed=[
                "Randomized experiment registry and stable definitions",
                "Treatment exposure and control integrity",
                "Incremental orders, contribution, and retention",
                "Offer/customer/channel similarity to history",
                "Out-of-sample and new-campaign holdout performance",
                "Privacy, fairness, and exclusion rules",
            ],
            red_flags=[
                "Only redeemers or attributed buyers are analyzed",
                "No untreated control exists",
                "Discount cost is excluded from uplift",
                "Outcome windows changed across experiments",
                "A materially new offer is launched without a fresh holdout",
            ],
            agent_lesson=(
                "Marketing owns causal targeting design, Finance owns incremental contribution, and Sales checks channel effects. A response model is not an incrementality model."
            ),
            hard_gate_candidate=(
                "Model-targeted promotions require randomized historical evidence, stable definitions, a live holdout, contribution economics, and drift monitoring."
            ),
            retrieval_triggers=[
                "promotion incrementality", "discount customers who would buy", "uplift model", "coupon targeting", "causal marketing"
            ],
        ),
        ScenarioCard(
            slug="bk097-search-friction-transparency-guardrail",
            title="Digital merchandising: treat profitable search friction as a transparency risk",
            source_type="retailer_field_experiments",
            trust_level="high",
            domains=["marketing", "compliance", "manager_coo"],
            agent_targets=["marketing_agent", "compliance_agent", "manager_coo"],
            tags=["digital_merchandising", "search_friction", "consumer_transparency", "dark_patterns", "retention"],
            source_url="https://www.hbs.edu/ris/download.aspx?name=19-080.pdf",
            local_file="archives/business-knowledge/research/hbs/19-080_aae30b91-4631-422c-b843-b55e7db9e3ae.pdf",
            situation=(
                "An interface test makes discounts, lower-priced options, cancellation, or comparison less visible and appears to improve conversion or average selling price."
            ),
            decision_pressure=(
                "Short-term revenue creates pressure to ship the treatment before measuring whether customers understood the choice or later returned, complained, churned, or lost trust."
            ),
            mistake_or_risk=(
                "Do not deliberately hide material prices, terms, eligibility, recurring charges, cancellation routes, or safer alternatives. A profitable friction can be a dark pattern or a deferred service and reputation cost."
            ),
            recommended_next_action=(
                "Classify the affected information; test task completion and comprehension with representative users; segment new and experienced customers; measure contribution alongside findability, choice quality, returns, cancellation, complaints, support load, and repeat behaviour; obtain current consumer-law review."
            ),
            evidence_needed=[
                "Exact element removed, delayed, or deprioritized",
                "Materiality to customer choice",
                "Findability and comprehension by segment",
                "Conversion, contribution, returns, and cancellation",
                "Complaints, support contacts, and repeat behaviour",
                "Current consumer-law and platform review",
            ],
            red_flags=[
                "Team celebrates price lift without comprehension data",
                "Cancellation or cheaper options require excessive steps",
                "Vulnerable or inexperienced users are disproportionately affected",
                "Material information exists only in fine print",
                "A/B test horizon ends before returns or churn appear",
            ],
            agent_lesson=(
                "Marketing may test discovery, but Compliance protects informed choice and the Manager rejects revenue mechanisms that convert transparency into customer harm."
            ),
            hard_gate_candidate=(
                "Tests that reduce price, term, option, or cancellation visibility require comprehension, downstream-harm metrics, and Compliance approval."
            ),
            retrieval_triggers=[
                "hide discounts conversion", "dark pattern test", "search friction ecommerce", "make cheaper plan less visible", "conversion versus transparency"
            ],
        ),
        ScenarioCard(
            slug="bk098-short-term-surrogates-for-long-term-targeting",
            title="Long-term targeting: validate short-term surrogate signals before trusting noisy CLV predictions",
            source_type="theory_simulation_and_field_experiment",
            trust_level="high",
            domains=["marketing", "finance"],
            agent_targets=["marketing_agent", "finance_agent"],
            tags=["targeting", "customer_lifetime_value", "surrogates", "retention", "causal_inference"],
            source_url="https://www.hbs.edu/ris/download.aspx?name=23-023.pdf",
            local_file="archives/business-knowledge/research/hbs/23-023_5b02c937-1c15-42ea-9a95-ae7b6f17fd21.pdf",
            situation=(
                "A campaign decision must optimize long-term customer value, but the final outcome is delayed, sparse, and noisy while early engagement signals arrive quickly."
            ),
            decision_pressure=(
                "Teams either optimize the easy short-term metric as if it were value or fit heterogeneous effects directly to a noisy long-term outcome and obtain unstable targeting."
            ),
            mistake_or_risk=(
                "Do not assume clicks, opens, first orders, or early frequency are valid surrogates. A treatment can improve the proxy while harming margin, habit quality, churn, or customer welfare."
            ),
            recommended_next_action=(
                "Use completed historical cohorts to test whether early signals predict treatment effects on long-term contribution; separate frequency and churn mechanisms; cross-validate against simple policies; run a live holdout; monitor surrogate drift and long-term reversals."
            ),
            evidence_needed=[
                "Completed cohorts with treatment, early signals, and long-term outcomes",
                "Contribution rather than gross activity",
                "Surrogate validity across segments and treatments",
                "Separate frequency, retention, and cost mechanisms",
                "Comparison with simple all/none rules",
                "Live holdout and delayed-outcome review",
            ],
            red_flags=[
                "A correlational proxy is declared causal without validation",
                "Only short-term engagement is reported",
                "Model complexity beats no credible baseline",
                "Churn and frequency are collapsed into one opaque score",
                "Targeting continues after surrogate relationships drift",
            ],
            agent_lesson=(
                "Marketing may use faster signals, but Finance validates that they preserve long-term contribution. The proxy earns trust only by predicting downstream treatment effects."
            ),
            hard_gate_candidate=(
                "Surrogate-based targeting requires completed-cohort validation, contribution outcomes, a simple-policy benchmark, live holdout, and delayed review."
            ),
            retrieval_triggers=[
                "optimize CLV with short data", "marketing surrogate metric", "long term targeting", "coupon targeting retention", "noisy customer lifetime value"
            ],
        ),
        ScenarioCard(
            slug="bk099-employee-referrals-quality-retention",
            title="Employee referrals: optimize retained productive hires and team effects, not referral volume",
            source_type="randomized_field_experiment",
            trust_level="high",
            domains=["manager_coo"],
            agent_targets=["manager_coo"],
            tags=["employee_referrals", "hiring", "retention", "incentives", "workforce"],
            source_url="https://www.nber.org/papers/w25920",
            local_file="archives/business-knowledge/research/management/nber_employee_referrals_w25920.pdf",
            situation=(
                "A growing business needs hires quickly and is considering larger employee referral bonuses after seeing high referral participation."
            ),
            decision_pressure=(
                "Referral count is immediate and visible, while hire quality, network homogeneity, non-referred employee effects, productivity, and retention emerge later."
            ),
            mistake_or_risk=(
                "Do not assume a larger bonus improves outcomes. In the field experiment it increased referral quantity while reducing average quality; programme value also came through indirect retention effects."
            ),
            recommended_next_action=(
                "Pilot by role/site; preserve the same structured assessment for referred and non-referred candidates; vary bonus carefully; track qualified referrals, hires, performance, 90/180-day retention, source diversity, team retention, and total hiring cost."
            ),
            evidence_needed=[
                "Role-specific hiring bottleneck and baseline",
                "Referral-to-qualified-to-hired conversion",
                "Standardized quality and performance measures",
                "Retention of referred and non-referred workers",
                "Network diversity and conflict-of-interest checks",
                "Bonus, vacancy, onboarding, and attrition cost",
            ],
            red_flags=[
                "Referral bypasses the normal assessment",
                "Bonus is paid on introduction rather than sustained hire",
                "Referral volume is the primary success metric",
                "Teams become dependent on one social network",
                "Non-referred staff effects are never measured",
            ],
            agent_lesson=(
                "The COO needs to recognize the incentive-quality trade-off and demand workforce outcome evidence; hiring specialists should own selection design and labour compliance."
            ),
            hard_gate_candidate=(
                "Employee referral programmes require equal assessment, delayed outcome metrics, conflict checks, and review of quality, retention, diversity, and total cost."
            ),
            retrieval_triggers=[
                "employee referral bonus", "hire through staff", "referral quality", "reduce employee attrition", "employee referral program ROI"
            ],
        ),
        ScenarioCard(
            slug="bk100-india-supply-chain-dependency-resilience",
            title="Supply resilience: distinguish irreplaceable relationships from replaceable capacity",
            source_type="event_study_firm_transaction_network",
            trust_level="high",
            domains=["manager_coo", "finance", "sales"],
            agent_targets=["manager_coo", "finance_agent", "sales_agent"],
            tags=["supply_chain", "india", "resilience", "supplier_risk", "business_continuity"],
            source_url="https://www.nber.org/papers/w30689",
            local_file="archives/business-knowledge/research/operations/nber_india_supply_chain_resilience_w30689.pdf",
            situation=(
                "A critical supplier, geography, logistics route, or input faces disruption and the business must decide whether to preserve the relationship, switch, dual-source, or redesign the offer."
            ),
            decision_pressure=(
                "Teams apply one rule—always diversify or always protect incumbents—even though substitutability, input complexity, qualification time, quality, and network position differ."
            ),
            mistake_or_risk=(
                "Do not infer that every concentrated relationship is fragile. Indian transaction evidence found difficult-to-replace complex links could be more persistent, while exposed firms also formed new links with larger, better-connected suppliers."
            ),
            recommended_next_action=(
                "Map tier-one dependencies by revenue/service impact, input complexity, concentration, geography, switching and qualification time, inventory cover, contractual rights, and supplier network strength; prequalify alternatives where feasible; preserve scarce relationship-specific knowledge; run disruption scenarios."
            ),
            evidence_needed=[
                "Product/customer impact of each critical input",
                "Supplier, geography, and route concentration",
                "Substitutability and qualification lead time",
                "Inventory cover and recovery-time objective",
                "Supplier financial/operational/network indicators",
                "Fallback economics and customer communication plan",
            ],
            red_flags=[
                "Single-source exposure is invisible below the direct supplier",
                "Backup supplier has never passed a real qualification order",
                "Cheapest supplier is chosen without recovery analysis",
                "Complex relationship knowledge is discarded during a temporary shock",
                "Sales promises dates without supply scenario evidence",
            ],
            agent_lesson=(
                "The Manager owns continuity choices; Finance quantifies cash and concentration exposure; Sales translates feasible recovery into customer commitments."
            ),
            hard_gate_candidate=(
                "Critical inputs require dependency mapping, tested alternatives or an explicit exception, recovery targets, and customer-impact scenarios."
            ),
            retrieval_triggers=[
                "supplier disruption India", "single supplier risk", "dual sourcing", "supply chain resilience", "supplier lockdown"
            ],
        ),
        ScenarioCard(
            slug="bk101-supplier-service-reliability-channel-demand",
            title="Channel sales: treat fill rate and service reliability as demand-generation variables",
            source_type="longitudinal_supplier_retailer_data_and_pilot",
            trust_level="medium",
            domains=["sales", "finance", "manager_coo"],
            agent_targets=["sales_agent", "finance_agent", "manager_coo"],
            tags=["channel_sales", "fill_rate", "supplier_service", "retailer_demand", "reliability"],
            source_url="https://www.hbs.edu/ris/download.aspx?name=11-034.pdf",
            local_file="archives/business-knowledge/research/hbs/11-034_95f981a4-388b-40f3-9751-fb654b05162e.pdf",
            situation=(
                "Retailers, distributors, or resellers reduce orders even though list price, sell-through opportunity, and seller activity appear competitive."
            ),
            decision_pressure=(
                "Sales reaches for discounts and incentives because service failures, partial fills, substitutions, claims, and recovery delays are recorded in operations rather than the account plan."
            ),
            mistake_or_risk=(
                "Do not copy the apparel study's estimated service-demand relationship as a universal elasticity or assume correlation proves every order decline was caused by fill rate."
            ),
            recommended_next_action=(
                "Join account orders with requested versus supplied quantity/date, stockouts, substitutions, claims, recovery time, sell-through, and margin; identify reliability-sensitive accounts/SKUs; repair root causes with Operations before funding a price concession; test whether restored service changes future orders."
            ),
            evidence_needed=[
                "Requested and fulfilled quantity/date by account and SKU",
                "Fill rate, stockouts, substitutions, and recovery time",
                "Retailer sell-through and inventory position",
                "Order trend before and after service changes",
                "Price, promotion, assortment, and season controls",
                "Contribution impact of service fix versus discount",
            ],
            red_flags=[
                "Discount is offered before service history is reviewed",
                "Aggregate fill rate hides priority-account failures",
                "Revenue credit ignores partial or late fulfilment",
                "Seller cannot see claims and stockouts",
                "Pilot result is presented as guaranteed causal uplift",
            ],
            agent_lesson=(
                "Sales diagnoses the account mechanism, Finance compares service-fix and discount economics, and the Manager resolves cross-functional ownership."
            ),
            hard_gate_candidate=(
                "Material channel discounts require account-level service reliability, sell-through, order trend, and contribution evidence."
            ),
            retrieval_triggers=[
                "distributor orders declining", "retailer not reordering", "fill rate sales impact", "channel service level", "discount or improve delivery"
            ],
        ),
        ScenarioCard(
            slug="bk102-trade-credit-liquidity-shock",
            title="Liquidity shock: manage supplier and customer credit as one connected system",
            source_type="quasi_experimental_liquidity_shock",
            trust_level="high",
            domains=["finance", "sales", "manager_coo"],
            agent_targets=["finance_agent", "sales_agent", "manager_coo"],
            tags=["trade_credit", "working_capital", "liquidity", "collections", "supplier_relationships"],
            source_url="https://www.nber.org/papers/w22286",
            local_file="archives/business-knowledge/research/finance/nber_trade_credit_liquidity_w22286.pdf",
            situation=(
                "A fraud, delayed receivable, bank interruption, or demand shock abruptly reduces cash and threatens payroll, suppliers, and customer fulfilment."
            ),
            decision_pressure=(
                "The business may stretch every supplier and freeze every customer account uniformly, transmitting distress to partners and damaging high-value relationships."
            ),
            mistake_or_risk=(
                "Do not treat supplier credit or faster customer collection as free cash. Changes can trigger supply holds, lost discounts, customer churn, bad debt, tax/accounting effects, and reputational damage."
            ),
            recommended_next_action=(
                "Build a 13-week cash and counterparty map; segment receivables and payables by collectability, criticality, concentration, terms, disputes, and relationship value; protect payroll/statutory and continuity-critical payments; negotiate rather than surprise; price new credit deliberately; monitor covenant and insolvency indicators."
            ),
            evidence_needed=[
                "13-week cash forecast with stress cases",
                "Receivables ageing, disputes, and probability/timing of collection",
                "Payables by criticality, terms, and supplier dependency",
                "Customer contribution, credit risk, and relationship value",
                "Financing facilities, covenants, and statutory priorities",
                "Counterparty communication and approval log",
            ],
            red_flags=[
                "All suppliers are delayed by the same number of days",
                "Sales extends credit without cash-cost approval",
                "Collections threaten disputed or strategic accounts blindly",
                "Statutory dues or payroll are omitted from the crisis view",
                "A temporary gap is funded by structurally unprofitable sales",
            ],
            agent_lesson=(
                "Finance controls the liquidity model, Sales manages customer-credit consequences, and the Manager arbitrates continuity and relationship trade-offs."
            ),
            hard_gate_candidate=(
                "Liquidity-crisis actions require a 13-week forecast, counterparty segmentation, protected obligations, named approvals, and daily variance review."
            ),
            retrieval_triggers=[
                "cash crunch", "delay supplier payment", "tighten customer credit", "trade credit liquidity", "13 week cash flow"
            ],
        ),
        ScenarioCard(
            slug="bk103-records-and-relationship-lending",
            title="SMB financing: combine lender-ready records with deliberate relationship evidence",
            source_type="empirical_small_business_banking_study",
            trust_level="medium",
            domains=["finance", "manager_coo"],
            agent_targets=["finance_agent", "manager_coo"],
            tags=["lending", "financial_records", "soft_information", "bank_relationship", "credit_readiness"],
            source_url="https://www.nber.org/papers/w8752",
            local_file="archives/business-knowledge/research/finance/nber_small_bank_soft_information_w8752.pdf",
            situation=(
                "A viable small business struggles to obtain or renew credit because statements are thin, cash flows are seasonal, or important operating strengths are not visible in standardized underwriting."
            ),
            decision_pressure=(
                "Owners may rely entirely on a personal banking relationship or, conversely, send raw financial files without explaining verified customers, contracts, collections, concentration, and operating controls."
            ),
            mistake_or_risk=(
                "Do not use 'soft information' to excuse poor books or assume the US large-versus-small-bank evidence maps directly to current Indian lenders and digital underwriting."
            ),
            recommended_next_action=(
                "Maintain a monthly lender pack—statements, GST/tax and bank reconciliations, ageing, cash forecast, debt schedule, concentration, and covenant view—plus verifiable operating evidence and downside actions; build contact before a crisis with lenders suited to the requirement; compare total terms and dependencies."
            ),
            evidence_needed=[
                "Reconciled financial, tax, and bank records",
                "Cash-flow forecast and debt-service capacity",
                "Receivable/payable ageing and concentration",
                "Contracts, orders, retention, and operating controls",
                "Collateral/guarantees, covenants, fees, and total cost",
                "Lender fit, relationship history, and alternatives",
            ],
            red_flags=[
                "Loan application begins only after cash is exhausted",
                "Relationship narrative conflicts with reconciled records",
                "Sanction amount is compared without covenants and total cost",
                "One lender becomes an unexamined single point of failure",
                "Owner guarantees are treated as administrative details",
            ],
            agent_lesson=(
                "Finance owns evidence quality and lender economics; the Manager ensures financing supports resilience and strategy rather than merely postponing an operating problem."
            ),
            hard_gate_candidate=(
                "Material borrowing requires reconciled records, a downside cash case, total-cost/covenant comparison, and at least one feasible alternative."
            ),
            retrieval_triggers=[
                "SMB loan rejected", "relationship banking", "lender pack", "bank wants more records", "small business credit"
            ],
        ),
        ScenarioCard(
            slug="bk104-payment-outage-fallback-and-reconciliation",
            title="Payment resilience: preserve alternate acceptance paths and rehearse reconciliation",
            source_type="payment_outage_event_studies_surveys_and_rct",
            trust_level="high",
            domains=["finance", "manager_coo", "sales"],
            agent_targets=["finance_agent", "manager_coo", "sales_agent"],
            tags=["payments", "business_continuity", "outage", "reconciliation", "customer_experience"],
            source_url="https://www.nber.org/papers/w35115",
            local_file="archives/business-knowledge/research/finance/nber_payment_resilience_w35115.pdf",
            situation=(
                "The primary payment rail, bank, acquirer, device, network, or settlement provider becomes unavailable during trading."
            ),
            decision_pressure=(
                "Staff improvise by sharing personal accounts, screenshots, handwritten credit, or duplicate payment links, creating fraud, privacy, tax, and reconciliation failures after service returns."
            ),
            mistake_or_risk=(
                "Do not prescribe cash universally or transfer the multi-country study's behaviour directly to India. Fallbacks must fit law, safety, customer needs, fraud controls, and the business's actual payment rails."
            ),
            recommended_next_action=(
                "Map payment and settlement dependencies; configure a genuinely independent alternate bank/acquirer/rail and a controlled offline or cash procedure where appropriate; publish staff thresholds and customer messages; record fallback transactions uniquely; rehearse outage, restoration, duplicate/refund, and settlement reconciliation."
            ),
            evidence_needed=[
                "Payment-to-settlement dependency map",
                "Independence and tested capacity of fallback methods",
                "Fraud, cash-safety, privacy, and regulatory controls",
                "Offline limits and staff decision rights",
                "Unique transaction log and customer receipt path",
                "Restoration, duplicate, refund, and settlement reconciliation test",
            ],
            red_flags=[
                "Fallback uses the same bank/network/provider dependency",
                "Staff accept screenshots as final payment proof",
                "Personal accounts are used for business collections",
                "Offline sales have no limit or identity/evidence rule",
                "No owner reconciles duplicates after recovery",
            ],
            agent_lesson=(
                "Finance designs controls and reconciliation, Sales protects the customer interaction, and the Manager owns continuity thresholds and provider concentration."
            ),
            hard_gate_candidate=(
                "Revenue-critical payment flows require an independently tested fallback, offline limits, customer evidence, and post-restoration reconciliation."
            ),
            retrieval_triggers=[
                "UPI down business", "payment gateway outage", "offline payment fallback", "cash backup", "duplicate payment reconciliation"
            ],
        ),
        ScenarioCard(
            slug="bk105-india-msme-cyber-minimum-control-stack",
            title="India MSME cyber baseline: prioritize the minimum control stack around critical services",
            source_type="official_indian_cyber_guidance_and_standard",
            trust_level="high",
            domains=["compliance", "manager_coo", "finance"],
            agent_targets=["compliance_agent", "manager_coo", "finance_agent"],
            tags=["cybersecurity", "india", "msme", "cert_in", "business_continuity"],
            source_url="https://www.cert-in.org.in/PDF/Elemental_Cyber_Defense_Controls_for_MSME.pdf",
            local_file="archives/business-knowledge/research/compliance/certin_15_msme_cyber_controls.pdf",
            situation=(
                "An Indian SMB relies on email, cloud tools, endpoints, vendors, customer data, payments, and backups but security work is an unowned collection of software purchases."
            ),
            decision_pressure=(
                "Limited capacity encourages either postponement or buying a complex tool before inventory, access, patching, backup, logging, vendor, and response basics are controlled."
            ),
            mistake_or_risk=(
                "Do not treat a checklist or certification claim as proof of resilience. Verify current CERT-In Directions, sector requirements, log retention/location, incident reporting, and applicable DPDP obligations before deterministic action."
            ),
            recommended_next_action=(
                "Map critical business services and assets; name an owner; close the highest-consequence gaps across licensed protection, MFA, secure configuration/patching, least privilege and offboarding, email/domain controls, logs, vendor due diligence, staff training, encrypted offline/offsite backups with restore tests, and annual vulnerability assessment."
            ),
            evidence_needed=[
                "Critical-service, asset, data, and dependency inventory",
                "Named owner and risk-ranked remediation plan",
                "MFA, patch, endpoint, email, and access evidence",
                "Quarterly access review and immediate offboarding test",
                "Backup isolation, restore result, and recovery targets",
                "Current CERT-In, sector, contractual, and privacy requirements",
            ],
            red_flags=[
                "Shared admin accounts or unlicensed/pirated software",
                "Departed users retain access",
                "Backup exists but restoration is untested",
                "Vendor access has no owner, expiry, or monitoring",
                "Compliance claim relies only on a tool dashboard",
            ],
            agent_lesson=(
                "Compliance owns the requirement map and evidence; technical specialists implement controls; Finance quantifies exposure; the Manager prioritizes continuity and accountable ownership."
            ),
            hard_gate_candidate=(
                "Critical services require an asset owner, MFA/least privilege, supported patching, tested isolated backups, logging, vendor controls, and current legal/sector verification."
            ),
            retrieval_triggers=[
                "cybersecurity checklist MSME India", "CERT-In controls", "small business ransomware", "MSME cyber baseline", "NIST small business"
            ],
        ),
        ScenarioCard(
            slug="bk106-cyber-incident-response-and-recovery",
            title="Cyber incident: contain, preserve evidence, report, recover, and reconcile business impact",
            source_type="official_indian_incident_guidance_and_standard",
            trust_level="high",
            domains=["compliance", "manager_coo", "finance"],
            agent_targets=["compliance_agent", "manager_coo", "finance_agent"],
            tags=["incident_response", "cert_in", "evidence", "recovery", "cyber_reporting"],
            source_url="https://www.cert-in.org.in/PDF/Elemental_Cyber_Defense_Controls_for_MSME.pdf",
            local_file="archives/business-knowledge/research/compliance/certin_15_msme_cyber_controls.pdf",
            situation=(
                "The business detects account takeover, ransomware, data exposure, suspicious payment instructions, service disruption, or vendor compromise."
            ),
            decision_pressure=(
                "Teams rush to delete evidence, restore from uncertain backups, communicate unverified facts, or delay escalation until the exact cause and legal classification are known."
            ),
            mistake_or_risk=(
                "Do not improvise reporting deadlines or assume every incident has identical obligations. Current CERT-In Directions, sector rules, contracts, law-enforcement needs, insurer terms, and privacy duties must be checked against the facts."
            ),
            recommended_next_action=(
                "Activate a named incident lead and out-of-band channel; preserve logs/evidence; contain compromised identities/devices without destroying forensic value; assess affected services/data/transactions; verify reporting and notification duties immediately; restore from trusted backups in priority order; reconcile payments/data; document decisions and corrective controls."
            ),
            evidence_needed=[
                "Detection time, source, affected identities/assets, and current containment",
                "Preserved logs, messages, payment and access evidence",
                "Critical service, data subject, customer, and counterparty impact",
                "Current reporting/notification deadlines and recipients",
                "Clean restore point and recovery verification",
                "Financial reconciliation, lessons, and control-owner actions",
            ],
            red_flags=[
                "Compromised email is used to coordinate response",
                "Systems are wiped before evidence is preserved",
                "Backups reconnect before compromise is understood",
                "Customer assurance is issued before facts are verified",
                "Reporting is postponed until root cause is certain",
            ],
            agent_lesson=(
                "Compliance determines mutable duties with qualified support; technical responders contain and recover; Finance reconciles loss; the Manager coordinates facts, priorities, and decisions."
            ),
            hard_gate_candidate=(
                "Cyber incidents require immediate named command, out-of-band communication, evidence preservation, current duty verification, clean recovery validation, and financial reconciliation."
            ),
            retrieval_triggers=[
                "company hacked what now", "CERT-In incident report", "ransomware response India", "data breach MSME", "restore after cyber attack"
            ],
        ),
        ScenarioCard(
            slug="bk107-bank-detail-change-and-invoice-fraud",
            title="Payment fraud: verify beneficiary and credit changes outside the requesting channel",
            source_type="official_incident_scenarios_and_reporting",
            trust_level="high",
            domains=["finance", "compliance", "sales", "manager_coo"],
            agent_targets=["finance_agent", "compliance_agent", "sales_agent", "manager_coo"],
            tags=["business_email_compromise", "invoice_fraud", "beneficiary_change", "dual_approval", "vendor_fraud"],
            source_url="https://www.ic3.gov/PSA/2023/PSA230324",
            local_file="archives/business-knowledge/research/compliance/ic3_vendor_invoice_fraud_2023.html",
            situation=(
                "A familiar vendor, customer, executive, employee, lawyer, or new account sends urgent bank-detail, beneficiary, purchase-order, credit-term, refund, or payment instructions."
            ),
            decision_pressure=(
                "The message looks authentic and urgency discourages independent verification; replying or calling details inside the same message keeps the attacker inside the control loop."
            ),
            mistake_or_risk=(
                "Do not rely on display name, thread history, attached letterhead, caller ID, invoice, or the requesting channel. IC3 cases are US incident intelligence, not Indian prevalence estimates."
            ),
            recommended_next_action=(
                "Freeze the change; compare exact domain and master data; independently call a previously verified number; require maker-checker approval; apply a cooling period and low test limit for changed beneficiaries; verify new customer POs and credit through the organization's main contact; rehearse rapid bank recall, evidence preservation, and India-specific reporting escalation."
            ),
            evidence_needed=[
                "Original message headers/domain and change request",
                "Previously verified contact and independent callback record",
                "Master-data change and maker-checker approvals",
                "Beneficiary age, test payment, limit, and cooling period",
                "PO/customer identity and credit verification",
                "Bank recall, incident, legal, insurer, and regulator escalation path",
            ],
            red_flags=[
                "Secrecy or urgency from a senior-looking sender",
                "Slightly altered domain or reply-to address",
                "Bank change immediately before a large payment",
                "Verification uses a number supplied in the same request",
                "New customer seeks large Net-30/60 goods before independent verification",
            ],
            agent_lesson=(
                "Finance controls beneficiary and payment changes; Sales verifies customer identity and credit; Compliance owns incident escalation; the Manager supports controls even under executive urgency."
            ),
            hard_gate_candidate=(
                "Bank, beneficiary, refund, PO, and material credit changes require independent known-channel verification, dual approval, and logged evidence."
            ),
            retrieval_triggers=[
                "vendor changed bank account", "CEO urgent payment email", "invoice redirection fraud", "fake purchase order", "business email compromise"
            ],
        ),
        ScenarioCard(
            slug="bk108-india-competition-contact-and-bid-protocol",
            title="Competition compliance: control competitor contacts, bids, pricing information, and association meetings",
            source_type="official_indian_regulator_guidance",
            trust_level="high",
            domains=["compliance", "sales", "marketing", "manager_coo"],
            agent_targets=["compliance_agent", "sales_agent", "marketing_agent", "manager_coo"],
            tags=["competition_law", "india", "bid_rigging", "trade_association", "pricing"],
            source_url="https://www.cci.gov.in/images/publications_booklet/en/competition-advocacy-booklet-2026-english1779277830.pdf",
            local_file="archives/business-knowledge/research/compliance/cci_competition_advocacy_2026.pdf",
            situation=(
                "Sales, Marketing, Procurement, or leadership meets competitors through tenders, associations, distributors, joint initiatives, benchmarking, hiring, or informal industry conversations."
            ),
            decision_pressure=(
                "Commercially useful discussion can drift into current/future price, customers, territories, output, bid intentions, sensitive costs, boycotts, or coordinated conduct before anyone recognizes the risk."
            ),
            mistake_or_risk=(
                "Do not make legal conclusions from a card. Indian competition analysis is fact-specific and mutable; vertical restrictions, dominance, joint work, public information, and legitimate associations can require qualified current advice."
            ),
            recommended_next_action=(
                "Define prohibited/sensitive topics and an exit/escalation script; preapprove agenda and attendees for competitor/association meetings; keep accurate minutes; never share bid intentions or competitively sensitive non-public data without approved legal structure; maintain an agreement register; review pricing, distribution, exclusivity, tying, and tender practices with current counsel."
            ),
            evidence_needed=[
                "Participants, purpose, agenda, and legal/business rationale",
                "Information requested, source, sensitivity, and aggregation",
                "Meeting minutes and any objection/exit record",
                "Independent bid and pricing decision evidence",
                "Current agreements and commercial restrictions register",
                "Current CCI/legal advice for the actual facts",
            ],
            red_flags=[
                "Competitor asks what price or customer the business will pursue",
                "Trade association circulates future pricing or output intentions",
                "Bidders discuss who should win or abstain",
                "Sensitive discussion is moved off-record",
                "Commercial agreement has no owner or competition review",
            ],
            agent_lesson=(
                "Compliance owns specialist legal escalation and programme design; commercial agents recognize and stop risky conversations; the Manager reinforces independent conduct and record discipline."
            ),
            hard_gate_candidate=(
                "Competitor, tender, association, and sensitive-data interactions require approved purpose, topic limits, records, an exit protocol, and current legal escalation."
            ),
            retrieval_triggers=[
                "competitor asked our price", "bid coordination", "trade association meeting", "CCI compliance program", "exclusive distribution competition law India"
            ],
        ),
        ScenarioCard(
            slug="bk109-india-smb-survey-benchmark-discipline",
            title="Indian SMB benchmarking: match the survey universe and weights before using a statistic",
            source_type="official_weighted_business_survey_and_metadata",
            trust_level="high",
            domains=["manager_coo", "finance", "marketing", "sales"],
            agent_targets=["manager_coo", "finance_agent", "marketing_agent", "sales_agent"],
            tags=["india", "smb", "benchmarking", "world_bank", "survey_weights"],
            source_url="https://microdata.worldbank.org/catalog/6495",
            local_file="archives/business-knowledge/research/datasets/ddi-documentation-english_microdata-6495.pdf",
            situation=(
                "An agent uses an Indian business statistic to set a target, diagnose a client, size a problem, compare finance access, or justify a sales/marketing recommendation."
            ),
            decision_pressure=(
                "A credible publisher and a precise percentage make the number look universal even when the sample covers a specific firm size, registration status, city set, sector, period, and weighted survey design."
            ),
            mistake_or_risk=(
                "Do not treat raw case counts as population prevalence or combine the Micro Enterprise Survey with the broader Enterprise Survey as if their universes and questionnaires were identical."
            ),
            recommended_next_action=(
                "Record the exact variable/question, universe, fieldwork and fiscal period, firm-size and registration definition, cities/regions, sector, missing-value codes, stratification, and weight; calculate weighted estimates with uncertainty where microdata access permits; state comparability gaps; use the benchmark as context, not a diagnosis."
            ),
            evidence_needed=[
                "Exact questionnaire item and response coding",
                "Target population, registration and employee-size definition",
                "Cities/regions, sector coverage, and reference period",
                "Sampling strata, weights, missing values, and uncertainty",
                "Client metric definition and comparability gaps",
                "Microdata access/licence or published table provenance",
            ],
            red_flags=[
                "Sample percentage is reported without applying weights",
                "Nine-city micro-firm result is called all-India SMB prevalence",
                "Different fiscal periods or variable definitions are compared",
                "Correlation is presented as a recommended causal intervention",
                "Restricted or registered microdata is obtained outside its licence process",
            ],
            agent_lesson=(
                "Specialists own domain interpretation; the Manager asks whether the comparison population, definition, period, and decision mechanism actually match the business."
            ),
            hard_gate_candidate=(
                "Business benchmarks used in recommendations require variable provenance, matched universe and period, survey weights where applicable, uncertainty, and explicit comparability limits."
            ),
            retrieval_triggers=[
                "India small business benchmark", "World Bank enterprise survey India", "micro enterprise statistics", "compare SMB performance", "survey weights business"
            ],
        ),
    ]
)

# Quality audit 2026-07-26. These records remain traceable in the archived
# source material but are deliberately excluded from RAG because a stronger
# surviving card now contains their distinct, useful conditions.
RETIRED_DUPLICATE_SLUGS = {
    "bk003-bad-debt-credit-note-wrong-tool",  # merged into BK004
    "bk022-digital-marketing-amplifies-business-model",  # superseded by BK089
    "bk023-low-ticket-local-business-paid-ads-vs-local-trust",  # merged into BK016/BK089
    "bk069-resource-allocation-inertia-review",  # duplicate source/action; merged into BK056
}
SCENARIOS = [card for card in SCENARIOS if card.slug not in RETIRED_DUPLICATE_SLUGS]

# This round is kept in a separate plain-data module so historical cases remain
# auditable without making this already-large generator harder to review.
SCENARIOS.extend(ScenarioCard(**card) for card in HISTORICAL_BUSINESS_CASES)


def slug_to_filename(index: int, slug: str) -> str:
    return f"{index:03d}-{slug}.md"


def yaml_list(values: list[str]) -> str:
    return "[" + ", ".join(values) + "]"


def markdown_body(card: ScenarioCard) -> str:
    evidence = "\n".join(f"- {item}" for item in card.evidence_needed)
    red_flags = "\n".join(f"- {item}" for item in card.red_flags)
    triggers = "\n".join(f"- {item}" for item in card.retrieval_triggers)
    agents = ", ".join(card.agent_targets)
    domains = ", ".join(card.domains)
    sections = [
        "*Executional scenario — extracted from archived public sources for RAG grounding. Validate mutable law/platform policy before deterministic action.*",
        "## Applies to",
        f"Agents: {agents}",
        f"Domains: {domains}",
        f"Source type: {card.source_type}",
        f"Trust level: {card.trust_level}",
        "## Situation",
        card.situation,
        "## Decision pressure",
        card.decision_pressure,
        "## Mistake or risk",
        card.mistake_or_risk,
        "## Recommended next action",
        card.recommended_next_action,
        "## Evidence needed",
        evidence,
        "## Red flags / escalation triggers",
        red_flags,
        "## Agent lesson",
        card.agent_lesson,
        "## Hard-gate candidate",
        card.hard_gate_candidate,
        "## Retrieval triggers",
        triggers,
        "## Provenance",
        f"Source URL: {card.source_url}",
        f"Local source file: `{card.local_file}`",
        f"Retrieved at: {RETRIEVED_AT}",
    ]
    return "\n\n".join(sections).strip() + "\n"


def markdown_doc(card: ScenarioCard) -> str:
    frontmatter = dedent(
        f"""\
        ---
        title: "{card.title}"
        authored_by: codex_public_source_extraction
        tags: {yaml_list(card.tags)}
        priority: 4
        version: 1
        ---
        """
    )
    return frontmatter + "\n" + markdown_body(card)


def as_json(card: ScenarioCard) -> dict[str, object]:
    return {
        "id": card.slug,
        "title": card.title,
        "retrieved_at": RETRIEVED_AT,
        "source_type": card.source_type,
        "trust_level": card.trust_level,
        "domains": card.domains,
        "agent_targets": card.agent_targets,
        "tags": card.tags,
        "situation": card.situation,
        "decision_pressure": card.decision_pressure,
        "mistake_or_risk": card.mistake_or_risk,
        "evidence_needed": card.evidence_needed,
        "recommended_next_action": card.recommended_next_action,
        "red_flags": card.red_flags,
        "agent_lesson": card.agent_lesson,
        "hard_gate_candidate": card.hard_gate_candidate,
        "retrieval_triggers": card.retrieval_triggers,
        "source_url": card.source_url,
        "local_file": card.local_file,
        "l4_markdown_file": f"archives/business-knowledge/extracted/l4_skill_corpus/{slug_to_filename(SCENARIOS.index(card) + 1, card.slug)}",
    }


def as_manager_decision_json(card: ScenarioCard) -> dict[str, object]:
    """Return the COO-facing view of a card.

    The Manager is not supposed to hold deep Sales/Marketing/Compliance/Finance
    expertise. Specialist agents own that. The Manager needs enough judgment to
    spot risk, decide whether an action benefits the business, ask the right
    specialist for detail, and stop decisions that would hurt cashflow, trust,
    compliance, or execution.
    """

    specialist_agents = [agent for agent in card.agent_targets if agent != "manager_coo"]
    return {
        "id": card.slug,
        "title": card.title,
        "retrieved_at": RETRIEVED_AT,
        "domains": card.domains,
        "specialist_agents": specialist_agents,
        "source_type": card.source_type,
        "trust_level": card.trust_level,
        "situation": card.situation,
        "decision_pressure": card.decision_pressure,
        "business_risk": card.mistake_or_risk,
        "manager_action": card.recommended_next_action,
        "red_flags": card.red_flags,
        "decision_lesson": card.agent_lesson,
        "when_to_escalate_or_gate": card.hard_gate_candidate,
        "retrieval_triggers": card.retrieval_triggers,
        "source_url": card.source_url,
        "local_file": card.local_file,
        "specialist_rag_file": f"archives/business-knowledge/extracted/l4_skill_corpus/{slug_to_filename(SCENARIOS.index(card) + 1, card.slug)}",
    }


def validate_markdown_shape(text: str, filename: str) -> None:
    if not text.startswith("---\n"):
        raise ValueError(f"{filename}: missing YAML frontmatter start")
    parts = text.split("---", 2)
    if len(parts) != 3:
        raise ValueError(f"{filename}: malformed frontmatter")
    fm, body = parts[1], parts[2].strip()
    for required in ("title:", "authored_by:", "tags:", "priority:", "version:"):
        if required not in fm:
            raise ValueError(f"{filename}: missing {required}")
    if "## Provenance" not in body or "## Retrieval triggers" not in body:
        raise ValueError(f"{filename}: missing required body sections")


def validate_cards(cards: list[ScenarioCard]) -> None:
    slugs = [c.slug for c in cards]
    if len(slugs) != len(set(slugs)):
        raise ValueError("duplicate scenario slug")
    for c in cards:
        if not re.fullmatch(r"bk\d{3}-[a-z0-9-]+", c.slug):
            raise ValueError(f"{c.slug}: slug must be bkNNN-kebab")
        if not c.agent_targets:
            raise ValueError(f"{c.slug}: missing agent targets")
        if not c.retrieval_triggers:
            raise ValueError(f"{c.slug}: missing retrieval triggers")
        if not Path(REPO_ROOT / c.local_file).exists():
            raise ValueError(f"{c.slug}: local source file not found: {c.local_file}")


def write_outputs(*, check_only: bool = False) -> dict[str, int]:
    validate_cards(SCENARIOS)
    rendered = []
    for idx, card in enumerate(SCENARIOS, start=1):
        filename = slug_to_filename(idx, card.slug)
        text = markdown_doc(card)
        validate_markdown_shape(text, filename)
        rendered.append((filename, text, card))

    if check_only:
        agent_count = len({agent for card in SCENARIOS for agent in card.agent_targets})
        return {
            "agent_indexes": agent_count,
            "cards": len(rendered),
            "jsonl_rows": len(rendered),
            "markdown_files": len(rendered),
        }

    L4_OUT.mkdir(parents=True, exist_ok=True)
    JSONL_OUT.parent.mkdir(parents=True, exist_ok=True)
    AGENT_INDEX_OUT.mkdir(parents=True, exist_ok=True)

    # Card order affects the numeric filename prefix. Retiring or inserting a
    # card therefore makes prior generated filenames stale. Remove only files
    # that match this generator's exact card pattern; preserve README and any
    # manually maintained material in the directory.
    expected_filenames = {filename for filename, _text, _card in rendered}
    generated_pattern = re.compile(r"\d{3}-bk\d{3}-[a-z0-9-]+\.md")
    for existing in L4_OUT.glob("*.md"):
        if generated_pattern.fullmatch(existing.name) and existing.name not in expected_filenames:
            existing.unlink()

    for filename, text, _card in rendered:
        (L4_OUT / filename).write_text(text, encoding="utf-8")

    with JSONL_OUT.open("w", encoding="utf-8") as fh:
        for _filename, _text, card in rendered:
            fh.write(json.dumps(as_json(card), ensure_ascii=False, sort_keys=True) + "\n")

    cards_by_agent: dict[str, list[dict[str, object]]] = {}
    for _filename, _text, card in rendered:
        row = as_json(card)
        for agent in card.agent_targets:
            if agent == "manager_coo":
                cards_by_agent.setdefault(agent, []).append(as_manager_decision_json(card))
            else:
                cards_by_agent.setdefault(agent, []).append(row)

    for agent, rows in sorted(cards_by_agent.items()):
        (AGENT_INDEX_OUT / f"{agent}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    index_readme = dedent(
        """\
        # Agent-specific scenario indexes

        Generated by `scripts/business_knowledge/extract_executional_scenarios.py`.

        Each JSON file contains the subset of executional scenario cards applicable
        to that role. Specialist-agent indexes contain full domain scenario cards.
        The `manager_coo.json` index is intentionally different: it is a decision
        lens, not a deep expertise corpus. It keeps situation, risk, red flags,
        manager action, and escalation/gate fields so the Manager can judge
        right/wrong and business benefit, while asking Sales/Marketing/Finance/
        Compliance specialists for domain execution.

        The Markdown corpus remains the L4/RAG source of text; these indexes are
        for filtering, eval construction, or a later ingestion job that wants to
        seed only one agent's knowledge slice.
        """
    )
    (AGENT_INDEX_OUT / "README.md").write_text(index_readme, encoding="utf-8")

    readme = dedent(
        f"""\
        # Extracted executional L4 corpus

        Generated by `scripts/business_knowledge/extract_executional_scenarios.py`.

        These Markdown files are compatible with the current L4 RAG loader shape:
        YAML frontmatter with `title`, `authored_by`, `tags`, `priority`, `version`,
        followed by a retrieval-ready body.

        They intentionally live outside `apps/team-orchestrator/skill_corpus` because
        that directory is a locked Fazal-authored corpus with exact-count/taxonomy
        tests. To seed these into `l4_documents`, either run a later loader against
        this directory or copy selected reviewed docs into the production corpus after
        updating the corpus tests and tag policy.

        Generated rows: {len(rendered)}

        JSONL mirror: `archives/business-knowledge/extracted/scenario_cards/executional_scenarios.jsonl`
        """
    )
    (L4_OUT / "README.md").write_text(readme, encoding="utf-8")

    return {
        "agent_indexes": len(cards_by_agent),
        "cards": len(rendered),
        "jsonl_rows": len(rendered),
        "markdown_files": len(rendered),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Validate definitions without writing files.")
    args = parser.parse_args()
    result = write_outputs(check_only=args.check)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
