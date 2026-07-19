# Cowork ↔ CC Operating Standard (portable, all projects)

**What this is:** the method by which an orchestrator agent (**Cowork**) and an implementer agent (**CC / Claude Code**) collaborate through a file-based signal pipeline, gated by adversarial review and authorized by a human. It is designed to be **copied into any repo** so every project runs the loop and tracks tasks the same way.

**When to use it:** any project where one agent plans/reviews/routes and another implements — i.e., you want a delivery captain separate from the coder, with a human holding final authority. If a project is a solo agent doing trivial work, this is overkill; adopt it when builds are multi-step, risky, or run across many sessions.

---

## 1. Roles (rename per project, keep the shape)

| Generic role | This project's name | Owns |
|---|---|---|
| **Principal** | Fazal (CEO) | Final calls; authorizes every production promotion; can override anything. When the Principal overrides, align and move on — no re-litigating. |
| **Orchestrator** | Cowork | The tracker, briefs, routing work to the implementer, the **adversarial gate**, authorizing low-risk/staging merges. Decides within-scope operational matters using standing rules. |
| **Implementer** | CC (Claude Code) | Code-level design, builds, refactors, tests, fixes. Self-merges low-risk rows on green; risk rows wait for the Orchestrator's gate. Logs decisions. |
| **Auditor** (optional) | Clau | Cross-cutting architecture/sequencing review, AFTER the fact, at boundaries or on request. |

**The non-negotiable separation:** the agent that **builds** is never the agent that **gates**. The Orchestrator reviews the Implementer's work with independent, executed evidence — never rubber-stamps it.

---

## 2. The `.running/` signal pipeline (copy this verbatim into every repo)

```
<repo>/.running/
├── to-claudecode/     # Orchestrator → Implementer  (brief, task, task-merge, review, addendum, answer, question)
├── to-cowork/         # Implementer → Orchestrator  (status, pr-ready, merged, task-result, question, answer)
└── processed/         # archive — move a signal here after it's handled
```

**Signal = one Markdown file** with YAML frontmatter + a body. Filename convention: `<UTC-timestamp>-<vt-or-topic>-<short-slug>.md` (e.g. `20260610T091500Z-vt365-DEV-MERGE-AUTHORIZED.md`). Timestamps sort the inbox oldest-first.

**Required frontmatter (every signal):**
```yaml
---
from: cowork | claudecode
to: claudecode | cowork
type: brief-ready | task | task-merge | review | addendum | answer | question
                  | status | pr-ready | merged | task-result
vt: <work-item-id or "unassigned">          # the unit of work
ts: <UTC ISO timestamp or a phase tag>
session_blanket_auth: true | false           # is this inside a granted scope?
authorized_by: fazal                         # ONLY when the Principal explicitly granted
authorization_basis: "<quoted Principal directive + when>"   # the receipt for the grant
in_reply_to: <the signal filename this answers>   # optional, for threading
---
# One-line headline of what this signal is / wants.
Body: the brief, the result, the gate verdict, etc.
```

**Lifecycle:** Orchestrator drops a `brief-ready`/`task` into `to-claudecode/` → Implementer consumes it (moves the consumed copy to `processed/` or `.bak`s it), works, drops `status`/`pr-ready` into `to-cowork/` → Orchestrator reads it, runs the gate, drops `task-merge`/`review` back → repeat. **Every handled signal ends up in `processed/`** so the live inboxes only hold open work.

