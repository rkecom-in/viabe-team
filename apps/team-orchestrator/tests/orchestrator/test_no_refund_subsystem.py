"""VT-365 acceptance — the refund subsystem + trial extensions are GONE from prod src.

Pure (no DB). Greps the orchestrator source for the removed SUBSYSTEM symbols. We do NOT ban the
bare word 'refund'/'refunded' — those remain in legitimate, unrelated domains (a customer's
ledger refund in upi_export/imported_transactions, 'refund' as a VTR-escalation keyword, the
owner→customer `refund_processing` service ack, "no refunds on API spend" policy notes). The ban
list is the deleted billing-refund machinery + the removed trial-extension/auto-charge edges.
"""

from __future__ import annotations

import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "orchestrator"

_BANNED = (
    "refund_executor",
    "execute_refund",
    "razorpay_refund",
    "refund_executions",
    "day39_evaluator",
    "day39_refund",
    "refund_offered",
    "trial_extended",
    "MAX_TRIAL_EXTENSIONS",
    "card_captured",
    "trial_extension",
)


def test_refund_subsystem_symbols_grep_zero():
    hits: list[str] = []
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for sym in _BANNED:
            if sym in text:
                hits.append(f"{path.relative_to(_SRC)}: {sym}")
    assert not hits, f"VT-365: removed refund/extension symbols still present: {hits}"
