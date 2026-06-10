"""VT-369 Gap-5 PR-1 — Sales Recovery specialist executor (plan §2 + CRITICAL-2).

Pipeline per dispatched work item: deterministic lapsed-customer DETECTION (pure SQL, zero-LLM,
version-aware consent) → per-candidate frozen fact BUNDLE (every number computed in Python) →
LLM DRAFTING of template params behind the CL-425 ``owner_inputs`` gate → deterministic
post-LLM GROUNDING validation (ungrounded candidates dropped, never repaired) → persist
``agent_draft_batches(awaiting_approval)`` + ``agent_drafts(drafted)`` → arm the Pillar-7
approval via the approval-wiring module.

CRITICAL-2 (the choke point is STRUCTURAL) — this agent is a PLAIN function-call LLM with NO
tool surface: ``AGENT_TOOLS`` is the empty tuple, pinned through
``tool_guardrail.assert_agent_tools_safe`` at import, and a structural test asserts the module
source/import-graph holds none of the forbidden sender capabilities (the VT-45 sender tools).
The agent emits ``template_name`` + ``params`` ONLY; the SOLE sender is the deterministic
``customer_send.agent_send_draft`` choke point (builder 3), which independently re-runs every
compliance gate (consent, opt-out, complaint, caps, 30d/90d suppression) at SEND time.

Privacy posture:
  - CL-425: the drafting LLM transmit is gated on ``tenants.owner_inputs`` (fail-closed) — the
    coordinator checks it at sweep AND dispatch; this executor re-checks before any transmit.
  - CL-390: logs carry IDs + counters ONLY — never a display name, phone, or fact bundle.
  - IDs-in-state (plan §3d): ``ItemExecutionResult`` carries a status + batch_id + counters;
    the fact bundle is built, used for ONE Messages-API call, and discarded — it never enters
    workflow state or DBOS step outputs.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import yaml

from orchestrator.agent.tool_guardrail import assert_agent_tools_safe
from orchestrator.agents.coordinator import AgentItemContext, ItemExecutionResult
from orchestrator.business_plan.store import OWNING_AGENTS
from orchestrator.db import tenant_connection

logger = logging.getLogger(__name__)

AGENT_NAME = "sales_recovery"
if AGENT_NAME not in OWNING_AGENTS:  # fail-loud at import — the registry key contract (plan §1.2)
    raise RuntimeError(
        f"{AGENT_NAME!r} is not a business_plan.store.OWNING_AGENTS member; "
        "the Gap-5 registry contract is broken"
    )

# CRITICAL-2: this agent holds NO tools — it is a plain function-call LLM. The empty surface is
# still run through the guardrail at import so any future tool addition trips
# ToolGuardrailViolation (or the structural no-sender test) instead of silently opening the
# send boundary.
AGENT_TOOLS: tuple[Any, ...] = ()
assert_agent_tools_safe(AGENT_TOOLS, surface="agents.sales_recovery_executor")

# ---------------------------------------------------------------------------
# Marketing-consent purpose limitation — MECHANICAL, not prose (plan §2.1 / §3a, Critic-1 fix #2)
# ---------------------------------------------------------------------------
#
# ███ LOUD AND DELIBERATE: THIS ALLOWLIST IS EMPTY AND THAT IS NOT A BUG. ███
#
# ``MARKETING_CONSENT_VERSIONS`` is the set of ``record_of_consent.consent_text_version`` values
# counsel has cleared for AUTOMATED MARKETING (win-back) use. Until counsel dependency C2
# (plan §3f) resolves, NO existing consent version is cleared, so this is ``frozenset()`` and
# detection is STRUCTURALLY fail-closed: zero candidates, always, even on a fully eligible
# customer base. Pre-existing/transactional consents are thereby excluded from marketing sends
# by construction (DPDP purpose limitation).
#
# Flipping it post-counsel = ONE constant change + a decisions-ledger entry (CL ref required).
# Do NOT "fix" the empty set; do NOT widen it from a test. Membership is checked in Python
# (short-circuit) AND as ``= ANY(%(versions)s)`` with a LIST parameter in SQL — NEVER a literal
# ``IN ()`` (MED-2: an empty literal IN () is a SQL syntax error, which would break the
# fail-closed property).
MARKETING_CONSENT_VERSIONS: frozenset[str] = frozenset()

# Detection thresholds (plan §2.1): recency at-or-above the tenant's p75 days-since-last-sale,
# lifetime spend at-or-above the tenant's p50 — computed over the tenant's customers WITH sales.
DETECTION_RECENCY_PERCENTILE = 0.75
DETECTION_SPEND_PERCENTILE = 0.50
DEFAULT_DETECTION_LIMIT = 50

# Cheap detection-time pre-filter (plan §2.3). The BINDING 30d/90d suppression is re-enforced at
# SEND time inside customer_send.check_agent_send_caps (builder 3) — this constant only keeps
# obviously-suppressed customers out of drafting.
RECONTACT_SUPPRESSION_DAYS = 30

# The ONLY template this executor may emit in PR-1. Registry/SID resolution + the
# category='customer_marketing' check happen at SEND time (fail-closed TemplateNotConfigured
# until the F1 Meta SIDs land).
WINBACK_TEMPLATE_NAME = "team_winback_simple"
WINBACK_TEMPLATE_PARAMS: tuple[str, ...] = ("customer_name", "days_since_last_visit")

_MODELS_YAML = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
# Drafting model slots: a dedicated ``agent_drafting`` models.yaml key wins when present (a
# DATA-only change to add); fallback is the ``business_plan`` slot — Sonnet prod / Haiku test,
# exactly the plan §2.2 "Haiku/Sonnet" band. NOT the ``sales_recovery`` slot: that is the VT-35
# Opus-calibrated agent-loop pin, not param drafting.
_DRAFTING_SLOT_PRIMARY = "agent_drafting"
_DRAFTING_SLOT_FALLBACK = "business_plan"
_MAX_OUTPUT_TOKENS = 512
_LLM_TIMEOUT_SECONDS = 30.0

# A param value that looks like a phone number is rejected REGARDLESS of grounding — drafts
# carry display-name-level PII at most (plan §2.4); E.164 shapes never enter ``params``.
_PHONE_SHAPE_RE = re.compile(r"\+?\d{8,}")

# Modules that may host builder-4's ``arm_agent_send_approval(tenant_id, run_id, batch_id,
# counts)`` — resolved lazily at call time (the wiring module lands in this same PR).
_ARM_FN_MODULES = (
    # approval_glue FIRST — the real home (adversarial-verify: omitting it made the live arm path
    # always raise → every batch cancelled approval_arm_failed; tests masked it via injected arm_fn).
    "orchestrator.agents.approval_glue",
    "orchestrator.agents.approval_wiring",
    "orchestrator.agents.customer_send",
    "orchestrator.agents.coordinator",
)
_ARM_FN_NAME = "arm_agent_send_approval"


# ---------------------------------------------------------------------------
# Value objects — IDs + numbers only; ``CustomerFactBundle`` is built, used for ONE LLM call,
# and discarded (never checkpointed, never logged).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LapsedCandidate:
    """One detection hit: customer_id + metrics ONLY (display_name is read separately into the
    fact bundle — candidates may be logged/counted, so they carry no name)."""

    customer_id: UUID
    days_since_last_sale: int
    last_sale_date: date
    lifetime_spend_paise: int


@dataclass(frozen=True, slots=True)
class CustomerFactBundle:
    """The Gap-4-style frozen grounding for ONE draft. Every number is computed in Python from
    ledger rows — the LLM only copies values, it never computes or invents them."""

    customer_id: UUID
    display_name: str | None
    days_since_last_sale: int
    last_sale_amount_paise: int
    lifetime_spend_paise: int


def _col(row: Any, key: str, idx: int) -> Any:
    """Read a column from a psycopg row that may be a dict or a tuple (consent.py pattern)."""
    return row[key] if isinstance(row, dict) else row[idx]


# ---------------------------------------------------------------------------
# Detection — deterministic SQL, zero-LLM, version-aware consent (plan §2.1)
# ---------------------------------------------------------------------------

# The consent EXISTS clause joins ``customers.phone_e164`` to ``record_of_consent.phone_token``
# by recomputing utils.phone_token.hash_phone IN SQL: 'phone_tok_' || hex(sha256(salt:phone)).
# DRIFT GUARD: a test pins the SQL expression against the Python hash_phone byte-for-byte; if
# VT-122 ever changes the tokenisation this drifts FAIL-CLOSED (no token match → no candidates)
# and the pin test fails loudly.
# The detection SQL itself lives in orchestrator.db.wrappers._LAPSED_CANDIDATES_SQL
# (CustomersWrapper.lapsed_candidates) — per-tenant customers SQL belongs to the wrapper
# layer (the no-direct-tenant-db-access lint). The drift-guard pin test targets it there.


def _phone_hash_salt() -> str:
    """The hash_phone salt (same env var, same failure mode — fail-loud, never empty-salt)."""
    salt = os.environ.get("TEAM_PHONE_HASH_SALT", "")
    if not salt:
        raise RuntimeError(
            "TEAM_PHONE_HASH_SALT not set (generate via: openssl rand -hex 32)"
        )
    return salt


def detect_lapsed_customers(
    tenant_id: UUID | str, *, conn: Any, limit: int = DEFAULT_DETECTION_LIMIT
) -> list[LapsedCandidate]:
    """Deterministic lapsed-customer detection (plan §2.1). Candidates are customers who are
    ``subscribed`` (not opted out), complaint-clear, hold an ACTIVE marketing-cleared consent row
    (``opted_out_at IS NULL`` AND ``consent_text_version`` in the C2 allowlist), sit at/above the
    tenant's p75 days-since-last-sale AND p50 lifetime spend, and have NO agent contact within
    the last ``RECONTACT_SUPPRESSION_DAYS`` — richest-first, capped at ``limit``.

    STRUCTURALLY FAIL-CLOSED: an empty ``MARKETING_CONSENT_VERSIONS`` returns ``[]`` before any
    SQL runs (and the SQL's ``= ANY(list)`` matches nothing either way). ``conn`` must be a
    ``tenant_connection`` (RLS-scoped) connection.
    """
    # Module-global read at CALL time (not bound at import) so the one-constant C2 flip — and
    # nothing subtler — changes behaviour.
    versions = sorted(MARKETING_CONSENT_VERSIONS)
    if not versions:
        return []
    from orchestrator.db.wrappers import CustomersWrapper

    rows = CustomersWrapper().lapsed_candidates(
        tenant_id,
        recency_pct=DETECTION_RECENCY_PERCENTILE,
        spend_pct=DETECTION_SPEND_PERCENTILE,
        salt=_phone_hash_salt(),
        versions=versions,
        suppression_days=RECONTACT_SUPPRESSION_DAYS,
        limit=limit,
        conn=conn,
    )
    return [
        LapsedCandidate(
            customer_id=UUID(str(_col(row, "customer_id", 0))),
            last_sale_date=_col(row, "last_sale_date", 1),
            days_since_last_sale=int(_col(row, "days_since_last_sale", 2)),
            lifetime_spend_paise=int(_col(row, "lifetime_spend_paise", 3)),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Fact bundle — every number computed in Python (plan §2.2)
# ---------------------------------------------------------------------------


def build_customer_fact_bundle(
    tenant_id: UUID | str, customer_id: UUID | str, *, conn: Any
) -> CustomerFactBundle:
    """The frozen per-customer grounding. Reads the customer's display_name and raw ``sale``
    ledger rows, then computes days_since_last_sale / last_sale_amount_paise /
    lifetime_spend_paise IN PYTHON (never SQL aggregates the LLM might be blamed for, never
    LLM arithmetic). NO raw phone, NO email. Raises ``LookupError`` when the customer or their
    sale history is missing (detection guarantees both; a miss is a bug, not a skip)."""
    tid, cid = str(tenant_id), str(customer_id)
    from orchestrator.db.wrappers import CustomersWrapper

    eligibility = CustomersWrapper().send_eligibility(tid, cid, conn=conn)
    if eligibility is None:
        raise LookupError(f"customer {cid} not found for tenant {tid}")
    raw_name = eligibility.get("display_name")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT entry_date, amount_paise FROM customer_ledger_entries "
            "WHERE tenant_id = %s AND customer_id = %s AND entry_type = 'sale' "
            "ORDER BY entry_date DESC, created_at DESC",
            (tid, cid),
        )
        sales = cur.fetchall()
    if not sales:
        raise LookupError(f"customer {cid} has no sale ledger rows — not a lapsed candidate")
    last_sale_date = _col(sales[0], "entry_date", 0)
    return CustomerFactBundle(
        customer_id=UUID(cid),
        display_name=str(raw_name) if raw_name else None,
        days_since_last_sale=(date.today() - last_sale_date).days,
        last_sale_amount_paise=int(_col(sales[0], "amount_paise", 1)),
        lifetime_spend_paise=sum(int(_col(r, "amount_paise", 1)) for r in sales),
    )


# ---------------------------------------------------------------------------
# Drafting — LLM behind the CL-425 gate; params are bundle literals (plan §2.2)
# ---------------------------------------------------------------------------


def _resolve_drafting_model() -> str:
    """models.yaml slot — VIABE_ENV=production → production model, else test (the house
    resolver pattern). See _DRAFTING_SLOT_* for the slot-preference rationale."""
    env = os.environ.get("VIABE_ENV", "test").lower()
    env_slot = "production" if env == "production" else "test"
    with open(_MODELS_YAML) as f:
        config = yaml.safe_load(f)
    slot = config.get(_DRAFTING_SLOT_PRIMARY) or config[_DRAFTING_SLOT_FALLBACK]
    return cast(str, slot[env_slot])


def _call_llm(prompt: str, model: str) -> str:
    """One non-streaming Messages call; returns the concatenated text blocks. max_retries=0:
    a failed/ungrounded draft is DROPPED (fail-closed), never retried into compliance."""
    from anthropic import Anthropic

    client = Anthropic(max_retries=0)
    resp = client.messages.create(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
        timeout=_LLM_TIMEOUT_SECONDS,
    )
    return "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()


def _allowed_param_values(bundle: CustomerFactBundle) -> dict[str, str | None]:
    """The EXACT literal each template param may carry, derived from the frozen bundle. This is
    both the LLM's menu and the validator's ground truth — one source, zero drift."""
    return {
        "customer_name": bundle.display_name,
        "days_since_last_visit": str(bundle.days_since_last_sale),
    }


def _build_draft_prompt(bundle: CustomerFactBundle) -> str:
    """The constrained drafting prompt: the model maps params to literals from
    ``<allowed_params>`` and NOTHING else (the GROUNDING discipline — validated after)."""
    allowed_json = json.dumps(_allowed_param_values(bundle), ensure_ascii=False, sort_keys=True)
    return (
        "You pick the WhatsApp template variable values for ONE win-back message to a lapsed "
        f"customer of a small Indian business. The template ({WINBACK_TEMPLATE_NAME}) is fixed "
        "and Meta-approved; you control ONLY the variable values.\n\n"
        f"<allowed_params>\n{allowed_json}\n</allowed_params>\n\n"
        "RULES (strict):\n"
        "- Respond with ONLY a JSON object mapping EVERY key in <allowed_params> to its value.\n"
        "- Copy every value LITERALLY from <allowed_params> — never invent, rephrase, compute, "
        "translate, or reformat a value.\n"
        "- Never output a phone number, an email address, or any fact not in <allowed_params>."
    )


def _parse_params(raw: str) -> dict[str, Any] | None:
    """Strict-JSON parse (tolerating a stray code fence). None on any shape failure."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _looks_like_phone(value: str) -> bool:
    normalized = re.sub(r"[\s\-()]", "", value)
    return bool(_PHONE_SHAPE_RE.search(normalized))


def validate_draft_params(params: Any, bundle: CustomerFactBundle) -> bool:
    """Deterministic post-LLM grounding validator (plan §2.2 — never relaxed). A draft passes
    iff its keys are EXACTLY the template params and every value is the exact bundle literal
    for that key. Phone-shaped values fail REGARDLESS of grounding (PII guard outranks the
    literal rule). Fail → the candidate is dropped + counted, never repaired."""
    if not isinstance(params, dict) or set(params) != set(WINBACK_TEMPLATE_PARAMS):
        return False
    allowed = _allowed_param_values(bundle)
    for key, value in params.items():
        if not isinstance(value, str) or not value.strip():
            return False
        if _looks_like_phone(value):
            return False
        if value != allowed.get(key):
            return False
    return True


# ---------------------------------------------------------------------------
# Persistence + approval arming
# ---------------------------------------------------------------------------


def _persist_draft_batch(
    tenant_id: UUID,
    *,
    work_item_id: UUID,
    drafts: list[tuple[UUID, dict[str, str]]],
    conn: Any,
) -> UUID:
    """One batch (``awaiting_approval``) + its ``drafted`` rows, atomically (one transaction —
    a half-persisted batch must never be armable). RLS via the caller's tenant_connection."""
    from psycopg.types.json import Jsonb

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status) "
            "VALUES (%s, %s, %s, 'awaiting_approval') RETURNING id",
            (str(tenant_id), str(work_item_id), AGENT_NAME),
        )
        row = cur.fetchone()
        assert row is not None
        batch_id = UUID(str(_col(row, "id", 0)))
        for customer_id, params in drafts:
            cur.execute(
                "INSERT INTO agent_drafts "
                "(tenant_id, batch_id, customer_id, template_name, params, status) "
                "VALUES (%s, %s, %s, %s, %s, 'drafted')",
                (str(tenant_id), str(batch_id), str(customer_id), WINBACK_TEMPLATE_NAME,
                 Jsonb(params)),
            )
    return batch_id


