"""S2 abandoned-checkout recovery: source-neutral, deterministic pre-send core.

This module deliberately has no database, transport, Twilio, customer-send, or ads dependency.
Adapters normalise source records into :class:`CheckoutAttempt`; deterministic code decides whether
an attempt is eligible and validates any drafted template parameters against frozen source facts.

The live persistence, delayed wake, owner-approval arm and eventual send bind outside this module.
The send is always downstream through the platform's generalised ``agent_send_draft`` rail; this
module cannot import or invoke that rail.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from orchestrator.agent.tool_guardrail import assert_agent_tools_safe


AGENT_NAME = "abandoned_checkout_recovery"
CHECKOUT_RECOVERY_PURPOSE = "checkout_recovery"
ROUTING_FLAG = "TEAM_S2_ABANDONED_CHECKOUT_ROUTING_ENABLED"
TEMPLATE_NAME_EN = "team_checkout_recovery_simple_en"
TEMPLATE_NAME_HI = "team_checkout_recovery_simple_hi"
TEMPLATE_PARAMS = ("customer_name", "business_name", "recovery_link")

# Structural sendlessness at import. Any future tool addition is denied before the process boots.
AGENT_TOOLS: tuple[Any, ...] = ()
assert_agent_tools_safe(AGENT_TOOLS, surface="agents.abandoned_checkout_recovery")


class CheckoutSourceKind(str, Enum):
    SHOPIFY = "shopify"
    VIABE_REPORTS = "reports_funnel"


class CheckoutSourceError(ValueError):
    """A source record is incomplete, ambiguous, or unsafe to normalise."""


@dataclass(frozen=True, slots=True)
class CheckoutAttempt:
    """The complete source-neutral fact set consumed by cohort selection.

    ``contact_token`` and ``destination_ref`` are already protected values. Raw phone numbers,
    checkout URLs, addresses, line-item prose and source payloads do not cross this boundary.
    """

    tenant_id: UUID
    source: CheckoutSourceKind
    attempt_id: str
    attempt_version: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    total_paise: int
    currency: str
    item_count: int
    contact_token: str
    destination_ref: str
    evidence_ref: str

    @property
    def key(self) -> tuple[UUID, str, str, str]:
        return (self.tenant_id, self.source.value, self.attempt_id, self.attempt_version)


@dataclass(frozen=True, slots=True)
class CommerceConsentSnapshot:
    tenant_id: UUID
    contact_token: str
    channel: str
    purpose: str
    notice_version: str
    affirmative_at: datetime
    state: str
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class CustomerSafetySnapshot:
    tenant_id: UUID
    contact_token: str
    subscribed: bool
    globally_opted_out: bool
    complaint_blocked: bool


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    attempt: CheckoutAttempt
    consent_evidence_ref: str


@dataclass(frozen=True, slots=True)
class RecoveryFactBundle:
    attempt_key: tuple[UUID, str, str, str]
    customer_name: str
    business_name: str
    cart_value: str
    item_count: int
    recovery_link: str


class CheckoutSignalSource(Protocol):
    """One source adapter. Implementations may read APIs, verified webhooks, or governed exports."""

    source_kind: CheckoutSourceKind

    def read_attempts(self, tenant_id: UUID, *, as_of: datetime) -> Sequence[CheckoutAttempt]: ...


RawReader = Callable[[UUID, datetime], Iterable[Mapping[str, Any]]]
PhoneTokenizer = Callable[[str], str]
DestinationProtector = Callable[[str], str]


def _required_text(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if value is None or not str(value).strip():
        raise CheckoutSourceError(f"missing required source field {key!r}")
    return str(value).strip()


def _time(value: Any, *, field: str, allow_none: bool = False) -> datetime | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise CheckoutSourceError(f"missing required timestamp {field!r}")
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise CheckoutSourceError(f"invalid timestamp {field!r}") from exc
    if parsed.tzinfo is None:
        raise CheckoutSourceError(f"timestamp {field!r} must carry a timezone")
    return parsed.astimezone(UTC)


def _paise(value: Any, *, field: str) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise CheckoutSourceError(f"invalid money field {field!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise CheckoutSourceError(f"money field {field!r} must be finite and non-negative")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _positive_count(value: Any, *, field: str) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise CheckoutSourceError(f"invalid count field {field!r}") from exc
    if count < 1:
        raise CheckoutSourceError(f"count field {field!r} must be at least one")
    return count


def _validate_attempt(attempt: CheckoutAttempt) -> CheckoutAttempt:
    if attempt.updated_at < attempt.created_at:
        raise CheckoutSourceError("updated_at predates created_at")
    if attempt.completed_at is not None and attempt.completed_at < attempt.created_at:
        raise CheckoutSourceError("completed_at predates created_at")
    if attempt.currency != "INR":
        raise CheckoutSourceError("S2 launch admits INR checkout attempts only")
    for label, value in (
        ("attempt_id", attempt.attempt_id),
        ("attempt_version", attempt.attempt_version),
        ("contact_token", attempt.contact_token),
        ("destination_ref", attempt.destination_ref),
        ("evidence_ref", attempt.evidence_ref),
    ):
        if not value.strip():
            raise CheckoutSourceError(f"{label} must be non-empty")
    return attempt


class ShopifyAbandonedCheckoutSource:
    """Normalise governed Shopify reads/webhook projections without retaining raw Shopify data."""

    source_kind = CheckoutSourceKind.SHOPIFY

    def __init__(
        self,
        *,
        reader: RawReader,
        tokenise_phone: PhoneTokenizer,
        protect_destination: DestinationProtector,
    ) -> None:
        self._reader = reader
        self._tokenise_phone = tokenise_phone
        self._protect_destination = protect_destination

    def read_attempts(self, tenant_id: UUID, *, as_of: datetime) -> Sequence[CheckoutAttempt]:
        attempts: list[CheckoutAttempt] = []
        for row in self._reader(tenant_id, as_of):
            customer = row.get("customer") if isinstance(row.get("customer"), Mapping) else {}
            phone = str(row.get("phone") or customer.get("phone") or "").strip()
            if not phone:
                raise CheckoutSourceError("Shopify attempt has no phone for tokenisation")
            line_items = row.get("line_items")
            item_count = len(line_items) if isinstance(line_items, Sequence) else row.get("item_count")
            created_at = _time(row.get("created_at"), field="created_at")
            updated_at = _time(row.get("updated_at"), field="updated_at")
            completed_at = _time(row.get("completed_at"), field="completed_at", allow_none=True)
            assert created_at is not None and updated_at is not None
            attempt = CheckoutAttempt(
                tenant_id=tenant_id,
                source=self.source_kind,
                attempt_id=_required_text(row, "id"),
                attempt_version=_required_text(row, "attempt_version"),
                created_at=created_at,
                updated_at=updated_at,
                completed_at=completed_at,
                total_paise=_paise(row.get("total_price"), field="total_price"),
                currency=_required_text(row, "currency").upper(),
                item_count=_positive_count(item_count, field="item_count"),
                contact_token=self._tokenise_phone(phone),
                destination_ref=self._protect_destination(
                    _required_text(row, "abandoned_checkout_url")
                ),
                evidence_ref=_required_text(row, "evidence_ref"),
            )
            attempts.append(_validate_attempt(attempt))
        return tuple(attempts)


class ReportsFunnelSource:
    """Normalise CC's Reports bridge records; the bridge must already protect contact/destination."""

    source_kind = CheckoutSourceKind.VIABE_REPORTS

    def __init__(self, *, reader: RawReader) -> None:
        self._reader = reader

    def read_attempts(self, tenant_id: UUID, *, as_of: datetime) -> Sequence[CheckoutAttempt]:
        attempts: list[CheckoutAttempt] = []
        for row in self._reader(tenant_id, as_of):
            clicked_at = _time(row.get("clicked_at"), field="clicked_at")
            updated_at = _time(row.get("source_updated_at"), field="source_updated_at")
            purchased_at = _time(row.get("purchased_at"), field="purchased_at", allow_none=True)
            assert clicked_at is not None and updated_at is not None
            # A daily export must say which snapshot produced the row. A click event may use its
            # immutable event id as the version. Missing either is quarantined, never synthesized.
            version = str(row.get("attempt_version") or row.get("export_snapshot_id") or "").strip()
            if not version:
                raise CheckoutSourceError(
                    "Reports attempt requires attempt_version or export_snapshot_id"
                )
            attempt = CheckoutAttempt(
                tenant_id=tenant_id,
                source=self.source_kind,
                attempt_id=_required_text(row, "checkout_attempt_id"),
                attempt_version=version,
                created_at=clicked_at,
                updated_at=updated_at,
                completed_at=purchased_at,
                total_paise=_paise(row.get("amount_inr"), field="amount_inr"),
                currency=_required_text(row, "currency").upper(),
                item_count=_positive_count(row.get("item_count"), field="item_count"),
                contact_token=_required_text(row, "contact_token"),
                destination_ref=_required_text(row, "destination_ref"),
                evidence_ref=_required_text(row, "evidence_url_or_export_ref"),
            )
            attempts.append(_validate_attempt(attempt))
        return tuple(attempts)


