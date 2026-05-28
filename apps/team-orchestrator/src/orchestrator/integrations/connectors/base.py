"""VT-205 — Abstract ``ConnectorBase`` interface.

Concrete connectors (VT-207 google_sheet, VT-208 shopify, etc.)
subclass this. The Integration Agent (VT-206) only interacts with
``ConnectorSpec`` entries from the registry + the standard
``pull_sample`` / ``dedupe_against_existing`` tool surface — it does
NOT call ConnectorBase directly. That surface lives behind the agent's
tool implementations (separate VT-N rows).

Per VT-205 brief AC-1: registry helpers `get_connector` /
`list_connectors`. ConnectorBase is the substrate that future
implementations attach to; this row only declares the contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
from uuid import UUID

from orchestrator.integrations.schemas import ConnectorSpec


class ConnectorBase(ABC):
    """Abstract base for concrete connector implementations.

    Each subclass implements the methods below against its vendor SDK.
    The class attribute ``connector_id`` must match a registry entry.

    VT-210 extension: ``pull_full``, ``parse_push_payload``, and
    ``verify_push_signature`` were promoted from ad-hoc connector
    helpers into the base contract so the generic scheduler + push
    receiver can drive every connector uniformly. Connectors that do
    not support a path (e.g. pull-only or push-only) raise
    ``NotImplementedError`` from the relevant method; the spec's
    ``push_supported`` field is the prior gate, so a properly-typed
    pull-only connector is never invoked on the push path.
    """

    connector_id: str = ""

    @property
    @abstractmethod
    def spec(self) -> ConnectorSpec:
        """Return the registry spec for this connector (immutable)."""

    @abstractmethod
    def start_auth(self, tenant_id: UUID) -> dict[str, Any]:
        """Begin the auth flow. Returns an envelope describing the next
        action the agent should take (e.g., display walkthrough URL,
        prompt for API key, etc.).
        """

    @abstractmethod
    def complete_auth(
        self, tenant_id: UUID, auth_payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Finalise auth — store credentials, return success metadata."""

    @abstractmethod
    def pull_sample(self, tenant_id: UUID) -> list[dict[str, Any]]:
        """Fetch the first ~50 rows for field-mapping confirmation."""

    def pull_full(
        self, tenant_id: UUID, since: datetime | None = None
    ) -> list[Any]:
        """Incremental full pull. Default raises; override per connector."""
        raise NotImplementedError(
            f"{self.connector_id}: pull_full not implemented"
        )

    def parse_push_payload(self, body: bytes) -> list[dict[str, Any]]:
        """Decode push webhook body into canonical row dicts.

        Default raises; override on push-supported connectors. The
        scheduler / generic push receiver routes through this so the
        same row-canonicalisation path runs whether the row arrived
        via pull or push.
        """
        raise NotImplementedError(
            f"{self.connector_id}: parse_push_payload not implemented"
        )

    def verify_push_signature(
        self, body: bytes, headers: dict[str, str], push_secret: str
    ) -> bool:
        """Verify a push webhook's authenticity. Default raises.

        Push-supported connectors override with their vendor's HMAC /
        shared-secret scheme. Returning False means reject; True means
        accept and continue. Raising indicates a programming bug, not
        a tampering attempt.
        """
        raise NotImplementedError(
            f"{self.connector_id}: verify_push_signature not implemented"
        )


__all__ = ["ConnectorBase"]
