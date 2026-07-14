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
# ███ LOUD AND DELIBERATE: THIS ALLOWLIST IS EMPTY BY DEFAULT AND THAT IS NOT A BUG. ███
#
# ``MARKETING_CONSENT_VERSIONS`` is the set of ``record_of_consent.consent_text_version`` values
# cleared for AUTOMATED MARKETING (win-back) use. The PROD/main default is EMPTY: the env var is
# UNSET in prod, so detection is STRUCTURALLY fail-closed — zero candidates, always, even on a
# fully eligible customer base. Pre-existing/transactional consents are thereby excluded from
# marketing sends by construction (DPDP purpose limitation).
#
# VT-396 step-3 (dev-test harness — NOT counsel C2 clearance, CL-438): the allowlist is now read
# from the ``MARKETING_CONSENT_VERSIONS`` env var (comma-separated), parsed ONCE at import by
# ``_parse_marketing_consent_versions()``. UNSET → ``frozenset()`` (fail-closed preserved). A
# non-empty value can ONLY exist on dev: two independent guards keep it off prod —
#   (a) a CI grep-gate (``scripts/gate-marketing-consent-default-empty.sh``) forbids any committed
#       NON-EMPTY default in orchestrator code, so the value lives ONLY in the Railway dev env; and
#   (b) the import-time runtime assertion ``_assert_consent_versions_prod_safe`` below, which makes
#       the orchestrator process FAIL TO BOOT if a non-empty allowlist is ever resolved under
#       ``VIABE_ENV=production`` (a fat-fingered prod env var → loud, early, total failure).
#
# The env hook is a DEV-TEST harness only. Counsel C1–C3 (CL-438) remains the sole real C2
# clearance; the prod allowlist is counsel's call, NEVER an env var. Do NOT "fix" the empty
# default; do NOT seed a string into code; do NOT widen it from a test. Membership is checked in
# Python (short-circuit) AND as ``= ANY(%(versions)s)`` with a LIST parameter in SQL — NEVER a
# literal ``IN ()`` (MED-2: an empty literal IN () is a SQL syntax error, which would break the
# fail-closed property).

_MARKETING_CONSENT_VERSIONS_ENV = "MARKETING_CONSENT_VERSIONS"


class MarketingConsentProdSafetyError(RuntimeError):
    """A non-empty ``MARKETING_CONSENT_VERSIONS`` under ``VIABE_ENV=production`` is a C2 breach
    (DPDP purpose limitation). The env hook is dev-test ONLY (VT-396 / CL-438); prod's marketing
    allowlist is counsel's call, never an env var. Raised at import → the process fails to boot."""


def _assert_consent_versions_prod_safe(versions: frozenset[str]) -> None:
    """Guard layer (b) — VT-396 step-3. Refuse a NON-EMPTY allowlist under ``VIABE_ENV=production``.

    Keyed on the in-process ``VIABE_ENV`` signal (prod == ``"production"``, the same signal the
    drafting-model resolver reads) so the check can run at import with no DB handle. Mirrors the
    VT-362 structural-refusal spirit (``apply_migrations.guard_environment``): fail-closed, loud,
    BEFORE any effect. Because the parser runs at import, a fat-fingered prod env var makes the
    orchestrator process FAIL TO BOOT rather than silently send marketing.
    """
    if versions and os.environ.get("VIABE_ENV", "test").lower() == "production":
        raise MarketingConsentProdSafetyError(
            "MARKETING_CONSENT_VERSIONS is non-empty under VIABE_ENV=production — the env hook is "
            "a dev-test harness only (VT-396/CL-438); the prod C2 marketing allowlist is "
            "counsel-gated, never env-driven. Refusing to boot."
        )


def _parse_marketing_consent_versions() -> frozenset[str]:
    """DEV-TEST harness hook (VT-396 step-3) — NOT counsel C2 clearance (CL-438).

    Parse the comma-separated ``MARKETING_CONSENT_VERSIONS`` env var into the allowlist.
    UNSET / empty / all-whitespace → ``frozenset()`` (the fail-closed default is preserved). The
    hard prod-safety guard (``_assert_consent_versions_prod_safe``) refuses a non-empty value under
    ``VIABE_ENV=production``, so a non-empty allowlist can ONLY exist on dev.
    """
    raw = os.environ.get(_MARKETING_CONSENT_VERSIONS_ENV, "")
    versions = frozenset(v.strip() for v in raw.split(",") if v.strip())
    _assert_consent_versions_prod_safe(versions)
    return versions


