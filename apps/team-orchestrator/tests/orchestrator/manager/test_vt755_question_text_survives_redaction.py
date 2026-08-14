"""VT-755 scope 0a — an owner question must survive redaction as READABLE TEXT.

THE DEFECT, measured on deployed dev 2026-08-14: **3 of 4 open `pending_questions` had their entire
body replaced with `<body:hash:…>`.** `_redact_text` called `redact()` with its default
`hash_long_body=True`, which swaps any string over 200 chars wholesale for a hash token. That rule
exists to bound LOG and span size — but a pending question is not a log line, it is text destined for
the owner. A question stored as a hash is unsendable at rest, so it could never be delivered even once
it has a delivery path (VT-755 scope 0).

Only questions under 200 chars survived, which is why the defect looked intermittent: the same code
path produced a readable question on one tenant and a hash on three others purely by length.

The fix is the opt-out VT-632 already established for owner-facing sends (`reply_to_owner.py:85`,
`embeddings.py:100`). These tests pin BOTH halves — the text survives AND the PII protection does not
regress, because "make the question readable" must never become "leak a phone number".
"""

from __future__ import annotations

import pytest

# The dep-less CI smoke job (uv --no-project --isolated) installs only pytest + pyyaml, and importing
# `pending_questions` pulls db.tenant_connection -> psycopg. Without this guard the whole FILE errors at
# COLLECTION there and the push is rejected — the trap that has bitten this repo before. These are pure
# redaction assertions and need no database; the skip only covers the import chain.
pytest.importorskip("psycopg")

from orchestrator.manager import pending_questions as pq  # noqa: E402 — after the dependency guard

_LONG_PREFIX = (
    "I couldn't build the win-back campaign yet because some information is missing, and I want to "
    "make sure I get this right before I message anyone on your behalf, so could you help me with "
    "the following detail before I go ahead and prepare the draft for your approval"
)


def test_a_long_question_is_no_longer_replaced_by_a_hash():
    """THE DEFECT. Over 200 chars, so the old default hashed the whole body."""
    text = _LONG_PREFIX + " — which offer would you like me to make?"
    assert len(text) > 200, "the fixture must exceed _LONG_BODY_THRESHOLD or it tests nothing"

    out = pq._redact_text(text)

    assert "<body:hash:" not in out, (
        "the question was replaced wholesale by a hash token — unsendable at rest. This is the state "
        "3 of 4 open questions were in on dev."
    )
    assert "win-back campaign" in out, "the question must survive as readable text"
    assert "which offer would you like me to make?" in out, (
        "the ASK is the part the owner needs; losing the tail is as bad as hashing the whole thing"
    )


def test_a_short_question_still_survives_unchanged():
    """Guards the other direction: short questions worked before and must keep working."""
    text = "Which offer would you like me to make to your lapsed customers?"
    assert len(text) <= 200
    assert pq._redact_text(text) == text


@pytest.mark.parametrize(
    "secret,label",
    [
        ("+919321553267", "E.164 phone"),
        ("9321553267", "Indian 10-digit phone"),
        ("owner@example.com", "email"),
        ("ABCDE1234F", "PAN"),
        ("27AAPFU0939F1ZV", "GSTIN"),
    ],
)
def test_pii_is_STILL_redacted_inside_a_long_question(secret: str, label: str):
    """The half that must not regress. Skipping the whole-body hash is not skipping redaction — the
    pattern substitutions ARE the PII protection here, exactly as `_redact_str`'s docstring says.

    If this ever fails, the VT-755 fix has traded an undeliverable question for a leaked identifier,
    which is a strictly worse bargain. CL-390: nothing raw is persisted.
    """
    text = f"{_LONG_PREFIX} — should I contact them on {secret} instead?"
    assert len(text) > 200

    out = pq._redact_text(text)

    assert secret not in out, f"{label} survived redaction inside a long question — PII leak"
    assert "<body:hash:" not in out, "and it must still not be hashed wholesale"
    assert "win-back campaign" in out, "and the question must still be readable"


def test_redaction_is_still_idempotent_on_its_own_output():
    """`_redact_str` promises idempotency and the tokens are designed not to re-match. Pinned here
    because `ask()` and `correlate_reply()` both redact, so a question can be passed through twice."""
    text = f"{_LONG_PREFIX} — reach them at +919321553267?"
    once = pq._redact_text(text)
    assert pq._redact_text(once) == once


def test_the_opt_out_is_the_documented_owner_facing_one():
    """Pins WHY, not just what. If someone reverts to the default they should have to delete this
    test and read the reason first — the same opt-out VT-632 gave every other owner-facing send."""
    import inspect

    src = inspect.getsource(pq._redact_text)
    assert "hash_long_body=False" in src
    assert "VT-755" in src, "the reason must stay next to the call"
