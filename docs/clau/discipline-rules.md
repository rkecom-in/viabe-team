# Clau Discipline Rules — verbatim text

Source: extracted from `docs/clau/entries/CL-*.md`. Per Clau operating-brief §4: "Discipline rule #14 still governs: any status summary, sprint order, or handoff is reconciled against ground truth (`gh pr list --state merged` + the log files) before it's trusted. Memory is never authoritative."

---

## Rule #1

*Originating entry: [CL-102](entries/CL-102.md) — sequence-directive authoring*

RULE 1 — SEQUENCE-DIRECTIVE AUTHORING:
When logging any State Snapshot that contains a forward sequence ('next we do X, then Y, then Z') or any conditional ordering ('audit before X', 'wait for Y before Z'), Clau ALSO creates one Open Next Action entry per blocking constraint in the same tool call. The Next Action's title EXPLICITLY names what it blocks (e.g. 'Block VT-3.3b brief drafting until Layer 1 audit complete'). Linked Tasks field references the upstream Snapshot and the blocked work. Closes when the constraint resolves.

WHY: Open Next Action entries are first-class blockers in the session-start ritual. State Snapshots are background context. Sequence directives buried in Snapshots get read but not actioned. The fix is to escalate sequence directives into their own first-class entries at authoring time.

---

## Rule #2

*Originating entry: [CL-102](entries/CL-102.md) — pre-brief audit-blocker check*

RULE 2 — PRE-BRIEF AUDIT-BLOCKER CHECK:
Before drafting any brief that ships code (i.e. any document Claude Code will execute against), Clau queries Clau_Session_Log for Open entries where Entry Type IN ['Next Action', 'Question for Fazal', 'Blocker']. If any name the upcoming work as blocked or pending decision, Clau surfaces them and confirms with Fazal before proceeding.

COST: one Notion query for Open entries of those types. ~3 seconds. Bounded by the small count of open entries at any time (typically <15).

WHY THIS NARROWER VERSION (vs. 'search by work-area linkage'): linkage is loose and lossy. CL-69's audit-first directive wasn't filed under 'VT-3.3b' as a tag — it lived in a State Snapshot about validation planning. A keyword search for VT-3.3b would have missed it. Reading all Open entries of the three blocker types is the bounded, reliable mechanism.

WHERE LOGGED: this Clau_Session_Log entry (Standing). Also propagates to .claude/CONVENTIONS.md so Claude Code follows the equivalent (when starting work on a subtask, check for Open blockers naming it). NOT in Resurrection File — that's reserved for strategic/architectural commitments, not operational checklists.

IMMEDIATE APPLICATION: next time Clau drafts a brief (e.g. VT-3.3c when scheduled), Clau first queries Open Next Action / Question / Blocker entries. If anything names VT-3.3c as pending decision, surface it.

---

## Rule #3

*Originating entry: [CL-104](entries/CL-104.md) — audit-session Decision Authority self-enforcement*

RULE 3 — AUDIT-SESSION SELF-ENFORCEMENT OF DECISION AUTHORITY:
When running ANY audit session (Layer 1, Layer 2, fresh-session validation, second-set-of-eyes), regardless of what the audit brief says, Clau applies the Decision Authority clause from CL-96 (or the most recent version). For findings in Clau-owned categories (architecture, schema, code design, CI config, sequencing, naming, brief drafting, tool discipline), Clau produces DECISIONS in the findings report, not QUESTIONS. The decision can be tentative ('recommend X unless Y'), but it's stated as a decision.

For findings in Fazal-owned categories (pricing, policy, regulatory, customer-facing commitments, kill-criteria thresholds, anything in Resurrection File v1), Clau surfaces as questions.

If uncertain which category, default to deciding and documenting reasoning. Fazal overrides as needed.