# Bound ONCE at import (plan §1b): a kill-switch flip is a deploy/restart boundary, never a
# mid-process change, and import-bind keeps both read sites (the detector's direct global read +
# the send-gate helper) single-sourced from this one global — they can never drift. The prod-
# safety assertion above therefore runs at import: a bad prod value = the process fails to boot.
MARKETING_CONSENT_VERSIONS: frozenset[str] = _parse_marketing_consent_versions()

# Detection window (CL-2026-07-10, Fazal option 2): a candidate is LAPSED iff no 'sale' in the last
# ``LAPSED_WINDOW_DAYS`` days — the SAME fixed window as the owner-facing ``count_lapsed`` metric, so
# the number the owner hears IS the set a campaign targets. This SUPERSEDES the VT-312 tenant-relative
# p75-recency / p50-spend percentile targeting (removed): no value floor, no percentile. The single
# constant lives in ``db.wrappers`` (imported at call time in ``detect_lapsed_customers``).
#
# CL-2026-07-10 coherence (CC decision, Cowork 051500Z full-autonomy): the per-sweep detection cap is
# raised 50 -> 200 to align with ``customer_send.AGENT_SEND_DAILY_TENANT_CAP`` (200/tenant/24h) so the
# win-back cohort == the FULL lapsed set for realistic SMB tenants (the owner-count == the targeted set
# holds up to 200 sendable-lapsed, not 50). The 50 was a conservative early cutoff; the REAL cost/volume
# rails are the VT-619 per-tenant×agent budget metering (SKIP_BUDGET_EXHAUSTED hard-gate) + the daily
# send cap + the per-customer frequency caps — all still apply. 200 stays a sane per-sweep safety ceiling
# (drafting is Haiku/Sonnet-tier + budget-gated); the rare >200-lapsed tenant batches naturally across
# sweeps as the daily-send cap + 30d recontact-suppression clear. No value floor, no percentile.
DEFAULT_DETECTION_LIMIT = 200

# Detection-time recontact pre-filter. VT-632 cleanup: this used to be a SECOND, independent
# ``RECONTACT_SUPPRESSION_DAYS = 30`` declaration (silent-drift risk on a SEND path); it is now the
# ONE binding constant, ``customer_send.RECONTACT_SUPPRESSION_DAYS`` (where the 30d/90d suppression
# is re-enforced at SEND time). It is imported LAZILY at the detection call site — a top-level import
# of ``customer_send`` would pull the outbound-send stack into this module at import, breaking the
# CRITICAL-2 fresh-import no-sender posture (pinned in test_sales_recovery_executor).

