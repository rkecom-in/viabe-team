# Executional Business Knowledge Source Index

Created: 2026-07-25

Purpose: scenario-rich source material for Viabe Team’s Manager and specialist agents. This layer is intentionally different from the base legal/policy archive: it is meant to teach judgment, trade-offs, failure modes, escalation points, and operating patterns.

## Source trust ladder

Use this order when agents resolve conflicts:

1. Regulator orders, statutory notifications, RBI/Government sources.
2. Official platform/network playbooks and policies.
3. Practitioner/operator forum posts, only as “real-world scenario signals.”
4. Business news, only as URL + short summary unless licensing permits archival.
5. Blogs/SEO content only when they point to primary material.

Forum/news material should never become deterministic compliance truth by itself. It should become scenario cards that are later checked against statutes, regulator orders, or policy docs.

## Regulator case corpus

These are the highest-value executional files in this tranche because they show “what actually triggered action,” not just abstract rules.

| Local file | Source | What to extract |
|---|---|---|
| `cases/ccpa_orders_index.html` | https://jagograhakjago.gov.in/CCPA_Orders/index.html | Master index of CCPA orders. Use as the refresh anchor. |
| `cases/ccpa_physicswallah_dark_patterns_2026.pdf` | CCPA order linked from the orders index | Dark-pattern decision card: conversion UI, consumer autonomy, misleading urgency/subscription/choice design, corrective action. |
| `cases/ccpa_mcafee_dark_patterns_2026.pdf` | CCPA order linked from the orders index | Subscription/dark-pattern decision card: renewal/cancellation, consent clarity, refund/support handling. |
| `cases/ccpa_tradeindia_marketplace_2026.pdf` | CCPA order linked from the orders index | Marketplace listing safety card: platform responsibility, seller verification, complaint handling. |
| `cases/ccpa_amazon_walkietalkie_marketplace_2026.pdf` | CCPA order linked from the orders index | Marketplace prohibited/restricted-product card: listing controls and pre-publication checks. |
| `cases/ccpa_facebook_marketplace_2026.pdf` | CCPA order linked from the orders index | Informal marketplace risk card: seller identity, fraud exposure, platform response. |
| `cases/ccpa_zepto_ecommerce_2025.pdf` | CCPA order linked from the orders index | Ecommerce/cart/charge/customer promise card. |
| `cases/ccpa_meesho_ecommerce_2025.pdf` | CCPA order linked from the orders index | Marketplace customer-experience and representation card. |
| `cases/ccpa_firstcry_ecommerce_2025.pdf` | CCPA order linked from the orders index | Ecommerce consumer promise / grievance / representation card. |
| `cases/ccpa_foo_ahmedabad_restaurant_2026.pdf` | CCPA order linked from the orders index | Restaurant pricing/service-charge disclosure card. |
| `cases/ccpa_zomato_order_2024.pdf` | CCPA order linked from the orders index | Food-delivery pricing/refund/fee communication card. |
| `cases/ccpa_swiggy_order_2024.pdf` | CCPA order linked from the orders index | Food-delivery pricing/refund/fee communication card. |
| `cases/asci_complaint_outcomes_index.html` | https://www.ascionline.in/complaint-outcomes/ | Marketing claims outcome index. Extract recurring claim-substantiation mistakes. |

## Finance / COO corpus

These are decision-making sources for working-capital, receivables, lender readiness, delayed payments, and CFO/COO escalation.

| Local file | Source | What to extract |
|---|---|---|
| `../finance/rbi_treds_faq.html` | https://www.rbi.org.in/scripts/FAQView.aspx/FAQView.aspx/FAQView.aspx?Id=132 | TReDS eligibility, seller/buyer/financier workflow, invoice discounting decision points. |
| `../finance/rbi_msme_lending_master_direction_2024.html` | https://systemhealth.rbi.org.in/Scripts/BS_ViewMasDirections.aspx_id%3D11060.html | Priority-sector/MSME lending rails, bank handling expectations, credit-facilitation context. |
| `../finance/msme_samadhaan_dashboard.html` | https://dashboard.msme.gov.in/msme_samadhaan.aspx | Delayed-payment public dashboard. Use as market severity signal and Samadhaan process anchor. |
| `../finance/msme_dashboard_overview.html` | https://dashboard.msme.gov.in/dashboard.aspx | MSME ecosystem metrics; use for COO context and macro priors. |
| `../finance/pib_cpse_treds_mandatory_2026.html` | https://www.pib.gov.in/PressReleasePage.aspx?PRID=2283195&lang=1&reg=48 | Public-sector buyer TReDS mandate signal; useful for supplier receivables strategy. |
| `../finance/pib_schemes_to_clear_delayed_payment_msmes_2023.html` | https://www.pib.gov.in/PressReleaseIframePage.aspx?PRID=1942085&lang=2&reg=48 | Official delayed-payment scheme context; useful for owner-facing escalation options. |

