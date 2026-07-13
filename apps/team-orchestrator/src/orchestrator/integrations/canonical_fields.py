"""VT-209 — canonical field enum + hint registry.

Canonical fields are the typed columns every connector maps INTO. The
field-mapping reasoner (``field_mapping.py``) translates arbitrary
source columns to these via heuristic + LLM-assisted match.

Hint registry extends VT-205's per-connector ``canonical_fields_hints``
with global aliases (every connector inherits these in addition to its
spec-level hints).
"""

from __future__ import annotations

from typing import Literal, get_args


CanonicalField = Literal[
    "customer_name",
    "phone",
    "email",
    "order_amount",
    "order_date",
    "last_seen",
    "address",
    "tags",
]


# Global aliases per canonical field (case-insensitive, normalised). Each
# connector inherits these in addition to its spec-level
# ``canonical_fields_hints``. The heuristic matcher walks this dict +
# the spec's hints; first exact-case-fold or fuzzy match wins.
GLOBAL_FIELD_HINTS: dict[CanonicalField, list[str]] = {
    "customer_name": [
        "customer_name", "customer", "name", "full_name", "fullname",
        "first_name", "buyer_name", "client", "client_name",
    ],
    "phone": [
        "phone", "phone_number", "mobile", "mobile_number",
        "contact", "contact_number", "customer_phone", "buyer_phone",
        "whatsapp", "whatsapp_number", "tel", "telephone",
    ],
    "email": [
        "email", "email_address", "customer_email", "buyer_email",
        "e-mail", "mail",
    ],
    "order_amount": [
        "order_amount", "amount", "total", "total_amount", "total_price",
        "value", "order_value", "price", "subtotal", "grand_total",
    ],
    "order_date": [
        "order_date", "date", "created_at", "created", "ordered_at",
        "purchase_date", "transaction_date", "timestamp",
    ],
    "last_seen": [
        "last_seen", "last_visit", "last_touch", "last_touch_date",
        "last_active", "updated_at", "last_interaction",
    ],
    "address": [
        "address", "shipping_address", "billing_address", "street",
        "location",
    ],
    "tags": [
        "tags", "labels", "categories", "segment", "cohort",
    ],
}


# Human-friendly display labels for every canonical field a connector imports. Used by the
# connector first-contact FIELD-MAPPING answer (connector_first_contact.py) to enumerate exactly
# what a connector maps IN, in plain words — schema-truth, never fabricated. Keyed by
# ``CanonicalField`` so the display set can never silently drift from the closed enum: a new
# canonical field with no entry here trips ``canonical_field_display_list`` at call time.
CANONICAL_FIELD_DISPLAY: dict[CanonicalField, str] = {
    "customer_name": "customer name",
    "phone": "phone number",
    "email": "email",
    "order_amount": "order amount",
    "order_date": "order date",
    "last_seen": "last seen / last visit",
    "address": "address",
    "tags": "tags",
}


def canonical_field_display_list() -> list[str]:
    """The ordered, human-friendly labels of every canonical field a connector imports, derived
    FROM ``CanonicalField`` (declaration order) so it can never drift from the enum. Raises
    ``KeyError`` if a canonical field lacks a display label — a deliberate fail-loud so a new field
    is never silently dropped from the honesty answer."""
    return [CANONICAL_FIELD_DISPLAY[f] for f in get_args(CanonicalField)]


__all__ = [
    "CanonicalField",
    "GLOBAL_FIELD_HINTS",
    "CANONICAL_FIELD_DISPLAY",
    "canonical_field_display_list",
]
