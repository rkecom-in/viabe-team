"""VT-209 — canonical field enum + hint registry.

Canonical fields are the typed columns every connector maps INTO. The
field-mapping reasoner (``field_mapping.py``) translates arbitrary
source columns to these via heuristic + LLM-assisted match.

Hint registry extends VT-205's per-connector ``canonical_fields_hints``
with global aliases (every connector inherits these in addition to its
spec-level hints).
"""

from __future__ import annotations

from typing import Literal


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


__all__ = ["CanonicalField", "GLOBAL_FIELD_HINTS"]
