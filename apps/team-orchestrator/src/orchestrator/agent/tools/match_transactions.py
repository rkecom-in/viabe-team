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
from datetime import datetime
from typing import Any, Literal

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
    """Fetch ledger entries within ±24h of any input transaction.

    Returns empty list when the customer_ledger_entries table is absent
    (forward-target schema; matches VT-40 pattern).
    """
    if not transactions:
        return []
    earliest = min(t.timestamp for t in transactions)
    latest = max(t.timestamp for t in transactions)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SET LOCAL app.current_tenant = %s", (tenant_id,),
            )
            try:
                cur.execute(
                    """
                    SELECT id::text AS id, amount_paise, entry_ts, ref_vpa
                    FROM customer_ledger_entries
                    WHERE tenant_id = %s
                      AND entry_ts >= %s - interval '1 day'
                      AND entry_ts <= %s + interval '1 day'
                    """,
                    (tenant_id, earliest, latest),
                )
                rows = cur.fetchall()
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ != "UndefinedTable":
                    raise
                return []
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            out.append(
                {
                    "id": r.get("id"),
                    "amount_paise": r.get("amount_paise"),
                    "entry_ts": r.get("entry_ts"),
                    "ref_vpa": r.get("ref_vpa"),
                },
            )
        else:
            out.append(
                {
                    "id": r[0],
                    "amount_paise": r[1],
                    "entry_ts": r[2],
                    "ref_vpa": r[3],
                },
            )
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


__all__ = [
    "AttributionMethod",
    "MatchTransactionsInput",
    "MatchTransactionsOutput",
    "TransactionInput",
    "TransactionMatch",
    "UnmatchedTransaction",
    "attribution_method_from_match_basis",
    "match_transactions",
]
