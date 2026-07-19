"""VT-382 (CL-437 ruling 3) — outbox body redaction + owner-message audit capture.

The policy (near-verbatim): outbox message bodies are retained ONLY while needed for
delivery / retry / replay / drain. On terminal completion — drafts: 'sent' / 'skipped' /
'halted'; batches: 'sent' / 'rejected' / 'cancelled' — the body fields are redacted IN
PLACE, keeping metadata + hashes. For a SENT draft the RECONSTRUCTION SUBSTRATE is
captured FIRST into the tenant-scoped ``owner_message_audit`` surface (migration 135;
STEP-0 proved no surface held it before), in the SAME transaction as the status flip +
the redaction — atomic by construction: no window where the outbox copy is gone but the
audit row absent.

What the capture stores (CL-437 + Fazal 'accept' 2026-06-12 — the RULED interpretation):
the template REF, the resolved Twilio SID, and the ORDERED send-resolved variable values
— NOT a literal Meta-rendered body snapshot (the fixed approved body lives at Meta/Twilio,
not in our store). The EXACT owner-facing text is RECONSTRUCTIBLE by folding the ordered
values into the registry's pinned approved body for that template+language
(``body_sha256``-pinned in ``config/twilio_templates.yaml``; the pin is what makes the
reconstruction exact and drift-detectable); the SID pins which approved body was sent.

Non-terminal rows ('drafted' / 'sending'; batch 'edit_requested' — ``owner_feedback`` is
the regeneration input) are NEVER touched: retain-while-needed is itself the policy, not
just the redaction.

Body fields (mig 126):

- ``agent_drafts.params`` (jsonb) — each value is replaced with
  ``{"redacted": true, "sha256": <hex>}``. The hash is sha256 over the SAME ``str()``
  coercion the send path applies to the value (``agent_send_draft`` builds
  ``template_params={k: str(v) ...}``), so the redacted row keeps idempotency/forensics
  ("metadata and hashes kept") while the key set survives intact.
- ``agent_draft_batches.owner_feedback`` (text) — replaced with the marker string
  ``redacted:sha256:<hex>`` (sha256 of the utf-8 raw body).

Every helper is IDEMPOTENT (already-redacted values pass through unchanged — a second
pass never re-hashes a hash) and runs on the CALLER's connection so the hook sites
compose capture + redaction into the terminal transition's own transaction. The terminal
status guards live in the SQL here, not in caller discipline: a non-terminal row can not
be redacted through these helpers.

``sweep_terminal_rows`` is the daily backfill/backstop (CL-437 ruling 3.3; registered in
``scheduled_triggers.outbox_redaction_sweep_scheduled``): it redacts rows ALREADY
terminal that the inline hooks never ran for, and — one-shot policy honesty — a
historical 'sent' draft still holding raw params has its exact owner-facing text
reconstructed (the same send-path resolution) and captured into ``owner_message_audit``
BEFORE the redaction, so no sent text is silently destroyed.

CL-390: no body text in logs — tenant/draft/batch ids + counts only.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# The redacted owner_feedback marker: 'redacted:sha256:<hex>'.
FEEDBACK_MARKER_PREFIX = "redacted:sha256:"

# The terminal sets (CL-437 ruling 3 — exact; everything else is retain-while-needed).
DRAFT_TERMINAL_STATUSES: tuple[str, ...] = ("sent", "skipped", "halted")
BATCH_TERMINAL_STATUSES: tuple[str, ...] = ("sent", "rejected", "cancelled")

# Sweep batch width — small enough to keep each backfill transaction short.
_SWEEP_BATCH_SIZE = 200

# The sweep's marker for a 'drafted' child stranded under an already-terminal batch
# (the crash-window backstop — the inline close hook never flipped it). Mirrors the
# halt_drafted_reason markers redact_batch_close writes at the live close.
_SWEEP_HALT_REASON = "halted_sweep_terminal_batch"


def _col(row: Any, key: str, idx: int) -> Any:
    """Read a column from a psycopg row that may be a dict or a tuple."""
    return row[key] if isinstance(row, dict) else row[idx]


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pure redaction shapes (idempotent; the contract-pinned at-rest forms)
# ---------------------------------------------------------------------------


def is_redacted_value(value: Any) -> bool:
    """True iff ``value`` is already in the redacted params shape."""
    return (
        isinstance(value, dict)
        and value.get("redacted") is True
        and isinstance(value.get("sha256"), str)
    )


def redact_param_value(value: Any) -> dict[str, Any]:
    """One params value -> ``{"redacted": true, "sha256": <hex>}``.

    The hash is over ``str(value)`` — the SAME coercion ``agent_send_draft`` applies
    when building the transport payload — so a known plaintext remains verifiable
    against the redacted row (forensics) and a re-run is a no-op (idempotent).
    """
    if is_redacted_value(value):
        return value
    return {"redacted": True, "sha256": _sha256_hex(str(value))}


def redact_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Redact every value of a params dict; the KEY SET is metadata and survives."""
    return {key: redact_param_value(value) for key, value in (params or {}).items()}


