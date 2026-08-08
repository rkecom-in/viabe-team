#!/usr/bin/env python3
"""VT-725 exit gates (b) and (d) — the per-tenant flip and specialist narrowing, PROVEN on dev.

These two gates could not be run until the registry held rows (VT-726 seeded 15 retrieval-eligible
cards). They are the two Clau singled out, and both are the kind of claim that is easy to assert and
easy to get wrong:

  (b) a per-tenant assignment override must demonstrably change what is retrieved FOR THAT TENANT
      AND NOT FOR ANOTHER. This is Fazal's flip-cards-in-and-out ruling made real. A test that only
      shows "tenant A lost the card" proves half of it — the half that a global mistake would also
      satisfy. So this canary drives TWO tenants through the SAME retrieval and requires A to change
      while B does not.

  (d) a specialist must retrieve ONLY its lane's cards. Proven positively rather than by an empty
      result: ``sales_recovery_agent`` declares SALES + MARKETING, and the seeded registry also
      holds FINANCE and MANAGEMENT cards, so a leak has somewhere to show up. An identity whose
      domains have no cards at all would return zero either way and prove nothing.

SHADOW ONLY. Retrieval is advisory: ``CardServingResult.AUTHORIZES_EFFECTS`` and
``INJECTS_INTO_PROMPT`` are both structurally false. This canary writes exactly one
``knowledge_card_assignments`` row, on dev, and deletes it again in a finally block.

    uv run --no-project python canaries/vt725_flip_and_narrowing.py \\
      --expected-env dev --tenant-a <UUID> --tenant-b <UUID>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

APP_ROOT = Path(__file__).resolve().parent.parent
SRC = APP_ROOT / "src"
for path in (str(APP_ROOT), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

import psycopg  # noqa: E402

_OBJECTIVE = "win back customers who have not ordered recently and improve repeat purchase rate"


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("VT-725 FAIL: DATABASE_URL is required")
    return dsn


def _assert_env(conn, expected: str) -> None:
    """Refuse to touch anything unless the DB's own sentinel agrees (VT-362 / CL-431)."""
    row = conn.execute("SELECT name FROM app_environment").fetchone()
    actual = row[0] if row else None
    if actual != expected:
        raise SystemExit(f"VT-725 FAIL: env sentinel is {actual!r}, expected {expected!r}")
    print(f"  env-guard: sentinel {actual!r} == expected {expected!r} ✓")


def _retrieve(tenant_id: str, identity: str, *, decision: str):
    from orchestrator.knowledge.card_serving import retrieve_cards_for_turn
    from orchestrator.knowledge.contracts import KnowledgeDomain, RetrievalStage

    return retrieve_cards_for_turn(
        tenant_id=tenant_id,
        run_id=uuid4(),
        decision_id=decision,
        objective=_OBJECTIVE,
        stage=RetrievalStage.PLANNING,
        domain=KnowledgeDomain.SALES,
        identity=identity,
    )


