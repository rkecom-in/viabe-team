#!/usr/bin/env python3
"""Run acquired VT-723 sources through VT-710 and emit governed hunt artifacts.

Raw HTML/PDF files are local-only inputs under archives/. Tracked outputs contain governed source
metadata, independently authored candidates, evidence locators, and disposition changes. There is
no network, database, embedding, or deployed-environment access in this builder.

Run with the orchestrator environment (from ``apps/team-orchestrator``)::

    uv run python ../../scripts/business_knowledge/build_o8_t4_corroboration.py
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "apps" / "team-orchestrator"
SRC = APP_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator.knowledge.contracts import (  # noqa: E402
    Applicability,
    ClaimValueType,
    EvidenceAuthority,
    KnowledgeDomain,
    SourceClass,
    TypedClaimValue,
    UsageRights,
    UsageRightsStatus,
    suggested_confidence_for_source,
)
from orchestrator.knowledge.ingestion import (  # noqa: E402
    AcquiredContentKind,
    AcquiredSource,
    CandidateGovernance,
    EmbeddingMode,
    ExtractedClaimDraft,
    InMemoryCandidateRegistry,
    InMemoryDedupeStore,
    IngestionPipeline,
    MappingRightsResolver,
    QuarantineRecord,
    SourceRightsDecision,
)

ARCHIVE = REPO_ROOT / "archives/business-knowledge/research/vt723-t4-corroboration"
CORPUS = APP_ROOT / "knowledge_corpus"
MANIFEST_OUT = CORPUS / "t4_corroboration_sources.jsonl"
CANDIDATES_OUT = CORPUS / "t4_corroboration_candidates.jsonl"
DELTA_OUT = CORPUS / "t4_corroboration_delta.jsonl"
REPORT_OUT = CORPUS / "T4_CORROBORATION_REPORT.md"
ACQUIRED_AT = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
RIGHTS_REVIEW = {
    "status": "unknown",
    "license_id": None,
    "terms_url": None,
    "allows_extraction": False,
    "allows_embedding": False,
    "allows_retrieval": False,
    "reviewed_at": "2026-08-05T12:00:00Z",
    "reviewed_by": "codex:vt723-source-governance",
}


def support(
    legacy_id: str,
    locator: str,
    finding: str,
    stance: str = "corroborates",
    qualifies: bool = True,
) -> dict[str, Any]:
    return {
        "legacy_id": legacy_id,
        "locator": locator,
        "finding": finding,
        "stance": stance,
        "qualifies_for_threshold": qualifies,
    }


def source(
    filename: str,
    url: str,
    title: str,
    publisher: str,
    source_class: str,
    domain: str,
    cluster: str,
    claim: str,
    action: str,
    supports: list[dict[str, Any]],
    *,
    jurisdiction: str | None = None,
    effective_from: str | None = None,
) -> dict[str, Any]:
    return {
        "filename": filename,
        "url": url,
        "title": title,
        "publisher": publisher,
        "source_class": source_class,
        "domain": domain,
        "cluster": cluster,
        "claim": claim,
        "action": action,
        "supports": supports,
        "jurisdiction": jurisdiction,
        "effective_from": effective_from,
    }


B1 = "bk001-gst-credit-note-notice-evidence-pack"
B2 = "bk002-gstr2b-itc-late-supplier-cashflow"
B4 = "bk004-output-gst-paid-customer-default"
B5 = "bk005-msme-delayed-payment-interest-and-relationship-pressure"
B6 = "bk006-msefc-notice-response-playbook"
B18 = "bk018-corporate-gifting-b2b-outbound-pipeline"
B19 = "bk019-bulky-product-shipping-unit-economics-local-first"
B20 = "bk020-community-listening-for-demand-discovery"
B24 = "bk024-first-ten-customers-founder-led-dream-list"
B25 = "bk025-high-trust-b2b-free-diagnostic-to-paid-pilot"
B26 = "bk026-stage-and-acv-decide-channel-migration"
B27 = "bk027-niche-community-launch-works-when-you-belong"
B28 = "bk028-comment-sample-loop-for-service-demand"
B29 = "bk029-campaign-success-operating-system"
B31 = "bk031-partner-network-co-selling-borrows-trust"
B32 = "bk032-brand-building-memory-proof-and-promise"
B33 = "bk033-presell-commitments-before-build-or-scale"
B113 = "bk113-good-glamm-integration-capacity"

TARGET_LEGACY_IDS = frozenset(
    {
        B1,
        B2,
        B4,
        B5,
        B6,
        B18,
        B19,
        B20,
        B24,
        B25,
        B26,
        B27,
        B28,
        B29,
        B31,
        B32,
        B33,
        B113,
    }
)


SOURCES = [
    source(
        "cbic-cgst-act.html",
        "https://cbic-gst.gov.in/hindi/cgst-act.html",
        "Central Goods and Services Tax Act, 2017",
        "CBIC",
        "t1",
        "compliance",
        "instrument:cgst-act-2017",
        "GST adjustment and recovery decisions must follow the statutory grounds, time limits, and records required by the CGST Act.",
        "Classify the statutory ground before changing tax treatment, and preserve invoice-level records for the position taken.",
        [
            support(
                B1,
                "Sections 34 and 35",
                "Credit-note adjustment is conditional and registered persons must maintain prescribed records.",
            ),
            support(
                B4,
                "Section 34",
                "The credit-note mechanism is tied to specified supply changes, not customer non-payment by itself.",
            ),
        ],
        jurisdiction="IN",
        effective_from="2017-07-01T00:00:00Z",
    ),
    source(
        "cbic-accounts-records-rules.html",
        "https://cbic-gst.gov.in/hindi/CGST-rules.html",
        "CGST Rules: Accounts and Records",
        "CBIC",
        "t1",
        "compliance",
        "instrument:cgst-accounts-records-rules-2017",
        "GST records must link tax documents, returns, and supporting business records in an auditable trail.",
        "Reconcile the notice issue invoice by invoice and retain the linked documentary trail used in the response.",
        [
            support(
                B1,
                "Rules 56 and 57",
                "The rules prescribe detailed accounts, document, and electronic-record retention duties.",
            )
        ],
        jurisdiction="IN",
        effective_from="2017-07-01T00:00:00Z",
    ),
    source(
        "gstn-gstr2b-advisory.pdf",
        "https://tutorial.gst.gov.in/downloads/news/updated_advisory_gstr_2b_12_10_2020.pdf",
        "Advisory on GSTR-2B",
        "GSTN",
        "t1",
        "compliance",
        "instrument:gstn-gstr2b-advisory-2020",
        "GSTR-2B is a static supplier-filing-derived statement that recipients should reconcile with their own records.",
        "Use the statement as a reconciliation control and follow up mismatches rather than treating it as a substitute for the purchase register.",
        [
            support(
                B2,
                "Paragraphs 2, 3, and 12",
                "GSTR-2B is generated from supplier filings, available on a set cycle, and should be reconciled with records.",
            )
        ],
        jurisdiction="IN",
        effective_from="2020-08-14T00:00:00Z",
    ),
    source(
        "gstn-ims-advisory.pdf",
        "https://tutorial.gst.gov.in/downloads/news/ims_advisory.pdf",
        "Invoice Management System Advisory",
        "GSTN",
        "t1",
        "compliance",
        "instrument:gstn-ims-advisory-2024",
        "Recipient action on supplier invoices creates an explicit loop for accepting, rejecting, or holding mismatched tax documents.",
        "Create a recurring supplier-exception workflow and preserve the recipient action trail before the return cycle closes.",
        [
            support(
                B2,
                "Paragraphs 1-6",
                "IMS manages invoice corrections and recipient actions that feed GSTR-2B.",
            )
        ],
        jurisdiction="IN",
        effective_from="2024-10-01T00:00:00Z",
    ),
    source(
        "cbic-circular-92-11-2019.pdf",
        "https://cbic-gst.gov.in/pdf/circular-cgst-92.pdf",
        "Circular No. 92/11/2019-GST",
        "CBIC",
        "t1",
        "finance",
        "instrument:cbic-circular-92-11-2019",
        "Post-supply commercial adjustments and GST credit notes have different statutory consequences.",
        "Separate commercial settlement from tax adjustment and obtain tax review before reducing output liability.",
        [
            support(
                B4,
                "Paragraphs C1-C2 and D3",
                "The circular distinguishes financial or commercial credit notes from GST credit notes and their tax effects.",
            )
        ],
        jurisdiction="IN",
        effective_from="2019-03-07T00:00:00Z",
    ),
    source(
        "msme-samadhaan.html",
        "https://ramp.msme.gov.in/ramp/msme-samadhaan.php",
        "MSME Samadhaan delayed-payment mechanism",
        "Ministry of MSME",
        "t1",
        "finance",
        "instrument:msmed-act-delayed-payment-framework",
        "Delayed MSE payments carry a statutory dispute and interest framework rather than being only an accounts-payable choice.",
        "Track MSE invoice acceptance and aging, respond invoice by invoice, and use the statutory forum only with a complete factual record.",
        [
            support(
                B5,
                "Portal explanation of MSMED Act sections 15-24",
                "The portal identifies delayed-payment duties, interest exposure, and MSEFC recourse.",
            ),
            support(
                B6,
                "Portal explanation of MSEFC sections 20-21",
                "The portal identifies the council process and eligible micro or small enterprise claims.",
            ),
        ],
        jurisdiction="IN",
        effective_from="2006-10-02T00:00:00Z",
    ),
    source(
        "msme-odr-guidelines.pdf",
        "https://ramp.msme.gov.in/ramp/pdf-guideline/Guidelines%20for%20MSE%20ODR.pdf",
        "Guidelines for MSE ODR Scheme",
        "Ministry of MSME",
        "t1",
        "finance",
        "instrument:mse-odr-scheme-2023-27",
        "The MSE ODR scheme provides a structured online route for delayed-payment dispute handling during its stated scheme period.",
        "Prepare the invoice history, dispute position, requested remedy, and proof before entering the ODR workflow.",
        [
            support(
                B5,
                "Sections 4-8",
                "The scheme operationalizes delayed-payment resolution and assistance.",
            ),
            support(
                B6,
                "Sections 9-12",
                "The guidelines describe application, scrutiny, mediation, and arbitration stages.",
            ),
        ],
        jurisdiction="IN",
        effective_from="2023-04-01T00:00:00Z",
    ),
    source(
        "salesforce-account-based-marketing.html",
        "https://www.salesforce.com/blog/b2b-account-based-marketing/",
        "Account-Based Marketing for B2B",
        "Salesforce",
        "t3",
        "sales",
        "practice:salesforce-abm",
        "Account-based selling coordinates a defined account list, relevant stakeholders, tailored value, and measured engagement.",
        "Select accounts deliberately, map buyer roles, tailor proof, and review account progression across touches.",
        [
            support(
                B18,
                "Sections on target accounts, personalization, and alignment",
                "ABM focuses resources on selected accounts and tailored engagement.",
            )
        ],
    ),
    source(
        "hubspot-target-accounts.html",
        "https://blog.hubspot.com/sales/how-to-choose-target-accounts-account-based-marketing",
        "How to Choose Target Accounts for ABM",
        "HubSpot",
        "t3",
        "sales",
        "practice:hubspot-target-account-selection",
        "An ABM motion starts by defining fit and choosing named accounts rather than broadcasting the same pitch.",
        "Use explicit fit criteria, stakeholder mapping, and account-level follow-up measures.",
        [
            support(
                B18,
                "Target-account selection framework",
                "The framework corroborates named-account selection and role-aware outreach.",
            )
        ],
    ),
    source(
        "sba-break-even.html",
        "https://www.sba.gov/business-guide/plan-your-business/calculate-your-startup-costs/break-even-point",
        "Break-even point calculation",
        "U.S. Small Business Administration",
        "t3",
        "finance",
        "practice:sba-break-even-analysis",
        "A channel or geography should be tested against contribution margin and break-even volume before expansion.",
        "Include packaging, fulfillment, and channel-specific variable cost in the break-even test.",
        [
            support(
                B19,
                "Break-even formula and decision uses",
                "The guide treats break-even analysis as a pricing, target, and decision control.",
            )
        ],
    ),
    source(
        "usps-parcel-size-weight.html",
        "https://faq.usps.com/articles/Knowledge/Parcel-Size-Weight-Fee-Standards/",
        "Parcel size, weight and fee standards",
        "USPS",
        "t1v",
        "operations",
        "policy:usps-dimensional-weight",
        "Parcel dimensions can change chargeable weight and therefore route-level unit economics.",
        "Measure the packed parcel and price each shipping lane using the carrier dimensional rules before promising national delivery.",
        [
            support(
                B19,
                "Dimensional weight and oversized parcel standards",
                "Carrier pricing explicitly depends on dimensions as well as physical weight.",
            )
        ],
    ),
    source(
        "arxiv-needmining-2101.06146.pdf",
        "https://arxiv.org/abs/2101.06146",
        "Needmining: Designing Digital Support to Elicit Needs from Social Media",
        "arXiv authors",
        "t2",
        "marketing",
        "study:needmining-2101.06146",
        "Public online language can be systematically mined to identify customer needs and unmet-demand signals.",
        "Code recurring needs and complaints before converting them into messaging or offer hypotheses.",
        [
            support(
                B20,
                "Abstract and method",
                "The research operationalizes detection and classification of customer needs in public posts.",
            )
        ],
    ),
    source(
        "nber-reviews-quality-w34934.pdf",
        "https://www.nber.org/papers/w34934",
        "From Complaint to Action: Technology-Enabled Quality Improvement from Consumer Reviews",
        "NBER",
        "t2",
        "marketing",
        "study:nber-w34934",
        "Structured review monitoring can reveal quality problems and guide operating responses.",
        "Treat recurring review themes as evidence to investigate and close with product or service changes.",
        [
            support(
                B20,
                "Abstract and empirical findings",
                "The study links online review information with product-quality monitoring and response.",
            )
        ],
    ),
    source(
        "nber-sampling-bias-w28882.pdf",
        "https://www.nber.org/papers/w28882",
        "Sampling Bias in Entrepreneurial Experiments",
        "NBER",
        "t2",
        "sales",
        "study:nber-w28882",
        "Early evidence from a narrow convenience sample can mislead product and market decisions.",
        "Name target accounts and treat early conversations as diagnostic evidence, not proof of broad demand.",
        [
            support(
                B24,
                "Abstract and sampling-bias results",
                "The study shows why who is sampled changes inference from early market evidence.",
            )
        ],
    ),
    source(
        "nber-experimentation-startup-w26278.pdf",
        "https://www.nber.org/papers/w26278",
        "Experimentation and Startup Performance: Evidence from A/B Testing",
        "NBER",
        "t2",
        "management",
        "study:nber-w26278",
        "Structured experimentation helps young firms learn before committing scarce resources.",
        "Run founder-led tests with explicit hypotheses and update the offer from observed behavior.",
        [
            support(
                B24,
                "Abstract and results",
                "Startup experimentation is associated with better learning and performance decisions.",
            )
        ],
    ),
    source(
        "hbs-customer-discovery-basics.pdf",
        "https://www.hbs.edu/ris/Publication%20Files/customer-discovery-basics.pdf",
        "Customer Discovery Basics",
        "Harvard Business School",
        "t3",
        "sales",
        "practice:hbs-customer-discovery",
        "Customer discovery requires bounded learning conversations and explicit assumptions before scaling a sales motion.",
        "Define what must be learned, interview the relevant buyer, and document the decision triggered by the evidence.",
        [
            support(
                B25,
                "Customer discovery process",
                "Supports diagnosis and bounded learning, but not the exact free-diagnostic-to-paid-pilot sequence.",
                "partial",
                False,
            )
        ],
    ),
    source(
        "nber-field-experiments-marketing.pdf",
        "https://www.nber.org/system/files/working_papers/w15992/w15992.pdf",
        "Field Experiments in Marketing",
        "NBER",
        "t2",
        "marketing",
        "study:nber-w15992",
        "Field experiments can isolate whether a commercial intervention changes customer behavior.",
        "Predefine treatment, outcome, and decision rule for a bounded commercial test.",
        [
            support(
                B25,
                "Experiment-design discussion",
                "Supports bounded proof and measurement, but not the exact diagnostic-to-paid-pilot tactic.",
                "partial",
                False,
            )
        ],
    ),
    source(
        "salesforce-b2b-omnichannel.html",
        "https://www.salesforce.com/commerce/b2b-omnichannel-guide/",
        "B2B Omnichannel Guide",
        "Salesforce",
        "t3",
        "sales",
        "practice:salesforce-b2b-omnichannel",
        "B2B buyers use multiple channels and the commercial motion must coordinate them around buyer needs.",
        "Choose channel roles by buying complexity and connect signals across the journey.",
        [
            support(
                B26,
                "B2B omnichannel sections",
                "The guide corroborates differentiated but coordinated channel roles in B2B.",
            )
        ],
    ),
    source(
        "bain-go-to-market.html",
        "https://www.bain.com/consulting-services/customer-strategy-and-marketing/go-to-market-strategy/",
        "Go-to-Market Strategy",
        "Bain & Company",
        "t3",
        "sales",
        "practice:bain-gtm",
        "Go-to-market design should align segments, channels, coverage, and economics rather than copy one motion across stages.",
        "Revisit coverage and channel design as customer economics and the offer mature.",
        [
            support(
                B26,
                "Go-to-market model description",
                "The framework corroborates matching coverage and channels to segment economics.",
            )
        ],
    ),
    source(
        "arxiv-community-identity-1705.09665.pdf",
        "https://arxiv.org/abs/1705.09665",
        "Community Identity and User Engagement in a Multi-Community Landscape",
        "arXiv authors",
        "t2",
        "marketing",
        "study:community-identity-1705.09665",
        "Community participation and identity shape whether contributions are accepted and sustained.",
        "Earn legitimacy through useful participation before introducing a commercial next step.",
        [
            support(
                B27,
                "Abstract and results",
                "The study links community identity with contribution and participation behavior.",
            )
        ],
    ),
    source(
        "arxiv-community-founders-2405.00601.pdf",
        "https://arxiv.org/abs/2405.00601",
        "How Founder Motivations, Goals, and Actions Influence Early Trajectories of Online Communities",
        "arXiv authors",
        "t2",
        "sales",
        "study:community-founders-2405.00601",
        "Online communities can help founders acquire contextual market knowledge and relationships.",
        "Learn the community problems and norms before using it as a distribution channel.",
        [
            support(
                B27,
                "Abstract and findings",
                "Independent research links founder community participation to learning and venture activity.",
            )
        ],
    ),
    source(
        "arxiv-personalized-free-trials-2006.13420.pdf",
        "https://arxiv.org/abs/2006.13420",
        "Design and Evaluation of Personalized Free Trials",
        "arXiv authors",
        "t2",
        "sales",
        "study:personalized-free-trials-2006.13420",
        "A carefully designed free trial can increase conversion when treatment is targeted and measured.",
        "Bound the trial, target it by fit, and specify the conversion outcome before offering it.",
        [
            support(
                B28,
                "Abstract and field experiment",
                "A field experiment finds conversion gains from personalized free-trial design.",
            )
        ],
    ),
    source(
        "free-sample-field-study.pdf",
        "https://www.researchgate.net/publication/338775144_In-store_free_sample_promotions_and_consumer_purchase_behavior",
        "In-Store Free Sample Promotions and Consumer Purchasing Behavior",
        "Published field-study authors",
        "t2",
        "sales",
        "study:in-store-free-sample-promotion",
        "In-store free samples can increase sales, with effects that vary substantially across customer groups.",
        "Treat sampling as a measured intervention and segment the observed purchase response.",
        [
            support(
                B28,
                "Abstract and field experiment",
                "Scanner-data field evidence reports a sales effect from in-store free sampling and heterogeneous returns.",
            )
        ],
    ),
    source(
        "ipa-free-distribution-demand.html",
        "https://poverty-action.org/publication/charge-or-not-charge-evidence-health-products-experiment-uganda",
        "To Charge or Not to Charge",
        "Innovations for Poverty Action",
        "t2",
        "sales",
        "study:ipa-uganda-free-distribution",
        "Free provision can reduce subsequent willingness to pay, so a free sample is not automatically a safe conversion tactic.",
        "Test paid follow-through and avoid assuming that free uptake predicts commercial demand.",
        [
            support(
                B28,
                "Study summary and results",
                "The randomized study reports lower later paid demand after free distribution.",
                "refutes",
                True,
            )
        ],
    ),
    source(
        "ipa-making-effectiveness-work.html",
        "https://ipa.co.uk/knowledge/publications-reports/making-effectiveness-work",
        "Making Effectiveness Work",
        "IPA",
        "t3",
        "marketing",
        "practice:ipa-effectiveness-system",
        "Campaign effectiveness depends on objectives, measurement, learning, and organizational follow-through, not creative output alone.",
        "Set the business outcome and evaluation plan before launch, then use results to change the next decision.",
        [
            support(
                B29,
                "Effectiveness framework",
                "The research treats effectiveness as a complete operating discipline.",
            )
        ],
    ),
    source(
        "google-measure-what-matters.pdf",
        "https://www.thinkwithgoogle.com/_qs/documents/measure-what-matters.pdf",
        "Measure What Matters",
        "Google",
        "t3",
        "marketing",
        "practice:google-measure-what-matters",
        "Marketing measurement should connect activity to the business outcome and account for the customer journey.",
        "Choose the KPI and experiment design that matches the campaign decision.",
        [
            support(
                B29,
                "Measurement framework, pages 3-12",
                "The guide corroborates outcome-led campaign measurement across the journey.",
            )
        ],
    ),
    source(
        "nber-firm-referrals-w33082.pdf",
        "https://www.nber.org/papers/w33082",
        "Firm Referrals and Business Relationships",
        "NBER",
        "t2",
        "sales",
        "study:nber-w33082",
        "Referrals transmit information and trust between firms and can change commercial matching.",
        "Design partner introductions around a specific buyer fit and track whether they create qualified opportunities.",
        [
            support(
                B31,
                "Abstract and empirical results",
                "The study shows a causal role for firm referrals in commercial matching.",
            )
        ],
    ),
    source(
        "nber-interfirm-w22951.pdf",
        "https://www.nber.org/papers/w22951",
        "Interfirm Relationships and Performance",
        "NBER",
        "t2",
        "sales",
        "study:nber-w22951",
        "Repeated interfirm relationships can transfer capabilities and improve outcomes beyond a one-off transaction.",
        "Pilot co-selling with shared responsibilities and measure partner and customer outcomes.",
        [
            support(
                B31,
                "Abstract and results",
                "Independent evidence links interfirm relationships with capability and performance effects.",
            )
        ],
    ),
    source(
        "ebi-mental-availability.html",
        "https://marketingscience.info/news-and-insights/mental-availability-is-not-awareness-brand-salience-is-not-awareness",
        "Mental Availability Is Not Awareness",
        "Ehrenberg-Bass Institute",
        "t2",
        "marketing",
        "research:ebi-mental-availability",
        "Brand growth depends on being recalled in relevant buying situations, not awareness in the abstract.",
        "Define category entry points and build distinctive memory cues around a consistent promise.",
        [
            support(
                B32,
                "Mental availability and category-entry-point explanation",
                "The evidence-based framework corroborates situation-linked brand memory.",
            )
        ],
    ),
    source(
        "ipa-long-short.html",
        "https://ipa.co.uk/knowledge/publications-reports/the-long-and-the-short-of-it",
        "The Long and the Short of It",
        "IPA",
        "t2",
        "marketing",
        "research:ipa-long-short",
        "Brand building and short-term activation play different but complementary roles in growth.",
        "Balance demand capture with consistent memory-building proof rather than judging brand only by immediate response.",
        [
            support(
                B32,
                "Findings on long- and short-term effects",
                "The research corroborates a repeatable memory-building role alongside activation.",
            )
        ],
    ),
    source(
        "nber-crowdfunding-w25881.pdf",
        "https://www.nber.org/papers/w25881",
        "Aiming for the Goal: Contribution Dynamics of Crowdfunding",
        "NBER",
        "t2",
        "sales",
        "study:nber-w25881",
        "Costly contributions can signal valuation and coordinate additional commitments in a crowdfunding market.",
        "Use a commitment mechanism with a clear decision threshold before scaling investment.",
        [
            support(
                B33,
                "Abstract and Kickstarter calibration",
                "The paper models and studies contributions as costly signals that affect further commitment.",
            )
        ],
    ),
    source(
        "arxiv-wtp-bias-2005.11318.pdf",
        "https://arxiv.org/abs/2005.11318",
        "A De-Biased Direct Question Approach to Measuring Consumers' Willingness to Pay",
        "arXiv authors",
        "t2",
        "sales",
        "study:wtp-bias-2005.11318",
        "Stated willingness to pay can diverge from choices that impose a real cost.",
        "Prefer deposits, paid pilots, or other costly commitments when testing commercial demand.",
        [
            support(
                B33,
                "Abstract and bias analysis",
                "The research corroborates the higher evidential value of consequential choices.",
            )
        ],
    ),
    source(
        "nber-ma-integration-w35074.pdf",
        "https://www.nber.org/papers/w35074",
        "When Does Acquisition Integration Succeed? Evidence from Inside the Integration Black Box",
        "NBER",
        "t2",
        "management",
        "study:nber-w35074",
        "Integration leadership and execution capacity affect whether acquisitions deliver expected operating outcomes.",
        "Name an integration owner and pace concurrent integrations against measurable capacity.",
        [
            support(
                B113,
                "Abstract and findings",
                "Supports the general integration-capacity mechanism, not the exact Good Glamm causal account.",
                "partial",
                False,
            )
        ],
    ),
]

# VT-750 — byte verification found only these six source-distilled cards earn a citation tier as
# currently written. Every other card is explicit T4 judgment. This is deliberately keyed by the
# archived filename (the byte-bound identity), not array index or publisher reputation.
VERIFIED_CITATION_FILES = frozenset(
    {
        "cbic-circular-92-11-2019.pdf",
        "msme-odr-guidelines.pdf",
        "nber-reviews-quality-w34934.pdf",
        "arxiv-personalized-free-trials-2006.13420.pdf",
        "nber-interfirm-w22951.pdf",
        "nber-crowdfunding-w25881.pdf",
    }
)

# A tiered card can be a faithful citation while still failing to corroborate the PARENT forum
# claim it was acquired for. These five are real citations but not qualifying edges for the target
# claim. The crowdfunding paper is the sole retained qualifying edge; one edge cannot promote.
NONQUALIFYING_VERIFIED_FILES = VERIFIED_CITATION_FILES - {"nber-crowdfunding-w25881.pdf"}

# Cai & Szeidl's two papers are one research program: same authors/region and one cites the other.
# The source rows remain individually auditable, but independence counting sees ONE cluster.
SHARED_INDEPENDENCE_CLUSTERS = {
    "nber-firm-referrals-w33082.pdf": "research:cai-szeidl-china-network-program",
    "nber-interfirm-w22951.pdf": "research:cai-szeidl-china-network-program",
}

TARGET_APPLICABILITY: dict[str, Applicability] = {
    B1: Applicability(jurisdictions=("IN",), channels=("gst_compliance",)),
    B2: Applicability(jurisdictions=("IN",), channels=("gst_compliance",)),
    B4: Applicability(jurisdictions=("IN",), channels=("gst_compliance",)),
    B5: Applicability(jurisdictions=("IN",), channels=("msme_b2b",)),
    B6: Applicability(jurisdictions=("IN",), channels=("msme_dispute_resolution",)),
    B18: Applicability(size_bands=("micro_small_business",), channels=("b2b_sales",)),
    B19: Applicability(size_bands=("micro_small_business",), channels=("ecommerce",)),
    B20: Applicability(size_bands=("micro_small_business",), channels=("online_community",)),
    B24: Applicability(maturity_stages=("early_stage",), channels=("founder_led_sales",)),
    B25: Applicability(size_bands=("micro_small_business",), channels=("b2b_sales",)),
    B26: Applicability(size_bands=("micro_small_business",), channels=("b2b_sales",)),
    B27: Applicability(size_bands=("micro_small_business",), channels=("online_community",)),
    B28: Applicability(size_bands=("micro_small_business",), channels=("sampling_trials",)),
    B29: Applicability(size_bands=("micro_small_business",), channels=("marketing_campaigns",)),
    B31: Applicability(size_bands=("micro_small_business",), channels=("b2b_partnerships",)),
    B32: Applicability(size_bands=("micro_small_business",), channels=("brand_marketing",)),
    B33: Applicability(maturity_stages=("early_stage",), channels=("demand_validation",)),
    B113: Applicability(size_bands=("micro_small_business",), channels=("business_integration",)),
}


def corrected_source_class(spec: dict[str, Any]) -> SourceClass:
    """The class this CARD earned after archived-byte verification (not source reputation)."""

    if spec["filename"] in VERIFIED_CITATION_FILES:
        return SourceClass(spec["source_class"])
    return SourceClass.T4_EXPERIENTIAL


def corrected_supports(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Make unsupported judgment and domain-transfer edges visibly non-qualifying."""

    qualifies = (
        spec["filename"] in VERIFIED_CITATION_FILES
        and spec["filename"] not in NONQUALIFYING_VERIFIED_FILES
    )
    corrected: list[dict[str, Any]] = []
    for item in spec["supports"]:
        row = dict(item)
        if not qualifies:
            # Preserve a genuine contradiction as a contradiction even when the card itself has
            # been demoted to T4. It remains non-qualifying, so it cannot independently dispute or
            # promote the parent claim, but the evidence graph does not reverse its meaning.
            if row["stance"] != "refutes":
                row["stance"] = "partial"
            row["qualifies_for_threshold"] = False
        corrected.append(row)
    return corrected