def is_redacted_feedback(text: str | None) -> bool:
    return isinstance(text, str) and text.startswith(FEEDBACK_MARKER_PREFIX)


def redact_feedback_value(text: str) -> str:
    """owner_feedback body -> its sha256 marker string. Idempotent."""
    if is_redacted_feedback(text):
        return text
    return f"{FEEDBACK_MARKER_PREFIX}{_sha256_hex(text)}"


def render_owner_facing_text(
    template_name: str, params: dict[str, Any] | None, *, language: str = "en"
) -> str:
    """Resolve the owner-facing message EXACTLY as the send path resolved it.

    ``agent_send_draft`` coerces every param with ``str()`` and the VT-45 delegate maps
    them positionally through the registry's ordered ``variables`` tuple (the ``{{N}}``
    slots Twilio renders). This mirrors that resolution: registry variable order, same
    ``str()`` coercion. The fixed template body lives at Meta/Twilio (the registry is
    SID + signature only), so the faithful local capture is the resolved
    template + ordered rendered variables.

    Fallback (registry drift after the fact / historical sweep rows whose template left
    the registry): deterministic sorted key order — the values are still captured.
    """
    template_params = {k: str(v) for k, v in (params or {}).items()}
    try:
        from orchestrator.templates_registry import resolve as registry_resolve

        entry = registry_resolve(template_name, language)
        ordered = [(name, template_params.get(name, "")) for name in entry.variables]
    except Exception:  # noqa: BLE001 — registry drift must never lose the capture
        ordered = sorted(template_params.items())
    body = ", ".join(f"{name}: {value}" for name, value in ordered)
    return f"[{template_name}/{language}] {body}"


# ---------------------------------------------------------------------------
# Row-level operations (caller's connection — compose into the terminal txn)
# ---------------------------------------------------------------------------


def redact_draft_params(conn: Any, tenant_id: UUID | str, draft_ids: list[str]) -> int:
    """Redact ``params`` on the given drafts — ONLY rows already in a TERMINAL status.

    The status guard is in the SQL (retain-while-needed is structural, not caller
    discipline): a 'drafted'/'sending' row passed in by mistake is left untouched.
    Idempotent; returns how many rows were rewritten. No body text is logged (CL-390).
    """
    tid = str(tenant_id)
    ids = [str(d) for d in draft_ids]
    if not ids:
        return 0
    rows = conn.execute(
        "SELECT id::text AS id, params FROM agent_drafts "
        "WHERE tenant_id = %s AND id = ANY(%s::uuid[]) AND status = ANY(%s) "
        "FOR UPDATE",
        (tid, ids, list(DRAFT_TERMINAL_STATUSES)),
    ).fetchall()
    changed = 0
    for row in rows:
        rid = str(_col(row, "id", 0))
        params = _col(row, "params", 1) or {}
        if not params or all(is_redacted_value(v) for v in params.values()):
            continue  # idempotent: nothing raw left
        from psycopg.types.json import Jsonb  # lazy: keep module import dep-less

        conn.execute(
            "UPDATE agent_drafts SET params = %s, updated_at = now() "
            "WHERE tenant_id = %s AND id = %s",
            (Jsonb(redact_params(params)), tid, rid),
        )
        changed += 1
    if changed:
        logger.info("outbox_redaction: draft params redacted tenant=%s n=%d", tid, changed)
    return changed