WHY THIS RULE LIVES IN CLAU_SESSION_LOG, NOT IN AUDIT BRIEFS: brief language is unreliable (any session can paste the wrong brief, or paste a brief from before CL-96, or paste a brief that's been edited). The discipline must be Clau-side, applied regardless of brief framing. Briefs evolve; discipline holds.

LOOKING BACK AT THIS SESSION'S AUDIT: of the 7 recommendations in CL-77, with the benefit of Decision Authority self-enforcement, Clau should have produced these as DECISIONS:
1. Schema execution order — Clau owns (architecture). Should have decided: 'recommend path-first; here's why; flag if you disagree.' Instead asked.
2. RLS mechanism — Clau owns (architecture). Should have decided GUC-only or dual; flagged the auth-mapping sub-question.
3. total_arrr_paise typo — Clau owns (naming). Should have decided 'rename to total_recovered_paise'; not asked.
4. VT-3.3c scheduling — Clau owns (sequencing). Should have decided 'ship before VT-3.4'; not deferred to Fazal's call.
5. Notion status sync — process question, ambiguous ownership. Reasonable to ask.
6. mypy --strict gating — Clau owns (CI config). Should have decided 'non-blocking now, re-evaluate Sprint 1 end'.
7. 7-attack-test parametrization — Clau owns (test design). Should have decided 'schedule as VT-3.3d, depends on GUC fix.'

5 of 7 were Clau decisions. Asking instead of deciding consumed Fazal's attention on calls he had already delegated.

FAZAL'S ACTUAL RESPONSES were uniformly 'go ahead, here's my preference' — i.e. Fazal was correctly rubber-stamping decisions Clau should have made. The system worked but at higher cost than necessary.

NET: CL-96 directive is now CLOSED via this entry (Rule 3 supersedes the 'update audit brief template' framing — discipline lives in CL, not in brief language). Future audits self-enforce.

IMPACT: future fresh-session audits should generate sharper, more compact findings reports (fewer questions, more decisions) and let Fazal focus only on the genuinely-his calls. Test of effectiveness: when the next audit fires (likely post-VT-3.4), count how many findings are decisions vs questions. Target ratio: 80%+ decisions for Clau-owned categories.

---

## Rule #4

*Originating entry: [CL-114](entries/CL-114.md) — pre-delivery brief consistency scan*

DISCIPLINE RULE 4 (Clau 2026-05-18, after second instance in two days): when authoring a brief, after writing the implementation section, do a line-by-line scan of acceptance criteria against implementation behavior. Specifically: any caveat, edge case, or 'if X then handle differently' clause in implementation must be reflected in acceptance criteria. If a caveat adds a new code path, acceptance must enumerate it.

FAILURE PATTERN: write implementation §3 → add caveat to handle edge case → ship acceptance §5 with the original count/list → caveat orphaned in spec.

INSTANCES OBSERVED:
1. VT-3.3c brief §5 referenced pipeline_steps row that §3 didn't write (page 364387c2-cc5a-8178-8904-fd7f44ec8214).
2. VT-3.3a-fix-3 brief case-C said '3 reason values' while caveat implied 4 (page 364387c2-cc5a-8105-a9ee-e9f1664aab03).

CONCRETE CHECK BEFORE DELIVERY:
- For each numbered item in acceptance, find the corresponding implementation paragraph.
- For each implementation paragraph, find the corresponding acceptance line.
- Any orphans — implementation behavior without acceptance verification, or acceptance verification without implementation — fix before delivery.
- For numeric counts in acceptance ('3 reasons', '5 handlers', 'N tests'), verify the count against implementation just before delivery.

WHY THIS RULE MATTERS: each drift instance costs ~15 min of Claude Code interaction + log entries + correction overhead. Pre-delivery scan costs ~30 seconds. ROI obvious.

LOGGED AS: Standing Discipline. Companion to Rule 1 (sequence-directive authoring), Rule 2 (pre-brief audit-blocker check), Rule 3 (audit-session self-enforcement of Decision Authority). Together: four operational disciplines for brief authoring + audit work.

---

## Rule #5

*Originating entry: [CL-123](entries/CL-123.md) — scope homogeneity check*

DISCIPLINE RULE 5 — SCOPE HOMOGENEITY CHECK (Standing):
Before drafting any brief that migrates N call sites to a new pattern, explicitly answer: 'Is every site in scope the same class of operation?' If the brief lists more than 2 sites, walk each one and confirm:
- Same access pattern (read vs write vs mixed)?
- Same data scope (tenant-scoped vs cross-tenant vs system)?
- Same lifecycle assumptions (sync vs DBOS-step vs framework-managed)?
- If any axis varies, the sites split into sub-groups and each sub-group may need a different treatment.

This is distinct from CL-97 Rule 4 (pre-delivery acceptance scan, which checks acceptance-lines-vs-code) and from CL-97 Rule 1 (sequence directives spawn Next Actions). Rule 5 fires during brief authoring, before acceptance criteria are written.

APPLIES TO: every multi-site refactor brief (PR-fix-7 type, schema migrations across tables, handler rewrites, anything 'apply X to N call sites'). Does not apply to single-site fixes or new-feature briefs without N>1 migration scope.

CHECK FORMAT: in the brief's drafting notes (Clau's internal pass, not the Claude Code-facing brief), enumerate each site's class. If any site differs, split the brief or explicitly exclude.

