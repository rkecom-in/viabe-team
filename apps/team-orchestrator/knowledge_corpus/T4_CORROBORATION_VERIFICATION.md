# VT-723 source verification — what the archived bytes actually say

Every card below was checked by opening the source snapshot it cites and reading the passage its
`locator` points at. This is the verifiability condition of **CL-2026-08-13-judgment-vs-citation**:
an AI-distilled citation may carry its source's tier only if the claim is verifiable against that
source. Where it is not, the tier is unearned and the card collapses to T4.

**6 of 33 cards are landing-grade.** `assert_corpus_verified` refuses to load the
corpus while any card is not — the gate runs before the database connection is opened, so this is
not advice about the data, it is the reason the data is still out.

Machine-readable record: `t4_corroboration_verification.jsonl`. `landing_grade` is recomputed
from the verdicts at load time, so editing the boolean cannot open the gate.

## The three unsound promotions

These matter more than the tiers: a promotion out of `research_only` rests on independent
qualifying clusters, and for these three the clusters do not hold.

- **`bk019-bulky-product-shipping-unit-economics-local-first`** — Both qualifying clusters fail: the USPS tariff is US-specific and non-transferable, and the SBA page does not discuss shipping at all (freight/postage/carrier/dimension/fulfil = 0 hits). Nothing corroborates the India shipping conclusion.

- **`bk031-partner-network-co-selling-borrows-trust`** — The two 'independent' clusters are Cai & Szeidl field experiments in the same Chinese province, and one paper cites the other — one research program counted twice. Neither studies co-selling, and the trust claim is attached to the paper that does not contain it.

- **`bk032-brand-memory-proof-and-promise`** — Both clusters are opinion posts: a 5-paragraph blog deferring to a book, and a blog post that is not the report its URL names. Neither carries the memory/proof/promise content.


## Per-card verdicts