def _cancel_batch(tenant_id: UUID, batch_id: UUID, *, conn: Any, reason: str) -> None:
    """Fail-closed unwind when arming refuses: the batch can never sit armable without its
    Pillar-7 approval row. Drafts → halted with the machine-readable reason."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_draft_batches SET status = 'cancelled', updated_at = now() "
            "WHERE tenant_id = %s AND id = %s",
            (str(tenant_id), str(batch_id)),
        )
        cur.execute(
            "UPDATE agent_drafts SET status = 'halted', skip_reason = %s, updated_at = now() "
            "WHERE tenant_id = %s AND batch_id = %s",
            (reason, str(tenant_id), str(batch_id)),
        )


def _resolve_arm_fn() -> Any:
    """Builder-4's ``arm_agent_send_approval(tenant_id, run_id, batch_id, counts)`` — resolved
    lazily so this module never hard-binds the wiring module's final home (it lands in this same
    PR). Fail-loud when absent: an unarmed batch must never be silently left awaiting."""
    from importlib import import_module

    last_exc: Exception | None = None
    for module_name in _ARM_FN_MODULES:
        try:
            module = import_module(module_name)
        except ImportError as exc:
            last_exc = exc
            continue
        fn = getattr(module, _ARM_FN_NAME, None)
        if callable(fn):
            return fn
    raise RuntimeError(
        f"{_ARM_FN_NAME} not found in any of {_ARM_FN_MODULES} — approval wiring missing"
    ) from last_exc


def _owner_inputs_ok(tenant_id: UUID) -> bool:
    """CL-425 fail-closed consent re-check (the coordinator checks at sweep AND dispatch; this
    is the last gate before the PII-bearing transmit). Any read error fails CLOSED."""
    try:
        from orchestrator.memory.l0_writer import _owner_inputs_enabled

        return _owner_inputs_enabled(tenant_id)
    except Exception:  # noqa: BLE001 — never draft on an unknown consent state
        logger.warning(
            "sales_recovery: owner_inputs consent check failed (tenant=%s); fail-closed",
            tenant_id,
        )
        return False


# ---------------------------------------------------------------------------
# The specialist agent (coordinator SpecialistAgent protocol, plan §1.2)
# ---------------------------------------------------------------------------


class SalesRecoveryAgent:
    """Gap-5 Sales Recovery specialist. ``llm`` / ``arm_fn`` are injectable for tests; the
    zero-arg construction is the registry contract (``coordinator.get_registry``)."""

    name = AGENT_NAME

    def __init__(self, *, llm: Any | None = None, arm_fn: Any | None = None) -> None:
        self._llm = llm
        self._arm_fn = arm_fn

    def execute_item(self, ctx: AgentItemContext) -> ItemExecutionResult:
        """Detect → bundle → draft (CL-425-gated LLM) → validate grounding → persist →
        arm Pillar-7. Returns IDs + counters ONLY (IDs-in-state). Outcomes:

        - ``cancelled`` + ``skipped_owner_inputs`` — consent gate tripped; no LLM transmit.
        - ``cancelled`` + ``skipped_no_candidates`` — detection empty (ALWAYS, until the C2
          allowlist is populated); the work-item slot frees for the next sweep.
        - ``cancelled`` + ``skipped_no_grounded_drafts`` — every draft dropped ungrounded.
        - ``cancelled`` + ``approval_arm_failed`` — arming refused (e.g. the one-open-per-tenant
          mutex); the batch is cancelled fail-closed and the next sweep retries.
        - ``awaiting_approval`` + batch_id — drafts persisted, Pillar-7 approval armed.

        Unexpected exceptions PROPAGATE — the dispatch workflow's fail-soft marks the work item
        ``failed`` (a crashed item must look failed, not cancelled-clean)."""
        tenant_id = UUID(str(ctx.tenant_id))

        if not _owner_inputs_ok(tenant_id):
            return ItemExecutionResult(
                work_item_status="cancelled", counters={"skipped_owner_inputs": 1}
            )

        with tenant_connection(tenant_id) as conn:
            candidates = detect_lapsed_customers(tenant_id, conn=conn)
            bundles = [
                build_customer_fact_bundle(tenant_id, cand.customer_id, conn=conn)
                for cand in candidates
            ]
        if not candidates:
            return ItemExecutionResult(
                work_item_status="cancelled", counters={"skipped_no_candidates": 1}
            )

        # LLM phase — no DB connection held; each bundle is used for ONE call, then discarded.
        model = _resolve_drafting_model()
        call = self._llm or _call_llm
        grounded: list[tuple[UUID, dict[str, str]]] = []
        dropped = 0
        for bundle in bundles:
            allowed = _allowed_param_values(bundle)
            if any(not isinstance(v, str) or not v.strip() for v in allowed.values()):
                dropped += 1  # e.g. no display_name — nothing grounded to greet with
                continue
            try:
                params = _parse_params(call(_build_draft_prompt(bundle), model))
            except Exception:  # noqa: BLE001 — one bad LLM call drops ONE candidate, fail-closed
                logger.warning(
                    "sales_recovery: draft call failed (customer=%s); candidate dropped",
                    bundle.customer_id,
                )
                dropped += 1
                continue
            if params is None or not validate_draft_params(params, bundle):
                dropped += 1
                continue
            grounded.append(
                (bundle.customer_id, {k: str(params[k]) for k in WINBACK_TEMPLATE_PARAMS})
            )

        if not grounded:
            return ItemExecutionResult(
                work_item_status="cancelled",
                counters={"dropped_ungrounded": dropped, "skipped_no_grounded_drafts": 1},
            )

        with tenant_connection(tenant_id) as conn:
            batch_id = _persist_draft_batch(
                tenant_id, work_item_id=UUID(str(ctx.work_item_id)), drafts=grounded, conn=conn
            )

        counters = {"drafted": len(grounded), "dropped_ungrounded": dropped}
        try:
            arm = self._arm_fn or _resolve_arm_fn()
            arm(str(tenant_id), str(ctx.run_id), str(batch_id), dict(counters))
        except Exception:  # noqa: BLE001 — arm refusal = defer-to-next-sweep (plan §4.1)
            logger.exception(
                "sales_recovery: approval arming failed (batch=%s); batch cancelled fail-closed",
                batch_id,
            )
            with tenant_connection(tenant_id) as conn:
                _cancel_batch(tenant_id, batch_id, conn=conn, reason="approval_arm_failed")
            return ItemExecutionResult(
                work_item_status="cancelled",
                batch_id=str(batch_id),
                counters={**counters, "approval_arm_failed": 1},
            )

        logger.info(
            "sales_recovery: item=%s batch=%s drafted=%d dropped_ungrounded=%d",
            ctx.item_id,
            batch_id,
            len(grounded),
            dropped,
        )
        return ItemExecutionResult(
            work_item_status="awaiting_approval", batch_id=str(batch_id), counters=counters
        )


__all__ = [
    "AGENT_NAME",
    "AGENT_TOOLS",
    "DEFAULT_DETECTION_LIMIT",
    "DETECTION_RECENCY_PERCENTILE",
    "DETECTION_SPEND_PERCENTILE",
    "MARKETING_CONSENT_VERSIONS",
    "RECONTACT_SUPPRESSION_DAYS",
    "WINBACK_TEMPLATE_NAME",
    "WINBACK_TEMPLATE_PARAMS",
    "CustomerFactBundle",
    "LapsedCandidate",
    "SalesRecoveryAgent",
    "build_customer_fact_bundle",
    "detect_lapsed_customers",
    "validate_draft_params",
]