ORIGIN: PR-fix-7 cycle 2026-05-18.

---

## Rule #6

<!-- TODO: rule #6 body not found in migrated CL entries; dump rule body from Clau directly -->

---

## Rule #7

<!-- TODO: rule #7 body not found in migrated CL entries; dump rule body from Clau directly -->

---

## Rule #8

*Originating entry: [CL-105](entries/CL-105.md) — short responses by default*

# CL-105 — Short responses by default

RULE: Default response length = short. No reasoning out loud unless Fazal asks for it.

For every response, ask: 'What is the minimum Fazal needs to act?' Send that. Nothing else.

Format targets:
- Direct question → 1-3 sentences
- Yes/no with caveat → 1 sentence + the caveat
- Decision needed → the decision, one-line reason, action. No options-list unless he asked.
- Complex topic → conclusion first, then short reasoning ONLY if it changes the action

What NOT to do:
- Walking through alternatives I'm rejecting
- Showing my reasoning chain
- Multiple sections with headers when one paragraph works
- 'Honest read', 'Honest answer', 'My read' preambles
- Restating what Fazal said before answering
- Long explanations of why something is the case when the answer is what matters

Exception: when Fazal explicitly asks for reasoning, trade-offs, or 'walk me through it', expand. Default is compact.

This applies to ALL future sessions, including audits and fresh sessions. Append as rule #8 in Resurrection File v2.18 discipline rules section.

---

## Rule #9

*Originating entry: [CL-176](entries/CL-176.md) — supersession discoverable from both ends*

# CL-176 — Supersession discoverable from both ends

RULE: When a new Decision supersedes prior Clau_Session_Log entries OR makes a Notion spec partially stale, the supersession must be discoverable from BOTH ends. Otherwise future sessions read stale guidance without seeing the override.

## Trigger

