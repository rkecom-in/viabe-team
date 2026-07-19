# VT-507 — Form-path entity discovery: async DBOS workflow

**Status:** plan-first (awaiting Cowork gate)
**Priority:** High
**Exec Order:** next after VT-508

---

## Root cause (evidence)

The confirmed timeout chain is:

```
browser → POST /api/team/onboard/entity-candidates (Vercel serverless, max ~60s)
           → fetchEntityCandidates (orchestrator-client.ts)
              → AbortSignal.timeout(_ENTITY_CANDIDATES_TIMEOUT_MS = 10_000)
                → POST /api/orchestrator/onboard/entity-candidates
                   → entity_match.fetch_candidates()
                      → _knowyourgst_candidates()
                         → KnowYourGSTScraper.search()
                            → ScrapingBee headless POST form
                               _TIMEOUT_S = 120.0   ← ~90s uncached
```

The knockyourgst.com leg (VT-495) renders a Django POST form through ScrapingBee's headless browser
(`render_js=True + js_scenario: fill → click → wait 6000ms`). Uncached, this completes in ~90s.
The client-side timeout is 10s (`_ENTITY_CANDIDATES_TIMEOUT_MS`). The orchestrator request aborts
at 10s → `reason:'timeout'` → fail-closed `{candidates:[]}` → the form shows "couldn't find."

The backend endpoint itself works fine and waits the full 90s — that's why proving via a direct
`curl /api/orchestrator/onboard/entity-candidates` with 120s timeout surfaced candidates but the
real form path did not.

**There is also a 6h TTL in-process cache** (`_cache` in `knowyourgst.py`): a second lookup for
the same query name returns instantly from cache. The problem is ONLY the first (cold) lookup.

---

## Why a simple timeout increase won't fix it

- `_ENTITY_CANDIDATES_TIMEOUT_MS = 10_000` → 10s abort in `orchestrator-client.ts`
- Even if raised to 120s: Vercel serverless functions time out at 60s (Pro) or 10s (Hobby) by
  platform limit. A server action that holds for 90s will still 504 at the platform layer.
- Even if the network held: the owner is staring at a spinner for 90s → UX is broken before the
  technical fix is complete.

The correct fix decouples the scrape latency from the HTTP response latency.

---

## Recommendation: async discovery DBOS workflow (Option A)

### Architecture

```
POST /api/team/onboard/entity-candidates  (team-web proxy)
  → POST /api/orchestrator/onboard/entity-candidates/start  (NEW, replaces existing)
     → entity_discovery_workflow(name, city, discovery_id)  ← @DBOS.workflow
        returns {discovery_id, status:'started'} IMMEDIATELY (within ~50ms)

GET  /api/team/onboard/entity-candidates/[id]  (NEW poll route in team-web)
  → GET /api/orchestrator/onboard/entity-candidates/[id]   (NEW poll endpoint)
     → SELECT from entity_discovery_requests WHERE id = $1
        returns {status, candidates, failure_reason, impact}

UI: "Searching for your business…" spinner while status='running'
    Transitions to candidate list when status='complete'
    Surfaces structured error copy when status='failed' (reason visible to owner)
```

### Why DBOS fits exactly

- **Durability**: if Railway restarts mid-scrape, DBOS recovers the workflow step — the ScrapingBee
  call re-runs at the step boundary, not from scratch (same `@DBOS.step` pattern as l2_send).