def evidence_predicate(claim: str) -> str:
    """Derive a card's claim predicate from ITS OWN claim.

    This function exists because of the defect it replaces. The predicate used to be copied from
    the parent T4 forum card, so a card whose `claim` faithfully cites the CGST Act carried a
    predicate demanding a "notice-upload and supporting-document checklist" — a behavioural control
    the Act has no concept of. Under CL-2026-08-13-judgment-vs-citation that is judgment riding a
    source's tier: the claim falls if the Act vanishes, the checklist does not.

    A predicate derived from the card's own claim cannot smuggle an instruction the source never
    gave. Retrieval matches on the predicate, so this is the field where the difference is load
    bearing, not cosmetic.
    """
    predicate = ""
    for word in re.findall(r"[a-z0-9]+", claim.casefold()):
        candidate = f"{predicate}_{word}" if predicate else word
        if len(candidate) > 200:  # ClaimKey.predicate max_length
            break
        predicate = candidate
    if not predicate:
        raise ValueError(f"claim yields no predicate: {claim!r}")
    return predicate


class DistillationExtractor:
    tools_enabled: Literal[False] = False

    def extract(self, raw_text: str) -> ExtractedClaimDraft:
        row = json.loads(raw_text)
        return ExtractedClaimDraft(
            claim=row["claim"],
            # "Cited", not "Decision". The card records what the source says and where it says it.
            # The owner-facing decision that used to live here was authored by us, not by the
            # source, and a card carrying a source's tier may not assert it.
            distillation_note=f"Cited: {row['claim']}\nEvidence locator: {row['locator']}",
            # The SUBJECT is a topic dimension and is legitimately shared with the claim this
            # source was acquired to corroborate. The PREDICATE is not: see evidence_predicate.
            claim_subject=row["claim_key"]["subject"],
            claim_predicate=row["claim_key"]["predicate"],
            claim_value=TypedClaimValue(value_type=ClaimValueType.TEXT, value=row["claim"]),
        )


