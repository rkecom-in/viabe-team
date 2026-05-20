"""Subprocess helper for the VT-3.4 Landmine 2 test — @DBOS.step + langgraph
node composition under a mid-step crash (VT-3.4 PR 2/3).

Run as: ``python _langgraph_step_resume_worker.py <db_url> <run_id>``

``run_id`` is used as BOTH the DBOS workflow_id AND the langgraph checkpointer
thread_id — the single-identifier convention established by runner.py
(``SetWorkflowID(run_id)`` + ``thread_id == run_id``; CL-209 refinement-2).

A langgraph graph (node_a -> node_b) is invoked inside ONE ``@DBOS.step``.
node_b inserts its probe row then sleeps — the test SIGKILLs the process
during that sleep, leaving the DBOS step PENDING. On the second launch DBOS
recovers the workflow and re-runs the step; the step re-detects langgraph's
post-node_a checkpoint and resumes (``invoke(None, ...)``) rather than
restarting, so node_a is NOT re-executed and only node_b runs again.

The filename has no ``test_`` prefix, so pytest does not collect it.
"""

from __future__ import annotations

import sys
import time
from typing import Any, TypedDict

import psycopg
import psycopg.errors
from dbos import DBOS, DBOSConfig, SetWorkflowID
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

_DB_URL = sys.argv[1] if len(sys.argv) > 2 else ""
_RUN_ID = sys.argv[2] if len(sys.argv) > 2 else ""

# node_b sleeps this long after its probe insert — the crash window the test
# SIGKILLs into. Short enough to keep the test fast; long enough to win the
# race against the test's probe poll.
_CRASH_WINDOW_SECONDS = 6


class _GraphState(TypedDict):
    """Minimal graph state — node identity is the probe table, not state."""

    thread_id: str
    history: list[str]


def _probe(thread_id: str, node_label: str) -> None:
    """Record one langgraph node execution. One row per execution — the test
    counts rows to detect DBOS-replay-induced double execution."""
    with psycopg.connect(_DB_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO _landgraph_node_probe (thread_id, node_label) "
            "VALUES (%s, %s)",
            (thread_id, node_label),
        )


def _node_a(state: _GraphState) -> dict[str, Any]:
    """Fast node — completes and checkpoints before the crash window."""
    _probe(state["thread_id"], "node_a")
    return {"history": [*state["history"], "node_a"]}


def _node_b(state: _GraphState) -> dict[str, Any]:
    """Node containing the crash window — probes, then sleeps. The test
    SIGKILLs the process during the sleep on the first launch."""
    _probe(state["thread_id"], "node_b")
    time.sleep(_CRASH_WINDOW_SECONDS)
    return {"history": [*state["history"], "node_b"]}


def _build_graph(checkpointer: PostgresSaver) -> Any:
    graph = StateGraph(_GraphState)
    graph.add_node("node_a", _node_a)
    graph.add_node("node_b", _node_b)
    graph.add_edge(START, "node_a")
    graph.add_edge("node_a", "node_b")
    graph.add_edge("node_b", END)
    return graph.compile(checkpointer=checkpointer)


@DBOS.step()
def invoke_graph_step(run_id: str) -> None:
    """Invoke the langgraph graph inside a single durable DBOS step.

    thread_id == run_id == DBOS workflow_id. A SIGKILL during node_b's crash
    window leaves this step PENDING; DBOS resume re-runs the whole step body.

    The step is replay-aware: if a checkpoint already exists for the thread
    (a crashed earlier attempt), it resumes with ``invoke(None, ...)`` so
    langgraph continues from node_a's checkpoint instead of restarting.
    """
    with PostgresSaver.from_conn_string(_DB_URL) as checkpointer:
        try:
            checkpointer.setup()  # creates checkpoint tables if absent
        except psycopg.errors.UniqueViolation:
            pass  # checkpoint_migrations already populated, schema present
        
        graph = _build_graph(checkpointer)
        config: RunnableConfig = {"configurable": {"thread_id": run_id}}
        if checkpointer.get_tuple(config) is None:
            graph.invoke({"thread_id": run_id, "history": []}, config=config)
        else:
            # An earlier (crashed) attempt left checkpoints — resume from the
            # last completed node rather than re-running the graph from START.
            graph.invoke(None, config=config)


@DBOS.workflow()
def resume_probe_workflow(run_id: str) -> str:
    invoke_graph_step(run_id)
    return "completed"


def main() -> None:
    config: DBOSConfig = {"name": "team-orchestrator", "database_url": _DB_URL}
    DBOS(config=config)
    DBOS.launch()  # recovers any workflow left PENDING by an earlier crash
    with SetWorkflowID(_RUN_ID):
        DBOS.start_workflow(resume_probe_workflow, _RUN_ID)
    time.sleep(40)  # stay alive while the workflow runs / recovery completes


if __name__ == "__main__":
    main()
