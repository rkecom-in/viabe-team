"""VT-616 — deterministic near-duplicate backstop (dispatch._reply_repeats_recent).

The advisory anti-repeat prompt rule + in-flight-state block do not stop the haiku hot tier
re-emitting a near-verbatim prior reply under repeat pressure. `_reply_repeats_recent` is the
hard backstop: it flags a composed reply that near-duplicates a recent assistant turn so
`_maybe_send_manager_reply` recomposes before transmitting. These are pure-logic units (the
DB read `active_window` is monkeypatched) — the send-path integration is validated on dev.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

# dispatch imports langchain_anthropic at module top; the dep-less CI 'test'/pre-push smoke runs
# WITHOUT heavy deps, so guard the whole module (skip there, run in the full suite). [depless-smoke-trap]
pytest.importorskip("langchain_anthropic")

from orchestrator.agent import dispatch  # noqa: E402 — must follow the importorskip guard

_TID = uuid4()

# A realistic long reply (the impatient_repeat step-0 answer, trimmed).
_LONG = (
    "I appreciate the question — you're right to ask, because understanding how this works "
    "matters. Here's the straight version: we help you grow sales and run things more smoothly. "
    "We connect to your customer data and read what's happening in your business."
)


def _patch_window(monkeypatch, turns):
    """Point dispatch's lazy `active_window` import at a stub returning `turns`."""
    import orchestrator.conversation_log as clog

    monkeypatch.setattr(clog, "active_window", lambda *a, **k: turns)


def test_normalize_collapses_case_and_whitespace():
    assert dispatch._normalize_reply("  Hello   WORLD\n") == "hello world"


def test_byte_identical_reply_is_flagged(monkeypatch):
    _patch_window(monkeypatch, [{"role": "assistant", "text": _LONG}])
    assert dispatch._reply_repeats_recent(_TID, _LONG) is True


def test_case_whitespace_variant_is_flagged(monkeypatch):
    _patch_window(monkeypatch, [{"role": "assistant", "text": _LONG}])
    variant = ("  " + _LONG.upper()).replace("  ", " ")
    assert dispatch._reply_repeats_recent(_TID, variant) is True


def test_long_reply_vs_truncated_prior_is_flagged(monkeypatch):
    # VT-621: record_turn caps stored turns at _TEXT_CAP (4096 chars since VT-625), but the candidate here
    # is the FULL untruncated reply. A byte-identical repeat whose STORED copy was truncated must still be
    # flagged. Pre-fix, difflib compared full-vs-truncated → ratio fell below 0.90 for any reply past
    # the cap (measured on dev: 1592 vs 995 = 0.77), so long verbatim repeats slipped through and the
    # manager shipped dupes. The common-prefix comparison catches it. Fixture exceeds 4096 so the
    # truncation path is still exercised at the current cap.
    full = (_LONG + " ") * 20  # ~5200 chars, over the 4096-char storage cap
    truncated_prior = full[:4096]  # what record_turn actually persists
    _patch_window(monkeypatch, [{"role": "assistant", "text": truncated_prior}])
    assert dispatch._reply_repeats_recent(_TID, full) is True


def test_genuinely_different_reply_not_flagged(monkeypatch):
    _patch_window(monkeypatch, [{"role": "assistant", "text": _LONG}])
    other = "Sure — what city is your business in? We have Surat on file, is that right?"
    assert dispatch._reply_repeats_recent(_TID, other) is False


def test_short_ack_never_flagged(monkeypatch):
    # Even against an identical short prior — a brief ack can legitimately recur.
    _patch_window(monkeypatch, [{"role": "assistant", "text": "haan, ho gaya done"}])
    assert dispatch._reply_repeats_recent(_TID, "haan, ho gaya done") is False


def test_owner_turn_is_ignored(monkeypatch):
    # A near-identical OWNER turn must not trip the guard (only prior ASSISTANT replies count).
    _patch_window(monkeypatch, [{"role": "owner", "text": _LONG}])
    assert dispatch._reply_repeats_recent(_TID, _LONG) is False


def test_no_recent_turns_not_flagged(monkeypatch):
    _patch_window(monkeypatch, [])
    assert dispatch._reply_repeats_recent(_TID, _LONG) is False


def test_read_error_is_fail_safe(monkeypatch):
    # A window-read exception must never block the reply → returns False (do NOT treat as dup).
    import orchestrator.conversation_log as clog

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(clog, "active_window", _boom)
    assert dispatch._reply_repeats_recent(_TID, _LONG) is False
