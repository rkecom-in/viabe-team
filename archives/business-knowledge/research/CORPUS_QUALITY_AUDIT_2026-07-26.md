# Business Knowledge Corpus Quality Audit — 2026-07-26

## Audit standard

A card belongs in RAG only when it provides a distinct decision mechanism, situation boundary, evidence request, failure condition, and next action. Related cards may coexist when they answer different decisions; cards are retired when the retrieval result would tell the agent substantially the same thing without adding a different control or mechanism.

The audit compared titles, tags, situations, risks, actions, sources, trust levels, and agent routing across all 109 cards. A token/tag similarity screen surfaced candidate pairs; each candidate was then reviewed manually. Similarity alone was not used to delete a card.

## Retired and consolidated

| Retired card | Surviving card(s) | Reason | Preserved learning |
|---|---|---|---|
| BK003 — bad-debt credit note | BK004 | Same tax/collections/accounting separation and overlapping triggers. | BK004 now classifies dispute, return/deficiency, waiver/discount, and true bad debt; separates recovery, write-off, and CA-reviewed GST treatment; adds demand/MSME evidence. |
| BK022 — digital marketing amplifies the model | BK089 | Medium-trust discussion was superseded by stronger randomized advertising evidence and the same readiness gate. | BK089 now requires contribution after shipping/returns/discounts, AOV, repeat purchase, fulfilment, CAC ceiling, and promise-proof fit. |
| BK023 — local trust before broad paid ads | BK016 and BK089 | Repeated listing/review/readiness advice already supported by official guidance and field evidence. | BK016 now includes ticket contribution, repeat interval, geographic radius, capture method, and CAC ceiling before local paid reach. |
| BK069 — resource-allocation inertia | BK056 | Same McKinsey source, situation, and quarterly reallocation action. | BK056 now adds risk, learning, switching cost, time horizon, quarterly review, and protection against short-term churn. |

Corpus after consolidation: 105 active cards.

## Historical-case admission round

Thirteen cases (BK110–BK122) were added after consolidation, bringing the active corpus to 118 cards. They cover growth-stage hiring, bootstrapped Indian growth, Indian reporting and acquisition failures, consumer/service turnarounds, business-model migration, customer migration, international rollout, duration/liquidity mismatch, concentration risk, operational recovery, and consent/incentive failure.

The case index records the chronology, mechanism, executional learning and inference limit. Nine source artifacts are locally archived and hash-verified; five manifest rows are explicitly marked `live_link_only` because the publisher blocked or did not expose a stable automated download. Those cards point to the local synthesis and never claim that it is the source original.

A second token/TF-IDF similarity screen compared every new card with the full corpus. The highest candidate score was 0.183 (BK111 versus BK089); manual review retained both because BK089 governs paid-channel readiness while BK111 governs customer-interest constraints, referral economics and incentive design. Other leading candidates were lower and had different controls (for example, trade-credit shock versus concentration/duration stress, and feedback-loop governance versus product-proof relaunch). No new card was retired.

Active generated outputs after the second audit: 118 JSONL rows, 118 generated card Markdown files, five agent indexes, no duplicate IDs, and no retired-card output.

## Similar but intentionally distinct

| Cards | Boundary that justifies both |
|---|---|
| BK012 and BK090 | BK012 prevents Google Business Profile policy misrepresentation; BK090 evaluates listing presence as acquisition infrastructure using a natural experiment. |
| BK013 and BK024 | BK013 creates a broad named prospect/referral operating list; BK024 tests a founder's ICP and pain hypothesis through a narrow Dream-10 discovery process. |
| BK043 and BK061 | BK043 structures the after-action conversation; BK061 requires the lesson to alter a future SOP, control, training artifact, or automated gate. |
| BK045 and BK066 | BK045 is the Manager's cross-functional net-benefit test; BK066 detects process/KPI proxies that have lost connection to customer or business outcomes. |
| BK074 and BK095 | BK074 governs realized-price exceptions; BK095 governs bounded causal price learning. |
| BK105 and BK106 | BK105 establishes preventive cyber controls; BK106 commands evidence preservation, reporting, recovery, and reconciliation during an incident. |

## Generator correction

The generator previously left stale numbered Markdown files after a card was retired or inserted. Because numeric prefixes shift, the directory could contain active and obsolete copies even when JSONL counts were correct. Generation now deletes only stale files matching its exact generated-card pattern (`NNN-bkNNN-kebab.md`) and preserves README or manually maintained files.

## Continuing admission rules

- News coverage alone is discovery evidence, not sufficient RAG grounding when a primary filing, regulator report, court record, investigation, or operator post-mortem is available.
- A growth/failure case must state the decision mechanism and disconfirm common myths; company fame is not value.
- Reported effect sizes stay tied to the observed company, market, and period.
- Fraud, compliance, and insolvency cases require qualified current escalation and must not become generalized legal advice.
- The Manager receives the governance decision and escalation trigger; specialists receive the execution method.
