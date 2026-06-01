"""VT-46 — match_transactions standalone tool.

Deterministic UPI/transaction-to-ledger matching. Pydantic IO; standalone
callable. NOT wired to an Agent yet (VT-4 SDK skeleton still Backlog).

Algorithm
- For each input transaction, score against every candidate ledger entry
  within a ±24h window around the transaction timestamp:
    score = amount_exact_match * 0.6
          + time_proximity     * 0.3
          + vpa_fuzzy_match    * 0.1
- amount_exact_match: 1.0 if amount_paise equal, else 0.0
- time_proximity: 1 - (|delta_sec| / 86400) clamped to [0, 1]
- vpa_fuzzy_match: case-insensitive substring on normalized VPA tokens
- Pick best score per transaction; require amount_exact_match for a
  declared match. Reject below 0.5 → unmatched.

NO PII (CL-390): logged fields are txn_id + counts only. VPA and
payer_name are consumed by the matcher but NEVER logged or returned
verbatim. match_basis is a short tag ("amount+time", "amount+vpa") —
not the raw values.

VT-240: each declared match also carries an ``attribution_method``
(exact_match / window_match) derived deterministically from
``match_basis`` via ``attribution_method_from_match_basis``. This is the
provenance substrate that attribution rows (migration 047) will store —
``attribution_method`` ← this field, ``attribution_confidence`` ← the
existing ``confidence`` field. The mapper is pure + reproducible (no float
ambiguity in the method choice) for the day-39 reproducibility gate.
VT-240 does NOT build the attributions writer (no writer exists yet —
VT-176) and does NOT lift VT-43.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

logger = logging.getLogger(__name__)

# The three values attributions.attribution_method may hold (migration 047
# CHECK). The matcher only ever PRODUCES the first two; manual_owner is
# owner-asserted via a separate path and never derived from a match_basis.
AttributionMethod = Literal["exact_match", "window_match", "manual_owner"]


def attribution_method_from_match_basis(match_basis: str) -> str:
    """Map a declared match's ``match_basis`` tag → ``attribution_method``.

    Pure + deterministic (Fazal day-39 reproducibility gate — same basis
    always yields the same method, no float comparison involved):

      - basis contains ``vpa`` (a strong payer identifier) → ``exact_match``
      - otherwise (``amount`` / ``amount+time``) → ``window_match``

    Only ever called on a DECLARED match, whose basis always contains
    ``amount`` (the matcher requires amount-exact for a match). ``manual_owner``
    is NOT produced here — it is owner-asserted on a different path. The
    non-vpa fallback is ``window_match`` (the weaker, conservative claim), so an
    unexpected basis can never be over-attributed as an exact match.
    """
    return "exact_match" if "vpa" in match_basis.split("+") else "window_match"


class TransactionInput(BaseModel):
    """One incoming transaction to match."""

    model_config = ConfigDict(frozen=True)

    txn_id: str = Field(..., min_length=1)
    amount_paise: int = Field(..., ge=0)
    timestamp: datetime
    vpa: str | None = None
    payer_name: str | None = None


class MatchTransactionsInput(BaseModel):
    """Tenant + transactions to match."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., min_length=1)
    transactions: list[TransactionInput] = Field(default_factory=list)


class TransactionMatch(BaseModel):
    """One matched transaction → ledger entry.

    VT-240: ``attribution_method`` is a computed field derived from
    ``match_basis`` so it can never drift from the basis — the mapper is the
    single source of truth. ``confidence`` doubles as the future
    ``attributions.attribution_confidence`` (already in [0,1]); no separate
    field is needed.
    """

    model_config = ConfigDict(frozen=True)

    txn_id: str
    ledger_entry_id: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    match_basis: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def attribution_method(self) -> str:
        """Provenance method (exact_match / window_match), derived from
        ``match_basis`` via the deterministic mapper."""
        return attribution_method_from_match_basis(self.match_basis)


class UnmatchedTransaction(BaseModel):
    """Transaction with no qualifying ledger candidate."""

    model_config = ConfigDict(frozen=True)

    txn_id: str
    reason: str


class MatchTransactionsOutput(BaseModel):
    """Resolved matches + unmatched bucket."""

    model_config = ConfigDict(frozen=True)

    matches: list[TransactionMatch] = Field(default_factory=list)
    unmatched: list[UnmatchedTransaction] = Field(default_factory=list)


def _normalize_vpa(s: str | None) -> str:
    return (s or "").strip().lower()


