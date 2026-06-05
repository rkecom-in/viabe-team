"""VT-84 — tenant-scoped customer resolution for the owner exclusion handler.

Phone-exact wins; else fuzzy display_name (difflib > 0.6): 0 -> None, 1 -> that id,
MANY -> ambiguous (the caller opens a clarification; NEVER auto-pick on ambiguity —
Pillar 7). All DB access goes through CustomersWrapper (the gate-sanctioned tenant-scoped
path — never raw SQL on the customers hot table).
"""

from __future__ import annotations

import difflib
from typing import NamedTuple
from uuid import UUID

from orchestrator.db.wrappers import CustomersWrapper

_FUZZY_THRESHOLD = 0.7  # conservative — a loose threshold risks the wrong customer (ambiguous -> ask)


class CustomerMatch(NamedTuple):
    customer_id: UUID | None
    ambiguous: bool  # True -> multiple fuzzy matches; the caller must clarify, not pick


def resolve_customer(
    tenant_id: UUID | str,
    *,
    phone_e164: str | None = None,
    name: str | None = None,
) -> CustomerMatch:
    """Resolve a customer for an owner exclusion. Phone-exact first; else fuzzy name."""
    wrapper = CustomersWrapper()

    if phone_e164:
        rows = wrapper.find_by_phone(tenant_id, phone_e164)
        return CustomerMatch(UUID(str(rows[0]["id"])), False) if rows else CustomerMatch(None, False)

    if name:
        target = name.strip().casefold()
        if not target:
            return CustomerMatch(None, False)
        qtokens = set(target.split())
        matches: list[UUID] = []
        for row in wrapper.list_id_and_display_name(tenant_id):
            display = str(row.get("display_name") or "").casefold()
            if not display:
                continue
            # All query tokens present (a first name inside a full name: "Rajesh" ->
            # "Rajesh Kumar"), OR a close full-string ratio (typo tolerance). The
            # token-subset path avoids the loose-ratio false positives a bare 0.6 ratio
            # would admit, while still catching first-name references.
            dtokens = set(display.split())
            if (qtokens and qtokens <= dtokens) or (
                difflib.SequenceMatcher(None, target, display).ratio() > _FUZZY_THRESHOLD
            ):
                matches.append(UUID(str(row["id"])))
        if len(matches) == 1:
            return CustomerMatch(matches[0], False)
        if len(matches) > 1:
            return CustomerMatch(None, True)  # ambiguous -> caller opens a clarification
        return CustomerMatch(None, False)

    return CustomerMatch(None, False)
