"""The one E.164 shape predicate — shared by the transport backstop and the sender resolver.

VT-487 put a structural E.164 assertion at the Twilio transport (`twilio_send._assert_e164`),
after a phone stored as a NUMBER rendered to scientific notation ("+91998886e+11") and Twilio
rejected six live sends with 21211.

VT-742 needs the same shape check one layer earlier: `resolve_sender` reads a sending number out
of the DATABASE, and a malformed value there must be refused at RESOLUTION, not discovered at the
transport — by then the choice of sender has already been made and a fail-closed refusal is
indistinguishable from a bad recipient.

Two modules needing the same rule is exactly how a second, drifting regex gets written. This module
owns the pattern and nothing else, so it can be imported from anywhere without a cycle (the transport
raises ``BlockedRecipientError``, the resolver raises ``SenderUnresolvable`` — the ERROR is the
caller's business, the SHAPE is not).
"""

from __future__ import annotations

import re

# A leading '+', a non-zero country-code digit, then 7..14 more digits (8..15 total — the ITU
# E.164 maximum). Anchored at both ends: no leading/trailing junk, no embedded 'e' from a float
# artifact, no whatsapp: scheme (strip that before calling).
E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def is_e164(number: str | None) -> bool:
    """True iff ``number`` is a well-formed bare E.164 string. None/empty is False, never a raise."""
    return bool(number) and E164_RE.match(number) is not None


__all__ = ["E164_RE", "is_e164"]
