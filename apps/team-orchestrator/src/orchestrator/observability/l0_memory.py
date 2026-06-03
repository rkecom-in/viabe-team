"""VT-126 L0 memory: cohort-keyed orchestrator-agent operational memory.

L0 is the orchestrator-agent's own working memory (CL-26). Fragments are
cohort-keyed (CL-390), NOT tenant-identifying — a fragment carries a
business-cohort signature (e.g. ``"restaurant|tier_2|founding"``) and is
aggregated across tenants under k-anonymity (CL-28: k=10).

Per CL-324 LOCKED: L0 stays custom (Mem0 deferred for L1-L3 post-launch).
Per CL-220: VT-181 ``@observability.tool_step`` decoration emits
``l0_write`` / ``l0_query`` pipeline_steps rows (envelopes registered in
``observability/envelopes/__init__.py``); the @tool_step wrappers live in
``agent/orchestrator_agent.py`` so the orchestrator-agent's tool
inventory consumes them as langchain ``BaseTool`` instances.
Per CL-417: canonical per-field columns (no JSONB-blob payload).

Two layers of k-anonymity defence
---------------------------------
1. SQL: RLS policy ``l0_fragments_kanon_select`` gates SELECT to
   ``observation_count >= 10`` for non-service-role connections.
2. App: ``query_l0`` adds an explicit ``AND observation_count >= 10``
   predicate so the SELECT cost is bounded even if RLS is bypassed
   (service-role read paths). Defense-in-depth per CL-122.

PII reject
----------
Every ``write_l0_fragment`` runs ``content`` through
``observability/pii.redact_for_log``. If the redacted copy differs from
the original (deep equality), PII is present → ``PiiInContentError`` is
raised; the write is rejected with NO row inserted. The orchestrator-
agent's system prompt instructs the model NEVER to embed tenant-
identifying content; this gate is the runtime backstop.

Connection
----------
``l0_fragments`` is cohort-keyed (no tenant_id column), so writes use
``get_pool().connection()`` directly (service-role bypasses RLS). The
write path's k-anon contract is the UPSERT shape itself — observation
count increments deterministically; the public read path (``query_l0``)
still honours the k>=10 threshold via the explicit predicate.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator.graph import get_pool
from orchestrator.observability.pii import redact_for_log

logger = logging.getLogger(__name__)


FragmentType = Literal[
    "routing_decision", "specialist_outcome", "trigger_pattern"
]

# CL-28: k-anonymity threshold for L0 fragment exposure.
K_ANONYMITY_THRESHOLD = 10


class PiiInContentError(ValueError):
    """Raised when ``write_l0_fragment.content`` contains tenant-identifying
    or PII data per the ``pii.redact_for_log`` scan.

    Carries the offending paths (best-effort) so the orchestrator-agent's
    callback can surface a structured error envelope.
    """

    def __init__(self, fragment_type: str, cohort_key: str) -> None:
        self.fragment_type = fragment_type
        self.cohort_key = cohort_key
        super().__init__(
            f"L0 write rejected: PII detected in content "
            f"(fragment_type={fragment_type} cohort_key={cohort_key})"
        )


def _content_has_pii(content: dict[str, Any]) -> bool:
    """Return True when ``redact_for_log(content)`` diverges from ``content``.

    The redactor preserves a value unchanged when it sees no PII pattern;
    any deep-equality divergence means at least one field was rewritten.
    Deep equality via JSON round-trip handles dict ordering + nested
    containers deterministically.
    """
    redacted = redact_for_log(content)
    # JSON canonicalises ordering + ensures hashable equality across
    # nested dicts/lists. content is JSON-serialisable by contract
    # (target column is JSONB).
    return json.dumps(content, sort_keys=True, default=str) != json.dumps(
        redacted, sort_keys=True, default=str
    )


def write_l0_fragment(
    *,
    fragment_type: FragmentType,
    cohort_key: str,
    content: dict[str, Any],
    tenant_id: UUID | str | None = None,
) -> dict[str, Any]:
    """UPSERT an L0 fragment. Idempotent on (fragment_type, cohort_key).

    First call inserts a row with observation_count=1. Each subsequent
    call with the same (fragment_type, cohort_key) increments
    observation_count by 1 and refreshes last_observed_at — the content
    of the FIRST observation is preserved (subsequent contents do not
    overwrite). This makes the cohort signature drift-resistant: once a
    pattern is recorded, repeated observations of the same pattern
    aggregate without rewriting the canonical exemplar.

    PII gate (CL-390): ``content`` runs through ``redact_for_log``; any
    redaction → ``PiiInContentError`` (no row inserted).

    VT-225 (per-tenant k-anon admission): when ``tenant_id`` is given, the
    contributing tenant is recorded in ``l0_cell_contributors`` IN THE SAME
    TXN as the fragment UPSERT (idempotent via the PK + ON CONFLICT DO
    NOTHING — strategy (c), no locks). The returned ``contributor_count`` is
    the DISTINCT-tenant count — the real k-anon signal (observation_count is
    a row counter that single-tenant poisoning can inflate). Legacy callers
    that omit ``tenant_id`` get ``contributor_count = None``.

    Returns ``{fragment_id, observation_count, inserted, contributor_count}``.
    """
    if _content_has_pii(content):
        raise PiiInContentError(fragment_type=fragment_type, cohort_key=cohort_key)

    pool = get_pool()
    contributor_count: int | None = None
    with pool.connection() as conn, conn.transaction():
        raw = conn.execute(
            """
            INSERT INTO l0_fragments (fragment_type, cohort_key, content)
            VALUES (%s, %s, %s)
            ON CONFLICT (fragment_type, cohort_key) DO UPDATE
              SET observation_count = l0_fragments.observation_count + 1,
                  last_observed_at = now()
            RETURNING id, observation_count, (xmax = 0) AS inserted
            """,
            (fragment_type, cohort_key, Jsonb(content)),
        ).fetchone()
        if raw is None:
            # UPSERT on a non-empty result always returns a row; defensive
            # branch for the impossible-but-typed path.
            raise RuntimeError(
                f"l0_fragments UPSERT returned no row "
                f"(fragment_type={fragment_type} cohort_key={cohort_key})"
            )
        row = cast("dict[str, Any]", raw)
        if tenant_id is not None:
            fragment_id = row["id"]
            conn.execute(
                "INSERT INTO l0_cell_contributors (fragment_id, tenant_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (str(fragment_id), str(tenant_id)),
            )
            cnt = conn.execute(
                "SELECT count(*) AS n FROM l0_cell_contributors WHERE fragment_id = %s",
                (str(fragment_id),),
            ).fetchone()
            contributor_count = int(cast("dict[str, Any]", cnt)["n"])
    return {
        "fragment_id": str(row["id"]),
        "observation_count": int(row["observation_count"]),
        "inserted": bool(row["inserted"]),
        "contributor_count": contributor_count,
    }


def query_l0(
    *,
    fragment_type: FragmentType,
    cohort_key: str,
    k: int = 5,
) -> dict[str, Any]:
    """SELECT up to ``k`` L0 fragments for (fragment_type, cohort_key).

    k-anonymity (CL-28, VT-225): only rows whose DISTINCT-tenant contributor
    count >= 10 are returned. This is the real k-anon gate — observation_count
    is a row counter a single tenant can inflate (poisoning), so the
    contributor-count subquery against ``l0_cell_contributors`` is the
    load-bearing predicate. ``observation_count >= 10`` is kept as a redundant
    transition-window double-check (a cell with 10 distinct contributors always
    has observation_count >= 10). Defence-in-depth over the SQL RLS policy.

    Order: most-recently-observed first.

    Returns ``{fragments: [...], matched_count: int}`` for the
    @tool_step decorator's output envelope.
    """
    pool = get_pool()
    with pool.connection() as conn:
        raw_rows = conn.execute(
            """
            SELECT id, fragment_type, cohort_key, content, observation_count,
                   last_observed_at
              FROM l0_fragments f
             WHERE f.fragment_type = %s
               AND f.cohort_key = %s
               AND f.observation_count >= %s
               AND (
                   SELECT count(*) FROM l0_cell_contributors c
                    WHERE c.fragment_id = f.id
               ) >= %s
             ORDER BY f.last_observed_at DESC
             LIMIT %s
            """,
            (fragment_type, cohort_key, K_ANONYMITY_THRESHOLD,
             K_ANONYMITY_THRESHOLD, k),
        ).fetchall()
    rows = cast("list[dict[str, Any]]", raw_rows)
    fragments = [
        {
            "fragment_id": str(row["id"]),
            "fragment_type": row["fragment_type"],
            "cohort_key": row["cohort_key"],
            "content": dict(row["content"])
            if isinstance(row["content"], dict)
            else row["content"],
            "observation_count": int(row["observation_count"]),
            "last_observed_at": row["last_observed_at"].isoformat(),
        }
        for row in rows
    ]
    return {"fragments": fragments, "matched_count": len(fragments)}


__all__ = [
    "FragmentType",
    "K_ANONYMITY_THRESHOLD",
    "PiiInContentError",
    "write_l0_fragment",
    "query_l0",
]