| # | source | tier | claim | vanish | action in source? | tier verdict | jurisdiction | confidence |
|---|---|---|---|---|---|---|---|---|
| 0 | Central Goods and Services Tax Act, 2017 | `t1` | **PARTIALLY_SUPPORTED** | CITATION | **NOT_IN_SOURCE** | TIER_OK | OK | OK |
| 1 | CGST Rules: Accounts and Records | `t1` | **PARTIALLY_SUPPORTED** | **MIXED** | **NOT_IN_SOURCE** | **NOT_DEFENSIBLE_AS_RECORDED** | OK | OK |
| 2 | Advisory on GSTR-2B | `t1` | SUPPORTED | CITATION | SOURCE_STATES_IT | **TIER_SHOULD_BE_t1v** | OK | **HIGH_TO_MEDIUM** |
| 3 | Invoice Management System Advisory | `t1` | **PARTIALLY_SUPPORTED** | **MIXED** | **NOT_IN_SOURCE** | **TIER_SHOULD_BE_t3** | OK | **HIGH_TO_MEDIUM** |
| 4 | Circular No. 92/11/2019-GST | `t1` | SUPPORTED | CITATION | **NOT_IN_SOURCE** | TIER_OK | OK | OK |
| 5 | MSME Samadhaan delayed-payment mechanism | `t1` | SUPPORTED | CITATION | **NOT_IN_SOURCE** | **TIER_SHOULD_BE_t3** | OK | **HIGH_TO_MEDIUM** |
| 6 | Guidelines for MSE ODR Scheme | `t1` | SUPPORTED | CITATION | SOURCE_STATES_IT | TIER_OK | OK | OK |
| 7 | Account-Based Marketing for B2B | `t3` | SUPPORTED | CITATION | SOURCE_STATES_IT | TIER_OK | **IN_UNEARNED** | OK |
| 8 | How to Choose Target Accounts for ABM | `t3` | **PARTIALLY_SUPPORTED** | **MIXED** | SOURCE_STATES_IT | TIER_OK | **IN_UNEARNED** | OK |
| 9 | Break-even point calculation | `t3` | **PARTIALLY_SUPPORTED** | **JUDGMENT** | **NOT_IN_SOURCE** | TIER_OK | **IN_WRONG** | OK |
| 10 | Parcel size, weight and fee standards | `t1v` | SUPPORTED | CITATION | **PARTIAL** | TIER_OK | **IN_WRONG** | OK |
| 11 | Needmining: Designing Digital Support to Eli | `t2` | SUPPORTED | CITATION | **NOT_IN_SOURCE** | TIER_OK | OK | **HIGH_TO_MEDIUM** |
| 12 | From Complaint to Action: Technology-Enabled | `t2` | SUPPORTED | CITATION | SOURCE_STATES_IT | TIER_OK | OK | OK |
| 13 | Sampling Bias in Entrepreneurial Experiments | `t2` | SUPPORTED | **MIXED** | **NOT_IN_SOURCE** | TIER_OK | OK | OK |
| 14 | Experimentation and Startup Performance: Evi | `t2` | **PARTIALLY_SUPPORTED** | **JUDGMENT** | **NOT_IN_SOURCE** | TIER_OK | OK | **HIGH_TO_MEDIUM** |
| 15 | Customer Discovery Basics | `t3` | **PARTIALLY_SUPPORTED** | **MIXED** | **PARTIAL** | TIER_OK | OK | OK |
| 16 | Field Experiments in Marketing | `t2` | SUPPORTED | **JUDGMENT** | **NOT_IN_SOURCE** | **TIER_SHOULD_BE_t3** | OK | **HIGH_TO_MEDIUM** |
| 17 | B2B Omnichannel Guide | `t3` | SUPPORTED | **JUDGMENT** | SOURCE_STATES_IT | TIER_OK | OK | **MEDIUM_TO_LOW** |
| 18 | Go-to-Market Strategy | `t3` | **PARTIALLY_SUPPORTED** | **JUDGMENT** | **NOT_IN_SOURCE** | TIER_OK | OK | **MEDIUM_TO_LOW** |
| 19 | Community Identity and User Engagement in a  | `t2` | **PARTIALLY_SUPPORTED** | **MIXED** | **NOT_IN_SOURCE** | TIER_OK | OK | **HIGH_TO_MEDIUM** |
| 20 | How Founder Motivations, Goals, and Actions  | `t2` | **NOT_FOUND** | **JUDGMENT** | **NOT_IN_SOURCE** | **CARD_CANNOT_INHERIT_TIER** | OK | **INDEFENSIBLE** |
| 21 | Design and Evaluation of Personalized Free T | `t2` | SUPPORTED | CITATION | SOURCE_STATES_IT | TIER_OK | OK | OK |
| 22 | In-Store Free Sample Promotions and Consumer | `t2` | SUPPORTED | CITATION | SOURCE_STATES_IT | **TIER_SHOULD_BE_t3** | OK | **HIGH_TO_MEDIUM** |
| 23 | To Charge or Not to Charge | `t2` | SUPPORTED | CITATION | SOURCE_STATES_IT | **TIER_OK_CONDITIONAL** | OK | **HIGH_TO_MEDIUM** |
| 24 | Making Effectiveness Work | `t3` | **PARTIALLY_SUPPORTED** | **JUDGMENT** | **NOT_IN_SOURCE** | TIER_OK | OK | OK |
| 25 | Measure What Matters | `t3` | SUPPORTED | **JUDGMENT** | SOURCE_STATES_IT | TIER_OK | OK | OK |
| 26 | Firm Referrals and Business Relationships | `t2` | **PARTIALLY_SUPPORTED** | **MIXED** | **NOT_IN_SOURCE** | **CARD_CANNOT_INHERIT_TIER** | OK | **INDEFENSIBLE** |
| 27 | Interfirm Relationships and Performance | `t2` | SUPPORTED | CITATION | **NOT_IN_SOURCE** | TIER_OK | OK | OK |
| 28 | Mental Availability Is Not Awareness | `t2` | **PARTIALLY_SUPPORTED** | **JUDGMENT** | **NOT_IN_SOURCE** | **TIER_SHOULD_BE_t3** | OK | **INDEFENSIBLE** |
| 29 | The Long and the Short of It | `t2` | **PARTIALLY_SUPPORTED** | **JUDGMENT** | **PARTIAL** | **WRONG_ARTIFACT** | OK | **INDEFENSIBLE** |
| 30 | Aiming for the Goal: Contribution Dynamics o | `t2` | SUPPORTED | CITATION | **NOT_IN_SOURCE** | TIER_OK | OK | OK |
| 31 | A De-Biased Direct Question Approach to Meas | `t2` | SUPPORTED | **MIXED** | **SOURCE_CONCLUSION_CONTRADICTS** | TIER_OK | OK | OK |
| 32 | When Does Acquisition Integration Succeed? E | `t2` | **PARTIALLY_SUPPORTED** | **MIXED** | **NOT_IN_SOURCE** | TIER_OK | **IN_WRONG** | OK |