def redact_batch_owner_feedback(
    conn: Any, tenant_id: UUID | str, batch_ids: list[str]
) -> int:
    """Redact ``owner_feedback`` on the given batches — ONLY rows already TERMINAL
    ('sent'/'rejected'/'cancelled'; 'edit_requested' is the regeneration input and is
    structurally excluded by the status guard). Idempotent; returns rows rewritten."""
    tid = str(tenant_id)
    bids = [str(b) for b in batch_ids]
    if not bids:
        return 0
    rows = conn.execute(
        "SELECT id::text AS id, owner_feedback FROM agent_draft_batches "
        "WHERE tenant_id = %s AND id = ANY(%s::uuid[]) AND status = ANY(%s) "
        "  AND owner_feedback IS NOT NULL "
        "FOR UPDATE",
        (tid, bids, list(BATCH_TERMINAL_STATUSES)),
    ).fetchall()
    changed = 0
    for row in rows:
        bid = str(_col(row, "id", 0))
        feedback = _col(row, "owner_feedback", 1)
        if is_redacted_feedback(feedback):
            continue
        conn.execute(
            "UPDATE agent_draft_batches SET owner_feedback = %s, updated_at = now() "
            "WHERE tenant_id = %s AND id = %s",
            (redact_feedback_value(feedback), tid, bid),
        )
        changed += 1
    if changed:
        logger.info(
            "outbox_redaction: owner_feedback redacted tenant=%s n=%d", tid, changed
        )
    return changed


def redact_batch_close(
    conn: Any,
    tenant_id: UUID | str,
    batch_ids: list[str],
    *,
    halt_drafted_reason: str | None = None,
) -> None:
    """The batch cancel/halt close hook (autonomy revoke/freeze, VTR cancel, executor
    unwind, approval-resolution terminal closes): redact ``owner_feedback`` on the
    now-terminal batches AND ``params`` on their 'skipped'/'halted' drafts, on the
    caller's connection.

    ``halt_drafted_reason`` (VT-382 gate F1): when set, child rows still 'drafted' are
    FIRST flipped to terminal 'halted' (``skip_reason`` = the given marker) — the
    ``apply_agent_decision`` terminal closes (rejected / edit-exhausted /
    timeout-cancelled) end the batch without the prior halt sweep the autonomy/VTR
    cancel paths run, and without this flip those children would sit 'drafted' forever:
    outside every redaction leg (the daily sweep correctly excludes non-terminal rows)
    with raw params at rest. The flip is guarded on the PARENT batch being terminal
    (status guard in the SQL, never caller discipline) and writes NO audit rows —
    nothing was sent. Idempotent: an already-halted child has no 'drafted' row to flip.

    'sent' drafts are deliberately NOT swept here: post-VT-382 each was captured +
    redacted at its own sent flip (idempotent no-op anyway), and a PRE-VT-382 sent row
    still holding raw params belongs to the daily sweep's capture-then-redact leg —
    never blind-redacted without its audit capture.
    """
    tid = str(tenant_id)
    bids = [str(b) for b in batch_ids]
    if not bids:
        return
    if halt_drafted_reason is not None:
        conn.execute(
            "UPDATE agent_drafts d SET status = 'halted', skip_reason = %s, "
            "updated_at = now() "
            "FROM agent_draft_batches b "
            "WHERE b.tenant_id = d.tenant_id AND b.id = d.batch_id "
            "  AND d.tenant_id = %s AND d.batch_id = ANY(%s::uuid[]) "
            "  AND d.status = 'drafted' AND b.status = ANY(%s)",
            (halt_drafted_reason, tid, bids, list(BATCH_TERMINAL_STATUSES)),
        )
    redact_batch_owner_feedback(conn, tid, bids)
    rows = conn.execute(
        "SELECT id::text AS id FROM agent_drafts "
        "WHERE tenant_id = %s AND batch_id = ANY(%s::uuid[]) "
        "  AND status IN ('skipped', 'halted')",
        (tid, bids),
    ).fetchall()
    draft_ids = [str(_col(r, "id", 0)) for r in rows]
    if draft_ids:
        redact_draft_params(conn, tid, draft_ids)