## Manager / COO high-reliability corpus

These sources train cross-functional judgment rather than specialist execution. The Manager index should retrieve the decision lens—business risk, manager action, red flags, and escalation/gate—while specialist indexes retain detailed domain execution.

| Local file | Source | Scenario value |
|---|---|---|
| `../manager/decision-making/COO_DECISION_RESEARCH_INDEX.md` | Locally authored two-round research synthesis | Complete capability map and live-link fallbacks for decision science, robust strategy, forecasting, capital allocation, red teaming, crisis command, governance, negotiation, and organizational learning. |
| `../manager/decision-making/national_academies_decision_guide_common_steps.html` | National Academies (2026) | Structured multi-objective decisions and separation of facts, values, alternatives, uncertainty, trade-offs, and implementation. |
| `../manager/decision-making/nasa_risk_informed_decision_handbook.pdf` | NASA | Risk-informed analysis of alternatives and precommitted risk thresholds. |
| `../manager/decision-making/uk_green_book_2026.html` | HM Treasury | Outside-view forecasting, optimism-bias correction, options appraisal, sensitivity, contingency, and real options. |
| `../manager/decision-making/oecd_supporting_decisions_strategic_foresight.pdf` | OECD | Plausible futures, signposts, no-regret moves, and adaptive strategy. |
| `../manager/decision-making/amazon_2016_high_velocity_decisions.html` | Amazon shareholder letter | Reversible decision speed, correction loops, disagree-and-commit, true misalignment, and proxy failure. |
| `../manager/decision-making/nasa_columbia_organizational_culture.html` | Columbia Accident Investigation synthesis | Production-pressure drift, discarded dissent, recurring anomaly normalization, and the need for independent assurance. |
| `../manager/decision-making/fema_ics_command_summary.html` | FEMA | One incident owner, objectives, modular roles, factual logs, spans of control, and operational periods. |
| `../manager/decision-making/nasa_lessons_learned.html` | NASA APPEL | Collect-record-disseminate-apply learning lifecycle; lessons must change future artifacts or controls. |
| `../manager/decision-making/oecd_risk_management_corporate_governance.pdf` | OECD | Strategy-integrated risk governance, independent assurance, and incentive risk. |
| `../manager/decision-making/harvard_pon_batna.html` | Harvard Program on Negotiation | BATNA, reservation point, total deal economics, dependency risk, and walk-away discipline. |
| `../manager/decision-making/toyota_tps_jidoka.html` | Toyota | Quality at source, frontline stop authority, containment, root-cause correction, and verified restart. |
| `../manager/decision-making/berkshire_owners_manual.html` | Berkshire Hathaway | Owner economics, long-horizon capital stewardship, and candid evaluation of allocation decisions. |

URL-only sources that resisted CLI mirroring—GAO cost-estimation guide, RAND Robust Decision Making, Army Red Team Handbook, McKinsey resource-allocation research, and Amazon's 2015 letter—are indexed with precise capability summaries in `COO_DECISION_RESEARCH_INDEX.md`.

## Consulting and academic execution research

The detailed evidence assessment, portal triage, source limitations, and RAG routing are in `../research/CONSULTING_ACADEMIC_RESEARCH_INDEX.md`. This tranche adds thirteen executional cards, BK068–BK080.

| Local file | Source family | Scenario value |
|---|---|---|
| `../research/CONSULTING_ACADEMIC_RESEARCH_INDEX.md` | McKinsey, BCG, Bain, HBS/NBER, and supplied discovery portals | Evidence map and operational synthesis for decision process, allocation, organizational health, transformation, change strategy, commercial coordination, omnichannel, experimentation, and portal/source trust. |
| `../research/consulting/bcg_transformation_office.html` | BCG transformation research | One baseline, value owners, Finance validation, stage gates, issue escalation, adoption, and business-as-usual handoff. |
| `../research/consulting/bcg_change_strategy.html` | BCG change research | Diagnose urgency, destination/path certainty, stakeholder mode, stages, and capability before choosing a change method. |
| `../research/consulting/bcg_commercial_excellence.html` | BCG commercial research | Join Marketing, Sales, Pricing, and Service; track price realization, handoffs, installed-base signals, and customer economics. |
| `../research/consulting/bain_strategy_beliefs.html` | Bain strategy synthesis | Strong-core and one-step-adjacency discipline, repeatable capabilities, future-back uncertainty, and staged feedback. |
| `../research/consulting/bain_closing_customer_feedback_loop.pdf` | Bain customer-system cases | Move feedback from score to rapid frontline recovery, root-cause coding, structural action, and outcome tracking. |
| `../research/academic/nber_workplace_knowledge_flows_w26660.pdf` | Sales-company field experiment | Structured peer technique conversations transfer tacit sales knowledge; joint-output incentives alone were not sufficient. |
| `../research/academic/nber_management_practices_airline_w25620.pdf` | Eight-month field experiment | Monitoring, feedback, targets, and prosocial incentives can improve a controllable behaviour when quality and gaming risks are protected. |