## Notes per card

**[0] Central Goods and Services Tax Act, 2017** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/cbic-cgst-act.html`  
'recovery decisions' is not at the cited locator (ss.34-35); recovery is Chapter XV. Archived text is the as-enacted 2017 version: 'thirtieth day of November' 0 hits, so the time limit this claim invokes is superseded.

**[1] CGST Rules: Accounts and Records** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/cbic-accounts-records-rules.html`  
'returns' occurs 0 times in the chapter body. Neither pointer resolves: canonical_url is the HINDI rules index while the bytes are the English accounts-and-records chapter, and locator 'Rules 56 and 57' appear nowhere (the page numbers them 1 and 2). Fix both and t1 stands.

**[2] Advisory on GSTR-2B** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/gstn-gstr2b-advisory.pdf`  
GSTN is the portal operator documenting its own system, not the regulator, and the page self-disclaims: 'For information and guidance purposes only'. Cluster and URL say 2020; the file was authored 2024 and contains QRMP examples.

**[3] Invoice Management System Advisory** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/gstn-ims-advisory.pdf`  
Claim says 'mismatched' documents; source says ALL supplier records appear in IMS. Claim omits the material default: inaction is DEEMED ACCEPTANCE. Artifact has no letterhead, no signature, no date, and PDF metadata names a private practitioner as author.

**[4] Circular No. 92/11/2019-GST**  
`archives/business-knowledge/research/vt723-t4-corroboration/cbic-circular-92-11-2019.pdf`  
Genuine signed circular (F.No.20/16/04/2018-GST, s.168(1)). RELEVANCE DEFECT: it governs SALES PROMOTION SCHEMES — free samples, BOGO, secondary discounts — and says nothing about customer non-payment, yet it is attached to bk004 output-GST-on-customer-default.

**[5] MSME Samadhaan delayed-payment mechanism** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/msme-samadhaan.html`  
The whole page is FOUR SENTENCES of substance (2,577 chars, mostly nav) and contains no statutory text — it describes the MSMED Act by section reference. The Act is t1; a portal page explaining the Act is not. The card's own locator admits it: 'Portal explanation'.

**[6] Guidelines for MSE ODR Scheme**  
`archives/business-knowledge/research/vt723-t4-corroboration/msme-odr-guidelines.pdf`  
Ministry scheme instrument with binding-directions clause (s.15.1). Needs an EXPIRY: the scheme sunsets FY2026-27 by its own s.8.1. Support finding cites 'scrutiny' stages — 0 hits in 29 pages.

**[7] Account-Based Marketing for B2B** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/salesforce-account-based-marketing.html`  
Salesforce vendor blog, US case study (Valpak, Florida), zero India content. Mechanism transfers but the operating assumptions do not: separate sales/marketing/service teams, CRM, marketing automation, attribution reporting. Citation of an ambient industry definition, so the source is load-bearing for nothing.

**[8] How to Choose Target Accounts for ABM** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/hubspot-target-accounts.html`  
The claim loses the source's own step 1: 'The first (and most critical) step... is to start with your campaign goals' — fit/ICP is step 3. The 'rather than broadcasting the same pitch' contrast has 0 hits and belongs to card 7's page.

**[9] Break-even point calculation** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/sba-break-even.html`  
SUBJECT IS WRONG: card subject is 'shipping' and the artifact has freight 0, postage 0, carrier 0, dimension 0, fulfil 0, expansion 0 hits. Break-even arithmetic IS there and transfers to India; the packaging/fulfillment/channel-cost instruction was imported and dressed in the SBA's authority. One of bk019's two qualifying clusters.

**[10] Parcel size, weight and fee standards** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/usps-parcel-size-weight.html`  
Honest t1v: the carrier's own fee standards — but authority over USPS operations only. Every threshold is US-specific (inches, pounds, 108/130in length+girth, 70lb, 1,728 in3, Zones 1-9, $200 fee). Volumetric weight as a CONCEPT transfers to India; this tariff does not. The other of bk019's two qualifying clusters.

**[11] Needmining: Designing Digital Support to Elicit Needs from Social Media** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/arxiv-needmining-2101.06146.pdf`  
Genuine Design-Science study, but arXiv v1 non-peer-reviewed, and instantiated on German tweets in e-mobility only. 'unmet' 0 hits; no precision/recall reported in front matter.