def build_recovery_cohort(
    attempts: Iterable[CheckoutAttempt],
    *,
    tenant_id: UUID,
    now: datetime,
    abandonment_delay: timedelta | None,
    allowed_notice_versions: frozenset[str],
    consents: Mapping[str, CommerceConsentSnapshot],
    safety: Mapping[str, CustomerSafetySnapshot],
    already_contacted: frozenset[tuple[UUID, str, str, str]],
) -> tuple[RecoveryCandidate, ...]:
    """Return the deterministic S2 cohort; unset activation values fail closed to an empty set."""

    if abandonment_delay is None or abandonment_delay <= timedelta(0):
        return ()
    if not allowed_notice_versions:
        return ()
    now_utc = _time(now, field="now")
    assert now_utc is not None
    selected: list[RecoveryCandidate] = []
    seen: set[tuple[UUID, str, str, str]] = set()
    for attempt in attempts:
        if attempt.tenant_id != tenant_id or attempt.key in seen:
            continue
        seen.add(attempt.key)
        if attempt.completed_at is not None or attempt.key in already_contacted:
            continue
        if attempt.updated_at + abandonment_delay > now_utc:
            continue
        consent = consents.get(attempt.contact_token)
        customer = safety.get(attempt.contact_token)
        if consent is None or customer is None:
            continue
        if consent.tenant_id != tenant_id or customer.tenant_id != tenant_id:
            continue
        if (
            consent.channel != "whatsapp"
            or consent.purpose != CHECKOUT_RECOVERY_PURPOSE
            or consent.state != "active"
            or consent.notice_version not in allowed_notice_versions
            or consent.affirmative_at > now_utc
        ):
            continue
        if not customer.subscribed or customer.globally_opted_out or customer.complaint_blocked:
            continue
        selected.append(
            RecoveryCandidate(
                attempt=attempt,
                consent_evidence_ref=consent.evidence_ref,
            )
        )
    return tuple(selected)