- **Failure tracking** (Fazal's explicit ask: "DBOS should track every failure reason + impact"):
  - As a workflow, every attempt and its terminal outcome is in `dbos.workflow_status`.
  - We additionally write a thin `entity_discovery_requests` row (one migration) with
    `{discovery_id, business_name_hash, city, status, candidates_json, failure_reason, impact, started_at, completed_at}`.
    - `failure_reason`: `timeout | scrape_error | parse_error | no_key | zero_results`
    - `impact`: `blocked_signup | degraded_to_manual | partial_candidates`
  - This is the queryable durability the DBOS expectation covers — durable log of every attempt
    vs. today's silent log-warning + fail-closed `[]`.
- **Mirrors existing patterns**: auto_discovery_workflow (`@DBOS.workflow` started non-blocking with
  `DBOS.start_workflow` post-commit) + l2_send_workflow (register-before-launch contract).

---

## Alternative A: cache-warm at an earlier step

Pre-scrape at the "details" step submission (business_name + city are available) → by the time the
owner hits "Search," the 6h TTL cache is warm → inline call returns in ~1s.

**Trade-off:**
- Cheaper to build (no new DB table, no polling, no workflow).
- BUT: the first-ever lookup (cold) still holds the POST request for 90s and will hit the Vercel
  timeout. Every subsequent lookup is fast (cache hit). This doesn't fix the FIRST impression.
- If the page loads on a fresh server process (Railway restart), the cache is cold again.
- Verdict: useful as a latency OPTIMIZATION layered on top of Option A, not a standalone fix.

## Alternative B: tight inline timeout + "searching…" first-paint + follow-up call

Immediately return `{candidates:[], status:'searching'}` on timeout; the form shows "still
searching…"; a second client poll re-hits the route until candidates arrive or a max time elapses.

**Trade-off:**
- No workflow, no new table, no migration.
- BUT: the scrape still runs inside a synchronous FastAPI handler → holds a Railway worker thread
  for ~90s per request → concurrency bottleneck under load, not suitable for signup volume.
- DBOS workflow tracks failure atomically; a poll-loop around a synchronous handler does not
  (failure logging is best-effort, not durably persisted).
- Verdict: a valid hack if we want to skip DBOS, but we lose the failure-tracking requirement.

---

## Files that would change

### New (orchestrator)
- `src/orchestrator/onboarding/entity_discovery_workflow.py`
  — `@DBOS.workflow` (`entity_discovery_workflow(discovery_id, name, city)`)
  — `@DBOS.step` for the ScrapingBee leg (same lazy-decorate pattern as l2_send)
  — writes the `entity_discovery_requests` row on start + updates on complete/fail
  — `register_entity_discovery()` called from main.py lifespan before `launch_dbos()`

- `src/orchestrator/api/entity_discovery.py`
  — `POST /api/orchestrator/onboard/entity-candidates/start` → starts workflow, returns `{discovery_id}`
  — `GET  /api/orchestrator/onboard/entity-candidates/{discovery_id}` → reads the row, returns status/candidates

### Modified (orchestrator)
- `src/orchestrator/api/__init__.py` — include entity_discovery_router
- `src/main.py` — `register_entity_discovery()` call in lifespan (same register-before-launch contract)

### New migration
- `migrations/145_vt507_entity_discovery_requests.sql`
  ```sql
  CREATE TABLE entity_discovery_requests (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name_hash      TEXT NOT NULL,            -- SHA-256(lower(business_name)) — CL-390: never store the name
    city_hash      TEXT NOT NULL,            -- SHA-256(lower(city))
    status         TEXT NOT NULL DEFAULT 'running'  -- running | complete | failed
                   CHECK (status IN ('running','complete','failed')),
    candidates     JSONB,
    failure_reason TEXT,                     -- timeout | scrape_error | parse_error | no_key | zero_results
    impact         TEXT,                     -- blocked_signup | degraded_to_manual | partial_candidates
    dbos_workflow_id TEXT,                   -- links to dbos.workflow_status for the durable log
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ
  );
  CREATE INDEX ON entity_discovery_requests (started_at DESC);
  -- Purge rows after 24h (discovery data is transient; the DBOS workflow_status is the durable audit)
  ```
  Migration number = 145 (next-migration file confirmed; claim via allocator before build).

### Modified (team-web)
- `lib/orchestrator-client.ts`
  — New `startEntityDiscovery(businessName, city)` → `{discovery_id, status}`
  — New `pollEntityDiscovery(discoveryId)` → `{status, candidates, failure_reason}`

- `app/api/team/onboard/entity-candidates/route.ts` (POST — stays as-is for backward compat)
  OR replace with a `start` POST + `[id]` GET poll route pair

- `app/(marketing)/team/signup/entity-match-step.tsx`
  — Replace direct `fetchEntityCandidates` call with: start discovery → poll until complete/timeout
  — "Searching for your business…" interim state (not "couldn't find" on a timeout)
  — Progressive reveal of candidates as the status resolves

---

## UX spec

### States

| State | User sees |
|---|---|
| `running` | "Searching for your business… (takes ~30 seconds)" spinner |
| `complete` with candidates | Candidate list (existing UI, no change) |
| `complete` with zero_results | "We couldn't auto-find your business. Enter your GSTIN manually." (manual path) |
| `failed` with `timeout` | "Our search is taking longer than expected — enter your GSTIN below to continue." (manual path, NOT "couldn't find") |
| `failed` with `scrape_error` or `no_key` | Same as timeout copy |
| `failed` with `parse_error` | Same |

**Key invariant**: only `complete` with zero candidates may show the "we couldn't find" terminal state.
A `failed` (timeout / error) must NEVER surface as "couldn't find" — that copy implies "your company
isn't GST-registered," which is false on a timeout. The manual GSTIN path is always the fallback.

### Poll cadence
- Poll every 3s while `status === 'running'`
- Max poll duration: 150s (covers even a slow ScrapingBee render + 30s grace)
- On max-poll-exceeded: treat as `failed`, surface the timeout copy + manual GSTIN path

---

## Failure tracking (DBOS expectation)

Each discovery attempt creates one `entity_discovery_requests` row. Terminal outcomes:

| failure_reason | impact |
|---|---|
| `timeout` | `blocked_signup` (owner couldn't auto-find) |
| `scrape_error` | `blocked_signup` |
| `zero_results` | `degraded_to_manual` (ran fine, no hit) |
| `no_key` | `degraded_to_manual` (ScrapingBee unconfigured) |
| `partial_candidates` | `degraded_to_manual` (web/GBP ran, knowyourgst didn't) |

Query to surface blocked_signup rate: `SELECT failure_reason, count(*) FROM entity_discovery_requests WHERE impact='blocked_signup' AND started_at > now()-'7 days'::interval GROUP BY 1`.

This is the durable, queryable failure log Fazal asked for — replacing the current silent `logger.warning("entity_match: knowyourgst discovery failed")` + `return []`.

---

## Proof plan (Fazal-live acceptance)

The test must go through the REAL FORM path: **browser → team-web → orchestrator → ScrapingBee → knowyourgst.com**. Not a direct orchestrator curl (that already works).

1. **Dev deploy** — push to `dev`, let Railway + Vercel auto-deploy.
2. **Open the form** at `viabe-team-web-dev.vercel.app/team/signup` (or deployed-dev URL).
3. Enter "RKeCom" (or another business whose GSTIN Fazal has verified = findable on knowyourgst).
4. **Observe**: form shows "Searching…" state (not an immediate "couldn't find").
5. **Wait ~90s**: form resolves with candidates including the RKeCom GSTIN.
6. **Failure path** (disable ScrapingBee key via Railway env, redeploy): form resolves after timeout
   to the manual GSTIN entry copy — NOT "couldn't find."
7. **Check `entity_discovery_requests`** (Supabase dev console): row exists with `failure_reason`
   and `impact` populated on the failure-path test.

---

## Bounded retry on transient scrape failure

Within the `entity_discovery_workflow`:
- ScrapingBee `4xx` (bad request) → `failure_reason='scrape_error'`, no retry (bad input).
- ScrapingBee `5xx` or network error → retry up to 2× with 10s DBOS.sleep between attempts.
  Uses `@DBOS.step` boundary for checkpoint-safe retry (no double-billing on recovery).
- Timeout (`httpx.TimeoutException`) → 1 retry; 2nd timeout → `failure_reason='timeout'`.

---

## Cowork gate checklist (plan-first)

- [ ] Cowork confirms the `entity_discovery_requests` migration number is claimed via allocator (not hand-picked)
- [ ] Cowork reviews the UX copy for timeout vs. zero-results distinction
- [ ] Cowork confirms the DBOS register-before-launch ordering (must precede `launch_dbos()` in main.py)
- [ ] Cowork confirms VT-507 exec_order relative to any other in-flight signups work
- [ ] Fazal's live-acceptance definition: form at deployed-dev URL surfaces GSTIN for a real business

---

## What does NOT change

- Sandbox GSTIN verify (`confirm_and_verify`) — untouched, still the sole authoritative gate.
- Fail-closed on candidate lookup: zero candidates → manual GSTIN path (always available).
- The `entity_match.fetch_candidates` Python function — the workflow calls it as a `@DBOS.step`.
  No change to the candidate-generation logic itself.
- VT-408 hard reject (no gstin_verified → no account) — untouched.