**[12] From Complaint to Action: Technology-Enabled Quality Improvement from Consumer Reviews**  
`archives/business-knowledge/research/vt723-t4-corroboration/nber-reviews-quality-w34934.pdf`  
NBER working paper, staggered-adoption two-stage DiD, +0.358 stars, improvements concentrated in previously-flagged dimensions. One gloss: 'recurring themes' has 0 hits — the trigger is negative-review alerts, not a recurrence threshold.

**[13] Sampling Bias in Entrepreneurial Experiments** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/nber-sampling-bias-w28882.pdf`  
'A biased sample can mislead inference' is definitional and survives the source's removal; what falls with it is the measured 45% growth gap. Operationalised bias is gender composition of Product Hunt beta testers, not a 'narrow convenience sample' (0 hits). SALVAGEABLE at t2 if the claim is rewritten to the paper's actual finding.

**[14] Experimentation and Startup Performance: Evidence from A/B Testing** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/nber-experimentation-startup-w26278.pdf`  
The card states the paper's MOTIVATING PROPOSITION, credited by the authors to others, not their finding — and their finding partly cuts the other way: younger ventures FAIL FASTER. 'learn before committing' appears nowhere; two-way fixed effects on self-selected adoption, no causal claim by the authors.

**[15] Customer Discovery Basics** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/hbs-customer-discovery-basics.pdf`  
The bounded quanta (5 interviews per persona, 2-3 person team) fall with the source; 'do discovery before scaling' survives on reasoning. The document's gate is proceeding to a SOLUTION, not to a sales motion. HBS-branded teaching brief with zero original data — the byline must not pull it to t2.

**[16] Field Experiments in Marketing** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/nber-field-experiments-marketing.pdf`  
PROVENANCE FABRICATED: publisher 'NBER' and cluster 'study:nber-w15992' are unsupported by the bytes — NBER and 'National Bureau' return 0 hits across 30 pages, there is no NBER cover page, and the title block reads MIT Sloan / Duncan Simester. Artifact is a LITERATURE REVIEW of 61 other papers, not a study. Its action is CONTRADICTED by its own text: 'randomization is not required to publish... In 29% of the papers, experimental treatments were not assigned by randomization.'

**[17] B2B Omnichannel Guide** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/salesforce-b2b-omnichannel.html`  
Vendor content-marketing whose every recommendation resolves to a Salesforce SKU. Zero statistics, zero citations, no author, no date — and it carries a stat with the number literally missing: 'statistically times more likely to exceed expected profit'.

**[18] Go-to-Market Strategy** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/bain-go-to-market.html`  
The artifact is a consultancy's own SERVICE SALES PAGE — 'Ready to talk?', partner headshots, engagement counts. coverage 0, stage 0, maturity 0, unit economics 0 hits: the claim's operative contrast is absent from the page.

**[19] Community Identity and User Engagement in a Multi-Community Landscape** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/arxiv-community-identity-1705.09665.pdf`  
Retention half genuinely supported (283 communities); 'whether contributions are accepted' has accept 0 hits and is never studied. Publisher says 'arXiv authors'; the venue is AAAI 2017. The authors remove non-English communities, so Hindi/Hinglish are out of sample.

**[20] How Founder Motivations, Goals, and Actions Influence Early Trajectories of Online Communities** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/arxiv-community-founders-2405.00601.pdf`  
CONSTRUCT SWAP on the word 'founder': in this paper it means SUBREDDIT CREATOR. business 0, venture 0, startup 0, entrepreneur 0, customer 0 hits; market 1 hit, in a bibliography entry. The study runs the opposite direction and its nearest real finding cuts against the card. supports[0].finding cites 'venture activity' — 0 hits — so the finding text is fabricated relative to these bytes. SALVAGEABLE by rewriting the claim to what CHI actually reports.

**[21] Design and Evaluation of Personalized Free Trials**  
`archives/business-knowledge/research/vt723-t4-corroboration/arxiv-personalized-free-trials-2006.13420.pdf`  
Real field experiment (+5.59%, t=2.58). But the source scopes ITSELF out of the use it is cited for: 'easy to implement a personalized free trial policy at scale for digital services, unlike physical products' — and it is wired to bk028, a physical/service sample loop. Domain transfer blocked by the source's own words.