# The ONLY template this executor may emit in PR-1. Registry/SID resolution + the
# category='customer_marketing' check happen at SEND time (fail-closed TemplateNotConfigured
# until the F1 Meta SIDs land).
WINBACK_TEMPLATE_NAME = "team_winback_simple"
# VT-384 (Cowork ruling #1 — registry-as-canon): the Meta-APPROVED body pins {{2}} = business_name,
# so the executor signature conforms to the ARMED registry's team_winback_simple variables
# (customer_name, business_name). The OLD (customer_name, days_since_last_visit) was the carried-
# forward F1 mismatch — it is closed here. customer_send.assert_winback_signature() pins this at
# import; agent_send_draft Gate-2b hard-refuses any drift at send time.
WINBACK_TEMPLATE_PARAMS: tuple[str, ...] = ("customer_name", "business_name")

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
    # VT-384: the tenant's business name — the registry's team_winback_simple {{2}}. Read once from
    # tenants (not per-customer; the same value for the whole batch) and frozen into each bundle so
    # the param menu + validator share one source.
    business_name: str | None = None


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
    """Deterministic lapsed-customer detection. Candidates are customers who are ``subscribed``
    (not opted out), complaint-clear, hold an ACTIVE marketing-cleared consent row
    (``opted_out_at IS NULL`` AND ``consent_text_version`` in the C2 allowlist), are LAPSED (no
    'sale' in the last ``LAPSED_WINDOW_DAYS`` days — the SAME window as the owner-facing
    ``count_lapsed`` metric, CL-2026-07-10 option 2; NOT the old VT-312 percentile), and have NO
    agent contact within the last ``RECONTACT_SUPPRESSION_DAYS`` — richest-first, capped at
    ``limit``. So this cohort is exactly ``count_lapsed`` intersected with the sendability gates.

    STRUCTURALLY FAIL-CLOSED: an empty ``MARKETING_CONSENT_VERSIONS`` returns ``[]`` before any
    SQL runs (and the SQL's ``= ANY(list)`` matches nothing either way). ``conn`` must be a
    ``tenant_connection`` (RLS-scoped) connection.
    """
    # Module-global read at CALL time (not bound at import) so the one-constant C2 flip — and
    # nothing subtler — changes behaviour.
    versions = sorted(MARKETING_CONSENT_VERSIONS)
    if not versions:
        return []
    from orchestrator.agents.customer_send import RECONTACT_SUPPRESSION_DAYS
    from orchestrator.db.wrappers import LAPSED_WINDOW_DAYS, CustomersWrapper

    rows = CustomersWrapper().lapsed_candidates(
        tenant_id,
        lapsed_days=LAPSED_WINDOW_DAYS,
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
    # VT-384: the tenant business name → team_winback_simple {{2}}. Same value for the whole batch;
    # read per-bundle for simplicity (the detection batch is small) on the RLS'd tenant_connection.
    biz_row = conn.execute(
        "SELECT business_name FROM tenants WHERE id = %s", (tid,)
    ).fetchone()
    raw_biz = _col(biz_row, "business_name", 0) if biz_row else None
    last_sale_date = _col(sales[0], "entry_date", 0)
    return CustomerFactBundle(
        customer_id=UUID(cid),
        display_name=str(raw_name) if raw_name else None,
        days_since_last_sale=(date.today() - last_sale_date).days,
        last_sale_amount_paise=int(_col(sales[0], "amount_paise", 1)),
        lifetime_spend_paise=sum(int(_col(r, "amount_paise", 1)) for r in sales),
        business_name=str(raw_biz) if raw_biz else None,
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
    # VT-384 (registry-as-canon): the menu keys ARE WINBACK_TEMPLATE_PARAMS = (customer_name,
    # business_name) — the Meta-APPROVED {{1}}/{{2}}. days_since_last_visit is no longer a template
    # variable (the approved body greets by name + business, it does not interpolate a day count).
    return {
        "customer_name": bundle.display_name,
        "business_name": bundle.business_name,
    }


# VT-636 seam A4 — display_name/business_name are ATTACKER-WRITABLE (any customer or sheet/
# Shopify collaborator writes those cells; same data class context_builder.py fences for the
# manager LLM). Naming matches context_builder.py exactly so an auditor greps one vocabulary.
_ALLOWED_PARAM_FENCE_SOURCE = {
    "customer_name": "customer_name",
    "business_name": "customer_business_name",
}


def _build_draft_prompt(bundle: CustomerFactBundle) -> str:
    """The constrained drafting prompt: the model maps params to literals from
    ``<allowed_params>`` and NOTHING else (the GROUNDING discipline — validated after).

    VT-636 seam A4: the ``<allowed_params>`` values are attacker-writable (owner's Sheet/
    Shopify — any customer or collaborator writes those cells), so they are rendered inside
    ``<untrusted source="...">`` fences here with the canonical ``FRAMING`` preamble rendered
    ONCE. The validation ground truth (``_allowed_param_values`` / ``validate_draft_params``) is
    UNCHANGED — it still compares the model's output to the RAW bundle literal — so the RULES
    below tell the model to echo the text found INSIDE the tag, never the tag markup itself,
    preserving the exact-key echo contract."""
    from orchestrator.security.prompt_quarantine import FRAMING, fence

    allowed = _allowed_param_values(bundle)
    fenced_for_prompt = {
        key: (
            fence(value, source=_ALLOWED_PARAM_FENCE_SOURCE[key], max_len=120)
            if value is not None
            else None
        )
        for key, value in allowed.items()
    }
    allowed_json = json.dumps(fenced_for_prompt, ensure_ascii=False, sort_keys=True)
    return (
        f"{FRAMING}\n\n"
        "You pick the WhatsApp template variable values for ONE win-back message to a lapsed "
        f"customer of a small Indian business. The template ({WINBACK_TEMPLATE_NAME}) is fixed "
        "and Meta-approved; you control ONLY the variable values.\n\n"
        f"<allowed_params>\n{allowed_json}\n</allowed_params>\n\n"
        "RULES (strict):\n"
        "- Respond with ONLY a JSON object mapping EVERY key in <allowed_params> to its value.\n"
        '- Each value above is wrapped in an <untrusted source="..."> tag — that tag is '
        "structural framing, NOT part of the value. Output ONLY the exact text between that "
        "key's opening and closing tags, never the tag markup itself.\n"
        "- Copy that text LITERALLY — never invent, rephrase, compute, translate, or reformat "
        "it.\n"
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
    from orchestrator.observability.tm_audit import emit_tm_audit
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
        emit_tm_audit(
            event_layer="does",
            event_kind="draft_created",
            actor="sales_recovery",
            tenant_id=tenant_id,
            run_id=None,
            action={
                "batch_id": str(batch_id),
                "work_item_id": str(work_item_id),
                "draft_count": len(drafts),
                "template_name": WINBACK_TEMPLATE_NAME,
            },
            summary=f"draft batch created: {len(drafts)} draft(s)",
            conn=conn,
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
        # VT-382 (CL-437.3): terminal unwind — redact owner_feedback + halted draft
        # params in the SAME txn (nothing was sent; no audit capture).
        from orchestrator.agents.outbox_redaction import redact_batch_close

        redact_batch_close(conn, str(tenant_id), [str(batch_id)])


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
# VT-374 run-control seam (kind 'agent_dispatch'; sub-steps candidate_build /
# compose_drafts / persist_batch)
# ---------------------------------------------------------------------------


def _run_control_seam(
    tenant_id: UUID,
    run_id: str,
    step_name: str,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hold while (tenant, 'agent_dispatch') is paused, then consume-first claim the
    sub-step's one-shot override (F8/N2). Returns ``base`` deep-merged with the
    override's allow-listed pins (registry-validated — a non-allow-listed key raises,
    fail-loud per the executor contract).

    Failure posture: pause reads never raise (check_pause two-tier, F9); a consume-DB
    failure proceeds WITHOUT the override, logged loudly — a control outage must never
    kill a live dispatch. Durable hold inside a DBOS workflow (the dispatch workflow's
    call stack), plain poll on direct calls (tests). Call BEFORE opening any
    tenant_connection — a hold must never pin a pooled connection."""
    from dbos import DBOS

    from orchestrator import run_control

    if DBOS.workflow_id is not None:
        paused_ms = run_control.hold_while_paused_durable(tenant_id, "agent_dispatch")
    else:
        paused_ms = run_control.hold_while_paused(tenant_id, "agent_dispatch")
    override = None
    try:
        from orchestrator.graph import get_pool

        with get_pool().connection() as conn:
            override = run_control.consume_override(
                conn,
                tenant_id=tenant_id,
                workflow_kind="agent_dispatch",
                step_name=step_name,
                run_id=run_id,
            )
    except Exception:  # noqa: BLE001 — control outage must not fail the work item (F9 spirit)
        logger.warning(
            "sales_recovery: override consume failed (step=%s run=%s) — proceeding without",
            step_name,
            run_id,
            exc_info=True,
        )
    merged = dict(base or {})
    if override is not None and override.pinned_input:
        entry = run_control.REGISTRY[("agent_dispatch", step_name)]
        merged = run_control.apply_pinned_input(entry, merged, override.pinned_input)
    if paused_ms or override is not None:
        _log_run_control(tenant_id, run_id, step_name, paused_ms, override)
    return merged


def _log_run_control(
    tenant_id: UUID, run_id: str, step_name: str, paused_ms: int, override: Any
) -> None:
    """Timeline substrate for the seam (IDs + counters only, CL-390) — one
    ``run_control_intervention`` pipeline_steps row with the mig-131 override_id /
    paused_ms COLUMNS set (B1 dead-columns fix). ``record_intervention`` itself
    never raises: a timeline miss must never alter control semantics."""
    from orchestrator.observability.pipeline_observability import record_intervention

    record_intervention(
        tenant_id,
        run_id,
        workflow_kind="agent_dispatch",
        step_name=step_name,
        override_id=override.id if override is not None else None,
        paused_ms=paused_ms or None,
        action="override_consumed" if override is not None else "released",
    )


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

    def _try_l3_arm(self, tenant_id: UUID, batch_id: UUID) -> bool:
        """VT-384 — attempt the L3 arm for a freshly-persisted batch. Returns True iff the batch
        was moved into the delivery-anchored hold (auto_send_pending), in which case the durable
        hold workflow is started and the caller MUST NOT run the L2 approval arm. Returns False on
        ANY refusal (non-L3 / frozen / always-confirm floor / CAS lost / notice-send failure) — the
        caller then falls back to the unchanged L2 arm. Never raises: an unexpected error logs and
        returns False (fail-closed to L2 — an owner-gated send is always the safe fallback).

        enter_l3_hold re-derives eligibility AT ARM TIME (autonomy L3 + not frozen + is_always_confirm
        FALSE), so a money-bearing / bulk / first-contact / novel batch can never flip to
        auto_send_pending here (CL-438 non-bypassable). It also sends the owner presend notice + sets
        presend_notice_sid; this then starts the hold (register_l3_hold ran at lifespan)."""
        from orchestrator.agents.l3_hold import (
            enter_l3_hold,
            start_l3_hold,
        )

        try:
            with tenant_connection(tenant_id) as conn:
                result = enter_l3_hold(tenant_id, batch_id, conn=conn)
        except Exception:  # noqa: BLE001 — a pre-flip arm error fails closed to the L2 owner-gated arm
            logger.exception(
                "sales_recovery: L3 arm errored batch=%s — falling back to L2 arm", batch_id
            )
            return False
        if not result.armed:
            logger.info(
                "sales_recovery: L3 arm declined batch=%s reason=%s — L2 fallback",
                batch_id, result.reason,
            )
            return False
        # The batch is now auto_send_pending (the flip + the presend notice both succeeded). The L2
        # fallback is NO LONGER safe (it would double-handle a batch already in the hold), so from
        # here we ALWAYS return True. start_l3_hold is idempotent on the batch-keyed workflow_id; a
        # start failure leaves the batch armed but the hold un-started — the owner-inbound demote /
        # kill paths still protect it, the C2 stop still blocks any send, and the next coordinator
        # sweep is the recovery seam. That residual is strictly safer than re-arming an L2 approval.
        try:
            start_l3_hold(str(tenant_id), str(batch_id))
        except Exception:  # noqa: BLE001 — armed-but-hold-unstarted: log; do NOT fall back to L2
            logger.exception(
                "sales_recovery: L3 hold start failed batch=%s (batch is armed auto_send_pending; "
                "hold un-started — recovered by the next sweep / protected by demote+C2)", batch_id
            )
        return True

    def execute_item(self, ctx: AgentItemContext) -> ItemExecutionResult:
        """Detect → bundle → draft (CL-425-gated LLM) → validate grounding → persist →
        arm Pillar-7. Returns IDs + counters ONLY (IDs-in-state). Outcomes:

        - ``cancelled`` + ``skipped_owner_inputs`` — consent gate tripped; no LLM transmit.
        - ``cancelled`` + ``skipped_not_onboarded`` — VT-421 onboarded gate tripped (tenant not
          fully onboarded): a clean NO-OP — 0 detect, 0 draft, 0 send. The DETECT-side
          short-circuit; the binding SEND boundary is Gate 0 in ``customer_send.agent_send_draft``.
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

        # VT-421 ACTIVATION gate (DETECT-side short-circuit) — SR runs ONLY for a tenant that has
        # crossed SR's registered activation bar (journey-complete + verified + data source +
        # customers; Fazal HALT 2026-06-25). This saves the detect/draft work for a non-activated
        # tenant; it is NOT the safety boundary (that is Gate 0 in customer_send.agent_send_draft,
        # which every L2/L3 send funnels through). Fail-closed (unknown/NULL/error → ineligible).
        # A cheap own RLS connection just for the read — NOT held across the candidate_build seam
        # pause below (a pause must never pin a pooled connection; the seam opens its own conn).
        from orchestrator.agents.onboarding_gate import tenant_is_sr_eligible

        with tenant_connection(tenant_id) as gate_conn:
            if not tenant_is_sr_eligible(tenant_id, conn=gate_conn):
                return ItemExecutionResult(
                    work_item_status="cancelled", counters={"skipped_not_onboarded": 1}
                )

        # VT-374 candidate_build seam — hold/consume BEFORE the tenant_connection opens
        # (a pause must never pin a pooled connection); 'limit' is the sole allow-listed pin.
        pins = _run_control_seam(
            tenant_id, ctx.run_id, "candidate_build", {"limit": DEFAULT_DETECTION_LIMIT}
        )
        with tenant_connection(tenant_id) as conn:
            candidates = detect_lapsed_customers(tenant_id, conn=conn, limit=int(pins["limit"]))
            bundles = [
                build_customer_fact_bundle(tenant_id, cand.customer_id, conn=conn)
                for cand in candidates
            ]
        if not candidates:
            return ItemExecutionResult(
                work_item_status="cancelled", counters={"skipped_no_candidates": 1}
            )

        # LLM phase — no DB connection held; each bundle is used for ONE call, then discarded.
        # VT-374 compose_drafts seam — whole-phase hold before any transmit; 'model' is the
        # sole allow-listed pin (an ops override can drop the drafting band for one run).
        compose_pins = _run_control_seam(
            tenant_id, ctx.run_id, "compose_drafts", {"model": _resolve_drafting_model()}
        )
        model = str(compose_pins["model"])
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

        # VT-374 persist_batch seam — hold BEFORE persist, never between persist and arm
        # (an unarmed awaiting_approval batch violates the _cancel_batch invariant, STEP-0).
        _run_control_seam(tenant_id, ctx.run_id, "persist_batch")
        with tenant_connection(tenant_id) as conn:
            batch_id = _persist_draft_batch(
                tenant_id, work_item_id=UUID(str(ctx.work_item_id)), drafts=grounded, conn=conn
            )

        counters = {"drafted": len(grounded), "dropped_ungrounded": dropped}

        # VT-384 — the L3 ARM (the orphaned wire, now connected). An L3-granted, non-frozen agent
        # routes the drafted batch into the delivery-anchored hold (enter_l3_hold) INSTEAD of the L2
        # approval arm. enter_l3_hold re-derives eligibility at arm time — autonomy L3 + not frozen
        # AND is_always_confirm FALSE (the money/bulk/first-contact/novel floor, CL-438
        # non-bypassable) — so a money-bearing batch can NEVER flip to auto_send_pending. ANY floor
        # trip (or a non-L3/frozen tenant) returns armed=False and we fall through to the unchanged
        # L2 arm. On a successful arm the durable hold workflow is started (register_l3_hold ran at
        # lifespan); the batch sits in auto_send_pending behind the presend notice + the C2 stop.
        l3_armed = self._try_l3_arm(tenant_id, batch_id)
        if l3_armed:
            logger.info(
                "sales_recovery: item=%s batch=%s L3-armed (auto_send_pending) drafted=%d",
                ctx.item_id, batch_id, len(grounded),
            )
            # The BATCH is now in 'auto_send_pending' (the delivery-anchored hold owns its
            # lifecycle). The WORK ITEM reports 'awaiting_approval' — the same valid terminal-ish
            # dispatch status the L2 arm reports (agent_work_items.status has no auto_send_pending
            # member, mig-125; the L3-vs-L2 distinction lives on the batch + the l3_armed counter,
            # not the work-item status). The hold workflow drives the batch to sent/demoted from here.
            return ItemExecutionResult(
                work_item_status="awaiting_approval",
                batch_id=str(batch_id),
                counters={**counters, "l3_armed": 1},
            )

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
    "MARKETING_CONSENT_VERSIONS",
    "MarketingConsentProdSafetyError",
    "WINBACK_TEMPLATE_NAME",
    "WINBACK_TEMPLATE_PARAMS",
    "CustomerFactBundle",
    "LapsedCandidate",
    "SalesRecoveryAgent",
    "build_customer_fact_bundle",
    "detect_lapsed_customers",
    "validate_draft_params",
]
