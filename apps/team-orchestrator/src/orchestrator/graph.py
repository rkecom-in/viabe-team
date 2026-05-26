"""LangGraph substrate for the orchestrator (VT-3.1).

Pillar 1: this graph is the SUBSTRATE the orchestrator-agent runs ON. It holds
NO LLM calls, NO reasoning, NO tool invocations — graph nodes only route and
persist state. Real nodes land in VT-3.2 / VT-3.8 / VT-3.9.

Pillar 8: one graph, one module-level checkpointer. No parallel mechanisms.
"""

from __future__ import annotations

from typing import Any, TypedDict
from uuid import UUID

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# LangGraph checkpoint tables get RLS keyed on thread_id -> pipeline_runs.
_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


class OrchestratorState(TypedDict):
    """Single source of truth for graph state shape.

    Expanded into the richer SubscriberState in VT-3.2 — for the VT-3.1
    substrate only these three fields exist.
    """

    tenant_id: UUID
    run_id: UUID
    history: list[str]


def placeholder_node(state: OrchestratorState) -> dict[str, list[str]]:
    """Plumbing-only node. Appends a marker so end-to-end runs are observable.

    Replaced by real nodes in VT-3.2 / VT-3.8 / VT-3.9 — there is deliberately
    no reasoning here.
    """
    return {"history": [*state["history"], "placeholder_node"]}


def build_graph() -> StateGraph:
    """Build the (uncompiled) orchestrator state graph: START -> node -> END."""
    graph: StateGraph = StateGraph(OrchestratorState)
    # observability:opt-out reason=VT-3.1-placeholder-node-pre-agent-substrate
    graph.add_node("placeholder_node", placeholder_node)
    graph.add_edge(START, "placeholder_node")
    graph.add_edge("placeholder_node", END)
    return graph


_pool: ConnectionPool | None = None
_compiled: Any | None = None


def _reset_connection(conn: Any) -> None:
    """Pool reset callback — defence-in-depth for tenant_connection (CL-122).

    tenant_connection() does SET ROLE app_role + sets app.current_tenant, and
    clears both in its own finally. This runs when a connection is returned to
    the pool, so no SET ROLE / GUC can leak to the next borrower even if that
    finally is bypassed.
    """
    with conn.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute("SELECT set_config('app.current_tenant', '', false)")


def _setup_checkpoint_rls(pool: ConnectionPool) -> None:
    """Pillar 3: tenant-isolate the LangGraph checkpoint tables.

    thread_id == run_id; a checkpoint row is visible only when its run belongs
    to the current tenant (the ``app.current_tenant`` GUC, via the
    ``app_current_tenant()`` helper from migration 000b). The orchestrator's
    owning DB role bypasses RLS; a tenant-scoped role is filtered.
    """
    with pool.connection() as conn:
        for table in _CHECKPOINT_TABLES:
            conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            conn.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
            conn.execute(
                f"CREATE POLICY {table}_tenant_isolation ON {table} "
                "USING (thread_id::uuid IN ("
                "SELECT id FROM pipeline_runs WHERE tenant_id = app_current_tenant()))"
            )


def init_substrate(database_url: str) -> None:
    """Create the module-level PostgresSaver + compiled graph. Idempotent.

    The PostgresSaver is module-level and reused across invocations — no
    per-call connection. ``setup()`` creates the checkpoint tables; RLS is
    applied immediately after (Pillar 3, no retrofit).
    """
    global _pool, _compiled
    if _compiled is not None:
        return
    _pool = ConnectionPool(
        database_url,
        min_size=1,
        max_size=4,
        kwargs={"autocommit": True, "row_factory": dict_row},
        reset=_reset_connection,
        open=True,
    )
    saver = PostgresSaver(_pool)
    saver.setup()
    _setup_checkpoint_rls(_pool)
    _compiled = build_graph().compile(checkpointer=saver)


def get_compiled_graph() -> Any:
    """Return the compiled graph. Raises if init_substrate() has not run."""
    if _compiled is None:
        raise RuntimeError("init_substrate() not called — launch_dbos() first")
    return _compiled


def get_pool() -> ConnectionPool:
    """Return the shared connection pool. Raises if init_substrate() has not run."""
    if _pool is None:
        raise RuntimeError("init_substrate() not called — launch_dbos() first")
    return _pool


def reset_substrate() -> None:
    """Close and clear the module-level substrate.

    Used by shutdown_dbos() so a later launch_dbos() rebuilds cleanly — e.g.
    when a test suite cycles DBOS across more than one module.
    """
    global _pool, _compiled
    if _pool is not None:
        _pool.close()
    _pool = None
    _compiled = None