def capture_then_redact_draft(
    conn: Any,
    draft_row: dict[str, Any],
    *,
    tenant_id: UUID | str,
    message_sid: str | None = None,
    language: str = "en",
) -> None:
    """The drafts -> 'sent' terminal hook (CL-437.3 capture clause).

    1. INSERT the ``owner_message_audit`` row holding the RECONSTRUCTION SUBSTRATE — the
       template ref + the ordered send-resolved variable values (``render_owner_facing_text``,
       resolved the same way the send path resolved them) + the resolved Twilio SID. NOT
       a literal Meta-rendered body: the exact owner-facing text is RECONSTRUCTIBLE by
       folding these values into the registry's pinned approved body (``body_sha256``-pinned;
       CL-437 + Fazal 'accept' 2026-06-12). Idempotent per draft (WHERE NOT EXISTS + the
       mig-135 unique index).
    2. THEN redact the draft's params — on the SAME connection, i.e. the SAME
       transaction as the caller's status flip. Atomic: a failure anywhere rolls back
       capture, redaction AND the flip together (no window where the outbox copy is
       gone but the audit row absent — and no redacted row whose text was never
       captured).

    ``draft_row`` is the ``customer_send._load_draft`` dict (draft_id / batch_id /
    customer_id / template_name / params) carrying the RAW pre-redaction params.
    """
    tid = str(tenant_id)
    did = str(draft_row["draft_id"])
    rendered = render_owner_facing_text(
        draft_row["template_name"], draft_row.get("params") or {}, language=language
    )
    conn.execute(
        "INSERT INTO owner_message_audit "
        "  (tenant_id, draft_id, batch_id, customer_id, template_name, "
        "   rendered_text, message_sid) "
        "SELECT %s, %s, %s, %s, %s, %s, %s "
        "WHERE NOT EXISTS (SELECT 1 FROM owner_message_audit "
        "                  WHERE tenant_id = %s AND draft_id = %s)",
        (
            tid, did, str(draft_row["batch_id"]), draft_row.get("customer_id"),
            draft_row["template_name"], rendered, message_sid,
            tid, did,
        ),
    )
    redact_draft_params(conn, tid, [did])


# ---------------------------------------------------------------------------
# Daily sweep — backfill of rows ALREADY terminal (CL-437 ruling 3.3)
# ---------------------------------------------------------------------------