def freeze_recovery_facts(
    candidate: RecoveryCandidate,
    *,
    customer_name: str | None,
    business_name: str,
    recovery_link: str,
) -> RecoveryFactBundle:
    """Create the only values a template-param drafter may return."""

    display_name = (customer_name or "there").strip() or "there"
    merchant = business_name.strip()
    link = recovery_link.strip()
    if not merchant or not link:
        raise ValueError("business_name and recovery_link must be non-empty")
    attempt = candidate.attempt
    return RecoveryFactBundle(
        attempt_key=attempt.key,
        customer_name=display_name,
        business_name=merchant,
        cart_value=f"₹{attempt.total_paise / 100:,.2f}",
        item_count=attempt.item_count,
        recovery_link=link,
    )


def validate_template_params(
    bundle: RecoveryFactBundle,
    params: Mapping[str, Any],
) -> tuple[str, str, str]:
    """Drop, never repair, a draft that departs from its frozen parameter menu."""

    if tuple(params.keys()) != TEMPLATE_PARAMS:
        raise ValueError(f"template params must be exactly {TEMPLATE_PARAMS!r} in order")
    expected = {
        "customer_name": bundle.customer_name,
        "business_name": bundle.business_name,
        "recovery_link": bundle.recovery_link,
    }
    if any(str(params[key]) != expected[key] for key in TEMPLATE_PARAMS):
        raise ValueError("template params contain a value outside the frozen fact bundle")
    return tuple(expected[key] for key in TEMPLATE_PARAMS)  # type: ignore[return-value]


__all__ = [
    "AGENT_NAME",
    "AGENT_TOOLS",
    "CHECKOUT_RECOVERY_PURPOSE",
    "ROUTING_FLAG",
    "TEMPLATE_NAME_EN",
    "TEMPLATE_NAME_HI",
    "TEMPLATE_PARAMS",
    "CheckoutAttempt",
    "CheckoutSignalSource",
    "CheckoutSourceError",
    "CheckoutSourceKind",
    "CommerceConsentSnapshot",
    "CustomerSafetySnapshot",
    "RecoveryCandidate",
    "RecoveryFactBundle",
    "ReportsFunnelSource",
    "ShopifyAbandonedCheckoutSource",
    "build_recovery_cohort",
    "freeze_recovery_facts",
    "validate_template_params",
]
