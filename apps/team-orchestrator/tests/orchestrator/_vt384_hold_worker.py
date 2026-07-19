"""Subprocess helper for the VT-384 L3-hold kill-and-recover test (build contract §B3).

Run as: ``python _vt384_hold_worker.py <db_url> <workflow_id> <tenant_id> <batch_id>``

Starts the durable L3 hold workflow for an already-``auto_send_pending`` batch and waits on
its result — the hold is a CHECKPOINTED workflow parking on the run-control poll idiom (a
``DBOS.sleep`` loop; each poll its own @DBOS.step) until ``send_not_before`` passes. The
parent test SIGKILLs this process DURING the park, leaving the workflow PENDING; on the
second launch ``launch_dbos()`` recovers it and DBOS recovery re-enters the workflow body
and resumes the hold (mirrors ``_webhook_pause_resume_worker.py`` for the VT-374
webhook-pause N2 leg).

The parent seeds the batch with a FAR-future ``send_not_before`` (and a delivered anchor)
BEFORE launch, so the hold NEVER fires during the test — the test asserts the PARK SURVIVED
the restart (durability), not the eventual send. Belt-and-braces: the batch carries the C2
stop (empty ``MARKETING_CONSENT_VERSIONS``), so even if the window somehow elapsed the
wake-side re-evaluation fail-closes at the consent gate — the durability leg can never send.

The L3 grant + the auto_send_pending batch + the drafted customer + the far-future window are
seeded by the parent test BEFORE launch (this worker only REGISTERS + STARTS the hold). The
worker makes ZERO live external calls. The DBOS workflow_id MUST be the production
``l3_hold_{batch_id}`` keying so the parent can address the same workflow across the crash.

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
_BATCH_ID = sys.argv[4] if len(sys.argv) > 4 else ""

os.environ["TEAM_SUPABASE_DB_URL"] = _DB_URL
os.environ.setdefault("DATABASE_URL", _DB_URL)
# Non-secret test stand-ins so the persistence boundary + the twilio send shim load; the
# worker makes ZERO live external calls.
os.environ.setdefault("TEAM_PHONE_HASH_SALT", "local-test-salt-not-secret")
os.environ.setdefault("TEAM_TWILIO_FROM_NUMBER", "+910000000000")
os.environ.setdefault("TEAM_TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TEAM_TWILIO_AUTH_TOKEN", "test-token")

from dbos import DBOS, SetWorkflowID  # noqa: E402

from dbos_config import launch_dbos  # noqa: E402


def main() -> None:
    from orchestrator.agents import l3_hold

    # Register the hold workflow into the DBOS registry BEFORE launch (house pattern —
    # registration must be present when launch computes the app_version hash).
    l3_hold.register_l3_hold()
    launch_dbos()  # recovers any workflow left PENDING by an earlier crash

    # Start the durable hold under the production workflow_id so the parent test addresses
    # the SAME workflow across the crash + recovery. The batch's far-future send_not_before
    # (seeded by the parent) keeps it parked. We DON'T block on the handle (the webhook-pause
    # worker precedent): the workflow runs on a DBOS worker thread; the main thread just stays
    # alive while it parks so the parent can SIGKILL it mid-park, leaving the workflow PENDING.
    with SetWorkflowID(_WORKFLOW_ID):
        DBOS.start_workflow(l3_hold.l3_hold_workflow, _TENANT_ID, _BATCH_ID)
    time.sleep(120)  # stay alive while the hold parks / recovery completes


if __name__ == "__main__":
    main()