def sweep_terminal_rows(*, pool: Any | None = None) -> dict[str, int]:
    """Redact params/owner_feedback on rows ALREADY in a terminal status — the backfill
    clause + the reliability backstop for the inline hooks. Cross-tenant: runs on the
    privileged service pool (the dsr_purge precedent), batched, idempotent, counts-only
    logging.

    Three legs:

    - Leg 1 — terminal drafts ('sent'/'skipped'/'halted') still holding RAW params:
      redacted; a 'sent' row's exact owner-facing text is reconstructed (possible
      precisely BECAUSE the params are still raw) and captured into
      ``owner_message_audit`` BEFORE the redaction, in the same per-batch transaction
      (one-shot policy honesty). 'skipped'/'halted' rows capture nothing — no send
      happened.
    - Leg 2 — terminal batches still holding RAW ``owner_feedback``: redacted to the
      sha256 marker.
    - Leg 3 — 'drafted' children STRANDED under an already-terminal batch (the
      crash-window backstop): the live close (``redact_batch_close`` /
      ``apply_agent_decision`` / the autonomy/VTR cancel paths) flips the parent
      terminal and halt-flips the children atomically, but if it died between those
      two writes the child sits 'drafted' with raw params OUTSIDE every other leg
      (Legs 1-2 + the inline hooks all require a terminal status). This leg halts those
      children ('drafted' -> 'halted', ``skip_reason`` = ``halted_sweep_terminal_batch``,
      parent-terminal SQL guard) AND redacts their params in the same UPDATE. NO audit
      capture — nothing was ever sent (the recorded policy reading: capturing
      never-sent text would itself violate retention). So the backstop now genuinely
      covers children, not just the parent batch.

    Non-terminal rows whose PARENT is also non-terminal ('drafted'/'sending' under a
    live batch, batch 'edit_requested' — ``owner_feedback`` is the regeneration input)
    are structurally untouched: the terminal-status predicates are in the SQL.
    """
    if pool is None:
        from orchestrator.graph import get_pool

        pool = get_pool()

    counts = {
        "drafts_redacted": 0,
        "drafts_captured": 0,
        "batches_redacted": 0,
        "children_halted": 0,
    }

    # The "still raw" jsonb predicate: at least one params value NOT in the redacted
    # shape (empty params have nothing to redact and are excluded via COALESCE).
    raw_params_predicate = (
        "COALESCE((SELECT bool_and(jsonb_typeof(value) = 'object' AND value ? 'redacted') "
        "          FROM jsonb_each(params)), true) = false"
    )

    with pool.connection() as conn:
        # --- Leg 1: terminal drafts still holding raw params (capture 'sent' first) ---
        while True:
            with conn.transaction():
                rows = conn.execute(
                    "SELECT tenant_id::text AS tenant_id, id::text AS id, "
                    "       batch_id::text AS batch_id, customer_id::text AS customer_id, "
                    "       template_name, params, status, message_sid "
                    "FROM agent_drafts "
                    f"WHERE status = ANY(%s) AND {raw_params_predicate} "
                    "LIMIT %s FOR UPDATE SKIP LOCKED",
                    (list(DRAFT_TERMINAL_STATUSES), _SWEEP_BATCH_SIZE),
                ).fetchall()
                if not rows:
                    break
                from psycopg.types.json import Jsonb  # lazy: dep-less module import

                for row in rows:
                    tid = str(_col(row, "tenant_id", 0))
                    did = str(_col(row, "id", 1))
                    params = _col(row, "params", 5) or {}
                    status = str(_col(row, "status", 6))
                    if status == "sent":
                        # Capture BEFORE redacting — the raw params are the only
                        # remaining source of the sent text (one-shot honesty).
                        rendered = render_owner_facing_text(
                            str(_col(row, "template_name", 4)), params
                        )
                        conn.execute(
                            "INSERT INTO owner_message_audit "
                            "  (tenant_id, draft_id, batch_id, customer_id, "
                            "   template_name, rendered_text, message_sid) "
                            "SELECT %s, %s, %s, %s, %s, %s, %s "
                            "WHERE NOT EXISTS (SELECT 1 FROM owner_message_audit "
                            "                  WHERE tenant_id = %s AND draft_id = %s)",
                            (
                                tid, did, str(_col(row, "batch_id", 2)),
                                _col(row, "customer_id", 3),
                                _col(row, "template_name", 4), rendered,
                                _col(row, "message_sid", 7),
                                tid, did,
                            ),
                        )
                        counts["drafts_captured"] += 1
                    conn.execute(
                        "UPDATE agent_drafts SET params = %s, updated_at = now() "
                        "WHERE tenant_id = %s AND id = %s",
                        (Jsonb(redact_params(params)), tid, did),
                    )
                    counts["drafts_redacted"] += 1

        # --- Leg 2: terminal batches still holding raw owner_feedback ---
        while True:
            with conn.transaction():
                rows = conn.execute(
                    "SELECT tenant_id::text AS tenant_id, id::text AS id, owner_feedback "
                    "FROM agent_draft_batches "
                    "WHERE status = ANY(%s) AND owner_feedback IS NOT NULL "
                    "  AND owner_feedback NOT LIKE %s "
                    "LIMIT %s FOR UPDATE SKIP LOCKED",
                    (
                        list(BATCH_TERMINAL_STATUSES),
                        f"{FEEDBACK_MARKER_PREFIX}%",
                        _SWEEP_BATCH_SIZE,
                    ),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    tid = str(_col(row, "tenant_id", 0))
                    bid = str(_col(row, "id", 1))
                    feedback = _col(row, "owner_feedback", 2)
                    conn.execute(
                        "UPDATE agent_draft_batches SET owner_feedback = %s, "
                        "updated_at = now() WHERE tenant_id = %s AND id = %s",
                        (redact_feedback_value(feedback), tid, bid),
                    )
                    counts["batches_redacted"] += 1

        # --- Leg 3: 'drafted' children stranded under an ALREADY-terminal batch ---
        # The crash-window backstop: if a terminal batch close (apply_agent_decision /
        # the autonomy/VTR cancel paths) flipped the parent but died before
        # redact_batch_close's halt-flip ran, the child sits 'drafted' with RAW params
        # outside every other redaction leg (Legs 1-2 + the inline hooks all require a
        # terminal status). Here the sweep halts those children (parent-terminal SQL
        # guard, same convention as redact_batch_close.halt_drafted_reason) — they are
        # then terminal 'halted' and Leg 1 (next sweep) / the inline legs cover their
        # params; we redact them in the SAME batch transaction for promptness. NO audit
        # capture: nothing was ever sent (the recorded policy reading — capturing
        # never-sent text would itself violate retention).
        while True:
            with conn.transaction():
                rows = conn.execute(
                    "SELECT d.tenant_id::text AS tenant_id, d.id::text AS id, d.params "
                    "FROM agent_drafts d "
                    "JOIN agent_draft_batches b "
                    "  ON b.tenant_id = d.tenant_id AND b.id = d.batch_id "
                    "WHERE d.status = 'drafted' AND b.status = ANY(%s) "
                    "LIMIT %s FOR UPDATE OF d SKIP LOCKED",
                    (list(BATCH_TERMINAL_STATUSES), _SWEEP_BATCH_SIZE),
                ).fetchall()
                if not rows:
                    break
                from psycopg.types.json import Jsonb  # lazy: dep-less module import

                for row in rows:
                    tid = str(_col(row, "tenant_id", 0))
                    did = str(_col(row, "id", 1))
                    params = _col(row, "params", 2) or {}
                    # Flip 'drafted' -> terminal 'halted' (parent-terminal guard already
                    # satisfied by the JOIN) AND redact params in the same UPDATE — no
                    # window where the child is terminal but still raw. The status guard
                    # repeats in the WHERE so a concurrent flip never double-applies.
                    conn.execute(
                        "UPDATE agent_drafts SET status = 'halted', skip_reason = %s, "
                        "params = %s, updated_at = now() "
                        "WHERE tenant_id = %s AND id = %s AND status = 'drafted'",
                        (_SWEEP_HALT_REASON, Jsonb(redact_params(params)), tid, did),
                    )
                    counts["children_halted"] += 1

    logger.info(
        "outbox_redaction: sweep done drafts_redacted=%d drafts_captured=%d "
        "batches_redacted=%d children_halted=%d",
        counts["drafts_redacted"], counts["drafts_captured"],
        counts["batches_redacted"], counts["children_halted"],
    )
    return counts


__all__ = [
    "BATCH_TERMINAL_STATUSES",
    "DRAFT_TERMINAL_STATUSES",
    "FEEDBACK_MARKER_PREFIX",
    "capture_then_redact_draft",
    "is_redacted_feedback",
    "is_redacted_value",
    "redact_batch_close",
    "redact_batch_owner_feedback",
    "redact_draft_params",
    "redact_feedback_value",
    "redact_param_value",
    "redact_params",
    "render_owner_facing_text",
    "sweep_terminal_rows",
]
