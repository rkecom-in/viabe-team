"""VT-719 S3 — the Manager's ASSERTED-FACTS ledger (CL-2026-07-28-single-voice-manager).

A durable, tenant-scoped record of what the Manager has TOLD the owner — fact_key + typed
fact_value + the sentence as said + provenance (including the exact O8 card version, §12.3).
"Never contradict yourself" is only enforceable against a record of what was said; this module
is that record and its deterministic reads.

Posture (mirrors conversation_log): voice-advisory, never an effect gate. Writes are fail-soft
(an assertion record must never break the send that carried it); the contradiction read is
deterministic key-equality — no LLM in this module, ever (the composer decides HOW to own a
change; this module only says THAT a prior assertion differs).

Supersession is append-only: recording a different value for an active key inserts the new row
and flips the prior row to status='superseded' + superseded_by — never a destructive update.
DSR: registered in dsr_purge._PURGE_ORDER (same change set as migration 187).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

#: Canonical fact keys — the registry of commitments the deterministic writers may record.
#: Grow this set deliberately (a key is a contract: the contradiction check joins on it).
#: Free-text keys are rejected so two surfaces can never record the same commitment under
#: different spellings and dodge the contradiction check.
FACT_KEYS: frozenset[str] = frozenset(
    {
        "weekly_report_day",       # which day the owner's weekly report lands
        "dormancy_definition",     # what "lapsed customer" means (the 45d one-definition, VT-632)
        "trial_terms",             # the agent free-trial promise (1 month, paid only on continue)
        "message_frequency_cap",   # per-customer contact frequency the Manager committed to
        "spend_ceiling",           # autonomous-spend bound the Manager stated
        "active_agent",            # which specialist the owner picked / was told is active
        "business_identity",       # the confirmed business identity line (name/city/what-it-does)
        "week_plan",               # the rolling 7-day plan (VT-721) — owner-visible on ask, so a
                                   # revision must supersede (owned change), never silently differ
    }
)


def record_assertion(
    tenant_id: UUID | str,
    fact_key: str,
    fact_value: Any,
    *,
    statement_text: str = "",
    surface: str = "manager",
    message_sid: str | None = None,
    derived_from_card_id: UUID | str | None = None,
    derived_from: dict[str, Any] | None = None,
) -> bool:
    """Record a fact the Manager just told the owner. Returns True on a recorded row.

    If an ACTIVE assertion exists for the key with a DIFFERENT value, that row is flipped to
    'superseded' (superseded_by → the new row) — the caller is expected to have OWNED the change
    in the outgoing text (the composer's job; ``contradiction_check`` is how it finds out).
    Same-value re-assertions are no-ops (the ledger stays one-row-per-active-fact).
    Fail-soft: any error logs and returns False — never breaks the send path.
    """
    if fact_key not in FACT_KEYS:
        logger.warning("asserted_facts: unknown fact_key %r rejected (registry-only)", fact_key)
        return False
    try:
        from orchestrator.db import tenant_connection

        value_json = json.dumps(fact_value, sort_keys=True, default=str)
        with tenant_connection(tenant_id) as conn:
            prior = conn.execute(
                "SELECT id, fact_value FROM manager_asserted_facts "
                "WHERE tenant_id = %s AND fact_key = %s AND status = 'active' "
                "ORDER BY asserted_at DESC LIMIT 1",
                (str(tenant_id), fact_key),
            ).fetchone()
            prior_id, prior_value = (None, None)
            if prior is not None:
                prior_id = prior["id"] if isinstance(prior, dict) else prior[0]
                prior_value = prior["fact_value"] if isinstance(prior, dict) else prior[1]
            if prior_id is not None and json.dumps(prior_value, sort_keys=True, default=str) == value_json:
                return True  # same value already active — nothing to record
            row = conn.execute(
                "INSERT INTO manager_asserted_facts "
                "(tenant_id, fact_key, fact_value, statement_text, surface, message_sid, "
                " derived_from_card_id, derived_from) "
                "VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb) RETURNING id",
                (
                    str(tenant_id), fact_key, value_json, statement_text[:1000], surface,
                    message_sid,
                    str(derived_from_card_id) if derived_from_card_id else None,
                    json.dumps(derived_from or {}, sort_keys=True, default=str),
                ),
            ).fetchone()
            new_id = row["id"] if isinstance(row, dict) else row[0]
            if prior_id is not None:
                conn.execute(
                    "UPDATE manager_asserted_facts "
                    "SET status = 'superseded', superseded_by = %s "
                    "WHERE id = %s AND tenant_id = %s",
                    (new_id, prior_id, str(tenant_id)),
                )
        return True
    except Exception:  # noqa: BLE001 — the ledger must never break the send that carried the fact
        logger.warning("asserted_facts: record failed (fail-soft) tenant=%s key=%s", tenant_id, fact_key, exc_info=True)
        return False


def active_assertion(tenant_id: UUID | str, fact_key: str) -> dict[str, Any] | None:
    """The latest ACTIVE assertion for a key, or None. Fail-soft → None."""
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            r = conn.execute(
                "SELECT fact_key, fact_value, statement_text, asserted_at, surface, "
                "       derived_from_card_id "
                "FROM manager_asserted_facts "
                "WHERE tenant_id = %s AND fact_key = %s AND status = 'active' "
                "ORDER BY asserted_at DESC LIMIT 1",
                (str(tenant_id), fact_key),
            ).fetchone()
        if r is None:
            return None
        if isinstance(r, dict):
            return dict(r)
        return {
            "fact_key": r[0], "fact_value": r[1], "statement_text": r[2],
            "asserted_at": r[3], "surface": r[4], "derived_from_card_id": r[5],
        }
    except Exception:  # noqa: BLE001 — advisory read, never a gate
        logger.warning("asserted_facts: read failed (fail-soft) tenant=%s key=%s", tenant_id, fact_key, exc_info=True)
        return None


def active_assertions(tenant_id: UUID | str) -> list[dict[str, Any]]:
    """ALL active assertions for a tenant (one per fact_key by construction) — the compose-context
    block: what the Manager has already told this owner. Fail-soft → []."""
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            rows = conn.execute(
                "SELECT fact_key, fact_value, statement_text, asserted_at "
                "FROM manager_asserted_facts "
                "WHERE tenant_id = %s AND status = 'active' "
                "ORDER BY fact_key, asserted_at DESC",
                (str(tenant_id),),
            ).fetchall()
        out = []
        for r in rows:
            if isinstance(r, dict):
                out.append(dict(r))
            else:
                out.append({"fact_key": r[0], "fact_value": r[1], "statement_text": r[2], "asserted_at": r[3]})
        return out
    except Exception:  # noqa: BLE001 — advisory read, never a gate
        logger.warning("asserted_facts: bulk read failed (fail-soft) tenant=%s", tenant_id, exc_info=True)
        return []


def contradiction_check(
    tenant_id: UUID | str, fact_key: str, new_value: Any
) -> dict[str, Any] | None:
    """Deterministic contradiction substrate for the compose seam: returns the PRIOR active
    assertion iff it exists and its value differs from ``new_value`` — the composer must then
    OWN the change ("earlier I said X — that's now Y because…") or keep the old value.
    None = no conflict (no prior, same value, or unknown key). Never raises."""
    if fact_key not in FACT_KEYS:
        return None
    prior = active_assertion(tenant_id, fact_key)
    if prior is None:
        return None
    try:
        same = json.dumps(prior.get("fact_value"), sort_keys=True, default=str) == json.dumps(
            new_value, sort_keys=True, default=str
        )
    except Exception:  # noqa: BLE001 — un-serializable value → treat as differing, surface the prior
        same = False
    return None if same else prior


def assertions_derived_from_card(
    tenant_id: UUID | str, card_id: UUID | str
) -> list[dict[str, Any]]:
    """The O8 §12.3 supersession sweep (per tenant): ACTIVE assertions derived from a given
    card version. The sweep caller iterates tenants and queues owned-change corrections through
    the owner_comms_queue. Fail-soft → []."""
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            rows = conn.execute(
                "SELECT fact_key, fact_value, statement_text, asserted_at "
                "FROM manager_asserted_facts "
                "WHERE tenant_id = %s AND derived_from_card_id = %s AND status = 'active' "
                "ORDER BY asserted_at DESC",
                (str(tenant_id), str(card_id)),
            ).fetchall()
        out = []
        for r in rows:
            if isinstance(r, dict):
                out.append(dict(r))
            else:
                out.append({"fact_key": r[0], "fact_value": r[1], "statement_text": r[2], "asserted_at": r[3]})
        return out
    except Exception:  # noqa: BLE001
        logger.warning("asserted_facts: card sweep read failed (fail-soft) tenant=%s", tenant_id, exc_info=True)
        return []


__all__ = [
    "FACT_KEYS",
    "active_assertion",
    "active_assertions",
    "assertions_derived_from_card",
    "contradiction_check",
    "record_assertion",
]