class ArchiveQuarantine:
    def put(self, source: AcquiredSource, *, content_hash: str) -> QuarantineRecord:
        return QuarantineRecord(
            quarantine_ref=f"archive://{source.locator}",
            source_id=source.source_id,
            content_hash=content_hash,
            acquired_at=source.acquired_at,
        )


class VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self.suppressed += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self.suppressed = max(0, self.suppressed - 1)

    def handle_data(self, data: str) -> None:
        if not self.suppressed and data.strip():
            self.parts.append(data)


def source_text(path: Path) -> str:
    if path.suffix.casefold() == ".pdf":
        completed = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=True,
            capture_output=True,
            text=True,
        )
        text = completed.stdout
    else:
        parser = VisibleText()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        text = "\n".join(parser.parts)
    normalized = " ".join(text.split())
    if len(normalized.split()) < 12:
        raise ValueError(f"{path.name}: source snapshot has no usable expression")
    return normalized


def build() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    original: dict[str, dict[str, Any]] = {}
    for line in (CORPUS / "candidate_cards.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["legacy_id"] in TARGET_LEGACY_IDS:
            original[row["legacy_id"]] = row
    if set(original) != TARGET_LEGACY_IDS:
        raise ValueError("the exact 18 T4 parent candidates were not found")

    decisions: dict[str, SourceRightsDecision] = {}
    prepared: list[tuple[dict[str, Any], Path, str, str]] = []
    for raw_spec in SOURCES:
        card_class = corrected_source_class(raw_spec)
        spec = {
            **raw_spec,
            "source_class_before_correction": raw_spec["source_class"],
            "source_class": card_class.value,
            "cluster": SHARED_INDEPENDENCE_CLUSTERS.get(raw_spec["filename"], raw_spec["cluster"]),
            "supports": corrected_supports(raw_spec),
        }
        path = ARCHIVE / spec["filename"]
        if not path.is_file():
            raise ValueError(f"missing local-only source snapshot: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        source_id = str(uuid5(NAMESPACE_URL, f"viabe:o8:source:{spec['url']}"))
        decisions[source_id] = SourceRightsDecision(
            source_class=card_class,
            usage_rights=UsageRights(
                status=UsageRightsStatus.UNKNOWN,
                reviewed_at=ACQUIRED_AT,
                reviewed_by="codex:vt723-source-governance",
            ),
            contractual_extraction_restriction=False,
            paywall_access_circumvented=False,
            compilation_concentration=False,
        )
        prepared.append((spec, path, digest, source_id))

    registry = InMemoryCandidateRegistry()
    pipeline = IngestionPipeline(
        rights=MappingRightsResolver(decisions),
        quarantine=ArchiveQuarantine(),
        dedupe=InMemoryDedupeStore(),
        extractor=DistillationExtractor(),
        registry=registry,
        embedder=None,
        embedding_mode=EmbeddingMode.DEFER,
    )
    manifests: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    support_by_legacy: dict[str, list[dict[str, Any]]] = {}
    for spec, path, digest, source_id in prepared:
        primary_parent = original[spec["supports"][0]["legacy_id"]]["card"]
        # The extraction input carries only what the card is allowed to assert: the cited claim,
        # where in the source it sits, and an identity whose predicate comes from the claim itself.
        # `spec["action"]` is deliberately ABSENT — it is our hypothesis about what an owner should
        # then do, it is not in the source, and a card carrying the source's tier may not smuggle
        # it. It stays in the acquisition table as provenance of intent, and a test asserts no
        # card's value ever equals it again.
        raw = json.dumps(
            {
                "claim": spec["claim"],
                "locator": "; ".join(item["locator"] for item in spec["supports"]),
                "claim_key": {
                    **primary_parent["claim_key"],
                    "predicate": evidence_predicate(spec["claim"]),
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if spec["source_class"] == "t1":
            applicability = Applicability(
                jurisdictions=(spec["jurisdiction"],),
                effective_from=datetime.fromisoformat(
                    spec["effective_from"].replace("Z", "+00:00")
                ),
            )
        else:
            # Every non-regulatory card is explicitly scoped. Empty/universal-by-accident cards are
            # not permitted: an absent dimension used to match every tenant context.
            applicability = TARGET_APPLICABILITY[spec["supports"][0]["legacy_id"]]
        expires_at = ACQUIRED_AT + timedelta(days=180) if spec["source_class"] == "t4" else None
        candidate = pipeline.ingest(
            AcquiredSource(
                source_id=source_id,
                canonical_url=spec["url"],
                publisher=spec["publisher"],
                acquired_at=ACQUIRED_AT,
                raw_text=raw,
                locator=str(path.relative_to(REPO_ROOT)),
                content_kind=AcquiredContentKind.OWNED_DISTILLATION,
                expression_reference_text=source_text(path),
            ),
            governance=CandidateGovernance(
                domain=KnowledgeDomain(spec["domain"]),
                authority=EvidenceAuthority.SEED,
                confidence=suggested_confidence_for_source(SourceClass(spec["source_class"])),
                applicability=applicability,
                retention_class="lifecycle_managed",
                independence_cluster=spec["cluster"],
                expires_at=expires_at,
            ),
            card_id=str(uuid5(NAMESPACE_URL, f"viabe:o8:evidence-card:{source_id}")),
            card_version_id=str(
                uuid5(NAMESPACE_URL, f"viabe:o8:evidence-card-version:{source_id}:1")
            ),
        )
        candidates.append(candidate.model_dump(mode="json"))
        manifests.append(
            {
                "source_id": source_id,
                "canonical_url": spec["url"],
                "title": spec["title"],
                "publisher": spec["publisher"],
                "source_class": spec["source_class"],
                "source_class_before_correction": spec["source_class_before_correction"],
                # knowledge_sources describes the acquired source reproduction, not our JSON
                # distillation. Keep that byte hash distinct from CandidateArtifact's
                # source_content_hash (which binds the owned extraction input).
                "content_hash": digest,
                "acquired_at": ACQUIRED_AT.isoformat().replace("+00:00", "Z"),
                "local_archive_path": str(path.relative_to(REPO_ROOT)),
                "local_archive_sha256": digest,
                "retention_class": "local_source_reproduction",
                "usage_rights": RIGHTS_REVIEW,
                "paywall_access_circumvented": False,
                "contractual_extraction_restriction": False,
                "compilation_concentration": False,
                "independence_cluster": spec["cluster"],
                "underlying_evidence_id": spec["cluster"],
                "depends_on_original_forum": False,
                "candidate_card_version_id": candidate.card.card_version_id,
                "pipeline_steps": list(candidate.pipeline_steps),
                "originality_mode": candidate.expression_originality.mode.value,
                "originality_scanner": candidate.expression_originality.scanner,
                "supports": spec["supports"],
            }
        )
        for item in spec["supports"]:
            support_by_legacy.setdefault(item["legacy_id"], []).append(
                {
                    "source_id": source_id,
                    "independence_cluster": spec["cluster"],
                    "stance": item["stance"],
                    "locator": item["locator"],
                    "qualifies_for_threshold": item["qualifies_for_threshold"],
                }
            )

    skipped = {
        B25: [
            "https://hbr.org/2014/07/the-value-of-keeping-the-right-customers",
            "https://www.gartner.com/en/sales/insights/sales-pipeline",
        ],
        B113: [
            "https://www.business-standard.com/companies/start-ups/beauty-unicorn-ceo-blames-strategy-collapse-125080101574_1.html"
        ],
    }
    delta: list[dict[str, Any]] = []
    for legacy_id in sorted(original):
        edges = support_by_legacy.get(legacy_id, [])
        corroborating = {
            edge["independence_cluster"]
            for edge in edges
            if edge["qualifies_for_threshold"] and edge["stance"] == "corroborates"
        }
        qualifying = {
            edge["independence_cluster"] for edge in edges if edge["qualifies_for_threshold"]
        }
        has_refutation = any(
            edge["qualifies_for_threshold"] and edge["stance"] == "refutes" for edge in edges
        )
        if has_refutation and corroborating:
            resolved, reason, absence = (
                "disputed",
                "Independent field evidence both supports and refutes the tactic; context controls the effect.",
                False,
            )
        elif len(corroborating) >= 2:
            resolved, reason, absence = (
                "candidate",
                "Two independent non-forum evidence clusters corroborate the claim.",
                False,
            )
        else:
            resolved, reason, absence = (
                "research_only",
                "The exact claim did not reach two independent qualifying clusters.",
                True,
            )
        card = original[legacy_id]["card"]
        delta.append(
            {
                "legacy_id": legacy_id,
                "prior_status": "research_only",
                "resolved_status": resolved,
                "original_source_id": original[legacy_id]["source_id"],
                "original_independence_cluster": card["independence_cluster"],
                "evidence_edges": edges,
                "qualifying_new_cluster_count": len(corroborating),
                "total_independence_cluster_count": 1 + len(qualifying),
                "search": {
                    "queries": [
                        f"{legacy_id} authoritative corroboration",
                        f"{card['claim_key']['subject']} published research",
                    ],
                    "skipped_paywalled_sources": skipped.get(legacy_id, []),
                    "semantic_retellings_collapsed": [],
                    "recorded_absence": absence,
                    "note": "Partial sources are recorded but excluded from the threshold."
                    if absence
                    else "No source depends on or cites the originating forum discussion.",
                },
                "resolution_reason": reason,
                "authorizes_effects": False,
            }
        )
    return manifests, candidates, delta


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> int:
    manifests, candidates, delta = build()
    write_jsonl(MANIFEST_OUT, manifests)
    write_jsonl(CANDIDATES_OUT, candidates)
    write_jsonl(DELTA_OUT, delta)
    counts = Counter(row["resolved_status"] for row in delta)
    tiers = Counter(row["source_class"] for row in manifests)
    independent_clusters = len({row["independence_cluster"] for row in manifests})
    report = f"""# VT-723 T4 corroboration report

- Exact forum claims reviewed: **{len(delta)}**
- New governed source records: **{len(manifests)}**, representing **{independent_clusters} independent clusters**
- VT-710 pipeline results: **{len(candidates)} inert candidates**, all embedding-deferred
- Earned card-tier mix after archived-byte verification: **{tiers["t1"]} T1 / {tiers["t1v"]} T1v / {tiers["t2"]} T2 / {tiers["t3"]} T3 / {tiers["t4"]} T4**
- **Source verification: `T4_CORROBORATION_VERIFICATION.md` + `t4_corroboration_verification.jsonl`.** Every card was checked against the bytes of the source it cites. Six faithful citations retain the class their evidence earns; the other 27 are explicitly demoted to T4 judgment. `assert_corpus_verified` accepts only those two exact postures and has no waiver path. The three unsound promotions are recorded before and after in `t4_corroboration_unsound_promotions.json`
- Authorship authority: **seed** for all Codex distillations; none labelled owner, VTR, or verified outcome
- Claim identity: subject inherited from the target claim, **predicate derived from each card's OWN claim** (it used to be inherited, which made a cited fact carry an invented behavioural instruction); no universal-by-default cards
- Byte binding: each card reaches its source bytes as card -> `provenance.source_ids[0]` -> `knowledge_sources.content_hash`, which is the sha256 of the acquired archive file. `source_content_hash` is the hash of our own extraction input and binds nothing about the source
- Source verifiability: archives are **local-only and gitignored**, so byte verification and deterministic regeneration run only where the archive is present; both are asserted by skip-guarded tests rather than claimed here
- Evidence-state result: **{counts["candidate"]} candidate / {counts["disputed"]} disputed / {counts["research_only"]} research_only**
- Semantic retellings counted as corroboration: **0**
- Paywall circumvention: **0**; paywalled candidates were skipped and logged
- Raw source reproductions committed: **0**; archive inputs remain local-only
- Retrieval eligibility granted: **0**
- Effect authority granted: **0**

## Landing posture

This corpus lands in **SHADOW**. The retrieval call site is not wired and prompt injection remains
locked off. These cards and evidence transitions become durable reviewable substrate only; this
change makes no claim of current product impact.

All 18 forum claims remain research-only. The corrected evidence set supplies at most one
qualifying independent cluster to any target. Non-qualifying partial and refuting evidence remains
recorded for audit, but it cannot promote or dispute a parent claim. This is recorded absence, not
silent rejection.
"""
    REPORT_OUT.write_text(report, encoding="utf-8")
    print(
        f"VT-723 hunt complete: {len(manifests)} sources; {counts['candidate']} candidate / {counts['disputed']} disputed / {counts['research_only']} research_only"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