**Bootstrapping a new project:** create the three dirs, add a `.running/protocol.md` defining your signal types + frontmatter (copy §2 of this file), and commit. Optionally add a one-line `.gitignore` decision: signals are usually committed (they're the audit trail) but `processed/` can be gitignored if it gets noisy.

---

## 3. The core loop (this is the whole method)

1. **Plan-first on anything with real architecture or risk.** The Implementer proposes a plan (or the Orchestrator writes a brief); for sensitive work the Orchestrator runs an **adversarial subagent on the PLAN itself** before any code — this catches design flaws (security ordering, unsafe inherited patterns) for free.
2. **Implementer builds.** It runs its own multi-builder + self-verify pass; it allocates IDs/migration numbers up-front (never mid-parallel-work).
3. **Orchestrator's adversarial GATE on the built code, with EXECUTED evidence.** Run greps, run tests, rebuild the logic in isolation — **never trust code-reading or the author's claims.** Silent shape/data bugs only surface on execution. The gate returns a clear verdict: CLEAR-TO-MERGE or RE-BOUNCE with specific blockers.
4. **Authorized merge.** On a clean gate + green checks, merge per the governance in §4.

**Self-triggered polling:** whenever the Implementer has open work, the Orchestrator **polls the `to-cowork/` inbox + `git` continuously** — it does NOT wait to be told "go check." Keep polling until a result lands or the Principal redirects. Don't false-alarm on quiet: confirm the inbox hasn't drained AND no branch/commit advanced AND it's not sitting in a foreground CI watch before concluding the Implementer is stuck.

**Session-blanket auth:** the Principal grants scope at batch level ("build these 6 things," "close the rest"). Inside that grant the Orchestrator dispatches + gates + authorizes staging merges autonomously — it does NOT re-ask "should I proceed?" for in-scope steps. **New scope = a new explicit grant.** Don't widen scope without asking.

---

## 4. Merge / authorization governance (adapt branch names per project)

- **Two long-lived branches:** `dev` (staging env) and `main` (production env). [viabe-specific: dev→Seoul/Railway-Dev, main→Mumbai/Railway-Prod — replace with your envs.]
- **Implementer self-merges low-risk rows to `dev` on green.** Risk rows (money / auth / PII / RLS / classifier / new external surface) get the **Orchestrator's adversarial gate first**, then the Orchestrator authorizes the `dev` merge.
- **`main` (production) is Principal-authorized ONLY.** A `dev → main` promotion opens only on the Principal's explicit word. Never auto-merge to prod.
- **Green-check nuance (generalize this):** if your CI uses an aggregate "all-green" check, confirm it doesn't **exclude flaky/quarantined jobs** (DB migrations, integration, e2e are common exclusions). The aggregate being green is necessary but not sufficient for a risk row — check the excluded jobs directly. [viabe-specific: `ci-success` excludes `migrations`/`orchestrator`/`e2e`.]

---

## 5. Task management (two layers — both required, in every project)

**Layer A — the live in-session task list (the Orchestrator's working tracker):**
- Create a task list at the **start** of virtually any multi-step request (the tool renders it as a progress widget the Principal sees).
- One task per coherent unit of work; **mark in_progress when you start it, completed the moment it's done** — don't batch updates.
- **Always include a final verification task** (fact-check / run tests / adversarial gate / diff review). For high-stakes work, the verification step is itself a subagent.
- Keep subjects specific and outcome-shaped ("Gate VT-365 PR #446 — adversarial subagent"), not vague ("review code").

**Layer B — the repo-persisted task + decision files (cross-session source of truth):**
The in-session list dies with the session; the repo files are the only cross-session/cross-window substrate. Standard layout (rename to taste):
```
<repo>/.tasks/  (or .viabe/sprint/)   # one file per work item: TASK-<N>.md  (status, priority, deps, acceptance)
<repo>/docs/log/entries/CL-<N>.md     # session log: what was done + every material decision (for the Auditor)
<repo>/docs/log/decisions-ledger.md   # flat list of STANDING decisions — do NOT re-litigate anything here
<repo>/docs/log/latest-snapshot.md    # 5-field state: Critical-path / In-flight / Blocked-on / Next-action / Do-not
```
**Disciplines for Layer B:**
- **IDs come from an allocator, never hand-picked** (a flock'd script that hands out monotonic IDs / migration numbers). Hand-picking under parallel work causes collisions. [viabe: `scripts/vt_id_allocate.py`, `scripts/migration_id_allocate.py`.]
- **The snapshot drifts — treat it as a hypothesis, not gospel.** Reconcile against `git log` + merged PRs before trusting any state claim. **Memory is never authoritative; the repo is.**
- **Standing decisions are settled.** If it's in the ledger, don't re-argue it.
- The board is the source of truth; if you ever find yourself querying a stale external tool (old Notion, etc.), stop and read the repo file instead.

---

## 6. Disciplines that made it work / failure modes to avoid

- **The gate must EXECUTE, not read.** Across one build run, executed-evidence gating caught ~10 real bugs that code-reading missed — several compliance-critical (opt-out being swallowed, missing FORCE-RLS, an SSRF, a foreign-key bug that would have permanently blocked data-deletion). A more capable model makes the gate *sharper*, not optional.
- **Gate the PLAN on sensitive work**, not just the code — that's where unsafe design (reintroducing a known bug, inheriting an unsafe auth precedent) gets caught before a line is written.
- **Allocate every ID/number once, up-front, before any parallel fan-out.**
- **Implementer-saturation is real.** Long multi-task runs degrade the implementer's context; it eventually needs a long wait or a **fresh restart**. Healthy pattern: the implementer **honest-checkpoints** (a clean WIP commit) when saturating, and a fresh session resumes from the checkpoint + the written spec — work in the shared tree is never lost. A per-task fresh-context runner (a daemon spawning a new implementer per signal) is the structural fix if you can run it.
- **Shared working tree caution (if applicable):** if the Principal + both agents share one working tree, the Orchestrator must NOT `git checkout`/`stash` it (it flips the human's tree and strands the implementer). Read via `git show <ref>:<path>`. Use explicit `git add <files>`, never `-am`.
- **Secrets hygiene:** an agent never writes a live secret VALUE into any signal/log/PR/commit (the repo is git — an echoed secret is a committed secret). Set secrets in the console; report only the variable NAME + the action.
- **Don't summarize at end-of-response to the Principal who reads the diff; lead with what's wrong, agree second.**

---

## 7. Adoption checklist (per new project)

- [ ] Create `.running/{to-claudecode,to-cowork,processed}/` + `.running/protocol.md` (signal types + frontmatter from §2).
- [ ] Drop this file (or a trimmed copy) at repo root as the operating standard; rename the roles/branches/IDs to the project.
- [ ] Create the Layer-B task/log/decision files (§5) + an ID allocator script.
- [ ] Add a root `AGENTS.md`/`CLAUDE.md` "bootstrap" that tells any fresh agent: read order = standard → snapshot (suspect) → decisions-ledger → reconcile against `git log`.
- [ ] Define the two branches + who authorizes prod (§4).
- [ ] First real task: run the full loop once (brief → build → adversarial gate → authorized merge) to validate the pipeline before trusting it.

---

## 8. Portable vs project-specific (so you copy the right things)

**Universal (copy everywhere):** §1 roles shape, §2 `.running/` pipeline + frontmatter, §3 the gate loop + polling + blanket-auth, §5 both task layers, §6 disciplines, §7 checklist.

**Project-specific (re-derive per project, don't copy verbatim):** exact branch→environment mapping and CI gate names (§4); the specific allocator script paths and the `.viabe/`/`docs/clau/` directory names (§5); any domain rules (the viabe DPDP/opt-out/RLS specifics are this product's, not universal).

---

*Standard distilled from the Viabe Team delivery loop. The point isn't the folder names — it's the discipline: a captain separate from the coder, a human holding authority, every risky change gated with executed evidence, and the whole state persisted in the repo so no session's memory is load-bearing.*