def _vpa_fuzzy_score(a: str | None, b: str | None) -> float:
    """Case-insensitive substring score in [0, 1]. Returns 0 when either
    side is empty."""
    na, nb = _normalize_vpa(a), _normalize_vpa(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.7
    return 0.0


def _time_proximity_score(txn_ts: datetime, ledger_ts: datetime) -> float:
    delta = abs((txn_ts - ledger_ts).total_seconds())
    if delta >= 86400.0:
        return 0.0
    return 1.0 - (delta / 86400.0)


def _score(txn: TransactionInput, ledger: dict[str, Any]) -> tuple[float, str]:
    """Return (composite_score, match_basis)."""
    ledger_amount = int(ledger.get("amount_paise", 0))
    ledger_ts = ledger["entry_ts"]
    amount_exact = 1.0 if txn.amount_paise == ledger_amount else 0.0
    time_prox = _time_proximity_score(txn.timestamp, ledger_ts)
    vpa_fuzz = _vpa_fuzzy_score(txn.vpa, ledger.get("ref_vpa"))
    composite = amount_exact * 0.6 + time_prox * 0.3 + vpa_fuzz * 0.1
    parts = []
    if amount_exact > 0:
        parts.append("amount")
    if time_prox > 0:
        parts.append("time")
    if vpa_fuzz > 0:
        parts.append("vpa")
    basis = "+".join(parts) if parts else "none"
    return composite, basis


def _fetch_candidate_ledger(
    pool: Any, tenant_id: str, transactions: list[TransactionInput],
) -> list[dict[str, Any]]:
    """Fetch attributed ledger entries within ±1 day of any input transaction.

    VT-275: reconciled to the CANONICAL customer_ledger_entries schema (061) —
    windows on ``entry_date`` (a DATE), scores on ``amount_paise``, and carries
    ``customer_id`` (the bridge attributes a parked import to that customer). The
    canonical ledger has NO entry_ts / ref_vpa, so the date is synthesised to
    midnight for the existing time-proximity scorer and ref_vpa is None (the
    graceful-degrade is gone — the schema is real). VPA precision for UPI is the
    UPI-scoped path (VT-57), not the shared ledger.
    """
    if not transactions:
        return []
    earliest = min(t.timestamp for t in transactions).date()
    latest = max(t.timestamp for t in transactions).date()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant_id,))
        cur.execute(
            """
            SELECT id::text AS id, customer_id::text AS customer_id,
                   amount_paise, entry_date
            FROM customer_ledger_entries
            WHERE tenant_id = %s
              AND entry_date >= %s - interval '1 day'
              AND entry_date <= %s + interval '1 day'
            """,
            (tenant_id, earliest, latest),
        )
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = r if isinstance(r, dict) else {
            "id": r[0], "customer_id": r[1], "amount_paise": r[2], "entry_date": r[3],
        }
        ed = d["entry_date"]
        entry_ts = ed if isinstance(ed, datetime) else datetime.combine(ed, time.min)
        out.append({
            "id": d["id"], "customer_id": d.get("customer_id"),
            "amount_paise": d["amount_paise"], "entry_ts": entry_ts, "ref_vpa": None,
        })
    return out


def match_transactions(
    payload: MatchTransactionsInput,
    *,
    pool: Any | None = None,
    candidate_ledger: list[dict[str, Any]] | None = None,
) -> MatchTransactionsOutput:
    """Match each input transaction against ledger candidates.

    `candidate_ledger` bypasses the DB fetch — tests inject; production
    callers pass `pool` and the helper fetches.
    """
    if candidate_ledger is None:
        if pool is None:
            from orchestrator.graph import get_pool

            pool = get_pool()
        candidate_ledger = _fetch_candidate_ledger(
            pool, payload.tenant_id, payload.transactions,
        )

    matches: list[TransactionMatch] = []
    unmatched: list[UnmatchedTransaction] = []

    for txn in payload.transactions:
        best_score = 0.0
        best_basis = "none"
        best_id: str | None = None
        for entry in candidate_ledger:
            score, basis = _score(txn, entry)
            if score > best_score:
                best_score = score
                best_basis = basis
                best_id = str(entry["id"])
        # amount-exact required + composite ≥ 0.5
        if best_id is not None and best_score >= 0.5 and "amount" in best_basis:
            matches.append(
                TransactionMatch(
                    txn_id=txn.txn_id,
                    ledger_entry_id=best_id,
                    confidence=round(best_score, 4),
                    match_basis=best_basis,
                ),
            )
        else:
            if not candidate_ledger:
                reason = "no_ledger_candidate"
            elif "amount" not in best_basis:
                reason = "no_amount_match"
            else:
                reason = "low_confidence"
            unmatched.append(
                UnmatchedTransaction(txn_id=txn.txn_id, reason=reason),
            )

    logger.info(
        "match_transactions: tenant=%s in=%d matched=%d unmatched=%d",
        payload.tenant_id, len(payload.transactions),
        len(matches), len(unmatched),
    )
    return MatchTransactionsOutput(matches=matches, unmatched=unmatched)