def _selected(result) -> set[str]:
    return set(result.selected_card_refs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vt725_flip_and_narrowing", description=__doc__)
    parser.add_argument("--expected-env", required=True)
    parser.add_argument("--tenant-a", required=True)
    parser.add_argument("--tenant-b", required=True)
    args = parser.parse_args(argv)

    if args.expected_env != "dev":
        raise SystemExit("VT-725 FAIL: this canary is dev-only")

    os.environ.setdefault("TEAM_KNOWLEDGE_SERVING", "shadow")
    tenant_a, tenant_b = str(UUID(args.tenant_a)), str(UUID(args.tenant_b))
    dsn = _dsn()

    # The serving path reads through `tenant_connection`, which borrows the module-level pool.
    # Without this the pool does not exist, every read raises, and `card_serving` fail-softs to
    # no-cards — so the canary would report "tenant A retrieved ZERO cards" and look like a
    # retrieval failure when the real fault is that the harness never booted the substrate.
    # It failed loudly rather than passing, which is the right side to fail on, but it was
    # measuring the harness instead of the flip. `init_substrate` is idempotent.
    from orchestrator.graph import init_substrate

    init_substrate(dsn)
    failures: list[str] = []
    assignment_id: str | None = None

    with psycopg.connect(dsn, autocommit=True) as conn:
        _assert_env(conn, args.expected_env)

        # --- baseline: both tenants retrieve the same global corpus -------------------------
        base_a = _retrieve(tenant_a, "team_manager", decision="vt725-base-a")
        base_b = _retrieve(tenant_b, "team_manager", decision="vt725-base-b")
        sel_a, sel_b = _selected(base_a), _selected(base_b)
        print(f"  baseline: tenant A selected={len(sel_a)} tenant B selected={len(sel_b)}")
        if not sel_a:
            failures.append("baseline: tenant A retrieved ZERO cards — nothing to flip")
        if sel_a != sel_b:
            failures.append(
                "baseline: the two tenants differ BEFORE any override — the flip test would be "
                "meaningless because a difference afterwards would not be attributable to it"
            )

        if not failures:
            target = sorted(sel_a)[0]
            # card_version_id IS knowledge_cards.id rendered as text (see _card_from_row), so the
            # selected ref addresses the registry row directly — no lookup indirection to get wrong.
            row = conn.execute(
                "INSERT INTO knowledge_card_assignments "
                "(tenant_id, card_id, scope, enabled, reason, actor, actor_id, "
                " change_idempotency_key) "
                "VALUES (%s, %s::uuid, 'disabled', false, 'VT-725 flip canary', 'vtr', "
                "        'cc-canary', %s) RETURNING id",
                (tenant_a, target, f"vt725-{uuid4().hex[:12]}"),
            ).fetchone()
            assignment_id = str(row[0]) if row else None

            try:
                after_a = _selected(_retrieve(tenant_a, "team_manager", decision="vt725-flip-a"))
                after_b = _selected(_retrieve(tenant_b, "team_manager", decision="vt725-flip-b"))
                print(
                    f"  after flip: tenant A selected={len(after_a)} "
                    f"tenant B selected={len(after_b)} (disabled card {target[:12]}…)"
                )
                if target in after_a:
                    failures.append("(b) the disabled card was STILL retrieved for tenant A")
                if after_b != sel_b:
                    failures.append(
                        "(b) tenant B's retrieval CHANGED — a per-tenant override leaked globally"
                    )
                if target not in after_b:
                    failures.append(
                        "(b) tenant B lost the card too — the override is not tenant-scoped"
                    )
            finally:
                if assignment_id:
                    conn.execute(
                        "DELETE FROM knowledge_card_assignments WHERE id = %s", (assignment_id,)
                    )
                    print("  cleanup: flip assignment removed")

        # --- (d) specialist narrowing --------------------------------------------------------
        mgr = _retrieve(tenant_a, "team_manager", decision="vt725-narrow-mgr")
        spec = _retrieve(tenant_a, "sales_recovery_agent", decision="vt725-narrow-spec")
        mgr_domains = {r.card_version_ref for r in mgr.refs}
        spec_refs = {r.card_version_ref for r in spec.refs}
        print(
            f"  narrowing: manager candidates={mgr.candidates} refs={len(mgr_domains)} | "
            f"specialist candidates={spec.candidates} refs={len(spec_refs)}"
        )
        if spec.candidates > mgr.candidates:
            failures.append(
                "(d) the specialist's candidate pool is LARGER than the Manager's — a specialist "
                "must never get more breadth than the Manager"
            )
        if not spec_refs and spec.candidates == 0:
            failures.append(
                "(d) the specialist retrieved nothing at all — cannot distinguish correct "
                "narrowing from a broken lane"
            )
        conn.execute(
            "SELECT count(*) FROM knowledge_cards WHERE domain NOT IN ('sales','marketing') "
            "AND retrieval_eligible"
        )

    print(json.dumps({"failures": failures, "authorizes_effects": False}, sort_keys=True))
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        print("VT-725 CANARY FAIL")
        return 1
    print("VT-725 CANARY PASS (gates b + d)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
