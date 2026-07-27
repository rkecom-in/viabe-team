# O8 Knowledge Intelligence Engine — Design Specification v0.2

**STATUS: DESIGN AHEAD OF BUILD — O8 PARKED. NOT RATIFIED.**
Ratification is Fazal's alone; this spec is not build authorization. Governance note: "Clau"
= the Cowork seat, renamed by Fazal 2026-07-22 (same duties: architecture + audit-after);
historical-Clau (removed 2026-07-02) is unrelated. Review chain: Codex pipeline → Clau
amendments A1–A5 → Codex additions C1–C5 → **Codex corrections R1–R7 (all accepted,
R1/R2 verified against code by Clau: `contracts.py` TENANT_SCOPED={L1,L2,…} GLOBAL={L3,L4};
`l4_corpus.py` = the Track-D-retired authored-seed loader).** 2026-07-22.

---

## 1. Purpose and boundaries

One engine, two flows sharing one governed registry: CURATION (external knowledge → validated,
versioned, per-domain corpora) and LEARNING (tenant outcomes/corrections/incidents → tenant
memory, k-gated global priors, or general candidates). Honors Track-D (LOCKED): corpora are
MUTABLE SEED the learning loop can overwrite — and per R1, this engine does NOT revive the
retired authored-playbook mechanism: the governed card registry is a NEW store occupying the
global-knowledge slot, superseding the hand-authored `l4_corpus.py` path (which stays retired).
Retrieval informs reasoning; effects still pass the deterministic gates — always.

## 2. The knowledge card (atomic unit)

- `claim` + `distillation_note` + **`claim_key` (R7: normalized claim identity — subject ×
  predicate × jurisdiction × population × channel — so conflict detection compares like with
  like, not string similarity)**
- `provenance`: source id(s) · source-class (§3) · retrieval date · publisher · **persistent
  taint flag (untrusted/derived — R5) that survives distillation**
- `independence_cluster` (C4): cards derived from one underlying study/announcement share a
  cluster id — N retellings = ONE corroboration
- `confidence` (maps to existing `EvidenceConfidence`: low/medium/high/verified — R7: the
  tier→confidence mapping is a build deliverable, not an assumption)
- `applicability` (C5): jurisdiction · size band · industry · maturity · channel · effective
  period (regulatory cards REQUIRE effective dates)
- `scope`: global | k-gated-prior (L3) | tenant (L1/L2, RLS)
- `version` + `status`: candidate → validated → **disputed (R4: visible, hedged — NOT
  auto-downgraded)** → superseded/expired/rolled-back/research-only · emergency-quarantined
- `impact record`: shadow/A-B admission evidence · ablation results · attributed incidents
- **Raw-source retention is SEPARATE from retrieval-eligible cards (R5)** — original text
  never becomes retrievable context.

## 3. Source classes and ingestion quarantine (R5 — rewritten)

Classes describe source CHARACTER, not medium (news is not automatically T4):
- **T1 regulatory/official** — government/regulator (GST, RBI). Authoritative FOR THEIR
  DOMAIN only (R4).
- **T1v vendor-policy** — Meta/Shopify official policy: authoritative about their platform
  rules; NOT a regulator.
- **T2 evidence** — peer-reviewed / WB / NBER / J-PAL; primary reporting of named studies.
- **T3 practitioner** — industry research (Baymard), reputable practitioner analyses;
  republished announcements resolve to their T1/T2 original via independence clusters.
- **T4 experiential** — forums, anonymous operator claims, unattributed news. 6mo auto-decay;
  corroboration required to leave research-only; can never outrank T1/T1v/T2 within their
  domains.

**Ingestion pipeline (R5 — `prompt_quarantine.py` is consumption-time fencing ONLY; the
ingestion quarantine is its own build):** isolated, tool-less extraction runs; raw content
fenced; strict structured-output validation; source metadata assigned DETERMINISTICALLY by
the pipeline — source text can never assign its own authority, confidence, or applicability;
taint persists onto every derived card.

## 4. Validation — tiered, capacity-real

Deterministic auto-checks (schema, applicability completeness, effective dates, claim_key +
cluster assignment) for all cards; harness-scored admission for measurable claims (§6); human
review ONLY for high-impact classes. **Per Codex-recommended policy (pending Fazal): certified
VTR pool reviews business-judgment cards; VTR review is NOT legal/regulatory interpretation —
high-impact regulatory cards stay reference-only until authoritative/counsel validation
exists.**

## 5. Retrieval policy (R3 — the full policy, previously under-specified)

Order of operations per retrieval:
1. **Hard applicability filter BEFORE ranking** — jurisdiction, effective period, business
   size/industry/channel must match the tenant context; non-matching cards are excluded, not
   down-ranked.
2. **Hybrid retrieval** — semantic (existing `embeddings.py`) + lexical + entity/claim_key.
3. **Weighted ranking** — authority (domain-scoped, §7) · evidence strength · applicability
   closeness · recency (only where time-sensitive) · source-independence (cluster-deduped).
4. **Diversity rule** — max 1–2 cards per independence_cluster in any context window; no ten
   near-identical cards.
5. **Minimum score + no-result behavior** — below threshold: retrieve NOTHING and the agent
   hedges/declines per the no-fabrication rail; never pad context with weak cards.