**[22] In-Store Free Sample Promotions and Consumer Purchasing Behavior** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/free-sample-field-study.pdf`  
Pay-to-publish venue: PDF author 'Windows User', Word 2010, a truncated heading ('HYPOTHES'), and a methodology section calling the design 'quasi-experiments' while the abstract and the card's finding both call it a field experiment. Korean grocery loyalty data; household income null for 2 of 3 products. source_title does not match the document's own title, so the canonical_url may point at a different record.

**[23] To Charge or Not to Charge** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/ipa-free-distribution-demand.html`  
The refutation on bk028, correctly modelled. But the archived bytes are the publication LANDING PAGE (abstract only, no method, no tables). The full working paper is ALREADY in the same archive directory and cited by no card: ipa-free-distribution-paper.pdf. Re-point. The page states its own null: the differences are not statistically significant.

**[24] Making Effectiveness Work** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/ipa-making-effectiveness-work.html`  
Artifact is a trade body's product LISTING page ('Buy now'), not the report. 'objectives' never appears and creative is never discussed, so the claim's contrast is absent. Verifiable only at blurb depth. Publisher collision: cards 23 and 24 both say 'IPA' for two unrelated organisations (Innovations for Poverty Action vs Institute of Practitioners in Advertising) — any dedup keyed on publisher merges a development-economics RCT with UK ad-trade marketing.

**[25] Measure What Matters** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/google-measure-what-matters.pdf`  
Google's own thought-leadership guide, PDF created 2014 — its attribution and cross-device assumptions predate consent mode, cookie deprecation and DPDP.

**[26] Firm Referrals and Business Relationships** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/nber-firm-referrals-w33082.pdf`  
The card states the arm the paper REFUTES: 'information referrals had small and insignificant effects on transactions' — only SUBSIDIZED referrals worked (+45pp). Trust is not a finding here at all (it appears as a baseline survey control); the actual trust result is in card 27's paper. Stance should be partial / qualifies false, and no co-selling is studied.

**[27] Interfirm Relationships and Performance**  
`archives/business-knowledge/research/vt723-t4-corroboration/nber-interfirm-w22951.pdf`  
Strong RCT (2,800 firms, +8.1% revenue, persisting a year). But the finding text calls it 'Independent evidence' and it is NOT: same author pair as card 26, same region of China, and card 26's paper cites this one. bk031 looks doubly corroborated while ONE research program stands behind it.

**[28] Mental Availability Is Not Awareness** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/ebi-mental-availability.html`  
Five-paragraph blog post ('News, 15 years ago'), schema.org WebPage, deferring to the book it sells: 'See chapter 12 of How Brands Grow'. The claim's operative half — that brand GROWTH depends on mental availability — is NOT_FOUND; the page only draws construct distinctions. 'category entry point' has 0 hits, and the page declines the how: 'that's another story'. Tagging it t2 is a two-tier inflation.

**[29] The Long and the Short of It** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/ipa-long-short.html`  
THE CITED REPORT WAS NEVER ACQUIRED. The bytes are an IPA BLOG POST (Alison Hoad, 13 Sep 2023, 10th-anniversary commentary closing with 'Book your ticket for EffWorks Global'), carrying an explicit 'not the opinion of the IPA' disclaimer, while canonical_url names the publications-reports slug for the Binet & Field report — which the page itself links at a THIRD slug. A resolvable-source failure under the ruling: the hash binds a blog post.

**[30] Aiming for the Goal: Contribution Dynamics of Crowdfunding**  
`archives/business-knowledge/research/vt723-t4-corroboration/nber-crowdfunding-w25881.pdf`  
Cleanest card in the set: claim is near-verbatim from the abstract, locator resolves to the Kickstarter calibration, stance and qualifies both correct.

**[31] A De-Biased Direct Question Approach to Measuring Consumers' Willingness to Pay** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/arxiv-wtp-bias-2005.11318.pdf`  
Hypothetical bias is textbook (the paper cites meta-analyses), so the claim as phrased survives the source. Worse, the card recruits the paper to justify 'prefer deposits, paid pilots' while the paper's whole contribution is REHABILITATING the cheap stated-preference survey: 'without resorting to any BDM mechanism'. qualifies should be partial.

**[32] When Does Acquisition Integration Succeed? Evidence from Inside the Integration Black Box** — **NOT LANDING-GRADE**  
`archives/business-knowledge/research/vt723-t4-corroboration/nber-ma-integration-w35074.pdf`  
Leader-experience -> integration success is real and IV-identified; 'execution capacity' is NOT this paper's tested construct (it appears as cited prior literature) and concurrent load is never tested. jurisdictions ['IN'] on a study of US public-company M&A (10-K/8-K text, US labor markets). Stance is correct — the only fully honest stance in 26-32.

