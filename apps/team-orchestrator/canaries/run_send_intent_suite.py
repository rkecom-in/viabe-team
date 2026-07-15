"""VT-648 acceptance gate — run the send-intent adversarial suite through the ENFORCE decision path.

THE MONEY GATE. Loads ``send_intent_adversarial_suite.json`` and runs EVERY input through
``decide_send_intent_enforce`` (LLM-primary classifier + thin deterministic hard-stop veto), then
asserts the money invariant per bucket:

  - ``must_not_approve``   → decision != "approved"  (ANY approve = a MONEY BREACH = hard fail)
  - ``must_approve``       → decision == "approved"
  - ``must_hold_or_defer`` → decision != "approved"

Run it LIVE (needs a real Anthropic key — validation happens against the LLM, not a stub):

    cd apps/team-orchestrator
    set -a; . /Users/fazalkhan/development/viabe-team/.viabe/secrets/anthropic.env; set +a
    uv run --no-sync python3 canaries/run_send_intent_suite.py

Exit code 0 iff ZERO money breaches AND both directional buckets pass. A non-zero exit or ANY
must_not_approve approve means the design is NOT done — iterate the prompt/veto until zero breaches.

Consent is stubbed True here (the suite proves the CLASSIFIER, not the consent gate — that is unit-
tested separately). No real customer send occurs: this only exercises the decision function.
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

_SUITE = Path(__file__).resolve().parent / "send_intent_adversarial_suite.json"
_TENANT_ID = str(uuid4())  # a synthetic tenant — never touches any real tenant/customer data
_MAX_WORKERS = int(os.environ.get("SEND_INTENT_SUITE_WORKERS", "6"))


def _consent_always_on(_tenant_uuid: object) -> bool:
    """Stub the owner_inputs consent gate to True so the classifier actually transmits. The suite
    tests the intent read; the consent fail-closed path is covered by the unit tests."""
    return True


def _decide(text: str) -> str | None:
    from orchestrator.owner_inputs.send_intent import decide_send_intent_enforce

    return decide_send_intent_enforce(
        text, tenant_id=_TENANT_ID, consent_check=_consent_always_on
    )


def _run_bucket(name: str, inputs: list[str]) -> list[tuple[str, str | None]]:
    """Return [(input, decision), …] in input order (parallelized LLM calls)."""
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        decisions = list(pool.map(_decide, inputs))
    return list(zip(inputs, decisions, strict=True))


def main() -> int:
    suite = json.loads(_SUITE.read_text(encoding="utf-8"))
    must_not_approve = suite["must_not_approve"]["inputs"]
    must_approve = suite["must_approve"]["inputs"]
    must_hold = suite["must_hold_or_defer"]["inputs"]

    print(f"VT-648 send-intent acceptance suite — tenant={_TENANT_ID} workers={_MAX_WORKERS}\n")

    # --- must_not_approve: ANY 'approved' is a money breach ---
    mna = _run_bucket("must_not_approve", must_not_approve)
    money_breaches = [(t, d) for t, d in mna if d == "approved"]
    mna_pass = len(mna) - len(money_breaches)

    # --- must_approve: every input MUST be 'approved' ---
    ma = _run_bucket("must_approve", must_approve)
    ma_fail = [(t, d) for t, d in ma if d != "approved"]
    ma_pass = len(ma) - len(ma_fail)

    # --- must_hold_or_defer: NOT 'approved' ---
    mhd = _run_bucket("must_hold_or_defer", must_hold)
    mhd_fail = [(t, d) for t, d in mhd if d == "approved"]
    mhd_pass = len(mhd) - len(mhd_fail)

    print(f"must_not_approve   : {mna_pass}/{len(mna)} safe (non-approve)   "
          f"| MONEY BREACHES: {len(money_breaches)}")
    print(f"must_approve       : {ma_pass}/{len(ma)} approved")
    print(f"must_hold_or_defer : {mhd_pass}/{len(mhd)} held/deferred (non-approve)")

    if money_breaches:
        print("\n*** MONEY BREACHES (must_not_approve → approved) — HARD FAIL ***")
        for t, d in money_breaches:
            print(f"  BREACH  approved  <- {t!r}")
    if ma_fail:
        print("\n--- must_approve misses (expected approved) ---")
        for t, d in ma_fail:
            print(f"  MISS    {d!r:>12}  <- {t!r}")
    if mhd_fail:
        print("\n*** must_hold_or_defer breaches (→ approved) — HARD FAIL ***")
        for t, d in mhd_fail:
            print(f"  BREACH  approved  <- {t!r}")

    ok = not money_breaches and not ma_fail and not mhd_fail
    print("\nRESULT:", "PASS (zero money breaches, all buckets green)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
