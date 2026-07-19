"""Subprocess helper for the VT-606 round-3 test-adequacy item (c): ONE real DBOS crash/replay
test for ``manager_task_workflow``, mirroring ``tests/orchestrator/_langgraph_step_resume_worker.py``'s
own crash-window/probe pattern (Landmine 2).

Run as: ``python _manager_workflow_sigkill_worker.py <db_url> <tenant_id> <task_id> <workflow_id>``

``manager_task_workflow``'s OWN ``_dispatch_specialist_step`` (a real ``@DBOS.step()``) is left
COMPLETELY UNMODIFIED — DBOS's step-identity/exactly-once tracking is keyed on the decorated
function's qualname, so swapping the function itself would break replay semantics. Instead, the
SWAPPABLE dependency INSIDE it (``orchestrator.supervisor.build_supervisor_graph``, a lazy import)
is replaced with a minimal probed graph using a REAL ``PostgresSaver`` (so the checkpoint survives
the process kill, not just an in-memory one) — the probed node inserts a row then sleeps; the test
SIGKILLs during that sleep, leaving the DBOS step PENDING.

The filename has no ``test_`` prefix, so pytest does not collect it.
"""

from __future__ import annotations

import sys
import time

_DB_URL = sys.argv[1] if len(sys.argv) > 4 else ""
_TENANT_ID = sys.argv[2] if len(sys.argv) > 4 else ""
_TASK_ID = sys.argv[3] if len(sys.argv) > 4 else ""
_WORKFLOW_ID = sys.argv[4] if len(sys.argv) > 4 else ""

# The crash window — the test SIGKILLs during this sleep, on the FIRST invocation only.
_CRASH_WINDOW_SECONDS = 6

import os  # noqa: E402

os.environ["TEAM_SUPABASE_DB_URL"] = _DB_URL
os.environ.setdefault("TEAM_PHONE_HASH_SALT", "test-salt")

import psycopg  # noqa: E402


def _probe(step_label: str) -> None:
    with psycopg.connect(_DB_URL, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _manager_workflow_probe ("
            "id serial PRIMARY KEY, workflow_id text, step_label text, "
            "at timestamptz DEFAULT now())"
        )
        conn.execute(
            "INSERT INTO _manager_workflow_probe (workflow_id, step_label) VALUES (%s, %s)",
            (_WORKFLOW_ID, step_label),
        )


def _probe_count(step_label: str) -> int:
    with psycopg.connect(_DB_URL, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM _manager_workflow_probe WHERE workflow_id = %s AND step_label = %s",
            (_WORKFLOW_ID, step_label),
        ).fetchone()
    return int(row[0]) if row else 0


def main() -> None:
    from dbos import DBOS, SetWorkflowID

    # Import + patch BEFORE launch_dbos(): manager_task_workflow must be REGISTERED with DBOS's
    # global registry before DBOS.launch() runs its own auto-recovery-at-launch pass (the SECOND
    # process launch recovers the PENDING workflow left by the first) — importing after launch
    # would make that auto-recovery attempt log "not a registered workflow function" and skip it
    # (harmless here since the explicit start_workflow call below still picks it up, but avoided
    # for a clean recovery path).
    import orchestrator.manager.workflow as workflow_mod
    import orchestrator.supervisor as supervisor_mod
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.graph import END, START, StateGraph
    from orchestrator.manager.workflow import manager_task_workflow
    from orchestrator.state.agent_graph_state import AgentGraphState

    # Patch the ONE swappable dependency (never manager_task_workflow / _dispatch_specialist_step
    # themselves — both stay the REAL, unmodified DBOS-decorated functions, so DBOS's own
    # exactly-once step tracking is exercised for real, not bypassed).
    def _probed_node(state):
        _probe("dispatch")
        already_ran = _probe_count("dispatch") > 1
        if not already_ran:
            time.sleep(_CRASH_WINDOW_SECONDS)  # the crash window — first invocation only
        return {"manager_review_outcome": "complete", "messages": [AIMessage(content="done")]}

    # A REAL PostgresSaver (not InMemorySaver) — the checkpoint must survive the process kill.
    # Entered manually (not `with ... as saver`) so the connection stays open past this function's
    # return — graph.invoke() runs LATER, inside _dispatch_specialist_step, after build_supervisor_
    # graph (below) has already returned the compiled graph.
    _saver_cm = PostgresSaver.from_conn_string(_DB_URL)
    _saver = _saver_cm.__enter__()
    try:
        _saver.setup()
    except Exception:  # noqa: BLE001 — tables already exist from an earlier run/migration
        pass

    def _minimal_graph(model, checkpointer=None, *, mode=None):
        g = StateGraph(AgentGraphState)
        g.add_node("probed_node", _probed_node)
        g.add_edge(START, "probed_node")
        g.add_edge("probed_node", END)
        return g.compile(checkpointer=_saver)

    supervisor_mod.build_supervisor_graph = _minimal_graph
    # This test's OBJECT is the DBOS+LangGraph crash/replay mechanics of _dispatch_specialist_step
    # itself — not the downstream completion-verification chain (which has its own extensive,
    # separately-proven test coverage and would otherwise need a real/mocked Anthropic call here
    # too). Bypass it with a canned 'verified' so the workflow settles cleanly post-replay.
    workflow_mod._verify_completion_step = lambda tenant_id, task_id: ("verified", "")

    # dbos_config.launch_dbos() (not a bare DBOS(config=...) + DBOS.launch()) — manager_task_
    # workflow's OWN @DBOS.step()s (_claim_step et al.) use tenant_connection -> orchestrator.
    # graph.get_pool(), which ONLY launch_dbos() initializes (the app's own LangGraph substrate,
    # not just the DBOS system tables). Recovers any workflow left PENDING by an earlier crash —
    # now correctly finding manager_task_workflow already registered (imported above).
    from dbos_config import launch_dbos

    launch_dbos()

    with SetWorkflowID(_WORKFLOW_ID):
        DBOS.start_workflow(manager_task_workflow, _TENANT_ID, _TASK_ID)
    time.sleep(40)  # stay alive while the workflow runs / recovery completes


if __name__ == "__main__":
    main()
