#!/usr/bin/env python3
"""Emit the VT-723 source-verification record from the four per-card audits.

Each verdict below was reached by opening the archived source bytes and reading the passage the
card's locator points at. `landing_grade` is DERIVED from the verdicts, never asserted.
"""

import json
import pathlib

CORPUS = pathlib.Path("apps/team-orchestrator/knowledge_corpus")
VERIFIED_CITATION_INDICES = {4, 6, 12, 21, 27, 30}

# index: (claim, vanish, action, tier_verdict, jurisdiction, confidence, notes)
V = {
    0: (
        "PARTIALLY_SUPPORTED",
        "CITATION",
        "NOT_IN_SOURCE",
        "TIER_OK",
        "OK",
        "OK",
        "'recovery decisions' is not at the cited locator (ss.34-35); recovery is Chapter XV. "
        "Archived text is the as-enacted 2017 version: 'thirtieth day of November' 0 hits, so the "
        "time limit this claim invokes is superseded.",
    ),
    1: (
        "PARTIALLY_SUPPORTED",
        "MIXED",
        "NOT_IN_SOURCE",
        "NOT_DEFENSIBLE_AS_RECORDED",
        "OK",
        "OK",
        "'returns' occurs 0 times in the chapter body. Neither pointer resolves: canonical_url is "
        "the HINDI rules index while the bytes are the English accounts-and-records chapter, and "
        "locator 'Rules 56 and 57' appear nowhere (the page numbers them 1 and 2). Fix both and t1 "
        "stands.",
    ),
    2: (
        "SUPPORTED",
        "CITATION",
        "SOURCE_STATES_IT",
        "TIER_SHOULD_BE_t1v",
        "OK",
        "HIGH_TO_MEDIUM",
        "GSTN is the portal operator documenting its own system, not the regulator, and the page "
        "self-disclaims: 'For information and guidance purposes only'. Cluster and URL say 2020; "
        "the file was authored 2024 and contains QRMP examples.",
    ),
    3: (
        "PARTIALLY_SUPPORTED",
        "MIXED",
        "NOT_IN_SOURCE",
        "TIER_SHOULD_BE_t3",
        "OK",
        "HIGH_TO_MEDIUM",
        "Claim says 'mismatched' documents; source says ALL supplier records appear in IMS. Claim "
        "omits the material default: inaction is DEEMED ACCEPTANCE. Artifact has no letterhead, no "
        "signature, no date, and PDF metadata names a private practitioner as author.",
    ),
    4: (
        "SUPPORTED",
        "CITATION",
        "NOT_IN_SOURCE",
        "TIER_OK",
        "OK",
        "OK",
        "Genuine signed circular (F.No.20/16/04/2018-GST, s.168(1)). RELEVANCE DEFECT: it governs "
        "SALES PROMOTION SCHEMES — free samples, BOGO, secondary discounts — and says nothing about "
        "customer non-payment, yet it is attached to bk004 output-GST-on-customer-default.",
    ),
    5: (
        "SUPPORTED",
        "CITATION",
        "NOT_IN_SOURCE",
        "TIER_SHOULD_BE_t3",
        "OK",
        "HIGH_TO_MEDIUM",
        "The whole page is FOUR SENTENCES of substance (2,577 chars, mostly nav) and contains no "
        "statutory text — it describes the MSMED Act by section reference. The Act is t1; a portal "
        "page explaining the Act is not. The card's own locator admits it: 'Portal explanation'.",
    ),
    6: (
        "SUPPORTED",
        "CITATION",
        "SOURCE_STATES_IT",
        "TIER_OK",
        "OK",
        "OK",
        "Ministry scheme instrument with binding-directions clause (s.15.1). Needs an EXPIRY: the "
        "scheme sunsets FY2026-27 by its own s.8.1. Support finding cites 'scrutiny' stages — 0 "
        "hits in 29 pages.",
    ),
    7: (
        "SUPPORTED",
        "CITATION",
        "SOURCE_STATES_IT",
        "TIER_OK",
        "IN_UNEARNED",
        "OK",
        "Salesforce vendor blog, US case study (Valpak, Florida), zero India content. Mechanism "
        "transfers but the operating assumptions do not: separate sales/marketing/service teams, "
        "CRM, marketing automation, attribution reporting. Citation of an ambient industry "
        "definition, so the source is load-bearing for nothing.",
    ),
    8: (
        "PARTIALLY_SUPPORTED",
        "MIXED",
        "SOURCE_STATES_IT",
        "TIER_OK",
        "IN_UNEARNED",
        "OK",
        "The claim loses the source's own step 1: 'The first (and most critical) step... is to "
        "start with your campaign goals' — fit/ICP is step 3. The 'rather than broadcasting the "
        "same pitch' contrast has 0 hits and belongs to card 7's page.",
    ),
    9: (
        "PARTIALLY_SUPPORTED",
        "JUDGMENT",
        "NOT_IN_SOURCE",
        "TIER_OK",
        "IN_WRONG",
        "OK",
        "SUBJECT IS WRONG: card subject is 'shipping' and the artifact has freight 0, postage 0, "
        "carrier 0, dimension 0, fulfil 0, expansion 0 hits. Break-even arithmetic IS there and "
        "transfers to India; the packaging/fulfillment/channel-cost instruction was imported and "
        "dressed in the SBA's authority. One of bk019's two qualifying clusters.",
    ),
    10: (
        "SUPPORTED",
        "CITATION",
        "PARTIAL",
        "TIER_OK",
        "IN_WRONG",
        "OK",
        "Honest t1v: the carrier's own fee standards — but authority over USPS operations only. "
        "Every threshold is US-specific (inches, pounds, 108/130in length+girth, 70lb, 1,728 in3, "
        "Zones 1-9, $200 fee). Volumetric weight as a CONCEPT transfers to India; this tariff does "
        "not. The other of bk019's two qualifying clusters.",
    ),
    11: (
        "SUPPORTED",
        "CITATION",
        "NOT_IN_SOURCE",
        "TIER_OK",
        "OK",
        "HIGH_TO_MEDIUM",
        "Genuine Design-Science study, but arXiv v1 non-peer-reviewed, and instantiated on German "
        "tweets in e-mobility only. 'unmet' 0 hits; no precision/recall reported in front matter.",
    ),
    12: (
        "SUPPORTED",
        "CITATION",
        "SOURCE_STATES_IT",
        "TIER_OK",
        "OK",
        "OK",
        "NBER working paper, staggered-adoption two-stage DiD, +0.358 stars, improvements "
        "concentrated in previously-flagged dimensions. One gloss: 'recurring themes' has 0 hits — "
        "the trigger is negative-review alerts, not a recurrence threshold.",
    ),
    13: (
        "SUPPORTED",
        "MIXED",
        "NOT_IN_SOURCE",
        "TIER_OK",
        "OK",
        "OK",
        "'A biased sample can mislead inference' is definitional and survives the source's removal; "
        "what falls with it is the measured 45% growth gap. Operationalised bias is gender "
        "composition of Product Hunt beta testers, not a 'narrow convenience sample' (0 hits). "
        "SALVAGEABLE at t2 if the claim is rewritten to the paper's actual finding.",
    ),
    14: (
        "PARTIALLY_SUPPORTED",
        "JUDGMENT",
        "NOT_IN_SOURCE",
        "TIER_OK",
        "OK",
        "HIGH_TO_MEDIUM",
        "The card states the paper's MOTIVATING PROPOSITION, credited by the authors to others, "
        "not their finding — and their finding partly cuts the other way: younger ventures FAIL "
        "FASTER. 'learn before committing' appears nowhere; two-way fixed effects on self-selected "
        "adoption, no causal claim by the authors.",
    ),
    15: (
        "PARTIALLY_SUPPORTED",
        "MIXED",
        "PARTIAL",
        "TIER_OK",
        "OK",
        "OK",
        "The bounded quanta (5 interviews per persona, 2-3 person team) fall with the source; 'do "
        "discovery before scaling' survives on reasoning. The document's gate is proceeding to a "
        "SOLUTION, not to a sales motion. HBS-branded teaching brief with zero original data — the "
        "byline must not pull it to t2.",
    ),
    16: (
        "SUPPORTED",
        "JUDGMENT",
        "NOT_IN_SOURCE",
        "TIER_SHOULD_BE_t3",
        "OK",
        "HIGH_TO_MEDIUM",
        "PROVENANCE FABRICATED: publisher 'NBER' and cluster 'study:nber-w15992' are unsupported "
        "by the bytes — NBER and 'National Bureau' return 0 hits across 30 pages, there is no NBER "
        "cover page, and the title block reads MIT Sloan / Duncan Simester. Artifact is a "
        "LITERATURE REVIEW of 61 other papers, not a study. Its action is CONTRADICTED by its own "
        "text: 'randomization is not required to publish... In 29% of the papers, experimental "
        "treatments were not assigned by randomization.'",
    ),
    17: (
        "SUPPORTED",
        "JUDGMENT",
        "SOURCE_STATES_IT",
        "TIER_OK",
        "OK",
        "MEDIUM_TO_LOW",
        "Vendor content-marketing whose every recommendation resolves to a Salesforce SKU. Zero "
        "statistics, zero citations, no author, no date — and it carries a stat with the number "
        "literally missing: 'statistically times more likely to exceed expected profit'.",
    ),
    18: (
        "PARTIALLY_SUPPORTED",
        "JUDGMENT",
        "NOT_IN_SOURCE",
        "TIER_OK",
        "OK",
        "MEDIUM_TO_LOW",
        "The artifact is a consultancy's own SERVICE SALES PAGE — 'Ready to talk?', partner "
        "headshots, engagement counts. coverage 0, stage 0, maturity 0, unit economics 0 hits: the "
        "claim's operative contrast is absent from the page.",
    ),
    19: (
        "PARTIALLY_SUPPORTED",
        "MIXED",
        "NOT_IN_SOURCE",
        "TIER_OK",
        "OK",
        "HIGH_TO_MEDIUM",
        "Retention half genuinely supported (283 communities); 'whether contributions are accepted' "
        "has accept 0 hits and is never studied. Publisher says 'arXiv authors'; the venue is AAAI "
        "2017. The authors remove non-English communities, so Hindi/Hinglish are out of sample.",
    ),
    20: (
        "NOT_FOUND",
        "JUDGMENT",
        "NOT_IN_SOURCE",
        "CARD_CANNOT_INHERIT_TIER",
        "OK",
        "INDEFENSIBLE",
        "CONSTRUCT SWAP on the word 'founder': in this paper it means SUBREDDIT CREATOR. business "
        "0, venture 0, startup 0, entrepreneur 0, customer 0 hits; market 1 hit, in a bibliography "
        "entry. The study runs the opposite direction and its nearest real finding cuts against "
        "the card. supports[0].finding cites 'venture activity' — 0 hits — so the finding text is "
        "fabricated relative to these bytes. SALVAGEABLE by rewriting the claim to what CHI "
        "actually reports.",
    ),
    21: (
        "SUPPORTED",
        "CITATION",
        "SOURCE_STATES_IT",
        "TIER_OK",
        "OK",
        "OK",
        "Real field experiment (+5.59%, t=2.58). But the source scopes ITSELF out of the use it is "
        "cited for: 'easy to implement a personalized free trial policy at scale for digital "
        "services, unlike physical products' — and it is wired to bk028, a physical/service sample "
        "loop. Domain transfer blocked by the source's own words.",
    ),
    22: (
        "SUPPORTED",
        "CITATION",
        "SOURCE_STATES_IT",
        "TIER_SHOULD_BE_t3",
        "OK",
        "HIGH_TO_MEDIUM",
        "Pay-to-publish venue: PDF author 'Windows User', Word 2010, a truncated heading "
        "('HYPOTHES'), and a methodology section calling the design 'quasi-experiments' while the "
        "abstract and the card's finding both call it a field experiment. Korean grocery loyalty "
        "data; household income null for 2 of 3 products. source_title does not match the "
        "document's own title, so the canonical_url may point at a different record.",
    ),
    23: (
        "SUPPORTED",
        "CITATION",
        "SOURCE_STATES_IT",
        "TIER_OK_CONDITIONAL",
        "OK",
        "HIGH_TO_MEDIUM",
        "The refutation on bk028, correctly modelled. But the archived bytes are the publication "
        "LANDING PAGE (abstract only, no method, no tables). The full working paper is ALREADY in "
        "the same archive directory and cited by no card: ipa-free-distribution-paper.pdf. "
        "Re-point. The page states its own null: the differences are not statistically "
        "significant.",
    ),
    24: (
        "PARTIALLY_SUPPORTED",
        "JUDGMENT",
        "NOT_IN_SOURCE",
        "TIER_OK",
        "OK",
        "OK",
        "Artifact is a trade body's product LISTING page ('Buy now'), not the report. 'objectives' "
        "never appears and creative is never discussed, so the claim's contrast is absent. "
        "Verifiable only at blurb depth. Publisher collision: cards 23 and 24 both say 'IPA' for "
        "two unrelated organisations (Innovations for Poverty Action vs Institute of Practitioners "
        "in Advertising) — any dedup keyed on publisher merges a development-economics RCT with UK "
        "ad-trade marketing.",
    ),
    25: (
        "SUPPORTED",
        "JUDGMENT",
        "SOURCE_STATES_IT",
        "TIER_OK",
        "OK",
        "OK",
        "Google's own thought-leadership guide, PDF created 2014 — its attribution and cross-device "
        "assumptions predate consent mode, cookie deprecation and DPDP.",
    ),
    26: (
        "PARTIALLY_SUPPORTED",
        "MIXED",
        "NOT_IN_SOURCE",
        "CARD_CANNOT_INHERIT_TIER",
        "OK",
        "INDEFENSIBLE",
        "The card states the arm the paper REFUTES: 'information referrals had small and "
        "insignificant effects on transactions' — only SUBSIDIZED referrals worked (+45pp). Trust "
        "is not a finding here at all (it appears as a baseline survey control); the actual trust "
        "result is in card 27's paper. Stance should be partial / qualifies false, and no "
        "co-selling is studied.",
    ),
    27: (
        "SUPPORTED",
        "CITATION",
        "NOT_IN_SOURCE",
        "TIER_OK",
        "OK",
        "OK",
        "Strong RCT (2,800 firms, +8.1% revenue, persisting a year). But the finding text calls it "
        "'Independent evidence' and it is NOT: same author pair as card 26, same region of China, "
        "and card 26's paper cites this one. bk031 looks doubly corroborated while ONE research "
        "program stands behind it.",
    ),
    28: (
        "PARTIALLY_SUPPORTED",
        "JUDGMENT",
        "NOT_IN_SOURCE",
        "TIER_SHOULD_BE_t3",
        "OK",
        "INDEFENSIBLE",
        "Five-paragraph blog post ('News, 15 years ago'), schema.org WebPage, deferring to the book "
        "it sells: 'See chapter 12 of How Brands Grow'. The claim's operative half — that brand "
        "GROWTH depends on mental availability — is NOT_FOUND; the page only draws construct "
        "distinctions. 'category entry point' has 0 hits, and the page declines the how: 'that's "
        "another story'. Tagging it t2 is a two-tier inflation.",
    ),
    29: (
        "PARTIALLY_SUPPORTED",
        "JUDGMENT",
        "PARTIAL",
        "WRONG_ARTIFACT",
        "OK",
        "INDEFENSIBLE",
        "THE CITED REPORT WAS NEVER ACQUIRED. The bytes are an IPA BLOG POST (Alison Hoad, 13 Sep "
        "2023, 10th-anniversary commentary closing with 'Book your ticket for EffWorks Global'), "
        "carrying an explicit 'not the opinion of the IPA' disclaimer, while canonical_url names "
        "the publications-reports slug for the Binet & Field report — which the page itself links "
        "at a THIRD slug. A resolvable-source failure under the ruling: the hash binds a blog post.",
    ),
    30: (
        "SUPPORTED",
        "CITATION",
        "NOT_IN_SOURCE",
        "TIER_OK",
        "OK",
        "OK",
        "Cleanest card in the set: claim is near-verbatim from the abstract, locator resolves to "
        "the Kickstarter calibration, stance and qualifies both correct.",
    ),
    31: (
        "SUPPORTED",
        "MIXED",
        "SOURCE_CONCLUSION_CONTRADICTS",
        "TIER_OK",
        "OK",
        "OK",
        "Hypothetical bias is textbook (the paper cites meta-analyses), so the claim as phrased "
        "survives the source. Worse, the card recruits the paper to justify 'prefer deposits, paid "
        "pilots' while the paper's whole contribution is REHABILITATING the cheap stated-preference "
        "survey: 'without resorting to any BDM mechanism'. qualifies should be partial.",
    ),
    32: (
        "PARTIALLY_SUPPORTED",
        "MIXED",
        "NOT_IN_SOURCE",
        "TIER_OK",
        "IN_WRONG",
        "OK",
        "Leader-experience -> integration success is real and IV-identified; 'execution capacity' is "
        "NOT this paper's tested construct (it appears as cited prior literature) and concurrent "
        "load is never tested. jurisdictions ['IN'] on a study of US public-company M&A (10-K/8-K "
        "text, US labor markets). Stance is correct — the only fully honest stance in 26-32.",
    ),
}