## Operator forum corpus

These pages are not authoritative law. They are valuable because they expose real SMB questions, ambiguity, panic points, missing documents, cashflow stress, and common bad instincts.

| Local file | Source | Scenario value |
|---|---|---|
| `forums/caclubindia_gst_notice_credit_notes_2025.html` | https://www.caclubindia.com/forum/supporting-documents-required-for-gst-notice-on-credit-notes-8211-fy-2021-8211-22-613780.asp | GST notice response: credit-note documentation, GSTR-1 linkage, buyer ITC reversal proof, CA review trigger. |
| `forums/caclubindia_gstr3b_itc_supplier_late_filing_2025.html` | https://www.caclubindia.com/forum/gst-3b-input-tax-credit-612404.asp | Cashflow vs compliance dilemma: supplier files late, buyer wants ITC before GSTR-2B reflection. |
| `forums/caclubindia_bad_debt_recovery_2026.html` | https://www.caclubindia.com/forum/bad-debt-recovery-615727.asp | Bad debt: owner tries to use credit note as recovery/tax workaround; agent should redirect to legal/MSME Samadhaan/TReDS/collections logic. |
| `forums/caclubindia_output_gst_bad_debts_2019.html` | https://www.caclubindia.com/forum/output-gst-claim-on-bad-debts-514602.asp | GST already paid but customer defaulted: tax vs receivable separation; do not invent refund where law does not provide one. |
| `forums/caclubindia_msme_delayed_payment_interest_2013.html` | https://www.caclubindia.com/forum/urgent-interest-on-msme-266538.asp | MSME 45-day payment discipline: book provisioning, buyer relationship pressure, council escalation. |
| `forums/caclubindia_msefc_delayed_payment_notice_2021.html` | https://www.caclubindia.com/experts/notice-for-delayed-payment-of-mse-by-msefc-2857399.asp | Buyer receives MSEFC notice: response drafting, evidence, explanation, and settlement path. |

## Sales and marketing discussion corpus

These files are operator-discussion signals, not authoritative sources. Their value is conditional tactical judgment: when a sales or marketing tactic works, when it becomes wasteful, and what proof/constraint changes the decision.

| Local file | Source | Scenario value |
|---|---|---|
| `../sales/discussions/DISCUSSION_SIGNAL_INDEX.md` | Locally authored index from archived discussion pages | Sales/marketing tactic matrix: first customers, local trust, digital/physical hybrid, community launches, channel migration, campaign success, and brand proof. |
| `../sales/discussions/hn_first_10_customers_2026.html` | https://news.ycombinator.com/item?id=44544542 | First-10-customer scenario: narrow ICP, Dream-10 prospect list, founder-led discovery, free diagnostics, and the danger of premature outsourced sales. |
| `../sales/discussions/hn_first_100_users_2024.html` | https://news.ycombinator.com/item?id=41862332 | First-100-user scenario: niche communities, sample loops, partner networks, channel migration by ACV, and low-ticket sales efficiency. |
| `../sales/discussions/hn_early_adopters_2022.html` | https://news.ycombinator.com/item?id=31930935 | Early-adopter scenario: talk to people already feeling the pain before build/scale decisions. |
| `../sales/discussions/indiehackers_first_10_paying_customers_no_audience.html` | https://www.indiehackers.com/post/founders-with-no-audience-how-did-you-get-your-first-10-paying-customers-3652b6b1c8 | No-audience acquisition examples across marketplaces, Product Hunt, Reddit, and cold outreach. |
| `../sales/discussions/indiehackers_first_customers.html` | https://www.indiehackers.com/post/how-did-you-get-your-first-customers-0116a1c4a7 | Founder examples across personal network, direct conversations, community posting, events, LinkedIn, Product Hunt, and Facebook groups. |
| `../marketing/discussions/reddit_ecommerce_marketing_operator_thread.html` | https://id.reddit.com/r/Entrepreneur/comments/125wuek/lets_talk_ecommerce_the_numbers_today_and_how_to/ | Ecommerce/DTC scenario: digital marketing as amplifier, CAC pressure, unit economics, micro-influencers, community, and testing discipline. |
| `../marketing/discussions/reddit_small_business_google_ads_local_bakery.html` | https://ja.reddit.com/r/smallbusinessesowners/comments/1ru2hnk/google_ads_has_become_suicide_for_small/ | Local low-ticket business scenario: paid ads vs GBP/reviews/UGC/loyalty/local discovery. |
| `../marketing/discussions/reddit_business_viral_marketing.html` | https://www.business.reddit.com/learning-hub/articles/viral-marketing | Platform guidance: authenticity, trust, native creative, and viral-campaign risk. |
| `../marketing/discussions/reddit_business_digital_marketing.html` | https://www.business.reddit.com/smb/grow-business-with-digital-marketing | Platform guidance: digital channels, targeting, social listening, retargeting, influencers, KPIs, and ROI. |

