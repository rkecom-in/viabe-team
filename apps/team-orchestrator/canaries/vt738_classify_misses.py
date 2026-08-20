#!/usr/bin/env python3
"""VT-738 — turn the re-drive's forensic dumps into a per-run VERDICT.

The point of the whole exercise: a turn that did not delegate is not one failure mode, it is at
least four, and they need opposite fixes. Before the instruments landed they were indistinguishable
— all four produced the same owner-facing "I couldn't complete it on my own" and left no row saying
which had happened. This script reads the rows that now exist and says which.

    M1  the enforce loop defaulted to escalate because manager_review never ran
        (the brain spawned nothing) -> `manager_task_escalate_defaulted`
    M1' the activation/prereq gate failed closed BEFORE any dispatch
        -> `manager_task_prereq_failed`  (a different bug with a different fix)
    RV  a specialist DID run and manager_review rejected its work
        -> `manager_review_decision.outcome='escalate'`  (not a delegation failure at all)
    M2  delegation happened but the campaigns row is not attributable to the asserted turn
        (measurement, not product)
    M3  the tenant's slot was already held, so triage collapsed to the legacy fall-through
        -> `triage_decision.has_active_task = true`
    M4  the deterministic net did not match and the classifier did not call it campaign_recovery
        -> `d3_matched = false` AND `task_kind != 'campaign_recovery'`
    BUD a hard budget cap converted a recorded spawn into a terminal
        -> `route_budget_suppressed`

Usage:
    uv run --no-project python canaries/vt738_classify_misses.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

FORENSICS = Path(__file__).resolve().parent / "_reports" / "vt738_forensics"


def _kinds(rows: list[dict]) -> Counter:
    return Counter(r.get("event_kind") for r in rows)


def _decisions(rows: list[dict], kind: str) -> list[dict]:
    out = []
    for r in rows:
        if r.get("event_kind") != kind:
            continue
        d = r.get("decision")
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except Exception:  # noqa: BLE001 — a malformed payload is data, not a crash
                d = {"_unparsed": d}
        out.append(d or {})
    return out


def classify(payload: dict) -> dict:
    """Return the verdict for one (scenario, run). Ordered most-specific first: an escalate that
    manager_review actually decided is NOT the loop defaulting, even though both end in 'escalate'.
    """
    audit = payload.get("tm_audit_log")
    if not isinstance(audit, list):
        return {"verdict": "NO_AUDIT", "why": "tm_audit_log missing from the dump"}

    kinds = _kinds(audit)
    triage = _decisions(audit, "triage_decision")
    routes = _decisions(audit, "route_decided")
    campaigns = payload.get("campaigns") if isinstance(payload.get("campaigns"), list) else []

    spawned = [r for r in routes if r.get("route_key") not in (None, "terminal")]
    d3_fired = kinds.get("campaign_first_contact_dispatched", 0) > 0
    llm_routed = kinds.get("triage_campaign_recovery_routed", 0) > 0
    delegated = bool(spawned) or d3_fired or llm_routed

    facts = {
        "delegated": delegated,
        "spawn_routes": len(spawned),
        "d3_dispatched": d3_fired,
        "llm_routed": llm_routed,
        "campaigns": len(campaigns),
        "task_kinds": [t.get("task_kind") for t in triage],
        "d3_matched": [t.get("d3_matched") for t in triage],
        "has_active_task": [t.get("has_active_task") for t in triage],
        # Present only if the instrument is live — its ABSENCE on an old dump is not evidence
        # the path did not fire, so it is reported as a tri-state rather than a bool.
        "escalate_defaulted": kinds.get("manager_task_escalate_defaulted", 0),
        "prereq_failed": kinds.get("manager_task_prereq_failed", 0),
        "budget_suppressed": kinds.get("route_budget_suppressed", 0),
        "review_decisions": kinds.get("manager_review_decision", 0),
    }

    if facts["budget_suppressed"]:
        return {"verdict": "BUD", "why": "a hard budget cap suppressed the spawn", "facts": facts}
    if facts["prereq_failed"]:
        return {"verdict": "M1'", "why": "prereq/activation gate failed closed before dispatch",
                "facts": facts}
    if facts["escalate_defaulted"]:
        return {"verdict": "M1", "why": "manager_review never ran; loop defaulted to escalate",
                "facts": facts}
    # Success is checked BEFORE the soft diagnostics. The first cut asked `has_active_task` first
    # and mislabelled two runs M3 that had in fact delegated AND landed a campaign — on any
    # multi-step scenario an active task is simply what step 0 created, so `has_active_task` is
    # normal on later turns and says nothing on its own. It only diagnoses anything on a run that
    # did NOT succeed. The hard signals above (BUD/M1'/M1) stay first because each is a real event
    # regardless of whether some other turn in the run happened to succeed.
    if delegated and campaigns:
        return {"verdict": "OK", "why": "delegated and a campaign landed", "facts": facts}
    if delegated and not campaigns:
        return {"verdict": "RV", "why": "delegated, but no campaign persisted (specialist declined "
                                        "or review rejected)", "facts": facts}
    if any(t.get("has_active_task") for t in triage):
        return {"verdict": "M3", "why": "tenant slot held — triage collapsed to legacy fall-through",
                "facts": facts}
    if triage and not any(t.get("d3_matched") for t in triage) and \
            not any(t.get("task_kind") == "campaign_recovery" for t in triage):
        return {"verdict": "M4", "why": "deterministic net missed AND the classifier did not call "
                                        "it campaign_recovery", "facts": facts}
    return {"verdict": "UNCLASSIFIED", "why": "no rule matched — read the dump by hand", "facts": facts}


def main() -> int:
    if not FORENSICS.is_dir():
        print(f"no forensics at {FORENSICS}")
        return 2
    files = sorted(FORENSICS.glob("*.json"))
    if not files:
        print("no dumps yet")
        return 2

    tally: Counter = Counter()
    print(f"{'scenario':44s} {'run':>3s}  {'verdict':13s} why")
    print("-" * 118)
    for path in files:
        payload = json.loads(path.read_text())
        res = classify(payload)
        tally[res["verdict"]] += 1
        print(f"{payload.get('scenario','?')[:44]:44s} {payload.get('run_index','?'):>3}  "
              f"{res['verdict']:13s} {res['why']}")

    print("\n=== tally ===")
    for verdict, n in tally.most_common():
        print(f"  {verdict:13s} {n}")

    delegating = tally["OK"]
    total = sum(tally.values())
    print(f"\ndelegated-and-landed: {delegating}/{total}")
    # Deliberately NOT printed as a "miss rate": these are the 5 scenarios chosen BECAUSE they
    # carry the known misses, so the denominator is adversarially selected. Quoting a rate off it
    # would repeat exactly the sampling error this row spent a day retracting.
    print("NOTE: this set is adversarially selected (the known-miss scenarios). It answers WHICH "
          "mechanism fires, not what the fleet-wide rate is. Do not quote a rate from it.")
    # Stated because it is the same weakness this row spent a day pinning on the harness marker,
    # and it would be dishonest to exploit it here: 'campaigns' is read TENANT-WIDE, so a campaign
    # created by step 0 makes the whole run read OK even if a later step did not delegate. The
    # mechanism verdicts (M1/M1'/M3/BUD) are per-run audit facts and do not have this problem; only
    # OK/RV do. Per-TURN attribution needs the campaign joined to the turn's run_id.
    print("NOTE: 'OK' is per-RUN, not per-TURN — campaigns are counted tenant-wide, so a step-0 "
          "delegation can mask a later step. M1/M1'/M3/BUD verdicts are unaffected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