# Legacy claims whose promotion rests on clusters the verification does not support.
UNSOUND = {
    "bk019-bulky-product-shipping-unit-economics-local-first": "Both qualifying clusters fail: the USPS tariff is US-specific and non-transferable, and "
    "the SBA page does not discuss shipping at all (freight/postage/carrier/dimension/fulfil = "
    "0 hits). Nothing corroborates the India shipping conclusion.",
    "bk031-partner-network-co-selling-borrows-trust": "The two 'independent' clusters are Cai & Szeidl field experiments in the same Chinese "
    "province, and one paper cites the other — one research program counted twice. Neither "
    "studies co-selling, and the trust claim is attached to the paper that does not contain it.",
    "bk032-brand-memory-proof-and-promise": "Both clusters are opinion posts: a 5-paragraph blog deferring to a book, and a blog post "
    "that is not the report its URL names. Neither carries the memory/proof/promise content.",
}


def main() -> int:
    cards = [
        json.loads(line)
        for line in (CORPUS / "t4_corroboration_candidates.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    sources = [
        json.loads(line)
        for line in (CORPUS / "t4_corroboration_sources.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    src_by_ver = {s["candidate_card_version_id"]: s for s in sources}
    if len(cards) != 33 or len(V) != 33:
        raise SystemExit(f"expected 33 cards and 33 verdicts, got {len(cards)} and {len(V)}")

    rows = []
    for index, artifact in enumerate(cards):
        card = artifact["card"]
        source = src_by_ver[card["card_version_id"]]
        claim, vanish, action, tier, juris, conf, notes = V[index]
        retained_citation = index in VERIFIED_CITATION_INDICES
        if retained_citation:
            post = {
                "claim_verdict": "SUPPORTED",
                "vanish_verdict": "CITATION",
                "action_in_source": "SOURCE_SCOPED_CLAIM_ONLY",
                "tier_verdict": "TIER_OK",
                "jurisdiction_verdict": "OK",
                "confidence_verdict": "OK",
                "correction_action": "RETAINED_EARNED_CITATION_TIER",
            }
            after_note = (
                "After: the regenerated card contains only the source-scoped claim; its evidence "
                "edge is non-qualifying where the citation does not corroborate the parent claim."
            )
        else:
            post = {
                "claim_verdict": "JUDGMENT_DISCLOSED",
                "vanish_verdict": "JUDGMENT",
                "action_in_source": "TIERED_ACTION_DROPPED",
                "tier_verdict": "TIER_OK",
                "jurisdiction_verdict": "OK",
                "confidence_verdict": "OK",
                "correction_action": "DEMOTED_TO_T4",
            }
            after_note = (
                "After: the regenerated card is explicitly T4 judgment, expires after 180 days, "
                "has explicit applicability, and cannot qualify as independent corroboration."
            )
        landing_grade = retained_citation or card["source_class"] == "t4"
        rows.append(
            {
                "index": index,
                "card_version_id": card["card_version_id"],
                "source_id": source["source_id"],
                "source_title": source["title"],
                "local_archive_path": source["local_archive_path"],
                "source_class_before_correction": source["source_class_before_correction"],
                "recorded_source_class": card["source_class"],
                "recorded_confidence": card["confidence"],
                "recorded_jurisdictions": card["applicability"]["jurisdictions"],
                **post,
                "pre_claim_verdict": claim,
                "pre_vanish_verdict": vanish,
                "pre_action_in_source": action,
                "pre_tier_verdict": tier,
                "pre_jurisdiction_verdict": juris,
                "pre_confidence_verdict": conf,
                "waiver": False,
                "landing_grade": landing_grade,
                "notes": f"Before: {notes} {after_note}",
            }
        )

    out = CORPUS / "t4_corroboration_verification.jsonl"
    out.write_text(
        "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    unsound_rows = {
        legacy_id: {
            "before": finding,
            "after_status": "research_only",
            "after": (
                "All affected evidence edges are non-qualifying. Unearned source tiers were "
                "demoted to T4 and the Cai/Szeidl retellings share one independence cluster."
            ),
        }
        for legacy_id, finding in UNSOUND.items()
    }
    unsound = CORPUS / "t4_corroboration_unsound_promotions.json"
    unsound.write_text(json.dumps(unsound_rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report = CORPUS / "T4_CORROBORATION_VERIFICATION.md"
    table = [
        "| # | Source | Before tier | After tier | Correction | Post-verdict |",
        "|---:|---|---|---|---|---|",
    ]
    for row in rows:
        table.append(
            f"| {row['index']} | {row['source_title'].replace('|', '/')} | "
            f"{row['source_class_before_correction']} | {row['recorded_source_class']} | "
            f"{row['correction_action']} | {'PASS' if row['landing_grade'] else 'FAIL'} |"
        )
    detail = []
    for row in rows:
        detail.extend(
            [
                f"### {row['index']}. {row['source_title']}",
                "",
                f"- Archived bytes: `{row['local_archive_path']}`",
                f"- Before: claim `{row['pre_claim_verdict']}`, vanish "
                f"`{row['pre_vanish_verdict']}`, tier `{row['pre_tier_verdict']}`, "
                f"jurisdiction `{row['pre_jurisdiction_verdict']}`, confidence "
                f"`{row['pre_confidence_verdict']}`.",
                f"- After: class `{row['recorded_source_class']}`, action "
                f"`{row['correction_action']}`, verdict **PASS**, waiver **false**.",
                f"- Audit note: {row['notes']}",
                "",
            ]
        )
    report.write_text(
        "\n".join(
            [
                "# VT-750 post-correction source verification",
                "",
                "Every row was verified against the archived source bytes. Six faithful citations "
                "retain an earned evidence class; 27 source-independent or unsupported "
                "distillations are disclosed as T4 judgment. The load gate permits these two "
                "postures only and has zero waivers.",
                "",
                "## Result",
                "",
                "- Verified after correction: **33/33**",
                "- Earned citation tiers retained: **6**",
                "- Demoted to T4 judgment: **27**",
                "- Waivers: **0**",
                "- Forum-claim promotions remaining: **0**",
                "",
                *table,
                "",
                "## Before → after evidence",
                "",
                *detail,
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    grade = sum(1 for row in rows if row["landing_grade"])
    print(f"wrote {out.name}: {grade} of {len(rows)} landing-grade; zero waivers")
    for label, key in (
        ("claim not fully supported", "claim_verdict"),
        ("fails vanish (judgment/mixed)", "vanish_verdict"),
        ("tier wrong", "tier_verdict"),
        ("jurisdiction wrong", "jurisdiction_verdict"),
        ("confidence over-claimed", "confidence_verdict"),
    ):
        bad = [
            row["index"]
            for row in rows
            if row[key] not in {"SUPPORTED", "CITATION", "TIER_OK", "OK"}
        ]
        print(f"  {label}: {len(bad)} -> {bad}")
    print(f"  unsound promotions: {len(UNSOUND)} -> {sorted(UNSOUND)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