## Forum/news URLs to keep as live links

These were useful from search, but not mirrored cleanly or should not be bulk-copied. Use URL + title + short internal summary only.

| Source URL | Why useful |
|---|---|
| https://fi.reddit.com/r/smallbusinessindia/comments/1ujeky8/how_do_you_guys_pitch_to_corporates_and_land_b2b/ | Indian SMB B2B corporate pitching: decision-makers, LinkedIn/cold email/channel mix, samples, response rates, early mistakes. Curl returned a block page, so ingest via compliant API/export/manual review. |
| https://dd.reddit.com/r/smallbusinessindia/comments/1qzz5tf/started_a_forever_flowers_business_advice_needed/ | Micro-business shipping/unit-economics scenario: volumetric weight, courier choice, local-first strategy, channel focus. Curl returned verification page. |
| https://www.caclubindia.com/forum/display.asp?cat_id=54 | GST discussion index with fresh scenario discovery: notices, ITC, returns, refund, export, credit note, e-invoice issues. |
| https://www.business.reddit.com/learning-hub/articles/smb-how-to-use-reddit | Reddit’s own SMB marketing guide. Use as platform guidance, not independent proof of ROI. |
| https://www.pib.gov.in/Pressreleaseshare.aspx?PRID=1955344&lang=2&reg=48 | Official business-news-style source for dark-pattern guideline context and industry consultation. |
| https://www.pib.gov.in/Pressreleaseshare.aspx?PRID=2090048&lang=2&reg=48 | Official DPDP draft-rules announcement and implementation overview. |

## Scenario-card schema for ingestion

Convert every case/forum/news item into this shape before vectorizing. This is the moat-bearing representation.

```json
{
  "id": "stable_source_slug",
  "retrieved_at": "2026-07-25",
  "source_type": "regulator_order | official_guidance | forum_operator_voice | business_news",
  "trust_level": "authoritative | high | medium | low",
  "domain": "sales | marketing | compliance | finance | manager_coo",
  "situation": "Plain-language business situation.",
  "actor": "seller | buyer | marketplace | restaurant | advertiser | finance_manager | owner",
  "business_stage": "pre-sale | sale | fulfilment | post-sale | collections | return_filing | audit_notice | dispute",
  "decision_pressure": "cashflow | conversion | customer_retention | regulatory_risk | platform_policy | reputation",
  "mistake_or_risk": "What went wrong or could go wrong.",
  "evidence_needed": ["invoice", "GSTR-1", "buyer communication", "proof of delivery"],
  "recommended_next_action": "Concrete next operational move.",
  "red_flags": ["When the agent must escalate to CA/lawyer/human manager."],
  "agent_lesson": "What a seasoned specialist should learn.",
  "hard_gate_candidate": "Whether this should become product-level guardrail.",
  "source_url": "https://...",
  "local_file": "archives/business-knowledge/..."
}
```

## Agent-specific lessons to extract

- Sales Agent: pipeline source selection, B2B buyer identification, sample/PO/credit-term handling, owner follow-up discipline, margin-aware deal qualification, first-10 founder-led discovery, partner/channel selling, ACV-aware sales motion choice.
- Marketing Agent: claim substantiation, dark-pattern avoidance, platform trust, local discovery, no fake urgency, no disguised advertising, campaign brief discipline, community-native content, digital/physical hybrid loops, brand memory/proof.
- Compliance Agent: GST notice triage, ITC timing, credit-note limits, DPDP notice/consent/breach posture, ecommerce consumer-protection rails.
- Finance Agent: receivables aging, MSME delayed-payment escalation, TReDS fit, cashflow vs tax-treatment separation, customer default playbooks.
- Manager / COO: trade-off arbitration between growth and risk, when to accept operational friction, when to escalate to CA/lawyer, when to change SOPs, and when to stop a tempting but dangerous workaround.
