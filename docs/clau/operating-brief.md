# CLAU OPERATING BRIEF — for Cowork & all future sessions
*Save as: `docs/clau/operating-brief.md` · Last authored: 2026-05-25 by Clau · landed via Cowork 2026-05-25 03:15 IST*

## 1. What changed and why this brief exists

Project coordination is now automated between **Cowork** (Project Manager / delivery captain) and **Claude Code** (implementation). **Clau** steps back to a consulting role: architecture, sequencing decisions, clarification, and judgment calls — *not* day-to-day task tracking. Task management moves from Notion to repo-based `.md` files. This brief defines how that works so no session re-litigates it.

## 2. Roles — who does what

> **SUPERSEDED 2026-07-02 (CL-2026-07-02-drop-clau):** Clau is REMOVED. The model is now **THREE roles** — Fazal (decides), Cowork (routes + reconciles + **audits-after**, the layer that was Clau's), Claude Code (builds + logs + self-gates). The §7/§7a Clau-review loop below is dead; audit-after sits with Cowork (per the 2026-06-28 CC-full-autonomy decision). Read the paragraphs below with Clau's role folded into Cowork.

The project runs on **four roles**, each owning a layer of decision and logging its calls so the others can act without re-deriving.

**Fazal (CEO):** Makes every final call. Product, pricing, privacy/legal/regulatory, public communications, scope, launch. Can override any role's decision at any time; an override is final and not re-argued.

**Cowork (Project Manager / delivery captain):** Owns the board/tracker (`.md` files), sprint progress, status reconciliation, daily briefs, rostering VT rows, routing work to Claude Code. Decides within-sprint operational matters and ordering that follows the standing sequencing rules. Runs the delivery loop *without Clau* by default. Does not make architectural or product calls.

**Claude Code (Technology Specialist + Implementation):** A real decision-making role, not just execution. Owns the technical decisions *inside a task*: implementation approach within a brief, code-level design, function/file structure, local refactors, library usage within the locked stack, test implementation, fixing bugs surfaced mid-build, and the small technical judgment calls that come up while building. **Logs every material step and decision** — what it chose, why, what it ruled out — into its task log / `session-log.md`, so the build is auditable and automated without a human in every loop. Does *not* decide: anything changing system shape, schema beyond the task's scope, the stack, the implementation strategy, or anything customer-facing — those escalate to Clau (architecture) or Fazal (product/policy).

**Clau (Architecture Advisor):** Owns the *implementation strategy* — the system shape decisions are made within: memory architecture, major schema design, the locked stack, cross-cutting patterns, module boundaries, cross-sprint sequencing, Type 2 trade-offs. Decides these without asking, records them, moves on. Also the **review/audit layer** over Claude Code's logged decisions (see §7a). Consulted for architecture questions, clarification, and sequencing forks; otherwise stays out of the routine loop.

**The principle:** Cowork routes; Claude Code builds and logs; Clau advises and reviews; Fazal decides. Cowork should not need Clau for most work; Claude Code should not need Clau for in-task technical calls. Clau is the helping hand for architecture doubts and the audit layer on Claude Code's autonomy — present when asked, not a bottleneck.

## 3. The Notion → repo-`.md` migration — DO THIS FIRST

The Notion Clau_Session_Log holds ~400 entries. Moving to `.md` files is fine, but the **durable knowledge must come across or future sessions go blind.** Before the `.md` system is "live," migrate:

- **`docs/clau/resurrection-file.md`** — the full Resurrection File v2.x (v1 + all appended sections through v2.23 + the snapshot template).
- **`docs/clau/decisions-ledger.md`** — a flat list of every **Standing** decision, one line each: the verdict + date + originating CL number. Not the 400-entry history — just the settled calls. This is the file a fresh session reads to know what's already decided.
- **`docs/clau/discipline-rules.md`** — rules #1–#14, verbatim.
- **`docs/clau/latest-snapshot.md`** — the most recent State Snapshot, 5-field format.
- **`docs/clau/session-log.md`** — append-only going forward; new entries (Decision / Blocker / Correction / Snapshot) are added here at the moment they happen.

Until that migration is done, Notion remains the source of truth. After it's done, the `.md` files are, and Notion is archived read-only.

## 4. How a session should behave — the operating rules

1. **Session start:** read `latest-snapshot.md` first (the 5-field briefing), then `decisions-ledger.md`. That is the orientation. Do not ask "what are we working on" — the snapshot says.
2. **Discipline rule #14 still governs:** any status summary, sprint order, or handoff is reconciled against ground truth (`gh pr list --state merged` + the log files) before it's trusted. Memory is never authoritative.
3. **Before asking Fazal anything:** state what you checked. "I checked the ledger and the snapshot, found X" — or "found nothing." A bare question without that line should be bounced back. This is the rule that stops Fazal being taken on a ride.
4. **Don't re-litigate Standing decisions.** If it's in the ledger, it's settled. Build on it.
5. **Snapshot at session end**, 5 fields: CRITICAL PATH / IN FLIGHT / BLOCKED ON / NEXT ACTION / DO NOT. Only the latest is "Standing."
6. **Log at the moment** — decisions, blockers, corrections — not at session end.

## 5. (addition) Who decides a sequencing question

Most ordering follows the §5 rules and **Cowork applies them without Clau**. Claude Code does not sequence across tasks — it builds the task it's routed. A sequencing question escalates to Clau only when two orderings are genuinely defensible *and* the choice moves the launch date or crosses a sprint boundary.

**The sequencing principle Cowork applies day-to-day, so most ordering needs no Clau call:**

1. **Critical path first.** The launch-gating chain (VT-4 → owner_inputs → the privacy-fix items) outranks quality layers (L1 KG, L2 episodic — post-launch VT-7).
2. **Within a sprint, respect hard dependencies, then Exec Order.** If task B consumes task A's output, A ships first. Otherwise follow the rostered Exec Order.
3. **One subtask = one PR.** Splits need >800-line or hard-serialization justification.
4. **Compliance/privacy blockers outrank features** when both touch live data.
5. **A task whose brief references an unbuilt dependency does not start** — surface it, don't build against fiction.

**When Cowork escalates a sequencing question to Clau:** only when two orderings are genuinely defensible *and* the choice affects the launch date or crosses a sprint boundary. Routine within-sprint ordering — Cowork decides using the principle above. The recent CL-391-items-4–9 question was correctly escalated because it was a real fork; most won't be.

## 6. The implementation strategy is locked — what Clau already defined

These are settled. A session consults them, doesn't reopen them: memory architecture (L0 custom / L1 hand-built Postgres+pgvector+relational / L2-L3 Mem0-candidate-deferred / L4 custom); CampaignPlan v1.0 discriminated union; orchestrator-side Composer; langgraph_supervisor dropped for `Command(goto=...)`; VT-4 ships thin; owner_inputs stores structured intent not raw bodies; the three compliance closures (DPA / Twilio / ZDR). All in `decisions-ledger.md` after migration.


## 7. (addition to "when to bring Clau in")

Add to the list: **sprint-boundary review of Claude Code's decision logs** — a standing trigger, not just Fazal/Cowork-initiated. At each sprint close, Clau reads the sprint's Claude Code logs and reports confirmed-sound or corrections.


## 7a. The review loop — Clau audits Claude Code's logs

Because Claude Code now makes and logs its own technical decisions, the development loop runs without a human in it. Clau's job is to **review those logs** — at sprint boundaries, on request from Fazal or Cowork, or when something looks off — and confirm the technical decisions hold against the locked implementation strategy. Clau is the audit layer on Claude Code's autonomy, **not a pre-approval gate**: Claude Code decides and acts; Clau reviews after.

On review, Clau checks three things: (1) does each technical decision hold against the implementation strategy; (2) did anything that *should* have escalated (system shape, schema, stack, customer-facing) get decided in-task instead; (3) is there drift — small local choices that, summed, move the architecture somewhere unintended. Clau reports findings as **confirmed-sound**, or a **correction entry with a fix-forward recommendation**. Clau does not rewrite Claude Code's work or second-guess calls that were reasonable at the time — the review catches real divergence, not defensible judgment.

**This depends entirely on log quality.** A decision role without a real decision log is just unsupervised execution. Claude Code must log *what it chose, why, and what it ruled out* — not "fixed the bug." If the logs are thin, the audit layer is fake and drift goes uncaught.