6. **Per-identity budgets** — every agent AND stage declares: corpus domains, layers, top-k,
   token budget (registered alongside the ACF manifest; retrieval scope is a declared
   capability). Specialist example: SR planning gets sales-domain deep cards; SR drafting gets
   copy-craft cards only.
7. **Manager vs specialist boundary (R3):** the Manager retrieves specialist CONCLUSIONS,
   business context, conflict/dispute summaries, and cross-functional evidence — never deep
   domain corpora; depth belongs to specialists, synthesis to the Manager.

## 6. Admission and demotion = measured impact + causality (A4 + R6)

- **O11 FIRST** (VT-688): the judgment baseline is the admission instrument.
- **Shadow rollout** (retrieved + logged, not injected) → A/B.
- **Graduation gate (R6-hardened):** outperforms baseline with **minimum sample sizes,
  non-inferiority margins on every safety-critical slice (money, consent, regulatory), and
  explicit confidence thresholds** — numbers set in the build brief, requirement set here.
- **Demotion requires causality (R6):** attribution logs (which cards were retrieved) select
  SUSPECTS; permanent demotion requires counterfactual replay / card-ablation — score with
  and without the card, reproduce the regression across relevant scenarios. **Emergency
  quarantine** (money/regulatory incidents) is immediate but reversible pending ablation.

## 7. Conflict resolution (R4 — rewritten)

1. **Comparability precondition:** cards are compared ONLY when claim_key dimensions overlap
   (type, jurisdiction, population, channel, effective period). No cross-domain adjudication.
2. **Authority is claim-scoped:** a GST notification is authoritative about GST requirements —
   not about whether a sales tactic works; Meta policy is authoritative about Meta rules.
   Within an overlapping claim: regulatory > vendor-policy (on their own domains) > evidence >
   practitioner > experiential.
3. **Recency wins ONLY on explicit supersession or genuinely time-sensitive evidence** — a
   newer article does not beat an older stronger study.
4. **Unresolved conflicts → status `disputed`:** both cards remain VISIBLE as disputed
   evidence; retrieving agents get the uncertainty-hedge behavior; human review queued. (Never
   auto-downgrade both — that could remove a correct official rule.)
5. Independence check precedes adjudication (same cluster = re-extract, not adjudicate).

## 8. Learning loop (R2-corrected layer mapping — privacy-binding)

Outcome attribution → lesson distillation → scope triage:
- **Tenant-specific → L1/L2 tenant memory** (TENANT_SCOPED layers, RLS).
- **Repeatable aggregate → anonymize + k-gate (REUSE VT-225 L0 k-anonymity admission) → L3
  global prior.** Per Codex-recommended policy (pending Fazal): CAPTURE candidates now, SERVE
  only when k-gate + stability pass — small cohorts never weaken the gate.
- **General lesson → candidate registry** (curation flow).
- **A5:** incidents attributed (and ablation-confirmed, §6) demote/supersede directly.
- Constitutional: no agent writes directly to trusted global knowledge.

## 9. Reuse vs BUILD (R1 + R7 — honest accounting)

**REUSE:** `knowledge/broker.py` (KnowledgeBroker contracts) · `knowledge/contracts.py`
(KnowledgeLayer scoping, KnowledgeSource, EvidenceConfidence, conflict-detection substrate) ·
`embeddings.py` · L1/L2 tenant-memory paths · k-anon admission (VT-225/196) ·
`prompt_quarantine.py` (consumption-time fencing, §3 scope honesty) · tm_audit · ACF manifests.
**EXPLICITLY NOT REUSED:** `l4_corpus.py` authored-seed loader (Track-D dead scope — the card
registry supersedes it as the global-knowledge store; no authored-playbook revival).
**BUILD (R7 — the real list):** card schema + lifecycle + impact-attribution tables ·
`ModuleResult` evidence-manifest field + adapter preservation (context.py has NO evidence
field today) · all Manager/specialist retrieval identities + budgets · tier↔EvidenceConfidence
mapping · claim_key normalization · ingestion quarantine pipeline (§3) · retrieval policy
engine (§5) · shadow/A-B harness hooks · DSR deletion + retention + PII redaction + embedding
privacy for cards · cross-tenant isolation + differencing-attack tests · **per-tenant
contribution caps so no single tenant dominates an L3 prior**.

## 10. Sequencing (accepted, Codex-refined)

1. **O11 baseline (VT-688)** — prerequisite; also identifies the worst-performing decision
   slices.
2. **Seed corpus targets those slices** (per Codex: no arbitrary card count — seed only what
   measurably improves the weak slices) → ingest as candidates → shadow.
3. First corpus version A/B vs baseline → graduate per §6 gate.
4. Learning-loop wiring with prod outcome data.
Each phase Fazal-granted separately. O8 stays PARKED until his word.

## 11. For Fazal at ratification

Codex-recommended answers (Clau concurs on all three): (1) VTR pool = business-judgment
review only; regulatory cards reference-only until counsel-grade validation; (2) capture
priors now, serve after k-gate passes — never weaken the gate for small cohorts; (3) no seed
budget number — O11's weak slices define what to seed. Plus the §6 gate numbers (sample
sizes, margins, thresholds) are set at build-brief time under your review.