@dataclass(frozen=True)
class AttributionResult:
    """VT-275 bridge counts — NO PII (CL-390)."""

    scanned: int          # unattributed imported_transactions considered
    tentative_set: int    # scored amount+date suggestions written (status=tentative)


def attribute_imported_transactions(
    tenant_id: UUID | str, *, pool: Any | None = None
) -> AttributionResult:
    """VT-275 bridge: SUGGEST customers for unattributed parked imports.

    Reads ``imported_transactions`` WHERE customer_id IS NULL (status
    'unattributed'), scores each against existing ATTRIBUTED ledger entries
    (amount + entry_date ±1d, via ``match_transactions``), and on a qualifying
    match sets a TENTATIVE link (customer_id + attribution_status='tentative' +
    match_confidence) — it does NOT write the clean ledger (D2/Cowork: parked rows
    carry no VPA, so amount+date alone is too weak to confirm). Promotion to
    customer_ledger_entries happens only when status becomes 'confirmed' (owner
    confirmation / strong signal — a follow-up surface).

    Idempotent: only 'unattributed' rows are scanned, so a re-run never re-touches
    an already-tentative row. tenant_id from invocation context (P3); RLS via
    tenant_connection (CL-82/88). Returns counts only.
    """
    from orchestrator.db.tenant_connection import tenant_connection

    with tenant_connection(tenant_id) as conn:
        unattributed = conn.execute(
            """
            SELECT id::text AS id, amount_paise, txn_date
            FROM imported_transactions
            WHERE customer_id IS NULL AND attribution_status = 'unattributed'
            """
        ).fetchall()
    rows = [r if isinstance(r, dict) else {"id": r[0], "amount_paise": r[1], "txn_date": r[2]}
            for r in unattributed]
    if not rows:
        return AttributionResult(scanned=0, tentative_set=0)

    txns = [
        TransactionInput(
            txn_id=str(r["id"]), amount_paise=int(r["amount_paise"]),
            timestamp=datetime.combine(r["txn_date"], time.min),
        )
        for r in rows
    ]
    # Candidate attributed ledger entries (carry customer_id) in the date window.
    if pool is None:
        from orchestrator.graph import get_pool

        pool = get_pool()
    candidates = _fetch_candidate_ledger(pool, str(tenant_id), txns)
    ledger_to_customer = {
        c["id"]: c["customer_id"] for c in candidates if c.get("customer_id")
    }

    result = match_transactions(
        MatchTransactionsInput(tenant_id=str(tenant_id), transactions=txns),
        candidate_ledger=candidates,
    )

    tentative = 0
    with tenant_connection(tenant_id) as conn:
        for m in result.matches:
            customer_id = ledger_to_customer.get(m.ledger_entry_id)
            if customer_id is None:
                continue  # matched a ledger entry with no customer (shouldn't happen)
            cur = conn.execute(
                """
                UPDATE imported_transactions
                   SET customer_id = %s,
                       attribution_status = 'tentative',
                       match_confidence = %s
                 WHERE id = %s AND attribution_status = 'unattributed'
                """,
                (str(customer_id), m.confidence, m.txn_id),
            )
            if cur.rowcount and cur.rowcount > 0:
                tentative += 1

    logger.info(
        "attribute_imported_transactions: tenant=%s scanned=%d tentative=%d",
        tenant_id, len(rows), tentative,
    )
    return AttributionResult(scanned=len(rows), tentative_set=tentative)


__all__ = [
    "AttributionMethod",
    "AttributionResult",
    "MatchTransactionsInput",
    "MatchTransactionsOutput",
    "TransactionInput",
    "TransactionMatch",
    "UnmatchedTransaction",
    "attribute_imported_transactions",
    "attribution_method_from_match_basis",
    "match_transactions",
]