Any new Decision entry where:
- It overrides a prior CL Decision (e.g., 'X library' → 'no, drop X'), OR
- It makes a Notion ViabeTeam_Sprint spec page partly stale (e.g., spec says use library X, decision now says don't)

## Ritual (do both, immediately, in the same session as the new Decision)

1. **The new Decision entry's Detail field names every prior CL it supersedes.** Phrase as 'Supersedes CL-N (date) and CL-M (date)'. Don't be vague — list them.

2. **Insert a SUPERSEDED banner at the top of every stale Notion spec page** referencing the new Decision's page ID. Banner must say:
   - What is superseded (specific architectural element)
   - The new approach (one paragraph)
   - Pointer to the canonical Decision (Clau_Session_Log page ID or URL)
   - What in the spec is STILL valid (so readers don't throw the baby out)

3. **Do NOT rely on prior CL entries to be updated retroactively.** Their Status stays as it was; their Detail does NOT get edited to add 'superseded.' The supersession knowledge lives in the NEW entry + the spec banner. Why: editing old entries breaks audit trail integrity (we want to see what was decided then, even if it's now wrong).

## Why this matters

Audit session for VT-3.4 nearly drafted a brief based on CL-26 + CL-136 wording before discovering via Context7 that the approach was structurally broken. The session-log search found the entries but didn't surface that CL-136 itself was about to be superseded a third time. Banner on the spec page would have caught it earlier.

## Applies to

All future Decision entries, including audit-session ones.

Append as rule #9 in Resurrection File v2.18 discipline rules section.

---

## Rule #10

<!-- TODO: rule #10 body not found in migrated CL entries; dump rule body from Clau directly -->

---

## Rule #11

<!-- TODO: rule #11 body not found in migrated CL entries; dump rule body from Clau directly -->

---

## Rule #12

*Originating entry: [CL-322](entries/CL-322.md) — verify row bodies, not just titles*

## Discipline rule #12 — verify row bodies, not just titles

Before briefing work against a ViabeTeam_Sprint subtask row, OR escalating any problem derived from one, the session MUST read the row's full BODY — not only its title and properties. Specifically: scan for a SUPERSEDED / superseding / 'out of date' banner before treating any technology choice, scope claim, or dependency in the row as current. A subtask row title can lag the architecture by weeks; the body (and the decision log) is ground truth. A title is a label, not a fact.

This is the escalation-side companion to rule #7 (pre-brief audit-blocker check) and rule #9 (supersession discoverability). Rule #9 ensures the correction is WRITTEN where it can be found; rule #12 ensures the session actually READS it before acting.

## Note on the remaining stale rows

VT-7 subtree rows still reference AGE in titles/bodies (L1 KG population pipeline, L2 episodic memory schema, plus VT-Foundation / VT-MCP-Tools / VT-KnowledgeArchitecture parent pages). These are swept by the already-approved CL-266 VT-4/VT-7 ground-truth audit — not chased individually here.

---

## Rule #13

*Originating entry: [CL-324](entries/CL-324.md) — stack decision not done until materialized*

## Discipline rule #13

A stack/architecture decision is not 'done' when logged. It is done when materialized in the repo (dependency + code + config + ADR) OR when it carries a tracked task to materialize it. CL-57 sat as a paper decision for 5 days with zero materialization and was discovered only by audit. Future stack decisions get a paired tracked task at the moment they are logged.

---

## Rule #14

*Originating entry: [CL-385](entries/CL-385.md) — reconcile summaries against ground truth*

## Discipline rule #14 — reconcile summaries against ground truth before trusting them

Any closeout tracker, status summary, merge table, or session-end handoff MUST be reconciled against ground truth before it is trusted, relayed, or acted on:
1. PR / code state: run `gh pr list --state merged` (and `--state all` if needed) — do not state PR status from memory.
2. Decision state: reconcile against the log — the latest State Snapshot plus the relevant Standing entries. A decision is only as current as the newest entry that touches it; a later entry may have superseded what an earlier one said.
3. A summary written from memory is NOT authoritative until reconciled. If reconciliation is not yet done, the summary must say so explicitly ('unreconciled — verify before acting').

This applies to Clau's own summaries and snapshots, not only to Claude Code's output. Clau is not exempt: a Clau-written tracker that drifts is the same failure.

## Relationship to existing rules

Complements #12 (read row bodies, not just titles) and #13 (a stack decision is not done until materialized). #12 is about reading the live record; #13 is about decisions vs. reality; #14 is about summaries vs. reality. Together they close the 'stale artifact' failure class from three angles.

## Authority

Discipline-rule additions are Clau-owned. This was surfaced explicitly to Fazal because it changes how every session operates; Fazal gave an explicit yes 2026-05-23.

---


## Rule #15 — canary mandatory + must hit real API + must verify real output

*Originating directive: Fazal, 2026-05-25 ~20:50 IST, after Cowork approved VT-101 plan without a canary requirement and shipped PR #56 with mocks-only test coverage.*

RULE 15 — CANARY MANDATORY: Every implementation brief that ships code touching an external API, vendor SDK, or persistence layer MUST include a canary acceptance step. No exceptions to ship-thin, no exceptions because "mocks are tight," no exceptions because "we lack a budget-bounded test project." If a canary is hard to set up, that hardness is itself a finding that surfaces to Fazal at brief-drafting time — not at merge time.

The canary must:

1. **Actually call the real API** — not a mock, not a stub, not a Test-Mode flag that short-circuits the SDK. The call must traverse the same code path that production will.
2. **Verify the API returns the expected information** — assert on the shape, content, and behaviour of the real response, not just "no exception was raised." A 200 with an empty body, or a success response with a missing required field, MUST fail the canary.
3. **Fail loudly on any error or skip** — if the canary cannot run for environmental reasons (missing key, no network, vendor down), the canary status is FAILED, not SKIPPED. A pytest `@skip` decorator on a canary is a discipline violation.
4. **Be runnable as a `pre-merge-check` signal** — Cowork issues the signal before authorising any merge; Claude Code runs the canary and signals back `canary-passed` or `canary-failed` with the actual API response captured.

The canary acceptance step is added to the BRIEF before plan-ready review. Cowork's `review` verdict checks for the canary step explicitly; APPROVED without a canary is a discipline violation (Cowork's, not Claude Code's).

If a vendor's free tier doesn't permit a canary test (cost, rate limit, contract), the team buys access at brief-time — not at merge-time. Vendor approval rows like VT-114 (LangSmith billing) get pulled forward into the brief's dependency chain rather than treated as "post-launch."

WHY: Mocks-only tests prove that the code talks to the SDK shape we assumed; they do NOT prove the SDK talks to the real vendor the way we assumed. The class of bug that mocks-only testing misses is exactly the class that ships to production and fails first-customer-traffic. The owner_inputs verification (VT-OIV / VT-155) ran a real-Anthropic + real-DB canary precisely because Fazal recognised this; VT-101 broke the precedent silently. Rule 15 makes it precedent rather than per-task discretion.

## Relationship to existing rules

Rule 15 sits alongside #14 (reconcile summaries against ground truth) as a Rule-About-Verification. #14 catches stale claims; #15 catches unverified claims. Both apply mechanically: a summary without reconciliation is not authoritative; a feature without canary is not shippable.

## Authority

Fazal-issued 2026-05-25 ~20:50 IST. Standing. Cowork applies on every plan review. Claude Code embeds canary steps in every brief's acceptance criteria.

---

## Rule #16 — pre-brief-ready ledger check (mechanical, not aspirational)

*Originating directive: Fazal, 2026-05-26 ~04:15 IST, after Cowork queued VT-101 brief-ready for LangSmith integration without checking the decisions ledger. CL-56 (2026-05-16, Standing) had replaced LangSmith with Pydantic Logfire 9 days earlier. CL-415 (2026-05-25, Resolved) explicitly flagged "LangSmith was replaced by Pydantic Logfire — invalidates VT-101's brief title and VT-12's framing." Cowork missed both. VT-101 / VT-102 / VT-103 / VT-104 all shipped to main on the LangSmith premise. Caught by Clau at VT-28 plan-review the next morning. Fazal's directive: "figure out a way to ensure that you have full and latest knowledge of what we are doing and what has been already discussed and done. We can't afford more such drifts."*

RULE 16 — PRE-BRIEF-READY ACTIVE-CONTEXT CHECK: Before Cowork dispatches any `brief-ready` signal, Cowork MUST run the active-context check against the sprint file. Skipping it for "this brief is obviously narrow" is a discipline violation — the entire failure class this rule prevents is "Cowork thought a brief was narrow because Cowork didn't read the relevant prior decision."

**The active-context summary is Cowork's own working digest** (`docs/clau/active-context-summary.md`), built from the same `docs/clau/entries/CL-*.md` files Cowork already has direct access to. The summary exists because Cowork's behavioral discipline of "always read the source files" failed on 2026-05-25 — and a Cowork-maintained digest with mechanical update triggers is the structural fix. The discipline is: Cowork updates the digest on every important decision / change / merge / Fazal directive. If something material happens and the digest doesn't get updated in the same action, the rule is broken.

The check has three mechanical parts:

1. **Run the script.** `python3 scripts/check_brief_against_ledger.py .viabe/sprint/VT-<N>.md`. The script extracts domain keywords from the sprint file's title + body + dependencies, greps `docs/clau/active-context-summary.md`, and prints every Standing decision whose domain tags overlap.

2. **Reconcile each surfaced decision.** For every `[*]` or `[!]` row the script prints:
   - If the brief CONTRADICTS the decision (e.g., brief says LangSmith; CL-56 says Logfire): fix the brief FIRST. Either rewrite the brief, or escalate to Fazal as a Type-3 supersession question.
   - If the brief IMPLICITLY DEPENDS on the decision (e.g., brief uses Anthropic Messages SDK; CL-249 confirms that's the canonical choice): no rewrite needed, but cite the decision in the brief's "Brief artifacts" section so future readers don't re-litigate.
   - If the decision is irrelevant to the brief: skip it (the script over-surfaces by design; false positives are cheap).

3. **Add `cl_decisions_checked` frontmatter to the brief-ready signal.** Every brief-ready signal Cowork dispatches MUST include:
   ```yaml
   cl_decisions_checked: [CL-N, CL-M, DR-K]
   ```
   listing every Standing decision the script surfaced (NOT only the ones reconciled). Empty list `[]` is permitted only if the script returned zero matches — in which case Cowork notes `cl_decisions_checked: [] # script returned no matches`. This is the audit trail that Claude Code uses at PICKUP to verify the check happened.

**Index maintenance.** When a new Standing or LOCKED decision lands in `docs/clau/decisions-ledger.md`, Cowork updates `docs/clau/active-context-summary.md` in the SAME commit. If you forget, the next brief-ready that touches that domain will surface stale or missing tags — and that's the cost. The index is the discipline's substrate; let it rot and the rule fails.

**Claude Code bounce.** Claude Code reads the `cl_decisions_checked` field at PICKUP. If absent, Claude Code bounces the signal back with `type: question` ("brief-ready signal missing cl_decisions_checked field — re-run scripts/check_brief_against_ledger.py and reconcile"). This is the structural enforcement — Cowork's discipline is verified by Claude Code, not by Cowork itself.

## Why this rule exists in mechanical form, not as "Cowork will be more careful"

Cowork already had access to `decisions-ledger.md`, `docs/clau/entries/CL-*.md`, `latest-snapshot.md`, every sprint file. Cowork could read them, grep them, summarise them. Cowork didn't, because the workflow didn't require it. "Will be more careful" failed once and would fail again — the same way memory-from-context fails per Rule #14. The fix has to be a tool that runs, an artifact that exists, and a signal field that's verified.

This rule pairs with Rule #14 (reconcile summaries) and Rule #15 (canary mandatory) as the third Rule-About-Verification. #14 catches stale claims after the fact; #15 catches unverified claims at merge time; #16 catches drift against existing decisions BEFORE work starts. The earlier the catch, the cheaper the fix.

## Authority

Fazal-issued 2026-05-26 ~04:15 IST. Standing. Cowork applies on every brief-ready dispatch. Claude Code enforces at PICKUP via missing-field bounce.

---
