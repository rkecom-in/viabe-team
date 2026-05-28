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
from typing import Any
from uuid import UUID

from orchestrator.integrations.schemas import ConnectorSpec


class ConnectorBase(ABC):
    """Abstract base for concrete connector implementations.

    Each subclass implements the methods below against its vendor SDK.
    The class attribute ``connector_id`` must match a registry entry.
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


__all__ = ["ConnectorBase"]
