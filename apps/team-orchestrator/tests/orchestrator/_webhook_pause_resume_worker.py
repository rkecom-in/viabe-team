"""Subprocess helper for the VT-374 webhook-pause kill-and-recover test (§10.2/N3).

Run as: ``python _webhook_pause_resume_worker.py <db_url> <workflow_id> <tenant_id> <run_id>``

Drives the LIVE ``webhook_pipeline_run`` for a tenant paused on ``webhook_inbound``.
The pre-``dispatch_brain`` controllable boundary (runner.py:591, N3) holds the run at
``hold_while_paused_durable`` — a CHECKPOINTED wait (each control read is its own
``@DBOS.step`` and the inter-poll wait is ``DBOS.sleep``). The test SIGKILLs the process
DURING that park, leaving the workflow PENDING; on the second launch DBOS recovery
re-enters the workflow body and resumes the hold. When the test releases the pause the
recovered run drains to ``completed``.

``dispatch_brain`` is stubbed to a deterministic completed result (no LLM / network):
the test exercises the PAUSE seam, not the brain. ``run_id == uuid5(NAMESPACE_URL,
message_sid)`` mirrors the twilio_ingress convention; the DBOS workflow_id is the
caller-supplied ``<workflow_id>`` so the parent test can address the same workflow
across the crash.

The owner-inputs consent flag + the workflow_controls pause are seeded by the parent
test BEFORE launch (this worker only drives the run). The body routes to the brain arm
(a plain status-ish inbound), so the durable hold is reached.

No ``test_`` prefix — pytest does not collect this file.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

_DB_URL = sys.argv[1] if len(sys.argv) > 4 else ""
_WORKFLOW_ID = sys.argv[2] if len(sys.argv) > 4 else ""
_TENANT_ID = sys.argv[3] if len(sys.argv) > 4 else ""
_RUN_ID = sys.argv[4] if len(sys.argv) > 4 else ""

os.environ["TEAM_SUPABASE_DB_URL"] = _DB_URL
os.environ.setdefault("DATABASE_URL", _DB_URL)
# Non-secret test stand-ins so the persistence boundary (hash_phone) + the twilio
# send shim load; the worker makes ZERO live external calls (dispatch_brain stubbed,
# the brain arm never sends).
os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt374-worker-salt")
os.environ.setdefault("TEAM_TWILIO_FROM_NUMBER", "+910000000000")
os.environ.setdefault("TEAM_TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TEAM_TWILIO_AUTH_TOKEN", "test-token")

from dbos import DBOS, SetWorkflowID  # noqa: E402

from dbos_config import launch_dbos  # noqa: E402


def _stub_dispatch_brain() -> None:
    """Replace the brain with a deterministic completed result — the lazy import in
    runner.webhook_pipeline_run resolves ``orchestrator.agent.dispatch.dispatch_brain``
    at CALL time, so patching the module attribute here takes effect inside the run."""
    import orchestrator.agent.dispatch as dispatch_mod

    def _completed(**_kwargs: object) -> object:
        return dispatch_mod.DispatchResult(final_status="completed", terminal_path=None)

    dispatch_mod.dispatch_brain = _completed  # type: ignore[assignment]


def main() -> None:
    _stub_dispatch_brain()
    launch_dbos()  # recovers any workflow left PENDING by an earlier crash

    from orchestrator.runner import webhook_pipeline_run

    fields = {
        "MessageSid": _RUN_ID,  # any non-empty sid; dedup is single-run here
        "From": "+15551110000",
        "To": "+15552220000",
        "Body": "hello what is my status please",
        "NumMedia": "0",
    }
    with SetWorkflowID(_WORKFLOW_ID):
        DBOS.start_workflow(webhook_pipeline_run, _TENANT_ID, _RUN_ID, fields)
    time.sleep(120)  # stay alive while the run parks / recovery completes


if __name__ == "__main__":
    main()
